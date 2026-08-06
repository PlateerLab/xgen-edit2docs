"""S3-compatible object storage adapter (works with AWS S3, MinIO, Cloudflare R2).

Built on aioboto3 — async-native, batteries-included. Tries hard to:
- Never log the access secret or presigned URL contents
- Surface "object missing" as KeyError (not a 12-level botocore exception)
- Encode Korean filenames on presigned GET URLs via Content-Disposition

For tests we use the in-memory adapter in `storage.memory`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..config import Settings, get_settings
from .base import ObjectStorage, PresignedUrl, StoredObject, assert_ascii_key
from .content_disposition import build_content_disposition


class S3Storage(ObjectStorage):
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._session = None  # aioboto3.Session, created lazily

    def _build_session(self):
        import aioboto3  # local import: tests that use memory storage don't need this dep

        return aioboto3.Session(
            aws_access_key_id=self._settings.s3_access_key_id,
            aws_secret_access_key=self._settings.s3_secret_access_key,
            region_name=self._settings.s3_region,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"service_name": "s3"}
        if self._settings.s3_endpoint_url:
            kwargs["endpoint_url"] = self._settings.s3_endpoint_url
        return kwargs

    @property
    def session(self):
        if self._session is None:
            self._session = self._build_session()
        return self._session

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> StoredObject:
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            extra: dict[str, Any] = {}
            if content_type:
                extra["ContentType"] = content_type
            await client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return StoredObject(key=key, size=len(data), content_type=content_type)

    async def get_bytes(self, key: str) -> bytes:
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            try:
                response = await client.get_object(Bucket=self.bucket, Key=key)
            except client.exceptions.NoSuchKey as exc:
                raise KeyError(key) from exc
            body = await response["Body"].read()
            return body

    async def delete(self, key: str) -> None:
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            await client.delete_object(Bucket=self.bucket, Key=key)

    async def exists(self, key: str) -> bool:
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            try:
                await client.head_object(Bucket=self.bucket, Key=key)
                return True
            except client.exceptions.NoSuchKey:
                return False
            except Exception as exc:
                # Some S3-compatible backends raise ClientError("404") instead of NoSuchKey.
                if "404" in str(exc) or "Not Found" in str(exc):
                    return False
                raise

    async def presigned_put_url(
        self,
        key: str,
        *,
        expires_in_seconds: int = 300,
        content_type: str | None = None,
    ) -> PresignedUrl:
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
            if content_type:
                params["ContentType"] = content_type
            url = await client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=expires_in_seconds,
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
        assert_ascii_key(key)
        async with self.session.client(**self._client_kwargs()) as client:
            params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
            if response_filename:
                # S3 signs the Content-Disposition value and returns it as-is on GET.
                params["ResponseContentDisposition"] = build_content_disposition(
                    response_filename
                )
            if response_content_type:
                params["ResponseContentType"] = response_content_type
            url = await client.generate_presigned_url(
                ClientMethod="get_object",
                Params=params,
                ExpiresIn=expires_in_seconds,
            )
        return PresignedUrl(url=url, expires_in_seconds=expires_in_seconds)


async def ensure_bucket_exists(storage: S3Storage) -> None:
    """Create the configured bucket if it doesn't already exist.

    Best-effort: used by the dev bootstrap script. Production deployments
    create the bucket out-of-band with proper IAM policies.
    """
    async with storage.session.client(**storage._client_kwargs()) as client:
        try:
            await client.head_bucket(Bucket=storage.bucket)
            return
        except Exception:
            pass  # fallthrough to create
        await client.create_bucket(Bucket=storage.bucket)


def run_ensure_bucket() -> None:  # pragma: no cover - CLI bootstrap helper
    """Synchronous entrypoint suitable for `python -m xgen_edit2docs.storage.s3`."""
    asyncio.run(ensure_bucket_exists(S3Storage()))
