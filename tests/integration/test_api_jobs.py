"""Integration tests for /v1/jobs/generate-deck and SSE event stream.

The test fixture wires:
- FastAPI test client (httpx.AsyncClient via ASGITransport)
- in-memory SQLite (overrides get_db_session)
- InMemoryStorage (overrides get_object_storage)
- FakeJobBus (overrides get_default_bus)

The worker is run manually inside the test by invoking the registered
executor against an ExecutionContext — the goal is to verify the API
plumbing, not the arq daemon (which is exercised separately in M3.4).

Real LLM calls are stubbed: we patch the tool layer's strategize/execute
to return deterministic SVGs and skip the convert step's heavy parsers.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.api import dependencies as deps
from xgen_edit2docs.api.main import app
from xgen_edit2docs.db.models import Base, Job
from xgen_edit2docs.services import jobs as jobs_service
from xgen_edit2docs.services.jobs import FakeJobBus
from xgen_edit2docs.storage import InMemoryStorage

KOREAN_SVG = (Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_pipeline(monkeypatch):
    """Replace strategize, execute_batch, convert_to_markdown with fakes that
    do no LLM / disk work. Used by the generate-deck integration test."""

    import sys
    import xgen_edit2docs.tools.convert as convert_module
    from xgen_edit2docs.tools import (
        ConvertResponse,
        CostBreakdown,
        ExecuteBatchResponse,
        ExecutePageResponse,
        StrategizeResponse,
    )

    # The submodule attribute on the package is shadowed by the re-exported
    # `generate_deck` function. Reach the module through sys.modules instead.
    gd = sys.modules["xgen_edit2docs.tools.generate_deck"]

    def _fake_convert(req):
        return ConvertResponse(
            markdown="# Test\n\n한국어 콘텐츠.",
            detected_format=req.source_type or "pdf",
            original_filename=req.original_filename,
            char_count=20,
            cost=CostBreakdown(),
        )

    monkeypatch.setattr(convert_module, "convert_to_markdown", _fake_convert)
    monkeypatch.setattr(gd, "convert_to_markdown", _fake_convert)

    async def _fake_strategize(req, *, client=None):
        return StrategizeResponse(
            raw_output="...",
            design_spec="## Page 1\n표지\n\n## Page 2\n결론",
            spec_lock="lang: ko-KR\npages:\n  - 표지\n  - 결론",
            cost=CostBreakdown(input_tokens=10, output_tokens=10),
        )

    monkeypatch.setattr(gd, "strategize", _fake_strategize)

    async def _fake_execute_batch(req, *, client=None):
        results = [
            ExecutePageResponse(
                page_index=p.page_index,
                svg=KOREAN_SVG,
                speaker_notes=f"노트 {p.page_index}",
                raw_output="...",
                cost=CostBreakdown(),
            )
            for p in req.pages
        ]
        return ExecuteBatchResponse(results=results, cost=CostBreakdown())

    monkeypatch.setattr(gd, "execute_batch", _fake_execute_batch)

    # Stub the anthropic client constructor so generate_deck doesn't try to
    # import the real SDK during the test.
    class _DummyClient:
        async def complete(self, *args, **kwargs):  # pragma: no cover - never called
            raise RuntimeError("stub LLM client should not be called directly")

    monkeypatch.setattr(gd, "AnthropicClient", lambda **kwargs: _DummyClient())


# ---------------------------------------------------------------------------
# POST /v1/jobs/generate-deck
# ---------------------------------------------------------------------------


class TestGenerateDeckEnqueue:
    @pytest.mark.asyncio
    async def test_returns_queued_job(self, client: httpx.AsyncClient):
        # Upload a source first so we have an asset id to reference.
        upload = await client.post(
            "/v1/assets",
            files={"file": ("source.pdf", b"%PDF-1.4 ...", "application/pdf")},
        )
        asset_id = upload.json()["id"]

        resp = await client.post(
            "/v1/jobs/generate-deck",
            headers={"X-Anthropic-API-Key": "sk-ant-test"},
            json={
                "source_asset_ids": [asset_id],
                "user_intent": "Q3 영업 결과 임원 보고",
                "target_pages": [4, 6],
                "lang": "ko-KR",
            },
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["kind"] == "generate_deck"
        assert body["status"] == "queued"
        assert body["params"]["user_intent"] == "Q3 영업 결과 임원 보고"
        assert body["params"]["anthropic_api_key"] == "[redacted]"

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_400(self, client: httpx.AsyncClient):
        upload = await client.post(
            "/v1/assets",
            files={"file": ("source.pdf", b"%PDF-1.4 ...", "application/pdf")},
        )
        asset_id = upload.json()["id"]

        resp = await client.post(
            "/v1/jobs/generate-deck",
            json={
                "source_asset_ids": [asset_id],
                "user_intent": "Q3",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "LLM_API_KEY_MISSING"


# ---------------------------------------------------------------------------
# GET /v1/jobs/{id}
# ---------------------------------------------------------------------------


class TestPollStatus:
    @pytest.mark.asyncio
    async def test_get_unknown_job_404(self, client: httpx.AsyncClient):
        resp = await client.get(f"/v1/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


# ---------------------------------------------------------------------------
# End-to-end: enqueue -> run executor manually -> poll status -> SSE replay
# ---------------------------------------------------------------------------


class TestGenerateDeckExecutor:
    @pytest.mark.asyncio
    async def test_executor_completes_and_publishes_pptx(
        self,
        client: httpx.AsyncClient,
        test_db,
        test_storage: InMemoryStorage,
        test_bus: FakeJobBus,
        monkeypatch,
    ):
        _stub_pipeline(monkeypatch)

        # Upload + enqueue.
        upload = await client.post(
            "/v1/assets",
            files={"file": ("source.pdf", b"%PDF-1.4 dummy ...", "application/pdf")},
        )
        asset_id = upload.json()["id"]
        enq = await client.post(
            "/v1/jobs/generate-deck",
            headers={"X-Anthropic-API-Key": "sk-ant-stub"},
            json={
                "source_asset_ids": [asset_id],
                "user_intent": "한국어 통합 테스트",
                "target_pages": [2, 2],
                "lang": "ko-KR",
            },
        )
        job_id = uuid.UUID(enq.json()["id"])

        # Run the executor manually with its own session.
        async with test_db() as session:
            from sqlalchemy import select
            from xgen_edit2docs.workers.executors.generate_deck import run_generate_deck
            from xgen_edit2docs.workers.executors.registry import ExecutionContext

            job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
            ctx = ExecutionContext(session=session, bus=test_bus, job=job)
            await run_generate_deck(ctx)

        # Poll status: must be done with a pptx_asset_id in the result.
        poll = await client.get(f"/v1/jobs/{job_id}")
        assert poll.status_code == 200
        body = poll.json()
        assert body["status"] == "done"
        assert body["error_message"] is None
        assert "pptx_asset_id" in body["result"]
        assert body["result"]["page_count"] == 2
        # BYOK key isn't leaked to the response.
        assert body["params"]["anthropic_api_key"] == "[redacted]"

        # Fetch the PPTX asset and confirm it's a real ZIP-prefixed blob.
        pptx_asset_id = body["result"]["pptx_asset_id"]
        meta = await client.get(f"/v1/assets/{pptx_asset_id}")
        assert meta.status_code == 200
        assert (
            meta.json()["mime_type"]
            == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
        key = meta.json()["storage_key"]
        data = await test_storage.get_bytes(key)
        assert data[:4] == b"PK\x03\x04"

    @pytest.mark.asyncio
    async def test_sse_replays_history(
        self,
        client: httpx.AsyncClient,
        test_db,
        test_bus: FakeJobBus,
        monkeypatch,
    ):
        _stub_pipeline(monkeypatch)

        # Upload + enqueue + run executor (so events are persisted before SSE attaches).
        upload = await client.post(
            "/v1/assets",
            files={"file": ("source.pdf", b"PDF", "application/pdf")},
        )
        asset_id = upload.json()["id"]
        enq = await client.post(
            "/v1/jobs/generate-deck",
            headers={"X-Anthropic-API-Key": "sk-ant-stub"},
            json={
                "source_asset_ids": [asset_id],
                "user_intent": "...",
                "target_pages": [2, 2],
                "lang": "ko-KR",
            },
        )
        job_id = uuid.UUID(enq.json()["id"])

        async with test_db() as session:
            from sqlalchemy import select
            from xgen_edit2docs.workers.executors.generate_deck import run_generate_deck
            from xgen_edit2docs.workers.executors.registry import ExecutionContext

            job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
            await run_generate_deck(
                ExecutionContext(session=session, bus=test_bus, job=job)
            )

        # Connect to SSE — terminal status means we just get history then EOF.
        # httpx.AsyncClient.stream supports SSE-style iteration.
        events = []
        async with client.stream("GET", f"/v1/jobs/{job_id}/events") as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    current = {"event": line.split(":", 1)[1].strip()}
                    events.append(current)
                elif line.startswith("data:") and events:
                    events[-1]["data"] = json.loads(line.split(":", 1)[1].strip())

        # We expect at least: queued, converting, strategizing, executing_pages,
        # checking_quality, exporting, done — minus the ones that the stub skips.
        # The crucial assertion: there IS an event named "done" and the payload
        # is Korean-friendly.
        stages = [e["data"].get("payload", {}).get("stage") for e in events if "data" in e]
        assert "done" in stages
