"""Export tool: render finalized SVGs into a PPTX (with Korean lang propagation).

This is the M1 capstone tool: it stitches together the core engine's
`create_pptx_with_native_svg` builder, hides the disk I/O behind a temp
workspace, and exposes a clean Pydantic-typed function the rest of the
server (and tests) can call.

The `lang` parameter threads through to OOXML `<a:rPr lang="...">` via the
G2 patch in `core/svg_to_pptx/{pptx_notes,drawingml_elements}.py`. When the
caller doesn't pass `lang`, each text run's language is detected from its
content.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import Field

from ..core.svg_to_pptx.pptx_builder import create_pptx_with_native_svg
from ..core.svg_to_pptx.drawingml_utils import detect_lang
from ._workspace import temp_workspace, write_text
from .types import (
    CanvasFormat,
    CostBreakdown,
    DEFAULT_CANVAS,
    DEFAULT_LANG,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)


class SlideInput(ToolRequest):
    """A single slide's rendered SVG + optional speaker notes."""

    index: int = Field(..., description="0-based slide index", ge=0)
    name: str = Field(..., description="Slide stem (used inside the PPTX rels)")
    svg: str = Field(..., description="Fully-rendered SVG markup")
    notes: str | None = Field(default=None, description="Speaker notes (Markdown)")


class ExportRequest(ToolRequest):
    """Inputs for `export_pptx`."""

    slides: list[SlideInput]
    canvas_format: CanvasFormat = DEFAULT_CANVAS
    lang: LangCode = Field(
        default=DEFAULT_LANG,
        description="OOXML lang attribute. ko-KR by default. Auto-detected per-run when None.",
    )
    transition: str | None = "fade"
    transition_duration: float = 0.5
    animation: str | None = None
    animation_duration: float = 0.4
    animation_stagger: float = 0.5
    animation_trigger: str = "after-previous"
    enable_notes: bool = True
    use_native_shapes: bool = True
    use_compat_mode: bool = True
    native_objects: bool = Field(
        default=False,
        description=(
            "Opt in to converting explicit data-pptx-native table/chart marker "
            "groups into native PowerPoint objects (graphicFrame table / chart "
            "part + embedded workbook). Default off: marked groups export "
            "through their SVG fallback children as plain shapes."
        ),
    )

    # Per-deck media assets. Keyed by the filename the SVGs reference, e.g.
    # `<image href="hero_cover.png">` -> images={"hero_cover.png": <png bytes>}.
    # Files land alongside the SVGs in the workspace; the engine embeds them
    # via svg_finalize / pptx_builder. ASCII-only filenames (Track A).
    images: dict[str, bytes] = Field(default_factory=dict)

    # Per-slide narration audio (MP3 bytes). Keys are the slide names that
    # match `SlideInput.name`. When non-empty the resulting PPTX embeds the
    # audio so PowerPoint can auto-play it during slide transitions.
    narration_audio: dict[str, bytes] = Field(default_factory=dict)
    narration_padding: float = Field(default=0.5, ge=0.0)
    use_narration_timings: bool = False

    # Template modes: when `host_pptx` is set the slides are spliced into
    # this user-provided package (masters/layouts/theme preserved) instead
    # of a fresh python-pptx deck. `clear_existing_slides=True` drops the
    # host's original slides afterwards (template_restyle). `host_px`
    # rescales each SVG from the canonical canvas to the host's real
    # pixel dimensions before DrawingML conversion.
    host_pptx: bytes | None = None
    clear_existing_slides: bool = False
    host_px: tuple[int, int] | None = None


class ExportResponse(ToolResponse):
    pptx: bytes
    page_count: int
    detected_langs: list[LangCode] = Field(
        default_factory=list,
        description="Per-slide language inferred from svg text content; useful for QA.",
    )
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def export_pptx(req: ExportRequest) -> ExportResponse:
    """Render a deck of SVGs into an editable PPTX.

    The deck's `lang` is applied to OOXML rPr blocks; per-run detection still
    fires when a slide contains text in a different script (e.g. an English
    chart label inside an otherwise-Korean deck — that run gets lang="en-US").

    Raises:
        ValueError: if `slides` is empty.
        RuntimeError: if the underlying engine returns failure.
    """
    if not req.slides:
        raise ValueError("export_pptx requires at least one slide")

    started = time.perf_counter()
    warnings: list[WarningEntry] = []
    detected: list[LangCode] = []

    with temp_workspace(prefix="xgen_edit2docs-export-") as ws:
        svg_dir = ws / "svgs"
        svg_dir.mkdir()
        svg_paths = []
        notes_map: dict[str, str] = {}

        # Drop image files alongside the SVGs so <image href="<name>.png">
        # resolves correctly during svg_finalize/embed_images.
        for filename, content in req.images.items():
            _validate_ascii_filename(filename)
            (svg_dir / filename).write_bytes(content)

        # Sort by index so the resulting deck is deterministic regardless of caller order.
        for slide in sorted(req.slides, key=lambda s: s.index):
            svg_markup = slide.svg
            if req.host_pptx is not None and req.host_px is not None:
                # Template mode: rescale from the canonical canvas to the
                # host deck's real dimensions so 1 px = 9525 EMU lands
                # exactly on the host slide.
                from ..core.svg_to_pptx.svg_scale import scale_svg_to_viewbox

                svg_markup = scale_svg_to_viewbox(
                    svg_markup, float(req.host_px[0]), float(req.host_px[1])
                )
            svg_path = write_text(svg_dir, f"{slide.name}.svg", svg_markup)
            svg_paths.append(svg_path)
            if slide.notes:
                notes_map[slide.name] = slide.notes
            detected.append(detect_lang(slide.svg, default=req.lang))  # type: ignore[arg-type]

        if req.host_pptx is not None:
            return _export_into_host(req, ws, svg_paths, notes_map, detected, started)

        # Narration audio: write MP3s and build the dict the engine expects
        # ({svg_stem: Path}). Keys must already match slide names.
        narration_map: dict[str, "Path"] = {}
        if req.narration_audio:
            audio_dir = ws / "audio"
            audio_dir.mkdir()
            for slide_name, mp3_bytes in req.narration_audio.items():
                _validate_ascii_filename(slide_name)
                path = audio_dir / f"{slide_name}.mp3"
                path.write_bytes(mp3_bytes)
                narration_map[slide_name] = path

        output_path = ws / "output.pptx"
        ok = create_pptx_with_native_svg(
            svg_files=svg_paths,
            output_path=output_path,
            canvas_format=req.canvas_format,
            verbose=False,
            transition=req.transition,
            transition_duration=req.transition_duration,
            use_compat_mode=req.use_compat_mode,
            notes=notes_map or None,
            enable_notes=req.enable_notes,
            use_native_shapes=req.use_native_shapes,
            animation=req.animation,
            animation_duration=req.animation_duration,
            animation_stagger=req.animation_stagger,
            animation_trigger=req.animation_trigger,
            narration_audio=narration_map or None,
            use_narration_timings=req.use_narration_timings,
            narration_padding=req.narration_padding,
            native_objects=req.native_objects,
        )
        if not ok:
            raise RuntimeError("core engine reported failure during PPTX assembly")
        pptx_bytes = output_path.read_bytes()

    duration = time.perf_counter() - started
    return ExportResponse(
        pptx=pptx_bytes,
        page_count=len(req.slides),
        detected_langs=detected,
        cost=CostBreakdown(duration_seconds=duration),
        warnings=warnings,
    )


def _export_into_host(
    req: ExportRequest,
    ws: Path,
    svg_paths: list[Path],
    notes_map: dict[str, str],
    detected: list[LangCode],
    started: float,
) -> ExportResponse:
    """Template-mode export: splice the SVG slides into the host PPTX.

    Narration audio is not embedded in template modes (v1) — the raw
    append path doesn't carry the narration timing machinery. Callers
    that requested it get a warning instead of an error.
    """
    from ..core.svg_to_pptx.pptx_append import append_svg_slides_to_pptx

    warnings: list[WarningEntry] = []
    if req.narration_audio:
        warnings.append(
            WarningEntry(
                code="template_narration_unsupported",
                message=(
                    "Narration audio is not embedded in template modes yet; "
                    "the deck exports without audio. 템플릿 모드에서는 나레이션 "
                    "오디오 임베드가 아직 지원되지 않아 오디오 없이 내보냅니다."
                ),
            )
        )

    host_path = ws / "host.pptx"
    host_path.write_bytes(req.host_pptx or b"")
    output_path = ws / "output.pptx"
    append_warnings = append_svg_slides_to_pptx(
        host_path,
        svg_paths,
        output_path,
        clear_existing=req.clear_existing_slides,
        notes=notes_map or None,
        enable_notes=req.enable_notes,
        lang=req.lang,
        transition=req.transition,
        transition_duration=req.transition_duration,
        verbose=False,
    )
    for w in append_warnings:
        warnings.append(WarningEntry(code=w["code"], message=w["message"]))

    return ExportResponse(
        pptx=output_path.read_bytes(),
        page_count=len(req.slides),
        detected_langs=detected,
        cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
        warnings=warnings,
    )


def _validate_ascii_filename(name: str) -> None:
    """Track A: filenames written into the export workspace must be ASCII."""
    try:
        name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"Workspace filenames must be ASCII (got {name!r}); "
            "Korean text belongs in slide content, not in resource filenames."
        ) from exc
    if "/" in name or "\\" in name or name.startswith(".."):
        raise ValueError(f"Workspace filename must be a single component: {name!r}")
