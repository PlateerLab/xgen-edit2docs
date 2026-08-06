"""MCP server factory.

Builds a FastMCP application with the tools registered for the current
milestone. The same factory is reused by:
  - stdio transport: for local agents (Claude Desktop / Cursor) that
    spawn the server as a subprocess
  - HTTP+SSE transport (M4.4): for remote agents that just need a URL

Tests construct the server in-process and call tools through MCP's
in-memory client (no actual transport).
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..db.models import Asset, AssetKind
from ..services.assets import (
    AssetError,
    UploadResult,
    _safe_ext,
    _storage_key,
    build_download,
    get_asset,
    upload_asset,
)
from ..tools import (
    ConvertRequest,
    GenerateDeckRequest,
    StageEvent,
    generate_deck,
)
from . import catalog
from .context import MCPContext, get_default_context


def build_mcp_server(context: MCPContext | None = None) -> FastMCP:
    """Construct and return a fresh FastMCP server.

    Pass an explicit *context* (e.g. in tests) to override the process-wide
    default. The MCP tools acquire DB sessions / storage / tenant from this
    context exactly once per tool call.
    """
    ctx_provider = context or get_default_context()

    mcp = FastMCP(
        name="xgen_edit2docs",
        instructions=(
            "xgen_edit2docs generates editable PowerPoint decks (English-first; first-class Korean). "
            "Discover templates with `list_templates`, narration voices with "
            "`list_voices`. Upload sources with `upload_source` (small files inline, "
            "or `request_upload_url` for larger files). Look up asset metadata with "
            "`get_asset` and produce signed download URLs with `download_url`."
        ),
    )

    # ---- Catalog tools --------------------------------------------------

    @mcp.tool(
        name="hello",
        description=(
            "Health check. Returns service identity and the list of MCP tools. "
            "Use this first to verify a remote xgen_edit2docs server is reachable."
        ),
    )
    def hello() -> dict[str, Any]:
        return {
            "service": "xgen_edit2docs",
            "ok": True,
            "tools": [t.name for t in mcp._tool_manager.list_tools()],
        }

    @mcp.tool(
        name="list_templates",
        description=(
            "List the layout templates on this server. Use a returned `name` as "
            "`template_name` later in generate_deck."
        ),
    )
    def list_templates(locale: str = "en-US") -> dict[str, Any]:
        return {"templates": catalog.list_templates(locale=locale)}

    @mcp.tool(
        name="list_voices",
        description=(
            "List curated Edge-TTS voices, optionally filtered by `lang` "
            "(e.g. 'ko-KR' or 'ko'). Use a returned `voice_id` in the narration step."
        ),
    )
    def list_voices(lang: str | None = None) -> dict[str, Any]:
        return {"voices": catalog.list_voices(lang=lang)}

    # ---- Asset tools ----------------------------------------------------

    @mcp.tool(
        name="upload_source",
        description=(
            "Upload a small source file (PDF / DOCX / PPTX / XLSX / image) inline as "
            "base64-encoded bytes. Korean filenames are preserved end-to-end (stored "
            "on the asset row as `original_filename`; the object storage key is "
            "always ASCII). For files larger than ~10MB, prefer `request_upload_url` "
            "and PUT directly to the presigned URL instead."
        ),
    )
    async def upload_source(
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
        kind: str = "source",
    ) -> dict[str, Any]:
        try:
            content = base64.b64decode(content_base64, validate=True)
        except binascii.Error as exc:
            raise AssetError(f"Invalid base64 content: {exc}") from exc

        async with ctx_provider.scope() as scope:
            try:
                kind_enum = AssetKind(kind)
            except ValueError as exc:
                raise AssetError(
                    f"Unknown asset kind {kind!r}. Valid: "
                    + ", ".join(k.value for k in AssetKind)
                ) from exc

            result: UploadResult = await upload_asset(
                session=scope.session,
                storage=scope.storage,
                tenant=scope.tenant,
                kind=kind_enum,
                content=content,
                original_filename=filename,
                mime_type=mime_type,
            )

            return {
                "asset_id": str(result.asset.id),
                "kind": result.asset.kind.value,
                "original_filename": result.asset.original_filename,
                "storage_key": result.asset.storage_key,
                "mime_type": result.asset.mime_type,
                "size": result.asset.size,
                "sha256": result.sha256,
            }

    @mcp.tool(
        name="request_upload_url",
        description=(
            "Allocate a presigned PUT URL for a large source upload. Returns "
            "`{ asset_id, upload_url, storage_key, expires_in_seconds }`. The "
            "caller PUTs the file bytes directly to `upload_url` within the TTL. "
            "Korean filenames are preserved in the registered asset row."
        ),
    )
    async def request_upload_url(
        filename: str,
        mime_type: str = "application/octet-stream",
        kind: str = "source",
        expires_in_seconds: int = 300,
    ) -> dict[str, Any]:
        try:
            kind_enum = AssetKind(kind)
        except ValueError as exc:
            raise AssetError(f"Unknown asset kind: {kind!r}") from exc

        if not (30 <= expires_in_seconds <= 3600):
            raise AssetError("expires_in_seconds must be between 30 and 3600.")

        async with ctx_provider.scope() as scope:
            asset_id = uuid.uuid4()
            ext = _safe_ext(filename, fallback_mime=mime_type)
            storage_key = _storage_key(scope.tenant.id, kind_enum, asset_id, ext)
            presigned = await scope.storage.presigned_put_url(
                storage_key,
                expires_in_seconds=expires_in_seconds,
                content_type=mime_type,
            )
            row = Asset(
                id=asset_id,
                tenant_id=scope.tenant.id,
                kind=kind_enum,
                original_filename=filename,
                storage_key=storage_key,
                mime_type=mime_type,
                size=0,
            )
            scope.session.add(row)
            await scope.session.flush()
            return {
                "asset_id": str(asset_id),
                "storage_key": storage_key,
                "upload_url": presigned.url,
                "expires_in_seconds": presigned.expires_in_seconds,
            }

    @mcp.tool(
        name="get_asset",
        description=(
            "Look up an asset's metadata by `asset_id`. Returns kind, size, mime, "
            "original_filename (Korean preserved), storage_key (ASCII), sha256, "
            "and timestamps. Use this to confirm an upload landed."
        ),
    )
    async def get_asset_tool(asset_id: str) -> dict[str, Any]:
        try:
            aid = uuid.UUID(asset_id)
        except ValueError as exc:
            raise AssetError(f"asset_id must be a valid UUID: {asset_id!r}") from exc

        async with ctx_provider.scope() as scope:
            asset = await get_asset(session=scope.session, tenant=scope.tenant, asset_id=aid)
            return {
                "asset_id": str(asset.id),
                "kind": asset.kind.value,
                "original_filename": asset.original_filename,
                "storage_key": asset.storage_key,
                "mime_type": asset.mime_type,
                "size": asset.size,
                "sha256": asset.sha256,
                "created_at": asset.created_at.isoformat(),
            }

    # ---- High-level tool ------------------------------------------------

    @mcp.tool(
        name="generate_deck",
        description=(
            "Generate an editable PPTX from previously uploaded "
            "sources. Returns `{ pptx_asset_id, page_count, spec_lock, "
            "detected_langs, design_spec }` on success. Pass the resulting "
            "pptx_asset_id to `download_url` to get a signed download link. "
            "Progress is streamed back to the agent as MCP progress "
            "notifications (stage + percent complete). "
            "Requires BYOK: provide your Anthropic API key in "
            "`anthropic_api_key`; we use it for this call only and never "
            "persist it. "
            "English (en-US) is the default language; pass `lang` to switch (e.g. ko-KR). "
            "Pass `image_api_keys={\"OPENAI_API_KEY\":...}` to enable AI-image "
            "generation for hero / chart slides. Pass `narrate=True` to embed "
            "Korean speaker-notes narration (Edge-TTS) into the resulting "
            "PPTX so PowerPoint auto-plays it on slide entry. "
            "To reuse a user's own PPTX design, upload the .pptx with "
            "upload_source, then pass its id as `template_asset_id` with "
            "`deck_mode='template_restyle'` (fresh deck on the template's "
            "masters/theme) or `deck_mode='template_extend'` (append the new "
            "slides after the template's existing slides)."
        ),
    )
    async def generate_deck_tool(
        user_intent: str,
        anthropic_api_key: str,
        source_asset_ids: list[str] | None = None,
        target_min_pages: int = 8,
        target_max_pages: int = 12,
        lang: str = "en-US",
        style: str = "general",
        template_name: str | None = None,
        template_asset_id: str | None = None,
        deck_mode: str = "new",
        canvas_format: str = "ppt169",
        model: str = "claude-opus-4-7",
        output_basename: str = "deck",
        image_api_keys: dict[str, str] | None = None,
        skip_images: bool = False,
        narrate: bool = False,
        narration_voice: str | None = None,
        narration_rate: str = "+0%",
        narration_use_timings: bool = False,
        mcp_ctx: Context | None = None,
    ) -> dict[str, Any]:
        # `source_asset_ids` is optional. When empty the Strategist designs
        # the deck from `user_intent` alone (chat / topic-only mode).
        source_asset_ids = source_asset_ids or []
        if not anthropic_api_key:
            raise AssetError(
                "anthropic_api_key is required. Pass it on this call only — "
                "xgen_edit2docs never persists BYOK keys."
            )

        try:
            asset_uuids = [uuid.UUID(s) for s in source_asset_ids]
        except ValueError as exc:
            raise AssetError(f"source_asset_ids must be valid UUIDs: {exc}") from exc

        if deck_mode not in ("new", "template_restyle", "template_extend"):
            raise AssetError(
                f"deck_mode '{deck_mode}' must be one of "
                "new | template_restyle | template_extend."
            )
        if template_asset_id is not None and deck_mode == "new":
            deck_mode = "template_restyle"
        if deck_mode != "new" and template_asset_id is None:
            raise AssetError(
                f"deck_mode '{deck_mode}' requires template_asset_id — upload "
                "the template PPTX with upload_source first."
            )

        async with ctx_provider.scope() as scope:
            # 1. Resolve source assets to ConvertRequests.
            convert_reqs: list[ConvertRequest] = []
            for aid in asset_uuids:
                asset = await get_asset(
                    session=scope.session, tenant=scope.tenant, asset_id=aid
                )
                content = await scope.storage.get_bytes(asset.storage_key)
                convert_reqs.append(
                    ConvertRequest(
                        source_type=_infer_source_type(asset.mime_type),
                        content=content,
                        original_filename=asset.original_filename,
                    )
                )

            # 1b. Resolve the template PPTX (template modes only).
            template_pptx: bytes | None = None
            if template_asset_id is not None:
                try:
                    template_uuid = uuid.UUID(template_asset_id)
                except ValueError as exc:
                    raise AssetError(
                        f"template_asset_id must be a valid UUID: {exc}"
                    ) from exc
                t_asset = await get_asset(
                    session=scope.session, tenant=scope.tenant, asset_id=template_uuid
                )
                template_pptx = await scope.storage.get_bytes(t_asset.storage_key)

            # 2. Stream stage events back as MCP progress notifications.
            seen_stages: list[str] = []

            async def on_event(event: StageEvent) -> None:
                seen_stages.append(event.stage)
                if mcp_ctx is None:
                    return
                try:
                    await mcp_ctx.report_progress(
                        progress=event.progress,
                        total=1.0,
                        message=event.stage,
                    )
                except Exception:
                    # Progress channel failures must not fail the job.
                    pass

            # 3. Run the orchestrator.
            deck_resp = await generate_deck(
                GenerateDeckRequest(
                    sources=convert_reqs,
                    user_intent=user_intent,
                    target_pages=(target_min_pages, target_max_pages),
                    canvas_format=canvas_format,
                    style=style,  # type: ignore[arg-type]
                    lang=lang,  # type: ignore[arg-type]
                    template_name=template_name,
                    template_pptx=template_pptx,
                    deck_mode=deck_mode,  # type: ignore[arg-type]
                    model=model,
                    anthropic_api_key=anthropic_api_key,
                    fail_on_quality_error=False,
                    image_api_keys=image_api_keys or {},
                    skip_images=skip_images,
                    narrate=narrate,
                    narration_voice=narration_voice,
                    narration_rate=narration_rate,
                    narration_use_timings=narration_use_timings,
                ),
                on_event=on_event,
            )

            # 4. Persist the PPTX as a new asset.
            pptx_upload = await upload_asset(
                session=scope.session,
                storage=scope.storage,
                tenant=scope.tenant,
                kind=AssetKind.pptx,
                content=deck_resp.pptx,
                original_filename=f"{output_basename}.pptx",
                mime_type=(
                    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                ),
            )

            return {
                "pptx_asset_id": str(pptx_upload.asset.id),
                "page_count": deck_resp.page_count,
                "spec_lock": deck_resp.spec_lock,
                "design_spec": deck_resp.design_spec,
                "detected_langs": list(deck_resp.detected_langs),
                "stages_seen": seen_stages,
                "cost": {
                    "input_tokens": deck_resp.cost.input_tokens,
                    "output_tokens": deck_resp.cost.output_tokens,
                    "cache_read_tokens": deck_resp.cost.cache_read_tokens,
                    "cache_write_tokens": deck_resp.cost.cache_write_tokens,
                    "duration_seconds": deck_resp.cost.duration_seconds,
                },
                "warnings": [
                    {"code": w.code, "message": w.message} for w in deck_resp.warnings
                ],
            }

    @mcp.tool(
        name="edit_deck",
        description=(
            "Apply one chat-edit turn to an existing PPTX. Upload the deck "
            "with upload_source (or use a pptx asset produced by "
            "generate_deck), then pass its id with a natural-language "
            "instruction — e.g. '3번 슬라이드 제목을 바꿔줘', 'add a roadmap "
            "slide after slide 2', 'delete the last slide'. Returns the new "
            "revision's pptx_asset_id (the input asset is left untouched), "
            "the applied operations and a chat reply. Question-only "
            "instructions answer without changing the deck."
        ),
    )
    async def edit_deck_tool(
        pptx_asset_id: str,
        instruction: str,
        anthropic_api_key: str,
        chat_history: list[dict] | None = None,
        source_asset_ids: list[str] | None = None,
        lang: str = "en-US",
        model: str = "claude-opus-4-7",
        output_basename: str = "deck",
        mcp_ctx: Context | None = None,
    ) -> dict[str, Any]:
        if not anthropic_api_key:
            raise AssetError(
                "anthropic_api_key is required. Pass it on this call only — "
                "xgen_edit2docs never persists BYOK keys."
            )
        try:
            deck_uuid = uuid.UUID(pptx_asset_id)
        except ValueError as exc:
            raise AssetError(f"pptx_asset_id must be a valid UUID: {exc}") from exc

        from ..tools.edit_deck import ChatTurn, EditDeckRequest, edit_deck

        async with ctx_provider.scope() as scope:
            asset = await get_asset(
                session=scope.session, tenant=scope.tenant, asset_id=deck_uuid
            )
            pptx_bytes = await scope.storage.get_bytes(asset.storage_key)

            source_reqs: list[ConvertRequest] = []
            for sid in source_asset_ids or []:
                try:
                    src_uuid = uuid.UUID(sid)
                except ValueError as exc:
                    raise AssetError(f"source_asset_ids must be valid UUIDs: {exc}") from exc
                src_asset = await get_asset(
                    session=scope.session, tenant=scope.tenant, asset_id=src_uuid
                )
                source_reqs.append(
                    ConvertRequest(
                        source_type=_infer_source_type(src_asset.mime_type),
                        content=await scope.storage.get_bytes(src_asset.storage_key),
                        original_filename=src_asset.original_filename,
                    )
                )

            async def on_event(event: StageEvent) -> None:
                if mcp_ctx is None:
                    return
                try:
                    await mcp_ctx.report_progress(
                        progress=event.progress, total=1.0, message=event.stage
                    )
                except Exception:
                    pass

            resp = await edit_deck(
                EditDeckRequest(
                    pptx=pptx_bytes,
                    instruction=instruction,
                    sources=source_reqs,
                    chat_history=[
                        ChatTurn(role=t["role"], content=str(t.get("content", "")))
                        for t in (chat_history or [])
                        if isinstance(t, dict) and t.get("role") in ("user", "assistant")
                    ],
                    lang=lang,  # type: ignore[arg-type]
                    model=model,
                    anthropic_api_key=anthropic_api_key,
                ),
                on_event=on_event,
            )

            result_asset_id = pptx_asset_id
            if resp.changed:
                pptx_upload = await upload_asset(
                    session=scope.session,
                    storage=scope.storage,
                    tenant=scope.tenant,
                    kind=AssetKind.pptx,
                    content=resp.pptx,
                    original_filename=f"{output_basename}.pptx",
                    mime_type=(
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                    ),
                )
                result_asset_id = str(pptx_upload.asset.id)

            return {
                "pptx_asset_id": result_asset_id,
                "changed": resp.changed,
                "page_count": resp.page_count,
                "reply": resp.reply,
                "operations": resp.operations,
                "cost": {
                    "input_tokens": resp.cost.input_tokens,
                    "output_tokens": resp.cost.output_tokens,
                    "cache_read_tokens": resp.cost.cache_read_tokens,
                    "cache_write_tokens": resp.cost.cache_write_tokens,
                    "duration_seconds": resp.cost.duration_seconds,
                },
                "warnings": [
                    {"code": w.code, "message": w.message} for w in resp.warnings
                ],
            }

    @mcp.tool(
        name="download_url",
        description=(
            "Issue a short-lived signed GET URL for downloading an asset. The "
            "URL carries a Content-Disposition that restores the original Korean "
            "filename when the user agent saves the file."
        ),
    )
    async def download_url(asset_id: str, expires_in_seconds: int = 300) -> dict[str, Any]:
        try:
            aid = uuid.UUID(asset_id)
        except ValueError as exc:
            raise AssetError(f"asset_id must be a valid UUID: {asset_id!r}") from exc
        if not (30 <= expires_in_seconds <= 3600):
            raise AssetError("expires_in_seconds must be between 30 and 3600.")

        async with ctx_provider.scope() as scope:
            info = await build_download(
                session=scope.session,
                storage=scope.storage,
                tenant=scope.tenant,
                asset_id=aid,
                expires_in_seconds=expires_in_seconds,
            )
            return {
                "download_url": info.url,
                "expires_in_seconds": info.expires_in_seconds,
                "filename": info.filename,
                "mime_type": info.mime_type,
            }

    return mcp


def _infer_source_type(mime_type: str | None) -> str:
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
    }.get(mime_type or "", "pdf")


__all__ = ["build_mcp_server"]
