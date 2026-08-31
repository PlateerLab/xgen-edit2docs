"""HWP 5.0 (한글 바이너리) → DOCX 구조 보존 변환.

HWP 5.0 은 OLE 복합문서다 (레퍼런스: 한컴 공개 스펙 + reference_data/pyhwp):

    FileHeader          32B 시그니처 + version + flags(bit0 압축, bit1 암호)
    DocInfo             레코드 스트림 — CHAR_SHAPE(글자 모양) 등
    BodyText/Section0…  레코드 스트림 — PARA_HEADER/PARA_TEXT/
                        PARA_CHAR_SHAPE/CTRL_HEADER/TABLE/LIST_HEADER/PAGE_DEF

레코드 헤더는 UINT32 하나: tagid(10b) | level(10b) | size(12b, 0xFFF 이면
다음 UINT32 가 실제 크기). 압축 플래그가 켜진 스트림은 raw zlib(-15).

본문 텍스트는 UTF-16LE 이되 0x00~0x1F 코드가 컨트롤 문자다 — 문자형(1워드)/
인라인·확장형(8워드) 크기 표에 따라 건너뛴다 (pyhwp ControlChar 표와 동일).
확장형 0x0B(표/그리기 개체)가 표의 앵커다.

충실도 범위: 문단·런 스타일(크기/굵게/기울임/밑줄/색)·표(rows×cols, 셀
텍스트)·페이지 크기/여백. 그림·수식·글상자 위치는 범위 밖(글상자 텍스트는
본문으로 이어붙는다 — 내용 유실은 없다).
"""

from __future__ import annotations

import io
import re
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import LegacyConvertError

# ── 레코드 태그 (HWPTAG_BEGIN = 0x10) ───────────────────────────
_TAG_BEGIN = 0x10
TAG_CHAR_SHAPE = _TAG_BEGIN + 5        # 0x15 DocInfo
TAG_PARA_HEADER = _TAG_BEGIN + 50      # 0x42
TAG_PARA_TEXT = _TAG_BEGIN + 51        # 0x43
TAG_PARA_CHAR_SHAPE = _TAG_BEGIN + 52  # 0x44
TAG_CTRL_HEADER = _TAG_BEGIN + 55      # 0x47
TAG_LIST_HEADER = _TAG_BEGIN + 56      # 0x48
TAG_PAGE_DEF = _TAG_BEGIN + 57         # 0x49
TAG_TABLE = _TAG_BEGIN + 61            # 0x4D

#: 컨트롤 문자 크기 표 (pyhwp ControlChar 와 동일) — 코드 → 워드 수.
_CTRL_SIZES = {
    0x00: 1, 0x01: 8, 0x02: 8, 0x03: 8, 0x04: 8, 0x05: 8, 0x06: 8, 0x07: 8,
    0x08: 8, 0x09: 8, 0x0A: 1, 0x0B: 8, 0x0C: 8, 0x0D: 1, 0x0E: 8, 0x0F: 8,
    0x10: 8, 0x11: 8, 0x12: 8, 0x13: 8, 0x14: 8, 0x15: 8, 0x16: 8, 0x17: 8,
    0x18: 1, 0x1E: 1, 0x1F: 1,
}
_CTRL_RE = re.compile(rb"[\x00-\x1f]\x00")

_HWPUNIT_PER_INCH = 7200.0
_EMU_PER_INCH = 914400.0


def _hu_to_emu(v: int) -> int:
    return int(round(v * _EMU_PER_INCH / _HWPUNIT_PER_INCH))


@dataclass
class _CharShape:
    size_pt: float = 10.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: Optional[str] = None  # RRGGBB


@dataclass
class _Para:
    """레코드에서 복원한 문단 — (텍스트, charshape 경계) 쌍."""
    text: str = ""
    #: (문자 위치, charshape id) — PARA_CHAR_SHAPE 그대로.
    shape_spans: List[tuple] = field(default_factory=list)
    #: 이 문단 텍스트 안의 표 앵커(확장 컨트롤 0x0B) 존재 여부.
    has_table_anchor: bool = False


@dataclass
class _Table:
    rows: int = 0
    cols: int = 0
    #: 셀별 문단 목록 (LIST_HEADER 순서 = 행 우선).
    cells: List[List[_Para]] = field(default_factory=list)


@dataclass
class _PageDef:
    width: int = 59528
    height: int = 84188
    left: int = 8504
    right: int = 8504
    top: int = 5668
    bottom: int = 4252


# ── 저수준 파서 ────────────────────────────────────────────────


def _iter_records(data: bytes):
    """(tagid, level, payload) 를 순서대로 — 손상 시 그 지점에서 멈춘다."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        (hdr,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tagid = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                return
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        if tagid == 0 and size == 0:
            return  # 패딩/끝
        if pos + size > n:
            return
        yield tagid, level, data[pos:pos + size]
        pos += size


def _decompress(raw: bytes) -> bytes:
    """HWP 압축 스트림 = raw deflate. 뒤에 패딩이 붙어 있어도 관용한다."""
    d = zlib.decompressobj(-15)
    out = d.decompress(raw)
    out += d.flush()
    return out


def _parse_text_chunks(payload: bytes) -> tuple[str, bool]:
    """PARA_TEXT → (본문 텍스트, 표 앵커 여부). 탭/줄바꿈은 보존."""
    parts: List[str] = []
    has_table = False
    idx, n = 0, len(payload)
    while idx < n:
        m = _CTRL_RE.search(payload, idx)
        # 홀수 오프셋 매치는 UTF-16 상위바이트 우연 — 다음 짝수로.
        while m is not None and (m.start() & 1):
            m = _CTRL_RE.search(payload, m.start() + 1)
        ctrl = m.start() if m is not None else n
        if idx < ctrl:
            parts.append(payload[idx:ctrl].decode("utf-16le", errors="replace"))
        if m is None:
            break
        code = payload[ctrl]
        words = _CTRL_SIZES.get(code, 1)
        if code == 0x09:
            parts.append("\t")
        elif code == 0x0A:
            parts.append("\n")
        elif code == 0x0B:
            has_table = True
        idx = ctrl + words * 2
    return "".join(parts), has_table


def _parse_char_shape(payload: bytes) -> _CharShape:
    """DocInfo CHAR_SHAPE — 표 28/30 레이아웃 (pyhwp binmodel 과 동일 오프셋).

    FontFace 7×WORD(14) + 폭/자간/상대크기/위치 7×BYTE ×4(28) = 42,
    INT32 basesize(pt×100), UINT32 flags(bit0 italic, bit1 bold,
    bit2-3 underline), INT8×2 shadow, COLORREF text_color(0x00BBGGRR).
    """
    st = _CharShape()
    if len(payload) >= 46:
        (base,) = struct.unpack_from("<i", payload, 42)
        if 100 <= base <= 50000:
            st.size_pt = base / 100.0
    if len(payload) >= 50:
        (flags,) = struct.unpack_from("<I", payload, 46)
        st.italic = bool(flags & 0x1)
        st.bold = bool(flags & 0x2)
        st.underline = ((flags >> 2) & 0x3) == 1
    if len(payload) >= 56:
        (colorref,) = struct.unpack_from("<I", payload, 52)
        r, g, b = colorref & 0xFF, (colorref >> 8) & 0xFF, (colorref >> 16) & 0xFF
        if (r, g, b) != (255, 255, 255):
            st.color = f"{r:02X}{g:02X}{b:02X}"
    return st


def _parse_page_def(payload: bytes) -> _PageDef:
    pd = _PageDef()
    if len(payload) >= 24:
        w, h, left, right, top, bottom = struct.unpack_from("<6I", payload, 0)
        # 손상/이형 방어 — 종이 크기로 말이 되는 값만 채택 (1~50 inch).
        if 7200 <= w <= 360000 and 7200 <= h <= 360000:
            pd.width, pd.height = w, h
            pd.left, pd.right, pd.top, pd.bottom = left, right, top, bottom
    return pd


# ── 섹션 → 문단/표 시퀀스 ──────────────────────────────────────


def _parse_section(data: bytes) -> tuple[List[object], Optional[_PageDef]]:
    """BodyText/Section 레코드 → [_Para | _Table] 블록 목록 (+PAGE_DEF).

    레벨 규칙: 본문 문단은 level 0. 표는 CTRL_HEADER('tbl ') 다음의 TABLE
    레코드로 열리고, LIST_HEADER 마다 다음 셀(행 우선)로 넘어가며, 그 아래
    깊은 level 의 문단들이 셀 내용이다. 표보다 얕은 레벨의 레코드가 오면
    표가 닫힌다.
    """
    blocks: List[object] = []
    page: Optional[_PageDef] = None

    open_table: Optional[_Table] = None
    table_level = 0
    cur_para: Optional[_Para] = None

    def close_para():
        nonlocal cur_para
        cur_para = None

    def close_table():
        nonlocal open_table
        open_table = None

    for tagid, level, payload in _iter_records(data):
        if tagid == TAG_PAGE_DEF and page is None:
            page = _parse_page_def(payload)
            continue

        if open_table is not None and level <= table_level and tagid != TAG_LIST_HEADER:
            close_table()

        if tagid == TAG_CTRL_HEADER:
            # chid 는 리틀엔디언 4바이트 — 'tbl ' 은 b' lbt' 로 저장된다.
            chid = payload[:4][::-1].decode("ascii", errors="replace") if len(payload) >= 4 else ""
            if chid == "tbl ":
                open_table = _Table()
                table_level = level
                blocks.append(open_table)
            close_para()
            continue

        if tagid == TAG_TABLE and open_table is not None:
            if len(payload) >= 8:
                rows, cols = struct.unpack_from("<HH", payload, 4)
                if 0 < rows <= 2000 and 0 < cols <= 256:
                    open_table.rows, open_table.cols = rows, cols
            continue

        if tagid == TAG_LIST_HEADER:
            if open_table is not None and level > table_level:
                open_table.cells.append([])
            close_para()
            continue

        if tagid == TAG_PARA_HEADER:
            cur_para = _Para()
            if open_table is not None and level > table_level and open_table.cells:
                open_table.cells[-1].append(cur_para)
            else:
                if open_table is not None:
                    close_table()
                blocks.append(cur_para)
            continue

        if tagid == TAG_PARA_TEXT and cur_para is not None:
            text, has_table = _parse_text_chunks(payload)
            cur_para.text += text
            cur_para.has_table_anchor = cur_para.has_table_anchor or has_table
            continue

        if tagid == TAG_PARA_CHAR_SHAPE and cur_para is not None:
            for off in range(0, len(payload) - 7, 8):
                pos, shape_id = struct.unpack_from("<II", payload, off)
                cur_para.shape_spans.append((pos, shape_id))
            continue

    return blocks, page


# ── DOCX 조립 ──────────────────────────────────────────────────


def hwp_to_docx(content: bytes) -> bytes:
    import olefile
    from docx import Document
    from docx.shared import Emu, Pt, RGBColor

    if not olefile.isOleFile(io.BytesIO(content)):
        raise LegacyConvertError("hwp 가 아닙니다 (OLE 복합문서 아님)")
    ole = olefile.OleFileIO(io.BytesIO(content))
    try:
        if not ole.exists("FileHeader"):
            raise LegacyConvertError("hwp FileHeader 스트림이 없습니다")
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            raise LegacyConvertError("hwp 시그니처가 아닙니다")
        (flags,) = struct.unpack_from("<I", header, 36)
        compressed = bool(flags & 0x1)
        if flags & 0x2:
            raise LegacyConvertError("암호로 보호된 hwp 는 열 수 없습니다")
        if flags & 0x4:
            raise LegacyConvertError("배포용(암호화) hwp 는 열 수 없습니다")

        def read_stream(name: str) -> bytes:
            raw = ole.openstream(name).read()
            return _decompress(raw) if compressed else raw

        # DocInfo — 글자 모양 목록 (id = 등장 순서)
        char_shapes: List[_CharShape] = []
        if ole.exists("DocInfo"):
            for tagid, _level, payload in _iter_records(read_stream("DocInfo")):
                if tagid == TAG_CHAR_SHAPE:
                    char_shapes.append(_parse_char_shape(payload))

        # BodyText/Section* — 숫자 순
        section_names = sorted(
            ("/".join(entry) for entry in ole.listdir()
             if len(entry) == 2 and entry[0] == "BodyText"
             and entry[1].startswith("Section")),
            key=lambda s: int(re.sub(r"\D", "", s) or 0),
        )
        if not section_names:
            raise LegacyConvertError("hwp 본문(BodyText/Section*)이 없습니다")

        doc = Document()
        page_applied = False

        def shape_of(idx: int) -> Optional[_CharShape]:
            return char_shapes[idx] if 0 <= idx < len(char_shapes) else None

        def emit_runs(para_obj, para: _Para) -> None:
            text = para.text
            if not text:
                return
            spans = sorted(para.shape_spans) or [(0, -1)]
            for i, (start, shape_id) in enumerate(spans):
                end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
                chunk = text[start:end]
                if not chunk:
                    continue
                st = shape_of(shape_id)
                # 줄바꿈은 run break 로 — 문단은 유지된다.
                pieces = chunk.split("\n")
                for j, piece in enumerate(pieces):
                    if j > 0:
                        para_obj.add_run().add_break()
                    if not piece:
                        continue
                    run = para_obj.add_run(piece)
                    if st is not None:
                        run.font.size = Pt(st.size_pt)
                        if st.bold:
                            run.bold = True
                        if st.italic:
                            run.italic = True
                        if st.underline:
                            run.underline = True
                        if st.color:
                            run.font.color.rgb = RGBColor.from_string(st.color)

        for sec_name in section_names:
            blocks, page = _parse_section(read_stream(sec_name))
            if page is not None and not page_applied:
                sec = doc.sections[0]
                sec.page_width = Emu(_hu_to_emu(page.width))
                sec.page_height = Emu(_hu_to_emu(page.height))
                sec.left_margin = Emu(_hu_to_emu(page.left))
                sec.right_margin = Emu(_hu_to_emu(page.right))
                sec.top_margin = Emu(_hu_to_emu(page.top))
                sec.bottom_margin = Emu(_hu_to_emu(page.bottom))
                page_applied = True
            for block in blocks:
                if isinstance(block, _Para):
                    emit_runs(doc.add_paragraph(), block)
                elif isinstance(block, _Table) and block.rows and block.cols:
                    table = doc.add_table(rows=block.rows, cols=block.cols)
                    table.style = "Table Grid"
                    for ci, cell_paras in enumerate(block.cells[: block.rows * block.cols]):
                        cell = table.cell(ci // block.cols, ci % block.cols)
                        first = True
                        for para in cell_paras:
                            if first:
                                cell.paragraphs[0].text = ""
                                target = cell.paragraphs[0]
                                first = False
                            else:
                                target = cell.add_paragraph()
                            emit_runs(target, para)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    finally:
        ole.close()
