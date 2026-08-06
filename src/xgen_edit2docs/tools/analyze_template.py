"""Analyze-template tool: user-provided PPTX bytes -> Strategist context.

Deterministic (no LLM): wraps ``core.template_import.manifest.build_manifest``
behind the tool layer's bytes-in / values-out contract. The orchestrator
runs this as the ``analyzing_template`` stage when the caller supplied a
template PPTX, then threads ``template_context`` into the Strategist and
``canvas_format`` / host dimensions into the Executor + export stages.
"""

from __future__ import annotations

import time

from pydantic import Field

from ..core.template_import.context import (
    TemplateCanvasError,
    build_template_context,
    resolve_template_canvas,
)
from ..core.template_import.manifest import build_manifest
from ._workspace import temp_workspace
from .types import (
    CanvasFormat,
    CostBreakdown,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)

DeckMode = str  # "template_restyle" | "template_extend" (validated upstream)


class AnalyzeTemplateRequest(ToolRequest):
    pptx: bytes = Field(..., description="The user-provided template PPTX bytes.")
    deck_mode: str = Field(
        default="template_restyle",
        description="template_restyle | template_extend — tweaks the Strategist guidance.",
    )


class AnalyzeTemplateResponse(ToolResponse):
    template_context: str = Field(
        ..., description="Markdown digest for the Strategist user message."
    )
    canvas_format: CanvasFormat
    host_width_px: int
    host_height_px: int
    slide_count: int
    theme_colors: dict[str, str] = Field(default_factory=dict)
    theme_fonts: dict[str, str] = Field(default_factory=dict)
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def analyze_template(req: AnalyzeTemplateRequest) -> AnalyzeTemplateResponse:
    """Extract theme + structure from the template PPTX.

    Raises:
        ValueError: the bytes are not a readable PPTX, or its slide size
            maps onto no supported canvas (16:9 / 4:3). The error message
            is bilingual so API layers can surface it directly.
    """
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    with temp_workspace(prefix="xgen_edit2docs-template-") as ws:
        pptx_path = ws / "template.pptx"
        pptx_path.write_bytes(req.pptx)
        out_dir = ws / "analysis"
        out_dir.mkdir()
        try:
            manifest = build_manifest(pptx_path, out_dir)
        except TemplateCanvasError:
            raise
        except Exception as exc:
            raise ValueError(
                f"Template PPTX could not be analyzed: {exc}. "
                "템플릿 PPTX 파일을 분석할 수 없습니다 — 올바른 .pptx 파일인지 확인하세요."
            ) from exc

    canvas_format, host_w, host_h = resolve_template_canvas(manifest)
    theme = manifest.get("theme") or {}
    slides = manifest.get("slides") or []

    if not (theme.get("colors") or theme.get("fonts")):
        warnings.append(
            WarningEntry(
                code="template_theme_not_detected",
                message=(
                    "No theme colors/fonts were detected in the template; the "
                    "Strategist will fall back to its own palette."
                ),
            )
        )

    return AnalyzeTemplateResponse(
        template_context=build_template_context(manifest, deck_mode=req.deck_mode),
        canvas_format=canvas_format,  # type: ignore[arg-type]
        host_width_px=host_w,
        host_height_px=host_h,
        slide_count=len(slides),
        theme_colors=dict(theme.get("colors") or {}),
        theme_fonts=dict(theme.get("fonts") or {}),
        cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
        warnings=warnings,
    )
