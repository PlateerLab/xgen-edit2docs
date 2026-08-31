"""PowerPoint 97 바이너리(.ppt) → PPTX 변환.

'PowerPoint Document' 스트림은 8바이트 헤더(recVer/instance 2B, recType 2B,
recLen 4B) 레코드 트리다. 슬라이드 텍스트의 정본은 DocumentContainer(1000)
안의 SlideListWithText(4080) — SlidePersistAtom(1011)이 슬라이드 경계를,
TextHeaderAtom(3999)이 자리 종류(0/5=제목, 1/6=본문 …)를, TextCharsAtom
(4000, UTF-16LE)/TextBytesAtom(4008, 8비트=UTF-16 하위바이트)이 내용을 준다.
슬라이드 크기는 DocumentAtom(1001)의 master unit(1/576 inch) 값.

충실도 범위: 슬라이드 수·순서, 자리별 텍스트(제목 큰 글씨/본문 문단),
슬라이드 크기. 도형 좌표(Escher)·이미지·표는 범위 밖 — 텍스트 유실은 없다.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass, field
from typing import List

from . import LegacyConvertError

_RT_DOCUMENT = 1000
_RT_DOCUMENT_ATOM = 1001
_RT_SLIDE_PERSIST_ATOM = 1011
_RT_SLIDE_LIST_WITH_TEXT = 4080
_RT_TEXT_HEADER_ATOM = 3999
_RT_TEXT_CHARS_ATOM = 4000
_RT_TEXT_BYTES_ATOM = 4008

_TITLE_TYPES = {0, 5}  # title / center title

_MASTER_PER_INCH = 576.0
_EMU_PER_INCH = 914400.0


def _iter_records(data: bytes, start: int = 0, end: int | None = None):
    """(rec_ver, rec_type, payload_start, payload_len) 평면 순회."""
    pos = start
    n = len(data) if end is None else end
    while pos + 8 <= n:
        ver_inst, rec_type, rec_len = struct.unpack_from("<HHI", data, pos)
        payload_start = pos + 8
        if rec_len > n - payload_start:
            return
        yield (ver_inst & 0x000F), rec_type, payload_start, rec_len
        pos = payload_start + rec_len


@dataclass
class _SlideText:
    titles: List[str] = field(default_factory=list)
    bodies: List[str] = field(default_factory=list)


def _decode_bytes_atom(payload: bytes) -> str:
    # TextBytesAtom — 각 바이트가 UTF-16 코드포인트의 하위 바이트다.
    return payload.decode("latin-1")


def _collect_slides(data: bytes) -> tuple[List[_SlideText], tuple[int, int]]:
    slide_size_mu = (9144, 6858)  # 10in × 7.5in 기본
    slides: List[_SlideText] = []

    def walk(start: int, end: int, in_sltwt: bool) -> None:
        nonlocal slide_size_mu
        cur_type = 1  # TextHeader 없이 오는 텍스트는 본문 취급
        for rec_ver, rec_type, p_start, p_len in _iter_records(data, start, end):
            if rec_ver == 0xF:  # 컨테이너 — 재귀
                walk(p_start, p_start + p_len,
                     in_sltwt or rec_type == _RT_SLIDE_LIST_WITH_TEXT)
                continue
            if rec_type == _RT_DOCUMENT_ATOM and p_len >= 8:
                w, h = struct.unpack_from("<ii", data, p_start)
                if 576 <= w <= 576 * 100 and 576 <= h <= 576 * 100:
                    slide_size_mu = (w, h)
            if not in_sltwt:
                continue
            if rec_type == _RT_SLIDE_PERSIST_ATOM:
                slides.append(_SlideText())
                cur_type = 1
            elif rec_type == _RT_TEXT_HEADER_ATOM and p_len >= 4:
                (cur_type,) = struct.unpack_from("<I", data, p_start)
            elif rec_type in (_RT_TEXT_CHARS_ATOM, _RT_TEXT_BYTES_ATOM):
                if not slides:
                    slides.append(_SlideText())
                raw = data[p_start:p_start + p_len]
                text = (raw.decode("utf-16le", errors="replace")
                        if rec_type == _RT_TEXT_CHARS_ATOM
                        else _decode_bytes_atom(raw))
                target = (slides[-1].titles if cur_type in _TITLE_TYPES
                          else slides[-1].bodies)
                target.append(text)

    walk(0, len(data), False)
    return slides, slide_size_mu


def ppt_to_pptx(content: bytes) -> bytes:
    import olefile
    from pptx import Presentation
    from pptx.util import Emu, Pt

    if not olefile.isOleFile(io.BytesIO(content)):
        raise LegacyConvertError("ppt 가 아닙니다 (OLE 복합문서 아님)")
    ole = olefile.OleFileIO(io.BytesIO(content))
    try:
        if not ole.exists("PowerPoint Document"):
            raise LegacyConvertError("ppt 'PowerPoint Document' 스트림이 없습니다")
        data = ole.openstream("PowerPoint Document").read()
    finally:
        ole.close()

    slides, (w_mu, h_mu) = _collect_slides(data)
    if not slides:
        raise LegacyConvertError("ppt 에서 슬라이드 텍스트를 찾지 못했습니다")

    prs = Presentation()
    prs.slide_width = Emu(int(w_mu * _EMU_PER_INCH / _MASTER_PER_INCH))
    prs.slide_height = Emu(int(h_mu * _EMU_PER_INCH / _MASTER_PER_INCH))
    blank = prs.slide_layouts[6]

    margin = Emu(int(0.5 * _EMU_PER_INCH))
    title_h = Emu(int(1.1 * _EMU_PER_INCH))
    content_w = prs.slide_width - margin * 2

    for st in slides:
        slide = prs.slides.add_slide(blank)
        y = margin
        if st.titles:
            box = slide.shapes.add_textbox(margin, y, content_w, title_h)
            tf = box.text_frame
            tf.word_wrap = True
            for i, t in enumerate(st.titles):
                para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                run = para.add_run()
                run.text = t.replace("\r", "\n").strip("\n")
                run.font.size = Pt(28)
                run.font.bold = True
            y = y + title_h
        if st.bodies:
            body_h = prs.slide_height - y - margin
            box = slide.shapes.add_textbox(margin, y, content_w, body_h)
            tf = box.text_frame
            tf.word_wrap = True
            first = True
            for chunk in st.bodies:
                # 0x0D = 문단 경계, 0x0B = 문단 내 줄바꿈.
                for line in chunk.replace("\x0b", "\n").split("\r"):
                    para = tf.paragraphs[0] if first else tf.add_paragraph()
                    first = False
                    run = para.add_run()
                    run.text = line
                    run.font.size = Pt(16)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
