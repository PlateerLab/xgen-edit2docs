"""Asset service: bridges the FastAPI layer to DB + Object Storage.

The handlers stay thin — all the "what should happen when a user uploads a
file" logic lives here. This makes the workflow testable without hitting
FastAPI (we just call the service methods with a session + storage).

Storage key layout (see ppt-master-analysis/04 §4.7):
    tenants/<tenant_id>/<kind_dir>/<asset_id>.<ext>

`<kind_dir>` is the AssetKind value (sources / pptx / svg / ...).
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Asset, AssetKind, Tenant
from ..storage import ObjectStorage, build_content_disposition

# Hard ceiling. Per-tenant overrides arrive in M6.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

# Per-kind directory under tenants/<tid>/.
_KIND_DIR = {
    AssetKind.source: "sources",
    AssetKind.markdown: "markdown",
    AssetKind.spec_lock: "spec_lock",
    AssetKind.svg: "svg",
    AssetKind.image: "images",
    AssetKind.pptx: "pptx",
    AssetKind.docx: "docx",
    AssetKind.xlsx: "xlsx",
    AssetKind.audio: "audio",
    AssetKind.preview: "previews",
}


@dataclass
class UploadResult:
    asset: Asset
    storage_key: str
    sha256: str


@dataclass
class DownloadInfo:
    url: str
    expires_in_seconds: int
    filename: str | None
    mime_type: str


class AssetError(Exception):
    """Base class for asset-service business errors. Maps to 4xx in the API."""

    status_code = 400
    code = "ASSET_ERROR"
    message_key: str | None = None
    vars: dict = {}


class AssetNotFound(AssetError):
    status_code = 404
    code = "ASSET_NOT_FOUND"

    def __init__(self, asset_id: str):
        super().__init__(f"asset {asset_id!r} not found")
        self.vars = {"asset_id": asset_id}
        self.message_key = "errors.asset_not_found"


class AssetTooLarge(AssetError):
    status_code = 413
    code = "SOURCE_TOO_LARGE"

    def __init__(self, size: int, limit: int = MAX_UPLOAD_BYTES):
        super().__init__(f"upload exceeds {limit} bytes (got {size})")
        self.vars = {
            "size_mb": round(size / 1024 / 1024, 2),
            "limit_mb": round(limit / 1024 / 1024, 2),
        }
        self.message_key = "errors.source_too_large"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def _safe_ext(filename: str | None, fallback_mime: str | None = None) -> str:
    """Return a leading-dot extension chosen from the filename or mime.

    Always ASCII; falls back to '' if neither input yields one.
    """
    if filename:
        ext = PurePosixPath(filename).suffix
        try:
            ext.encode("ascii")
            if ext:
                return ext
        except UnicodeEncodeError:
            pass
    if fallback_mime:
        guessed = mimetypes.guess_extension(fallback_mime) or ""
        if guessed:
            return guessed
    return ""


def _storage_key(tenant_id: uuid.UUID, kind: AssetKind, asset_id: uuid.UUID, ext: str) -> str:
    """Build the canonical object key for an asset. Track A: ASCII only."""
    kind_dir = _KIND_DIR[kind]
    return f"tenants/{tenant_id}/{kind_dir}/{asset_id}{ext}"


async def upload_asset(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    tenant: Tenant,
    kind: AssetKind,
    content: bytes,
    original_filename: str | None,
    mime_type: str | None,
    project_id: uuid.UUID | None = None,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> UploadResult:
    """Persist *content* to storage, record metadata in DB, return the Asset row.

    Korean filenames in *original_filename* are stored verbatim on the row;
    the storage key is built from UUID + extension so it stays ASCII.
    """
    size = len(content)
    if size > max_bytes:
        raise AssetTooLarge(size=size, limit=max_bytes)

    asset_id = uuid.uuid4()
    ext = _safe_ext(original_filename, fallback_mime=mime_type)
    storage_key = _storage_key(tenant.id, kind, asset_id, ext)

    effective_mime = mime_type or (mimetypes.guess_type(original_filename or "")[0]) or "application/octet-stream"
    sha = hashlib.sha256(content).hexdigest()

    # Storage write FIRST. If DB insert fails we leave an orphaned object —
    # cheap, swept by a TTL cleaner. The reverse order would orphan a DB row
    # pointing at nothing, which is worse for callers.
    await storage.put_bytes(storage_key, content, content_type=effective_mime)

    asset = Asset(
        id=asset_id,
        tenant_id=tenant.id,
        project_id=project_id,
        kind=kind,
        original_filename=original_filename,
        storage_key=storage_key,
        mime_type=effective_mime,
        size=size,
        sha256=sha,
    )
    session.add(asset)
    await session.flush()
    return UploadResult(asset=asset, storage_key=storage_key, sha256=sha)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def get_asset(
    *,
    session: AsyncSession,
    tenant: Tenant,
    asset_id: uuid.UUID,
) -> Asset:
    """Look up an asset scoped to the caller's tenant."""
    stmt = select(Asset).where(Asset.id == asset_id, Asset.tenant_id == tenant.id)
    result = (await session.execute(stmt)).scalar_one_or_none()
    if result is None:
        raise AssetNotFound(asset_id=str(asset_id))
    return result


async def build_download(
    *,
    session: AsyncSession,
    storage: ObjectStorage,
    tenant: Tenant,
    asset_id: uuid.UUID,
    expires_in_seconds: int = 300,
) -> DownloadInfo:
    asset = await get_asset(session=session, tenant=tenant, asset_id=asset_id)
    presigned = await storage.presigned_get_url(
        asset.storage_key,
        expires_in_seconds=expires_in_seconds,
        response_filename=asset.original_filename,
        response_content_type=asset.mime_type,
    )
    return DownloadInfo(
        url=presigned.url,
        expires_in_seconds=presigned.expires_in_seconds,
        filename=asset.original_filename,
        mime_type=asset.mime_type,
    )


def content_disposition_header(filename: str) -> str:
    """Convenience re-export so route handlers don't have to import storage internals."""
    return build_content_disposition(filename)
