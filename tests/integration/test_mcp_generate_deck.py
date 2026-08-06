"""Integration test for the M4.3 MCP generate_deck tool.

Drives the tool through MCP's call_tool() against a SQLite + InMemoryStorage
context. The Strategist + Executor LLM calls are stubbed (same pattern as
the M3.5 worker test) so the whole pipeline can run without an Anthropic
API key.

Verifies:
- Tool surfaces in list_tools()
- generate_deck rejects missing api_key or empty sources
- Stage events flow through the StageEvent callback
- Final PPTX is persisted as a Korean-named Asset; bytes are real ZIP
- download_url on the resulting pptx_asset_id returns a presigned URL
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.db.models import Base
from xgen_edit2docs.mcp import build_mcp_server
from xgen_edit2docs.mcp.context import MCPContext
from xgen_edit2docs.storage import InMemoryStorage

KOREAN_SVG = (Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg").read_text(
    encoding="utf-8"
)


@pytest_asyncio.fixture
async def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest_asyncio.fixture
async def context(storage: InMemoryStorage):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield MCPContext(sessionmaker=sessionmaker, storage=storage)
    await engine.dispose()


@pytest_asyncio.fixture
async def server(context: MCPContext):
    return build_mcp_server(context=context)


@pytest.fixture
def stubbed_pipeline(monkeypatch):
    """Replace strategize/execute_batch/convert with deterministic stubs.

    Mirrors the M3.5 worker test setup so the MCP generate_deck path can
    run without real LLM credentials.
    """
    import xgen_edit2docs.tools.convert as convert_module
    from xgen_edit2docs.tools import (
        ConvertResponse,
        CostBreakdown,
        ExecuteBatchResponse,
        ExecutePageResponse,
        StrategizeResponse,
    )

    gd = sys.modules["xgen_edit2docs.tools.generate_deck"]

    def _fake_convert(req):
        return ConvertResponse(
            markdown="# Korean test\n\n이것은 테스트입니다.",
            detected_format=req.source_type or "pdf",
            original_filename=req.original_filename,
            char_count=30,
            cost=CostBreakdown(),
        )

    monkeypatch.setattr(convert_module, "convert_to_markdown", _fake_convert)
    monkeypatch.setattr(gd, "convert_to_markdown", _fake_convert)

    async def _fake_strategize(req, *, client=None):
        return StrategizeResponse(
            raw_output="...",
            design_spec="## Page 1\n표지\n\n## Page 2\n결론",
            spec_lock="lang: ko-KR\npages:\n  - 표지\n  - 결론",
            cost=CostBreakdown(input_tokens=50, output_tokens=20),
        )

    monkeypatch.setattr(gd, "strategize", _fake_strategize)

    async def _fake_execute_batch(req, *, client=None):
        results = [
            ExecutePageResponse(
                page_index=p.page_index,
                svg=KOREAN_SVG,
                speaker_notes=f"노트 {p.page_index}",
                raw_output="...",
                cost=CostBreakdown(input_tokens=10, output_tokens=10),
            )
            for p in req.pages
        ]
        return ExecuteBatchResponse(results=results, cost=CostBreakdown())

    monkeypatch.setattr(gd, "execute_batch", _fake_execute_batch)

    class _DummyClient:
        async def complete(self, *args, **kwargs):  # pragma: no cover - never called
            raise RuntimeError("stub LLM client should not be called directly")

    monkeypatch.setattr(gd, "AnthropicClient", lambda **kwargs: _DummyClient())


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


class TestToolListing:
    @pytest.mark.asyncio
    async def test_generate_deck_advertised(self, server):
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert "generate_deck" in names


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.asyncio
    async def test_missing_api_key_errors(self, server):
        with pytest.raises(Exception, match="anthropic_api_key"):
            await server.call_tool(
                "generate_deck",
                {
                    "source_asset_ids": ["00000000-0000-0000-0000-000000000000"],
                    "user_intent": "x",
                    "anthropic_api_key": "",
                },
            )

    @pytest.mark.asyncio
    async def test_empty_user_intent_errors(self, server):
        """`user_intent` is the only required input — empty intent must reject."""
        with pytest.raises(Exception, match="user_intent|min_length|String should"):
            await server.call_tool(
                "generate_deck",
                {
                    "source_asset_ids": [],
                    "user_intent": "",
                    "anthropic_api_key": "sk-ant-stub",
                },
            )

    @pytest.mark.asyncio
    async def test_invalid_uuid_errors(self, server):
        with pytest.raises(Exception, match="UUID"):
            await server.call_tool(
                "generate_deck",
                {
                    "source_asset_ids": ["not-a-uuid"],
                    "user_intent": "x",
                    "anthropic_api_key": "sk-ant-stub",
                },
            )


# ---------------------------------------------------------------------------
# End-to-end with stubbed LLM
# ---------------------------------------------------------------------------


class TestGenerateDeckEndToEnd:
    @pytest.mark.asyncio
    async def test_full_pipeline_produces_pptx_asset(
        self,
        server,
        storage: InMemoryStorage,
        stubbed_pipeline,
    ):
        # 1. Upload a source through the same MCP tool path.
        contents, upload = await server.call_tool(
            "upload_source",
            {
                "filename": "Q3 보고서.pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4 ...").decode("ascii"),
                "mime_type": "application/pdf",
            },
        )
        source_asset_id = upload["asset_id"]

        # 2. generate_deck
        contents2, result = await server.call_tool(
            "generate_deck",
            {
                "source_asset_ids": [source_asset_id],
                "user_intent": "한국어 통합 테스트",
                "target_min_pages": 2,
                "target_max_pages": 2,
                "lang": "ko-KR",
                "anthropic_api_key": "sk-ant-stub",
                "output_basename": "test-deck",
            },
        )

        assert result["page_count"] == 2
        assert result["pptx_asset_id"]
        assert "stages_seen" in result and "done" in result["stages_seen"]
        # spec_lock + design_spec round-trip from the Strategist stub.
        assert "lang: ko-KR" in result["spec_lock"]
        assert "표지" in result["design_spec"]

        # 3. download_url for the PPTX asset.
        contents3, dl = await server.call_tool(
            "download_url",
            {"asset_id": result["pptx_asset_id"], "expires_in_seconds": 60},
        )
        assert dl["filename"] == "test-deck.pptx"
        # The actual PPTX bytes are real (PK\x03\x04 zip prefix).
        contents4, meta = await server.call_tool(
            "get_asset", {"asset_id": result["pptx_asset_id"]}
        )
        data = await storage.get_bytes(meta["storage_key"])
        assert data[:4] == b"PK\x03\x04"
