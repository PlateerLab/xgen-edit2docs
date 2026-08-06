"""SVG viewBox normalisation at the Executor boundary.

deck_5.pptx failed quality with 9 `viewBox mismatch: expected
'0 0 1280 720', got '0 0 1920 1080'` errors. The DrawingML converter
assumes 1 SVG px = 9525 EMU and doesn't read viewBox, so a 1920×1080
SVG positions every shape past the slide canvas.

The normaliser wraps content in `<g transform="scale(sx, sy)">`
whenever the viewBox is non-canonical but aspect-matching. The
output viewBox / width / height are rewritten to the canonical
canvas. Result: the deck looks identical regardless of whether the
model picked 1280×720, 1600×900, 1920×1080, or any other 16:9
resolution.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from xgen_edit2docs.tools.execute import _normalise_viewbox_to_canonical


SVG = "http://www.w3.org/2000/svg"


def _root(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _attr(svg: str, tag: str, attr: str) -> str | None:
    root = _root(svg)
    el = next(iter(root.iter(f"{{{SVG}}}{tag}")), None)
    return None if el is None else el.get(attr)


# ---------------------------------------------------------------------------
# Pass-through cases (already canonical or unsupported)
# ---------------------------------------------------------------------------


def test_canonical_1280x720_passes_through_unchanged():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720"><rect width="100" height="50"/></svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert out == svg


def test_missing_viewbox_passes_through():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="100" height="50"/></svg>'
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert out == svg


def test_non_169_aspect_passes_through():
    """A 4:3 SVG fed into a 16:9 deck would distort if we rescaled.
    Leave it alone — legacy quality check still catches the
    mismatch."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 768" '
        'width="1024" height="768"><rect width="100" height="50"/></svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert out == svg


def test_malformed_viewbox_passes_through():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="garbage"><rect/></svg>'
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert out == svg


def test_unknown_canvas_format_passes_through():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080"/>'
    out = _normalise_viewbox_to_canonical(svg, "instagram_reel")
    assert out == svg


# ---------------------------------------------------------------------------
# Rescale cases
# ---------------------------------------------------------------------------


def test_1920x1080_wrapped_with_scale_two_thirds():
    """1920×1080 → 1280×720 needs scale(2/3)."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
        'width="1920" height="1080">'
        '<rect width="1920" height="1080" fill="#000"/>'
        '</svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    # ViewBox rewritten.
    assert 'viewBox="0 0 1280 720"' in out
    assert 'width="1280"' in out
    assert 'height="720"' in out
    # Content wrapped in a scale group. 1280/1920 = 0.666666...
    m = re.search(r'transform="[^"]*scale\(([\d.]+)', out)
    assert m is not None, "expected a scale() transform"
    factor = float(m.group(1))
    assert abs(factor - 1280 / 1920) < 0.001


def test_1600x900_wrapped_with_scale_0_8():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900" '
        'width="1600" height="900">'
        '<rect width="1600" height="900" fill="#fff"/>'
        '</svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert 'viewBox="0 0 1280 720"' in out
    m = re.search(r'transform="[^"]*scale\(([\d.]+)', out)
    assert m is not None
    assert abs(float(m.group(1)) - 0.8) < 0.001


def test_content_preserved_inside_wrapper():
    """Visual content is moved INTO the wrapper, not lost."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
        'width="1920" height="1080">'
        '<rect id="bg" width="1920" height="1080" fill="#000"/>'
        '<text id="title" x="100" y="200" font-size="60">제목</text>'
        '</svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    # Original content survives somewhere in the output.
    assert 'id="bg"' in out
    assert 'id="title"' in out
    assert "제목" in out
    # The wrapper carries our marker attribute so we can find it again.
    assert "data-xgen_edit2docs-viewbox-normalise" in out


def test_defs_stay_outside_the_wrapper():
    """`<defs>` is referenced by id; if it gets dragged inside the
    transform, its coordinate space changes and `<use href>` would
    render at the wrong position. Defs must stay at the root."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
        'width="1920" height="1080">'
        '<defs><circle id="dot" cx="0" cy="0" r="5"/></defs>'
        '<rect width="1920" height="1080"/>'
        '</svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    root = _root(out)
    # Defs is a direct child of <svg>, not nested in the wrapper.
    direct_children = [c for c in root]
    defs_idx = next(
        (i for i, c in enumerate(direct_children) if c.tag.endswith("}defs")),
        None,
    )
    g_idx = next(
        (i for i, c in enumerate(direct_children) if c.tag.endswith("}g")),
        None,
    )
    assert defs_idx is not None
    assert g_idx is not None
    # Order: defs before the wrapper group.
    assert defs_idx < g_idx


def test_viewbox_with_offset_translated_then_scaled():
    """`viewBox="40 40 1840 1035"` (non-zero origin) becomes
    `0 0 1280 720` with the wrapper carrying both translate + scale."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="40 40 1920 1080" '
        'width="1920" height="1080">'
        '<rect width="1920" height="1080"/>'
        '</svg>'
    )
    out = _normalise_viewbox_to_canonical(svg, "ppt169")
    assert 'viewBox="0 0 1280 720"' in out
    assert "translate(" in out
    assert "scale(" in out


# ---------------------------------------------------------------------------
# Quality check now accepts any aspect-matching viewBox
# ---------------------------------------------------------------------------


def test_quality_accepts_1920x1080_aspect_match(tmp_path):
    """The legacy strict-string check is replaced with an
    aspect-ratio check. 1920×1080 is the FHD form of 16:9 and must
    pass the legacy `_check_viewbox` once we accept aspect-matches."""
    from xgen_edit2docs.core.svg_quality_checker import SVGQualityChecker

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
        'width="1920" height="1080">'
        '<rect width="1920" height="1080" fill="#fff"/>'
        '</svg>'
    )
    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(svg, encoding="utf-8")
    checker = SVGQualityChecker()
    result = checker.check_file(str(svg_path), expected_format="ppt169")
    viewbox_errors = [e for e in result.get("errors", []) if "viewBox" in e]
    assert viewbox_errors == [], viewbox_errors


def test_quality_rejects_4_3_in_16_9_deck(tmp_path):
    """Aspect mismatch is still an error — we tolerate dimension
    variation, not aspect variation."""
    from xgen_edit2docs.core.svg_quality_checker import SVGQualityChecker

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 768" '
        'width="1024" height="768">'
        '<rect width="1024" height="768" fill="#fff"/>'
        '</svg>'
    )
    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(svg, encoding="utf-8")
    checker = SVGQualityChecker()
    result = checker.check_file(str(svg_path), expected_format="ppt169")
    viewbox_errors = [e for e in result.get("errors", []) if "viewBox" in e]
    assert any("aspect" in e or "mismatch" in e for e in viewbox_errors), viewbox_errors
