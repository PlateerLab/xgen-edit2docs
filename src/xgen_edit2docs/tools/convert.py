"""Convert tool: source documents -> Markdown.

Wraps the core engine's source_to_md/* scripts behind a single stateless
function. The wrapper hides the disk-based interface: callers pass bytes (or
a URL), and get back the markdown string + extracted metadata.

Korean filenames in `original_filename` are preserved through the temp
workspace (the temp file inside the workspace uses an ASCII name so the
underlying engine never sees non-ASCII paths, but the response echoes back
whatever the caller supplied).
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import Field, model_validator

from ._workspace import temp_workspace, write_bytes
from .types import (
    CostBreakdown,
    SourceFormat,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)

# Extension -> SourceFormat mapping. Used when callers don't set source_type explicitly.
_EXT_TO_FORMAT: dict[str, SourceFormat] = {
    ".pdf": "pdf",
    ".docx": "docx", ".doc": "doc", ".odt": "doc", ".rtf": "doc",
    ".pptx": "pptx",
    ".xlsx": "xlsx", ".xlsm": "xlsm",
    ".html": "html", ".htm": "html",
    ".epub": "epub",
    ".ipynb": "ipynb",
}


class ConvertRequest(ToolRequest):
    """Inputs for `convert_to_markdown`."""

    source_type: SourceFormat | None = Field(
        default=None,
        description="Source format. Inferred from `original_filename` if omitted.",
    )
    content: bytes | None = Field(default=None, description="File bytes (binary).")
    url: str | None = Field(default=None, description="Required when source_type='url'.")
    original_filename: str | None = Field(
        default=None,
        description="Filename the caller wants to preserve in the response. May be Korean / any Unicode.",
    )

    @model_validator(mode="after")
    def _check_one_source(self) -> "ConvertRequest":
        has_content = self.content is not None
        has_url = self.url is not None
        if has_content == has_url:
            raise ValueError("Exactly one of `content` or `url` must be provided")
        if has_url and self.source_type not in (None, "url"):
            raise ValueError("source_type must be 'url' (or omitted) when `url` is set")
        if has_content and self.source_type == "url":
            raise ValueError("source_type='url' requires `url`, not `content`")
        return self


class ConvertResponse(ToolResponse):
    markdown: str
    detected_format: SourceFormat
    original_filename: str | None
    char_count: int
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def convert_to_markdown(req: ConvertRequest) -> ConvertResponse:
    """Convert a single source into Markdown.

    Raises:
        ValueError: invalid request (missing/conflicting fields, unknown format).
        RuntimeError: the underlying engine failed (empty markdown returned).
    """
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    fmt = req.source_type or _infer_format(req)
    if fmt is None:
        raise ValueError(
            f"Could not infer source_type. Provide source_type explicitly. "
            f"Filename: {req.original_filename!r}"
        )

    if fmt == "url":
        if req.url is None:
            raise ValueError("source_type='url' requires `url`")
        markdown = _convert_url(req.url, warnings)
    else:
        if req.content is None:
            raise ValueError(f"source_type={fmt!r} requires `content` bytes")
        markdown = _convert_bytes(req.content, fmt, warnings)

    if not markdown.strip():
        raise RuntimeError("Conversion produced empty markdown")

    return ConvertResponse(
        markdown=markdown,
        detected_format=fmt,
        original_filename=req.original_filename,
        char_count=len(markdown),
        cost=CostBreakdown(duration_seconds=time.perf_counter() - started),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Format dispatch (internal)
# ---------------------------------------------------------------------------

def _infer_format(req: ConvertRequest) -> SourceFormat | None:
    if req.url:
        return "url"
    if req.original_filename:
        ext = Path(req.original_filename).suffix.lower()
        return _EXT_TO_FORMAT.get(ext)
    return None


def _convert_bytes(content: bytes, fmt: SourceFormat, warnings: list[WarningEntry]) -> str:
    """Save bytes to an ASCII-named temp file, invoke the engine, return markdown."""
    ext = _format_to_ext(fmt)
    with temp_workspace(prefix=f"xgen_edit2docs-convert-{fmt}-") as ws:
        input_path = write_bytes(ws, f"source{ext}", content)
        output_path = ws / "output.md"

        if fmt == "pdf":
            from ..core.source_to_md.pdf_to_md import extract_pdf_to_markdown
            extract_pdf_to_markdown(str(input_path), str(output_path), images="filtered")
        elif fmt in {"docx", "doc", "html", "epub", "ipynb"}:
            from ..core.source_to_md.doc_to_md import convert_to_markdown as _doc_convert
            _doc_convert(str(input_path), str(output_path))
        elif fmt == "pptx":
            from ..core.source_to_md.ppt_to_md import convert_presentation_to_markdown
            convert_presentation_to_markdown(str(input_path), str(output_path))
        elif fmt in {"xlsx", "xlsm"}:
            from ..core.source_to_md.excel_to_md import convert_to_markdown as _xls_convert
            _xls_convert(str(input_path), str(output_path))
        else:
            raise ValueError(f"Unsupported source_type: {fmt}")

        if not output_path.exists():
            raise RuntimeError(f"{fmt}_to_md engine did not write output.md")
        return output_path.read_text(encoding="utf-8")


def _convert_url(url: str, warnings: list[WarningEntry]) -> str:
    """Use the engine's process_url to fetch + convert in an isolated workspace."""
    from ..core.source_to_md.web_to_md import process_url

    with temp_workspace(prefix="xgen_edit2docs-convert-url-") as ws:
        output_path = ws / "page.md"
        ok, message, error = process_url(url, output_file=str(output_path))
        if not ok:
            raise RuntimeError(f"web_to_md failed: {message} ({error or 'no detail'})")
        if not output_path.exists():
            raise RuntimeError("web_to_md reported success but did not write output")
        return output_path.read_text(encoding="utf-8")


def _format_to_ext(fmt: SourceFormat) -> str:
    return {
        "pdf": ".pdf",
        "docx": ".docx", "doc": ".doc",
        "pptx": ".pptx",
        "xlsx": ".xlsx", "xlsm": ".xlsm",
        "html": ".html",
        "epub": ".epub",
        "ipynb": ".ipynb",
    }.get(fmt, "")
