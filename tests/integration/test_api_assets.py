"""Integration tests for /v1/assets endpoints.

The test app runs against a fresh in-memory SQLite database and
InMemoryStorage on each test, so no Postgres / Redis / MinIO is required.

Verifies the Korean filename pipeline survives a full upload -> metadata ->
download roundtrip: original_filename round-trips through the DB and shows
up in the presigned download URL's Content-Disposition.
"""

from __future__ import annotations

import urllib.parse
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.api import dependencies as deps
from xgen_edit2docs.api.main import app
from xgen_edit2docs.db.models import Base
from xgen_edit2docs.storage import InMemoryStorage


@pytest_asyncio.fixture
async def test_storage() -> InMemoryStorage:
    s = InMemoryStorage()
    deps.set_test_storage(s)
    yield s
    deps.set_test_storage(None)


@pytest_asyncio.fixture
async def test_db():
    """Fresh in-memory SQLite engine + override get_db_session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[deps.get_db_session] = _override
    try:
        yield maker
    finally:
        app.dependency_overrides.pop(deps.get_db_session, None)
        await engine.dispose()


@pytest_asyncio.fixture
async def client(test_storage, test_db) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Upload + metadata + download roundtrip with a Korean filename
# ---------------------------------------------------------------------------


class TestKoreanFilenameRoundTrip:
    @pytest.mark.asyncio
    async def test_upload_returns_metadata(self, client: httpx.AsyncClient):
        files = {"file": ("Q3 영업보고서.pdf", b"PDF DATA", "application/pdf")}
        resp = await client.post("/v1/assets", files=files)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["original_filename"] == "Q3 영업보고서.pdf"
        assert body["kind"] == "source"
        assert body["mime_type"] == "application/pdf"
        assert body["size"] == len(b"PDF DATA")
        # Storage key must be ASCII (Track A).
        body["storage_key"].encode("ascii")
        assert "보고서" not in body["storage_key"]

    @pytest.mark.asyncio
    async def test_download_url_carries_korean_filename(
        self,
        client: httpx.AsyncClient,
        test_storage: InMemoryStorage,
    ):
        # Upload first.
        files = {"file": ("Q3 영업보고서.pdf", b"PDF DATA", "application/pdf")}
        up = await client.post("/v1/assets", files=files)
        asset_id = up.json()["id"]

        # Then request a download URL.
        dl = await client.get(f"/v1/assets/{asset_id}/download")
        assert dl.status_code == 200, dl.text
        body = dl.json()
        assert body["filename"] == "Q3 영업보고서.pdf"

        # InMemoryStorage emits memory:// URLs with the Content-Disposition
        # baked into the query string. Decode it and verify the original
        # Korean filename round-trips.
        parsed = urllib.parse.urlparse(body["download_url"])
        params = urllib.parse.parse_qs(parsed.query)
        disposition = params["X-Test-Content-Disposition"][0]
        rfc = disposition.split("filename*=UTF-8''", 1)[1]
        assert urllib.parse.unquote(rfc) == "Q3 영업보고서.pdf"

    @pytest.mark.asyncio
    async def test_storage_object_actually_holds_bytes(
        self,
        client: httpx.AsyncClient,
        test_storage: InMemoryStorage,
    ):
        files = {"file": ("plain.txt", b"hello", "text/plain")}
        up = await client.post("/v1/assets", files=files)
        key = up.json()["storage_key"]
        data = await test_storage.get_bytes(key)
        assert data == b"hello"


# ---------------------------------------------------------------------------
# Presigned PUT (large uploads)
# ---------------------------------------------------------------------------


class TestPresignedUpload:
    @pytest.mark.asyncio
    async def test_presigned_url_returned(self, client: httpx.AsyncClient):
        body = {
            "kind": "source",
            "original_filename": "원본자료.pdf",
            "mime_type": "application/pdf",
        }
        resp = await client.post("/v1/assets/presigned", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["asset_id"]
        assert data["upload_url"].startswith("memory://upload/")
        # Storage key must be ASCII.
        data["storage_key"].encode("ascii")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_get_unknown_asset_404(self, client: httpx.AsyncClient):
        from uuid import uuid4

        resp = await client.get(f"/v1/assets/{uuid4()}")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == "ASSET_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_korean_error_message_when_korean_locale(self, client: httpx.AsyncClient):
        from uuid import uuid4

        resp = await client.get(
            f"/v1/assets/{uuid4()}",
            headers={"Accept-Language": "ko-KR"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert "찾을 수 없" in body["error"]["message"]
        assert "not found" in body["error"]["message_en"].lower()


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_storage_object_and_row(
        self,
        client: httpx.AsyncClient,
        test_storage: InMemoryStorage,
    ):
        up = await client.post(
            "/v1/assets",
            files={"file": ("x.txt", b"hi", "text/plain")},
        )
        asset_id = up.json()["id"]
        storage_key = up.json()["storage_key"]

        del_resp = await client.delete(f"/v1/assets/{asset_id}")
        assert del_resp.status_code == 204

        # Subsequent GET returns 404.
        again = await client.get(f"/v1/assets/{asset_id}")
        assert again.status_code == 404
        # Storage object is gone.
        assert not await test_storage.exists(storage_key)
