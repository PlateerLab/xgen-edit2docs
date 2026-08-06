"""Auto-id on top-level <g> children of the SVG root.

The quality checker warns on every anonymous top-level visual group;
production decks routinely accumulated a dozen such warnings. The
converter now backfills `id="auto_grp_NN"` on every top-level <g> that
lacks one, so the warning column stays clean and any animation /
timing reference can resolve the group.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
    _autogen_top_level_group_ids,
)

SVG = "http://www.w3.org/2000/svg"


def _parse(src: str) -> ET.Element:
    return ET.fromstring(src)


def test_anonymous_groups_get_ids():
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g><rect width="10" height="10"/></g>'
        '<g><circle r="5"/></g>'
        '<g><path d="M 0 0 L 10 10"/></g>'
        '</svg>'
    )
    root = _parse(src)
    count = _autogen_top_level_group_ids(root)
    assert count == 3
    ids = [g.get("id") for g in root if g.tag == f"{{{SVG}}}g"]
    assert ids == ["auto_grp_01", "auto_grp_02", "auto_grp_03"]


def test_existing_ids_preserved():
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="hero"><rect/></g>'
        '<g><circle/></g>'
        '<g id="footer"><line/></g>'
        '</svg>'
    )
    root = _parse(src)
    count = _autogen_top_level_group_ids(root)
    assert count == 1  # only the anonymous group
    ids = [g.get("id") for g in root if g.tag == f"{{{SVG}}}g"]
    assert ids == ["hero", "auto_grp_01", "footer"]


def test_auto_id_avoids_collision_with_existing():
    """If `auto_grp_01` is already taken, the generator skips ahead."""
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="auto_grp_01"><rect/></g>'
        '<g><circle/></g>'  # would have been auto_grp_01 naively
        '</svg>'
    )
    root = _parse(src)
    _autogen_top_level_group_ids(root)
    ids = [g.get("id") for g in root if g.tag == f"{{{SVG}}}g"]
    assert ids[0] == "auto_grp_01"
    assert ids[1] == "auto_grp_02"
    assert ids[1] != "auto_grp_01"


def test_non_group_top_level_children_ignored():
    """The auto-id is only for `<g>` — `<rect>` / `<path>` at root stay
    untouched."""
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect width="10" height="10"/>'
        '<g><circle/></g>'
        '<path d="M 0 0"/>'
        '</svg>'
    )
    root = _parse(src)
    count = _autogen_top_level_group_ids(root)
    assert count == 1
    rect = root.find(f"{{{SVG}}}rect")
    path = root.find(f"{{{SVG}}}path")
    assert rect.get("id") is None
    assert path.get("id") is None


def test_nested_groups_not_touched():
    """Only the IMMEDIATE children of <svg> matter — nested groups stay
    anonymous (the warning is about top-level animation references)."""
    src = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g>'
        '  <g><rect/></g>'  # nested, no id needed
        '</g>'
        '</svg>'
    )
    root = _parse(src)
    _autogen_top_level_group_ids(root)
    outer = list(root)[0]
    inner = list(outer)[0]
    assert outer.get("id") == "auto_grp_01"
    assert inner.get("id") is None
