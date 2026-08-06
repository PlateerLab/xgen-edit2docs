"""Tests for P2.3 — self-check sections in Strategist + Executor prompts.

The prompts ship as markdown files loaded at runtime. A regression
where the section gets dropped (e.g. by a clumsy edit) would silently
remove a load-bearing safety check from production. These tests
assert every check item we rely on stays present.
"""

from __future__ import annotations

from xgen_edit2docs.llm import load_prompt


# ---------------------------------------------------------------------------
# Strategist self-check
# ---------------------------------------------------------------------------


def _strategist() -> str:
    return load_prompt("strategist")


def test_strategist_self_check_section_present():
    """The Strategist prompt must end with a self-check section."""
    text = _strategist()
    assert "Self-check" in text or "self-check" in text
    assert "Z. Self-check" in text or "Z.1" in text


def test_strategist_self_check_counts_alignment():
    """Z.1 — pages_total must equal §IX page count, consecutive P-ids."""
    text = _strategist()
    assert "pages_total" in text
    assert "consecutive" in text or "no gaps" in text


def test_strategist_self_check_palette_rule():
    """Z.2 — palette discipline + hex format."""
    text = _strategist()
    assert "6-digit uppercase" in text or "#0A1628" in text
    # Bans rgb()/rgba() in the palette section.
    assert "rgb(" in text or "rgba(" in text


def test_strategist_self_check_windows_safe_font_tail():
    """Z.3 — every stack ends with a Windows-installed family."""
    text = _strategist()
    assert "Windows-installed" in text or "Malgun Gothic" in text


def test_strategist_self_check_icon_inventory():
    """Z.4 — icon names must be real filenames; one library per deck."""
    text = _strategist()
    assert "icons.inventory" in text or "icons" in text.lower()
    assert "library" in text.lower()


def test_strategist_self_check_image_placeholder_convention():
    """Z.5 — bare filename, not `../images/...`."""
    text = _strategist()
    assert "../images/" in text  # called out as a forbidden pattern
    assert "bare filename" in text or "acquire_via" in text


def test_strategist_self_check_layout_zone_rules():
    """Z.6 — page_number ≥ 130 px, chapter-label vs title disjoint."""
    text = _strategist()
    assert "130" in text  # min page-number width
    assert "chapter-label" in text or "chapter_label" in text


# ---------------------------------------------------------------------------
# Executor self-check
# ---------------------------------------------------------------------------


def _executor_base() -> str:
    return load_prompt("executor-base")


def test_executor_self_check_section_present():
    text = _executor_base()
    assert "Self-check" in text or "self-check" in text


def test_executor_brief_alignment_rule():
    """Z.1 — every text element sits inside its brief zone."""
    text = _executor_base()
    assert "Layout brief" in text or "layout brief" in text
    assert "page_number" in text


def test_executor_coordinate_hygiene_rule():
    """Z.2 — canvas bounds, no overlapping text."""
    text = _executor_base()
    assert "1280" in text
    assert "720" in text
    assert "overlap" in text


def test_executor_font_size_band_rule():
    """Z.3 — 12 to 180 pt band, no weight in family name."""
    text = _executor_base()
    assert "12" in text
    assert "180" in text
    # Specifically calls out the broken-family-name pattern from
    # past deck regressions.
    assert "Pretendard 700" in text or "weight inside the family name" in text


def test_executor_forbidden_elements_rule():
    """Z.4 — no use href / foreignObject / script / rgba."""
    text = _executor_base()
    for token in ("foreignObject", "script", "rgba"):
        assert token in text


def test_executor_hierarchy_rule():
    """Z.5 — one primary title; oversize glyphs are layout failure."""
    text = _executor_base()
    assert "primary title" in text or "ONE primary" in text


# ---------------------------------------------------------------------------
# Build assertions — the prompt is included end-to-end in the
# system message the model receives.
# ---------------------------------------------------------------------------


def test_executor_system_prompt_includes_self_check():
    """The full system prompt threaded together by `_build_system_prompt`
    contains the Executor self-check section."""
    from xgen_edit2docs.tools.execute import _build_system_prompt

    full = _build_system_prompt(style="general", lang="ko-KR")
    assert "Self-check" in full
    assert "Layout brief" in full  # P2.1 contract still present
