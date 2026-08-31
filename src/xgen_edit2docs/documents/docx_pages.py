"""DOCX → per-page SVG (native-render plan M3).

A deterministic page-layout engine over python-docx's XML model: reads
``w:sectPr`` for the page geometry, flows paragraphs with measured
line wrap (``xgen_edit2docs.render.fonts`` — the same fonts resvg
rasterizes with), lays out tables/images/headers/footers, and emits
one self-contained SVG per page. ``render_doc`` feeds these to the
resvg/PyMuPDF raster layer for PNG/PDF — the piece LibreOffice used to
provide.

Fidelity scope (deliberate): body paragraphs (runs with bold/italic/
size/color/underline/strike, w:jc alignment), heading styles, bullet/
numbered lists, hard + automatic page breaks, tables (tblGrid widths,
gridSpan/vMerge merges drawn as one spanning rect, per-run cell styles
+ per-paragraph cell alignment, w:vAlign, trHeight minimums, cell
shading, **per-side w:tcBorders (single/dashed/dotted/double/nil) with
width+color, w:tcMar cell padding, w:spacing line-spacing multiples and
before/after gaps**, row-boundary page splitting, nested tables
flattened to text), inline images (extent-scaled, base64), single-section page
size/margins, first-section header/footer text with PAGE field
support. Floating shapes, multi-column sections and footnote blocks
are out of scope — the HTML preview covers reading those.

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
    strike: bool = False
    color: str = "#222222"
    family: Optional[str] = None

    def width(self) -> float:
        return _text_width(self.text, self.size_px, self.bold, self.family)

    def deco(self) -> str:
        parts = []
        if self.underline:
            parts.append("underline")
        if self.strike:
            parts.append("line-through")
        return " ".join(parts)


@dataclass
class _Line:
    segs: list[_Seg] = field(default_factory=list)
    #: 문단 줄간격 배수 (w:spacing lineRule="auto" line/240) — 1.0 = 기본.
    spacing_mult: float = 1.0

    @property
    def height(self) -> float:
        base = max((s.size_px for s in self.segs),
                   default=_DEFAULT_FONT_PT * 96 / 72)
        return base * _LINE_SPACING * self.spacing_mult

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

    def emit_line(self, line: _Line, indent: float = 0.0, align: str = "left") -> None:
        """*align*: 'left' | 'center' | 'right' — 남는 폭만큼 시작점을 민다."""
        self.ensure(line.height)
        x = self.m["left"] + indent
        if align in ("center", "right"):
            line_w = sum(s.width() for s in line.segs)
            slack = max(0.0, self.content_w - indent - line_w)
            x += slack / 2 if align == "center" else slack
        baseline = self.y + line.ascent
        for seg in line.segs:
            if seg.text:
                style = []
                if seg.italic:
                    style.append('font-style="italic"')
                if seg.bold:
                    style.append('font-weight="bold"')
                deco = seg.deco()
                if deco:
                    style.append(f'text-decoration="{deco}"')
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


def _para_spacing(p_el) -> tuple[float, float, float]:
    """w:pPr/w:spacing → (앞 px, 뒤 px, 줄간격 배수).

    before/after 는 twips, line 은 lineRule="auto" 일 때 240 = 1배.
    (HWP 변환물의 160%/200% 줄간격이 여기로 온다.)"""
    sp = p_el.find(f"{_w('pPr')}/{_w('spacing')}")
    if sp is None:
        return 0.0, 0.0, 1.0

    def twips(attr: str) -> float:
        try:
            return float(sp.get(_w(attr))) / _TWIPS_PER_PX
        except (TypeError, ValueError):
            return 0.0

    mult = 1.0
    rule = (sp.get(_w("lineRule")) or "auto").lower()
    try:
        line = float(sp.get(_w("line")))
        if rule == "auto" and 60 <= line <= 1200:
            mult = line / 240.0
    except (TypeError, ValueError):
        pass
    return twips("before"), twips("after"), mult


def _para_align(p_el) -> str:
    """w:pPr/w:jc → 'left' | 'center' | 'right'. justify(both)/distribute 는
    좌측 흘림으로 근사한다 (이 엔진은 자간 조정을 하지 않는다)."""
    jc = p_el.find(f"{_w('pPr')}/{_w('jc')}")
    val = (jc.get(_w("val")) or "").lower() if jc is not None else ""
    if val in ("center",):
        return "center"
    if val in ("right", "end"):
        return "right"
    return "left"


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
        try:
            strike = bool(run.font.strike)
        except Exception:  # noqa: BLE001
            strike = False
        seg_style = dict(
            size_px=size_px,
            bold=bold,
            italic=bool(run.italic),
            underline=bool(run.underline),
            strike=strike,
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


@dataclass
class _CellBox:
    """앵커 셀 하나 — 병합(gridSpan/vMerge)을 흡수한 격자상의 사각형."""

    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    #: (줄, 그 줄의 문단 정렬) — 셀 문단별 jc 를 줄 단위로 내린 것.
    lines: list = field(default_factory=list)
    fill: Optional[str] = None
    valign: str = "top"  # w:tcPr/w:vAlign — top | center | bottom
    #: 변별 테두리 {side: (val, width_px, color)} — None = 기본 회색 격자.
    borders: Optional[dict] = None
    #: 안쪽 여백 (left, right, top, bottom) px.
    pad: tuple = (4.0, 4.0, 4.0, 4.0)

    def content_h(self) -> float:
        return sum(ln.height for ln, _a in self.lines)


#: 테두리 val → SVG dasharray (None = 실선, "skip" = 안 그림)
_BORDER_DASH = {
    "nil": "skip", "none": "skip",
    "dashed": "6 3", "dotted": "2 2", "dotdash": "6 3 2 3",
    "dotdotdash": "6 3 2 3 2 3",
}


def _cell_borders(tc_el) -> Optional[dict]:
    """w:tcPr/w:tcBorders → {side: (val, width_px, color)}.

    없으면 None — 표 전체 기본 격자(회색 0.8)로 그린다. sz 는 1/8pt
    (px = sz/8 × 96/72), 색은 RRGGBB 또는 auto."""
    tcb = tc_el.find(f"{_w('tcPr')}/{_w('tcBorders')}")
    if tcb is None:
        return None
    out: dict = {}
    for side in ("left", "right", "top", "bottom"):
        el = tcb.find(_w(side))
        if el is None:
            continue
        val = (el.get(_w("val")) or "single").lower()
        try:
            width_px = max(0.5, float(el.get(_w("sz")) or 4) / 8 * 96 / 72)
        except ValueError:
            width_px = 0.8
        color = el.get(_w("color")) or "444444"
        if color.lower() == "auto":
            color = "444444"
        out[side] = (val, min(width_px, 6.0), f"#{color}")
    return out or None


def _cell_pad(tc_el, default: float) -> tuple:
    """w:tcPr/w:tcMar → (l, r, t, b) px (dxa=twips)."""
    mar = tc_el.find(f"{_w('tcPr')}/{_w('tcMar')}")
    pads = [default] * 4
    if mar is not None:
        for i, side in enumerate(("left", "right", "top", "bottom")):
            el = mar.find(_w(side))
            if el is not None:
                try:
                    pads[i] = min(40.0, max(0.0, float(el.get(_w("w"))) / _TWIPS_PER_PX))
                except (TypeError, ValueError):
                    pass
    return tuple(pads)


def _cell_valign(tc_el) -> str:
    va = tc_el.find(f"{_w('tcPr')}/{_w('vAlign')}")
    val = (va.get(_w("val")) or "").lower() if va is not None else ""
    if val in ("center",):
        return "center"
    if val in ("bottom",):
        return "bottom"
    return "top"


def _cell_lines(tc_el, document, avail_w: float) -> list:
    """셀 내용 → [(줄, 정렬)] — 문단별 런 스타일과 jc 를 보존해 감싼다.

    중첩 표는 이 엔진의 격자 밖이라 행 단위 텍스트(' | ' 연결)로 평탄화
    한다 — 배치는 잃지만 내용은 잃지 않는다.
    """
    from docx.text.paragraph import Paragraph

    out: list = []
    for child in tc_el:
        if child.tag == _w("p"):
            paragraph = Paragraph(child, document)
            align = _para_align(child)
            _bf, _af, mult = _para_spacing(child)
            for segs in _paragraph_segments(paragraph, 0):
                if not any(s.text for s in segs):
                    continue
                for ln in _wrap_segments(segs, avail_w):
                    if ln.segs:
                        ln.spacing_mult = mult
                        out.append((ln, align))
        elif child.tag == _w("tbl"):
            for tr in child.findall(_w("tr")):
                texts = []
                for tc2 in tr.findall(_w("tc")):
                    t = "".join(t.text or "" for t in tc2.iter(_w("t"))).strip()
                    if t:
                        texts.append(t)
                if texts:
                    seg = _Seg(text=" | ".join(texts), size_px=10.0 * 96 / 72)
                    for ln in _wrap_segments([seg], avail_w):
                        if ln.segs:
                            out.append((ln, "left"))
    return out


def _table_model(tbl_el, document, widths: list[float], pad: float):
    """w:tbl → (앵커 셀 목록, 행 높이 목록). vMerge continue 는 앵커의
    rowspan 으로 흡수되고, 행 높이는 내용/trHeight/병합 부족분 순으로 채운다."""
    font_px = 10.0 * 96 / 72
    rows = tbl_el.findall(_w("tr"))
    boxes: list[_CellBox] = []
    anchor_at: dict[int, _CellBox] = {}  # col → 위 행에서 내려오는 vMerge 앵커
    min_row_h: list[float] = []

    for r_i, tr in enumerate(rows):
        trh = tr.find(f"{_w('trPr')}/{_w('trHeight')}")
        try:
            min_h = float(trh.get(_w("val"))) / _TWIPS_PER_PX if trh is not None else 0.0
        except (TypeError, ValueError):
            min_h = 0.0
        min_row_h.append(max(min_h, font_px * _LINE_SPACING + pad * 2))

        col_cursor = 0
        for tc in tr.findall(_w("tc")):
            tc_pr = tc.find(_w("tcPr"))
            span = 1
            vmerge = None  # None | 'restart' | 'continue'
            if tc_pr is not None:
                gs = tc_pr.find(_w("gridSpan"))
                if gs is not None:
                    try:
                        span = max(1, int(gs.get(_w("val")) or 1))
                    except ValueError:
                        span = 1
                vm = tc_pr.find(_w("vMerge"))
                if vm is not None:
                    vmerge = (vm.get(_w("val")) or "continue").lower()
            if vmerge == "continue":
                anchor = anchor_at.get(col_cursor)
                if anchor is not None:
                    anchor.rowspan = r_i - anchor.row + 1
                col_cursor += span
                continue
            width = sum(widths[col_cursor:col_cursor + span]) or widths[-1]
            cpad = _cell_pad(tc, pad)
            box = _CellBox(
                row=r_i, col=col_cursor, colspan=span,
                lines=_cell_lines(tc, document,
                                  max(width - cpad[0] - cpad[1], 10.0)),
                fill=_cell_fill(tc), valign=_cell_valign(tc),
                borders=_cell_borders(tc), pad=cpad,
            )
            boxes.append(box)
            if vmerge == "restart":
                anchor_at[col_cursor] = box
            else:
                anchor_at.pop(col_cursor, None)
            col_cursor += span

    # 행 높이: ① 단일행 셀 내용 → ② trHeight 최소 → ③ 병합 셀 부족분은
    # 마지막 스팬 행에 몰아준다 (한/워드의 자동 늘림과 같은 근사).
    row_h = list(min_row_h)
    for box in boxes:
        if box.rowspan == 1:
            r = box.row
            if r < len(row_h):
                row_h[r] = max(row_h[r], box.content_h() + box.pad[2] + box.pad[3])
    for box in boxes:
        if box.rowspan > 1:
            end = min(box.row + box.rowspan, len(row_h))
            have = sum(row_h[box.row:end])
            need = box.content_h() + box.pad[2] + box.pad[3]
            if need > have and end - 1 >= box.row:
                row_h[end - 1] += need - have
    return boxes, row_h


def _draw_cell_box(writer: _PageWriter, table_idx: int, box: _CellBox,
                   x: float, y: float, w: float, h: float, pad: float) -> None:
    writer.raw(
        f'<g data-e2d-table="{table_idx}" data-e2d-cell="{box.row},{box.col}">'
    )
    fill_attr = f' fill="{box.fill}"' if box.fill else ' fill="none"'
    if box.borders is None:
        # 변별 테두리 없음 — 기본 격자 (기존과 동일)
        writer.raw(
            f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" '
            f'height="{_f(h)}"{fill_attr} stroke="#B9B9B9" stroke-width="0.8"/>'
        )
    else:
        if box.fill:
            writer.raw(
                f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" '
                f'height="{_f(h)}" fill="{box.fill}"/>'
            )
        coords = {
            "left": (x, y, x, y + h), "right": (x + w, y, x + w, y + h),
            "top": (x, y, x + w, y), "bottom": (x, y + h, x + w, y + h),
        }
        for side, (x1, y1, x2, y2) in coords.items():
            spec = box.borders.get(side)
            if spec is None:
                # 선언 안 된 변 — 기본 격자선으로 이음새를 메운다
                writer.raw(
                    f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" '
                    f'y2="{_f(y2)}" stroke="#B9B9B9" stroke-width="0.8"/>'
                )
                continue
            val, width_px, color = spec
            dash = _BORDER_DASH.get(val)
            if dash == "skip":
                continue  # 선 없음 (한글 표의 '테두리 없음' 셀)
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            if val in ("double", "triple"):
                # 이중선 근사 — 가는 선 2개
                thin = max(0.6, width_px / 3)
                off = max(1.2, width_px)
                dx, dy = ((off, 0) if side in ("left", "right") else (0, off))
                writer.raw(
                    f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" '
                    f'stroke="{color}" stroke-width="{_f(thin)}"/>'
                    f'<line x1="{_f(x1 + dx)}" y1="{_f(y1 + dy)}" '
                    f'x2="{_f(x2 + dx)}" y2="{_f(y2 + dy)}" '
                    f'stroke="{color}" stroke-width="{_f(thin)}"/>'
                )
            else:
                writer.raw(
                    f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" '
                    f'y2="{_f(y2)}" stroke="{color}" '
                    f'stroke-width="{_f(width_px)}"{dash_attr}/>'
                )
    pl, pr, pt, pb = box.pad
    content_h = box.content_h()
    if box.valign == "center":
        ty = y + max(pt, (h - content_h) / 2)
    elif box.valign == "bottom":
        ty = y + max(pt, h - content_h - pb)
    else:
        ty = y + pt
    inner_w = max(w - pl - pr, 1.0)
    for ln, align in box.lines:
        if ty + ln.height > y + h + 0.5:  # 넘치는 줄은 셀 경계에서 끊는다
            break
        baseline = ty + ln.ascent
        line_w = sum(s.width() for s in ln.segs)
        tx = x + pl
        if align == "center":
            tx += max(0.0, (inner_w - line_w) / 2)
        elif align == "right":
            tx += max(0.0, inner_w - line_w)
        for seg2 in ln.segs:
            if seg2.text:
                style = []
                if seg2.italic:
                    style.append('font-style="italic"')
                if seg2.bold:
                    style.append('font-weight="bold"')
                deco = seg2.deco()
                if deco:
                    style.append(f'text-decoration="{deco}"')
                fam = f"'{seg2.family}', {_FONT_STACK}" if seg2.family else _FONT_STACK
                writer.raw(
                    f'<text x="{_f(tx)}" y="{_f(baseline)}" '
                    f'font-size="{_f(seg2.size_px)}" fill="{seg2.color}" '
                    f'font-family="{fam}" {" ".join(style)} xml:space="preserve">'
                    f"{_esc(seg2.text)}</text>"
                )
            tx += seg2.width()
        ty += ln.height
    writer.raw("</g>")


def _layout_table(writer: _PageWriter, table, table_idx: int) -> None:
    """표 전체를 격자 모델로 그린다 — 병합(gridSpan/vMerge)은 앵커 셀
    사각형 하나가 스팬 전체를 덮는다. 페이지에 다 안 들어가면 행 경계에서
    쪼개고, 경계를 걸치는 병합 셀은 그 페이지 조각까지만 그린다."""
    tbl_el = table._tbl
    widths = _grid_widths(tbl_el, writer.content_w)
    pad = 4.0
    document = getattr(table, "_parent", None)

    boxes, row_h = _table_model(tbl_el, document, widths, pad)
    if not row_h:
        return
    n_rows = len(row_h)
    col_x = [0.0]
    for w in widths:
        col_x.append(col_x[-1] + w)

    r = 0
    while r < n_rows:
        avail = writer.bottom - writer.y
        total_rest = sum(row_h[r:])
        if total_rest > avail and writer.y > writer.m["top"] + 1:
            writer.page_break()
            avail = writer.bottom - writer.y
        # 이 페이지에 들어가는 행 조각 [r, chunk_end)
        chunk_end, acc = r, 0.0
        while chunk_end < n_rows and (acc + row_h[chunk_end] <= avail or chunk_end == r):
            acc += row_h[chunk_end]
            chunk_end += 1
        y_of = {r: writer.y}
        for ri in range(r, chunk_end):
            y_of[ri + 1] = y_of[ri] + row_h[ri]
        for box in boxes:
            b_end = box.row + box.rowspan
            if b_end <= r or box.row >= chunk_end:
                continue
            # 페이지 조각으로 클립 — 경계를 걸치면 이 조각 몫만 그린다.
            top_r = max(box.row, r)
            bot_r = min(b_end, chunk_end)
            x = writer.m["left"] + col_x[min(box.col, len(widths))]
            w = sum(widths[box.col:box.col + box.colspan]) or widths[-1]
            h = sum(row_h[top_r:bot_r])
            draw = box if top_r == box.row else _CellBox(
                row=box.row, col=box.col, fill=box.fill,
                borders=box.borders, pad=box.pad)  # 이월 조각은 빈 칸
            _draw_cell_box(writer, table_idx, draw, x, y_of[top_r], w, h, pad)
        writer.y += acc
        r = chunk_end
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
                    align = _para_align(child)
                    before_px, after_px, mult = _para_spacing(child)
                    if before_px > 0:
                        writer.y += before_px
                    first = True
                    for segs in logical_lines:
                        if bullet and first and segs:
                            segs = [_Seg(text=bullet, size_px=segs[0].size_px,
                                         bold=segs[0].bold, color="#222222")] + segs
                        for ln in _wrap_segments(segs, writer.content_w - indent):
                            if ln.segs:
                                ln.spacing_mult = mult
                                writer.emit_line(ln, indent=indent, align=align)
                        first = False
                    writer.y += _PARA_GAP_PX + after_px
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
