"""Tests for P2.1 — deterministic layout brief generator.

Before this PR the Executor invented box geometry every call: the
page-number box was 42 px wide on slide A and 60 px on slide B, the
chapter label width varied, the body zone shifted up and down. The
brief generator produces ONE canonical geometry per rhythm tag so
every page of the deck (and every run of the deck) lands on the same
layout skeleton.
"""

from __future__ import annotations

import re

import pytest

from xgen_edit2docs.tools._layout_brief import (
    PageLayoutBrief,
    Zone,
    build_layout_briefs,
    render_brief_yaml,
    _parse_rhythm_from_spec_lock,
)


# ---------------------------------------------------------------------------
# Rhythm parsing from spec_lock
# ---------------------------------------------------------------------------


def test_rhythm_parsed_from_yaml_map():
    spec = (
        "page_rhythm:\n"
        "  P01: anchor\n"
        "  P02: dense\n"
        "  P03: breathing\n"
    )
    out = _parse_rhythm_from_spec_lock(spec)
    assert out == {1: "anchor", 2: "dense", 3: "breathing"}


def test_rhythm_parsed_from_markdown_list():
    spec = (
        "## page_rhythm\n"
        "- P01: anchor\n"
        "- P02: dense\n"
    )
    out = _parse_rhythm_from_spec_lock(spec)
    assert out == {1: "anchor", 2: "dense"}


def test_rhythm_parsed_with_two_digit_pid():
    """P10 / P12 are common in 12-page decks."""
    spec = "P09: breathing\nP10: anchor\nP12: breathing\n"
    out = _parse_rhythm_from_spec_lock(spec)
    assert out == {9: "breathing", 10: "anchor", 12: "breathing"}


def test_rhythm_unknown_value_skipped():
    """Only known tags are accepted; typos are ignored."""
    spec = "P01: anchor\nP02: typo\n"
    out = _parse_rhythm_from_spec_lock(spec)
    assert out == {1: "anchor"}


def test_rhythm_empty_spec_returns_empty():
    assert _parse_rhythm_from_spec_lock("") == {}


# ---------------------------------------------------------------------------
# Brief generation
# ---------------------------------------------------------------------------


def test_build_briefs_one_per_page():
    briefs = build_layout_briefs(spec_lock="", page_count=10)
    assert len(briefs) == 10
    assert [b.page_id for b in briefs] == [f"P{i:02d}" for i in range(1, 11)]


def test_first_and_last_default_to_anchor():
    """Without an explicit declaration, P01 (cover) and Pn (closing)
    default to `anchor`; everything else defaults to `dense`."""
    briefs = build_layout_briefs(spec_lock="", page_count=5)
    assert briefs[0].rhythm == "anchor"
    assert briefs[-1].rhythm == "anchor"
    assert all(b.rhythm == "dense" for b in briefs[1:-1])


def test_explicit_rhythm_overrides_default():
    spec = "P01: dense\nP05: breathing\n"
    briefs = build_layout_briefs(spec_lock=spec, page_count=5)
    assert briefs[0].rhythm == "dense"
    assert briefs[4].rhythm == "breathing"
    # Pages without an explicit declaration still get defaults.
    assert briefs[1].rhythm == "dense"


def test_every_brief_carries_canonical_footer_zones():
    """Page-number zone is always 1100×684, 140×20. This is the
    invariant that eliminates the "01 / 10" overflow issue once and
    for all."""
    briefs = build_layout_briefs(spec_lock="", page_count=3)
    for b in briefs:
        page_no = [z for z in b.zones if z.role == "page_number"]
        assert len(page_no) == 1, b.zones
        pz = page_no[0]
        assert pz.w >= 130  # generous for "NN / MM" at 12 pt
        assert pz.h >= 20


def test_every_brief_carries_chapter_label_zone():
    briefs = build_layout_briefs(spec_lock="", page_count=3)
    for b in briefs:
        chapter = [z for z in b.zones if z.role == "chapter_label"]
        assert len(chapter) == 1
        cz = chapter[0]
        # Full safe-area width so Korean+English chapter labels fit.
        assert cz.w == 1200


def test_zones_for_each_rhythm_match_their_purpose():
    """anchor → has hero, dense → has body grid, breathing → big hero."""
    anchor = build_layout_briefs(spec_lock="P01: anchor\n", page_count=1)[0]
    dense = build_layout_briefs(spec_lock="P01: dense\n", page_count=1)[0]
    breathing = build_layout_briefs(spec_lock="P01: breathing\n", page_count=1)[0]
    roles = lambda b: {z.role for z in b.zones}
    assert "hero" in roles(anchor)
    assert "body" in roles(dense)
    assert "hero" in roles(breathing)
    # Breathing pages don't carry a body grid — they're a single
    # concept page.
    assert "body" not in roles(breathing)


def test_zero_pages_returns_empty():
    assert build_layout_briefs(spec_lock="", page_count=0) == []


# ---------------------------------------------------------------------------
# YAML rendering (what the model actually sees)
# ---------------------------------------------------------------------------


def test_render_brief_yaml_is_valid_yaml():
    import yaml

    brief = build_layout_briefs(spec_lock="", page_count=1)[0]
    text = render_brief_yaml(brief)
    parsed = yaml.safe_load(text)
    assert parsed["page_id"] == "P01"
    assert parsed["rhythm"] in ("anchor", "dense", "breathing")
    assert parsed["canvas"] == {"w": 1280, "h": 720}
    assert parsed["safe_area"]["w"] == 1200
    # Zones round-trip cleanly.
    assert any(z["role"] == "page_number" for z in parsed["zones"])
    assert any(z["role"] == "chapter_label" for z in parsed["zones"])


def test_yaml_text_includes_role_names_model_recognises():
    """The model reads the rendered YAML — every role name should be
    a meaningful English token (title, hero, body, footer,
    page_number, chapter_label)."""
    brief = build_layout_briefs(spec_lock="P01: dense\n", page_count=1)[0]
    text = render_brief_yaml(brief)
    for token in ("page_number", "chapter_label", "footer", "title", "body"):
        assert token in text


# ---------------------------------------------------------------------------
# Integration with execute.py user_message
# ---------------------------------------------------------------------------


def test_user_message_carries_brief_first():
    """When the request has a layout_brief_yaml, it appears in the
    Executor's user_message BEFORE the page content so the LLM sees the
    constraints first.

    Token optimization: spec_lock is no longer inlined in the per-page
    user message — it now rides in the cached system suffix
    (`_build_spec_lock_suffix`), written once and read back per page.
    So we assert the brief precedes page content and spec_lock is absent
    from the user message but present in the suffix.
    """
    from xgen_edit2docs.tools.execute import (
        _build_spec_lock_suffix,
        _build_user_message,
        ExecutePageRequest,
    )

    brief = build_layout_briefs(spec_lock="", page_count=1)[0]
    req = ExecutePageRequest(
        spec_lock="lang: ko-KR\n",
        page_index=0,
        page_summary="test",
        lang="ko-KR",
        anthropic_api_key="x",
        layout_brief_yaml=render_brief_yaml(brief),
    )
    msg = _build_user_message(req)
    # Brief precedes page content; spec_lock has moved to the suffix.
    brief_pos = msg.find("Layout brief")
    content_pos = msg.find("Page content")
    assert 0 < brief_pos < content_pos
    assert "spec_lock" not in msg
    assert "spec_lock" in _build_spec_lock_suffix(req.spec_lock)


def test_user_message_without_brief_unchanged():
    """Legacy requests (no brief) get the previous message shape, minus
    the spec_lock block (now delivered via the cached system suffix)."""
    from xgen_edit2docs.tools.execute import _build_user_message, ExecutePageRequest

    msg = _build_user_message(
        ExecutePageRequest(
            spec_lock="lang: ko-KR\n",
            page_index=0,
            page_summary="test",
            lang="ko-KR",
            anthropic_api_key="x",
        )
    )
    assert "Layout brief" not in msg
    # spec_lock moved to the cached system suffix (token optimization).
    assert "spec_lock" not in msg
    assert "Page content" in msg
