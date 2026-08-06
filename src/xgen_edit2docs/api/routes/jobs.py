"""Job endpoints: enqueue, poll, stream events (SSE).

Routes:
    POST /v1/jobs/generate-deck      enqueue a deck generation job
    GET  /v1/jobs/{id}               poll status + result
    GET  /v1/jobs/{id}/events        SSE stream of stage events

The generate-deck endpoint accepts the BYOK Anthropic API key in the body
(or via an X-Anthropic-API-Key header). It is persisted only on the queued
job row for the worker to consume, then can be wiped by a cleanup pass —
M6 will tighten this with column-level encryption.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ...db.models import Job, JobKind, JobStatus
from ...db.session import get_sessionmaker
from ...services.jobs import (
    JobEventEnvelope,
    JobNotFound,
    enqueue_job,
    get_default_bus,
    get_job,
    list_past_events,
)
from ..errors import bilingual_detail
from ..dependencies import (
    Catalog,
    CurrentTenant,
    DbSession,
    RequestLocale,
    Storage,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class GenerateDeckBody(BaseModel):
    """Body for POST /v1/jobs/generate-deck."""

    source_asset_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description=(
            "Asset ids from /v1/assets. May be empty — the Strategist will "
            "design the deck from `user_intent` alone (topic-only / chat mode)."
        ),
    )
    user_intent: str = Field(..., description="What the deck is for. Korean / any language welcome.")
    target_pages: tuple[int, int] = (8, 12)
    canvas_format: str = "ppt169"
    style: str = Field(default="general", description="general | consultant | consultant-top")
    # Output document family: pptx (full deck pipeline) | docx | xlsx
    # (single writer/designer call + deterministic render).
    output_format: str = Field(default="pptx", description="pptx | docx | xlsx")
    lang: str = "en-US"
    template_name: str | None = None
    # User-provided PPTX template: upload the .pptx via POST /v1/assets
    # first, then reference it here. deck_mode picks how it is used:
    #   template_restyle — fresh deck inside the template package
    #                      (masters/theme preserved, original slides removed)
    #   template_extend  — generated slides are appended after the
    #                      template's existing slides
    # Omitting deck_mode while template_asset_id is set implies
    # template_restyle.
    template_asset_id: uuid.UUID | None = None
    deck_mode: str = Field(
        default="new",
        description="new | template_restyle | template_extend",
    )
    model: str = "claude-opus-4-7"
    output_basename: str | None = None
    project_id: uuid.UUID | None = None
    fail_on_quality_error: bool = False


class EditDeckBody(BaseModel):
    """Body for POST /v1/jobs/edit-deck (one chat-edit turn)."""

    pptx_asset_id: uuid.UUID = Field(
        ..., description="Current deck revision — a pptx asset from /v1/assets or a prior job."
    )
    instruction: str = Field(..., min_length=1, description="The chat message to apply.")
    chat_history: list[dict] = Field(
        default_factory=list,
        description='Prior turns: [{"role": "user"|"assistant", "content": "..."}].',
    )
    source_asset_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Reference documents attached to this turn (assets from /v1/assets).",
    )
    lang: str = "en-US"
    model: str = "claude-opus-4-7"
    output_basename: str | None = None
    project_id: uuid.UUID | None = None


class JobResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    kind: JobKind
    status: JobStatus
    params: dict
    cost: dict
    result: dict
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, job: Job) -> "JobResponse":
        # Redact the BYOK key from the params echo so polling clients never see
        # other tenants' keys on a shared cache layer.
        params = dict(job.params or {})
        if "anthropic_api_key" in params:
            params["anthropic_api_key"] = "[redacted]"
        return cls(
            id=job.id,
            tenant_id=job.tenant_id,
            kind=job.kind,
            status=job.status,
            params=params,
            cost=dict(job.cost or {}),
            result=dict(job.result or {}),
            error_message=job.error_message,
            created_at=job.created_at.isoformat(),
            started_at=job.started_at.isoformat() if job.started_at else None,
            finished_at=job.finished_at.isoformat() if job.finished_at else None,
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "/generate-deck",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a deck-generation job",
)
async def enqueue_generate_deck(
    body: GenerateDeckBody,
    request: Request,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
    x_anthropic_api_key: Annotated[str | None, Header(alias="X-Anthropic-API-Key")] = None,
) -> JobResponse:
    """Create a queued generate_deck job and trigger execution.

    Queue mode is decided by lifespan:
    - `arq` pool set on `request.app.state.arq_pool` (when EDIT2DOCS_REDIS_URL
      is configured) — push onto Redis; the worker process consumes.
    - Otherwise — fire off `asyncio.create_task(_run_inline(job_id))` so
      execution starts immediately in this process.
    """
    anthropic_key = x_anthropic_api_key or ""
    if not anthropic_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "LLM_API_KEY_MISSING",
                en="Anthropic API key required. Pass via X-Anthropic-API-Key header.",
                ko="Anthropic API 키가 필요합니다. X-Anthropic-API-Key 헤더로 전달하세요.",
                locale=locale,
            ),
        )

    if body.output_format not in ("pptx", "docx", "xlsx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "INVALID_OUTPUT_FORMAT",
                en=f"Unsupported output_format '{body.output_format}'.",
                ko=f"output_format '{body.output_format}' 는 pptx | docx | xlsx 중 하나여야 합니다.",
                locale=locale,
            ),
        )

    if body.output_format != "pptx" and (
        body.template_asset_id is not None or body.deck_mode != "new"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "TEMPLATE_UNSUPPORTED_FOR_FORMAT",
                en=(
                    "template_asset_id / deck_mode are pptx-only options "
                    f"(output_format={body.output_format})."
                ),
                ko=(
                    f"템플릿/deck_mode는 pptx 출력에서만 지원됩니다 "
                    f"(output_format={body.output_format})."
                ),
                locale=locale,
            ),
        )

    deck_mode = body.deck_mode
    if deck_mode not in ("new", "template_restyle", "template_extend"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "INVALID_DECK_MODE",
                en=(
                    f"Unsupported deck_mode '{deck_mode}'; expected one of "
                    "new | template_restyle | template_extend."
                ),
                ko=(
                    f"deck_mode '{deck_mode}' 는 지원되지 않습니다 — "
                    "new | template_restyle | template_extend 중 하나여야 합니다."
                ),
                locale=locale,
            ),
        )
    if body.template_asset_id is not None and deck_mode == "new":
        deck_mode = "template_restyle"
    if deck_mode != "new" and body.template_asset_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "TEMPLATE_ASSET_REQUIRED",
                en=(
                    f"deck_mode '{deck_mode}' requires template_asset_id; "
                    "upload the template PPTX via POST /v1/assets first."
                ),
                ko=(
                    f"deck_mode '{deck_mode}' 는 template_asset_id 가 필요합니다. "
                    "먼저 POST /v1/assets 로 템플릿 PPTX를 업로드하세요."
                ),
                locale=locale,
            ),
        )

    params = {
        "source_asset_ids": [str(x) for x in body.source_asset_ids],
        "user_intent": body.user_intent,
        "target_pages": list(body.target_pages),
        "canvas_format": body.canvas_format,
        "style": body.style,
        "output_format": body.output_format,
        "lang": body.lang,
        "template_name": body.template_name,
        "template_asset_id": (
            str(body.template_asset_id) if body.template_asset_id else None
        ),
        "deck_mode": deck_mode,
        "model": body.model,
        "output_basename": body.output_basename or "deck",
        "fail_on_quality_error": body.fail_on_quality_error,
        # BYOK key — worker reads + nulls this out on completion (M6 encrypts).
        "anthropic_api_key": anthropic_key,
    }
    arq_pool = getattr(request.app.state, "arq_pool", None)
    job = await enqueue_job(
        session=session,
        tenant=tenant,
        kind=JobKind.generate_deck,
        params=params,
        project_id=body.project_id,
        arq_pool=arq_pool,
    )

    # Inline mode: the request handler is the only place that knows the job
    # has just been created, so kick off the worker right away. We can't
    # use the request-scoped session for this (it commits/closes when the
    # request returns) — _run_inline opens its own session.
    if arq_pool is None:
        asyncio.create_task(_run_inline(job.id))

    return JobResponse.from_row(job)


@router.post(
    "/edit-deck",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue one chat-edit turn on an existing deck",
)
async def enqueue_edit_deck(
    body: EditDeckBody,
    request: Request,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
    x_anthropic_api_key: Annotated[str | None, Header(alias="X-Anthropic-API-Key")] = None,
) -> JobResponse:
    """Create a queued edit_deck job (same queue semantics as generate-deck)."""
    anthropic_key = x_anthropic_api_key or ""
    if not anthropic_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=bilingual_detail(
                "LLM_API_KEY_MISSING",
                en="Anthropic API key required. Pass via X-Anthropic-API-Key header.",
                ko="Anthropic API 키가 필요합니다. X-Anthropic-API-Key 헤더로 전달하세요.",
                locale=locale,
            ),
        )

    for turn in body.chat_history:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=bilingual_detail(
                    "INVALID_CHAT_HISTORY",
                    en='chat_history entries must be {"role": "user"|"assistant", "content": "..."}.',
                    ko='chat_history 항목은 {"role": "user"|"assistant", "content": "..."} 형식이어야 합니다.',
                    locale=locale,
                ),
            )

    params = {
        "pptx_asset_id": str(body.pptx_asset_id),
        "instruction": body.instruction,
        "chat_history": body.chat_history[-12:],
        "source_asset_ids": [str(x) for x in body.source_asset_ids],
        "lang": body.lang,
        "model": body.model,
        "output_basename": body.output_basename or "deck",
        # BYOK key — worker reads + nulls this out on completion (M6 encrypts).
        "anthropic_api_key": anthropic_key,
    }
    arq_pool = getattr(request.app.state, "arq_pool", None)
    job = await enqueue_job(
        session=session,
        tenant=tenant,
        kind=JobKind.edit_deck,
        params=params,
        project_id=body.project_id,
        arq_pool=arq_pool,
    )
    if arq_pool is None:
        asyncio.create_task(_run_inline(job.id))
    return JobResponse.from_row(job)


async def _run_inline(job_id: uuid.UUID) -> None:
    """Inline executor used when no Redis-backed arq worker is configured.

    Mirrors workers.main.run_job but lives inside the FastAPI process. The
    asyncio task is fire-and-forget: when something fails we record it on
    the Job row and log, but we never raise back to the API caller (the
    request has already returned 202 by the time we start).
    """
    from ...workers.executors.registry import EXECUTORS, ExecutionContext

    sessionmaker = get_sessionmaker()
    bus = get_default_bus()

    async with sessionmaker() as session:
        from sqlalchemy import select

        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if job is None:
            logger.warning("inline runner: unknown job_id=%s", job_id)
            return

        executor = EXECUTORS.get(job.kind)
        if executor is None:
            logger.error("inline runner: no executor for kind=%s", job.kind)
            job.status = JobStatus.failed
            job.error_message = f"no executor for kind={job.kind.value}"
            await session.commit()
            return

        ctx = ExecutionContext(session=session, bus=bus, job=job)
        try:
            await executor(ctx)
        except Exception as exc:
            logger.exception("inline runner: job %s failed", job_id)
            job.status = JobStatus.failed
            job.error_message = str(exc)
            await session.commit()
        finally:
            # Wake any SSE subscribers so they can drain and disconnect.
            # FakeJobBus exposes close(); RedisJobBus relies on connection
            # teardown instead, so this is best-effort.
            close = getattr(bus, "close", None)
            if close is not None:
                try:
                    await close(job_id)
                except Exception:
                    logger.exception("inline runner: bus.close failed for job %s", job_id)


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Get job status + cost + result",
)
async def get_job_status(
    job_id: uuid.UUID,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
) -> JobResponse:
    try:
        job = await get_job(session=session, tenant=tenant, job_id=job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=bilingual_detail(
                "JOB_NOT_FOUND",
                en=f"Job {job_id} not found.",
                ko=f"작업 {job_id} 를 찾을 수 없습니다.",
                locale=locale,
            ),
        ) from exc
    return JobResponse.from_row(job)


@router.get(
    "/{job_id}/events",
    summary="Server-sent events: stream stage progress",
)
async def stream_job_events(
    job_id: uuid.UUID,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
    after_id: Annotated[uuid.UUID | None, Query(description="Resume after this event id")] = None,
):
    """SSE stream of all stage events for *job_id*.

    Replays the DB history first (so a freshly-connected client doesn't miss
    earlier stages), then keeps the connection open and tails new events via
    the JobBus. The stream closes when the job reaches a terminal status.

    Tail subscription requires the FakeJobBus or RedisJobBus to expose a
    `subscriber` context manager. Both implementations do (see services/jobs.py).
    """
    try:
        job = await get_job(session=session, tenant=tenant, job_id=job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=bilingual_detail(
                "JOB_NOT_FOUND",
                en=f"Job {job_id} not found.",
                ko=f"작업 {job_id} 를 찾을 수 없습니다.",
                locale=locale,
            ),
        ) from exc

    history = await list_past_events(session=session, job_id=job_id, after_id=after_id)
    # Snapshot the terminal status BEFORE we attach a subscriber — if the job
    # already finished, history alone is enough.
    terminal = job.status in (JobStatus.done, JobStatus.failed, JobStatus.cancelled)

    from ...services.jobs import get_default_bus

    bus = get_default_bus()

    async def event_generator() -> AsyncIterator[dict]:
        # 1. Replay history.
        for envelope in history:
            yield _sse_payload(envelope)
        if terminal:
            return
        # 2. Tail new events via the bus subscriber.
        # FakeJobBus and RedisJobBus both expose a `subscriber` context manager
        # yielding either an asyncio.Queue (Fake) or a redis pubsub (Redis).
        # We handle the Fake case here directly; Redis fan-out is implemented
        # inline so we don't depend on a unified interface yet.
        if hasattr(bus, "subscriber"):
            async with bus.subscriber(job_id) as subscriber:
                if hasattr(subscriber, "get"):  # asyncio.Queue (FakeJobBus)
                    while True:
                        item = await subscriber.get()
                        if item is None:
                            return
                        yield _sse_payload(item)
                else:  # redis pubsub
                    async for msg in subscriber.listen():  # pragma: no cover - prod path
                        if msg["type"] != "message":
                            continue
                        # publish() sends json.dumps(envelope.to_jsonable());
                        # with decode_responses=True this arrives as a STRING.
                        data = msg["data"]
                        if isinstance(data, (bytes, str)):
                            data = json.loads(data)
                        yield {
                            "event": data.get("type", "progress"),
                            "data": json.dumps(data, ensure_ascii=False),
                        }
                        stage = (data.get("payload") or {}).get("stage")
                        if stage in ("done", "failed"):
                            return
        else:  # pragma: no cover
            return

    return EventSourceResponse(event_generator())


def _sse_payload(envelope: JobEventEnvelope) -> dict:
    return {
        "event": envelope.type.value,
        "data": json.dumps(envelope.to_jsonable(), ensure_ascii=False),
    }
