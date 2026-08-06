"""Render-preview tool: PPTX bytes -> self-contained per-slide SVGs.

Deterministic (no LLM). Powers the web studio's slide canvas and the
edit-deck pipeline's "current slide" context: each slide is rendered in
flat inheritance mode (master + layout shapes inlined) with images
base64-embedded, so a browser can display the SVG directly.
"""

from __future__ import annotations

import time

from pydantic import Field

from ..core.pptx_to_svg.converter import ConvertOptions, convert_pptx_to_svg
from ._workspace import temp_workspace
from .types import CostBreakdown, ToolRequest, ToolResponse, WarningEntry


class RenderPreviewRequest(ToolRequest):
    pptx: bytes = Field(..., description="The PPTX package to render.")
    max_slides: int = Field(
        default=100,
        ge=1,
        le=500,
        description="Safety cap; decks longer than this are truncated with a warning.",
    )


class SlidePreview(ToolResponse):
    index: int = Field(..., description="0-based slide position.")
    svg: str = Field(..., description="Self-contained SVG (images embedded).")


class RenderPreviewResponse(ToolResponse):
    slides: list[SlidePreview]
    width_px: float
    height_px: float
    page_count: int = Field(..., description="Total slides in the deck (pre-truncation).")
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def render_preview(req: RenderPreviewRequest) -> RenderPreviewResponse:
    """Convert every slide to a flat, self-contained SVG.

    Raises:
        ValueError: the bytes are not a readable PPTX (bilingual message).
    """
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    with temp_workspace(prefix="xgen_edit2docs-preview-") as ws:
        pptx_path = ws / "deck.pptx"
        pptx_path.write_bytes(req.pptx)
        out_dir = ws / "svg"
        out_dir.mkdir()
        try:
            result = convert_pptx_to_svg(
                pptx_path,
                out_dir,
                ConvertOptions(embed_images=True, inheritance_mode="flat"),
            )
        except Exception as exc:
            raise ValueError(
                f"PPTX could not be rendered for preview: {exc}. "
                "PPTX 파일을 미리보기로 변환할 수 없습니다 — 올바른 .pptx 파일인지 확인하세요."
            ) from exc

    # Pure flat mode populates `slides` with the flat view.
    artifacts = result.slides
    page_count = len(artifacts)
    if page_count > req.max_slides:
        warnings.append(
            WarningEntry(
                code="preview_truncated",
                message=(
                    f"Deck has {page_count} slides; preview truncated to the "
                    f"first {req.max_slides}."
                ),
                detail={"page_count": page_count, "max_slides": req.max_slides},
            )
        )
        artifacts = artifacts[: req.max_slides]

    return RenderPreviewResponse(
        slides=[
            SlidePreview(index=i, svg=a.svg) for i, a in enumerate(artifacts)
        ],
        width_px=result.canvas_px[0],
        height_px=result.canvas_px[1],
        page_count=page_count,
        cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
        warnings=warnings,
    )
