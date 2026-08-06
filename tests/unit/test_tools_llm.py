"""Tests for the LLM-backed tool functions using a stub LLM client.

The stub mimics the AnthropicClient.complete coroutine and returns a fixed
payload, so we can exercise the prompt construction + output parsing +
orchestration paths without needing real API credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools import (
    ConvertRequest,
    ExecuteBatchRequest,
    ExecutePageRequest,
    StrategizeRequest,
    execute_batch,
    strategize,
)
from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg"
KOREAN_SVG = FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stub LLM client
# ---------------------------------------------------------------------------


@dataclass
class StubLLM:
    """Configurable stand-in for AnthropicClient. Each .complete() call returns
    the next item from `responses`, or `default` if the list is exhausted."""

    responses: list[str] = field(default_factory=list)
    default: str = ""
    calls: list[dict] = field(default_factory=list)

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_output_tokens: int = 8192,
        temperature: float = 0.6,
        cache_system: bool = True,
        model: str | None = None,
        # Token-cost levers: the Executor now delivers the deck-wide
        # spec_lock via `system_suffix` (cached once, read per page) rather
        # than inlining it in each user message. Record them so tests can
        # assert on the request shape.
        system_suffix: str | None = None,
        user_suffix: str = "",
        stream: bool | None = None,
        **_kwargs,
    ) -> LLMResult:
        self.calls.append(
            {
                "system": system_prompt,
                "user": user_message,
                "max_tokens": max_output_tokens,
                "temperature": temperature,
                "model": model,
                "cache_system": cache_system,
                "system_suffix": system_suffix,
                "user_suffix": user_suffix,
            }
        )
        text = self.responses.pop(0) if self.responses else self.default
        return LLMResult(
            text=text,
            usage=LLMUsage(input_tokens=100, output_tokens=200),
            model=model or "stub",
            stop_reason="end_turn",
        )


# ---------------------------------------------------------------------------
# Strategize
# ---------------------------------------------------------------------------


class TestStrategize:
    @pytest.mark.asyncio
    async def test_extracts_design_spec_and_spec_lock(self):
        llm = StubLLM(
            responses=[
                "Some preamble.\n"
                "```design_spec\n"
                "# Design\n"
                "## Page 1\nTitle slide for Q3.\n"
                "## Page 2\nKey findings.\n"
                "```\n"
                "```spec_lock\n"
                "lang: ko-KR\npages:\n  - title: 표지\n  - title: 결론\n"
                "```\n"
            ]
        )
        result = await strategize(
            StrategizeRequest(
                sources_markdown=["# Source\n원본 보고서 내용 ..."],
                user_intent="Q3 영업 결과 보고",
                lang="ko-KR",
                anthropic_api_key="stub",
            ),
            client=llm,
        )
        assert "# Design" in result.design_spec
        assert "lang: ko-KR" in result.spec_lock
        assert result.cost.input_tokens == 100
        assert result.cost.output_tokens == 200

    @pytest.mark.asyncio
    async def test_missing_spec_lock_block_warns(self):
        llm = StubLLM(
            responses=[
                "```design_spec\n# Design\n## Page 1\nHi.\n```\n"  # no spec_lock fence
            ]
        )
        result = await strategize(
            StrategizeRequest(
                sources_markdown=["x"],
                user_intent="...",
                lang="ko-KR",
                anthropic_api_key="stub",
            ),
            client=llm,
        )
        codes = {w.code for w in result.warnings}
        assert "missing_spec_lock_block" in codes

    @pytest.mark.asyncio
    async def test_user_message_includes_korean_intent_and_lang(self):
        llm = StubLLM(default="```design_spec\nx\n```\n```spec_lock\nx\n```")
        await strategize(
            StrategizeRequest(
                sources_markdown=["내용"],
                user_intent="한국어 의도",
                lang="ko-KR",
                anthropic_api_key="stub",
            ),
            client=llm,
        )
        assert llm.calls, "strategize must call the LLM"
        call = llm.calls[0]
        assert "ko-KR" in call["user"]
        assert "한국어 의도" in call["user"]
        # System prompt is the strategist role text — fallback is the .en.md
        assert "Strategist" in call["system"] or "strategist" in call["system"]


# ---------------------------------------------------------------------------
# Execute (batch)
# ---------------------------------------------------------------------------


class TestExecute:
    @pytest.mark.asyncio
    async def test_batch_runs_in_parallel_and_preserves_order(self):
        # Two pages, two LLM responses. Pop order interleaves but final result
        # must be sorted by page_index.
        page_count = 3
        responses = [
            f"```svg\n{KOREAN_SVG}\n```\n```notes\n페이지 {i} 노트\n```"
            for i in range(page_count)
        ]
        llm = StubLLM(responses=responses)

        page_reqs = [
            ExecutePageRequest(
                spec_lock="lang: ko-KR\n",
                page_index=i,
                page_summary=f"## Page {i}\n내용",
                style="general",
                lang="ko-KR",
                anthropic_api_key="stub",
            )
            for i in range(page_count)
        ]
        batch = await execute_batch(
            ExecuteBatchRequest(spec_lock="lang: ko-KR\n", pages=page_reqs, max_concurrency=4),
            client=llm,
        )
        assert [r.page_index for r in batch.results] == [0, 1, 2]
        assert all("<svg" in r.svg for r in batch.results)
        # Cost rolls up.
        assert batch.cost.input_tokens == page_count * 100

    @pytest.mark.asyncio
    async def test_unfenced_svg_tolerated_with_warning(self):
        llm = StubLLM(responses=[KOREAN_SVG])  # raw SVG, no fence
        result_batch = await execute_batch(
            ExecuteBatchRequest(
                spec_lock="lang: ko-KR\n",
                pages=[
                    ExecutePageRequest(
                        spec_lock="lang: ko-KR\n",
                        page_index=0,
                        page_summary="...",
                        lang="ko-KR",
                        anthropic_api_key="stub",
                    )
                ],
            ),
            client=llm,
        )
        page = result_batch.results[0]
        assert "<svg" in page.svg
        assert any(w.code == "unfenced_svg" for w in page.warnings)

    @pytest.mark.asyncio
    async def test_missing_svg_surfaces_warning_not_exception(self):
        """After C3 (partial-result preservation), per-page failures don't
        propagate — they record an `execute_page_failed` warning and place
        a placeholder SVG so subsequent stages still see N slides."""
        llm = StubLLM(responses=["nothing here, just text"])
        batch = await execute_batch(
            ExecuteBatchRequest(
                spec_lock="x",
                pages=[
                    ExecutePageRequest(
                        spec_lock="x",
                        page_index=0,
                        page_summary="x",
                        lang="ko-KR",
                        anthropic_api_key="stub",
                    )
                ],
            ),
            client=llm,
        )
        assert len(batch.results) == 1
        # Placeholder SVG content is recognizable.
        assert "could not be generated" in batch.results[0].svg
        codes = {w.code for w in batch.warnings}
        assert "execute_page_failed" in codes


# ---------------------------------------------------------------------------
# Full pipeline with mocked LLM (and a real text-file source)
# ---------------------------------------------------------------------------


class TestGenerateDeckPipeline:
    """End-to-end smoke test: real convert (skipped — too heavy without deps),
    mocked strategize, mocked execute, real quality + export.

    We construct the GenerateDeckRequest with a single markdown source so the
    convert step is light. Since text->markdown isn't a registered source_type,
    we use html which doc_to_md handles natively.
    """

    @pytest.mark.asyncio
    async def test_pipeline_with_mocked_llm_and_html_source(self, monkeypatch):
        # Make convert_to_markdown a no-op stub so we don't need mammoth.
        from xgen_edit2docs.tools import convert as convert_module

        def _fake_convert(req):
            return convert_module.ConvertResponse(
                markdown="# Korean test\n\n이것은 테스트입니다.",
                detected_format=req.source_type or "html",
                original_filename=req.original_filename,
                char_count=20,
                cost=convert_module.CostBreakdown(),
            )

        monkeypatch.setattr(convert_module, "convert_to_markdown", _fake_convert)
        # tools/__init__.py re-exports `generate_deck` (the function), shadowing
        # the submodule attribute. Fetch the actual module via sys.modules to
        # patch its import-time reference to convert_to_markdown.
        import sys
        gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        monkeypatch.setattr(gd, "convert_to_markdown", _fake_convert)

        # Stub LLM client: strategize call yields plan, then execute calls yield SVGs.
        strategize_resp = (
            "```design_spec\n"
            "## Page 1\n표지 슬라이드\n"
            "## Page 2\n결론 슬라이드\n"
            "```\n"
            "```spec_lock\nlang: ko-KR\npages:\n  - 표지\n  - 결론\n```"
        )
        executor_resp_template = f"```svg\n{KOREAN_SVG}\n```\n```notes\n노트 {{i}}\n```"
        llm = StubLLM(
            responses=[
                strategize_resp,
                executor_resp_template.format(i=0),
                executor_resp_template.format(i=1),
            ]
        )
        # Inject our stub by patching the AnthropicClient construction inside generate_deck.
        from xgen_edit2docs.llm import anthropic_client

        monkeypatch.setattr(
            anthropic_client,
            "AnthropicClient",
            lambda **kwargs: llm,
        )
        monkeypatch.setattr(gd, "AnthropicClient", lambda **kwargs: llm)

        # Capture progress events.
        events: list = []

        async def on_event(e):
            events.append(e.stage)

        result = await generate_deck(
            GenerateDeckRequest(
                sources=[ConvertRequest(source_type="html", content=b"<p>x</p>")],
                user_intent="한국어 1샷 파이프라인 테스트",
                target_pages=(2, 2),
                lang="ko-KR",
                anthropic_api_key="stub",
                fail_on_quality_error=False,  # the stub SVG may not pass strict checks
            ),
            on_event=on_event,
        )

        assert result.page_count == 2
        assert result.pptx[:4] == b"PK\x03\x04"
        assert "done" in events
        assert events[0] == "queued"
        # The Strategist's spec_lock is preserved verbatim.
        assert "lang: ko-KR" in result.spec_lock
