"""HWP 5.0 (한글 바이너리) → 자립형 HTML 한 장.

:class:`~.hwp_convert.HwpFile` 의 해석(문단·글자 모양·표·그림·글상자·머리말)을
DOCX 를 거치지 않고 HTML 로 옮긴다. 글자 크기·굵기·색·글꼴, 문단 정렬·여백·
내어쓰기·줄간격, 표 병합·열 너비·셀 배경·변별 테두리·세로 정렬·안쪽 여백,
그림 크기가 그대로 간다.

안전 계약 — 이 HTML 은 **정제 없이 화면에 주입될 수 있다** (문서 출처 패널):

- 본문 글자와 속성 값은 전부 이스케이프한다. 색은 파서가 정수에서 만든
  6자리 hex 뿐이고, 크기는 숫자로만 찍는다.
- 글꼴 이름은 파일이 준 문자열이다 — 글자·숫자·스페이스·``-._`` 밖은 버려서
  CSS 선언을 끊지 못하게 한다.
- ``<style>`` 규칙은 모두 루트 클래스(``.hwp-doc``) 아래에 둔다. 주입된
  ``<style>`` 은 페이지 전체에 적용되므로 맨 선택자(``p``, ``table``)는 앱
  화면까지 바꾼다.
- 그림은 브라우저가 그리는 형식(png/jpeg/gif/bmp)만 data URI 로 싣고, 싣는
  총량에 상한을 둔다. 스크립트가 도는 형식(svg)은 싣지 않는다.

본 제품은 한글과컴퓨터의 글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.
"""

from __future__ import annotations

import base64
import html
import re

from .hwp_convert import (
    HwpFile,
    _BorderSide,
    _header_footer_paras,
    _Image,
    _interpret_paras,
    _master_page_paras,
    _Note,
    _PageDef,
    _Para,
    _para_runs,
    _Table,
    _TextBox,
    first_page_def,
)

#: 루트 클래스 — 모든 CSS 규칙이 이 아래에만 걸린다.
ROOT_CLASS = "hwp-doc"

#: HWPUNIT(1/7200 in) → CSS px(1/96 in)
_PX_PER_HU = 96.0 / 7200.0

_IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
}
#: 브라우저가 못 그리거나(tif) 너무 큰(무압축 bmp) 형식 — png 로 바꿔 싣는다.
_TO_PNG = {"bmp", "tif", "tiff"}

#: 싣는 그림 원본의 총량 상한 — 넘으면 나머지 그림은 싣지 않는다.
_MAX_IMAGE_BYTES = 64 * 1024 * 1024

#: 테두리선 종류 → CSS border-style. 0 = 선 없음 (실파일 기준 번호,
#: DOCX 조립의 _STROKE_VAL 과 같은 표).
_STROKE_CSS = {
    0: "none", 1: "solid", 2: "dashed", 3: "dotted", 4: "dashed",
    5: "dashed", 6: "dashed", 7: "dotted", 8: "double", 9: "double",
    10: "double", 11: "double", 12: "solid", 13: "double",
}

_FONT_UNSAFE = re.compile(r"[^\w \-.]")

_BASE_CSS = (
    f".{ROOT_CLASS}{{background:#f2f2f2;padding:12px 0;color:#000;"
    "font-family:'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',sans-serif;"
    "font-size:10pt;line-height:1.6;}"
    f".{ROOT_CLASS} .pg{{box-sizing:border-box;margin:0 auto 12px;background:#fff;"
    "box-shadow:0 1px 3px rgba(0,0,0,.25);}"
    f".{ROOT_CLASS} p{{margin:0;white-space:pre-wrap;tab-size:4;overflow-wrap:break-word;}}"
    f".{ROOT_CLASS} table{{border-collapse:collapse;margin:2px 0;}}"
    f".{ROOT_CLASS} td{{border:0.12mm solid #000;padding:1.9px 6.8px;"
    "vertical-align:middle;}"
    f".{ROOT_CLASS} img{{display:inline-block;max-width:100%;height:auto;}}"
    f".{ROOT_CLASS} img{{vertical-align:middle;}}"
    f".{ROOT_CLASS} .hd,.{ROOT_CLASS} .ft,.{ROOT_CLASS} .mp{{color:#555;}}"
    f".{ROOT_CLASS} .mp{{margin-bottom:8px;padding-bottom:4px;border-bottom:1px dashed #bbb;}}"
    f".{ROOT_CLASS} .hd{{margin-bottom:8px;}}.{ROOT_CLASS} .ft{{margin-top:8px;}}"
    f".{ROOT_CLASS} .nr{{font-size:.75em;vertical-align:super;line-height:0;}}"
    f".{ROOT_CLASS} .nt{{margin-top:12px;padding-top:4px;border-top:1px solid #999;"
    "font-size:.9em;}"
)


def hwp_to_html(content: bytes) -> str:
    """HWP 5.0 바이트 → 자립형 HTML 문자열.

    열 수 없는 파일(OLE 아님·암호·DRM·깨진 스트림)은
    :class:`~xgen_edit2docs.documents.legacy.LegacyConvertError` 로 거부한다.
    배포용 문서의 복사·인쇄 방지 설정은 화면에서도 지킨다 (선택·인쇄 막음).
    """
    with HwpFile(content) as hf:
        return _Renderer(hf).render()


def _px(hu: float) -> str:
    return f"{hu * _PX_PER_HU:.1f}px"


def _pt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") + "pt"


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _safe_font(name: str | None) -> str:
    if not name:
        return ""
    return _FONT_UNSAFE.sub("", name).strip()[:64]


class _Renderer:
    def __init__(self, hf: HwpFile):
        self.hf = hf
        self.info = hf.info
        self.used_char: set[int] = set()
        self.used_para: set[int] = set()
        self.used_fill: set[int] = set()
        self.image_bytes = 0
        self._image_cache: dict[int, str | None] = {}
        self._notes: list[str] = []
        self._note_seq = 0

    # ── 문서 ─────────────────────────────────────────────────

    def render(self) -> str:
        out: list[str] = []
        page = _PageDef()
        for roots in self.hf.sections():
            page = first_page_def(roots) or page
            # 머리말·꼬리말은 그 구역이 새로 정했을 때만 그린다 — 앞 구역 것을 잇는
            # 구역마다 되풀이하면 같은 머리말(과 그림)이 구역 수만큼 쌓인다.
            header, footer = _header_footer_paras(roots)
            body = "".join(self._blocks(_interpret_paras(roots)))
            master = _master_page_paras(roots)
            if master:  # 바탕쪽(쪽 머리 장식·틀)은 구역 맨 위에 흐리게
                body = f'<div class="mp">{"".join(self._blocks(master))}</div>' + body
            head = "".join(self._blocks(header)) if _visible(header) else ""
            foot = "".join(self._blocks(footer)) if _visible(footer) else ""
            if self._notes:  # 각주·미주 내용은 그 구역 끝에
                body += f'<div class="nt">{"".join(self._notes)}</div>'
                self._notes = []
            style = (f"width:{_px(page.width)};padding:{_px(page.top)} {_px(page.right)} "
                     f"{_px(page.bottom)} {_px(page.left)}")
            out.append(
                f'<div class="pg" style="{style}">'
                + (f'<div class="hd">{head}</div>' if head else "")
                + body
                + (f'<div class="ft">{foot}</div>' if foot else "")
                + "</div>")

        guard = ""
        if self.hf.copy_protected:  # 배포용 문서의 복사 방지 설정
            guard += (f".{ROOT_CLASS}{{-webkit-user-select:none;user-select:none;}}")
        if self.hf.print_protected:  # 인쇄 방지 설정
            guard += f"@media print{{.{ROOT_CLASS}{{display:none;}}}}"
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8"/>'
            f"<style>{_BASE_CSS}{self._shape_css()}{guard}</style></head>"
            f'<body><div class="{ROOT_CLASS}">{"".join(out)}</div></body></html>'
        )

    def _blocks(self, blocks: list[object]) -> list[str]:
        out: list[str] = []
        for block in blocks:
            if isinstance(block, _Para):
                out.extend(self._para_blocks(block))
        return out

    # ── 문단 ─────────────────────────────────────────────────

    def _para_attr(self, para: _Para) -> tuple[str, int]:
        """<p> 속성 문자열과 문단 기본 글자 모양 id."""
        classes: list[str] = []
        pid = para.parashape_id
        if pid is not None and 0 <= pid < len(self.info.para_shapes):
            self.used_para.add(pid)
            classes.append(f"p{pid}")
        runs = _para_runs(para)
        # 빈 문단의 줄 높이도 그 문단의 글자 모양을 따른다.
        first_shape = runs[0][1] if runs else (
            sorted(para.shape_spans)[0][1] if para.shape_spans else -1)
        if self._char_ok(first_shape):
            classes.append(f"c{first_shape}")
        attr = f' class="{" ".join(classes)}"' if classes else ""
        return attr, first_shape

    def _tokens(self, para: _Para, base_shape: int) -> list[tuple[str, str]]:
        """문단 → [("inline"|"block", html)] — 글자와 줄 안 개체를 제자리 순서로.

        글자처럼 취급하는 그림·글상자와 각주 참조는 "inline", 글자처럼 취급
        하는 표는 "block"(표는 <p> 안에 둘 수 없다). 떠 있는 개체는 여기서
        빠지고 문단 뒤에 붙는다(:meth:`_para_blocks`).
        """
        events: list[tuple[int, int, str, str]] = []
        for order, att in enumerate(para.attachments):
            if isinstance(att, _Note):
                events.append((att.pos, order, "inline", self._note(att)))
            elif getattr(att, "inline", False):
                kind, html_ = self._inline_object(att)
                if html_:
                    events.append((att.pos, order, kind, html_))
        events.sort(key=lambda e: (e[0], e[1]))

        tokens: list[tuple[str, str]] = []
        k = 0
        off = 0
        for chunk, sid in _para_runs(para):
            seg = 0
            while k < len(events) and events[k][0] < off + len(chunk):
                cut = max(events[k][0] - off, seg)
                if cut > seg:
                    tokens.append(("inline", self._text(chunk[seg:cut], sid, base_shape)))
                seg = cut
                tokens.append((events[k][2], events[k][3]))
                k += 1
            if seg < len(chunk):
                tokens.append(("inline", self._text(chunk[seg:], sid, base_shape)))
            off += len(chunk)
        tokens.extend((e[2], e[3]) for e in events[k:])
        return tokens

    def _text(self, chunk: str, sid: int, base_shape: int) -> str:
        text = "<br/>".join(_esc(piece) for piece in chunk.split("\n"))
        if self._char_ok(sid) and sid != base_shape:
            return f'<span class="c{sid}">{text}</span>'
        return text

    def _para_blocks(self, para: _Para) -> list[str]:
        attr, base_shape = self._para_attr(para)
        out: list[str] = []
        pieces: list[str] = []
        for kind, html_ in self._tokens(para, base_shape):
            if kind == "block":
                if pieces:
                    out.append(f"<p{attr}>{''.join(pieces)}</p>")
                    pieces = []
                out.append(html_)
            else:
                pieces.append(html_)
        if pieces:
            out.append(f"<p{attr}>{''.join(pieces)}</p>")
        elif not out and not para.attachments:
            out.append(f"<p{attr}><br/></p>")  # 빈 줄도 한 줄의 높이를 가진다
        out.extend(self._attachments(para))
        return out

    def _inline_object(self, att: object) -> tuple[str, str]:
        if isinstance(att, _Table):
            return "block", self._table(att)
        if isinstance(att, _Image):
            return "inline", self._image(att)
        if isinstance(att, _TextBox):
            if not _fits_in_line(att):
                # 줄 안에 둘 수 없는 내용(표·떠 있는 개체) — 블록으로.
                return "block", f'<div class="tb">{"".join(self._blocks(att.paras))}</div>'
            lines: list[str] = []
            for p in att.paras:
                _, base = self._para_attr(p)
                open_tag = f'<span class="c{base}">' if self._char_ok(base) else "<span>"
                inner = "".join(h for _, h in self._tokens(p, base))
                lines.append(f"{open_tag}{inner}</span>")
            return "inline", '<span class="ib">' + "<br/>".join(lines) + "</span>"
        return "inline", ""

    def _note(self, note: _Note) -> str:
        """각주·미주 참조 표시 — 내용은 구역 끝 목록으로 모은다."""
        self._note_seq += 1
        mark = note.number or f"{self._note_seq})"
        self._notes.append("".join(self._blocks(note.paras)))
        return f'<span class="nr">{_esc(mark)}</span>'

    def _char_ok(self, sid: int) -> bool:
        if 0 <= sid < len(self.info.char_shapes):
            self.used_char.add(sid)
            return True
        return False

    # ── 표·그림·글상자 ────────────────────────────────────────

    def _attachments(self, para: _Para) -> list[str]:
        """떠 있는 개체(글자처럼 취급하지 않는 표·그림·글상자) — 문단 뒤에."""
        out: list[str] = []
        for att in para.attachments:
            if isinstance(att, _Note) or getattr(att, "inline", False):
                continue
            if isinstance(att, _Table):
                out.append(self._table(att))
            elif isinstance(att, _Image):
                img = self._image(att)
                if img:
                    out.append(f"<p>{img}</p>")
            elif isinstance(att, _TextBox):
                # 글상자 안의 표·그림도 그 문단의 첨부다.
                out.append(f'<div class="tb">{"".join(self._blocks(att.paras))}</div>')
        return out

    def _cell_content(self, paras: list[_Para]) -> str:
        return "".join(self._blocks(list(paras)))

    def _table(self, tbl: _Table) -> str:
        rows, cols = tbl.rows, tbl.cols
        col_w: dict[int, int] = {}
        row_h: dict[int, int] = {}
        for cell in tbl.cells:
            if cell.colspan == 1 and 0 <= cell.col < cols and cell.width_hu > 0:
                col_w.setdefault(cell.col, cell.width_hu)
            if cell.rowspan == 1 and 0 <= cell.row < rows and cell.height_hu > 0:
                row_h[cell.row] = max(row_h.get(cell.row, 0), cell.height_hu)

        occupied = [[False] * cols for _ in range(rows)]
        starts: dict[tuple[int, int], object] = {}
        spill: list[object] = []  # 격자에 못 앉는 셀(겹침·범위 밖) — 내용은 버리지 않는다
        for cell in tbl.cells:
            r, c = cell.row, cell.col
            if not (0 <= r < rows and 0 <= c < cols) or occupied[r][c]:
                spill.append(cell)
                continue
            rs = max(1, min(cell.rowspan, rows - r))
            cs = max(1, min(cell.colspan, cols - c))
            # 이미 찬 칸과 겹치지 않는 만큼만 넓힌다.
            while cs > 1 and any(occupied[r][c + k] for k in range(cs)):
                cs -= 1
            while rs > 1 and any(occupied[r + k][c + j] for k in range(rs) for j in range(cs)):
                rs -= 1
            for k in range(rs):
                for j in range(cs):
                    occupied[r + k][c + j] = True
            starts[(r, c)] = (cell, rs, cs)

        # 열 너비는 원본대로 주되 자동 배치 — 글꼴이 달라 한 어절이 칸보다 길면
        # 칸이 넓어진다(고정 배치는 어절을 글자 단위로 끊어 버린다).
        style = ""
        if len(col_w) == cols:
            style = f' style="width:{_px(sum(col_w.values()))}"'
        cls = ""
        fid = tbl.borderfill_id - 1
        if 0 <= fid < len(self.info.border_fills):
            self.used_fill.add(fid)
            cls = f' class="f{fid}"'
        out = [f"<table{cls}{style}>"]
        if col_w:
            out.append("<colgroup>")
            for j in range(cols):
                w = col_w.get(j)
                out.append(f'<col style="width:{_px(w)}"/>' if w else "<col/>")
            out.append("</colgroup>")
        filled = [[False] * cols for _ in range(rows)]
        for r in range(rows):
            out.append("<tr>")
            for c in range(cols):
                if filled[r][c]:
                    continue
                entry = starts.get((r, c))
                if entry is None:
                    filled[r][c] = True
                    out.append("<td></td>")
                    continue
                cell, rs, cs = entry
                for k in range(rs):
                    for j in range(cs):
                        filled[r + k][c + j] = True
                out.append(self._td(cell, rs, cs, row_h.get(r) if rs == 1 else None))
            out.append("</tr>")
        for cell in spill:
            out.append(f'<tr><td colspan="{cols}">{self._cell_content(cell.paras)}</td></tr>')
        out.append("</table>")
        caption = "".join(self._blocks(tbl.caption))
        if tbl.caption_side in ("top", "left"):
            return caption + "".join(out)
        return "".join(out) + caption

    def _td(self, cell, rs: int, cs: int, height_hu: int | None) -> str:
        attrs: list[str] = []
        if rs > 1:
            attrs.append(f'rowspan="{rs}"')
        if cs > 1:
            attrs.append(f'colspan="{cs}"')
        fid = cell.borderfill_id - 1
        if 0 <= fid < len(self.info.border_fills):
            self.used_fill.add(fid)
            attrs.append(f'class="f{fid}"')
        style = [f"vertical-align:{'middle' if cell.valign == 'center' else cell.valign}"]
        if any(cell.padding_hu):
            left, right, top, bottom = cell.padding_hu
            style.append(f"padding:{_px(top)} {_px(right)} {_px(bottom)} {_px(left)}")
        if height_hu:
            style.append(f"height:{_px(height_hu)}")
        attrs.append(f'style="{";".join(style)}"')
        return f"<td {' '.join(attrs)}>{self._cell_content(cell.paras)}</td>"

    def _image(self, img: _Image) -> str:
        if img.bindata_id in self._image_cache:
            uri = self._image_cache[img.bindata_id]
        else:
            uri = None
            got = self.hf.bin_blob(img.bindata_id, exts=set(_IMAGE_MIME) | _TO_PNG)
            if got is not None:
                blob, mime = _browser_image(*got)
                if blob and self.image_bytes + len(blob) <= _MAX_IMAGE_BYTES:
                    self.image_bytes += len(blob)
                    uri = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
            self._image_cache[img.bindata_id] = uri
        if not uri:
            return ""
        style = []
        if img.width_hu > 0:
            style.append(f"width:{_px(img.width_hu)}")
            if img.height_hu > 0:
                # 쪽보다 넓으면 줄이되 원본 상자의 가로세로 비는 지킨다.
                style.append(f"aspect-ratio:{img.width_hu}/{img.height_hu}")
        style_attr = f' style="{";".join(style)}"' if style else ""
        return f'<img src="{uri}" alt=""{style_attr}/>'

    # ── 모양 CSS ─────────────────────────────────────────────

    def _shape_css(self) -> str:
        info = self.info
        rules: list[str] = []
        for sid in sorted(self.used_char):
            st = info.char_shapes[sid]
            decl = [f"font-size:{_pt(st.size_pt)}"]
            if st.bold:
                decl.append("font-weight:bold")
            if st.italic:
                decl.append("font-style:italic")
            lines = [n for n, on in (("underline", st.underline), ("overline", st.overline),
                                     ("line-through", st.strike)) if on]
            if lines:
                decl.append(f"text-decoration:{' '.join(lines)}")
            if st.superscript or st.subscript:
                decl.append(f"vertical-align:{'super' if st.superscript else 'sub'};"
                            "font-size:smaller")
            if st.color:
                decl.append(f"color:#{st.color}")
            face = None
            if st.face_id is not None and 0 <= st.face_id < len(info.ko_faces):
                face = _safe_font(info.ko_faces[st.face_id])
            if face:
                decl.append(f"font-family:'{face}','Malgun Gothic','Apple SD Gothic Neo',"
                            "'Noto Sans KR',sans-serif")
            rules.append(f".{ROOT_CLASS} .c{sid}{{{';'.join(decl)}}}")
        for pid in sorted(self.used_para):
            pp = info.para_shapes[pid]
            decl = []
            if pp.align == "distribute":
                decl.append("text-align:justify;text-align-last:justify")
            elif pp.align != "left":
                decl.append(f"text-align:{pp.align}")
            if not pp.korean_by_char:  # 한글은 어절 단위로 줄을 나눈다 (한글 기본)
                decl.append("word-break:keep-all")
            if pp.line_spacing is not None:
                decl.append(f"line-height:{pp.line_spacing:.2f}")
            if pp.space_before_pt > 0.05:
                decl.append(f"margin-top:{_pt(pp.space_before_pt)}")
            if pp.space_after_pt > 0.05:
                decl.append(f"margin-bottom:{_pt(pp.space_after_pt)}")
            left = pp.left_pt + max(0.0, -pp.indent_pt)
            if left > 0.05:
                decl.append(f"margin-left:{_pt(left)}")
            if pp.right_pt > 0.05:
                decl.append(f"margin-right:{_pt(pp.right_pt)}")
            if abs(pp.indent_pt) > 0.05:
                decl.append(f"text-indent:{_pt(pp.indent_pt)}")
            if decl:
                rules.append(f".{ROOT_CLASS} .p{pid}{{{';'.join(decl)}}}")
        for fid in sorted(self.used_fill):
            bf = info.border_fills[fid]
            decl = []
            if bf.bg:
                decl.append(f"background-color:#{bf.bg}")
            if bf.sides is not None:
                for name, side in zip(("left", "right", "top", "bottom"), bf.sides, strict=False):
                    decl.append(f"border-{name}:{_border(side)}")
            if decl:
                rules.append(f".{ROOT_CLASS} .f{fid}{{{';'.join(decl)}}}")
        return "".join(rules)


def _browser_image(blob: bytes, ext: str) -> tuple[bytes, str]:
    """(그림 바이트, MIME) — bmp/tif 는 png 로 바꾼다. 못 바꾸면 bmp 는 그대로,
    tif 는 싣지 않는다(빈 바이트)."""
    if ext not in _TO_PNG:
        return blob, _IMAGE_MIME[ext]
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(blob)) as im:
            if im.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue(), "image/png"
    except Exception:  # noqa: BLE001 — 못 읽는 그림 하나로 문서를 버리지 않는다
        return (blob, "image/bmp") if ext == "bmp" else (b"", "")


def _visible(paras: list[_Para]) -> bool:
    """보일 내용(글자·첨부)이 있는 문단이 하나라도 있는가."""
    return any(p.text.strip() or p.attachments for p in paras)


def _fits_in_line(box: _TextBox) -> bool:
    """글상자 내용이 줄 안에 들어가는가 — 표와 떠 있는 개체가 없어야 한다."""
    for p in box.paras:
        for a in p.attachments:
            if isinstance(a, _Table):
                return False
            if isinstance(a, _TextBox) and not _fits_in_line(a):
                return False
            if not isinstance(a, _Note) and not getattr(a, "inline", False):
                return False
    return True


def _border(side: _BorderSide) -> str:
    style = _STROKE_CSS.get(side.stroke, "solid")
    if style == "none":
        return "none"
    width = side.width_mm
    if style == "double":
        width = max(width, 0.8)  # 두 줄이 보이려면 3px 는 있어야 한다
    return f"{width:g}mm {style} #{side.color}"
