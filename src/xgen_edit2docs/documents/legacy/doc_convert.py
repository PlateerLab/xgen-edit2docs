"""Word 97 바이너리(.doc) → DOCX 변환 (MS-DOC 스펙의 piece table 복원).

.doc 의 본문은 연속 텍스트가 아니다 — FIB 가 가리키는 Clx(piece table)가
"CP 구간 → 파일 오프셋(fc) + 압축 여부" 조각들을 나열하고, 유니코드(UTF-16LE)
조각과 8비트(레거시 코드페이지) 조각이 섞인다. 이 조각들을 CP 순서로 이어야
정확한 본문이 된다 (fcMin..fcMac 만 읽는 옛 방식은 Word97+ 에서 어긋난다).

    WordDocument 스트림   FIB: wIdent(0xA5EC)@0, flags@0x0A(bit9 → 1Table),
                          fcClx@0x01A2, lcbClx@0x01A6
    0Table/1Table 스트림  Clx: (Prc)* 그리고 Pcdt(0x02 + lcb + PlcPcd)
    PlcPcd                (n+1)×CP UINT32 + n×PCD(2B flags, 4B fc, 2B prm)
                          fc bit30 = 8비트 압축 조각 (offset = fc/2)

특수 문자: 0x0D 문단 끝, 0x07 표 셀/행 마크(탭으로), 0x0B 강제 줄바꿈,
0x0C 페이지 나눔, 0x13/0x14/0x15 필드(명령부는 버리고 결과만), 0x01/0x08
개체 앵커(버림). 충실도 범위: 문단 구조 + 본문 전체 — 글자 서식(CHPX FKP)
은 범위 밖(기본 서식으로 렌더, 내용 유실 없음).
"""

from __future__ import annotations

import io
import struct
from typing import List

from . import LegacyConvertError

_FC_COMPRESSED = 0x40000000
_FC_MASK = 0x3FFFFFFF


def _read_pieces(word_stream: bytes, table_stream: bytes) -> str:
    if len(word_stream) < 0x01AA:
        raise LegacyConvertError("doc FIB 가 너무 짧습니다")
    (w_ident,) = struct.unpack_from("<H", word_stream, 0)
    if w_ident != 0xA5EC:
        raise LegacyConvertError("doc 시그니처(wIdent)가 아닙니다")

    fc_clx, lcb_clx = struct.unpack_from("<II", word_stream, 0x01A2)
    if lcb_clx == 0 or fc_clx + lcb_clx > len(table_stream):
        raise LegacyConvertError("doc piece table(Clx) 위치가 잘못됐습니다")
    clx = table_stream[fc_clx:fc_clx + lcb_clx]

    # Clx = (clxtGrpprl Prc)* clxtPlcPcd Pcdt
    pos = 0
    while pos < len(clx) and clx[pos] == 0x01:
        (cb,) = struct.unpack_from("<H", clx, pos + 1)
        pos += 3 + cb
    if pos >= len(clx) or clx[pos] != 0x02:
        raise LegacyConvertError("doc Pcdt 마커가 없습니다")
    (lcb,) = struct.unpack_from("<I", clx, pos + 1)
    plc = clx[pos + 5:pos + 5 + lcb]
    if len(plc) < 4 or (len(plc) - 4) % 12 != 0:
        raise LegacyConvertError("doc PlcPcd 크기가 어긋납니다")
    n = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    out: List[str] = []
    for i in range(n):
        _flags, fc_raw, _prm = struct.unpack_from("<HIH", plc, 4 * (n + 1) + 8 * i)
        count = cps[i + 1] - cps[i]
        if count <= 0:
            continue
        if fc_raw & _FC_COMPRESSED:
            off = (fc_raw & _FC_MASK) // 2
            raw = word_stream[off:off + count]
            out.append(raw.decode("cp1252", errors="replace"))
        else:
            off = fc_raw & _FC_MASK
            raw = word_stream[off:off + count * 2]
            out.append(raw.decode("utf-16le", errors="replace"))
    return "".join(out)


def _strip_fields(text: str) -> str:
    """필드 명령부(0x13..0x14)는 버리고 결과부(0x14..0x15)만 남긴다."""
    out: List[str] = []
    depth_cmd = 0
    for ch in text:
        code = ord(ch)
        if code == 0x13:
            depth_cmd += 1
            continue
        if code == 0x14:
            if depth_cmd > 0:
                depth_cmd -= 1
            continue
        if code == 0x15:
            continue
        if depth_cmd > 0:
            continue
        out.append(ch)
    return "".join(out)


def doc_to_docx(content: bytes) -> bytes:
    import olefile
    from docx import Document

    if not olefile.isOleFile(io.BytesIO(content)):
        raise LegacyConvertError("doc 가 아닙니다 (OLE 복합문서 아님)")
    ole = olefile.OleFileIO(io.BytesIO(content))
    try:
        if not ole.exists("WordDocument"):
            raise LegacyConvertError("doc WordDocument 스트림이 없습니다")
        word_stream = ole.openstream("WordDocument").read()
        (flags,) = struct.unpack_from("<H", word_stream, 0x0A)
        table_name = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(table_name):
            # 저장 도중 죽은 파일 등 — 반대쪽이라도 있으면 그걸 쓴다.
            table_name = "0Table" if table_name == "1Table" else "1Table"
            if not ole.exists(table_name):
                raise LegacyConvertError("doc Table 스트림이 없습니다")
        table_stream = ole.openstream(table_name).read()

        text = _strip_fields(_read_pieces(word_stream, table_stream))

        doc = Document()
        # 0x0D = 문단, 0x0C = 페이지 나눔(문단 경계 + 하드 브레이크), 0x07 = 셀 마크.
        for page_part_idx, page_part in enumerate(text.split("\x0c")):
            if page_part_idx > 0:
                from docx.enum.text import WD_BREAK

                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            for raw_para in page_part.split("\r"):
                cleaned = (
                    raw_para
                    .replace("\x07", "\t")   # 표 셀/행 마크 — 구조 대신 탭 구분
                    .replace("\x0b", "\n")   # 강제 줄바꿈
                    .replace("\x01", "")     # 그림 앵커
                    .replace("\x08", "")     # 그리기 개체 앵커
                    .replace("\x1e", "-")    # 줄바꿈 없는 하이픈
                    .replace("\x1f", "")     # 소프트 하이픈
                    .replace("\x00", "")
                )
                para = doc.add_paragraph()
                for j, line in enumerate(cleaned.split("\n")):
                    if j > 0:
                        para.add_run().add_break()
                    if line:
                        para.add_run(line)

        buf = io.BytesIO()
        doc.save(buf)
        return buf.getvalue()
    finally:
        ole.close()
