"""DOCX → per-page SVG (native-render plan M3).

A deterministic page-layout engine over python-docx's XML model: reads
``w:sectPr`` for the page geometry, flows paragraphs with measured
line wrap (``xgen_edit2docs.render.fonts`` — the same fonts resvg
rasterizes with), lays out tables/images/headers/footers, and emits
one self-contained SVG per page. ``render_doc`` feeds these to the
resvg/PyMuPDF raster layer for PNG/PDF — the piece LibreOffice used to
provide.

Fidelity scope (deliberate): body paragraphs (runs with
bold/italic/size/color/underline), heading styles, bullet/numbered
lists, hard + automatic page breaks, tables (tblGrid widths, gridSpan/
vMerge merges, cell shading), inline images (extent-scaled, base64),
single-section page size/margins, first-section header/footer text
with PAGE field support. Floating shapes, multi-column sections and
footnote blocks are out of scope — the HTML preview covers reading
those.

Addressing: each body paragraph's lines are wrapped in
``<g data-e2d-para="i">`` and table cells carry ``data-e2d-table`` /
``data-e2d-cell``, matching ``docx_outline`` / ``set_doc_text``
addresses (same convention as the HTML preview).
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Optional

from docx import Document

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


_TWIPS_PER_PX = 15.0  # 1440 twips/in ÷ 96 px/in
_EMU_PER_PX = 9525.0
_DEFAULT_FONT_PT = 11.0
_LINE_SPACING = 1.35
_PARA_GAP_PX = 6.0
_IMAGE_BUDGET_BYTES = 8 * 1024 * 1024
_MAX_PAGES = 200

_HEADING_PT = {1: 20.0, 2: 16.0, 3: 14.0, 4: 12.0, 5: 11.0, 6: 11.0}

_FONT_STACK = "'Noto Sans', 'Segoe UI', 'Noto Sans CJK KR', 'Malgun Gothic', sans-serif"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _f(v: float) -> str:
    out = f"{v:.2f}".rstrip("0").rstrip(".")
    return out or "0"


def _text_width(text: str, size_px: float, bold: bool, family: Optional[str]) -> float:
    """Measured width in px; falls back to the shared heuristic."""
    try:
        from xgen_edit2docs.render.fonts import default_font_resolver

        resolver = default_font_resolver()
    except Exception:  # noqa: BLE001
        resolver = None
    total = 0.0
    for ch in text:
        adv = resolver.char_advance(ch, family=family, size=size_px) if resolver else None
        if adv is None:
            code = ord(ch)
            if code > 0x2E80:
                adv = size_px
            elif ch == " ":
                adv = size_px * 0.3
            elif ch in "mMwWOQ":
                adv = size_px * 0.75
            elif ch in "iIlj1!|":
                adv = size_px * 0.3
            else:
                adv = size_px * 0.55
        if bold and ord(ch) <= 0x2E80:
            adv *= 1.03
        total += adv
    return total


@dataclass
class _Seg:
    """One styled text segment (a run, or a slice of one after wrap)."""

    text: str
    size_px: float
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = "#222222"
    family: Optional[str] = None

    def width(self) -> float:
        return _text_width(self.text, self.size_px, self.bold, self.family)


@dataclass
class _Line:
    segs: list[_Seg] = field(default_factory=list)

    @property
    def height(self) -> float:
        return max((s.size_px for s in self.segs), default=_DEFAULT_FONT_PT * 96 / 72) * _LINE_SPACING

    @property
    def ascent(self) -> float:
        return max((s.size_px for s in self.segs), default=_DEFAULT_FONT_PT * 96 / 72) * 0.85


class _PageWriter:
    """Accumulates SVG per page, opening new pages on demand."""

    def __init__(self, page_w: float, page_h: float, margins: dict,
                 header: list["_Seg"], footer: list["_Seg"]) -> None:
        self.page_w = page_w
        self.page_h = page_h
        self.m = margins
        self.header = header
        self.footer = footer
        self.pages: list[list[str]] = []
        self.y = 0.0
        self._open_para: Optional[int] = None
        self._new_page()

    # ── page lifecycle ──────────────────────────────────────

    def _chrome(self, parts: list[str], page_no: int) -> None:
        parts.append(
            f'<rect width="{_f(self.page_w)}" height="{_f(self.page_h)}" fill="#ffffff"/>'
        )
        if self.header:
            text = "".join(s.text for s in self.header).replace("￼PAGE￼", str(page_no))
            parts.append(
                f'<text x="{_f(self.page_w / 2)}" y="{_f(self.m["header"] + 10)}" '
                f'text-anchor="middle" font-size="9.5" fill="#8a8a8a" '
                f'font-family="{_FONT_STACK}">{_esc(text)}</text>'
            )
        if self.footer:
            text = "".join(s.text for s in self.footer).replace("￼PAGE￼", str(page_no))
            parts.append(
                f'<text x="{_f(self.page_w / 2)}" y="{_f(self.page_h - self.m["footer"])}" '
                f'text-anchor="middle" font-size="9.5" fill="#8a8a8a" '
                f'font-family="{_FONT_STACK}">{_esc(text)}</text>'
            )

    def _new_page(self) -> None:
        if len(self.pages) >= _MAX_PAGES:
            raise _PageBudgetExceeded()
        reopen = self._open_para
        if reopen is not None:
            self._close_para_group()
        parts: list[str] = []
        self._chrome(parts, len(self.pages) + 1)
        self.pages.append(parts)
        self.y = self.m["top"]
        if reopen is not None:
            self.open_para_group(reopen)

    @property
    def content_w(self) -> float:
        return self.page_w - self.m["left"] - self.m["right"]

    @property
    def bottom(self) -> float:
        return self.page_h - self.m["bottom"]

    def ensure(self, height: float) -> None:
        """Room for *height*; else new page (oversize blocks stay put)."""
        if self.y + height > self.bottom and self.y > self.m["top"] + 1:
            self._new_page()

    def page_break(self) -> None:
        self._new_page()

    # ── addressable paragraph groups ────────────────────────

    def open_para_group(self, para_idx: int) -> None:
        self.pages[-1].append(f'<g data-e2d-para="{para_idx}">')
        self._open_para = para_idx

    def _close_para_group(self) -> None:
        if self._open_para is not None:
            self.pages[-1].append("</g>")
            self._open_para = None

    def close_para_group(self) -> None:
        self._close_para_group()

    # ── primitives ──────────────────────────────────────────

    def emit_line(self, line: _Line, indent: float = 0.0) -> None:
        self.ensure(line.height)
        x = self.m["left"] + indent
        baseline = self.y + line.ascent
        for seg in line.segs:
            if seg.text:
                style = []
                if seg.italic:
                    style.append('font-style="italic"')
                if seg.bold:
                    style.append('font-weight="bold"')
                if seg.underline:
                    style.append('text-decoration="underline"')
                fam = f"'{seg.family}', {_FONT_STACK}" if seg.family else _FONT_STACK
                self.pages[-1].append(
                    f'<text x="{_f(x)}" y="{_f(baseline)}" font-size="{_f(seg.size_px)}" '
                    f'fill="{seg.color}" font-family="{fam}" {" ".join(style)} '
                    f'xml:space="preserve">{_esc(seg.text)}</text>'
                )
            x += seg.width()
        self.y += line.height

    def raw(self, markup: str) -> None:
        self.pages[-1].append(markup)

    def finish(self) -> list[str]:
        self._close_para_group()
        return [
            (
                f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                f'width="{_f(self.page_w)}" height="{_f(self.page_h)}" '
                f'viewBox="0 0 {_f(self.page_w)} {_f(self.page_h)}">'
                + "".join(parts)
                + "</svg>"
            )
            for parts in self.pages
        ]


class _PageBudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------------------
# Model extraction
# ---------------------------------------------------------------------------


def _section_geometry(body) -> tuple[float, float, dict]:
    sect = body.find(_w("sectPr"))
    pg_sz = sect.find(_w("pgSz")) if sect is not None else None
    pg_mar = sect.find(_w("pgMar")) if sect is not None else None

    def twips(el, attr, default):
        try:
            return float(el.get(_w(attr))) / _TWIPS_PER_PX
        except (TypeError, ValueError, AttributeError):
            return default / _TWIPS_PER_PX

    page_w = twips(pg_sz, "w", 11906)  # A4 portrait default
    page_h = twips(pg_sz, "h", 16838)
    margins = {
        "top": twips(pg_mar, "top", 1440),
        "bottom": twips(pg_mar, "bottom", 1440),
        "left": twips(pg_mar, "left", 1440),
        "right": twips(pg_mar, "right", 1440),
        "header": twips(pg_mar, "header", 720),
        "footer": twips(pg_mar, "footer", 720),
    }
    return page_w, page_h, margins


def _heading_level(paragraph) -> int:
    try:
        name = (paragraph.style.name or "").lower()
    except Exception:  # noqa: BLE001
        return 0
    for prefix in ("heading ", "제목 "):
        if name.startswith(prefix):
            try:
                return max(1, min(int(name[len(prefix):].strip()), 6))
            except ValueError:
                return 0
    return 0


def _num_pr(paragraph):
    return paragraph._p.find(f"{_w('pPr')}/{_w('numPr')}")


def _effective_size_pt(run, paragraph, heading: int) -> float:
    try:
        if run.font.size is not None:
            return run.font.size.pt
    except Exception:  # noqa: BLE001
        pass
    if heading:
        return _HEADING_PT[heading]
    try:
        if paragraph.style.font.size is not None:
            return paragraph.style.font.size.pt
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_FONT_PT


def _run_color(run) -> str:
    try:
        rgb = run.font.color.rgb
        if rgb is not None:
            return f"#{rgb}"
    except Exception:  # noqa: BLE001
        pass
    return "#222222"


def _paragraph_segments(paragraph, heading: int) -> list[list[_Seg]]:
    """Paragraph → logical lines (split on w:br), each a list of segments.

    Emits the sentinel ``\\ufffcPAGE\\ufffc`` for PAGE fields so header/
    footer chrome can substitute the live page number.
    """
    lines: list[list[_Seg]] = [[]]
    p_el = paragraph._p
    for child in p_el.iter():
        tag = child.tag
        if tag == _w("fldSimple") and "PAGE" in (child.get(_w("instr")) or ""):
            lines[-1].append(_Seg("￼PAGE￼", _DEFAULT_FONT_PT * 96 / 72))
    for run in paragraph.runs:
        size_px = _effective_size_pt(run, paragraph, heading) * 96.0 / 72.0
        bold = bool(run.bold) or heading in (1, 2, 3)
        seg_style = dict(
            size_px=size_px,
            bold=bold,
            italic=bool(run.italic),
            underline=bool(run.underline),
            color=_run_color(run),
            family=(run.font.name or None),
        )
        # split on explicit line breaks inside the run
        chunks = (run.text or "").split("\n")
        # detect <w:br/> which python-docx renders into run.text as \n? It
        # doesn't — walk the XML for br/tab to be exact.
        text_parts: list[str] = []
        for node in run._r.iter():
            if node.tag == _w("t"):
                text_parts.append(node.text or "")
            elif node.tag == _w("br") and node.get(_w("type")) not in ("page", "column"):
                text_parts.append("\n")
            elif node.tag == _w("tab"):
                text_parts.append("    ")
        joined = "".join(text_parts) if text_parts else (run.text or "")
        chunks = joined.split("\n")
        for ci, chunk in enumerate(chunks):
            if ci > 0:
                lines.append([])
            if chunk:
                lines[-1].append(_Seg(text=chunk, **seg_style))
    return lines


def _wrap_segments(segs: list[_Seg], max_w: float) -> list[_Line]:
    """Greedy wrap: word boundaries for Latin, char boundaries for CJK."""
    lines: list[_Line] = [_Line()]
    cur_w = 0.0

    def push(seg: _Seg) -> None:
        nonlocal cur_w
        lines[-1].segs.append(seg)
        cur_w += seg.width()

    def newline() -> None:
        nonlocal cur_w
        lines.append(_Line())
        cur_w = 0.0

    for seg in segs:
        tokens: list[str] = []
        word = ""
        for ch in seg.text:
            if ord(ch) > 0x2E80:
                if word:
                    tokens.append(word)
                    word = ""
                tokens.append(ch)
            elif ch == " ":
                tokens.append(word + " ")
                word = ""
            else:
                word += ch
        if word:
            tokens.append(word)
        buf = ""
        for tok in tokens:
            tok_w = _text_width(tok, seg.size_px, seg.bold, seg.family)
            buf_w = _text_width(buf, seg.size_px, seg.bold, seg.family)
            if cur_w + buf_w + tok_w > max_w and (buf or lines[-1].segs):
                if buf:
                    push(_Seg(**{**seg.__dict__, "text": buf}))
                    buf = ""
                newline()
                tok = tok.lstrip() or tok
            buf += tok
        if buf:
            push(_Seg(**{**seg.__dict__, "text": buf}))
    return lines


# ---------------------------------------------------------------------------
# Table layout
# ---------------------------------------------------------------------------


def _grid_widths(tbl_el, content_w: float) -> list[float]:
    cols = [
        float(gc.get(_w("w")) or 0) / _TWIPS_PER_PX
        for gc in tbl_el.findall(f"{_w('tblGrid')}/{_w('gridCol')}")
    ]
    if not cols:
        return [content_w]
    total = sum(cols) or content_w
    if total > content_w:
        scale = content_w / total
        cols = [c * scale for c in cols]
    return cols


def _cell_fill(tc_el) -> Optional[str]:
    shd = tc_el.find(f"{_w('tcPr')}/{_w('shd')}")
    if shd is not None:
        fill = shd.get(_w("fill"))
        if fill and fill not in ("auto",):
            return f"#{fill}"
    return None


def _layout_table(writer: _PageWriter, table, table_idx: int) -> None:
    tbl_el = table._tbl
    widths = _grid_widths(tbl_el, writer.content_w)
    pad = 4.0
    font_px = 10.0 * 96 / 72

    rows = tbl_el.findall(_w("tr"))
    grid_cols = len(widths)
    # vMerge tracking: (col) -> remaining anchor rect to extend
    for r_i, tr in enumerate(rows):
        cells = tr.findall(_w("tc"))
        # measure row height
        col_cursor = 0
        cell_layouts = []
        row_h = font_px * _LINE_SPACING + pad * 2
        for tc in cells:
            tc_pr = tc.find(_w("tcPr"))
            span = 1
            v_merge_cont = False
            if tc_pr is not None:
                gs = tc_pr.find(_w("gridSpan"))
                if gs is not None:
                    span = int(gs.get(_w("val")) or 1)
                vm = tc_pr.find(_w("vMerge"))
                if vm is not None and (vm.get(_w("val")) or "continue") != "restart":
                    v_merge_cont = True
            width = sum(widths[col_cursor:col_cursor + span]) or widths[-1]
            text = " ".join(
                "".join(t.text or "" for t in p.iter(_w("t")))
                for p in tc.findall(_w("p"))
            ).strip()
            seg = _Seg(text=text, size_px=font_px)
            wrapped = _wrap_segments([seg], max(width - pad * 2, 10.0)) if text else []
            cell_h = max(
                sum(ln.height for ln in wrapped) + pad * 2,
                font_px * _LINE_SPACING + pad * 2,
            )
            if not v_merge_cont:
                row_h = max(row_h, cell_h)
            cell_layouts.append(
                (col_cursor, span, width, wrapped, v_merge_cont, _cell_fill(tc))
            )
            col_cursor += span
        writer.ensure(row_h)
        x0 = writer.m["left"]
        y0 = writer.y
        for col_i, span, width, wrapped, v_cont, fill in cell_layouts:
            cx = x0 + sum(widths[:col_i])
            if v_cont:
                continue
            attrs = f' fill="{fill}"' if fill else ' fill="none"'
            writer.raw(
                f'<g data-e2d-table="{table_idx}" data-e2d-cell="{r_i},{col_i}">'
                f'<rect x="{_f(cx)}" y="{_f(y0)}" width="{_f(width)}" '
                f'height="{_f(row_h)}"{attrs} stroke="#B9B9B9" stroke-width="0.8"/>'
            )
            ty = y0 + pad
            for ln in wrapped:
                baseline = ty + ln.ascent
                tx = cx + pad
                for seg2 in ln.segs:
                    writer.raw(
                        f'<text x="{_f(tx)}" y="{_f(baseline)}" '
                        f'font-size="{_f(seg2.size_px)}" fill="#222222" '
                        f'font-family="{_FONT_STACK}" xml:space="preserve">'
                        f"{_esc(seg2.text)}</text>"
                    )
                    tx += seg2.width()
                ty += ln.height
            writer.raw("</g>")
        writer.y += row_h
    writer.y += _PARA_GAP_PX


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _layout_images(writer: _PageWriter, paragraph, part, budget: list[int]) -> bool:
    """Render inline images of *paragraph*; True when any were drawn."""
    drew = False
    for drawing in paragraph._p.iter(f"{{{W_NS.replace('wordprocessingml/2006/main', 'wordprocessingml/2006/main')}}}drawing"):
        extent = drawing.find(f".//{{{WP_NS}}}extent")
        blip = drawing.find(f".//{{{A_NS}}}blip")
        if blip is None:
            continue
        rid = blip.get(f"{{{R_NS}}}embed")
        if not rid or rid not in part.rels:
            continue
        try:
            blob = part.rels[rid].target_part.blob
            content_type = part.rels[rid].target_part.content_type
        except Exception:  # noqa: BLE001
            continue
        if budget[0] + len(blob) > _IMAGE_BUDGET_BYTES:
            continue
        budget[0] += len(blob)
        w_px = float(extent.get("cx")) / _EMU_PER_PX if extent is not None else 300.0
        h_px = float(extent.get("cy")) / _EMU_PER_PX if extent is not None else 200.0
        if w_px > writer.content_w:
            h_px *= writer.content_w / w_px
            w_px = writer.content_w
        writer.ensure(h_px + _PARA_GAP_PX)
        b64 = base64.b64encode(blob).decode("ascii")
        writer.raw(
            f'<image x="{_f(writer.m["left"] + (writer.content_w - w_px) / 2)}" '
            f'y="{_f(writer.y)}" width="{_f(w_px)}" height="{_f(h_px)}" '
            f'href="data:{content_type};base64,{b64}"/>'
        )
        writer.y += h_px + _PARA_GAP_PX
        drew = True
    return drew


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def docx_to_page_svgs(content: bytes) -> list[str]:
    """Render a DOCX to a list of per-page SVG strings."""
    document = Document(io.BytesIO(content))
    body = document.element.body
    page_w, page_h, margins = _section_geometry(body)

    def _hf_segments(container) -> list[_Seg]:
        segs: list[_Seg] = []
        try:
            for p in container.paragraphs:
                for line in _paragraph_segments(p, 0):
                    segs.extend(line)
        except Exception:  # noqa: BLE001
            pass
        return [s for s in segs if s.text.strip() or "￼" in s.text]

    header_segs: list[_Seg] = []
    footer_segs: list[_Seg] = []
    try:
        sec = document.sections[0]
        if not sec.header.is_linked_to_previous or True:
            header_segs = _hf_segments(sec.header)
        footer_segs = _hf_segments(sec.footer)
    except Exception:  # noqa: BLE001
        pass

    writer = _PageWriter(page_w, page_h, margins, header_segs, footer_segs)
    image_budget = [0]
    num_counters: dict[tuple[str, str], int] = {}

    para_idx = 0
    table_idx = 0
    try:
        for child in body:
            if child.tag == _w("p"):
                from docx.text.paragraph import Paragraph

                paragraph = Paragraph(child, document)
                heading = _heading_level(paragraph)

                # pageBreakBefore + explicit page-break runs
                if child.find(f"{_w('pPr')}/{_w('pageBreakBefore')}") is not None:
                    writer.page_break()
                has_page_break = any(
                    br.get(_w("type")) == "page" for br in child.iter(_w("br"))
                )

                writer.open_para_group(para_idx)
                drew_image = _layout_images(writer, paragraph, document.part, image_budget)

                logical_lines = _paragraph_segments(paragraph, heading)
                indent = 0.0
                bullet: Optional[str] = None
                num = _num_pr(paragraph)
                style_name = ""
                try:
                    style_name = (paragraph.style.name or "").lower()
                except Exception:  # noqa: BLE001
                    pass
                if num is None and style_name.startswith(("list bullet", "list number")):
                    # Style-driven lists (e.g. docx_from_markdown) carry no
                    # numPr — infer the marker from the style name.
                    indent = 18.0
                    if style_name.startswith("list number"):
                        key = ("style", style_name)
                        num_counters[key] = num_counters.get(key, 0) + 1
                        bullet = f"{num_counters[key]}. "
                    else:
                        bullet = "• "
                if num is not None:
                    num_id_el = num.find(_w("numId"))
                    ilvl_el = num.find(_w("ilvl"))
                    num_id = num_id_el.get(_w("val")) if num_id_el is not None else "0"
                    ilvl = ilvl_el.get(_w("val")) if ilvl_el is not None else "0"
                    indent = 18.0 * (int(ilvl) + 1)
                    key = (num_id, ilvl)
                    num_counters[key] = num_counters.get(key, 0) + 1
                    ordered = "number" in style_name
                    bullet = f"{num_counters[key]}. " if ordered else "• "

                text_present = any(s.text.strip() for line in logical_lines for s in line)
                if text_present:
                    if heading:
                        writer.y += _PARA_GAP_PX  # breathing room above headings
                    first = True
                    for segs in logical_lines:
                        if bullet and first and segs:
                            segs = [_Seg(text=bullet, size_px=segs[0].size_px,
                                         bold=segs[0].bold, color="#222222")] + segs
                        for ln in _wrap_segments(segs, writer.content_w - indent):
                            if ln.segs:
                                writer.emit_line(ln, indent=indent)
                        first = False
                    writer.y += _PARA_GAP_PX
                elif not drew_image:
                    # empty paragraph — vertical rhythm (Word keeps them)
                    writer.y += _DEFAULT_FONT_PT * 96 / 72 * 0.9
                writer.close_para_group()
                para_idx += 1

                if has_page_break:
                    writer.page_break()
            elif child.tag == _w("tbl"):
                from docx.table import Table

                _layout_table(writer, Table(child, document), table_idx)
                table_idx += 1
    except _PageBudgetExceeded:
        pass

    return writer.finish()
