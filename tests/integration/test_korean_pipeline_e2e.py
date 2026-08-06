"""End-to-end Korean pipeline test via the MCP generate_deck tool.

Drives the full pipeline (upload_source -> generate_deck -> download_url)
through MCP's call_tool() against a stub LLM. Verifies:

- The runtime Output Language directive AND the Korean appendices reach
  the LLM system prompt
- The Strategist + Executor stubs receive ko-KR context
- The resulting PPTX is a real ZIP, persisted with the Korean filename
  intact
- A presigned download URL preserves the Korean filename through
  Content-Disposition

This is the M5.4 capstone: the whole stack (M0-M5.3) wired together with
Korean as the runtime language end-to-end.
"""

from __future__ import annotations

import base64
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.db.models import Base
from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.mcp import build_mcp_server
from xgen_edit2docs.mcp.context import MCPContext
from xgen_edit2docs.storage import InMemoryStorage

KOREAN_SVG = (Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg").read_text(
    encoding="utf-8"
)


@dataclass
class CapturingLLM:
    """Stub LLM that records every (system_prompt, user_message) it sees."""

    calls: list[dict] = field(default_factory=list)
    response_queue: list[str] = field(default_factory=list)

    async def complete(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        text = self.response_queue.pop(0) if self.response_queue else ""
        return LLMResult(
            text=text,
            usage=LLMUsage(input_tokens=10, output_tokens=10),
            model="stub",
            stop_reason="end_turn",
        )


@pytest_asyncio.fixture
async def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest_asyncio.fixture
async def context(storage: InMemoryStorage):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield MCPContext(
        sessionmaker=async_sessionmaker(engine, expire_on_commit=False),
        storage=storage,
    )
    await engine.dispose()


@pytest_asyncio.fixture
async def server(context: MCPContext):
    return build_mcp_server(context=context)


# ---------------------------------------------------------------------------
# E2E
# ---------------------------------------------------------------------------


class TestKoreanPipelineEndToEnd:
    @pytest.mark.asyncio
    async def test_directive_and_korean_guidance_reach_llm(
        self,
        server,
        storage: InMemoryStorage,
        monkeypatch,
    ):
        # 1. Capture every LLM call into a shared CapturingLLM.
        captured = CapturingLLM(
            response_queue=[
                # Strategist response
                "```design_spec\n"
                "## Page 1\n표지\n\n## Page 2\n결론\n"
                "```\n"
                "```spec_lock\n"
                "lang: ko-KR\n"
                "typography:\n"
                "  body_stack: '\"Pretendard\", \"Apple SD Gothic Neo\", \"Malgun Gothic\", sans-serif'\n"
                "pages:\n  - 표지\n  - 결론\n"
                "```",
                # Page 0 executor response
                f"```svg\n{KOREAN_SVG}\n```\n```notes\n표지 노트입니다.\n```",
                # Page 1 executor response
                f"```svg\n{KOREAN_SVG}\n```\n```notes\n결론 노트입니다.\n```",
            ]
        )

        # 2. Stub convert (avoid pulling in mammoth / PyMuPDF), and inject our
        # capturing LLM into both the strategize and execute_batch call paths.
        import xgen_edit2docs.tools.convert as convert_module
        from xgen_edit2docs.tools import ConvertResponse, CostBreakdown

        def _fake_convert(req):
            return ConvertResponse(
                markdown="# Test\n\n한국어 소스.",
                detected_format=req.source_type or "pdf",
                original_filename=req.original_filename,
                char_count=20,
                cost=CostBreakdown(),
            )

        gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        monkeypatch.setattr(convert_module, "convert_to_markdown", _fake_convert)
        monkeypatch.setattr(gd, "convert_to_markdown", _fake_convert)

        # Replace AnthropicClient construction with our capture.
        monkeypatch.setattr(gd, "AnthropicClient", lambda **kwargs: captured)
        # tools.strategize / tools.execute both invoke the client passed to them
        # only when no `client=` kwarg is given. In generate_deck we DO pass a
        # client, so monkeypatching AnthropicClient hits the path. To also
        # patch strategize/execute_batch (which construct their own clients on
        # the inner branch when client= is None), inject directly:
        strat_mod = sys.modules["xgen_edit2docs.tools.strategize"]
        exec_mod = sys.modules["xgen_edit2docs.tools.execute"]
        monkeypatch.setattr(strat_mod, "AnthropicClient", lambda **kwargs: captured)
        monkeypatch.setattr(exec_mod, "AnthropicClient", lambda **kwargs: captured)

        # 3. Upload a source through MCP.
        _contents, upload = await server.call_tool(
            "upload_source",
            {
                "filename": "Q3 보고서.pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4 ...").decode("ascii"),
                "mime_type": "application/pdf",
            },
        )

        # 4. Run generate_deck with Korean as the runtime language.
        _contents2, result = await server.call_tool(
            "generate_deck",
            {
                "source_asset_ids": [upload["asset_id"]],
                "user_intent": "Q3 영업 결과 임원 보고",
                "target_min_pages": 2,
                "target_max_pages": 2,
                "lang": "ko-KR",
                "style": "general",
                "anthropic_api_key": "sk-ant-stub",
                "output_basename": "Q3_보고서_프레젠테이션",
            },
        )

        # 5. The Strategist call (first) MUST have the directive at the top.
        assert captured.calls, "Strategist was not invoked"
        strat_system = captured.calls[0]["system"]
        assert strat_system.startswith("# Output Language")
        head = strat_system.split("---")[0]
        assert "Korean (한국어)" in head
        assert "ko-KR" in head
        # And the body of the Strategist prompt (including §K) shows up.
        assert "Role: Strategist" in strat_system
        assert "Appendix K. Korean" in strat_system
        assert "Pretendard" in strat_system  # K.1 stack reference

        # 6. The Executor calls (per page) MUST also carry the directive AND
        # the executor-base Korean appendix.
        page_calls = captured.calls[1:]
        assert len(page_calls) >= 2
        for call in page_calls:
            sys_prompt = call["system"]
            assert sys_prompt.startswith("# Output Language")
            assert "ko-KR" in sys_prompt.split("---")[0]
            assert "executor" in sys_prompt.lower()
            assert "Appendix K. Korean" in sys_prompt

        # 7. Final PPTX is real and persisted.
        assert result["page_count"] == 2
        assert result["pptx_asset_id"]
        meta_contents, meta = await server.call_tool(
            "get_asset", {"asset_id": result["pptx_asset_id"]}
        )
        # The Korean output_basename round-trips through original_filename.
        assert meta["original_filename"] == "Q3_보고서_프레젠테이션.pptx"
        data = await storage.get_bytes(meta["storage_key"])
        assert data[:4] == b"PK\x03\x04"

        # 8. Download URL preserves the Korean filename via Content-Disposition.
        dl_contents, dl = await server.call_tool(
            "download_url",
            {"asset_id": result["pptx_asset_id"], "expires_in_seconds": 60},
        )
        assert dl["filename"] == "Q3_보고서_프레젠테이션.pptx"
        parsed = urllib.parse.urlparse(dl["download_url"])
        params = urllib.parse.parse_qs(parsed.query)
        disposition = params["X-Test-Content-Disposition"][0]
        rfc = disposition.split("filename*=UTF-8''", 1)[1]
        assert urllib.parse.unquote(rfc) == "Q3_보고서_프레젠테이션.pptx"


class TestTemplateReferencesKoreanAppendix:
    def test_design_spec_reference_has_korean_appendix(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "xgen_edit2docs"
            / "core"
            / "templates"
            / "design_spec_reference.md"
        ).read_text(encoding="utf-8")
        assert "Appendix K. Korean" in text
        # Korean font stack reference.
        assert "Pretendard" in text and "Malgun Gothic" in text
        # Drop-in color palettes (at least Toss-blue and Korean navy).
        assert "#0064FF" in text
        assert "#003478" in text
        # Typography ramp.
        assert "letter-spacing" in text.lower() or "letter_spacing" in text

    def test_spec_lock_reference_has_korean_appendix(self):
        from pathlib import Path

        text = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "xgen_edit2docs"
            / "core"
            / "templates"
            / "spec_lock_reference.md"
        ).read_text(encoding="utf-8")
        assert "Appendix K. Korean" in text
        # Minimal Korean spec_lock example.
        assert "lang: ko-KR" in text
        assert "Pretendard" in text and "Malgun Gothic" in text
        assert "font_style: normal" in text
        # Mandatory rules section.
        assert "Track A" in text or "stay English" in text
