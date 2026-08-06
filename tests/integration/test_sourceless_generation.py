"""End-to-end test for source-less ("topic-only" / "chat-mode") generation.

Confirms that calling generate_deck with sources=[] runs the Strategist
from `user_intent` alone — the convert stage is skipped and the
Strategist's user message instructs the model to design from intent only.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools import (
    CostBreakdown,
    ExecuteBatchResponse,
    ExecutePageResponse,
    StrategizeResponse,
)
from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck

KOREAN_SVG = (Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg").read_text(
    encoding="utf-8"
)


@dataclass
class _CapturingLLM:
    calls: list[dict] = field(default_factory=list)
    response: str = ""

    async def complete(self, system_prompt, user_message, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        return LLMResult(
            text=self.response,
            usage=LLMUsage(input_tokens=10, output_tokens=10),
            model="stub",
            stop_reason="end_turn",
        )


@dataclass
class _ExecuteStub:
    pages: int = 2

    async def __call__(self, req, *, client=None):
        return ExecuteBatchResponse(
            results=[
                ExecutePageResponse(
                    page_index=i,
                    svg=KOREAN_SVG,
                    speaker_notes="",
                    raw_output="...",
                    cost=CostBreakdown(),
                    warnings=[],
                )
                for i in range(self.pages)
            ],
            cost=CostBreakdown(),
            warnings=[],
        )


class TestSourceLessGeneration:
    def setup_method(self):
        self.gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        self.strat = sys.modules["xgen_edit2docs.tools.strategize"]

    def _wire(self, monkeypatch, strat_response: str):
        strat_llm = _CapturingLLM(response=strat_response)

        async def fake_strategize(req, *, client=None):
            from xgen_edit2docs.tools.strategize import strategize as real_strategize
            return await real_strategize(req, client=strat_llm)

        monkeypatch.setattr(self.gd, "strategize", fake_strategize)
        monkeypatch.setattr(self.gd, "execute_batch", _ExecuteStub())
        # convert_to_markdown should NEVER be called when sources=[]; if it is,
        # this lambda will surface a clear failure.
        monkeypatch.setattr(
            self.gd, "convert_to_markdown", lambda req: pytest.fail(
                "convert_to_markdown called in source-less path"
            )
        )
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())
        return strat_llm

    @pytest.mark.asyncio
    async def test_no_sources_skips_convert_and_invokes_strategist(self, monkeypatch):
        strat_response = (
            "```design_spec\n"
            "## Page 1\n표지\n## Page 2\n결론\n"
            "```\n"
            "```spec_lock\nlang: ko-KR\npages:\n  - 표지\n  - 결론\n```"
        )
        strat_llm = self._wire(monkeypatch, strat_response)

        result = await generate_deck(
            GenerateDeckRequest(
                sources=[],  # the load-bearing change
                user_intent="조선시대 정치 구조에 대한 입문 발표",
                target_pages=(2, 2),
                lang="ko-KR",
                anthropic_api_key="sk-ant-stub",
                fail_on_quality_error=False,
                skip_images=True,
            )
        )

        # 1. The Strategist was called, exactly once, with the user_intent.
        assert len(strat_llm.calls) == 1
        user_msg = strat_llm.calls[0]["user"]
        assert "조선시대 정치 구조" in user_msg
        # 2. The user message contained the "No source document" directive,
        #    not a "## Sources" block — the Strategist must work from intent.
        assert "No source document was provided" in user_msg
        assert "## Sources" not in user_msg
        # 3. Final PPTX is real.
        assert result.pptx[:4] == b"PK\x03\x04"
        assert result.page_count == 2

    @pytest.mark.asyncio
    async def test_user_intent_required(self):
        with pytest.raises(Exception, match="user_intent|min_length|String should"):
            GenerateDeckRequest(
                sources=[],
                user_intent="",  # empty
                anthropic_api_key="x",
            )
