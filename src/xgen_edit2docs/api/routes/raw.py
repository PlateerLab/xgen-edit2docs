"""`/v1/raw/{key}` — serves files for LocalFilesystemStorage's presigned URLs.

When the engine runs without external S3, asset downloads are signed URLs
that hit *this endpoint* (instead of the MinIO presigned URL pattern).
The signature is HMAC-SHA256 over `METHOD\\nKEY\\nEXPIRES`; the storage
adapter mints them in `LocalFilesystemStorage.presigned_get_url`.

Response carries the Korean filename via RFC 5987 Content-Disposition,
matching the contract the S3 adapter satisfies via ResponseContentDisposition.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ...storage import LocalFilesystemStorage, build_content_disposition, get_default_storage
from ..dependencies import RequestLocale
from ..errors import bilingual_detail

router = APIRouter(prefix="/v1/raw", tags=["assets"])


@router.get("/{key:path}")
async def serve_raw(
    key: str,
    locale: RequestLocale,
    e: int = Query(..., description="Expiry as a Unix timestamp"),
    s: str = Query(..., description="HMAC-SHA256 signature in lowercase hex"),
    fn: str | None = Query(default=None, description="Original filename (Unicode OK)"),
    ct: str | None = Query(default=None, description="Response Content-Type override"),
) -> Response:
    """Serve a single local-fs object after verifying the HMAC signature."""
    storage = get_default_storage()
    if not isinstance(storage, LocalFilesystemStorage):
        # Other backends (S3, in-memory) don't use this endpoint.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=bilingual_detail(
                "RAW_DISABLED",
                en="This endpoint is only enabled with LocalFilesystemStorage.",
                ko="이 엔드포인트는 LocalFilesystemStorage 에서만 활성화됩니다.",
                locale=locale,
            ),
        )

    if not storage.verify(key=key, method="GET", expires=e, sig=s):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=bilingual_detail(
                "RAW_SIGNATURE_INVALID",
                en="URL signature missing, mismatched, or expired.",
                ko="URL 서명이 잘못되었거나 만료되었습니다.",
                locale=locale,
            ),
        )

    try:
        data = await storage.get_bytes(key)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=bilingual_detail(
                "RAW_NOT_FOUND",
                en="Object not found.",
                ko="해당 자산 파일을 찾을 수 없습니다.",
                locale=locale,
            ),
        ) from exc

    headers = {}
    if fn:
        headers["Content-Disposition"] = build_content_disposition(fn)
    return Response(
        content=data,
        media_type=ct or "application/octet-stream",
        headers=headers,
    )
