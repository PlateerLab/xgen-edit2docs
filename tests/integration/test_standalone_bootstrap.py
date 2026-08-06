"""Integration test for the standalone-mode bootstrap.

Spins up a FastAPI app pointing at:
- An ephemeral SQLite database under a tmp_path data_dir
- LocalFilesystemStorage (no S3 env vars set)
- No Redis URL (inline queue mode)

Asserts that `bootstrap()` then HTTP requests against a fresh deployment
just work end-to-end:

1. The data_dir + storage subdir get created automatically.
2. The schema is created via metadata.create_all (no Alembic needed).
3. /v1/assets multipart upload writes a file under data_dir/storage/.
4. /v1/assets/{id}/download yields an HMAC-signed /v1/raw URL.
5. GET /v1/raw/<key> serves the file with the Korean Content-Disposition.

If any of this regresses, an operator deploying the engine on a fresh
volume will get a 5xx instead of a working app — so the test fails loudly.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from xgen_edit2docs.config import get_settings, reset_settings_cache


@pytest_asyncio.fixture
async def standalone_env(tmp_path, monkeypatch):
    """Configure Settings to point at a fresh, empty filesystem.

    Sets EDIT2DOCS_DATA_DIR + clears DATABASE_URL/REDIS/S3 so we exercise
    the standalone defaults end-to-end.
    """
    monkeypatch.setenv("EDIT2DOCS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("EDIT2DOCS_DATABASE_URL", raising=False)
    monkeypatch.delenv("EDIT2DOCS_REDIS_URL", raising=False)
    monkeypatch.delenv("EDIT2DOCS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("EDIT2DOCS_S3_BUCKET", raising=False)
    monkeypatch.setenv("EDIT2DOCS_RAW_SIGNING_KEY", "test-signing-key" * 4)
    monkeypatch.delenv("EDIT2DOCS_AUTH_DEV_API_KEY", raising=False)

    reset_settings_cache()
    settings = get_settings()
    assert settings.uses_sqlite, settings.database_url
    assert not settings.uses_s3_storage
    assert not settings.uses_redis_queue

    # Reset the db_session + storage caches so each test gets a fresh
    # engine bound to the new SQLite URL.
    from xgen_edit2docs.db import session as session_mod
    from xgen_edit2docs.storage import set_default_storage

    session_mod.reset_engine_cache()
    set_default_storage(None)
    # Drop any cached default storage instance from a prior test run.
    from xgen_edit2docs.storage import get_default_storage

    if hasattr(get_default_storage, "_instance"):
        delattr(get_default_storage, "_instance")

    yield settings

    # Cleanup so the next test starts fresh.
    set_default_storage(None)
    session_mod.reset_engine_cache()
    reset_settings_cache()


@pytest_asyncio.fixture
async def app(standalone_env):
    """Import the FastAPI app *after* the env vars are in place so the
    lifespan picks up the standalone settings."""
    # Importing late is critical — `from xgen_edit2docs.api.main import app` reads
    # get_settings() at import time only inside lifespan, but the storage
    # default-getter resolves lazily, so an early import would have been
    # OK too. Keep the late import for clarity.
    from xgen_edit2docs.api.main import app  # noqa: PLC0415

    return app


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    # Drive the app's lifespan explicitly so bootstrap runs.
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000"
    ) as ac:
        async with app.router.lifespan_context(app):  # type: ignore[arg-type]
            yield ac


# ---------------------------------------------------------------------------
# Boot-time bootstrap creates the data dir + tables
# ---------------------------------------------------------------------------


class TestBootstrap:
    @pytest.mark.asyncio
    async def test_data_dir_and_storage_subdir_created(self, client, standalone_env):
        assert standalone_env.data_dir.exists()
        assert (standalone_env.data_dir / "storage").is_dir()

    @pytest.mark.asyncio
    async def test_sqlite_file_created_with_tables(self, client, standalone_env):
        sqlite_path = standalone_env.data_dir / "xgen_edit2docs.db"
        assert sqlite_path.exists()

        # All six tables present.
        from xgen_edit2docs.db.session import get_engine
        from sqlalchemy import inspect

        engine = get_engine()
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync: set(inspect(sync).get_table_names())
            )
        expected = {"tenants", "api_keys", "projects", "assets", "jobs", "job_events"}
        assert expected <= tables, f"missing: {expected - tables}"

    @pytest.mark.asyncio
    async def test_health_reports_standalone_mode(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"]["database"] == "sqlite"
        assert body["mode"]["storage"] == "local-fs"
        assert body["mode"]["queue"] == "inline"


# ---------------------------------------------------------------------------
# Upload + LocalFilesystemStorage round-trip
# ---------------------------------------------------------------------------


class TestUploadDownloadRoundTrip:
    @pytest.mark.asyncio
    async def test_korean_filename_roundtrip_local_fs(
        self, client, standalone_env
    ):
        # 1. Upload a file with a Korean filename.
        files = {"file": ("Q3 영업보고서.pdf", b"%PDF-1.4 dummy", "application/pdf")}
        up = await client.post("/v1/assets", files=files)
        assert up.status_code == 201, up.text
        asset = up.json()
        assert asset["original_filename"] == "Q3 영업보고서.pdf"

        # 2. The local-fs adapter materialized a file under data_dir/storage.
        storage_path = standalone_env.data_dir / "storage" / asset["storage_key"]
        assert storage_path.exists()
        assert storage_path.read_bytes().startswith(b"%PDF-1.4")

        # 3. Download URL points at /v1/raw with an HMAC.
        dl = await client.get(f"/v1/assets/{asset['id']}/download")
        assert dl.status_code == 200, dl.text
        download_url = dl.json()["download_url"]
        assert download_url.startswith("/v1/raw/")
        assert "s=" in download_url and "e=" in download_url
        assert "fn=" in download_url  # Korean filename embedded

        # 4. Fetching that URL returns the bytes + Korean Content-Disposition.
        raw = await client.get(download_url)
        assert raw.status_code == 200
        cd = raw.headers["content-disposition"]
        assert "filename*=UTF-8''Q3%20" in cd
        assert raw.content.startswith(b"%PDF-1.4")

    @pytest.mark.asyncio
    async def test_raw_endpoint_rejects_unsigned_url(self, client):
        # Upload one file so we have a real key.
        up = await client.post(
            "/v1/assets",
            files={"file": ("x.txt", b"hi", "text/plain")},
        )
        asset = up.json()
        # Build an unsigned URL — should 403.
        raw = await client.get(f"/v1/raw/{asset['storage_key']}?e=0&s=zero")
        assert raw.status_code == 403
        body = raw.json()
        assert body["error"]["code"] == "RAW_SIGNATURE_INVALID"
