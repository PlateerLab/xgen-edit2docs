"""SVG quality check tool.

Wraps `core.svg_quality_checker.SVGQualityChecker` behind a stateless function
that accepts SVG strings (instead of file paths) and returns structured issues.
The engine still writes scratch files (it does internal cross-reference checks
across the deck), so we use a temp workspace.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import Field

from ._workspace import temp_workspace, write_text
from .types import (
    CanvasFormat,
    CostBreakdown,
    DEFAULT_CANVAS,
    QualityIssue,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)


class QualitySlide(ToolRequest):
    index: int = Field(..., ge=0)
    name: str
    svg: str


class QualityCheckRequest(ToolRequest):
    slides: list[QualitySlide]
    canvas_format: CanvasFormat = DEFAULT_CANVAS
    template_mode: bool = Field(
        default=False,
        description="Skip spec_lock drift / image attribution checks for template authoring.",
    )
    images: dict[str, bytes] = Field(
        default_factory=dict,
        description=(
            "Image files (by basename) the slides reference. Written to the "
            "quality workspace so file-existence checks resolve. Optional — "
            "when missing, the legacy `<image href>` resolver may flag a "
            "real image as missing."
        ),
    )


class QualityCheckResponse(ToolResponse):
    issues: list[QualityIssue]
    passed: bool = Field(..., description="True iff no error-severity issues.")
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def check_svg_quality(req: QualityCheckRequest) -> QualityCheckResponse:
    """Run the engine's quality checks across an in-memory deck of SVGs."""
    from ..core.svg_quality_checker import SVGQualityChecker

    started = time.perf_counter()
    issues: list[QualityIssue] = []

    if not req.slides:
        return QualityCheckResponse(
            issues=[],
            passed=True,
            cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
        )

    checker = SVGQualityChecker(template_mode=req.template_mode)

    with temp_workspace(prefix="xgen_edit2docs-quality-") as ws:
        svg_dir = ws / "svgs"
        svg_dir.mkdir()
        for slide in sorted(req.slides, key=lambda s: s.index):
            write_text(svg_dir, f"{slide.name}.svg", slide.svg)
        # Drop image bytes in BOTH the svg directory (which is what the
        # converter / export workspace uses) and a sibling `images/`
        # directory (which is what the LLM tends to reference as
        # `../images/<name>`). Without these, the legacy
        # `_check_image_references` reports `Image file not found` even
        # for images that were successfully acquired and bundled.
        if req.images:
            images_dir = ws / "images"
            images_dir.mkdir(exist_ok=True)
            for name, content in req.images.items():
                if "/" in name or "\\" in name:
                    continue
                (svg_dir / name).write_bytes(content)
                (images_dir / name).write_bytes(content)

        # Run per-file checks. The engine also has a directory-level checker,
        # but the per-file path is enough for the M2 contract.
        for slide in sorted(req.slides, key=lambda s: s.index):
            svg_path = svg_dir / f"{slide.name}.svg"
            result = checker.check_file(str(svg_path), expected_format=req.canvas_format)
            for err in result.get("errors", []):
                issues.append(
                    QualityIssue(
                        page_index=slide.index,
                        severity="error",
                        code="quality_error",
                        message=str(err),
                        location=slide.name,
                    )
                )
            for warn in result.get("warnings", []):
                issues.append(
                    QualityIssue(
                        page_index=slide.index,
                        severity="warning",
                        code="quality_warning",
                        message=str(warn),
                        location=slide.name,
                    )
                )

            # Converter-parity check: flag anything that would later make
            # `convert_svg_to_slide_shapes` raise, BEFORE the export stage.
            # The legacy `_check_forbidden_elements` only catches
            # `<symbol>+<use>` co-occurrence; a bare `<use href="#missing"/>`
            # or `<use data-icon="unknown/x"/>` slips through it but still
            # crashes the converter. Catching those here lets the per-page
            # retry loop in generate_deck fix them before the deck reaches
            # export.
            for code, msg in _converter_parity_issues(slide.svg):
                issues.append(
                    QualityIssue(
                        page_index=slide.index,
                        severity="error",
                        code=code,
                        message=msg,
                        location=slide.name,
                    )
                )

            # Stylistic discipline: palette + font diversity. These are
            # warnings (not errors) — the deck still ships, but the
            # operator sees that the LLM drifted away from spec_lock.
            for code, msg, detail in _style_discipline_issues(slide.svg):
                issues.append(
                    QualityIssue(
                        page_index=slide.index,
                        severity="warning",
                        code=code,
                        message=msg,
                        location=slide.name,
                    )
                )

    passed = not any(i.severity == "error" for i in issues)
    return QualityCheckResponse(
        issues=issues,
        passed=passed,
        cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
    )


def _style_discipline_issues(svg: str) -> list[tuple[str, str, dict]]:
    """Surface per-slide stylistic drift from spec_lock conventions.

    Two checks, both **warnings** (don't trigger retry, just inform):

    * ``style_palette_too_large`` — > 8 distinct hex colors on one
      slide. The spec_lock palette discipline targets 4-6 colors; >8
      means the LLM invented alpha-variants or accent tones outside
      the declared palette.
    * ``style_font_diversity_high`` — > 3 distinct font families on
      one slide. spec_lock.typography typically declares 1-2 stacks;
      anything past 3 is the LLM mixing decorative fonts the model
      pulled from training data (Times New Roman / Nanum Myeongjo
      etc. showed up in deck_2.pptx beyond the declared Pretendard +
      Malgun Gothic).
    """
    import re

    issues: list[tuple[str, str, dict]] = []

    # Hex colors — capture every #RRGGBB or #RGB in the slide. Lower
    # the string first so we count `#0a0a0a` and `#0A0A0A` as one hue.
    colors = set()
    for m in re.finditer(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", svg):
        v = m.group(1).lower()
        if len(v) == 3:
            v = v[0] * 2 + v[1] * 2 + v[2] * 2
        colors.add(v)
    # Threshold calibrated against real disciplined decks (deck_3.pptx
    # analysis): a healthy slide uses 1 background + 3-4 background
    # layering variants + 3 grays for text hierarchy + 2-3 accents,
    # which is 9-12 colors. Above 14 is genuine drift.
    if len(colors) > 14:
        issues.append((
            "style_palette_too_large",
            (
                f"Slide uses {len(colors)} colors (recommended ≤ 14) — "
                "likely mixing colors outside the spec_lock palette. "
                f"슬라이드에 {len(colors)}개의 색상이 사용됨 (권장 ≤ 14)."
            ),
            {"colors_count": len(colors)},
        ))

    # Font families. Look for `font-family="..."` and `font-family:` in
    # inline style. The promoter (P1.4) already lifted inline style →
    # attribute, but legacy / hand-written SVG may still carry both.
    fonts = set()
    for m in re.finditer(r'font-family\s*[:=]\s*["\']?([^"\';\n]+)', svg):
        stack = m.group(1)
        # Take only the FIRST font in the stack — that's the one the
        # model picked. Fallbacks are commodity by definition.
        first = stack.split(",", 1)[0].strip().strip("\"'")
        if first:
            fonts.add(first)
    if len(fonts) > 3:
        issues.append((
            "style_font_diversity_high",
            (
                f"Slide uses {len(fonts)} font families (recommended ≤ 3) — "
                "likely drifting from the spec_lock.typography stack. "
                f"슬라이드에 {len(fonts)}개의 폰트 family 사용됨 (권장 ≤ 3)."
            ),
            {"fonts": sorted(fonts)},
        ))

    return issues


def _converter_parity_issues(svg: str) -> list[tuple[str, str]]:
    """Surface every element / attribute pattern that would later make
    `convert_svg_to_slide_shapes` raise — but only AFTER running the
    same pre-dispatch expanders the converter uses.

    The Executor is explicitly instructed to emit `<use data-icon="...">`
    for icons; the `use_expander` resolves those into primitive shapes
    at convert time. So we mirror that pipeline here: parse, run both
    expanders, then report on what survived. Anything still present at
    that point is an unfixable element the converter will reject — and
    it's exactly what the retry-hint builder can give the model
    targeted feedback about (icon name, dangling href, etc.).

    Each tuple is (issue_code, human-readable message). Codes are
    machine-friendly so the per-page retry loop in generate_deck can
    pull a specific correction directive from `_RETRY_HINTS`.
    """
    import re
    from pathlib import Path
    from xml.etree import ElementTree as ET

    from ..core.svg_to_pptx.use_href_expander import expand_use_href

    found: list[tuple[str, str]] = []

    # Parse once. If the SVG is malformed XML, the converter will choke
    # too — flag and bail.
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return [("malformed_xml", f"SVG is not valid XML: {exc}")]

    # Run the same data-icon expander the converter uses. If the icon
    # library directory is on disk, this resolves every <use data-icon>
    # to inline primitives. Anything left after this pass is a missing
    # or mistyped icon name — and the retry hint must call it out.
    icons_dir = Path(__file__).resolve().parent.parent / "core" / "templates" / "icons"
    if icons_dir.exists():
        try:
            from ..core.svg_to_pptx.use_expander import expand_use_data_icons
            expand_use_data_icons(root, icons_dir)
        except Exception:
            # Best effort — if the expander itself errors, fall through;
            # the residual-<use> scan below will still surface the issue.
            pass

    # Standard <use href="#id"/> — usually nothing to do at executor
    # stage but we run it for safety.
    try:
        expand_use_href(root)
    except Exception:
        pass

    # Survey what survived. Anything in the dispatcher's blocklist is a
    # quality error the model can fix on retry.
    SVG_NS = "http://www.w3.org/2000/svg"

    def _local(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    flagged_codes: set[str] = set()
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "use":
            # Distinguish failure modes for actionable feedback.
            data_icon = elem.get("data-icon")
            href = elem.get("href") or elem.get(
                "{http://www.w3.org/1999/xlink}href"
            )
            if data_icon and "forbidden_use_data_icon" not in flagged_codes:
                found.append(
                    (
                        "forbidden_use_data_icon",
                        f"Unresolvable <use data-icon=\"{data_icon}\"/>: icon "
                        "not found in the bundled library. Either pick an "
                        "icon from the spec_lock inventory or inline the "
                        "primitive shapes (<path>/<circle>/<rect>) directly.",
                    )
                )
                flagged_codes.add("forbidden_use_data_icon")
            elif href and "forbidden_use_href" not in flagged_codes:
                found.append(
                    (
                        "forbidden_use_href",
                        f"Unresolvable <use href=\"{href}\"/>: the referenced "
                        "id does not exist in this SVG. Inline the shape it "
                        "was supposed to clone.",
                    )
                )
                flagged_codes.add("forbidden_use_href")
            elif "forbidden_use_bare" not in flagged_codes:
                found.append(
                    (
                        "forbidden_use_bare",
                        "<use> element with no href / data-icon — remove or "
                        "replace with primitive shapes.",
                    )
                )
                flagged_codes.add("forbidden_use_bare")
        elif tag == "foreignObject" and "forbidden_foreign_object" not in flagged_codes:
            found.append(
                (
                    "forbidden_foreign_object",
                    "<foreignObject> — use <tspan> for line breaks and SVG "
                    "primitives for everything else.",
                )
            )
            flagged_codes.add("forbidden_foreign_object")
        elif tag == "script" and "forbidden_script" not in flagged_codes:
            found.append(
                (
                    "forbidden_script",
                    "<script> — strip it. SVG must contain only static "
                    "shapes / text / images.",
                )
            )
            flagged_codes.add("forbidden_script")

    return found
