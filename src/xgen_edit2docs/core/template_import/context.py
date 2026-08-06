"""Turn a template-import manifest into pipeline-ready context.

Two consumers:

* ``build_template_context`` renders a compact markdown digest of the
  user-provided PPTX (theme colors, fonts, canvas, page-type inventory,
  text tone samples) that the Strategist receives verbatim. It is
  deterministic — no LLM in the loop — so the same template always
  produces the same context.
* ``resolve_template_canvas`` maps the template's slide size onto one of
  the engine's canonical canvas formats. The Executor keeps emitting at
  the canonical viewBox; the export stage rescales to the host deck's
  real pixel dimensions (see ``core.svg_to_pptx.svg_scale``).
"""

from __future__ import annotations

from typing import Any

# Canonical Executor canvases the template pipeline can target. Other
# engine formats (xhs / story / wechat) are social-media canvases that
# user decks never use, so they are deliberately not listed.
_TEMPLATE_CANVASES: dict[str, tuple[int, int]] = {
    "ppt169": (1280, 720),
    "ppt43": (1024, 768),
}

# A slide size counts as "matching" a canvas when the aspect ratios
# differ by at most this fraction. 2% absorbs EMU rounding in real decks.
_ASPECT_TOLERANCE = 0.02


class TemplateCanvasError(ValueError):
    """The template's slide size doesn't map onto a supported canvas."""

    def __init__(self, width_px: int, height_px: int) -> None:
        self.width_px = width_px
        self.height_px = height_px
        super().__init__(
            f"Template slide size {width_px}x{height_px}px matches neither 16:9 nor 4:3 "
            "(supported template canvases). "
            f"템플릿 슬라이드 크기 {width_px}x{height_px}px 가 16:9 / 4:3 어느 쪽에도 "
            "해당하지 않아 템플릿 모드로 생성할 수 없습니다."
        )


def resolve_template_canvas(manifest: dict[str, Any]) -> tuple[str, int, int]:
    """Return ``(canvas_format, host_width_px, host_height_px)``.

    ``host_*_px`` are the template's real slide dimensions (EMU / 9525);
    the export stage scales canonical-canvas SVGs to exactly these before
    converting to DrawingML, so shapes land on the host slide precisely.

    Raises:
        TemplateCanvasError: when the slide aspect matches no supported canvas.
    """
    size = manifest.get("slideSize") or {}
    width_px = int(size.get("width_px") or 0)
    height_px = int(size.get("height_px") or 0)
    if width_px <= 0 or height_px <= 0:
        raise TemplateCanvasError(width_px, height_px)

    aspect = width_px / height_px
    for canvas_format, (cw, ch) in _TEMPLATE_CANVASES.items():
        if abs(aspect - cw / ch) <= _ASPECT_TOLERANCE * (cw / ch):
            return canvas_format, width_px, height_px
    raise TemplateCanvasError(width_px, height_px)


def build_template_context(manifest: dict[str, Any], *, deck_mode: str = "template_restyle") -> str:
    """Render the Strategist-facing markdown digest of the template.

    ``deck_mode`` tweaks the guidance line: in ``template_extend`` the
    generated slides land inside the user's deck, whose masters/layouts
    already draw background chrome (logos, footers) behind every slide.
    """
    theme = manifest.get("theme") or {}
    colors: dict[str, str] = theme.get("colors") or {}
    fonts: dict[str, str] = theme.get("fonts") or {}
    size = manifest.get("slideSize") or {}
    slides: list[dict[str, Any]] = manifest.get("slides") or []
    page_types: dict[str, list[int]] = manifest.get("pageTypeCandidates") or {}
    source_name = (manifest.get("source") or {}).get("name", "template.pptx")

    lines: list[str] = [
        f"The user uploaded `{source_name}` as the design template for this deck.",
        "Adopt its visual identity: reuse the theme palette and fonts below in the",
        "spec_lock instead of inventing new ones. Keep the tone of the text samples.",
        "",
        "## Canvas",
        f"- Slide size: {size.get('width_px', '?')} x {size.get('height_px', '?')} px",
        f"- Slide count: {len(slides)}",
    ]

    if colors:
        lines.append("")
        lines.append("## Theme colors (OOXML scheme slot -> hex)")
        # Scheme-slot order is meaningful: dk/lt are text/background pairs,
        # accent1..6 are the brand accents the Strategist should pick from.
        for name, value in colors.items():
            lines.append(f"- {name}: {value}")

    if fonts:
        lines.append("")
        lines.append("## Theme fonts")
        for slot, typeface in fonts.items():
            lines.append(f"- {slot}: {typeface}")

    if page_types:
        lines.append("")
        lines.append("## Page-type inventory (from the template's own slides)")
        for ptype, indexes in page_types.items():
            shown = ", ".join(str(i) for i in indexes[:12])
            suffix = ", ..." if len(indexes) > 12 else ""
            lines.append(f"- {ptype}: slides {shown}{suffix}")

    samples: list[str] = []
    for slide in slides:
        for text in slide.get("textSamples") or []:
            cleaned = " ".join(str(text).split())
            if cleaned and cleaned not in samples:
                samples.append(cleaned)
        if len(samples) >= 10:
            break
    if samples:
        lines.append("")
        lines.append("## Text samples (tone / voice reference)")
        for sample in samples[:10]:
            lines.append(f"- {sample[:120]}")

    lines.append("")
    if deck_mode == "template_extend":
        lines.append(
            "These new slides will be APPENDED into the user's existing deck. "
            "The deck's slide master already draws its background chrome (logos, "
            "footers, decorative shapes) behind every slide — prefer minimal, "
            "light background fills so the master chrome stays visible, and do "
            "not re-draw logos or footer text."
        )
    else:
        lines.append(
            "A fresh deck will be generated on top of this template's slide "
            "master, so its background chrome (logos, footers) appears behind "
            "every slide automatically. Design content that harmonises with the "
            "palette above; do not re-draw logos or footer text."
        )
    return "\n".join(lines)
