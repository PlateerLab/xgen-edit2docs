"""API tests for the multi-format hosted surface (preview / text-edits / jobs)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.api import dependencies as deps
from xgen_edit2docs.api.main import app
from xgen_edit2docs.db.models import Base
from xgen_edit2docs.documents.docx_engine import docx_from_markdown, docx_outline
from xgen_edit2docs.documents.xlsx_engine import xlsx_from_spec
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


DOCX = docx_from_markdown("# 보고서\n\n첫 문단입니다.\n\n- 항목 하나")
XLSX = xlsx_from_spec(
    {"sheets": [{"name": "매출", "headers": ["분기", "금액"], "rows": [["1분기", 120]]}]}
)


async def _upload(client, content: bytes, name: str) -> str:
    resp = await client.post("/v1/assets", files={"file": (name, content)})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestPreviewDispatch:
    @pytest.mark.asyncio
    async def test_docx_preview_returns_html(self, client):
        asset_id = await _upload(client, DOCX, "report.docx")
        resp = await client.post("/v1/preview", json={"pptx_asset_id": asset_id})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "docx"
        assert "<h1" in body["html"] and "첫 문단" in body["html"]
        # The preview is addressable: paragraph addresses match the outline.
        assert 'data-e2d-para="' in body["html"]
        assert body["slides"] == []

    @pytest.mark.asyncio
    async def test_xlsx_preview_returns_html_tables(self, client):
        asset_id = await _upload(client, XLSX, "sales.xlsx")
        resp = await client.post("/v1/preview", json={"pptx_asset_id": asset_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["format"] == "xlsx"
        assert "<table" in body["html"] and "1분기" in body["html"]
        # The preview is addressable: cells carry set_cell-style addresses.
        assert 'data-e2d-cell="A2"' in body["html"]
        assert body["page_count"] == 1  # one sheet


class TestTextEditsDispatch:
    @pytest.mark.asyncio
    async def test_docx_paragraph_edit(self, client, test_storage):
        asset_id = await _upload(client, DOCX, "report.docx")
        target = next(e for e in docx_outline(DOCX) if "첫 문단" in e["text"])
        resp = await client.post(
            "/v1/text-edits",
            json={
                "pptx_asset_id": asset_id,
                "edits": [
                    {
                        "action": "replace",
                        "para": target["para"],
                        "new_text": "수정된 문단입니다.",
                        "old_text": target["text"],
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "docx" and body["applied"] == 1
        assert body["doc_asset_id"] != asset_id

        meta = await client.get(f"/v1/assets/{body['doc_asset_id']}")
        assert meta.json()["original_filename"].endswith(".docx")
        raw = await test_storage.get_bytes(meta.json()["storage_key"])
        assert any("수정된 문단" in e["text"] for e in docx_outline(raw))

    @pytest.mark.asyncio
    async def test_xlsx_cell_edit(self, client, test_storage):
        asset_id = await _upload(client, XLSX, "sales.xlsx")
        resp = await client.post(
            "/v1/text-edits",
            json={
                "pptx_asset_id": asset_id,
                "edits": [
                    {"action": "set_cell", "sheet": "매출", "cell": "B2", "value": 999}
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["format"] == "xlsx" and body["applied"] == 1

        from xgen_edit2docs.documents.xlsx_engine import xlsx_outline

        meta = await client.get(f"/v1/assets/{body['doc_asset_id']}")
        raw = await test_storage.get_bytes(meta.json()["storage_key"])
        assert xlsx_outline(raw)["sheets"][0]["sample"][1][1] == 999

    @pytest.mark.asyncio
    async def test_zero_applied_keeps_asset(self, client):
        asset_id = await _upload(client, XLSX, "sales.xlsx")
        resp = await client.post(
            "/v1/text-edits",
            json={
                "pptx_asset_id": asset_id,
                "edits": [{"action": "set_cell", "sheet": "없음", "cell": "A1", "value": 1}],
            },
        )
        body = resp.json()
        assert body["applied"] == 0
        assert body["doc_asset_id"] == asset_id


class TestGenerateJobRoute:
    @pytest.mark.asyncio
    async def test_output_format_validated_and_persisted(self, client, monkeypatch):
        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop)

        bad = await client.post(
            "/v1/jobs/generate-deck",
            json={"user_intent": "x", "output_format": "hwp"},
            headers={"X-Anthropic-API-Key": "k"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "INVALID_OUTPUT_FORMAT"

        ok = await client.post(
            "/v1/jobs/generate-deck",
            json={"user_intent": "주간 보고서", "output_format": "docx"},
            headers={"X-Anthropic-API-Key": "k"},
        )
        assert ok.status_code == 202
        assert ok.json()["params"]["output_format"] == "docx"


class TestEditJobDispatch:
    @pytest.mark.asyncio
    async def test_docx_edit_job_runs_document_editor(
        self, client, test_db, test_bus, monkeypatch
    ):
        # Stub the planner LLM used by edit_document.
        import sys
        from dataclasses import dataclass, field

        from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage

        target = next(e for e in docx_outline(DOCX) if "첫 문단" in e["text"])
        plan = (
            "```reply\n문단을 수정합니다.\n```\n"
            "```edit_plan\noperations:\n"
            f"  - action: replace\n    para: {target['para']}\n"
            "    new_text: \"잡으로 수정됨\"\n```"
        )

        @dataclass
        class _LLM:
            calls: list = field(default_factory=list)

            async def complete(self, system_prompt, user_message, **kw):
                self.calls.append(user_message)
                return LLMResult(text=plan, usage=LLMUsage(input_tokens=1, output_tokens=1),
                                 model="stub", stop_reason="end_turn")

        import xgen_edit2docs.tools.edit_doc  # noqa: F401  (ensure module is loaded)

        ed = sys.modules["xgen_edit2docs.tools.edit_doc"]
        monkeypatch.setattr(ed, "AnthropicClient", lambda **kw: _LLM())

        # The route's fire-and-forget inline runner opens its own default
        # sessionmaker (not the test override) — noop it and drive the
        # executor manually, same as the existing job tests.
        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop)

        asset_id = await _upload(client, DOCX, "report.docx")
        resp = await client.post(
            "/v1/jobs/edit-deck",
            json={"pptx_asset_id": asset_id, "instruction": "첫 문단 고쳐줘"},
            headers={"X-Anthropic-API-Key": "sk-stub"},
        )
        assert resp.status_code == 202, resp.text
        job_id = uuid.UUID(resp.json()["id"])

        async with test_db() as session:
            from sqlalchemy import select

            from xgen_edit2docs.db.models import Job
            from xgen_edit2docs.workers.executors.edit_deck import run_edit_deck
            from xgen_edit2docs.workers.executors.registry import ExecutionContext

            job_row = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            await run_edit_deck(
                ExecutionContext(session=session, bus=test_bus, job=job_row)
            )

        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert job["status"] == "done", job
        assert job["result"]["format"] == "docx"
        assert job["result"]["changed"] is True
        assert job["result"]["doc_asset_id"] != str(asset_id)


class TestGenerateJobDocxXlsx:
    @pytest.mark.asyncio
    async def test_docx_generate_job_end_to_end(
        self, client, test_db, test_bus, monkeypatch
    ):
        import sys
        from dataclasses import dataclass, field

        from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage

        text = "```document\n# 잡 생성 보고서\n\n- 항목 하나\n```"

        @dataclass
        class _LLM:
            calls: list = field(default_factory=list)

            async def complete(self, system_prompt, user_message, **kw):
                self.calls.append(user_message)
                return LLMResult(text=text, usage=LLMUsage(input_tokens=1, output_tokens=1),
                                 model="stub", stop_reason="end_turn")

        import xgen_edit2docs.tools.generate_doc  # noqa: F401

        gd = sys.modules["xgen_edit2docs.tools.generate_doc"]
        monkeypatch.setattr(gd, "AnthropicClient", lambda **kw: _LLM())

        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop)

        resp = await client.post(
            "/v1/jobs/generate-deck",
            json={"user_intent": "주간 보고서", "output_format": "docx",
                  "output_basename": "weekly"},
            headers={"X-Anthropic-API-Key": "sk-stub"},
        )
        assert resp.status_code == 202, resp.text
        job_id = uuid.UUID(resp.json()["id"])

        async with test_db() as session:
            from sqlalchemy import select

            from xgen_edit2docs.db.models import Job
            from xgen_edit2docs.workers.executors.generate_deck import run_generate_deck
            from xgen_edit2docs.workers.executors.registry import ExecutionContext

            job_row = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            await run_generate_deck(
                ExecutionContext(session=session, bus=test_bus, job=job_row)
            )

        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert job["status"] == "done", job
        assert job["result"]["format"] == "docx"
        assert "doc_asset_id" in job["result"]

        meta = await client.get(f"/v1/assets/{job['result']['doc_asset_id']}")
        assert meta.json()["original_filename"] == "weekly.docx"

    @pytest.mark.asyncio
    async def test_xlsx_generate_job_end_to_end(
        self, client, test_db, test_bus, test_storage, monkeypatch
    ):
        import sys
        from dataclasses import dataclass, field

        from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage

        text = (
            "```sheet_spec\nsheets:\n  - name: \"진척\"\n"
            "    headers: [\"항목\", \"상태\"]\n    rows:\n"
            "      - [\"배포\", \"완료\"]\n```"
        )

        @dataclass
        class _LLM:
            async def complete(self, system_prompt, user_message, **kw):
                return LLMResult(text=text, usage=LLMUsage(input_tokens=1, output_tokens=1),
                                 model="stub", stop_reason="end_turn")

        import xgen_edit2docs.tools.generate_doc  # noqa: F401

        gd = sys.modules["xgen_edit2docs.tools.generate_doc"]
        monkeypatch.setattr(gd, "AnthropicClient", lambda **kw: _LLM())

        import xgen_edit2docs.api.routes.jobs as jobs_route

        async def _noop(job_id):
            return None

        monkeypatch.setattr(jobs_route, "_run_inline", _noop)

        resp = await client.post(
            "/v1/jobs/generate-deck",
            json={"user_intent": "진척 시트", "output_format": "xlsx"},
            headers={"X-Anthropic-API-Key": "sk-stub"},
        )
        job_id = uuid.UUID(resp.json()["id"])

        async with test_db() as session:
            from sqlalchemy import select

            from xgen_edit2docs.db.models import Job
            from xgen_edit2docs.workers.executors.generate_deck import run_generate_deck
            from xgen_edit2docs.workers.executors.registry import ExecutionContext

            job_row = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            await run_generate_deck(
                ExecutionContext(session=session, bus=test_bus, job=job_row)
            )

        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert job["status"] == "done", job
        assert job["result"]["format"] == "xlsx"

        from xgen_edit2docs.documents.xlsx_engine import xlsx_outline

        meta = (await client.get(f"/v1/assets/{job['result']['doc_asset_id']}")).json()
        raw = await test_storage.get_bytes(meta["storage_key"])
        assert xlsx_outline(raw)["sheets"][0]["name"] == "진척"


class TestTemplateFormatGuard:
    @pytest.mark.asyncio
    async def test_template_with_docx_output_rejected(self, client):
        resp = await client.post(
            "/v1/jobs/generate-deck",
            json={
                "user_intent": "x",
                "output_format": "docx",
                "template_asset_id": str(uuid.uuid4()),
            },
            headers={"X-Anthropic-API-Key": "k"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "TEMPLATE_UNSUPPORTED_FOR_FORMAT"

    @pytest.mark.asyncio
    async def test_deck_mode_with_xlsx_output_rejected(self, client):
        resp = await client.post(
            "/v1/jobs/generate-deck",
            json={
                "user_intent": "x",
                "output_format": "xlsx",
                "deck_mode": "template_extend",
            },
            headers={"X-Anthropic-API-Key": "k"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "TEMPLATE_UNSUPPORTED_FOR_FORMAT"


class TestAssetKindByFormat:
    @pytest.mark.asyncio
    async def test_docx_revision_has_docx_kind(self, client):
        asset_id = await _upload(client, DOCX, "report.docx")
        target = next(e for e in docx_outline(DOCX) if "첫 문단" in e["text"])
        resp = await client.post(
            "/v1/text-edits",
            json={
                "pptx_asset_id": asset_id,
                "edits": [
                    {"action": "replace", "para": target["para"], "new_text": "수정"}
                ],
            },
        )
        body = resp.json()
        meta = (await client.get(f"/v1/assets/{body['doc_asset_id']}")).json()
        assert meta["kind"] == "docx"
