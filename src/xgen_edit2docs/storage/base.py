"""Abstract object-storage interface.

Lets the application talk to "some blob store" without knowing whether it's
S3, MinIO, R2, or a local filesystem (tests). All methods are async so the
real S3 adapter can do non-blocking I/O.

Storage keys must be ASCII (Track A — see ppt-master-analysis/06-bilingual-conventions.md);
the human-readable filename a user uploaded lives elsewhere (DB column
`assets.original_filename`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import BinaryIO


@dataclass
class StoredObject:
    """Metadata for a stored object."""

    key: str
    size: int
    content_type: str | None = None


@dataclass
class PresignedUrl:
    url: str
    expires_in_seconds: int


class ObjectStorage(ABC):
    """Pluggable object-storage backend (S3 / MinIO / R2 / in-memory)."""

    @abstractmethod
    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        """Upload bytes at *key*. Overwrites any existing object."""

    @abstractmethod
    async def get_bytes(self, key: str) -> bytes:
        """Download the full object. Raises KeyError if absent."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete *key*. Idempotent (no error if missing)."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    async def presigned_put_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        content_type: str | None = None,
    ) -> PresignedUrl:
        """Issue a short-lived URL that lets the client PUT directly to storage."""

    @abstractmethod
    async def presigned_get_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        response_filename: str | None = None,
        response_content_type: str | None = None,
    ) -> PresignedUrl:
        """Issue a short-lived URL that lets the client GET directly from storage.

        *response_filename* may be any Unicode string (Korean OK). The backend
        encodes it via RFC 5987 in the response's Content-Disposition header so
        the user's browser/curl/agent saves the file under the original name.
        """


def assert_ascii_key(key: str) -> None:
    """Track A enforcement: storage keys must be ASCII."""
    try:
        key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Storage key must be ASCII (got non-ASCII char). Korean filenames belong "
            f"in DB.assets.original_filename, not in storage keys. Key={key!r}"
        ) from exc
