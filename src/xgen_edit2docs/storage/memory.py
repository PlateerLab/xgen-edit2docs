"""In-memory object storage adapter — for tests only.

Behaves like a dict-of-bytes that satisfies the same ObjectStorage contract
as the S3 adapter. Presigned URLs are stub strings the test can recognize.
Two separate InMemoryStorage instances do NOT share state, which makes them
safe to use in parallel test scenarios.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field

from .base import ObjectStorage, PresignedUrl, StoredObject, assert_ascii_key
from .content_disposition import build_content_disposition


@dataclass
class _StoredItem:
    data: bytes
    content_type: str | None = None


@dataclass
class InMemoryStorage(ObjectStorage):
    """All objects live in this dict. Reset by constructing a new instance."""

    _store: dict[str, _StoredItem] = field(default_factory=dict)

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        assert_ascii_key(key)
        self._store[key] = _StoredItem(data=data, content_type=content_type)
        return StoredObject(key=key, size=len(data), content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        assert_ascii_key(key)
        if key not in self._store:
            raise KeyError(key)
        return self._store[key].data

    async def delete(self, key: str) -> None:
        assert_ascii_key(key)
        self._store.pop(key, None)

    async def exists(self, key: str) -> bool:
        assert_ascii_key(key)
        return key in self._store

    async def presigned_put_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        content_type: str | None = None,
    ) -> PresignedUrl:
        assert_ascii_key(key)
        params = {"X-Test-Method": "PUT"}
        if content_type:
            params["X-Test-Content-Type"] = content_type
        url = f"memory://upload/{urllib.parse.quote(key)}?{urllib.parse.urlencode(params)}"
        return PresignedUrl(url=url, expires_in_seconds=expires_in_seconds)

    async def presigned_get_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        response_filename: str | None = None,
        response_content_type: str | None = None,
    ) -> PresignedUrl:
        assert_ascii_key(key)
        params = {"X-Test-Method": "GET"}
        if response_filename:
            params["X-Test-Content-Disposition"] = build_content_disposition(response_filename)
        if response_content_type:
            params["X-Test-Content-Type"] = response_content_type
        url = f"memory://download/{urllib.parse.quote(key)}?{urllib.parse.urlencode(params)}"
        return PresignedUrl(url=url, expires_in_seconds=expires_in_seconds)
