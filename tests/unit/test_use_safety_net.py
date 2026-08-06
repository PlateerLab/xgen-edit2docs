"""Tests for the orphan-<use> safety net.

This is the last-resort layer that runs after both expanders. Anything
that survives both `use_expander` (data-icon) and `use_href_expander`
(standard href) becomes an unrenderable element that would otherwise
crash the entire deck conversion. The safety net replaces each leftover
<use> with an empty <g/> so the slide still builds.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from xgen_edit2docs.core.svg_to_pptx.use_safety_net import strip_orphan_uses

SVG = "http://www.w3.org/2000/svg"


def _parse(src: str) -> ET.Element:
    return ET.fromstring(src)


def test_orphan_use_replaced_with_empty_g():
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <g>
        <use data-icon="lucide/star" fill="#fff"/>
      </g>
    </svg>
    """
    root = _parse(src)
    count = strip_orphan_uses(root)
    assert count == 1
    # No <use> remains.
    assert list(root.iter(f"{{{SVG}}}use")) == []
    # The position is preserved as an empty <g/>.
    groups = list(root.iter(f"{{{SVG}}}g"))
    # The outer <g> plus one new empty <g> placeholder = 2 groups.
    assert len(groups) == 2
    # The inner placeholder has no children.
    inner = [g for g in groups if g.find(f"{{{SVG}}}*") is None]
    assert any(len(list(g)) == 0 for g in inner)


def test_id_is_preserved_on_placeholder():
    """When the <use> carried an id, the placeholder keeps it so any
    animation/timing references still resolve to *something*."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <use id="anim_target_07" data-icon="lucide/star"/>
    </svg>
    """
    root = _parse(src)
    strip_orphan_uses(root)
    placeholders = [el for el in root if el.tag == f"{{{SVG}}}g"]
    assert placeholders
    assert placeholders[0].get("id") == "anim_target_07"


def test_multiple_orphans_all_replaced():
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <use href="#missing-1"/>
      <g><use data-icon="missing/icon"/></g>
      <g><use xlink:href="#missing-2" xmlns:xlink="http://www.w3.org/1999/xlink"/></g>
    </svg>
    """
    root = _parse(src)
    count = strip_orphan_uses(root)
    assert count == 3
    assert list(root.iter(f"{{{SVG}}}use")) == []


def test_no_use_elements_is_a_noop():
    src = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    root = _parse(src)
    count = strip_orphan_uses(root)
    assert count == 0


def test_converter_no_longer_raises_on_unresolvable_use(tmp_path):
    """End-to-end: the DrawingML converter must accept an SVG with an
    unresolvable <use> now that the safety net runs before the
    unsupported-element check. Regression for the production failure
    where a single bad <use> on slide_00 crashed the whole deck."""
    from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
        convert_svg_to_slide_shapes,
    )

    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
      <rect width="1280" height="720" fill="#0A0E14"/>
      <g>
        <use href="#does-not-exist"/>
      </g>
      <g>
        <use data-icon="library-not-installed/icon"/>
      </g>
      <text x="100" y="100" font-size="40" fill="white">제목</text>
    </svg>
    """
    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(svg, encoding="utf-8")

    # Must not raise SvgNativeConversionError.
    slide_xml, media, rels, anim, _pkg, _cto = convert_svg_to_slide_shapes(svg_path)
    assert "<p:sp" in slide_xml
    # The text element survived.
    assert "제목" in slide_xml
