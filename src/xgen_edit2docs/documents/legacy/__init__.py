"""Legacy / 한글 문서 포맷 → OOXML 정규화 컨버터 (native, no subprocess).

렌더 파이프라인의 앞단이다: hwp/hwpx/doc → docx, xls → xlsx, ppt → pptx 로
**구조를 보존해**(문단·런 스타일·표·이미지·페이지 기하) 변환한 뒤, 검증된
네이티브 페이지 엔진(docx_pages / xlsx_pages / pptx→svg)이 렌더한다.

텍스트 추출기가 아니다 — 각 모듈은 해당 포맷의 실제 바이너리/XML 스펙을
직접 파싱한다:

    hwpx_convert  OWPML(zip+XML) — Contents/header.xml 의 charPr/paraPr 를
                  해석해 런 스타일까지 살린다. BinData 이미지 포함.
    hwp_convert   HWP 5.0 바이너리 — OLE 복합문서 + zlib 레코드 스트림.
                  레코드 헤더(tagid/level/size), DocInfo 의 CHAR_SHAPE,
                  BodyText 의 PARA_TEXT/PARA_CHAR_SHAPE/TABLE/PAGE_DEF.
    doc_convert   Word 97 바이너리 — FIB + piece table 로 전체 텍스트를
                  유니코드/압축 혼합 조각에서 복원, 문단 구조 유지.
    xls_convert   BIFF8 — SST/LABELSST/NUMBER/RK/MULRK/FORMULA(캐시值)/
                  FONT/XF/COLINFO/ROW/MERGEDCELLS 를 openpyxl 워크북으로.
    ppt_convert   PowerPoint 97 바이너리 — 레코드 트리에서 슬라이드별
                  텍스트(TextHeader/Chars/Bytes)를 모아 pptx 슬라이드로.

공개 API: :func:`convert_to_ooxml`.
"""

from __future__ import annotations

from pathlib import Path

#: 이 계층이 정규화하는 확장자 → (변환 후 확장자)
LEGACY_FORMATS = {
    "hwp": "docx",
    "hwpx": "docx",
    "doc": "docx",
    "xls": "xlsx",
    "ppt": "pptx",
}


class LegacyConvertError(ValueError):
    """원본이 해당 포맷이 아니거나(시그니처 불일치) 구조가 깨져 변환 불가."""


def convert_to_ooxml(content: bytes, fmt: str) -> tuple[bytes, str]:
    """``fmt`` 원본 바이트를 OOXML 로 변환해 (bytes, 새 포맷) 을 돌려준다."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "hwpx":
        from .hwpx_convert import hwpx_to_docx

        return hwpx_to_docx(content), "docx"
    if fmt == "hwp":
        from .hwp_convert import hwp_to_docx

        return hwp_to_docx(content), "docx"
    if fmt == "doc":
        from .doc_convert import doc_to_docx

        return doc_to_docx(content), "docx"
    if fmt == "xls":
        from .xls_convert import xls_to_xlsx

        return xls_to_xlsx(content), "xlsx"
    if fmt == "ppt":
        from .ppt_convert import ppt_to_pptx

        return ppt_to_pptx(content), "pptx"
    raise LegacyConvertError(f"unsupported legacy format: {fmt}")


def normalize_to_ooxml(path: str | Path) -> Path:
    """레거시 파일이면 같은 디렉터리에 OOXML 사본을 만들고 그 경로를 돌려준다.

    이미 OOXML 이면 원본 경로 그대로. 변환 결과는 ``<stem>.converted.<ext>``
    로 캐시된다 — 같은 원본의 재렌더는 재변환 없이 바로 페이지 엔진으로 간다.
    """
    p = Path(path)
    ext = p.suffix.lower().lstrip(".")
    if ext not in LEGACY_FORMATS:
        return p
    out = p.with_name(f"{p.stem}.converted.{LEGACY_FORMATS[ext]}")
    if out.exists() and out.stat().st_mtime >= p.stat().st_mtime:
        return out
    converted, _ = convert_to_ooxml(p.read_bytes(), ext)
    out.write_bytes(converted)
    return out
