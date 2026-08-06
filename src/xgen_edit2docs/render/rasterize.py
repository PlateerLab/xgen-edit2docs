"""SVG → PNG / PDF rasterization (resvg + PyMuPDF).

Backend choice is deliberate (verified by spike, 2026-07-05):

- ``resvg`` (via the self-contained ``resvg-py`` wheel) renders the
  SVG our converters emit — linear/radial gradients, clipPath,
  stroke-dasharray, CJK text — faithfully, with no system libraries.
- PyMuPDF's built-in SVG parser was disqualified: it rasterizes
  gradient/pattern fills as black boxes. It is still the right tool
  for the *assembly* step (PNG pages → one PDF), which is pure image
  placement.

DPI semantics: SVG user units are treated as CSS px (96/inch), so a
960×720 slide at ``dpi=144`` rasterizes to 1440×1080. PDF pages keep
the SVG's aspect ratio at its natural point size (1 px = 0.75 pt).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence

_BASE_DPI = 96.0
_DEFAULT_DPI = 144.0


def _font_dirs_from_env() -> list[str]:
    raw = os.environ.get("E2D_FONT_DIRS", "")
    return [d for d in raw.split(os.pathsep) if d.strip()]


def svg_to_png(
    svg: str,
    *,
    dpi: float = _DEFAULT_DPI,
    background: str | None = None,
    font_dirs: Sequence[str] | None = None,
) -> bytes:
    """Rasterize one SVG document to PNG bytes.

    ``font_dirs`` supplements the system font directories resvg scans
    by default; the ``E2D_FONT_DIRS`` environment variable (pathsep-
    separated) is always appended so deployments can mount brand fonts
    without code changes.
    """
    import resvg_py

    dirs = [*(font_dirs or []), *_font_dirs_from_env()]
    data = resvg_py.svg_to_bytes(
        svg_string=svg,
        zoom=float(dpi) / _BASE_DPI,
        background=background,
        font_dirs=dirs or None,
    )
    return bytes(data)


def svgs_to_pngs(
    svgs: Iterable[str],
    out_dir: str | Path,
    *,
    dpi: float = _DEFAULT_DPI,
    stem: str = "page",
    background: str | None = "#ffffff",
    font_dirs: Sequence[str] | None = None,
) -> list[Path]:
    """Rasterize pages to ``<out_dir>/<stem>-1.png`` … (1-based).

    The ``page-N.png`` naming intentionally matches what pdftoppm
    produced, so hosts that paged over LibreOffice previews (e.g.
    Geny's CanvasTab) work unchanged. Pre-existing ``<stem>-*.png``
    files are removed first — a shrunk document must not leave stale
    trailing pages behind.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob(f"{stem}-*.png"):
        old.unlink(missing_ok=True)
    paths: list[Path] = []
    for i, svg in enumerate(svgs, 1):
        png = svg_to_png(svg, dpi=dpi, background=background, font_dirs=font_dirs)
        p = out / f"{stem}-{i}.png"
        p.write_bytes(png)
        paths.append(p)
    return paths


def svgs_to_pdf(
    svgs: Sequence[str],
    *,
    dpi: float = _DEFAULT_DPI,
    background: str | None = "#ffffff",
    font_dirs: Sequence[str] | None = None,
) -> bytes:
    """Assemble pages into one PDF (raster pages at *dpi*).

    Raster-page PDFs are the deliberate M1 default: they render
    identically everywhere and need zero extra dependencies. A vector
    backend (cairosvg, system cairo) can slot in later behind the same
    signature — see docs/native-render-plan.md.
    """
    import fitz  # PyMuPDF — core dependency

    if not svgs:
        raise ValueError("svgs_to_pdf needs at least one SVG page")

    scale = 72.0 / float(dpi)  # px at *dpi* → PDF points
    doc = fitz.open()
    try:
        for svg in svgs:
            png = svg_to_png(svg, dpi=dpi, background=background, font_dirs=font_dirs)
            # Pixmap gives raw pixel dimensions — fitz.open() would
            # reinterpret them through the PNG's embedded DPI metadata
            # and shrink/grow the page.
            pix = fitz.Pixmap(png)
            w_pt = pix.width * scale
            h_pt = pix.height * scale
            page = doc.new_page(width=w_pt, height=h_pt)
            page.insert_image(fitz.Rect(0, 0, w_pt, h_pt), stream=png)
        return doc.tobytes(deflate=True)
    finally:
        doc.close()
