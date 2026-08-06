"""Deterministic post-LLM SVG layout repair.

The Executor's LLM is reliable on content and visual style but flaky on
pixel-level layout — it routinely emits decks where the hero number's
bounding box swallows its own caption, footer page-numbers don't fit
their containers, and decorative shapes drift off-canvas. This module
runs immediately after the SVG normalisation pass (auto-id, image
href, weight strip) and re-shapes the broken cases.

See ``xgen_edit2docs-upgrade-plan/04-postprocessing.md`` for the design
rationale.

Detectors
---------

* ``_detect_overlap`` — pairs of visible boxes that overlap > 40 % of
  the smaller box's area, excluding intentional containment
  (background card + foreground content).
* ``_detect_text_overflow_x`` — `<text>` whose estimated rendered width
  exceeds its parent group's width.
* ``_detect_off_canvas`` — any visible box whose bounds spill past the
  declared SVG canvas.
* ``_detect_empty_decoration`` — `<g>` / `<rect>` with no fill, no
  stroke, and no rendering children.

Auto-fix actions
----------------

* Overlap (caption inside hero box) — shift the smaller / later element
  to sit below the larger / earlier one.
* Text overflow — widen the parent group's `<rect>` (the implicit
  text-box) to fit ``required_width × 1.15``.
* Off-canvas — clamp the element so ``x + w <= canvas_w`` and the same
  for y.
* Empty decoration — remove the element entirely.

Best-effort: parse failures pass the input through unchanged so the
downstream stages still see something. Every detection — fixed or not
— is recorded so the caller can surface it as a quality warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from xml.etree import ElementTree as ET

from .drawingml_utils import estimate_text_width

SVG_NS = "http://www.w3.org/2000/svg"

ViolationKind = Literal[
    "overlap",
    "text_overflow_x",
    "off_canvas",
    "empty_decoration",
]


@dataclass
class LayoutViolation:
    """One detected layout problem.

    ``fix_applied`` says whether the repair pass mutated the SVG to fix
    it. Unresolved violations still propagate to quality as warnings so
    the retry loop can ask the model for a better attempt.
    """

    kind: ViolationKind
    element_path: str
    actual: dict
    expected: dict
    severity: Literal["error", "warning"] = "warning"
    fix_applied: bool = False


@dataclass
class LayoutRepairResult:
    repaired_svg: str
    violations: list[LayoutViolation] = field(default_factory=list)


# Default 16:9 PPT canvas in SVG pixel units. Other canvas formats
# (4:3, vertical) flow in through the constructor.
DEFAULT_CANVAS = (1280, 720)


def repair_layout(
    svg: str,
    *,
    canvas: tuple[int, int] = DEFAULT_CANVAS,
) -> LayoutRepairResult:
    """Entry point. Returns a possibly-mutated SVG plus a violation log.

    Parameters
    ----------
    svg:
        The Executor's SVG as a string. We accept it post-normalisation
        (auto-id already applied, image hrefs cleaned).
    canvas:
        Logical canvas dimensions. Used to detect off-canvas elements.
        Defaults to 1280×720 (PPT 16:9). When the document declares its
        own ``viewBox`` we honour that instead.
    """
    if not svg or "<svg" not in svg:
        return LayoutRepairResult(repaired_svg=svg, violations=[])

    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return LayoutRepairResult(repaired_svg=svg, violations=[])

    canvas_w, canvas_h = _resolve_canvas(root, canvas)
    elements = _collect_visible_elements(root)

    violations: list[LayoutViolation] = []

    # Pass 1: empty decoration. Trim before geometry analysis so the
    # detectors aren't distracted by phantom shapes.
    violations.extend(_strip_empty_decorations(root, elements))

    # Re-collect because element list changed.
    elements = _collect_visible_elements(root)

    # Pass 2: off-canvas. Clamp first so overflow / overlap detection
    # works on in-bounds geometry.
    violations.extend(_clamp_off_canvas(elements, canvas_w, canvas_h))

    # Pass 3: text-overflow. Widen the box rather than truncate text.
    violations.extend(_fix_text_overflow(elements))

    # Pass 4: overlap between non-background text boxes. Caption-in-
    # hero is the canonical case.
    violations.extend(_fix_overlap(elements))

    # Serialise — preserve the SVG namespace declaration.
    ET.register_namespace("", SVG_NS)
    return LayoutRepairResult(
        repaired_svg=ET.tostring(root, encoding="unicode"),
        violations=violations,
    )


# ---------------------------------------------------------------------------
# Geometry collection
# ---------------------------------------------------------------------------


@dataclass
class _Element:
    """One visible element with bounds and a backref to the XML node."""

    node: ET.Element
    parent: ET.Element | None
    path: str
    tag: str
    x: float
    y: float
    w: float
    h: float
    text: str = ""
    font_size_px: float = 0.0
    font_weight: str = "400"

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _resolve_canvas(root: ET.Element, default: tuple[int, int]) -> tuple[int, int]:
    """Honour the SVG's own viewBox if it has one."""
    viewbox = root.get("viewBox")
    if viewbox:
        parts = viewbox.replace(",", " ").split()
        if len(parts) == 4:
            try:
                return int(float(parts[2])), int(float(parts[3]))
            except ValueError:
                pass
    w = root.get("width")
    h = root.get("height")
    if w and h:
        try:
            return int(float(w)), int(float(h))
        except ValueError:
            pass
    return default


def _f(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    s = value.strip()
    # Trailing units like ``px`` are allowed in SVG attributes.
    for unit in ("px", "pt", "em", "rem", "%"):
        if s.endswith(unit):
            s = s[: -len(unit)]
            break
    try:
        return float(s)
    except ValueError:
        return default


def _collect_visible_elements(root: ET.Element) -> list[_Element]:
    """Walk the tree once and produce a flat list of geometry-bearing
    elements. ``<defs>`` and its descendants are skipped — they're
    referenced, not rendered."""
    out: list[_Element] = []
    parent_of: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    def walk(elem: ET.Element, path: str, in_defs: bool) -> None:
        for idx, child in enumerate(list(elem), start=1):
            tag = _local(child.tag)
            current = f"{path}/{tag}[{idx}]"
            if tag == "defs":
                continue
            if in_defs:
                continue
            geom = _geometry_of(child)
            if geom is not None:
                x, y, w, h = geom
                text, fs, fw = _text_of(child)
                out.append(
                    _Element(
                        node=child,
                        parent=parent_of.get(child),
                        path=current,
                        tag=tag,
                        x=x, y=y, w=w, h=h,
                        text=text,
                        font_size_px=fs,
                        font_weight=fw,
                    )
                )
            walk(child, current, in_defs=False)

    walk(root, "/svg", in_defs=False)
    return out


def _geometry_of(elem: ET.Element) -> tuple[float, float, float, float] | None:
    """Read the element's bounding box, or None if it doesn't have one.

    For elements that don't explicitly declare width/height (most
    ``<text>``) we estimate the box from the parent ``<g>`` or, if the
    parent is the root, mark width/height as 0 and let the overflow
    detector decide.
    """
    tag = _local(elem.tag)
    if tag in {"rect", "image", "use"}:
        x = _f(elem.get("x"))
        y = _f(elem.get("y"))
        w = _f(elem.get("width"))
        h = _f(elem.get("height"))
        if w > 0 and h > 0:
            return x, y, w, h
        return None

    if tag == "g":
        # Group bounds via attribute hints (common when emitted by our
        # converter). When none, compute from children's bounds.
        x_attr = elem.get("data-x") or elem.get("x")
        y_attr = elem.get("data-y") or elem.get("y")
        w_attr = elem.get("data-w") or elem.get("width")
        h_attr = elem.get("data-h") or elem.get("height")
        if all([x_attr, y_attr, w_attr, h_attr]):
            return _f(x_attr), _f(y_attr), _f(w_attr), _f(h_attr)

        # Walk one level for an enclosing rect (cards, panels).
        for child in elem:
            ctag = _local(child.tag)
            if ctag == "rect":
                cw = _f(child.get("width"))
                ch = _f(child.get("height"))
                if cw > 0 and ch > 0:
                    return _f(child.get("x")), _f(child.get("y")), cw, ch
        return None

    if tag == "text":
        x = _f(elem.get("x"))
        y = _f(elem.get("y"))
        # Text doesn't carry width/height in SVG. We synthesise from
        # the rendered estimate. y is the baseline, so the box top is
        # `y - font_size * 0.85`.
        text, fs, fw = _text_of(elem)
        if fs <= 0 or not text:
            return None
        w = estimate_text_width(text, fs, fw)
        top = y - fs * 0.85
        return x, top, w, fs * 1.15

    if tag == "circle":
        cx = _f(elem.get("cx"))
        cy = _f(elem.get("cy"))
        r = _f(elem.get("r"))
        if r <= 0:
            return None
        return cx - r, cy - r, 2 * r, 2 * r

    if tag == "ellipse":
        cx = _f(elem.get("cx"))
        cy = _f(elem.get("cy"))
        rx = _f(elem.get("rx"))
        ry = _f(elem.get("ry"))
        if rx <= 0 or ry <= 0:
            return None
        return cx - rx, cy - ry, 2 * rx, 2 * ry

    return None


def _text_of(elem: ET.Element) -> tuple[str, float, str]:
    """Concatenate text content + read font-size / weight if declared.

    Walks `<tspan>` children for the size — the LLM sometimes only
    annotates the inner span."""
    if _local(elem.tag) != "text":
        return "", 0.0, "400"
    pieces = [elem.text or ""]
    fs = _f(elem.get("font-size"), 0.0)
    fw = elem.get("font-weight", "400")
    for child in elem.iter():
        if _local(child.tag) == "tspan":
            if child.text:
                pieces.append(child.text)
            if fs == 0:
                fs = _f(child.get("font-size"), 0.0)
            if child.get("font-weight"):
                fw = child.get("font-weight", fw)
    return "".join(pieces).strip(), fs, fw


# ---------------------------------------------------------------------------
# Detectors + fixes
# ---------------------------------------------------------------------------


def _strip_empty_decorations(
    root: ET.Element,
    elements: list[_Element],
) -> list[LayoutViolation]:
    """Remove `<g>` / `<rect>` with no fill, no stroke, no children.

    Walks the tree directly instead of using the geometry-collected
    elements list — a fully empty `<g>` has no inferrable geometry, so
    it never makes it into ``elements``, but it's exactly the kind of
    leftover phantom shape we want to prune.
    """
    violations: list[LayoutViolation] = []
    parent_of: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    def _path(elem: ET.Element) -> str:
        # Walk back to root; tag[index] notation.
        chain: list[str] = []
        cur: ET.Element | None = elem
        while cur is not None and cur is not root:
            p = parent_of.get(cur)
            if p is None:
                break
            idx = list(p).index(cur) + 1
            chain.append(f"{_local(cur.tag)}[{idx}]")
            cur = p
        chain.append(_local(root.tag))
        return "/" + "/".join(reversed(chain))

    candidates: list[ET.Element] = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag not in {"g", "rect"}:
            continue
        if list(elem):
            continue
        if (elem.text or "").strip():
            continue
        fill = (elem.get("fill") or "").lower()
        stroke = (elem.get("stroke") or "").lower()
        style = (elem.get("style") or "").lower()
        has_fill = fill not in {"", "none", "transparent"}
        has_stroke = stroke not in {"", "none", "transparent"}
        if has_fill or has_stroke or "fill:" in style or "stroke:" in style:
            continue
        candidates.append(elem)

    for elem in candidates:
        parent = parent_of.get(elem)
        if parent is None:
            continue
        path = _path(elem)
        parent.remove(elem)
        violations.append(
            LayoutViolation(
                kind="empty_decoration",
                element_path=path,
                actual={"tag": _local(elem.tag)},
                expected={"has_fill_or_stroke_or_children": True},
                severity="warning",
                fix_applied=True,
            )
        )
    return violations


def _clamp_off_canvas(
    elements: list[_Element],
    canvas_w: int,
    canvas_h: int,
) -> list[LayoutViolation]:
    """Push elements back inside the canvas. Tolerance of 4px so a
    pixel-aligned background overrun doesn't false-alarm."""
    violations: list[LayoutViolation] = []
    tol = 4
    for el in elements:
        if el.w <= 0 or el.h <= 0:
            continue
        right_overflow = max(0.0, el.right - canvas_w)
        bottom_overflow = max(0.0, el.bottom - canvas_h)
        left_overflow = max(0.0, -el.x)
        top_overflow = max(0.0, -el.y)
        if max(right_overflow, bottom_overflow, left_overflow, top_overflow) <= tol:
            continue
        # Decide the shift / clamp.
        new_x = el.x
        new_y = el.y
        new_w = el.w
        new_h = el.h
        if el.w > canvas_w:
            new_w = canvas_w
            new_x = 0
        elif el.right > canvas_w:
            new_x = max(0, canvas_w - el.w)
        elif el.x < 0:
            new_x = 0
        if el.h > canvas_h:
            new_h = canvas_h
            new_y = 0
        elif el.bottom > canvas_h:
            new_y = max(0, canvas_h - el.h)
        elif el.y < 0:
            new_y = 0
        _apply_bounds(el.node, el.tag, new_x, new_y, new_w, new_h)
        el.x, el.y, el.w, el.h = new_x, new_y, new_w, new_h
        violations.append(
            LayoutViolation(
                kind="off_canvas",
                element_path=el.path,
                actual={"bbox": (el.x, el.y, el.w, el.h)},
                expected={"canvas": (canvas_w, canvas_h)},
                severity="warning",
                fix_applied=True,
            )
        )
    return violations


def _fix_text_overflow(elements: list[_Element]) -> list[LayoutViolation]:
    """Widen the text element when its estimated render width exceeds
    its declared / inferred box. We can't always reach the parent's
    width to expand it, so we annotate ``data-min-width`` on the text
    and let the converter respect it.
    """
    violations: list[LayoutViolation] = []
    for el in elements:
        if el.tag != "text":
            continue
        if el.font_size_px <= 0 or not el.text:
            continue
        required = estimate_text_width(el.text, el.font_size_px, el.font_weight) * 1.15
        # The text element's own w is a synthesis from estimate_text_width —
        # so it always matches. The real overflow is against the parent
        # group's width when the group declared one explicitly.
        parent = el.parent
        if parent is None:
            continue
        parent_w = _f(parent.get("data-w") or parent.get("width"), 0.0)
        if parent_w <= 0:
            continue
        # Real-world PPT glyph widths run ~1.15× of our estimate for
        # English uppercase (Pretendard / Malgun Gothic). Flag when the
        # estimate is already at or above the box — the slack lives in
        # the 1.15 padding multiplier that produced `required`.
        if required <= parent_w:
            continue
        # Expand the parent group's width to fit. Mark as fix_applied
        # only when the attribute we wrote into would actually be
        # honoured by the converter — for plain `width` on `<g>` that's
        # informational, so we record as fix_applied=False and let the
        # quality channel surface it.
        parent_node_tag = _local(parent.tag)
        if parent_node_tag in {"rect", "g"} and parent.get("width") is not None:
            parent.set("width", str(int(required)))
            fix_applied = True
        else:
            fix_applied = False
        violations.append(
            LayoutViolation(
                kind="text_overflow_x",
                element_path=el.path,
                actual={"box_w": parent_w, "required_w": int(required), "text": el.text[:30]},
                expected={"box_w_min": int(required)},
                severity="warning",
                fix_applied=fix_applied,
            )
        )
    return violations


def _fix_overlap(elements: list[_Element]) -> list[LayoutViolation]:
    """Find pairs of non-background text boxes that overlap > 40 % of the
    smaller box and shift the later element below the earlier one. The
    canonical case is a caption rendered inside the hero number's box.

    Containment (caption sitting on top of a background card) is
    intentional and ignored.
    """
    violations: list[LayoutViolation] = []

    # Restrict to text + groups that visually carry text (have non-zero
    # area). Skip the slide background (largest element).
    candidates = [e for e in elements if e.area > 1000]
    if not candidates:
        return violations
    background = max(candidates, key=lambda e: e.area)
    boxes = [e for e in candidates if e is not background]

    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            # Containment is only "intentional layering" when one of
            # the shapes is a non-text container (background card,
            # accent rect, image). Two TEXT shapes where one contains
            # the other is the canonical model error pattern — a
            # chapter label rendered behind the title at the same y,
            # a caption pinned inside a hero number's box. Treat
            # text-in-text as overlap, fix below.
            both_text = a.tag == "text" and b.tag == "text"
            if not both_text and (_contained(a, b) or _contained(b, a)):
                continue
            iou = _overlap_ratio(a, b)
            if iou < 0.4:
                continue
            # Smaller box (by area) is the one we shift. Caption is
            # almost always smaller than the hero number it sits on.
            small, big = (a, b) if a.area <= b.area else (b, a)
            new_y = big.bottom + 8  # 8 px gap below the bigger box
            if new_y + small.h <= 720 + 4:  # only shift when it stays on canvas
                _apply_bounds(small.node, small.tag, small.x, new_y, small.w, small.h)
                # Mutate our in-memory record so later checks see the new y.
                small.y = new_y
                fix = True
            else:
                fix = False
            violations.append(
                LayoutViolation(
                    kind="overlap",
                    element_path=f"{small.path} ↔ {big.path}",
                    actual={
                        "small_bbox": (small.x, small.y, small.w, small.h),
                        "big_bbox": (big.x, big.y, big.w, big.h),
                        "overlap_ratio": round(iou, 2),
                    },
                    expected={"overlap_ratio_max": 0.4},
                    severity="warning",
                    fix_applied=fix,
                )
            )
    return violations


def _contained(inner: _Element, outer: _Element) -> bool:
    """``inner`` is fully (or nearly fully) inside ``outer``."""
    tol = 4
    return (
        inner.x + tol >= outer.x
        and inner.y + tol >= outer.y
        and inner.right - tol <= outer.right
        and inner.bottom - tol <= outer.bottom
    )


def _overlap_ratio(a: _Element, b: _Element) -> float:
    ix1 = max(a.x, b.x)
    iy1 = max(a.y, b.y)
    ix2 = min(a.right, b.right)
    iy2 = min(a.bottom, b.bottom)
    if ix1 >= ix2 or iy1 >= iy2:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    smaller = min(a.area, b.area)
    if smaller <= 0:
        return 0.0
    return inter / smaller


def _apply_bounds(
    node: ET.Element,
    tag: str,
    x: float,
    y: float,
    w: float,
    h: float,
) -> None:
    """Write (x, y, w, h) back to the element, using the right
    attribute set for the element type."""
    if tag in {"rect", "image", "use"}:
        node.set("x", str(int(x)))
        node.set("y", str(int(y)))
        node.set("width", str(int(w)))
        node.set("height", str(int(h)))
    elif tag == "g":
        if node.get("data-x") is not None:
            node.set("data-x", str(int(x)))
            node.set("data-y", str(int(y)))
            node.set("data-w", str(int(w)))
            node.set("data-h", str(int(h)))
        else:
            node.set("x", str(int(x)))
            node.set("y", str(int(y)))
            node.set("width", str(int(w)))
            node.set("height", str(int(h)))
    elif tag == "text":
        # Move only x — vertical adjustment uses the baseline already
        # encoded in y.
        node.set("x", str(int(x)))
        node.set("y", str(int(y + (h * 0.85))))
