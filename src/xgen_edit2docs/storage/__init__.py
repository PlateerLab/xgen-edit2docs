"""Object storage layer for xgen_edit2docs: pluggable adapters.

- `LocalFilesystemStorage` — standalone default; files under a single
  directory tree; presigned URLs are HMAC-signed pointers to the engine's
  own `/v1/raw/{key}` endpoint.
- `S3Storage` — production-grade; AWS S3 / MinIO / Cloudflare R2.
- `InMemoryStorage` — tests.

`get_default_storage()` picks the right adapter based on Settings — S3 when
both `s3_endpoint_url` and `s3_bucket` are configured, local-fs otherwise.

See ppt-master-analysis/04-integration-plan.md §4.7 and
ppt-master-analysis/06-bilingual-conventions.md §6.6.3 for the
Korean-filename-on-download story.
"""

from .base import ObjectStorage, PresignedUrl, StoredObject, assert_ascii_key
from .content_disposition import build_content_disposition
from .local_fs import LocalFilesystemStorage
from .memory import InMemoryStorage

# S3 is imported lazily so test environments without aioboto3 still work.
__all__ = [
    "ObjectStorage",
    "PresignedUrl",
    "StoredObject",
    "assert_ascii_key",
    "build_content_disposition",
    "LocalFilesystemStorage",
    "InMemoryStorage",
    "get_default_storage",
    "set_default_storage",
]


_storage_override: ObjectStorage | None = None


def set_default_storage(storage: ObjectStorage | None) -> None:
    """Tests / dev tooling: swap the process-wide storage backend."""
    global _storage_override
    _storage_override = storage
    if hasattr(get_default_storage, "_instance"):
        delattr(get_default_storage, "_instance")


def get_default_storage() -> ObjectStorage:
    """Return the process-wide storage backend.

    Resolution order:
    1. `set_default_storage()` override (tests + the FastAPI deps).
    2. `S3Storage` if `settings.uses_s3_storage` is True.
    3. `LocalFilesystemStorage` rooted at `settings.data_dir/storage`.

    The chosen instance is cached on the function object so we don't
    re-instantiate on every request.
    """
    if _storage_override is not None:
        return _storage_override

    cached = getattr(get_default_storage, "_instance", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    from ..config import get_settings

    settings = get_settings()
    if settings.uses_s3_storage:
        from .s3 import S3Storage  # local import: aioboto3 only required for S3
        instance: ObjectStorage = S3Storage(settings=settings)
    else:
        instance = LocalFilesystemStorage(
            root=settings.data_dir / "storage",
            signing_key=settings.raw_signing_key,
        )

    get_default_storage._instance = instance  # type: ignore[attr-defined]
    return instance
