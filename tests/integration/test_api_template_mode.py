"""API-level tests for template-mode params on POST /v1/jobs/generate-deck.

Validation only — the full pipeline behaviour is covered by
test_template_mode_generation.py; here we assert the route's deck_mode /
template_asset_id contract and the params persisted on the job row.
"""

from __future__ import annotations

import uuid
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.api import dependencies as deps
from xgen_edit2docs.api.main import app
from xgen_edit2docs.db.models import Base
from xgen_edit2docs.services import jobs as jobs_service
from xgen_edit2docs.services.jobs import FakeJobBus
from xgen_edit2docs.storage import InMemoryStorage


@pytest_asyncio.fixture
async def test_storage():
    s = InMemoryStorage()
    deps.set_test_storage(s)
    yield s
    deps.set_test_storage(None)


@pytest_asyncio.fixture
async def test_bus():
    bus = FakeJobBus()
    jobs_service.set_default_bus(bus)
    yield bus
    jobs_service.set_default_bus(None)


@pytest_asyncio.fixture
async def test_db():
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
async def client(test_storage, test_bus, test_db) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _body(**overrides) -> dict:
    body = {"user_intent": "분기 보고", "target_pages": [2, 2]}
    body.update(overrides)
    return body


HEADERS = {"X-Anthropic-API-Key": "sk-ant-stub"}


class TestDeckModeValidation:
    @pytest.mark.asyncio
    async def test_unknown_deck_mode_is_rejected(self, client):
        resp = await client.post(
            "/v1/jobs/generate-deck",
            json=_body(deck_mode="fancy"),
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DECK_MODE"

    @pytest.mark.asyncio
    async def test_template_mode_without_asset_is_rejected(self, client):
        resp = await client.post(
            "/v1/jobs/generate-deck",
            json=_body(deck_mode="template_extend"),
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "TEMPLATE_ASSET_REQUIRED"

    @pytest.mark.asyncio
    async def test_template_asset_defaults_mode_to_restyle(self, client, monkeypatch):
        # Block inline execution: this test asserts enqueue-time params only.
        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop_run_inline(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop_run_inline)

        template_id = str(uuid.uuid4())
        resp = await client.post(
            "/v1/jobs/generate-deck",
            json=_body(template_asset_id=template_id),
            headers=HEADERS,
        )
        assert resp.status_code == 202
        params = resp.json()["params"]
        assert params["deck_mode"] == "template_restyle"
        assert params["template_asset_id"] == template_id

    @pytest.mark.asyncio
    async def test_plain_new_mode_keeps_null_template(self, client, monkeypatch):
        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop_run_inline(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop_run_inline)

        resp = await client.post(
            "/v1/jobs/generate-deck", json=_body(), headers=HEADERS
        )
        assert resp.status_code == 202
        params = resp.json()["params"]
        assert params["deck_mode"] == "new"
        assert params["template_asset_id"] is None
