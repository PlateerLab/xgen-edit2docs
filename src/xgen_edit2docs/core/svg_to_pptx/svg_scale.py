"""Rescale an SVG's coordinate space to a target viewBox.

The DrawingML converter assumes 1 SVG px = 9525 EMU, so a slide SVG must
be authored in the exact pixel dimensions of the deck it lands in. Two
callers need to re-map coordinates:

* the Executor boundary normalises model output (1920x1080 et al.) down
  to the canonical canvas (`tools/execute.py`), and
* the template export path scales canonical-canvas SVGs to the host
  deck's real dimensions (e.g. a legacy 4:3 deck at 960x720 px) before
  appending slides into a user-provided PPTX.

Both wrap the SVG's visual children in a `<g transform="scale(...)">`
and rewrite the viewport, keeping `<defs>` outside the transform so
`<use href>` consumers keep resolving original coordinates.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"


def scale_svg_to_viewbox(
    svg: str,
    target_w: float,
    target_h: float,
    *,
    aspect_tolerance: float = 0.01,
) -> str:
    """Rewrite *svg* so its viewBox is ``0 0 target_w target_h``.

    Behaviour:
      * Exact-match viewBox: pass-through.
      * Aspect-matching viewBox (within *aspect_tolerance*): wrap + rewrite.
      * Different aspect: pass-through — rescaling would distort the deck;
        the quality check downstream surfaces the mismatch.
      * Missing viewBox / parse failure / degenerate input: pass-through.
    """
    if not svg or "<svg" not in svg or target_w <= 0 or target_h <= 0:
        return svg
    target_ratio = target_w / target_h

    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return svg

    vb = root.get("viewBox")
    if not vb:
        return svg
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        return svg
    try:
        vb_x, vb_y, vb_w, vb_h = (float(p) for p in parts)
    except ValueError:
        return svg
    if vb_w <= 0 or vb_h <= 0:
        return svg

    # Already at the target — nothing to do.
    if (vb_x, vb_y) == (0.0, 0.0) and abs(vb_w - target_w) < 0.5 and abs(vb_h - target_h) < 0.5:
        return svg

    actual_ratio = vb_w / vb_h
    if abs(actual_ratio - target_ratio) > aspect_tolerance * target_ratio:
        return svg

    sx = target_w / vb_w
    sy = target_h / vb_h
    # Move the original viewport origin into the transform too so the
    # rewritten viewBox starts at (0, 0).
    tx = -vb_x * sx
    ty = -vb_y * sy

    wrapper = ET.Element(f"{{{SVG_NS}}}g")
    transform_pieces = []
    if tx != 0 or ty != 0:
        transform_pieces.append(f"translate({tx:g}, {ty:g})")
    transform_pieces.append(f"scale({sx:g}, {sy:g})")
    wrapper.set("transform", " ".join(transform_pieces))
    wrapper.set("data-xgen_edit2docs-viewbox-normalise", "1")

    # Move every root child (except <defs> — references stay in place)
    # under the wrapper.
    children = list(root)
    defs_children = [c for c in children if c.tag.split("}", 1)[-1] == "defs"]
    visual_children = [c for c in children if c.tag.split("}", 1)[-1] != "defs"]
    for c in children:
        root.remove(c)
    for d in defs_children:
        root.append(d)
    for v in visual_children:
        wrapper.append(v)
    root.append(wrapper)

    root.set("viewBox", f"0 0 {target_w:g} {target_h:g}")
    if root.get("width") is not None:
        root.set("width", f"{target_w:g}")
    if root.get("height") is not None:
        root.set("height", f"{target_h:g}")

    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode")
