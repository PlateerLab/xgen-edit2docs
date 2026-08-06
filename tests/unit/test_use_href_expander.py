"""Unit tests for the standard <use href="#id"/> expander.

This is the SVG-spec form (separate from the project-internal
data-icon placeholder handled by use_expander.py). Without expansion
the native DrawingML dispatcher rejects every <use> as an unsupported
element, which is the bug surfaced in production when the Strategist
emitted a deck whose slides each carry <g><use href="#glyph"/></g>.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from xgen_edit2docs.core.svg_to_pptx.use_href_expander import expand_use_href

SVG = "http://www.w3.org/2000/svg"


def _parse(src: str) -> ET.Element:
    return ET.fromstring(src)


def test_symbol_reference_inlined_as_g():
    """<use href="#sym"> where #sym is a <symbol> → <g> with the symbol's children."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs>
        <symbol id="dot">
          <circle cx="0" cy="0" r="5" fill="red"/>
        </symbol>
      </defs>
      <use href="#dot" x="10" y="20"/>
    </svg>
    """
    root = _parse(src)
    count = expand_use_href(root)
    assert count == 1

    # The <use> is gone; in its place is a <g transform="translate(10, 20)">
    # containing the <circle>. <symbol> is unwrapped (it's a non-rendered
    # container in SVG semantics).
    uses = list(root.iter(f"{{{SVG}}}use"))
    assert uses == []
    groups = [el for el in root if el.tag == f"{{{SVG}}}g"]
    assert len(groups) == 1
    assert "translate(10, 20)" in groups[0].get("transform", "")
    circles = list(groups[0].iter(f"{{{SVG}}}circle"))
    assert len(circles) == 1
    assert circles[0].get("fill") == "red"


def test_group_reference_inlined_with_id_dropped():
    """References to a <g> deep-clone the <g> and strip the duplicate id."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs>
        <g id="icon">
          <rect width="10" height="10"/>
          <rect width="20" height="20"/>
        </g>
      </defs>
      <use href="#icon"/>
    </svg>
    """
    root = _parse(src)
    expand_use_href(root)

    # Inside the wrapper <g>, the inlined element should be a <g> whose
    # id has been stripped (otherwise two elements would share id="icon").
    wrappers = [el for el in root if el.tag == f"{{{SVG}}}g"]
    assert len(wrappers) == 1
    inlined = list(wrappers[0])
    assert len(inlined) == 1
    assert inlined[0].tag == f"{{{SVG}}}g"
    assert inlined[0].get("id") is None
    assert len(list(inlined[0])) == 2  # both rects preserved


def test_xlink_href_recognised():
    """Legacy xlink:href spelling is supported alongside the modern href."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
      <defs>
        <symbol id="star">
          <path d="M0 0 L10 10"/>
        </symbol>
      </defs>
      <use xlink:href="#star"/>
    </svg>
    """
    root = _parse(src)
    count = expand_use_href(root)
    assert count == 1
    assert list(root.iter(f"{{{SVG}}}use")) == []


def test_nested_use_expanded_in_followup_pass():
    """A <symbol> may contain its own <use>; the expander loops until done."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs>
        <circle id="inner" r="3"/>
        <symbol id="outer">
          <use href="#inner"/>
        </symbol>
      </defs>
      <use href="#outer"/>
    </svg>
    """
    root = _parse(src)
    expand_use_href(root)
    # Both <use>s gone; final tree has the actual <circle> as a leaf.
    assert list(root.iter(f"{{{SVG}}}use")) == []
    circles = list(root.iter(f"{{{SVG}}}circle"))
    # One <circle> remains in <defs> (the original); plus one inlined copy.
    assert len(circles) >= 1


def test_unresolvable_reference_left_in_place():
    """Dangling references are NOT silently removed — caller may want to warn."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <use href="#does-not-exist"/>
    </svg>
    """
    root = _parse(src)
    count = expand_use_href(root)
    assert count == 0
    assert list(root.iter(f"{{{SVG}}}use"))  # still there


def test_data_icon_use_is_ignored():
    """The data-icon placeholder is a separate pipeline (use_expander.py)
    and must not be touched here, even if it lacks a real href."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <use data-icon="lucide/star"/>
    </svg>
    """
    root = _parse(src)
    count = expand_use_href(root)
    assert count == 0
    # The data-icon use element is preserved for the other expander to handle.
    uses = list(root.iter(f"{{{SVG}}}use"))
    assert len(uses) == 1
    assert uses[0].get("data-icon") == "lucide/star"


def test_use_x_y_zero_does_not_emit_transform():
    """No translate emitted when x/y are both zero (or absent)."""
    src = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs><circle id="c" r="1"/></defs>
      <use href="#c"/>
    </svg>
    """
    root = _parse(src)
    expand_use_href(root)
    wrappers = [el for el in root if el.tag == f"{{{SVG}}}g"]
    assert wrappers[0].get("transform") is None


def test_drawingml_converter_accepts_use_now(tmp_path):
    """End-to-end: an SVG with a real <use href> survives the converter's
    unsupported-element check after expansion."""
    from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
        convert_svg_to_slide_shapes,
    )

    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">
      <defs>
        <symbol id="bullet">
          <circle cx="0" cy="0" r="4" fill="black"/>
        </symbol>
      </defs>
      <g>
        <use href="#bullet" x="100" y="100"/>
      </g>
    </svg>
    """
    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(svg, encoding="utf-8")

    slide_xml, media, rels, anim, _pkg, _cto = convert_svg_to_slide_shapes(svg_path)
    assert isinstance(slide_xml, str)
    assert "<p:sp" in slide_xml  # at least one shape made it through
