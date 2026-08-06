"""Local-filesystem object-storage adapter.

Production-ish standalone alternative to the S3 adapter. Files live under a
single root directory; "presigned URLs" are minted as HMAC-signed pointers
back at the engine's own `/v1/raw/{key}` endpoint (see api/routes/raw.py).

Trade-offs vs S3:
- No multi-host scaling (single-writer filesystem); fine for single-instance demos.
- No object lifecycle policies; the engine's TTL housekeeping has to delete files.
- Presigned URLs are served by the engine process itself, so the byte stream
  flows through Python — slower than S3's edge but tolerable for small decks.

Switch to `S3Storage` by setting `EDIT2DOCS_S3_ENDPOINT_URL` + `EDIT2DOCS_S3_BUCKET`.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from .base import ObjectStorage, PresignedUrl, StoredObject, assert_ascii_key


class LocalFilesystemStorage(ObjectStorage):
    """ObjectStorage backed by a directory tree on the local filesystem.

    Args:
        root: directory under which every object key resolves. Must be writable.
        signing_key: HMAC-SHA256 key used to mint presigned URLs.
        public_base_path: URL prefix (relative to the API host) at which the
            engine's `/v1/raw` route serves files. Default `/v1/raw`.
    """

    def __init__(
        self,
        root: Path,
        signing_key: str,
        public_base_path: str = "/v1/raw",
    ):
        self.root = Path(root).resolve()
        self.signing_key = signing_key.encode("utf-8")
        self.public_base_path = public_base_path.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path resolution + safety
    # ------------------------------------------------------------------

    def _resolve(self, key: str) -> Path:
        """Resolve a storage key to a filesystem path, refusing traversal."""
        assert_ascii_key(key)
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"unsafe storage key: {key!r}")
        target = (self.root / key).resolve()
        if not str(target).startswith(str(self.root)):
            raise ValueError(f"key resolves outside storage root: {key!r}")
        return target

    # ------------------------------------------------------------------
    # ObjectStorage contract
    # ------------------------------------------------------------------

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(key=key, size=len(data), content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise KeyError(key)
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    async def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    async def presigned_put_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        content_type: str | None = None,
    ) -> PresignedUrl:
        """Mint a signed URL the caller can PUT to.

        Not enabled in the current API surface (the `POST /v1/assets/presigned`
        route is dormant). We return a URL anyway so the API contract stays
        unified across adapters.
        """
        url = self._build_url(
            key,
            method="PUT",
            expires_in_seconds=expires_in_seconds,
            content_type=content_type,
        )
        return PresignedUrl(url=url, expires_in_seconds=expires_in_seconds)

    async def presigned_get_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        response_filename: str | None = None,
        response_content_type: str | None = None,
    ) -> PresignedUrl:
        url = self._build_url(
            key,
            method="GET",
            expires_in_seconds=expires_in_seconds,
            filename=response_filename,
            content_type=response_content_type,
        )
        return PresignedUrl(url=url, expires_in_seconds=expires_in_seconds)

    # ------------------------------------------------------------------
    # URL signing
    # ------------------------------------------------------------------

    def _build_url(
        self,
        key: str,
        *,
        method: str,
        expires_in_seconds: int,
        filename: str | None = None,
        content_type: str | None = None,
    ) -> str:
        expires = int(time.time()) + expires_in_seconds
        canon = f"{method}\n{key}\n{expires}"
        sig = hmac.new(self.signing_key, canon.encode("utf-8"), hashlib.sha256).hexdigest()
        params: dict[str, str] = {"e": str(expires), "s": sig}
        if filename is not None:
            params["fn"] = filename
        if content_type is not None:
            params["ct"] = content_type
        return f"{self.public_base_path}/{quote(key)}?{urlencode(params)}"

    def verify(
        self,
        key: str,
        method: str,
        expires: int,
        sig: str,
    ) -> bool:
        """Constant-time HMAC verification used by /v1/raw."""
        if expires < int(time.time()):
            return False
        canon = f"{method}\n{key}\n{expires}"
        expected = hmac.new(self.signing_key, canon.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
