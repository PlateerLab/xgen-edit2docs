"""Deterministic spec_lock validator.

The Strategist emits ``spec_lock`` (YAML- or markdown-shaped) declaring
color palette, font stacks, icon inventory, image plan. The Executor
treats every entry as ground truth. Without validation, a typo in the
icon inventory or a hex color without ``#`` propagates all the way to
the PPTX and only surfaces as a per-page failure later.

This module re-reads the freshly-generated ``spec_lock`` and:

* Verifies each icon name resolves to an actual file in the bundled
  library (auto-substitutes the closest fuzzy match when it doesn't).
* Normalises color hex (``#fff`` → ``#FFFFFF``, ``rgb(...)`` → ``#RRGGBB``).
* Ensures every font stack ends with a Windows-safe family.
* Surfaces missing required fields (canvas, colors, fonts) as warnings.

The output is the (possibly-edited) ``spec_lock`` plus a list of
``ValidationWarning`` records. The Strategist tool wires both back into
``StrategizeResponse`` so downstream stages see a clean spec.

Best-effort: YAML parse failures fall through to a no-op pass so the
spec still flows to the Executor — the legacy quality checker is the
last line of defence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ValidationWarning:
    code: str
    message: str
    detail: dict


@dataclass
class SpecLockValidation:
    spec_lock: str
    warnings: list[ValidationWarning]


# Windows-safe font tails for the typography stack discipline.
_WIN_SAFE_TAILS = frozenset(
    {
        "malgun gothic", "맑은 고딕",
        "arial", "arial black", "calibri", "segoe ui", "verdana",
        "times new roman", "georgia", "cambria",
        "consolas", "courier new",
        "microsoft yahei", "simsun", "simhei", "kaiti",
    }
)

# Hex normalisation matches `#abc`, `#aabbcc`, `rgb(1, 2, 3)`, `rgba(...)`.
_RE_HEX_SHORT = re.compile(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])(?=\b|[^0-9a-fA-F])")
_RE_HEX_LONG = re.compile(r"#([0-9a-fA-F]{6})(?=\b|[^0-9a-fA-F])")
_RE_RGB = re.compile(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)")
_RE_RGBA = re.compile(r"rgba\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*[\d.]+\s*\)")


def validate_spec_lock(
    spec_lock: str,
    *,
    icons_dir: Path | None = None,
) -> SpecLockValidation:
    """Run every validator over *spec_lock* and return the corrected text
    plus a flat list of warnings."""
    warnings: list[ValidationWarning] = []
    if not spec_lock or not spec_lock.strip():
        return SpecLockValidation(spec_lock=spec_lock, warnings=warnings)

    spec_lock = _normalise_hex_colors(spec_lock, warnings)
    spec_lock = _resolve_icons(spec_lock, icons_dir, warnings)
    _check_required_fields(spec_lock, warnings)
    _check_windows_safe_font_tails(spec_lock, warnings)
    _check_page_zones(spec_lock, warnings)

    return SpecLockValidation(spec_lock=spec_lock, warnings=warnings)


def _check_page_zones(
    spec_lock: str, warnings: list[ValidationWarning]
) -> None:
    """Validate `page_zones` bbox declarations against the canvas.

    Errors caught here:
      * Any zone whose bbox spills past the canvas (1280×720).
      * Page-number zone narrower than 130 px (can't fit "NN / MM"
        at 12 pt).
      * Chapter-label and title zones whose y ranges overlap (the
        deck_2.pptx slide-10 pattern).

    The validator does NOT mutate the spec_lock — we surface warnings
    so the operator (and the model on retry) see exactly what's
    wrong. The layout-brief generator clamps off-canvas zones at
    consumption time as a safety net.
    """
    if not spec_lock or "page_zones" not in spec_lock:
        return
    try:
        import yaml
        doc = yaml.safe_load(spec_lock)
    except (ImportError, Exception):
        return
    if not isinstance(doc, dict):
        return
    zones_block = doc.get("page_zones")
    if not isinstance(zones_block, dict):
        return

    offenders: list[dict] = []
    for page_key, page_zones in zones_block.items():
        if not isinstance(page_zones, dict):
            continue
        title_y_range = None
        chapter_y_range = None
        pn_width = None
        for role, bbox in page_zones.items():
            if not isinstance(bbox, dict):
                continue
            try:
                x = int(bbox["x"]); y = int(bbox["y"])
                w = int(bbox["w"]); h = int(bbox["h"])
            except (KeyError, ValueError, TypeError):
                continue
            if x < 0 or y < 0 or x + w > 1280 or y + h > 720:
                offenders.append({
                    "page": str(page_key),
                    "role": str(role),
                    "issue": "off_canvas",
                    "bbox": (x, y, w, h),
                })
            if role == "page_number":
                pn_width = w
            if role == "title":
                title_y_range = (y, y + h)
            if role == "chapter_label":
                chapter_y_range = (y, y + h)
        if pn_width is not None and pn_width < 130:
            offenders.append({
                "page": str(page_key),
                "role": "page_number",
                "issue": "page_number_too_narrow",
                "actual_width": pn_width,
                "minimum_required": 130,
            })
        if title_y_range and chapter_y_range:
            t0, t1 = title_y_range
            c0, c1 = chapter_y_range
            if t0 < c1 and c0 < t1:
                offenders.append({
                    "page": str(page_key),
                    "issue": "title_and_chapter_overlap_y",
                    "title_y": title_y_range,
                    "chapter_y": chapter_y_range,
                })

    if offenders:
        warnings.append(
            ValidationWarning(
                code="spec_validator_page_zones",
                message=(
                    f"{len(offenders)} page_zones entries have problems — "
                    "off-canvas, page_number too narrow, or title/chapter y-range "
                    f"overlap. (page_zones 항목 {len(offenders)}개 문제)"
                ),
                detail={"offenders": offenders[:10]},
            )
        )


# ---------------------------------------------------------------------------
# Hex color normalisation
# ---------------------------------------------------------------------------


def _normalise_hex_colors(
    spec_lock: str, warnings: list[ValidationWarning]
) -> str:
    """Replace short-hand and rgb(...) values with `#RRGGBB`.

    The Executor passes hex literals straight into SVG `fill` /
    `stroke`. Browser SVG accepts the shorthands but the DrawingML
    converter's hex parser doesn't (it needs the literal 6-char form),
    and the legacy quality checker outright bans `rgb(...)` /
    `rgba(...)`. Normalising here avoids both failure modes.
    """
    fixes = 0

    def upper_long(m: re.Match) -> str:
        nonlocal fixes
        new = "#" + m.group(1).upper()
        if new != m.group(0):
            fixes += 1
        return new

    def expand_short(m: re.Match) -> str:
        nonlocal fixes
        fixes += 1
        a, b, c = m.group(1), m.group(2), m.group(3)
        return "#" + (a + a + b + b + c + c).upper()

    def rgb_to_hex(m: re.Match) -> str:
        nonlocal fixes
        fixes += 1
        r, g, b = (max(0, min(255, int(v))) for v in m.groups())
        return f"#{r:02X}{g:02X}{b:02X}"

    spec_lock = _RE_RGBA.sub(rgb_to_hex, spec_lock)
    spec_lock = _RE_RGB.sub(rgb_to_hex, spec_lock)
    spec_lock = _RE_HEX_SHORT.sub(expand_short, spec_lock)
    spec_lock = _RE_HEX_LONG.sub(upper_long, spec_lock)

    if fixes:
        warnings.append(
            ValidationWarning(
                code="spec_validator_hex_normalised",
                message=(
                    f"Normalized {fixes} color(s) to standard 6-digit HEX. "
                    f"({fixes}개 색상 정규화)"
                ),
                detail={"fixes": fixes},
            )
        )
    return spec_lock


# ---------------------------------------------------------------------------
# Icon inventory resolution
# ---------------------------------------------------------------------------

# Icon entries inside spec_lock typically appear as:
#   - chunk-filled/alert-triangle
#   - tabler-outline/brain
# So we sniff anywhere in the text for `<lib>/<name>` tokens that match
# our library naming convention. Conservative: 3-30 char names, lib
# folder must end in `-filled`, `-outline`, `-duotone`, or be one of
# the explicit lists.
_RE_ICON_TOKEN = re.compile(
    r"\b(chunk-filled|tabler-filled|tabler-outline|phosphor-duotone|simple-icons)/([a-z0-9][a-z0-9-]{2,29})\b"
)


def _resolve_icons(
    spec_lock: str,
    icons_dir: Path | None,
    warnings: list[ValidationWarning],
) -> str:
    """For each `<lib>/<name>` token mentioned in spec_lock, verify the
    file exists in ``icons_dir/<lib>/``. Substitute the closest fuzzy
    match when it doesn't (we share the resolver with the SVG-level
    use_expander)."""
    if icons_dir is None or not icons_dir.exists():
        return spec_lock

    from ..core.svg_to_pptx.use_expander import _fuzzy_resolve

    substitutions: dict[str, str] = {}
    dropped: list[str] = []

    def _resolve_token(m: re.Match) -> str:
        token = f"{m.group(1)}/{m.group(2)}"
        if token in substitutions:
            return substitutions[token]
        exact = icons_dir / m.group(1) / f"{m.group(2)}.svg"
        if exact.exists():
            return token
        fuzzy = _fuzzy_resolve(token, icons_dir)
        if fuzzy is not None:
            substitute = f"{m.group(1)}/{fuzzy.stem}"
            substitutions[token] = substitute
            return substitute
        dropped.append(token)
        substitutions[token] = token  # leave verbatim — surface as warning
        return token

    new_spec = _RE_ICON_TOKEN.sub(_resolve_token, spec_lock)

    swaps = {k: v for k, v in substitutions.items() if k != v}
    if swaps:
        warnings.append(
            ValidationWarning(
                code="spec_validator_icon_substituted",
                message=(
                    f"Auto-replaced {len(swaps)} icon name(s) with the closest "
                    f"existing library icons. (아이콘 {len(swaps)}개 자동 교체)"
                ),
                detail={"substitutions": swaps},
            )
        )
    if dropped:
        warnings.append(
            ValidationWarning(
                code="spec_validator_icon_missing",
                message=(
                    f"{len(dropped)} icon(s) not found in the library — the "
                    f"Executor will leave those slots empty. (아이콘 {len(dropped)}개 미발견)"
                ),
                detail={"missing": dropped},
            )
        )
    return new_spec


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


def _check_required_fields(
    spec_lock: str, warnings: list[ValidationWarning]
) -> None:
    """Surface obviously-missing top-level sections. Cheap regex scan
    (not a full YAML parse) so we tolerate the markdown form too."""
    lowered = spec_lock.lower()
    missing: list[str] = []
    for label in ("canvas", "colors", "typography"):
        if not re.search(rf"(?:^|\n)\s*##?\s*{label}\b", lowered) and not re.search(
            rf"(?:^|\n){label}\s*:", lowered
        ):
            missing.append(label)
    if missing:
        warnings.append(
            ValidationWarning(
                code="spec_validator_missing_section",
                message=(
                    f"spec_lock is missing required sections: {', '.join(missing)} — "
                    "the Executor falls back to defaults. (필수 섹션 누락)"
                ),
                detail={"missing": missing},
            )
        )


# ---------------------------------------------------------------------------
# Windows-safe font tails
# ---------------------------------------------------------------------------

_RE_FONT_STACK = re.compile(
    r"""(?:font[-_]?stack|stack|family)\s*:\s*['"]?([^\n"'#]+)['"]?""",
    re.IGNORECASE,
)


def _check_windows_safe_font_tails(
    spec_lock: str, warnings: list[ValidationWarning]
) -> None:
    """For every `font_stack: '...'` line, verify the last concrete
    family is in the Windows-safe set. Generic `sans-serif` / `serif` /
    `monospace` are dropped before the check."""
    offenders: list[str] = []
    for stack in _RE_FONT_STACK.findall(spec_lock):
        parts = [p.strip().strip("'\"").lower() for p in stack.split(",") if p.strip()]
        # Drop generic terminators.
        concrete = [p for p in parts if p not in {"sans-serif", "serif", "monospace"}]
        if not concrete:
            continue
        if concrete[-1] not in _WIN_SAFE_TAILS:
            offenders.append(stack.strip())
    if offenders:
        warnings.append(
            ValidationWarning(
                code="spec_validator_font_tail_unsafe",
                message=(
                    f"{len(offenders)} font stack(s) do not end with a Windows-safe "
                    "family — machines without e.g. Pretendard will fall back to a "
                    f"system font. (폰트 스택 {len(offenders)}개)"
                ),
                detail={"stacks": offenders[:5]},
            )
        )
