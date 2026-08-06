"""Token-optimization tests for the GENERATION pipeline.

These verify the request-assembly + orchestration changes without a real
API key (all LLM calls go through recording/stub clients):

* Strategist source cap — oversized sources are truncated with a warning;
  under-cap sources are untouched; cap=0 disables.
* Executor spec_lock moved into the cached system suffix — the per-page
  user message no longer carries spec_lock (it rides in `system_suffix`,
  written once and read back per page), but the per-page layout brief and
  page summary still live in the user message.
* Executor fan-out cache warm-up — the first page runs to completion
  (warming the shared cache) before the rest fan out.
* Retry severity tiering — only structurally-broken layout violations
  promote to retry-worthy quality errors; cosmetic ones stay warnings.
* Per-stage cost visibility — `stage_costs` attributes token spend to the
  pipeline stage that incurred it.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage

# ---------------------------------------------------------------------------
# Shared fixtures / fakes
# ---------------------------------------------------------------------------

# A canonical-viewBox SVG that survives the executor normalisation passes
# and passes quality (no forbidden elements).
_GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<rect width="1280" height="720" fill="#FFFFFF"/>'
    '<text x="60" y="120" font-size="40" font-family="Pretendard, sans-serif">제목</text>'
    '</svg>'
)


@dataclass
class _RecordingClient:
    """Fake AnthropicClient that records every complete() call's kwargs and
    the start/end ordering of concurrent calls, without needing the
    `anthropic` package.

    `bad_pages` return text with no SVG block, so `execute_page` raises
    (used to exercise the warm-up-failure path)."""

    svg: str = _GOOD_SVG
    bad_pages: set = field(default_factory=set)
    calls: list = field(default_factory=list)
    events: list = field(default_factory=list)  # ("start"|"end", page_index)

    async def complete(
        self,
        system_prompt,
        user_message,
        *,
        system_suffix=None,
        model=None,
        max_output_tokens=8192,
        **kwargs,
    ) -> LLMResult:
        m = re.search(r"# Page (\d+)", user_message)
        page_index = int(m.group(1)) if m else -1
        self.events.append(("start", page_index))
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "system_suffix": system_suffix,
                "model": model,
                "max_output_tokens": max_output_tokens,
            }
        )
        # Yield so concurrent calls can interleave — the point of the
        # ordering assertions is that they DON'T interleave with page 0.
        await asyncio.sleep(0)
        self.events.append(("end", page_index))
        if page_index in self.bad_pages:
            body = "no svg here, just prose"
        else:
            body = f"```svg\n{self.svg}\n```\n```notes\nnote\n```"
        return LLMResult(
            text=body,
            usage=LLMUsage(input_tokens=10, output_tokens=10),
            model=model or "stub",
            stop_reason="end_turn",
        )


# ---------------------------------------------------------------------------
# 1. Strategist source cap
# ---------------------------------------------------------------------------


class TestStrategistSourceCap:
    def _build(self, sources, warnings, cap, monkeypatch):
        from xgen_edit2docs.config import reset_settings_cache
        from xgen_edit2docs.tools.strategize import StrategizeRequest, _build_user_message

        monkeypatch.setenv("EDIT2DOCS_STRATEGIST_SOURCE_CHAR_CAP", str(cap))
        reset_settings_cache()
        try:
            req = StrategizeRequest(
                sources_markdown=sources,
                user_intent="intent",
                anthropic_api_key="stub",
            )
            return _build_user_message(req, warnings)
        finally:
            monkeypatch.delenv("EDIT2DOCS_STRATEGIST_SOURCE_CHAR_CAP", raising=False)
            reset_settings_cache()

    def test_over_cap_source_truncated_and_warned(self, monkeypatch):
        big = "X" * 5000
        warnings: list = []
        msg = self._build([big], warnings, cap=1000, monkeypatch=monkeypatch)
        # Truncation marker present; the removed-count is reported.
        assert "…(truncated 4000 chars)" in msg
        # The full 5000-char body is NOT embedded.
        assert "X" * 1001 not in msg
        codes = [w.code for w in warnings]
        assert "strategist_source_truncated" in codes
        warn = next(w for w in warnings if w.code == "strategist_source_truncated")
        assert warn.detail["cap"] == 1000
        assert warn.detail["removed_chars"] == 4000

    def test_under_cap_source_untouched(self, monkeypatch):
        body = "small source body"
        warnings: list = []
        msg = self._build([body], warnings, cap=60000, monkeypatch=monkeypatch)
        assert body in msg
        assert "truncated" not in msg
        assert warnings == []

    def test_cap_zero_disables(self, monkeypatch):
        big = "Y" * 200000
        warnings: list = []
        msg = self._build([big], warnings, cap=0, monkeypatch=monkeypatch)
        # No truncation at all when cap is 0.
        assert big in msg
        assert "truncated" not in msg
        assert warnings == []


# ---------------------------------------------------------------------------
# 2. Executor spec_lock moved to the cached system suffix
# ---------------------------------------------------------------------------


class TestSpecLockInSystemSuffix:
    @pytest.mark.asyncio
    async def test_spec_lock_in_suffix_not_user_message(self):
        from xgen_edit2docs.tools.execute import ExecutePageRequest, execute_page

        client = _RecordingClient()
        req = ExecutePageRequest(
            spec_lock="canvas: ppt169\nMARKER_SPEC_LOCK_CONTENT: true\n",
            page_index=0,
            page_summary="MARKER_PAGE_SUMMARY body",
            layout_brief_yaml="zones:\n  title: MARKER_LAYOUT_BRIEF\n",
            lang="ko-KR",
            anthropic_api_key="stub",
        )
        await execute_page(req, client=client)

        call = client.calls[0]
        # spec_lock rides in the cached system suffix, written once per deck.
        assert call["system_suffix"] is not None
        assert "MARKER_SPEC_LOCK_CONTENT" in call["system_suffix"]
        assert "spec_lock" in call["system_suffix"]
        # ...and is NOT re-sent inside the per-page user message.
        assert "MARKER_SPEC_LOCK_CONTENT" not in call["user_message"]
        assert "spec_lock" not in call["user_message"]
        # The per-page layout brief + page summary DO live in the user message.
        assert "MARKER_LAYOUT_BRIEF" in call["user_message"]
        assert "MARKER_PAGE_SUMMARY" in call["user_message"]

    def test_suffix_builder_wraps_spec_lock(self):
        from xgen_edit2docs.tools.execute import _build_spec_lock_suffix

        suffix = _build_spec_lock_suffix("key: value\n")
        assert suffix.startswith("## spec_lock")
        assert "key: value" in suffix


# ---------------------------------------------------------------------------
# 3. Executor fan-out cache warm-up
# ---------------------------------------------------------------------------


class TestFanOutWarmUp:
    @pytest.mark.asyncio
    async def test_first_page_completes_before_rest_start(self):
        from xgen_edit2docs.tools.execute import (
            ExecuteBatchRequest,
            ExecutePageRequest,
            execute_batch,
        )

        client = _RecordingClient()
        pages = [
            ExecutePageRequest(
                spec_lock="k: v\n",
                page_index=i,
                page_summary=f"page {i}",
                lang="ko-KR",
                anthropic_api_key="stub",
            )
            for i in range(3)
        ]
        batch = await execute_batch(
            ExecuteBatchRequest(spec_lock="k: v\n", pages=pages), client=client
        )

        # All three pages present, in order.
        assert [r.page_index for r in batch.results] == [0, 1, 2]
        # Warm-up: page 0 starts first AND finishes before pages 1/2 start.
        assert client.events[0] == ("start", 0)
        end0 = client.events.index(("end", 0))
        assert end0 < client.events.index(("start", 1))
        assert end0 < client.events.index(("start", 2))

    @pytest.mark.asyncio
    async def test_single_page_batch_unaffected(self):
        from xgen_edit2docs.tools.execute import (
            ExecuteBatchRequest,
            ExecutePageRequest,
            execute_batch,
        )

        client = _RecordingClient()
        batch = await execute_batch(
            ExecuteBatchRequest(
                spec_lock="k: v\n",
                pages=[
                    ExecutePageRequest(
                        spec_lock="k: v\n",
                        page_index=0,
                        page_summary="only",
                        lang="ko-KR",
                        anthropic_api_key="stub",
                    )
                ],
            ),
            client=client,
        )
        assert len(batch.results) == 1
        assert client.events == [("start", 0), ("end", 0)]

    @pytest.mark.asyncio
    async def test_warmup_failure_still_fans_out_rest(self):
        from xgen_edit2docs.tools.execute import (
            ExecuteBatchRequest,
            ExecutePageRequest,
            execute_batch,
        )

        # Page 0 (the warm-up page) fails to produce an SVG.
        client = _RecordingClient(bad_pages={0})
        pages = [
            ExecutePageRequest(
                spec_lock="k: v\n",
                page_index=i,
                page_summary=f"page {i}",
                lang="ko-KR",
                anthropic_api_key="stub",
            )
            for i in range(3)
        ]
        batch = await execute_batch(
            ExecuteBatchRequest(spec_lock="k: v\n", pages=pages), client=client
        )
        # Still three results (page 0 is a placeholder), rest succeeded.
        assert [r.page_index for r in batch.results] == [0, 1, 2]
        assert "could not be generated" in batch.results[0].svg
        assert any(w.code == "execute_page_failed" for w in batch.warnings)
        assert "제목" in batch.results[1].svg  # page 1 real content survived


# ---------------------------------------------------------------------------
# 4. Retry severity tiering
# ---------------------------------------------------------------------------


@dataclass
class _StubWarning:
    code: str
    message: str = ""
    detail: dict | None = None


@dataclass
class _StubPage:
    page_index: int
    warnings: list


class _StubQualityResp:
    def __init__(self):
        self.issues: list = []
        self.passed: bool = True


def _promote(warnings):
    from xgen_edit2docs.tools.generate_deck import _promote_layout_violations

    page = _StubPage(page_index=0, warnings=warnings)
    resp = _StubQualityResp()
    _promote_layout_violations({0: page}, resp)
    return resp


class TestRetrySeverityTiering:
    def test_cosmetic_small_overlap_not_promoted(self):
        """A minor overlap (below the 0.5 ratio gate) stays a warning — it
        does NOT drive an expensive full-page re-generation."""
        resp = _promote(
            [
                _StubWarning(
                    code="layout_overlap",
                    detail={"actual": {"overlap_ratio": 0.3}, "fix_applied": False},
                )
            ]
        )
        assert resp.issues == []
        assert resp.passed is True

    def test_heavy_overlap_promoted(self):
        resp = _promote(
            [
                _StubWarning(
                    code="layout_overlap",
                    detail={"actual": {"overlap_ratio": 0.8}, "fix_applied": False},
                )
            ]
        )
        assert any(i.code == "layout_overlap" and i.severity == "error" for i in resp.issues)
        assert resp.passed is False

    def test_off_canvas_always_promoted(self):
        resp = _promote(
            [
                _StubWarning(
                    code="layout_off_canvas",
                    detail={"actual": {"bbox": (1300, 100, 50, 50)}, "fix_applied": False},
                )
            ]
        )
        assert any(i.code == "layout_off_canvas" and i.severity == "error" for i in resp.issues)
        assert resp.passed is False

    def test_subthreshold_overflow_not_promoted(self):
        """required_w/box_w = 1.05 < 1.15 → cosmetic, stays a warning."""
        resp = _promote(
            [
                _StubWarning(
                    code="layout_text_overflow_x",
                    detail={
                        "actual": {"required_w": 105, "box_w": 100, "text": "x"},
                        "fix_applied": False,
                    },
                )
            ]
        )
        assert resp.issues == []
        assert resp.passed is True

    def test_structural_overflow_promoted(self):
        """required_w/box_w = 2.0 > 1.15 → the box genuinely can't hold the
        text, so it promotes."""
        resp = _promote(
            [
                _StubWarning(
                    code="layout_text_overflow_x",
                    detail={
                        "actual": {"required_w": 200, "box_w": 100, "text": "x"},
                        "fix_applied": False,
                    },
                )
            ]
        )
        assert any(i.code == "layout_text_overflow_x" for i in resp.issues)
        assert resp.passed is False

    def test_fixed_violation_never_promoted(self):
        resp = _promote(
            [
                _StubWarning(
                    code="layout_off_canvas",
                    detail={"actual": {"bbox": (1300, 0, 10, 10)}, "fix_applied": True},
                )
            ]
        )
        assert resp.issues == []
        assert resp.passed is True

    def test_helper_thresholds_are_module_constants(self):
        # tools/__init__ re-exports the `generate_deck` function, shadowing
        # the submodule name — fetch the module via sys.modules.
        import sys

        gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        assert gd._OVERLAP_PROMOTE_RATIO == 0.5
        assert gd._TEXT_OVERFLOW_PROMOTE_RATIO == 1.15


# ---------------------------------------------------------------------------
# 5. Per-stage cost visibility
# ---------------------------------------------------------------------------


class TestStageCosts:
    @pytest.mark.asyncio
    async def test_stage_costs_populated(self, monkeypatch):
        import sys

        from xgen_edit2docs.tools.execute import execute_batch
        from xgen_edit2docs.tools.export import ExportResponse
        from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck
        from xgen_edit2docs.tools.strategize import StrategizeResponse
        from xgen_edit2docs.tools.types import CostBreakdown

        gd = sys.modules["xgen_edit2docs.tools.generate_deck"]

        async def fake_strategize(req, *, client=None):
            return StrategizeResponse(
                raw_output="stub",
                design_spec="## IX. Outline\n#### P01. a\n#### P02. b\n",
                spec_lock="canvas:\n  format: ppt169\ncolors:\n  primary: '#000000'\n",
                cost=CostBreakdown(),
            )

        stub_llm = _RecordingClient()

        async def patched_execute_batch(req, *, client=None):
            return await execute_batch(req, client=stub_llm)

        def fake_export(req):
            return ExportResponse(
                pptx=b"PK\x03\x04stub",
                page_count=len(req.slides),
                detected_langs=[],
                cost=CostBreakdown(),
            )

        monkeypatch.setattr(gd, "strategize", fake_strategize)
        monkeypatch.setattr(gd, "execute_batch", patched_execute_batch)
        monkeypatch.setattr(gd, "export_pptx", fake_export)
        monkeypatch.setattr(gd, "AnthropicClient", lambda **kw: stub_llm)

        resp = await generate_deck(
            GenerateDeckRequest(
                sources=[],
                user_intent="테스트",
                anthropic_api_key="sk-ant-stub",
                target_pages=(2, 2),
                skip_images=True,
                fail_on_quality_error=False,
            )
        )

        # The named pipeline stages are all attributed.
        assert {"strategize", "execute", "quality", "export"} <= set(resp.stage_costs)
        # Each entry carries exactly the four billable token counters.
        for entry in resp.stage_costs.values():
            assert set(entry) == {
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            }
        # Execute spend (2 pages * 10 input tokens) is attributed to execute.
        assert resp.stage_costs["execute"]["input_tokens"] == 20
        # Aggregate cost is unchanged in shape and includes the execute spend.
        assert resp.cost.input_tokens >= 20
