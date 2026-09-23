"""EPUB 은 지원하지 않는다 — 들어오면 조용히 비지 말고 분명히 거부한다.

2026-09-23: EPUB 변환은 ebooklib(AGPL-3.0) 위에 있었다. XGEN 은 AGPL 의존을
두지 않으므로 EPUB 경로를 통째로 걷어냈다. 예전에는 ebooklib 이 없으면 변환기가
빈 문자열을 돌려줘서 "근거 자료가 비어 있는" 결과가 조용히 나왔다. 이제는 모든
입구가 EPUB 을 이름으로 거부한다.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pydantic
import pytest

from xgen_edit2docs.simple import _convert_requests
from xgen_edit2docs.tools.convert import ConvertRequest, convert_to_markdown

_ROOT = Path(__file__).resolve().parents[2]


def test_a_generate_or_edit_source_is_refused(tmp_path):
    book = tmp_path / "book.epub"
    book.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError, match="Unsupported source format: book.epub"):
        _convert_requests([book])


def test_the_convert_tool_does_not_guess_a_format_for_it():
    req = ConvertRequest(content=b"PK\x03\x04", original_filename="book.epub")
    with pytest.raises(ValueError, match="Could not infer source_type"):
        convert_to_markdown(req)


def test_epub_is_not_a_source_type():
    with pytest.raises(pydantic.ValidationError):
        ConvertRequest(source_type="epub", content=b"PK\x03\x04")


def test_hosted_uploads_are_refused_instead_of_read_as_pdf():
    """호스티드 입구는 모르는 MIME 을 pdf 로 떨어뜨린다 — EPUB 은 그 전에 막는다."""
    from xgen_edit2docs.mcp.server import _infer_source_type as mcp_infer
    from xgen_edit2docs.workers.executors.generate_deck import _infer_source_type as job_infer

    for infer in (mcp_infer, job_infer):
        with pytest.raises(ValueError, match="EPUB"):
            infer("application/epub+zip")
        assert infer("application/pdf") == "pdf"


def test_the_markdown_converter_lists_no_epub_path():
    from xgen_edit2docs.core.source_to_md import doc_to_md

    assert ".epub" not in doc_to_md.NATIVE_FORMATS
    assert not hasattr(doc_to_md, "_convert_epub")


# AGPL/GPL 계열 — XGEN 은 이 의존을 두지 않는다 (PyMuPDF·ebooklib·pyhwp 를 걷어낸 이유).
_FORBIDDEN = {"pymupdf", "pymupdfb", "pymupdf4llm", "fitz", "ebooklib", "pyhwp", "xgen-contextifier"}


def _requirement_names() -> set[str]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        reqs.extend(r for r in group if isinstance(r, str))
    return {re.split(r"[\s<>=!~;\[@]", r, maxsplit=1)[0].strip().lower().replace("_", "-") for r in reqs}


def test_no_agpl_dependency_comes_back():
    names = _requirement_names()
    assert names, "pyproject 의존 목록을 읽지 못했다"
    assert not (names & _FORBIDDEN), f"AGPL 의존이 다시 들어왔다: {sorted(names & _FORBIDDEN)}"
