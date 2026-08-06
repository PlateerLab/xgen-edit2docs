"""Synchronous, deterministic text edits on a document asset.

    POST /v1/text-edits
        {pptx_asset_id, edits: [...]} -> {doc_asset_id, applied, results}

Works on .pptx / .docx / .xlsx assets — the asset's format picks the
engine and the expected edit shape:

    pptx: {slide, shape_id, para, new_text, old_text?, row?, col?}
    docx: {action: replace|insert_after|delete, para | table/row/col,
           new_text | markdown, old_text?}
    xlsx: {action: set_cell|append_rows|add_sheet, sheet, cell?, value?,
           old_value?, rows?, headers?}

No LLM in the loop — this is the studio canvas's inline editor, so it
runs in the request (worker thread) and returns the new revision id
immediately. The input asset is preserved (same revision model as
edit-deck jobs). `pptx_asset_id` request/response field names are kept
from the PPTX-only era for wire compatibility; `doc_asset_id` mirrors
the response value.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...db.models import Asset, AssetKind, Tenant
from ...documents import doc_format_of
from ...storage import get_default_storage
from ...services.assets import upload_asset
from ..errors import bilingual_detail
from ..dependencies import CurrentTenant, DbSession, RequestLocale

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/text-edits", tags=["text-edits"])

_MIME = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class TextEditBody(BaseModel):
    """Format-specific edit object; the asset's engine validates fields."""

    model_config = ConfigDict(extra="allow")


class TextEditsBody(BaseModel):
    pptx_asset_id: uuid.UUID
    edits: list[TextEditBody] = Field(..., min_length=1, max_length=50)
    output_basename: str | None = None


class TextEditsResponse(BaseModel):
    pptx_asset_id: uuid.UUID
    doc_asset_id: uuid.UUID
    format: str
    applied: int
    results: list[dict]


def _apply_for_format(fmt: str, content: bytes, edits: list[dict]):
    """Run the right deterministic engine; returns (bytes, applied, results)."""
    if fmt == "pptx":
        from ...tools.apply_text_edits import (
            ApplyTextEditsRequest,
            TextEdit,
            apply_text_edits,
        )

        resp = apply_text_edits(
            ApplyTextEditsRequest(
                pptx=content, edits=[TextEdit(**e) for e in edits]
            )
        )
        return resp.pptx, resp.applied, [r.model_dump() for r in resp.results]

    if fmt == "docx":
        from ...documents.docx_engine import DocxEdit, apply_docx_edits

        new_content, results = apply_docx_edits(
            content, [DocxEdit(**{"action": "replace", **e}) for e in edits]
        )
        dumped = [
            {"action": r.action, "status": r.status, "message": r.message}
            for r in results
        ]
        return new_content, sum(1 for r in results if r.status == "applied"), dumped

    from ...documents.xlsx_engine import XlsxEdit, apply_xlsx_edits

    new_content, results = apply_xlsx_edits(
        content, [XlsxEdit(**{"action": "set_cell", **e}) for e in edits]
    )
    dumped = [
        {"action": r.action, "status": r.status, "message": r.message}
        for r in results
    ]
    return new_content, sum(1 for r in results if r.status == "applied"), dumped


@router.post("", response_model=TextEditsResponse, summary="Apply direct text edits")
async def apply_doc_text_edits(
    body: TextEditsBody,
    tenant: CurrentTenant,
    session: DbSession,
    locale: RequestLocale,
) -> TextEditsResponse:
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

    fmt = doc_format_of(asset.original_filename, asset.mime_type) or "pptx"
    storage = get_default_storage()
    try:
        content = await storage.get_bytes(asset.storage_key)
    except KeyError:
        # Presigned uploads register the Asset row before the bytes land.
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
        edits = [e.model_dump() for e in body.edits]
        new_content, applied, results = await asyncio.to_thread(
            _apply_for_format, fmt, content, edits
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "TEXT_EDIT_FAILED",
                "message": str(exc),
                "message_en": str(exc),
            },
        ) from exc

    if applied == 0:
        # Nothing changed — don't forge a new revision; report why per edit.
        return TextEditsResponse(
            pptx_asset_id=body.pptx_asset_id,
            doc_asset_id=body.pptx_asset_id,
            format=fmt,
            applied=0,
            results=results,
        )

    tenant_row = (
        await session.execute(select(Tenant).where(Tenant.id == tenant.id))
    ).scalar_one()
    basename = body.output_basename or (
        asset.original_filename or f"document.{fmt}"
    ).rsplit(".", 1)[0]
    upload = await upload_asset(
        session=session,
        storage=storage,
        tenant=tenant_row,
        kind=AssetKind(fmt),
        content=new_content,
        original_filename=f"{basename}.{fmt}",
        mime_type=_MIME[fmt],
        project_id=asset.project_id,
    )
    return TextEditsResponse(
        pptx_asset_id=upload.asset.id,
        doc_asset_id=upload.asset.id,
        format=fmt,
        applied=applied,
        results=results,
    )
