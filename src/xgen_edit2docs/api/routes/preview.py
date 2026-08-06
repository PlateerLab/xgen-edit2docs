"""Synchronous PPTX preview: every slide rendered to a self-contained SVG.

    POST /v1/preview   {pptx_asset_id} -> {slides: [{index, svg}], ...}

Deterministic and LLM-free, so it runs inline in the request (in a worker
thread) rather than through the job queue — the studio needs previews
immediately after upload and after every edit turn.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from ...db.models import Asset
from ...storage import get_default_storage
from ...tools import RenderPreviewRequest, render_preview
from ..dependencies import CurrentTenant, DbSession, RequestLocale
from ..errors import bilingual_detail

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/preview", tags=["preview"])


class PreviewBody(BaseModel):
    # Field name kept from the PPTX-only era for wire compatibility;
    # any .pptx / .docx / .xlsx asset id works.
    pptx_asset_id: uuid.UUID


class PreviewSlide(BaseModel):
    index: int
    svg: str


class PreviewResponse(BaseModel):
    format: str  # pptx | docx | xlsx
    page_count: int
    warnings: list[dict]
    # pptx: one SVG per slide + canvas dims.
    slides: list[PreviewSlide] = []
    width_px: float | None = None
    height_px: float | None = None
    # docx / xlsx: a display-HTML rendering (structural tags only).
    html: str | None = None


@router.post("", response_model=PreviewResponse, summary="Render a document preview")
async def preview_doc(
    body: PreviewBody,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
) -> PreviewResponse:
    asset = (
        await session.execute(
            select(Asset).where(
                Asset.id == body.pptx_asset_id, Asset.tenant_id == tenant.id
            )
        )
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=bilingual_detail(
                "ASSET_NOT_FOUND",
                en=f"Asset {body.pptx_asset_id} not found.",
                ko=f"에셋 {body.pptx_asset_id} 를 찾을 수 없습니다.",
                locale=locale,
            ),
        )

    from ...documents import doc_format_of

    fmt = doc_format_of(asset.original_filename, asset.mime_type) or "pptx"
    storage = get_default_storage()
    try:
        content = await storage.get_bytes(asset.storage_key)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=bilingual_detail(
                "ASSET_BYTES_MISSING",
                en="Asset bytes are not uploaded yet.",
                ko="에셋 파일이 아직 업로드되지 않았습니다.",
                locale=locale,
            ),
        )
    try:
        if fmt == "pptx":
            resp = await asyncio.to_thread(
                render_preview, RenderPreviewRequest(pptx=content)
            )
            return PreviewResponse(
                format="pptx",
                slides=[PreviewSlide(index=s.index, svg=s.svg) for s in resp.slides],
                width_px=resp.width_px,
                height_px=resp.height_px,
                page_count=resp.page_count,
                warnings=[
                    {"code": w.code, "message": w.message} for w in resp.warnings
                ],
            )
        if fmt == "docx":
            from ...documents.docx_engine import docx_preview

            html, doc_warnings = await asyncio.to_thread(docx_preview, content)
            return PreviewResponse(
                format="docx", page_count=1, html=html, warnings=doc_warnings
            )

        from ...documents.xlsx_engine import xlsx_outline, xlsx_preview

        html, sheet_warnings = await asyncio.to_thread(xlsx_preview, content)
        sheet_count = len(xlsx_outline(content)["sheets"])
        return PreviewResponse(
            format="xlsx", page_count=sheet_count, html=html, warnings=sheet_warnings
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "PREVIEW_RENDER_FAILED",
                "message": str(exc),
                "message_en": str(exc),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=bilingual_detail(
                "PREVIEW_RENDER_FAILED",
                en=f"Preview rendering failed: {exc}",
                ko=f"미리보기 렌더링 실패: {exc}",
                locale=locale,
            ),
        ) from exc
