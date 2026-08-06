"""Tests for the font-family numeric-weight-suffix stripping.

Production failure (analysed from a real deck): the LLM emitted
`font-family="Pretendard 700, Apple SD Gothic Neo, ..."`. The converter
treated `Pretendard 700` as a single typeface name, fed it to
`<a:latin typeface="Pretendard 700"/>`, and PowerPoint fell back to
the system default because no font with that literal name exists. 243
text runs in a single 10-slide deck rendered in the wrong typeface.

The fix: strip a trailing numeric CSS-weight token from each font name
before the EA_FONTS / FONT_FALLBACK_WIN lookup. Numeric-only is
conservative — word weights are left alone because they appear in
legitimate family names (`Arial Black`, `Helvetica Neue Light`).
"""

from __future__ import annotations

from xgen_edit2docs.core.svg_to_pptx.drawingml_utils import (
    _strip_weight_suffix,
    parse_font_family,
)


def test_strips_trailing_numeric_weight():
    assert _strip_weight_suffix("Pretendard 700") == "Pretendard"
    assert _strip_weight_suffix("Pretendard 400") == "Pretendard"
    assert _strip_weight_suffix("Pretendard 900") == "Pretendard"


def test_strips_with_extra_words_in_family():
    assert _strip_weight_suffix("Pretendard Variable 900") == "Pretendard Variable"
    assert _strip_weight_suffix("Noto Sans KR 700") == "Noto Sans KR"


def test_leaves_genuine_family_names_alone():
    """Word-form weight tokens (`Black`, `Light`, `Bold`) inside real
    family names must NOT be stripped — only numeric suffixes."""
    assert _strip_weight_suffix("Arial Black") == "Arial Black"
    assert _strip_weight_suffix("Helvetica Neue Light") == "Helvetica Neue Light"
    assert _strip_weight_suffix("Lucida Bright") == "Lucida Bright"
    assert _strip_weight_suffix("Pretendard Bold") == "Pretendard Bold"


def test_leaves_mid_string_numbers_alone():
    """A number that isn't at the very end is part of the name."""
    assert _strip_weight_suffix("Inter 400 Display") == "Inter 400 Display"
    assert _strip_weight_suffix("Pretendard 800 Title") == "Pretendard 800 Title"


def test_no_strip_when_token_isnt_a_css_weight():
    """100-900 only — 50, 150, 950 are not CSS weights."""
    assert _strip_weight_suffix("Pretendard 50") == "Pretendard 50"
    assert _strip_weight_suffix("Pretendard 950") == "Pretendard 950"
    assert _strip_weight_suffix("Pretendard 1000") == "Pretendard 1000"


def test_empty_input_is_passthrough():
    assert _strip_weight_suffix("") == ""


def test_parse_font_family_now_picks_pretendard_correctly():
    """End-to-end: the broken production stack must resolve to
    Pretendard (latin) → Malgun Gothic mapping (ea) instead of staying
    as the unresolved `Pretendard 700`."""
    stack = '"Pretendard 700", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
    parsed = parse_font_family(stack)
    # Latin is set from the lead family (Pretendard maps to Malgun Gothic
    # via FONT_FALLBACK_WIN). The critical assertion is that the broken
    # string `Pretendard 700` does NOT show up in the output.
    assert "Pretendard 700" not in parsed["latin"]
    assert "Pretendard 700" not in parsed["ea"]
    # EA must end up Malgun Gothic for Korean rendering.
    assert parsed["ea"] == "Malgun Gothic"


def test_parse_font_family_900_variant():
    parsed = parse_font_family('"Pretendard 900", "Malgun Gothic", sans-serif')
    assert "900" not in parsed["latin"]
    assert parsed["ea"] == "Malgun Gothic"


def test_korean_stack_mirrors_ea_into_latin():
    """When the stack contains only CJK families plus a generic
    `sans-serif`, both `latin` and `ea` must resolve to the Korean
    font. Otherwise the trailing `sans-serif` would set
    `latin=Segoe UI` and Hangul glyphs would lose their weight."""
    parsed = parse_font_family(
        '"Pretendard 700", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
    )
    assert parsed["latin"] == "Malgun Gothic"
    assert parsed["ea"] == "Malgun Gothic"


def test_western_stack_keeps_generic_fallback():
    """A pure-Latin stack with a generic fallback should still end at
    the generic — the CJK-mirror behaviour only kicks in when an EA
    font won the stack."""
    parsed = parse_font_family("Inter, Roboto, sans-serif")
    assert parsed["latin"] == "Inter"  # explicit pick wins
    parsed = parse_font_family("sans-serif")
    assert parsed["latin"] == "Segoe UI"
