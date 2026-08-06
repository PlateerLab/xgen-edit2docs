"""Deterministic DOCX / XLSX engines.

The PPTX pipeline lives in ``core/`` (inherited from edit2ppt); this
package adds the same deterministic building blocks for the other two
OOXML families:

* ``docx_engine`` — markdown -> Word, paragraph outline/addressing,
  targeted text edits, Word -> markdown.
* ``xlsx_engine`` — sheet spec -> Excel, workbook outline, targeted cell
  edits, Excel -> markdown tables.

LLM orchestration on top of these lives in ``tools/generate_doc.py`` and
``tools/edit_doc.py``.
"""

_MIME_TO_FORMAT = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}


def doc_format_of(filename: str | None, mime_type: str | None = None) -> str | None:
    """Resolve an asset's document format ("pptx"|"docx"|"xlsx") or None.

    Extension wins (users upload with correct suffixes); MIME is the
    fallback for presigned uploads that lost the original name.
    """
    if filename:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix in ("pptx", "docx", "xlsx"):
            return suffix
    return _MIME_TO_FORMAT.get(mime_type or "")
