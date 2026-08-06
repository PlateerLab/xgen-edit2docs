"""Regression for deck_4.pptx — extra chart-template slides leaked in.

Production failure: deck_4.pptx shipped 15 slides where 1-5 were
chart / KPI / timeline / comparison / action template references and
6-15 were the actual 10-page presentation. The Strategist's
design_spec §VII Visualization Reference contained entries like

    - P03 · BAR CHART — 막대그래프: 항목 간 수치 비교
    - P04 · KEY METRIC — KPI 카드
    - P07 · COMPARISON — 두 가지 대안

and the generic P-id regex picked each up as a page boundary even
though they were just references. Two filters now guard the page
plan: a consecutive-run filter that anchors on P01, and a hard
truncation to spec_lock's `pages_total`.
"""

from __future__ import annotations

from xgen_edit2docs.tools.generate_deck import (
    _consecutive_run_starting_at_one,
    _expected_page_count,
)


# ---------------------------------------------------------------------------
# Page-count extraction from spec_lock
# ---------------------------------------------------------------------------


def test_pages_total_extracted_from_yaml_form():
    spec = (
        "project:\n"
        "  pages_total: 10\n"
        "  format: ppt169\n"
    )
    assert _expected_page_count(spec) == 10


def test_page_count_extracted_from_alternative_key_names():
    """The Strategist sometimes uses `page_count` or `total_pages`."""
    assert _expected_page_count("page_count: 12") == 12
    assert _expected_page_count("total_pages = 8") == 8
    assert _expected_page_count("deck_pages: 15") == 15


def test_no_page_count_returns_none():
    assert _expected_page_count("colors:\n  primary: '#000'") is None
    assert _expected_page_count("") is None


def test_unreasonable_page_count_returns_none():
    """A 200-page count is obviously garbage; treat as missing."""
    assert _expected_page_count("pages_total: 250") is None
    assert _expected_page_count("pages_total: 0") is None


# ---------------------------------------------------------------------------
# Consecutive run anchored at P01
# ---------------------------------------------------------------------------


def test_consecutive_run_drops_leading_reference_pages():
    """deck_4 case: regex picked up [P03, P04, P07, P01, P02, P03, ...,
    P10]. The run anchored at P01 should be the kept slice."""
    summaries = [
        "P03 · BAR CHART · 막대그래프",
        "P04 · KEY METRIC · KPI",
        "P07 · COMPARISON",
        "P01 · 커버 · 개발자의 종말",
        "P02 · 문제 제기",
        "P03 · 통계 현황",
        "P04 · 현실 진단",
        "P05 · 진화",
        "P06 · 도구",
        "P07 · 분기점",
        "P08 · 행동",
        "P09 · 메시지",
        "P10 · 클로징",
    ]
    out = _consecutive_run_starting_at_one(summaries)
    assert len(out) == 10
    assert out[0].startswith("P01")
    assert out[-1].startswith("P10")


def test_consecutive_run_handles_unnumbered_titles_in_between():
    """Slide titles without a P-id (cover pages named by intent only)
    pass through inside the run."""
    summaries = [
        "P01 · Cover",
        "Untitled slide",  # no P-id
        "P02 · Intro",
        "P03 · Body",
    ]
    out = _consecutive_run_starting_at_one(summaries)
    assert len(out) == 4


def test_consecutive_run_stops_at_index_gap():
    """When the indexes jump (P01..P03 then suddenly P10), the run
    ends at the gap."""
    summaries = ["P01", "P02", "P03", "P10 · stray ref"]
    out = _consecutive_run_starting_at_one(summaries)
    assert len(out) == 3


def test_consecutive_run_no_p_one_passes_through():
    """When the deck doesn't use P-id markers at all (just titles),
    we leave the list alone."""
    summaries = ["Cover", "Section A", "Section B"]
    out = _consecutive_run_starting_at_one(summaries)
    assert out == summaries


def test_consecutive_run_tolerates_repeated_index():
    """Same P-id appearing twice in the same summary (title + body)
    must not break the loop."""
    summaries = [
        "P01 · Cover · P01 reference",
        "P02 · Intro",
    ]
    out = _consecutive_run_starting_at_one(summaries)
    assert len(out) == 2


def test_empty_input_returns_empty():
    assert _consecutive_run_starting_at_one([]) == []


# ---------------------------------------------------------------------------
# End-to-end: realistic deck_4 design_spec snippet
# ---------------------------------------------------------------------------


def test_deck_4_template_references_filtered_out():
    """Build a minimal design_spec mirroring the failing case (chart
    template references in §VII, real outline in §IX) and verify the
    extracted pages start with the actual P01 cover."""
    from xgen_edit2docs.tools.generate_deck import _split_page_plan

    design_spec = (
        "## VII. Visualization Reference List\n"
        "- P03 · BAR CHART — 막대그래프: 항목 간 수치 비교\n"
        "- P04 · KEY METRIC — KPI 카드: 핵심 수치 강조\n"
        "- P07 · COMPARISON — 비교 분석\n\n"
        "## IX. 콘텐츠 아웃라인\n"
        "#### P01. 커버 — 개발자의 종말\n"
        "- 레이아웃: 풀블리드\n"
        "#### P02. 문제 제기\n"
        "#### P03. 통계 현황\n"
        "#### P04. 현실 진단\n"
        "#### P05. AI 코딩의 진화\n"
        "#### P06. AI는 도구가 아니다\n"
        "#### P07. 분기점\n"
        "#### P08. 행동 강령\n"
        "#### P09. 핵심 메시지\n"
        "#### P10. 클로징\n"
    )

    pages = _split_page_plan(design_spec, "")
    # Outline-scoped regex finds the §IX heading and confines the scan
    # to the 10 entries below it.
    assert len(pages) == 10
    assert "커버" in pages[0]
    assert "클로징" in pages[-1]
