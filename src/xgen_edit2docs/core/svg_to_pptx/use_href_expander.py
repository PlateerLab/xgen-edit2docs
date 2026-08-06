"""In-memory expansion of standard SVG ``<use href="#id"/>`` references.

Distinct from ``use_expander.py``, which handles the project-internal
``<use data-icon="lib/name">`` placeholder. This module deals with the
plain SVG spec form where a ``<use>`` element references another element
(``<symbol>`` / ``<g>`` / shape) by id and the renderer is expected to
clone that subtree in place.

The native DrawingML dispatcher does not know how to follow ``<use>``,
so an unexpanded reference is rejected as "unsupported visual SVG
element" at conversion time. LLM-emitted SVGs frequently rely on this
pattern (e.g. defining a logo or bullet glyph once in ``<defs>`` and
referencing it from each slide), so we inline the reference before
dispatch.

Spec compliance is intentionally narrow:
- Only ``#fragment`` references (no external file URLs).
- ``x`` / ``y`` on the ``<use>`` become a ``translate(x, y)`` prepended
  to the inlined element's own ``transform`` (matches SVG semantics for
  the common case; we don't try to honour viewBox-based ``<symbol>``
  width/height scaling because that's rare in slide SVGs).
- ``href`` and the legacy ``xlink:href`` are both recognised.
- Cycle-safe: a small recursion budget prevents pathological self-
  references from blowing the stack.

Returns the number of replacements made so callers can log a count.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

_MAX_NESTED_EXPANSIONS = 16


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _href_target(use_elem: ET.Element) -> str | None:
    """Return the fragment id referenced by a <use>, or None."""
    href = use_elem.get("href") or use_elem.get(f"{{{XLINK_NS}}}href")
    if not href or not href.startswith("#"):
        return None
    return href[1:] or None


def _build_id_index(root: ET.Element) -> dict[str, ET.Element]:
    """Walk the whole tree and index every element that carries an id.

    Standard SVG resolution is global by id; references aren't limited to
    ``<defs>``. We mirror that.
    """
    index: dict[str, ET.Element] = {}
    for elem in root.iter():
        elem_id = elem.get("id")
        if elem_id and elem_id not in index:
            index[elem_id] = elem
    return index


def _clone_for_inline(target: ET.Element, use_elem: ET.Element) -> ET.Element:
    """Deep-copy *target* and wrap it in a <g> that absorbs the <use>'s
    positional attributes plus any presentation attributes it carries.

    ``<symbol>`` elements are unwrapped — their children become the <g>'s
    children directly, matching SVG rendering semantics.
    """
    import copy

    g = ET.Element(f"{{{SVG_NS}}}g")

    # Translate from x/y on the <use>. SVG default is 0/0.
    x = use_elem.get("x", "0")
    y = use_elem.get("y", "0")
    translate = None
    if x not in ("0", "", None) or y not in ("0", "", None):
        translate = f"translate({x or 0}, {y or 0})"

    use_transform = use_elem.get("transform")
    pieces = [p for p in (translate, use_transform) if p]
    if pieces:
        g.set("transform", " ".join(pieces))

    # Forward presentation hints that often live on <use> (fill, stroke,
    # opacity, class, style). The cloned target keeps its own attrs;
    # inherited attrs on the wrapper are the SVG-spec way to push styling
    # down without rewriting every descendant.
    for attr in ("fill", "stroke", "stroke-width", "opacity", "class", "style"):
        val = use_elem.get(attr)
        if val is not None and g.get(attr) is None:
            g.set(attr, val)

    if _local(target.tag) == "symbol":
        # <symbol> is a non-rendered container — emit its children only.
        for child in target:
            g.append(copy.deepcopy(child))
    else:
        clone = copy.deepcopy(target)
        # Drop the id from the clone so we don't end up with duplicate ids
        # in the document after inlining.
        if "id" in clone.attrib:
            del clone.attrib["id"]
        g.append(clone)

    return g


def expand_use_href(root: ET.Element) -> int:
    """Replace every resolvable ``<use href="#id"/>`` in *root* with the
    inlined target subtree.

    Returns the number of substitutions made. References whose target
    cannot be located are left in place so the caller's unsupported-
    element check still surfaces them (callers can decide whether to
    warn or fail).
    """
    parent_of: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    id_index = _build_id_index(root)

    expanded = 0
    # Run multiple passes because a <symbol> may itself contain <use>
    # references that need expanding too. Hard-cap the loop count to
    # protect against pathological cycles.
    for _ in range(_MAX_NESTED_EXPANSIONS):
        targets: list[ET.Element] = []
        for elem in root.iter():
            if _local(elem.tag) != "use":
                continue
            if elem.get("data-icon"):
                # data-icon placeholders are handled by use_expander.py.
                continue
            if _href_target(elem):
                targets.append(elem)
        if not targets:
            break

        any_progress = False
        for use_elem in targets:
            ref_id = _href_target(use_elem)
            if ref_id is None:
                continue
            target = id_index.get(ref_id)
            if target is None:
                continue
            parent = parent_of.get(use_elem)
            if parent is None:
                continue
            replacement = _clone_for_inline(target, use_elem)
            idx = list(parent).index(use_elem)
            parent.remove(use_elem)
            parent.insert(idx, replacement)
            # Refresh parent_of / id_index for the new subtree so the
            # next pass can resolve any <use> nested inside.
            for sub in replacement.iter():
                for sub_child in sub:
                    parent_of[sub_child] = sub
                if sub.get("id") and sub.get("id") not in id_index:
                    id_index[sub.get("id")] = sub
            expanded += 1
            any_progress = True

        if not any_progress:
            break

    return expanded
