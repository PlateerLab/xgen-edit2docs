"""Inline CSS-style promotion at the Executor boundary.

The LLM sometimes emits ``<text style="font-size:14px;font-weight:700">…</text>``
instead of using the SVG attribute form. Our DrawingML converter only
reads attributes, so the styled run silently renders at the default
16-px regular black. The promoter lifts each declaration into a real
SVG attribute before the converter sees it.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from xgen_edit2docs.tools.execute import _promote_inline_styles


SVG = "http://www.w3.org/2000/svg"


def _attr(svg: str, tag: str, key: str) -> str | None:
    root = ET.fromstring(svg)
    el = next(iter(root.iter(f"{{{SVG}}}{tag}")), None)
    return None if el is None else el.get(key)


def test_font_size_promoted_drops_px_suffix():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text style="font-size:14px">본문</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "font-size") == "14"
    assert _attr(out, "text", "style") is None  # style fully consumed


def test_multiple_declarations_promoted():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text style="font-size:18px;font-weight:700;fill:#0a0a0a">제목</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "font-size") == "18"
    assert _attr(out, "text", "font-weight") == "700"
    assert _attr(out, "text", "fill") == "#0a0a0a"


def test_existing_attribute_wins_over_inline_style():
    """Explicit attributes are authoritative. The promoter only fills
    gaps."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text font-size="20" style="font-size:14px">본문</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "font-size") == "20"


def test_unknown_property_left_in_style():
    """Non-promotable declarations (animations, transforms, custom
    props) stay inside the `style` attribute so the model's intent
    isn't silently dropped."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text style="font-size:14px;animation-name:fade;--brand:#000">제목</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "font-size") == "14"
    style = _attr(out, "text", "style") or ""
    assert "animation-name:fade" in style
    assert "--brand:#000" in style
    assert "font-size" not in style


def test_promotion_walks_nested_groups():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g>'
        '  <g>'
        '    <text style="font-size:24px">depth 2</text>'
        '  </g>'
        '</g>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "font-size") == "24"


def test_no_style_attribute_passes_through_unchanged():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text font-size="20">no inline style</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    # No change. Compare normalised forms (ET may reformat).
    assert "font-size=\"20\"" in out
    assert "style=" not in out


def test_letter_spacing_and_text_anchor_promoted():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text style="letter-spacing:-0.02em;text-anchor:middle">중앙 정렬</text>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "text", "letter-spacing") == "-0.02em"
    assert _attr(out, "text", "text-anchor") == "middle"


def test_malformed_xml_passes_through():
    bad = "<svg><unclosed style='font-size:10px'>"
    assert _promote_inline_styles(bad) == bad


def test_empty_input_passes_through():
    assert _promote_inline_styles("") == ""
    assert _promote_inline_styles("plain text") == "plain text"


def test_opacity_and_strokes_promoted():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect style="opacity:0.5;stroke:#ff0000;stroke-width:2;fill-opacity:0.3" width="10" height="10"/>'
        '</svg>'
    )
    out = _promote_inline_styles(svg)
    assert _attr(out, "rect", "opacity") == "0.5"
    assert _attr(out, "rect", "stroke") == "#ff0000"
    assert _attr(out, "rect", "stroke-width") == "2"
    assert _attr(out, "rect", "fill-opacity") == "0.3"
