"""Unit tests for the job service + worker dispatch.

Exercises:
- enqueue_job creates a JobRow with queued status
- record_event persists JobEvents AND publishes on the bus
- FakeJobBus subscribe / publish / close lifecycle
- convert_noop executor round-trips: queued -> running -> done with three events
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from xgen_edit2docs.db.models import Base, Job, JobEventType, JobKind, JobStatus, Tenant
from xgen_edit2docs.services.jobs import (
    FakeJobBus,
    JobEventEnvelope,
    JobNotFound,
    enqueue_job,
    get_job,
    list_past_events,
    record_event,
)
from xgen_edit2docs.workers.executors.noop import convert_noop
from xgen_edit2docs.workers.executors.registry import ExecutionContext


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> Tenant:
    t = Tenant(name="T")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# enqueue + get
# ---------------------------------------------------------------------------


class TestEnqueueAndGet:
    @pytest.mark.asyncio
    async def test_enqueue_creates_queued_row(self, session: AsyncSession, tenant: Tenant):
        job = await enqueue_job(
            session=session,
            tenant=tenant,
            kind=JobKind.convert,
            params={"hello": "world"},
        )
        await session.commit()
        assert job.id is not None
        assert job.status == JobStatus.queued
        assert job.params == {"hello": "world"}
        assert job.tenant_id == tenant.id

    @pytest.mark.asyncio
    async def test_get_job_scoped_to_tenant(self, session: AsyncSession, tenant: Tenant):
        job = await enqueue_job(session=session, tenant=tenant, kind=JobKind.convert, params={})
        await session.commit()
        fetched = await get_job(session=session, tenant=tenant, job_id=job.id)
        assert fetched.id == job.id

    @pytest.mark.asyncio
    async def test_get_job_other_tenant_404(self, session: AsyncSession, tenant: Tenant):
        other = Tenant(name="Other")
        session.add(other)
        await session.commit()
        await session.refresh(other)

        job = await enqueue_job(session=session, tenant=tenant, kind=JobKind.convert, params={})
        await session.commit()
        with pytest.raises(JobNotFound):
            await get_job(session=session, tenant=other, job_id=job.id)


# ---------------------------------------------------------------------------
# FakeJobBus + record_event
# ---------------------------------------------------------------------------


class TestEventBusAndPersistence:
    @pytest.mark.asyncio
    async def test_record_event_persists_and_publishes(
        self, session: AsyncSession, tenant: Tenant
    ):
        job = await enqueue_job(session=session, tenant=tenant, kind=JobKind.convert, params={})
        await session.commit()

        bus = FakeJobBus()
        async with bus.subscriber(job.id) as queue:
            await record_event(
                session=session,
                bus=bus,
                job_id=job.id,
                type=JobEventType.progress,
                payload={"progress": 0.42},
            )
            envelope = await asyncio.wait_for(queue.get(), timeout=2)

        assert isinstance(envelope, JobEventEnvelope)
        assert envelope.type == JobEventType.progress
        assert envelope.payload == {"progress": 0.42}

        # And the DB row exists.
        events = await list_past_events(session=session, job_id=job.id)
        assert len(events) == 1
        assert events[0].payload == {"progress": 0.42}

    @pytest.mark.asyncio
    async def test_list_past_events_after_id(
        self, session: AsyncSession, tenant: Tenant
    ):
        job = await enqueue_job(session=session, tenant=tenant, kind=JobKind.convert, params={})
        await session.commit()

        bus = FakeJobBus()
        ev1 = await record_event(
            session=session, bus=bus, job_id=job.id,
            type=JobEventType.stage, payload={"stage": "a"},
        )
        await record_event(
            session=session, bus=bus, job_id=job.id,
            type=JobEventType.stage, payload={"stage": "b"},
        )
        await record_event(
            session=session, bus=bus, job_id=job.id,
            type=JobEventType.stage, payload={"stage": "c"},
        )
        await session.commit()

        # Resume after ev1: expect 'b' and 'c'.
        events = await list_past_events(session=session, job_id=job.id, after_id=ev1.id)
        assert [e.payload["stage"] for e in events] == ["b", "c"]


# ---------------------------------------------------------------------------
# Worker round-trip via convert_noop
# ---------------------------------------------------------------------------


class TestNoopExecutor:
    @pytest.mark.asyncio
    async def test_convert_noop_completes_job(
        self, session: AsyncSession, tenant: Tenant
    ):
        job = await enqueue_job(session=session, tenant=tenant, kind=JobKind.convert, params={})
        await session.commit()

        bus = FakeJobBus()
        # Subscribe BEFORE running so we capture publishes.
        envelopes: list[JobEventEnvelope] = []

        async def consume():
            async with bus.subscriber(job.id) as q:
                for _ in range(3):
                    envelopes.append(await asyncio.wait_for(q.get(), timeout=2))

        consumer = asyncio.create_task(consume())
        # Give the consumer a tick to subscribe before we publish.
        await asyncio.sleep(0)

        ctx = ExecutionContext(session=session, bus=bus, job=job)
        await convert_noop(ctx)
        await consumer

        await session.refresh(job)
        assert job.status == JobStatus.done
        assert job.started_at is not None
        assert job.finished_at is not None
        assert job.result == {"noop": True}
        # All three published envelopes arrived.
        stages = [e.payload.get("stage") or e.payload.get("progress") for e in envelopes]
        assert stages == ["converting", 0.5, "done"]
