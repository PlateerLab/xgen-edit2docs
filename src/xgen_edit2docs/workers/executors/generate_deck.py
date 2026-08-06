"""Generate-deck executor.

Bridges the M3 server (DB + storage + queue) to the M2 tool layer
(tools.generate_deck). For each queued job:

1. Read source asset bytes from object storage (one per source ref).
2. Read BYOK keys from the job's params (caller stashed them in the
   POST /v1/jobs/generate-deck request).
3. Call tools.generate_deck(...) with an on_event callback that records
   each StageEvent as a JobEvent + publishes on the bus (SSE fan-out).
4. Persist the resulting PPTX as a new Asset.
5. Flip the Job to done with the asset id + page count in `result`.

Errors during steps 1-4 flip the job to failed and persist an error_message.
The worker (workers.main.run_job) wraps this in a try/except so unexpected
exceptions still produce a failed job rather than crashing the worker.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ...db.models import Asset, AssetKind, JobEventType, JobKind, JobStatus
from ...services.assets import upload_asset
from ...services.jobs import record_event
from ...storage import get_default_storage
from ...tools import (
    ConvertRequest,
    GenerateDeckRequest,
    StageEvent,
    generate_deck,
)
from .registry import ExecutionContext, register

logger = logging.getLogger(__name__)


@register(JobKind.generate_deck)
async def run_generate_deck(ctx: ExecutionContext) -> None:
    job = ctx.job
    session = ctx.session
    bus = ctx.bus

    job.status = JobStatus.running
    job.started_at = datetime.now(timezone.utc)
    await session.flush()

    params = dict(job.params or {})
    source_asset_ids: list[str] = params.get("source_asset_ids", [])
    user_intent: str = params["user_intent"]
    target_pages = tuple(params.get("target_pages", [8, 12]))  # type: ignore[arg-type]
    lang: str = params.get("lang", "en-US")
    style: str = params.get("style", "general")
    template_name: str | None = params.get("template_name")
    template_asset_id: str | None = params.get("template_asset_id")
    deck_mode: str = params.get("deck_mode", "new")
    canvas_format: str = params.get("canvas_format", "ppt169")
    model: str = params.get("model", "claude-opus-4-7")
    anthropic_api_key: str = params["anthropic_api_key"]

    storage = get_default_storage()

    # 1. Pull source bytes for each referenced asset.
    convert_reqs: list[ConvertRequest] = []
    for asset_id_str in source_asset_ids:
        asset_id = uuid.UUID(asset_id_str)
        asset = (
            await session.execute(
                select(Asset).where(
                    Asset.id == asset_id, Asset.tenant_id == job.tenant_id
                )
            )
        ).scalar_one_or_none()
        if asset is None:
            raise RuntimeError(f"source asset {asset_id} not found for tenant {job.tenant_id}")
        content = await storage.get_bytes(asset.storage_key)
        convert_reqs.append(
            ConvertRequest(
                source_type=_infer_source_type(asset.mime_type),
                content=content,
                original_filename=asset.original_filename,
            )
        )

    # 1b. Pull the user-provided template PPTX (template modes only).
    template_pptx: bytes | None = None
    if template_asset_id:
        t_asset_id = uuid.UUID(template_asset_id)
        t_asset = (
            await session.execute(
                select(Asset).where(
                    Asset.id == t_asset_id, Asset.tenant_id == job.tenant_id
                )
            )
        ).scalar_one_or_none()
        if t_asset is None:
            raise RuntimeError(
                f"template asset {t_asset_id} not found for tenant {job.tenant_id}"
            )
        template_pptx = await storage.get_bytes(t_asset.storage_key)

    # 1c. DOCX / XLSX generation: one writer/designer call + deterministic
    # render — handled here and returned early (the deck pipeline below is
    # pptx-only). Stage events are emitted manually for the UI.
    output_format: str = params.get("output_format", "pptx")
    if output_format in ("docx", "xlsx"):
        await _run_generate_document(ctx, output_format, convert_reqs, params, storage)
        return

    # 2. Stream tool-layer StageEvents to the job event bus.
    async def on_event(event: StageEvent) -> None:
        await record_event(
            session=session,
            bus=bus,
            job_id=job.id,
            type=JobEventType.stage if event.stage else JobEventType.progress,
            payload={
                "stage": event.stage,
                "progress": event.progress,
                "message_key": event.message_key,
                "message_vars": event.message_vars,
                "page_index": event.page_index,
            },
        )
        # We commit per event so SSE subscribers see them in real time.
        # If a downstream stage raises, prior events stay in the DB.
        await session.commit()

    # 3. Run the orchestrator.
    deck_resp = await generate_deck(
        GenerateDeckRequest(
            sources=convert_reqs,
            user_intent=user_intent,
            target_pages=target_pages,  # type: ignore[arg-type]
            canvas_format=canvas_format,  # type: ignore[arg-type]
            style=style,  # type: ignore[arg-type]
            lang=lang,  # type: ignore[arg-type]
            template_name=template_name,
            template_pptx=template_pptx,
            deck_mode=deck_mode,  # type: ignore[arg-type]
            model=model,
            anthropic_api_key=anthropic_api_key,
            fail_on_quality_error=bool(params.get("fail_on_quality_error", False)),
        ),
        on_event=on_event,
    )

    # 4. Persist the PPTX as a new asset.
    from ...db.models import Tenant

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == job.tenant_id))
    ).scalar_one()
    pptx_upload = await upload_asset(
        session=session,
        storage=storage,
        tenant=tenant,
        kind=AssetKind.pptx,
        content=deck_resp.pptx,
        original_filename=f"{params.get('output_basename', 'deck')}.pptx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        project_id=job.project_id,
    )

    # 5. Finalize job.
    job.result = _serializable_result(deck_resp, pptx_upload.asset.id)
    job.cost = _cost_dict(deck_resp.cost)
    job.status = JobStatus.done
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infer_source_type(mime_type: str) -> str:
    return {
        "application/pdf": "pdf",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
        "text/html": "html",
        "application/epub+zip": "epub",
        "application/x-ipynb+json": "ipynb",
    }.get(mime_type or "", "pdf")  # default to pdf if unknown


def _serializable_result(deck_resp, pptx_asset_id: uuid.UUID) -> dict[str, Any]:
    return {
        "pptx_asset_id": str(pptx_asset_id),
        "page_count": deck_resp.page_count,
        "spec_lock": deck_resp.spec_lock,
        "design_spec": deck_resp.design_spec,
        "detected_langs": list(deck_resp.detected_langs),
        "quality_issues": [
            {
                "page_index": q.page_index,
                "severity": q.severity,
                "code": q.code,
                "message": q.message,
                "location": q.location,
            }
            for q in deck_resp.quality_issues
        ],
    }


def _cost_dict(cost) -> dict[str, Any]:
    return {
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "cache_read_tokens": cost.cache_read_tokens,
        "cache_write_tokens": cost.cache_write_tokens,
        "image_count": cost.image_count,
        "audio_seconds": cost.audio_seconds,
        "duration_seconds": cost.duration_seconds,
    }


_DOC_MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


async def _run_generate_document(
    ctx: ExecutionContext, fmt: str, convert_reqs, params: dict, storage
) -> None:
    """DOCX / XLSX branch of the generate job (see run_generate_deck)."""
    from ...tools import StageEvent as _StageEvent
    from ...tools.convert import convert_to_markdown
    from ...tools.generate_doc import GenerateDocRequest, generate_document

    job = ctx.job
    session = ctx.session
    bus = ctx.bus

    async def emit(stage: str, progress: float) -> None:
        await record_event(
            session=session,
            bus=bus,
            job_id=job.id,
            type=JobEventType.stage,
            payload=_StageEvent(
                stage=stage, progress=progress, message_key=f"stages.{stage}"
            ).model_dump(),
        )
        await session.commit()

    await emit("strategizing", 0.2)
    import asyncio as _asyncio

    converted = await _asyncio.gather(
        *(_asyncio.to_thread(convert_to_markdown, r) for r in convert_reqs)
    )
    sources_markdown = [c.markdown for c in converted]
    resp = await generate_document(
        GenerateDocRequest(
            intent=params["user_intent"],
            fmt=fmt,  # type: ignore[arg-type]
            sources_markdown=sources_markdown,
            lang=params.get("lang", "en-US"),  # type: ignore[arg-type]
            model=params.get("model", "claude-opus-4-7"),
            anthropic_api_key=params["anthropic_api_key"],
        )
    )
    await emit("exporting", 0.9)

    from ...db.models import Tenant

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == job.tenant_id))
    ).scalar_one()
    upload = await upload_asset(
        session=session,
        storage=storage,
        tenant=tenant,
        kind=AssetKind(fmt),
        content=resp.content,
        original_filename=f"{params.get('output_basename', 'document')}.{fmt}",
        mime_type=_DOC_MIME[fmt],
        project_id=job.project_id,
    )
    await emit("done", 1.0)

    job.result = {
        "pptx_asset_id": str(upload.asset.id),
        "doc_asset_id": str(upload.asset.id),
        "format": fmt,
        "page_count": 0,
        "design_spec": resp.artifact,
        "spec_lock": "",
        "detected_langs": [],
        "quality_issues": [],
        "warnings": [
            {"code": w.code, "message": w.message} for w in resp.warnings
        ],
    }
    job.cost = {
        "input_tokens": resp.cost.input_tokens,
        "output_tokens": resp.cost.output_tokens,
        "cache_read_tokens": resp.cost.cache_read_tokens,
        "cache_write_tokens": resp.cost.cache_write_tokens,
        "image_count": 0,
        "audio_seconds": 0.0,
        "duration_seconds": resp.cost.duration_seconds,
    }
    job.status = JobStatus.done
    job.finished_at = datetime.now(timezone.utc)
    await session.commit()
