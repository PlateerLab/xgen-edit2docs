"""Job service: queue, status, events.

This layer hides the storage backend (Redis via arq) and the DB persistence
from the rest of the application. Callers ask for "give me a job id and run
this work in the background"; subscribers ask for "stream me the events for
this job id".

Architecture:
    enqueue()        — creates a DB Job row, pushes onto Redis queue
    record_event()   — appends a JobEvent row + publishes on a Redis pub/sub
                       channel (worker -> API server fan-out)
    stream_events()  — async generator that yields stored events + tails new
                       ones via the pub/sub channel
    get_status()     — returns the current Job row

Tests use FakeJobBus (also in this module) which keeps everything in-memory.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings, get_settings
from ..db.models import Job, JobEvent, JobEventType, JobKind, JobStatus, Tenant


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class JobView:
    """Minimal projection of the Job row for API responses."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    params: dict
    cost: dict
    result: dict
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_row(cls, job: Job) -> "JobView":
        return cls(
            id=job.id,
            tenant_id=job.tenant_id,
            kind=job.kind,
            status=job.status,
            params=dict(job.params or {}),
            cost=dict(job.cost or {}),
            result=dict(job.result or {}),
            error_message=job.error_message,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


@dataclass
class JobEventEnvelope:
    """An event as delivered to subscribers."""

    job_id: uuid.UUID
    type: JobEventType
    payload: dict
    created_at: datetime

    def to_jsonable(self) -> dict:
        return {
            "job_id": str(self.job_id),
            "type": self.type.value,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


class JobNotFound(Exception):
    status_code = 404
    code = "JOB_NOT_FOUND"

    def __init__(self, job_id: uuid.UUID):
        super().__init__(f"job {job_id} not found")
        self.job_id = job_id


# ---------------------------------------------------------------------------
# Pub/sub bus — abstract + Redis + fake
# ---------------------------------------------------------------------------


class JobBus(Protocol):
    """Worker -> API fan-out channel. One implementation is Redis; the other
    is in-memory for tests."""

    async def publish(self, job_id: uuid.UUID, event: JobEventEnvelope) -> None: ...

    async def subscribe(self, job_id: uuid.UUID) -> AsyncIterator[JobEventEnvelope]:  # type: ignore[override]
        """Yield events as they arrive. Must be cancelable."""
        if False:  # pragma: no cover - protocol stub
            yield  # type: ignore[unreachable]


class FakeJobBus:
    """In-memory JobBus for tests + single-process dev. Backed by asyncio.Queue
    so multiple subscribers can attach to the same job."""

    def __init__(self) -> None:
        self._subscribers: dict[uuid.UUID, list[asyncio.Queue[JobEventEnvelope | None]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, job_id: uuid.UUID, event: JobEventEnvelope) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(job_id, ()))
        for q in queues:
            await q.put(event)

    async def close(self, job_id: uuid.UUID) -> None:
        """Signal subscribers that no more events are coming."""
        async with self._lock:
            queues = list(self._subscribers.get(job_id, ()))
        for q in queues:
            await q.put(None)

    @asynccontextmanager
    async def subscriber(self, job_id: uuid.UUID) -> "AsyncIterator[asyncio.Queue[JobEventEnvelope | None]]":
        q: asyncio.Queue[JobEventEnvelope | None] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
        try:
            yield q
        finally:
            async with self._lock:
                if job_id in self._subscribers and q in self._subscribers[job_id]:
                    self._subscribers[job_id].remove(q)
                if not self._subscribers.get(job_id):
                    self._subscribers.pop(job_id, None)


class RedisJobBus:
    """Redis-backed JobBus using pub/sub.

    Channel naming: xgen_edit2docs:job:<job_id>:events
    Payload: JSON-serialized JobEventEnvelope.
    """

    CHANNEL_PREFIX = "xgen_edit2docs:job:"

    def __init__(self, redis_url: str):
        self._url = redis_url
        self._client = None  # lazy

    def _channel(self, job_id: uuid.UUID) -> str:
        return f"{self.CHANNEL_PREFIX}{job_id}:events"

    async def _ensure_client(self):
        if self._client is None:
            import redis.asyncio as redis_async  # local import

            self._client = redis_async.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, job_id: uuid.UUID, event: JobEventEnvelope) -> None:
        client = await self._ensure_client()
        await client.publish(self._channel(job_id), json.dumps(event.to_jsonable()))

    @asynccontextmanager
    async def subscriber(self, job_id: uuid.UUID) -> AsyncIterator:
        client = await self._ensure_client()
        pubsub = client.pubsub()
        await pubsub.subscribe(self._channel(job_id))
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe(self._channel(job_id))
            await pubsub.close()


# ---------------------------------------------------------------------------
# Pluggable bus accessor
# ---------------------------------------------------------------------------


_default_bus: JobBus | None = None


def set_default_bus(bus: JobBus | None) -> None:
    """Tests / dev mode: swap in a custom bus (e.g. FakeJobBus)."""
    global _default_bus
    _default_bus = bus


def get_default_bus() -> JobBus:
    if _default_bus is not None:
        return _default_bus
    settings = get_settings()
    return RedisJobBus(settings.redis_url)


# ---------------------------------------------------------------------------
# Job enqueue + status (DB-backed)
# ---------------------------------------------------------------------------


async def enqueue_job(
    *,
    session: AsyncSession,
    tenant: Tenant,
    kind: JobKind,
    params: dict,
    project_id: uuid.UUID | None = None,
    arq_pool=None,
) -> Job:
    """Persist a Job row and (optionally) push it onto the arq worker queue.

    `arq_pool` is the result of `arq.create_pool(settings)`. When None we just
    record the row — useful in tests and in the synchronous worker mode where
    a separate process polls the DB instead of Redis.
    """
    job = Job(
        tenant_id=tenant.id,
        project_id=project_id,
        kind=kind,
        status=JobStatus.queued,
        params=params,
    )
    session.add(job)
    await session.flush()  # populate job.id

    if arq_pool is not None:
        # Worker entry name matches Worker.functions in workers/main.py.
        await arq_pool.enqueue_job(
            "run_job",
            str(job.id),
            _job_id=f"xgen_edit2docs:{job.id}",
        )

    return job


async def get_job(
    *,
    session: AsyncSession,
    tenant: Tenant,
    job_id: uuid.UUID,
) -> Job:
    stmt = select(Job).where(Job.id == job_id, Job.tenant_id == tenant.id)
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise JobNotFound(job_id)
    return job


# ---------------------------------------------------------------------------
# Event persistence (worker side) + replay (API side)
# ---------------------------------------------------------------------------


async def record_event(
    *,
    session: AsyncSession,
    bus: JobBus,
    job_id: uuid.UUID,
    type: JobEventType,
    payload: dict,
) -> JobEvent:
    """Append a JobEvent row + publish on the bus (atomic-ish).

    On Redis bus errors we still keep the DB row, so SSE clients reconnecting
    can replay history from the DB.
    """
    event = JobEvent(job_id=job_id, type=type, payload=payload)
    session.add(event)
    await session.flush()
    envelope = JobEventEnvelope(
        job_id=job_id,
        type=type,
        payload=payload,
        created_at=event.created_at or datetime.now(timezone.utc),
    )
    try:
        await bus.publish(job_id, envelope)
    except Exception:  # pragma: no cover - publish failures shouldn't fail the worker
        pass
    return event


async def list_past_events(
    *,
    session: AsyncSession,
    job_id: uuid.UUID,
    after_id: uuid.UUID | None = None,
) -> list[JobEventEnvelope]:
    """Pull all events for *job_id* ordered by created_at.

    `after_id` lets clients resume after the last event they saw — handy when
    a long SSE stream drops and reconnects mid-job.
    """
    stmt = select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
    rows = (await session.execute(stmt)).scalars().all()
    if after_id is not None:
        seen = False
        out = []
        for row in rows:
            if seen:
                out.append(_envelope(row))
            elif row.id == after_id:
                seen = True
        return out
    return [_envelope(row) for row in rows]


def _envelope(row: JobEvent) -> JobEventEnvelope:
    return JobEventEnvelope(
        job_id=row.job_id,
        type=row.type,
        payload=dict(row.payload or {}),
        created_at=row.created_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# arq plumbing — connection helpers (used by api.main + workers.main)
# ---------------------------------------------------------------------------

def arq_redis_settings(settings: Settings | None = None):
    """Return the RedisSettings dataclass arq expects."""
    from arq.connections import RedisSettings  # local import; arq not needed in tests

    s = settings or get_settings()
    # Parse our redis_url into RedisSettings fields. Acceptable in dev/local;
    # production deployments may pass RedisSettings directly via env.
    from urllib.parse import urlparse

    parsed = urlparse(s.redis_url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or 0),
        password=parsed.password,
    )
