"""Asset upload / metadata / download endpoints.

Routes:
    POST   /v1/assets             — multipart upload
    POST   /v1/assets/presigned   — request a presigned PUT URL (large files)
    GET    /v1/assets/{id}        — metadata
    GET    /v1/assets/{id}/download — short-lived signed GET URL
    DELETE /v1/assets/{id}        — delete object + row

See ppt-master-analysis/04-integration-plan.md §4.4.3.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from ...db.models import Asset, AssetKind
from ...services.assets import (
    DownloadInfo,
    MAX_UPLOAD_BYTES,
    UploadResult,
    build_download,
    get_asset,
    upload_asset,
)
from ..errors import bilingual_detail
from ..dependencies import Catalog, CurrentTenant, DbSession, RequestLocale, Storage

router = APIRouter(prefix="/v1/assets", tags=["assets"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetMetadata(BaseModel):
    """Public view of an Asset row. Keys are English snake_case (Track A);
    `original_filename` may be Korean / any Unicode (Track C)."""

    id: uuid.UUID
    kind: AssetKind
    original_filename: str | None
    mime_type: str
    size: int
    sha256: str | None
    storage_key: str = Field(..., description="ASCII-only object storage key.")
    project_id: uuid.UUID | None
    created_at: str  # ISO-8601

    @classmethod
    def from_row(cls, asset: Asset) -> "AssetMetadata":
        return cls(
            id=asset.id,
            kind=asset.kind,
            original_filename=asset.original_filename,
            mime_type=asset.mime_type,
            size=asset.size,
            sha256=asset.sha256,
            storage_key=asset.storage_key,
            project_id=asset.project_id,
            created_at=asset.created_at.isoformat(),
        )


class PresignedUploadResponse(BaseModel):
    asset_id: uuid.UUID
    storage_key: str
    upload_url: str
    expires_in_seconds: int


class DownloadResponse(BaseModel):
    download_url: str
    expires_in_seconds: int
    filename: str | None
    mime_type: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=AssetMetadata,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a source / asset via multipart form",
)
async def upload(
    tenant: CurrentTenant,
    session: DbSession,
    storage: Storage,
    locale: RequestLocale,
    file: Annotated[UploadFile, File(description="File contents")],
    kind: Annotated[AssetKind, Form(description="Asset kind (default: source)")] = AssetKind.source,
    project_id: Annotated[uuid.UUID | None, Form()] = None,
) -> AssetMetadata:
    """Accept a multipart upload, persist to object storage, register in DB.

    The user's Korean filename is preserved in `original_filename`. The
    object storage key uses the asset's UUID + safe extension (Track A).
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        # Service layer also enforces this; surface a clean 413 here for the
        # client before we touch storage.
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=bilingual_detail(
                "SOURCE_TOO_LARGE",
                en="Upload exceeds size limit.",
                ko="업로드 파일이 제한을 초과했습니다.",
                locale=locale,
            ),
        )
    result: UploadResult = await upload_asset(
        session=session,
        storage=storage,
        tenant=tenant,
        kind=kind,
        content=content,
        original_filename=file.filename,
        mime_type=file.content_type,
        project_id=project_id,
    )
    return AssetMetadata.from_row(result.asset)


class PresignedUploadRequest(BaseModel):
    kind: AssetKind = AssetKind.source
    original_filename: str = Field(..., description="User-facing filename (any Unicode).")
    mime_type: str = "application/octet-stream"
    project_id: uuid.UUID | None = None
    expires_in_seconds: int = Field(default=300, ge=30, le=3600)


@router.post(
    "/presigned",
    response_model=PresignedUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Allocate a presigned PUT URL for large uploads",
)
async def presigned_upload(
    tenant: CurrentTenant,
    session: DbSession,
    storage: Storage,
    body: PresignedUploadRequest,
) -> PresignedUploadResponse:
    """Pre-register an asset row and return a short-lived PUT URL.

    The client uploads directly to object storage; on success the row already
    exists in DB. (M3.5's generate-deck flow accepts asset_id even before
    the bytes have been PUT; the worker checks `storage.exists()` first.)
    """
    from ...services.assets import _safe_ext, _storage_key  # internal helpers

    asset_id = uuid.uuid4()
    ext = _safe_ext(body.original_filename, fallback_mime=body.mime_type)
    storage_key = _storage_key(tenant.id, body.kind, asset_id, ext)

    presigned = await storage.presigned_put_url(
        storage_key,
        expires_in_seconds=body.expires_in_seconds,
        content_type=body.mime_type,
    )

    asset = Asset(
        id=asset_id,
        tenant_id=tenant.id,
        project_id=body.project_id,
        kind=body.kind,
        original_filename=body.original_filename,
        storage_key=storage_key,
        mime_type=body.mime_type,
        size=0,  # filled in by a verify-after-upload pass; M3.5 handles it
    )
    session.add(asset)
    await session.flush()
    return PresignedUploadResponse(
        asset_id=asset_id,
        storage_key=storage_key,
        upload_url=presigned.url,
        expires_in_seconds=presigned.expires_in_seconds,
    )


@router.get(
    "/{asset_id}",
    response_model=AssetMetadata,
    summary="Asset metadata",
)
async def metadata(
    asset_id: uuid.UUID,
    tenant: CurrentTenant,
    session: DbSession,
) -> AssetMetadata:
    asset = await get_asset(session=session, tenant=tenant, asset_id=asset_id)
    return AssetMetadata.from_row(asset)


@router.get(
    "/{asset_id}/download",
    response_model=DownloadResponse,
    summary="Issue a presigned download URL (Korean filenames preserved)",
)
async def download(
    asset_id: uuid.UUID,
    tenant: CurrentTenant,
    session: DbSession,
    storage: Storage,
    expires_in_seconds: Annotated[int, Query(ge=30, le=3600)] = 300,
) -> DownloadResponse:
    info: DownloadInfo = await build_download(
        session=session,
        storage=storage,
        tenant=tenant,
        asset_id=asset_id,
        expires_in_seconds=expires_in_seconds,
    )
    return DownloadResponse(
        download_url=info.url,
        expires_in_seconds=info.expires_in_seconds,
        filename=info.filename,
        mime_type=info.mime_type,
    )


@router.delete(
    "/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an asset (storage object + DB row)",
)
async def delete(
    asset_id: uuid.UUID,
    tenant: CurrentTenant,
    session: DbSession,
    storage: Storage,
) -> None:
    asset = await get_asset(session=session, tenant=tenant, asset_id=asset_id)
    await storage.delete(asset.storage_key)
    await session.delete(asset)
    await session.flush()
