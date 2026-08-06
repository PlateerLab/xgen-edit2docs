"""Unit tests for the C3 robustness improvements.

Covers three areas:
- Page-plan parsing tolerates many heading patterns (Korean, Japanese,
  numbered without keyword, hyphenated, colon-separated).
- execute_batch preserves partial results when a per-page call raises —
  the failing page gets a placeholder SVG; subsequent stages see N slides.
- generate_deck retries quality-error pages when retry_pages_on_quality_error
  is set, surfacing each retry as a warning.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools import (
    ConvertRequest,
    ConvertResponse,
    CostBreakdown,
    ExecuteBatchRequest,
    ExecuteBatchResponse,
    ExecutePageRequest,
    ExecutePageResponse,
    StrategizeResponse,
    execute_batch,
)
from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck, _split_page_plan


# ---------------------------------------------------------------------------
# Page-plan parsing
# ---------------------------------------------------------------------------


class TestPagePlanParsing:
    def test_english_page_headings(self):
        spec = "## Page 1\ncover\n## Page 2\nsummary\n## Page 3\nconclusion"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 3
        assert "cover" in chunks[0]
        assert "summary" in chunks[1]

    def test_korean_page_keyword(self):
        spec = "## 페이지 1\n표지\n## 페이지 2\n결론"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2
        assert "표지" in chunks[0]

    def test_korean_slide_keyword(self):
        spec = "## 슬라이드 1\n표지\n## 슬라이드 2\n결론"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_japanese_keywords(self):
        spec = "## ページ 1\n表紙\n## スライド 2\nまとめ"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_hyphenated_form(self):
        spec = "## Page-1\ncover\n## Page-2\nsummary"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_colon_with_title(self):
        spec = "## Slide 1: Cover page\nintro\n## Slide 2: Summary\nfindings"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_em_dash_form(self):
        spec = "## 페이지 1 — 표지\ncontents\n## 페이지 2 — 결론\nmore"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_numbered_without_keyword(self):
        spec = "## 1. Cover\nintro\n## 2. Summary\nfindings\n## 3) Conclusion\nwrap"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 3

    def test_mixed_levels(self):
        # Strategist may emit h1 or h3 instead of h2.
        spec = "# Page 1\nintro\n### Page 2\nbody"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_no_headings_falls_back_to_spec_lock(self):
        design_spec = "Just freeform text with no headings."
        spec_lock = "pages:\n  - title: a\n  - title: b\n  - title: c"
        chunks = _split_page_plan(design_spec, spec_lock)
        assert len(chunks) >= 1  # falls back to spec_lock pages parsing

    def test_reference_template_h4_slide_headings(self):
        """The shipped design_spec_reference.md uses h4 (`#### Slide 01 - Cover`)
        for page outlines, under `## IX. Content Outline` / `### Part 1`. The
        parser must reach that depth or the executor never runs."""
        spec = (
            "## IX. Content Outline\n"
            "### Part 1: Intro\n"
            "#### Slide 01 - Cover\n"
            "- Title: 표지\n"
            "#### Slide 02 - Overview\n"
            "- Title: 개요\n"
            "#### Slide 03 - Conclusion\n"
            "- Title: 결론\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 3
        assert "표지" in chunks[0]
        assert "개요" in chunks[1]
        assert "결론" in chunks[2]

    def test_h5_h6_headings_still_caught(self):
        """Strategist occasionally nests page outlines one level deeper."""
        spec = "##### Slide 1\na\n###### Slide 2\nb"
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2

    def test_yaml_fallback_pages_list(self):
        """No headings in design_spec; spec_lock has a `pages:` YAML list
        with structured dict entries. YAML parser is the robust path."""
        design_spec = "freeform text without page headings"
        spec_lock = (
            "lang: ko-KR\n"
            "pages:\n"
            "  - title: 표지\n"
            "    layout: cover\n"
            "  - title: 본문\n"
            "    layout: split\n"
            "  - title: 결론\n"
            "    layout: closing\n"
        )
        chunks = _split_page_plan(design_spec, spec_lock)
        assert len(chunks) == 3
        # Dict entries are rendered as YAML so the executor can still read them.
        assert "표지" in chunks[0]
        assert "layout" in chunks[0]

    def test_yaml_fallback_accepts_slides_synonym(self):
        """When the Strategist names the list `slides` instead of `pages`."""
        spec_lock = "slides:\n  - one\n  - two\n  - three\n"
        chunks = _split_page_plan("", spec_lock)
        assert chunks == ["one", "two", "three"]

    def test_markdown_spec_lock_page_rhythm_rows(self):
        """The shipped spec_lock reference uses markdown (`## page_rhythm`
        with `- P01: anchor` data lines) instead of pure YAML. The page-
        rhythm row count must drive the page list."""
        spec_lock = (
            "## canvas\n- viewBox: 0 0 1280 720\n\n"
            "## page_rhythm\n"
            "- P01: anchor\n"
            "- P02: dense\n"
            "- P03: breathing\n"
            "- P04: dense\n"
        )
        chunks = _split_page_plan("freeform with no headings", spec_lock)
        assert len(chunks) == 4
        # P01 carries the rhythm tag so the executor sees the structure.
        assert "anchor" in chunks[0]
        assert chunks[0].startswith("# P01")

    def test_markdown_spec_lock_dedupes_across_sections(self):
        """`## page_rhythm` + `## page_layouts` reference the same P-ids;
        the parser merges rather than double-counting them."""
        spec_lock = (
            "## page_rhythm\n"
            "- P01: anchor\n"
            "- P02: dense\n"
            "## page_layouts\n"
            "- P01: 01_cover\n"
            "- P02: 03a_content\n"
        )
        chunks = _split_page_plan("", spec_lock)
        assert len(chunks) == 2  # not 4
        assert "anchor" in chunks[0]
        assert "01_cover" in chunks[0]

    def test_raw_output_fallback_when_design_spec_truncated(self):
        """When fence extraction truncated design_spec mid-document and
        §IX Content Outline only exists in raw_output, scan that as a
        fallback so the executor still gets pages."""
        truncated_spec = "## I. Project Info\n## II. Canvas\n"
        raw_output = (
            "```design_spec\n"
            + truncated_spec
            + "## V. Layout\n```svg\n<svg/>\n```\n"  # nested fence broke extraction
            + "## IX. Content Outline\n"
            + "#### Slide 01 - Cover\n- Title: 표지\n"
            + "#### Slide 02 - Body\n- Title: 본문\n"
            + "```\n"
        )
        chunks = _split_page_plan(truncated_spec, "", raw_output=raw_output)
        assert len(chunks) == 2
        assert "표지" in chunks[0]
        assert "본문" in chunks[1]

    def test_returns_empty_when_truly_nothing_parseable(self):
        """Diagnostic path: returning [] is the signal for the caller to
        log and raise — must NOT crash inside the parser itself."""
        chunks = _split_page_plan("just prose", "lang: ko-KR\n", raw_output="")
        assert chunks == []

    def test_bold_p_id_pages_inside_chapter_headings(self):
        """Real production output: Strategist used `### Chapter N` for
        section breaks and `**P06 — 두 가지 길 (anchor)**` for the page
        markers themselves. The page regex must catch the bold P-ids."""
        spec = (
            "## IX. 콘텐츠 아웃라인\n\n"
            "### Chapter 1 — 도입\n\n"
            "**P01 — 표지 (anchor)**\n"
            "- 레이아웃: 풀블리드\n"
            "- 메인 메시지: 개발자의 종말\n\n"
            "**P02 — 도입 (breathing)**\n"
            "- 레이아웃: 풀블리드 인용\n\n"
            "### Chapter 2 — 현실 진단\n\n"
            "**P03 — 통계 (dense)**\n"
            "- 레이아웃: KPI 카드 4x1\n"
            "- KPI 1: 55%\n\n"
            "**P04 — 비교 (dense)**\n"
            "- 레이아웃: 좌우 분할\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 4
        assert "표지" in chunks[0]
        assert "도입" in chunks[1]
        assert "KPI" in chunks[2]
        assert "비교" in chunks[3]

    def test_yaml_spec_lock_page_rhythm_as_dict(self):
        """Real production spec_lock: page_rhythm is a YAML map
        (`P01: anchor`, `P02: breathing`, …), not a list. Each entry
        must become one page summary in declaration order."""
        spec_lock = (
            "project:\n"
            "  pages_total: 4\n"
            "page_rhythm:\n"
            "  P01: anchor\n"
            "  P02: breathing\n"
            "  P03: dense\n"
            "  P04: dense\n"
        )
        chunks = _split_page_plan("", spec_lock)
        assert len(chunks) == 4
        assert chunks[0].startswith("# P01")
        assert "anchor" in chunks[0]
        assert "page_rhythm: anchor" in chunks[0]

    def test_yaml_spec_lock_unions_multiple_dict_sections(self):
        """page_rhythm + page_layouts both map P-id → tag. The merged
        result lists every P-id once with every section's attribute."""
        spec_lock = (
            "page_rhythm:\n"
            "  P01: anchor\n"
            "  P02: dense\n"
            "page_layouts:\n"
            "  P01: 01_cover\n"
            "  P02: 03a_content\n"
            "page_charts:\n"
            "  P02: bar_chart\n"
        )
        chunks = _split_page_plan("", spec_lock)
        assert len(chunks) == 2
        assert "page_rhythm: anchor" in chunks[0]
        assert "page_layouts: 01_cover" in chunks[0]
        assert "page_rhythm: dense" in chunks[1]
        assert "page_charts: bar_chart" in chunks[1]

    def test_real_world_production_design_spec_snippet(self):
        """Regression guard: a near-verbatim slice of the failing prod
        case (chapter headings + bold P-id pages + Korean) must parse."""
        spec = (
            "## IX. 콘텐츠 아웃라인\n\n"
            "### Chapter 3 — 갈림길\n\n"
            "**P06 — 두 가지 길 (anchor)**\n"
            "- 레이아웃: 좌우 분할 5:5\n"
            "- 좌측 (Alert Red): `활용하지 못한 자`\n"
            "- 우측 (Neon Lime): `활용하는 자`\n\n"
            "**P07 — 결정적 질문 (breathing)**\n"
            "- 레이아웃: 풀블리드 인용\n\n"
            "### Chapter 4 — 행동\n\n"
            "**P08 — 무엇을 잃지 말아야 하는가 (dense)**\n"
            "- 레이아웃: 센터 라디에이팅\n\n"
            "**P09 — 지금부터 해야 할 일 (dense)**\n"
            "- 레이아웃: 수평 타임라인\n\n"
            "**P10 — 마지막 메시지 (breathing)**\n"
            "- 레이아웃: 풀블리드\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 5
        # First chunk holds P06; last chunk holds P10.
        assert "P06" in chunks[0]
        assert "두 가지 길" in chunks[0]
        assert "P10" in chunks[-1]

    def test_markdown_heading_with_p_id_prefix(self):
        """The format that failed in iteration #4: `#### P01. 커버`
        markdown headings under `## IX. 콘텐츠 아웃라인 (Content Outline)`,
        with chapter subsections (`### Chapter 1: Opening`) between."""
        spec = (
            "## IX. 콘텐츠 아웃라인 (Content Outline)\n\n"
            "### Chapter 1: Opening\n\n"
            "#### P01. 커버\n"
            "- 레이아웃: 풀블리드\n\n"
            "#### P02. 현실 진단 — AI는 이미 여기 있다\n"
            "- 레이아웃: 헤로 인용 + 보조 통계\n\n"
            "#### P03. AI 코딩 능력의 진화\n"
            "- 레이아웃: 타임라인 (3 step)\n\n"
            "### Chapter 2: 위기의 본질\n\n"
            "#### P04. 5단계 위기\n"
            "- 레이아웃: 단계 카드\n\n"
            "#### P05. 살아남는 vs 사라지는\n"
            "- 레이아웃: 좌우 비교\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 5
        assert "커버" in chunks[0]
        assert "현실 진단" in chunks[1]
        assert "타임라인" in chunks[2]
        assert "5단계" in chunks[3]
        assert "사라지는" in chunks[4]

    def test_outline_scoping_avoids_inline_p_id_mentions(self):
        """Prose in earlier sections may mention P01/P02; the outline-
        scoped scan must ignore those false positives once it finds the
        actual §IX. Content Outline section."""
        spec = (
            "## I. Project Info\n\n"
            "이 발표는 10페이지로 구성되며 P01-P10 으로 명명됩니다.\n\n"
            "## V. Layout Principles\n\n"
            "P01 은 풀블리드, P10 은 호흡형 레이아웃을 권장합니다.\n\n"
            "## IX. Content Outline\n\n"
            "#### P01. 커버\n- 레이아웃: 풀블리드\n\n"
            "#### P02. 도입\n- 레이아웃: 호흡형\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 2
        # The prose mentions of P01 / P10 in §I and §V must not register
        # as page boundaries — confined scan starts at §IX.
        assert "커버" in chunks[0]
        assert "도입" in chunks[1]

    def test_outline_section_with_only_roman_numeral(self):
        """`## IX 콘텐츠 아웃라인` without the period after IX should also
        be recognized as the outline section start."""
        spec = (
            "## IX 콘텐츠 아웃라인\n\n"
            "#### P01. 커버\n- a\n#### P02. 본문\n- b\n#### P03. 결론\n- c\n"
        )
        chunks = _split_page_plan(spec, "")
        assert len(chunks) == 3


# ---------------------------------------------------------------------------
# execute_batch partial-result preservation
# ---------------------------------------------------------------------------


@dataclass
class _MixedSuccessClient:
    """Returns one good SVG, raises on the second call, returns good on the third."""

    calls: list[dict] = field(default_factory=list)
    fail_on_call_index: int = 1

    async def complete(self, system_prompt, user_message, **kwargs):
        idx = len(self.calls)
        self.calls.append({"system": system_prompt, "user": user_message, **kwargs})
        if idx == self.fail_on_call_index:
            raise RuntimeError("simulated upstream LLM blow-up")
        return LLMResult(
            text="```svg\n<svg></svg>\n```\n```notes\nx\n```",
            usage=LLMUsage(input_tokens=10, output_tokens=5),
            model="stub",
            stop_reason="end_turn",
        )


class TestExecuteBatchPartialResults:
    @pytest.mark.asyncio
    async def test_failed_page_gets_placeholder_and_others_succeed(self):
        client = _MixedSuccessClient()
        page_reqs = [
            ExecutePageRequest(
                spec_lock="lang: ko-KR",
                page_index=i,
                page_summary=f"page {i}",
                lang="ko-KR",
                anthropic_api_key="stub",
            )
            for i in range(3)
        ]
        batch = await execute_batch(
            ExecuteBatchRequest(spec_lock="lang: ko-KR", pages=page_reqs),
            client=client,
        )

        # All three pages have a result entry (no aborted gather()).
        assert len(batch.results) == 3
        assert [r.page_index for r in batch.results] == [0, 1, 2]

        # The failing page is a placeholder; others have real SVG from the stub.
        # The placeholder contains the specific failure text.
        failed = [r for r in batch.results if "could not be generated" in r.svg]
        assert len(failed) == 1
        # The warning is surfaced with the right code.
        codes = {w.code for w in batch.warnings}
        assert "execute_page_failed" in codes


# ---------------------------------------------------------------------------
# Quality retry inside generate_deck
# ---------------------------------------------------------------------------


@dataclass
class _StrategizeStub:
    page_count: int

    async def __call__(self, req, *, client=None):
        return StrategizeResponse(
            raw_output="...",
            design_spec="\n\n".join(
                f"## Page {i+1}\npage {i} content" for i in range(self.page_count)
            ),
            spec_lock="lang: ko-KR\npages:\n  - p0\n  - p1",
            cost=CostBreakdown(input_tokens=5, output_tokens=5),
        )


# Canvas-format default is ppt169 -> viewBox `0 0 1280 720`. Stick to that
# so quality_check accepts the SVG without complaining about size mismatch.
_GOOD_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<rect x="0" y="0" width="1280" height="720" fill="#ffffff"/>'
    '<text x="120" y="360" font-family="Pretendard, sans-serif" '
    'font-size="48" fill="#1a1a1a">테스트</text>'
    "</svg>"
)
# Forbidden element <foreignObject> reliably triggers the quality checker.
_BAD_SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<foreignObject x="0" y="0" width="1280" height="720">'
    "<div>broken</div>"
    "</foreignObject>"
    "</svg>"
)


@dataclass
class _RetryingExecuteBatchStub:
    """First call: bad SVG on page 1. Retry call: good SVG. Page 0 stays good
    the whole time."""

    received_specs: list[list[ExecutePageRequest]] = field(default_factory=list)

    async def __call__(self, req, *, client=None):
        self.received_specs.append(req.pages)
        results = []
        is_retry = len(self.received_specs) > 1
        for p in req.pages:
            if is_retry or p.page_index == 0:
                svg = _GOOD_SVG
            else:
                svg = _BAD_SVG
            results.append(
                ExecutePageResponse(
                    page_index=p.page_index,
                    svg=svg,
                    speaker_notes="",
                    raw_output="...",
                    cost=CostBreakdown(),
                    warnings=[],
                )
            )
        return ExecuteBatchResponse(results=results, cost=CostBreakdown(), warnings=[])


class TestQualityRetry:
    def setup_method(self):
        self.gd = sys.modules["xgen_edit2docs.tools.generate_deck"]

    @pytest.mark.asyncio
    async def test_retry_invoked_with_simplification_hint(self, monkeypatch):
        # Wire stubs.
        strat = _StrategizeStub(page_count=2)
        execute = _RetryingExecuteBatchStub()
        monkeypatch.setattr(self.gd, "strategize", strat)
        monkeypatch.setattr(self.gd, "execute_batch", execute)
        monkeypatch.setattr(self.gd, "convert_to_markdown",
                            lambda r: ConvertResponse(
                                markdown="# x", detected_format="pdf",
                                original_filename=None, char_count=1,
                                cost=CostBreakdown(),
                            ))
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())

        result = await generate_deck(
            GenerateDeckRequest(
                sources=[ConvertRequest(source_type="pdf", content=b"%PDF")],
                user_intent="x",
                target_pages=(2, 2),
                lang="ko-KR",
                anthropic_api_key="sk-ant-stub",
                fail_on_quality_error=False,  # don't fail; we want to inspect warnings
                retry_pages_on_quality_error=1,
                skip_images=True,
            )
        )

        # execute_batch was called twice: initial + 1 retry.
        assert len(execute.received_specs) == 2

        # The retry batch contained only the failing page (page_index=1).
        retry_pages = execute.received_specs[1]
        assert len(retry_pages) == 1
        assert retry_pages[0].page_index == 1
        # The retry hint is appended to the summary.
        assert "Retry hint" in retry_pages[0].page_summary

        # Warnings surface the retry.
        codes = {w.code for w in result.warnings}
        assert "quality_retry" in codes
