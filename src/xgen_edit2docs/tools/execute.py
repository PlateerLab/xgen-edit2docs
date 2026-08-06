"""Execute tool: per-page SVG generation by the Executor LLM role.

For each page in the Strategist's plan, this tool calls the LLM with:
- system prompt: executor-base + style variant (consultant/general/...) for the page lang
- user message: spec_lock YAML + this page's content outline + any images for it

The LLM returns an SVG string (plus optional speaker notes). Pages are
independent so the worker can fan them out in parallel — see
ppt-master-analysis/04-integration-plan.md §4.9.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Literal

from pydantic import Field

from ..config import resolve_model
from ..llm import AnthropicClient, DEFAULT_MODEL, build_output_lang_directive, load_prompt
from ..llm.anthropic_client import LLMResult, LLMUsage
from .strategize import LLMCallable
from .types import (
    CostBreakdown,
    DEFAULT_LANG,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)

ExecutorStyle = Literal["general", "consultant", "consultant-top"]


class ExecutorImage(ToolRequest):
    """One image available to a page (bytes or external URL)."""

    placeholder: str = Field(..., description="Token used by the LLM to reference this image.")
    url: str | None = None
    description: str | None = None


class ExecutePageRequest(ToolRequest):
    spec_lock: str = Field(..., description="YAML spec_lock from the Strategist.")
    page_index: int = Field(..., ge=0)
    page_summary: str = Field(..., description="Per-page content outline (markdown).")
    images: list[ExecutorImage] = Field(default_factory=list)
    # Deterministic per-page layout brief (P2.1). When set, injected
    # as the first section of the user message so the LLM has hard
    # bounding-box constraints to work inside. Optional so legacy
    # callers (and unit tests that bypass generate_deck) still compile.
    layout_brief_yaml: str | None = None
    style: ExecutorStyle = "general"
    lang: LangCode = DEFAULT_LANG
    model: str = DEFAULT_MODEL
    anthropic_api_key: str


class ExecutePageResponse(ToolResponse):
    page_index: int
    svg: str
    speaker_notes: str
    raw_output: str
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


class ExecuteBatchRequest(ToolRequest):
    """Parallel execution of multiple pages with a shared spec_lock."""

    spec_lock: str
    pages: list[ExecutePageRequest]
    max_concurrency: int = Field(default=4, ge=1, le=16)


class ExecuteBatchResponse(ToolResponse):
    results: list[ExecutePageResponse]
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


async def execute_page(
    req: ExecutePageRequest,
    *,
    client: LLMCallable | None = None,
) -> ExecutePageResponse:
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    system_prompt = _build_system_prompt(req.style, req.lang)
    user_message = _build_user_message(req)

    llm = client or AnthropicClient(api_key=req.anthropic_api_key, model=req.model)
    result = await llm.complete(
        system_prompt=system_prompt,
        user_message=user_message,
        max_output_tokens=8192,
        cache_system=True,
        model=resolve_model("executor", req.model),
        # spec_lock is identical for every page in a deck, so it rides in a
        # cached system suffix (written once, read back on every page)
        # instead of being re-sent inside each per-page user message.
        system_suffix=_build_spec_lock_suffix(req.spec_lock),
    )

    svg, notes = _parse_output(result.text, warnings)
    # Normalise the LLM's raw SVG before it flows downstream. Quality
    # and export both run on this exact string — every fix-up applied
    # here means one less stage-specific patch elsewhere:
    #   * id-backfill on anonymous top-level <g> (kills the
    #     `<g> has no id` warning class).
    #   * <image href> normalised to bare basename. The model loves to
    #     prefix references with `../images/`, which doesn't resolve in
    #     the workspace where images sit alongside SVGs.
    #   * Strip `opacity` from <image> (PPT doesn't support image
    #     opacity; the legacy checker bans it. We could decompose into
    #     image + overlay rect but losing the mute is acceptable for
    #     keeping the build green).
    svg = _autoid_top_level_groups(svg)
    svg = _normalise_viewbox_to_canonical(svg, getattr(req, "canvas_format", "ppt169"))
    svg = _promote_inline_styles(svg)
    svg = _quantize_colors_to_palette(svg, req.spec_lock)
    svg, image_basenames = _normalise_image_refs(svg, req.images)
    # `canvas_format` is a deck-level attribute; ExecutePageRequest
    # doesn't always carry it (batch tests pass per-page reqs only).
    # Default to ppt169 — the layout repair pass also falls back to
    # the same canvas, so this is effectively a no-op when missing.
    svg, layout_violations = _repair_layout(svg, getattr(req, "canvas_format", "ppt169"))
    for v in layout_violations:
        warnings.append(
            WarningEntry(
                code=f"layout_{v.kind}",
                message=_layout_violation_message(v),
                detail={
                    "element_path": v.element_path,
                    "actual": v.actual,
                    "expected": v.expected,
                    "fix_applied": v.fix_applied,
                },
            )
        )

    return ExecutePageResponse(
        page_index=req.page_index,
        svg=svg,
        speaker_notes=notes,
        raw_output=result.text,
        cost=_cost_from_usage(result.usage, time.perf_counter() - started),
        warnings=warnings,
    )


async def execute_batch(
    req: ExecuteBatchRequest,
    *,
    client: LLMCallable | None = None,
) -> ExecuteBatchResponse:
    """Run every page in parallel under a concurrency cap.

    Per-page exceptions are captured and surfaced as warnings (the failed
    page gets a placeholder SVG so subsequent stages — quality, export —
    still see N slides). This preserves the rest of the deck when one
    Executor call goes sideways.

    Cache warm-up: the shared prompt cache (executor system prompt +
    spec_lock suffix) is written by the FIRST page that runs. If all pages
    fan out at once, the first ``max_concurrency`` pages each race the
    write and re-pay it (~2x waste on the shared prefix). So we run page 0
    to completion first — warming the cache — then fan out the rest, which
    read the warmed prefix. A single-page batch is unaffected.
    """
    started = time.perf_counter()
    sem = asyncio.Semaphore(req.max_concurrency)

    async def _run_one(p: ExecutePageRequest) -> ExecutePageResponse:
        async with sem:
            return await execute_page(p, client=client)

    raw_results: list = []
    if req.pages:
        # Warm-up: first page awaited alone so the shared cache is written
        # before the fan-out reads it. Capture its exception the same way
        # `gather(return_exceptions=True)` would, so a failed warm-up still
        # yields a placeholder + warning (and the rest still fan out).
        try:
            raw_results.append(await execute_page(req.pages[0], client=client))
        except Exception as exc:  # noqa: BLE001 — mirrors gather's capture
            raw_results.append(exc)
        if len(req.pages) > 1:
            rest = await asyncio.gather(
                *[_run_one(p) for p in req.pages[1:]], return_exceptions=True
            )
            raw_results.extend(rest)

    results: list[ExecutePageResponse] = []
    warnings: list[WarningEntry] = []
    for page_req, outcome in zip(req.pages, raw_results):
        if isinstance(outcome, Exception):
            warnings.append(
                WarningEntry(
                    code="execute_page_failed",
                    message=f"Page {page_req.page_index} executor failed: {outcome}",
                    detail={
                        "page_index": page_req.page_index,
                        "error_type": type(outcome).__name__,
                    },
                )
            )
            results.append(_placeholder_response(page_req))
        else:
            results.append(outcome)

    total = CostBreakdown(duration_seconds=time.perf_counter() - started)
    for r in results:
        total = CostBreakdown(
            input_tokens=total.input_tokens + r.cost.input_tokens,
            output_tokens=total.output_tokens + r.cost.output_tokens,
            cache_read_tokens=total.cache_read_tokens + r.cost.cache_read_tokens,
            cache_write_tokens=total.cache_write_tokens + r.cost.cache_write_tokens,
            duration_seconds=total.duration_seconds,
        )
        warnings.extend(r.warnings)

    return ExecuteBatchResponse(
        results=sorted(results, key=lambda r: r.page_index),
        cost=total,
        warnings=warnings,
    )


def _placeholder_response(req: ExecutePageRequest) -> ExecutePageResponse:
    """Minimal valid SVG used when an executor call fails. Keeps the deck
    aligned to N slides so quality / export still run cleanly."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" '
        'width="1920" height="1080">'
        '<rect x="0" y="0" width="1920" height="1080" fill="#fafafa"/>'
        f'<text x="120" y="540" font-family="sans-serif" font-size="36" fill="#888">'
        f'Page {req.page_index + 1} could not be generated.'
        '</text>'
        '</svg>'
    )
    return ExecutePageResponse(
        page_index=req.page_index,
        svg=svg,
        speaker_notes="",
        raw_output="",
        cost=CostBreakdown(),
        warnings=[],
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_system_prompt(style: ExecutorStyle, lang: LangCode) -> str:
    """Stitch the runtime language directive + base executor + style variant.

    All prompts are English single-source (see llm/prompt_loader.py); the
    directive tells the LLM to emit slide content in *lang* while keeping
    SVG attribute names / asset filenames / token names English.
    """
    directive = build_output_lang_directive(lang)
    base = load_prompt("executor-base")
    variant_role = {
        "general": "executor-general",
        "consultant": "executor-consultant",
        "consultant-top": "executor-consultant-top",
    }[style]
    variant = load_prompt(variant_role)
    brief_directive = (
        "## Layout brief contract\n\n"
        "When the user message begins with a `## Layout brief` section, "
        "those bounding boxes are HARD constraints:\n\n"
        "- Place each visible element inside the zone whose role matches "
        "  its semantic purpose (title in `title`, page number in "
        "  `page_number`, etc.).\n"
        "- Do NOT emit content outside `safe_area`. The 40 px margin on "
        "  every edge is reserved.\n"
        "- The page number zone always lives at the brief's declared "
        "  position — render `NN / MM` (or `Page N`) inside that exact "
        "  box. Do not invent a smaller box for it; the model has "
        "  historically sized this at 42 px wide for 7-char text, "
        "  which is too narrow.\n"
        "- The chapter label zone is full-width across the top, separate "
        "  from the title. Do NOT stack the chapter label on top of the "
        "  title — they belong in different y-bands.\n"
        "- Body / hero zones are guidance, not pixel-perfect cells. "
        "  Stay inside their bounds; you may subdivide them.\n"
        "- The brief's coordinate space is 1280×720. If you prefer to "
        "  emit at a different resolution, you may — the pipeline "
        "  normalises any 16:9 viewBox to canonical canvas before "
        "  conversion.\n"
    )
    return f"{directive}\n\n---\n\n{base}\n\n---\n\n{brief_directive}\n\n---\n\n{variant}"


def _build_spec_lock_suffix(spec_lock: str) -> str:
    """Render the deck-wide spec_lock as a cached system suffix.

    spec_lock is identical for every page in a deck, so it belongs in a
    cached system block (see ``AnthropicClient.complete(system_suffix=)``)
    that pairs with the executor system prompt as one cached prefix —
    written once, read back on every subsequent page — rather than being
    re-sent (uncached) inside each per-page user message.
    """
    return "## spec_lock\n```yaml\n" + spec_lock.strip() + "\n```"


def _build_user_message(req: ExecutePageRequest) -> str:
    lines: list[str] = []
    lines.append(f"# Page {req.page_index} ({req.lang})")
    lines.append("")
    # Layout brief comes BEFORE page content. This is the per-page
    # hard-constraint section: the LLM should place every shape inside
    # the declared zones rather than inventing coordinates. (spec_lock is
    # no longer inlined here — it rides in the cached system suffix; see
    # `_build_spec_lock_suffix`.)
    if req.layout_brief_yaml:
        lines.append("## Layout brief (HARD constraint — use these bounding boxes)")
        lines.append("```yaml")
        lines.append(req.layout_brief_yaml.strip())
        lines.append("```")
        lines.append("")
    lines.append("## Page content")
    lines.append(req.page_summary.strip())
    lines.append("")
    if req.images:
        lines.append("## Images available")
        for img in req.images:
            extra = f" — {img.description}" if img.description else ""
            location = img.url or "(inline, bound to placeholder)"
            lines.append(f"- `{img.placeholder}` @ {location}{extra}")
        lines.append("")
    lines.append("## Output format")
    lines.append(
        "Produce two fenced blocks in this order:\n"
        "1. ```svg ... ``` — the full slide SVG for this page only.\n"
        "2. ```notes ... ``` — speaker notes (markdown). May be empty."
    )
    return "\n".join(lines)


_SVG_BLOCK_RE = re.compile(r"```(?:svg|xml)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_NOTES_BLOCK_RE = re.compile(r"```(?:notes|markdown)\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def _parse_output(text: str, warnings: list[WarningEntry]) -> tuple[str, str]:
    svg_match = _SVG_BLOCK_RE.search(text)
    if svg_match:
        svg = svg_match.group(1).strip()
    else:
        # Some models emit a raw <svg>...</svg> block without fences. Tolerate it.
        bare = re.search(r"<svg[\s\S]*?</svg>", text, re.IGNORECASE)
        if bare:
            svg = bare.group(0).strip()
            warnings.append(
                WarningEntry(
                    code="unfenced_svg",
                    message="Executor returned an unfenced <svg> block; accepted but verify formatting.",
                )
            )
        else:
            raise ValueError("Executor output did not contain an SVG block")

    notes_match = _NOTES_BLOCK_RE.search(text)
    notes = notes_match.group(1).strip() if notes_match else ""
    return svg, notes


# Canvas dimensions in SVG pixel units for each declared format. The
# repair pass uses these to clamp off-canvas elements.
_CANVAS_DIMS: dict[str, tuple[int, int]] = {
    "ppt169": (1280, 720),
    "ppt43": (1024, 768),
    "xiaohongshu": (1242, 1660),
    "wechat": (1080, 1080),
    "story": (1080, 1920),
}


def _repair_layout(svg: str, canvas_format: str):
    """Run the deterministic layout-repair pass.

    Imported lazily so tests that don't exercise the LLM path (and
    therefore don't need lxml-style SVG parsing) keep their light
    dependency surface."""
    from ..core.svg_to_pptx.layout_repair import LayoutViolation, repair_layout

    canvas = _CANVAS_DIMS.get(canvas_format, _CANVAS_DIMS["ppt169"])
    result = repair_layout(svg, canvas=canvas)
    return result.repaired_svg, result.violations


def _layout_violation_message(v) -> str:
    """One-line human description for `WarningEntry.message`."""
    if v.kind == "overlap":
        ratio = v.actual.get("overlap_ratio", 0)
        return (
            f"Two elements overlap by {int(ratio * 100)}%; "
            + ("moved the smaller one down automatically." if v.fix_applied
               else "could not auto-fix.")
        )
    if v.kind == "text_overflow_x":
        required = v.actual.get("required_w")
        actual = v.actual.get("box_w")
        return (
            f"Text exceeds its box width (needs {required}px / box {actual:.0f}px); "
            + ("widened the box." if v.fix_applied else "box left as-is.")
        )
    if v.kind == "off_canvas":
        return (
            "Element was outside the canvas — moved it inside." if v.fix_applied
            else "Element is outside the canvas."
        )
    if v.kind == "empty_decoration":
        return "Removed an empty decorative element."
    return f"Layout violation: {v.kind}"


_RE_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int] | None:
    """Parse `#abc` or `#aabbcc` → (r, g, b). Returns None if malformed."""
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _palette_from_spec_lock(spec_lock: str) -> list[tuple[str, tuple[int, int, int]]]:
    """Pull every `#RRGGBB` literal out of the spec_lock as a palette.

    Tolerant of both YAML and markdown forms. Returns an ordered list
    of (hex_uppercase, rgb_tuple). Duplicates are removed but order is
    preserved so the snap-to-nearest deterministically prefers earlier
    declarations (Strategist's first palette colors are the most
    intentional).
    """
    if not spec_lock:
        return []
    seen: dict[str, tuple[int, int, int]] = {}
    ordered: list[tuple[str, tuple[int, int, int]]] = []
    for m in _RE_HEX.finditer(spec_lock):
        rgb = _hex_to_rgb(m.group(0))
        if rgb is None:
            continue
        hex_up = "#{:02X}{:02X}{:02X}".format(*rgb)
        if hex_up not in seen:
            seen[hex_up] = rgb
            ordered.append((hex_up, rgb))
    return ordered


def _quantize_colors_to_palette(svg: str, spec_lock: str) -> str:
    """Snap every hex color in *svg* to the nearest spec_lock palette
    color when the RGB distance is small.

    The LLM routinely emits slight variants of palette colors (different
    alpha layering, hand-tweaked shadows). Each variant is reported by
    the quality stage as palette bloat. Snapping them to the declared
    palette restores the Strategist's typography discipline without
    requiring a retry.

    Snap policy:
      * Compute squared Euclidean RGB distance from the SVG color to
        every palette entry.
      * If the closest palette color is within 30 RGB units (about
        9% perceptual similarity), snap.
      * Otherwise leave the original — the model invented a genuinely
        different color and the operator should see it.

    Returns the SVG unchanged when there's no palette to snap to.
    """
    palette = _palette_from_spec_lock(spec_lock)
    if not palette or not svg or "#" not in svg:
        return svg

    # Squared distance threshold — 30 RGB units per channel → r² = 30² * 3
    THRESHOLD_SQ = 30 * 30 * 3
    cache: dict[str, str] = {}

    def _snap(match: "re.Match[str]") -> str:
        raw = match.group(0)
        if raw in cache:
            return cache[raw]
        rgb = _hex_to_rgb(raw)
        if rgb is None:
            cache[raw] = raw
            return raw
        best: tuple[str, int] | None = None
        for p_hex, p_rgb in palette:
            d = (rgb[0] - p_rgb[0]) ** 2 + (rgb[1] - p_rgb[1]) ** 2 + (rgb[2] - p_rgb[2]) ** 2
            if best is None or d < best[1]:
                best = (p_hex, d)
        if best is not None and best[1] <= THRESHOLD_SQ:
            cache[raw] = best[0]
            return best[0]
        cache[raw] = raw
        return raw

    return _RE_HEX.sub(_snap, svg)


# Canonical canvas dimensions per format. The converter assumes
# 1 SVG px = 9525 EMU, so any SVG whose viewBox doesn't match these
# dimensions emits coordinates that fall off-canvas in the final
# PPTX. We canonicalise at the Executor boundary by wrapping content
# in a `<g transform="scale(...)">` whenever the model picked a
# differently-sized 16:9 / 4:3 viewBox.
_CANONICAL_VIEWBOX: dict[str, tuple[int, int]] = {
    "ppt169": (1280, 720),
    "ppt43": (1024, 768),
    "xiaohongshu": (1242, 1660),
    "wechat": (1080, 1080),
    "story": (1080, 1920),
}


def _normalise_viewbox_to_canonical(svg: str, canvas_format: str) -> str:
    """Rewrite the SVG so its viewBox matches the canonical canvas.

    The user keeps emitting decks at 1920×1080 / 1600×900 / other
    16:9-aspect viewBoxes (any popular video resolution). Our DrawingML
    converter assumes 1 SVG px = 9525 EMU, so a non-canonical viewBox
    pushes every pixel past the slide's actual canvas.

    Delegates to ``core.svg_to_pptx.svg_scale.scale_svg_to_viewbox`` —
    aspect-matching viewBoxes are wrapped in a scale group; mismatched
    aspects / missing viewBox / parse failures pass through unchanged.
    """
    canonical = _CANONICAL_VIEWBOX.get(canvas_format)
    if canonical is None:
        return svg
    from ..core.svg_to_pptx.svg_scale import scale_svg_to_viewbox

    return scale_svg_to_viewbox(svg, float(canonical[0]), float(canonical[1]))


def _promote_inline_styles(svg: str) -> str:
    """Lift CSS declarations on ``style="..."`` into native SVG
    attributes so the DrawingML converter sees them.

    The Executor sometimes emits ``<text style="font-size:14px;
    font-weight:700;color:#0a0">...</text>``. Our converter only reads
    ``font-size`` / ``font-weight`` / ``fill`` as element attributes;
    inline styles are silently ignored, so the run ends up rendered at
    the default 16-px regular black. Promoting the declarations
    upgrades whatever the model intended.

    Promoted properties:
      * font-size, font-weight, font-style, font-family
      * fill, stroke, stroke-width, opacity, fill-opacity, stroke-opacity
      * letter-spacing, text-anchor

    Existing element attributes win — if the element already declared
    ``font-size="20"`` and the inline style says ``font-size:14px``, the
    explicit attribute is preserved (model intent is ambiguous; the
    explicit attr is the safer pick).

    Best effort: parse failures pass the SVG through unchanged.
    """
    if not svg or "style=" not in svg:
        return svg
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(svg)
    except ET.ParseError:
        return svg

    promotable = {
        "font-size",
        "font-weight",
        "font-style",
        "font-family",
        "fill",
        "stroke",
        "stroke-width",
        "opacity",
        "fill-opacity",
        "stroke-opacity",
        "letter-spacing",
        "text-anchor",
    }

    for elem in root.iter():
        style = elem.get("style")
        if not style:
            continue
        # CSS declarations are `key:value` separated by `;`. Tolerant
        # of trailing semicolons, whitespace, and `key: value` spacing.
        declarations: list[tuple[str, str]] = []
        keep: list[str] = []
        for decl in style.split(";"):
            decl = decl.strip()
            if not decl or ":" not in decl:
                if decl:
                    keep.append(decl)
                continue
            key, _, value = decl.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if not key or not value:
                continue
            # Drop the `px` suffix off numeric font-size — SVG attr
            # is unit-less by convention.
            if key == "font-size" and value.lower().endswith("px"):
                value = value[:-2].strip()
            if key in promotable:
                declarations.append((key, value))
            else:
                keep.append(f"{key}:{value}")

        # Promote only when the element doesn't already declare the attr.
        for key, value in declarations:
            if elem.get(key) is None:
                elem.set(key, value)

        # Re-serialise the remaining style or drop it entirely.
        if keep:
            elem.set("style", "; ".join(keep))
        elif "style" in elem.attrib:
            del elem.attrib["style"]

    ET.register_namespace("", "http://www.w3.org/2000/svg")
    return ET.tostring(root, encoding="unicode")


def _normalise_image_refs(svg: str, available: list) -> tuple[str, set[str]]:
    """Make every `<image>` resilient to the workspace layout.

    Three transforms applied in one pass:
      1. `href` (or `xlink:href`) is reduced to the **basename** of the
         path. The model frequently writes `../images/cover_bg.png`
         expecting an `images/` subfolder; export puts the bytes
         directly next to the SVG. Normalising to `cover_bg.png` makes
         the reference resolve regardless of layout.
      2. The `opacity` attribute is removed — PPTX has no native
         image opacity, the legacy quality rule bans it, and the
         tiniest production case where this fires (a chapter divider
         dimmed for readability) is acceptable to render at full
         opacity. The retry hint would otherwise loop the model
         needlessly.
      3. `<image>` elements whose basename is not in the executor's
         image bundle are dropped entirely — the reference would
         dangle and crash the converter. The slide loses a decoration
         but stays intact.

    Returns (svg, basenames_referenced). The basename set lets the
    caller cross-check against the bundle.

    Best effort: parse failures fall through, returning the input.
    """
    if not svg or "<image" not in svg:
        return svg, set()
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(svg)
    except ET.ParseError:
        return svg, set()

    SVG_NS = "http://www.w3.org/2000/svg"
    XLINK_NS = "http://www.w3.org/1999/xlink"

    # Build the lookup set of bundled basenames.
    bundle: set[str] = set()
    for img in available or []:
        url = getattr(img, "url", None)
        if url:
            bundle.add(url.rsplit("/", 1)[-1])

    parent_of: dict[ET.Element, ET.Element] = {}
    for p in root.iter():
        for c in p:
            parent_of[c] = p

    referenced: set[str] = set()
    drops: list[tuple[ET.Element, ET.Element]] = []

    for elem in list(root.iter()):
        tag = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
        if tag != "image":
            continue

        href = elem.get("href") or elem.get(f"{{{XLINK_NS}}}href")
        if href and not href.startswith("data:"):
            basename = href.rsplit("/", 1)[-1]
            if elem.get("href") is not None:
                elem.set("href", basename)
            else:
                elem.set(f"{{{XLINK_NS}}}href", basename)
            referenced.add(basename)
            # If the basename isn't in the bundle, drop the element —
            # better a slide missing one decoration than a crash.
            if bundle and basename not in bundle:
                parent = parent_of.get(elem)
                if parent is not None:
                    drops.append((parent, elem))

        if "opacity" in elem.attrib:
            del elem.attrib["opacity"]

    for parent, elem in drops:
        parent.remove(elem)

    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="unicode"), referenced


def _autoid_top_level_groups(svg: str) -> str:
    """Backfill `id` on every top-level <g> of *svg* that lacks one.

    Runs at the boundary where the LLM's raw SVG enters the pipeline.
    Quality, retry, export and the final PPTX all see the normalized
    text — eliminates the spammy `Top-level visible <g> #N has no id`
    warnings without changing the LLM's behaviour or the visible
    output.

    Best-effort: if the SVG fails to parse we return it untouched and
    let the downstream stages report the real error.
    """
    if not svg or "<svg" not in svg:
        return svg
    try:
        from xml.etree import ElementTree as ET

        from ..core.svg_to_pptx.drawingml_converter import (
            _autogen_top_level_group_ids,
        )

        root = ET.fromstring(svg)
        if _autogen_top_level_group_ids(root) == 0:
            return svg
        # Preserve the `xmlns` declaration in the serialized output —
        # ElementTree drops the namespace prefix when re-serialising,
        # so we explicitly re-register the default SVG namespace.
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        return ET.tostring(root, encoding="unicode")
    except Exception:
        return svg


def _cost_from_usage(usage: LLMUsage, duration_seconds: float) -> CostBreakdown:
    return CostBreakdown(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        duration_seconds=duration_seconds,
    )
