"""Last-resort fallback for ``<use>`` elements that survived every
expansion pass.

The DrawingML dispatcher does not understand ``<use>``. Two upstream
modules try to inline references first:

* :mod:`use_expander` resolves the project-internal
  ``<use data-icon="lib/name">`` placeholder when the icon library is
  present and the named icon exists.
* :mod:`use_href_expander` resolves the SVG-spec
  ``<use href="#id"/>`` form when the referenced id is in the same
  document.

Anything still left after both runs is either a typo (icon name that
doesn't exist), a stylesheet artifact, or a corrupted reference. The
right behaviour in production is **not** to crash the entire deck
because one slide had an unknown icon — the operator would much rather
see a deck with a single missing glyph than no deck at all. This
module rewrites every remaining ``<use>`` into a no-op ``<g/>``,
keeping layout structure intact while letting the converter proceed.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def strip_orphan_uses(root: ET.Element) -> int:
    """Replace every remaining ``<use>`` in *root* with an empty ``<g>``.

    Returns the number of substitutions made so callers can log a
    warning. The replacement carries no children — the icon is silently
    dropped — but preserves the element's position so sibling layout
    isn't disturbed.
    """
    parent_of: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    orphans: list[ET.Element] = []
    for elem in root.iter():
        if _local(elem.tag) == "use":
            orphans.append(elem)

    replaced = 0
    for use_elem in orphans:
        parent = parent_of.get(use_elem)
        if parent is None:
            continue
        placeholder = ET.Element(f"{{{SVG_NS}}}g")
        # Preserve `id` so any animation/timing reference can still find
        # something at this position; the converter will treat it as an
        # empty group and produce no shapes.
        elem_id = use_elem.get("id")
        if elem_id:
            placeholder.set("id", elem_id)
        idx = list(parent).index(use_elem)
        parent.remove(use_elem)
        parent.insert(idx, placeholder)
        replaced += 1

    return replaced
