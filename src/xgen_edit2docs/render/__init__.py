"""Native raster layer — per-page SVG → PNG / PDF without LibreOffice.

M1 of docs/native-render-plan.md. Every document format converges on
per-page SVG (the pptx pipeline already emits it; docx/xlsx page
engines land in M3/M4), and this package turns those pages into
deliverables:

- :func:`svg_to_png` / :func:`svgs_to_pngs` — resvg (self-contained
  Rust rasterizer wheel; gradients/clipPath/CJK verified) at a chosen
  DPI.
- :func:`svgs_to_pdf` — raster pages assembled into a PDF by PyMuPDF
  (already a core dependency).
- :class:`FontResolver` — system-font discovery + fontTools metrics so
  layout code can measure real advance widths instead of the fixed
  per-character heuristics.
"""

from xgen_edit2docs.render.fonts import FontResolver, default_font_resolver
from xgen_edit2docs.render.rasterize import (
    svg_to_png,
    svgs_to_pdf,
    svgs_to_pngs,
)

__all__ = [
    "FontResolver",
    "default_font_resolver",
    "svg_to_png",
    "svgs_to_pdf",
    "svgs_to_pngs",
]
