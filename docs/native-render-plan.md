# Native Render Plan — LibreOffice-free PNG/PDF for all formats

Goal: make xgen_edit2docs the complete rendering engine for docx/xlsx/pptx so
hosts (Geny et al.) can drop LibreOffice + poppler entirely. One
architecture: **every format renders to per-page SVG (single IR), then a
thin raster layer produces PNG/PDF.**

Verified by spikes (2026-07-05):

- **resvg-py** (Rust resvg, self-contained pip wheel, zero system deps)
  renders our real slide SVGs — gradients, clipPath, dash, CJK text —
  pixel-faithfully. PyMuPDF's own SVG parser was disqualified (renders
  gradients/clips as black boxes).
- **PyMuPDF** (already a core dependency) assembles PNG pages into PDF.
- **fontTools** (pure python) reads system Noto CJK TTCs and yields real
  advance widths → replaces the fixed-multiplier `_char_width` heuristic
  for accurate line wrap.
- Chart OOXML (`c:barChart > c:ser > c:cat/c:val > c:pt`) is compact and
  deterministic — a native chart→SVG renderer is tractable.

## Milestones

### M1 — Render backbone (v0.5.0)  ← this cycle
- `xgen_edit2docs/render/` package:
  - `rasterize.py` — `svg_to_png()` (resvg-py), `svgs_to_pdf()`
    (PNG pages assembled by PyMuPDF), `svgs_to_pngs()`.
  - `fonts.py` — `FontResolver`: discovers system/env font dirs,
    resolves family→file (TTC-aware), caches fontTools metrics,
    `text_width(text, family, size)` with the old heuristic as fallback.
- New unified verb **`render_doc(doc, to="png"|"pdf"|"svg", out_dir=,
  dpi=)`** — pptx works end-to-end now (convert→SVG→raster); docx/xlsx
  raise a clear not-yet error until M3/M4.
- Deps: `resvg-py>=0.3`, `fonttools>=4.50` added to core (both pure wheels).

### M2 — PPTX fidelity (v0.5.x)
- Metric-based line wrap: swap `txbody_to_svg._char_width` heuristics for
  `render.fonts` measurements (heuristic stays as fallback).
- **Chart renderer** `core/pptx_to_svg/chart_to_svg.py`: column/bar
  (clustered+stacked), line, pie/doughnut, area, scatter — axes, legend,
  data labels, theme palette. Replaces the dashed placeholder.
- Table theme-style baseline (header emphasis + banding) so default
  PowerPoint tables don't render borderless.
- SmartArt: extract dgm text → basic auto-layout (list/process boxes).
  Explicit non-goal: pixel-faithful SmartArt.
- EMF/WMF: keep ImageMagick optional; document.

### M3 — DOCX page engine (v0.6.0)
- `documents/docx_pages.py`: sectPr page size/margins/orientation →
  flow paragraphs (metric wrap, heading/list/quote styles), tables
  (merges, column widths), inline images, headers/footers, page breaks
  → per-page SVG carrying the same `data-e2d-*` addresses as the HTML
  preview. `render_doc` gains docx.

### M4 — XLSX grid engine (v0.6.x)
- Number-format subset (`#,##0.00`, `0%`, currency, dates), column
  widths/row heights, cell fill/font/border, print-area pagination →
  per-page SVG. Same improvements applied to `xlsx_to_html`.
  `render_doc` gains xlsx.

### M5 — Surfaces + host migration
- `/v1/preview` optional `format=png|pdf`; agent tool `render_doc`;
  Geny `doc_convert` pdf/png → `render_doc`, `_regen_preview` → native
  SVG/HTML, drop LibreOffice+poppler from the image (legacy .ppt/.odt
  ingest stays text-only via the converter).

## Rasterization policy
- PNG: resvg-py, default dpi 144 (px = pt * dpi/72), fonts resolved from
  system dirs + `E2D_FONT_DIRS`.
- PDF: raster pages at dpi≥144 assembled by PyMuPDF (searchable-text
  vector PDF is a later optional backend via cairosvg when system cairo
  exists — never a core requirement).
