"""Post-export structural metrics over the final PPTX.

After the deck has been assembled and zipped, this module unpacks the
result in-memory and gathers slide-level statistics: shape count per
slide, embedded image count, color palette size, font set,
placeholder-slide count, and rough whitespace ratio.

The metrics serve two purposes:

* Surfaces "the deck has 1 placeholder slide" / "0 images embedded
  vs 4 planned" / "8 fonts in use" as warnings the operator can act
  on, even when no per-stage check fired.
* Lands on the API response (`GenerateDeckResponse.export_metrics`)
  so the web UI can show a quality summary card alongside the
  download button.

Best effort: zip / xml errors fall through to an empty metrics block
so an unparseable PPTX doesn't sink the whole response.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# Slide canvas in EMU for the standard 16:9 deck. Larger / smaller
# canvases scale linearly and the ratios we compute are unaffected.
_DEFAULT_CANVAS_EMU = (12192000, 6858000)


@dataclass
class ExportMetrics:
    """Structural summary of the assembled deck."""

    total_slides: int = 0
    placeholder_slides: int = 0
    embedded_images: int = 0
    avg_shapes_per_slide: float = 0.0
    color_palette_size: int = 0
    fonts_used: list[str] = field(default_factory=list)
    canvas_fill_ratio: float = 0.0  # avg(content area) / canvas area

    def to_dict(self) -> dict:
        return {
            "total_slides": self.total_slides,
            "placeholder_slides": self.placeholder_slides,
            "embedded_images": self.embedded_images,
            "avg_shapes_per_slide": round(self.avg_shapes_per_slide, 2),
            "color_palette_size": self.color_palette_size,
            "fonts_used": list(self.fonts_used),
            "canvas_fill_ratio": round(self.canvas_fill_ratio, 3),
        }


def compute_export_metrics(pptx_bytes: bytes) -> ExportMetrics:
    """Open the .pptx zip in memory and walk its slide XML.

    Each measurement is best-effort. If any sub-step throws, the
    relevant field is left at its default rather than propagating the
    exception — the metrics block is a diagnostic, not a hard contract.
    """
    metrics = ExportMetrics()
    try:
        zf = zipfile.ZipFile(io.BytesIO(pptx_bytes))
    except zipfile.BadZipFile:
        return metrics

    try:
        with zf:
            slide_names = sorted(
                n for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            media_names = [
                n for n in zf.namelist()
                if n.startswith("ppt/media/")
            ]

            metrics.total_slides = len(slide_names)
            metrics.embedded_images = len(media_names)

            shape_counts: list[int] = []
            content_ratios: list[float] = []
            colors: set[str] = set()
            fonts: set[str] = set()
            placeholders = 0

            for name in slide_names:
                try:
                    slide_xml = zf.read(name)
                    root = ET.fromstring(slide_xml)
                except (KeyError, ET.ParseError):
                    continue
                shape_count = _count_shapes(root)
                shape_counts.append(shape_count)
                _harvest_colors(root, colors)
                _harvest_fonts(root, fonts)
                if _looks_like_placeholder(root):
                    placeholders += 1
                content_ratios.append(_estimate_fill_ratio(root))

            if shape_counts:
                metrics.avg_shapes_per_slide = sum(shape_counts) / len(shape_counts)
            if content_ratios:
                metrics.canvas_fill_ratio = sum(content_ratios) / len(content_ratios)
            metrics.color_palette_size = len(colors)
            metrics.fonts_used = sorted(fonts)
            metrics.placeholder_slides = placeholders
    except Exception:
        # Anything unexpected — give back whatever we managed to fill.
        pass
    return metrics


def _count_shapes(root: ET.Element) -> int:
    return len(list(root.iter(f"{P_NS}sp"))) + len(list(root.iter(f"{P_NS}pic")))


def _harvest_colors(root: ET.Element, colors: set[str]) -> None:
    """Collect every srgbClr value mentioned in the slide. Different
    alpha variants of the same hue count as one — alpha is stored
    separately and would inflate the palette count otherwise."""
    for el in root.iter(f"{A_NS}srgbClr"):
        v = el.get("val")
        if v:
            colors.add(v.upper())


def _harvest_fonts(root: ET.Element, fonts: set[str]) -> None:
    """Collect every typeface mentioned via `<a:latin>` / `<a:ea>` /
    `<a:cs>`. Empty / generic faces are skipped."""
    for tag in ("latin", "ea", "cs"):
        for el in root.iter(f"{A_NS}{tag}"):
            face = el.get("typeface")
            if face and face.strip():
                fonts.add(face.strip())


def _looks_like_placeholder(root: ET.Element) -> bool:
    """Heuristic: a slide is a render-failure placeholder if its only
    `<p:sp>` carries the literal text we wrote in pptx_builder."""
    sp_list = list(root.iter(f"{P_NS}sp"))
    if len(sp_list) != 1:
        return False
    for t in sp_list[0].iter(f"{A_NS}t"):
        if t.text and "could not be rendered" in t.text:
            return True
        if t.text and "렌더링 실패" in t.text:
            return True
    return False


def _estimate_fill_ratio(root: ET.Element) -> float:
    """Sum of every shape's bbox area divided by the canvas area.

    Overlapping shapes inflate this above 1.0 — that's OK as a
    diagnostic ("dense / sparse / overdrawn"), it's not a strict
    metric. The slide background is excluded so it doesn't drown the
    signal.
    """
    canvas_area = _DEFAULT_CANVAS_EMU[0] * _DEFAULT_CANVAS_EMU[1]
    total = 0
    background_candidate = 0
    for sp in root.iter(f"{P_NS}sp"):
        ext = sp.find(f".//{P_NS}spPr/{A_NS}xfrm/{A_NS}ext")
        if ext is None:
            continue
        try:
            w = int(ext.get("cx", 0))
            h = int(ext.get("cy", 0))
        except ValueError:
            continue
        area = w * h
        if area >= canvas_area * 0.95:
            # Slide background fill — exclude.
            background_candidate = max(background_candidate, area)
            continue
        total += area
    if canvas_area == 0:
        return 0.0
    return total / canvas_area
