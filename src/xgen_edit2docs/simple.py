"""High-level facade: xgen_edit2docs as a plain Python library.

Everything here is file-path-first, stateless and infra-free — no server,
no database, no object storage. The same five verbs back the agent-tools
surface (``xgen_edit2docs.agent_tools``) and the zero-infra MCP server
(``xgen_edit2docs-mcp``), so an agent and a script call literally the same code.

    from xgen_edit2docs import generate_pptx, edit_pptx, preview_pptx

    generate_pptx("Executive briefing on Q3 sales results", output="deck.pptx")
    result = edit_pptx("deck.pptx", "Change the title of slide 3 to 'Q3 Summary'")
    # Any language works the same way (Korean shown):
    # generate_pptx("Q3 영업 결과 임원 보고", lang="ko-KR", output="deck.pptx")
    svgs = preview_pptx(result.path)

Sync functions wrap async internals with ``asyncio.run``; use the
``async_*`` variants inside an existing event loop.

BYOK: pass ``api_key=`` or set ``ANTHROPIC_API_KEY``. Deterministic verbs
(preview / set_text / analyze) need no key at all.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "claude-opus-4-7"

__all__ = [
    "GenerateResult",
    "EditResult",
    "TextEditsResult",
    # Unified, extension-dispatched verbs (docx / xlsx / pptx)
    "generate_doc",
    "build_doc",
    "edit_doc",
    "preview_doc",
    "set_doc_text",
    "edit_chart",
    "list_charts",
    "analyze_doc",
    "list_doc_parts",
    "get_doc_xml",
    "set_doc_xml",
    "async_generate_doc",
    "async_edit_doc",
    # PPTX-specific surface (full deck pipeline)
    "generate_pptx",
    "edit_pptx",
    "preview_pptx",
    "set_pptx_text",
    "analyze_pptx",
    "async_generate_pptx",
    "async_edit_pptx",
]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class GenerateResult:
    path: Path
    page_count: int
    design_spec: str
    warnings: list[dict] = field(default_factory=list)


@dataclass
class EditResult:
    path: Path
    changed: bool
    reply: str
    page_count: int
    operations: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


@dataclass
class TextEditsResult:
    path: Path
    applied: int
    results: list[dict] = field(default_factory=list)


@dataclass
class ArrangeResult:
    """Output of :func:`arrange_doc` — structural (slide/sheet) edits."""

    path: Path
    applied: int
    results: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


@dataclass
class RenderResult:
    """Output of :func:`render_doc` — files written to disk."""

    paths: list[Path]
    page_count: int
    format: str  # source document format ("pptx" | "docx" | "xlsx")
    to: str  # "png" | "pdf" | "svg"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_api_key(api_key: str | None) -> str:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError(
            "Anthropic API key required: pass api_key=... or set ANTHROPIC_API_KEY. "
            "Anthropic API 키가 필요합니다 — api_key 인자 또는 ANTHROPIC_API_KEY 환경변수."
        )
    return key


def _read_pptx(pptx: str | Path | bytes) -> bytes:
    if isinstance(pptx, bytes):
        return pptx
    return Path(pptx).read_bytes()


def _default_output(pptx: str | Path | bytes, suffix: str) -> Path:
    if isinstance(pptx, bytes):
        return Path(f"deck{suffix}.pptx")
    p = Path(pptx)
    return p.with_name(f"{p.stem}{suffix}{p.suffix or '.pptx'}")


_SOURCE_TYPES = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "doc", ".pptx": "pptx",
    ".xlsx": "xlsx", ".xlsm": "xlsm", ".html": "html", ".htm": "html",
    ".epub": "epub", ".ipynb": "ipynb",
}


def _convert_requests(sources: list[str | Path]) -> list:
    from .tools import ConvertRequest

    reqs = []
    for src in sources:
        path = Path(src)
        source_type = _SOURCE_TYPES.get(path.suffix.lower())
        if source_type is None:
            raise ValueError(
                f"Unsupported source format: {path.name} "
                f"(supported: {', '.join(sorted(_SOURCE_TYPES))})"
            )
        reqs.append(
            ConvertRequest(
                source_type=source_type,
                content=path.read_bytes(),
                original_filename=path.name,
            )
        )
    return reqs


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


async def async_generate_pptx(
    intent: str,
    *,
    output: str | Path,
    api_key: str | None = None,
    sources: list[str | Path] | None = None,
    template: str | Path | None = None,
    deck_mode: str = "new",
    pages: tuple[int, int] = (8, 12),
    lang: str = "en-US",
    style: str = "general",
    model: str = DEFAULT_MODEL,
    images: bool = False,
    narrate: bool = False,
    on_event=None,
) -> GenerateResult:
    """Generate a deck from an intent (plus optional sources / template).

    ``template`` + ``deck_mode``: pass a PPTX path with
    ``deck_mode="template_restyle"`` (fresh deck on its masters/theme) or
    ``"template_extend"`` (append after its slides). A template with
    deck_mode="new" implies template_restyle.

    ``images=False`` (default) skips AI image acquisition — enable it and
    export provider keys (OPENAI_API_KEY / PEXELS_API_KEY...) to use it.
    """
    from .tools.generate_deck import GenerateDeckRequest, generate_deck

    template_bytes = Path(template).read_bytes() if template is not None else None
    resp = await generate_deck(
        GenerateDeckRequest(
            sources=_convert_requests(list(sources or [])),
            user_intent=intent,
            target_pages=pages,
            lang=lang,  # type: ignore[arg-type]
            style=style,  # type: ignore[arg-type]
            model=model,
            anthropic_api_key=_resolve_api_key(api_key),
            template_pptx=template_bytes,
            deck_mode=deck_mode,  # type: ignore[arg-type]
            skip_images=not images,
            narrate=narrate,
            fail_on_quality_error=False,
            image_api_keys={
                k: v
                for k in ("OPENAI_API_KEY", "PEXELS_API_KEY", "PIXABAY_API_KEY")
                if (v := os.environ.get(k))
            }
            if images
            else {},
        ),
        on_event=on_event,
    )
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.pptx)
    return GenerateResult(
        path=out,
        page_count=resp.page_count,
        design_spec=resp.design_spec,
        warnings=[{"code": w.code, "message": w.message} for w in resp.warnings],
    )


def generate_pptx(intent: str, **kwargs) -> GenerateResult:
    """Sync wrapper for :func:`async_generate_pptx`."""
    return asyncio.run(async_generate_pptx(intent, **kwargs))


# ---------------------------------------------------------------------------
# Chat edit
# ---------------------------------------------------------------------------


async def async_edit_pptx(
    pptx: str | Path | bytes,
    instruction: str,
    *,
    output: str | Path | None = None,
    api_key: str | None = None,
    sources: list[str | Path] | None = None,
    chat_history: list[dict] | None = None,
    lang: str = "en-US",
    model: str = DEFAULT_MODEL,
    on_event=None,
) -> EditResult:
    """Apply one natural-language edit turn to an existing deck.

    Question-only instructions answer in ``reply`` without touching the
    file (``changed=False``; nothing is written). ``chat_history`` entries
    are ``{"role": "user"|"assistant", "content": ...}``.
    """
    from .tools.edit_deck import ChatTurn, EditDeckRequest, edit_deck

    resp = await edit_deck(
        EditDeckRequest(
            pptx=_read_pptx(pptx),
            instruction=instruction,
            sources=_convert_requests(list(sources or [])),
            chat_history=[
                ChatTurn(role=t["role"], content=str(t.get("content", "")))
                for t in (chat_history or [])
                if isinstance(t, dict) and t.get("role") in ("user", "assistant")
            ],
            lang=lang,  # type: ignore[arg-type]
            model=model,
            anthropic_api_key=_resolve_api_key(api_key),
        ),
        on_event=on_event,
    )
    out = Path(output) if output is not None else _default_output(pptx, "_edited")
    if resp.changed:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.pptx)
    elif isinstance(pptx, (str, Path)):
        out = Path(pptx)  # unchanged: point back at the input
    return EditResult(
        path=out,
        changed=resp.changed,
        reply=resp.reply,
        page_count=resp.page_count,
        operations=resp.operations,
        warnings=[{"code": w.code, "message": w.message} for w in resp.warnings],
    )


def edit_pptx(pptx: str | Path | bytes, instruction: str, **kwargs) -> EditResult:
    """Sync wrapper for :func:`async_edit_pptx`."""
    return asyncio.run(async_edit_pptx(pptx, instruction, **kwargs))


# ---------------------------------------------------------------------------
# Deterministic verbs (no LLM, no key)
# ---------------------------------------------------------------------------


def preview_pptx(
    pptx: str | Path | bytes,
    *,
    out_dir: str | Path | None = None,
) -> list[str] | list[Path]:
    """Render every slide to a self-contained SVG.

    Returns SVG strings, or file paths (``slide_000.svg`` ...) when
    ``out_dir`` is given.
    """
    from .tools.render_preview import RenderPreviewRequest, render_preview

    resp = render_preview(RenderPreviewRequest(pptx=_read_pptx(pptx)))
    if out_dir is None:
        return [s.svg for s in resp.slides]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for s in resp.slides:
        p = out / f"slide_{s.index:03d}.svg"
        p.write_text(s.svg, encoding="utf-8")
        paths.append(p)
    return paths


def set_pptx_text(
    pptx: str | Path | bytes,
    edits: list[dict],
    *,
    output: str | Path | None = None,
) -> TextEditsResult:
    """Deterministic in-place text edits (no LLM).

    Each edit: ``{"slide": 0, "shape_id": 2, "para": 0, "new_text": "...",
    "old_text": ..., "row": ..., "col": ...}`` — shape ids / paragraph
    indices / table cells come from the preview SVG's ``data-e2p-*`` tags
    (see :func:`analyze_pptx` for a text outline with addresses).
    """
    from .tools.apply_text_edits import (
        ApplyTextEditsRequest,
        TextEdit,
        apply_text_edits,
    )

    resp = apply_text_edits(
        ApplyTextEditsRequest(
            pptx=_read_pptx(pptx),
            edits=[TextEdit(**e) for e in edits],
        )
    )
    out = Path(output) if output is not None else _default_output(pptx, "_edited")
    if resp.applied > 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.pptx)
    elif isinstance(pptx, (str, Path)):
        out = Path(pptx)
    return TextEditsResult(
        path=out,
        applied=resp.applied,
        results=[r.model_dump() for r in resp.results],
    )


def analyze_pptx(pptx: str | Path | bytes) -> dict:
    """Inspect a deck: canvas, theme, and a per-slide text outline whose
    entries carry the addresses :func:`set_pptx_text` needs.

    Returns::

        {"page_count", "width_px", "height_px",
         "theme": {"colors", "fonts"},
         "slides": [{"index", "texts": [{"shape_id"|"table_id"+"row"/"col",
                                          "para", "text"}, ...]}, ...]}
    """
    import re
    from xml.etree import ElementTree as ET

    from .core.template_import.manifest import build_manifest
    from .tools._workspace import temp_workspace
    from .tools.render_preview import RenderPreviewRequest, render_preview

    content = _read_pptx(pptx)
    preview = render_preview(RenderPreviewRequest(pptx=content))

    with temp_workspace(prefix="xgen_edit2docs-analyze-") as ws:
        p = ws / "deck.pptx"
        p.write_bytes(content)
        out = ws / "analysis"
        out.mkdir()
        manifest = build_manifest(p, out)

    slides = []
    for s in preview.slides:
        texts: list[dict] = []
        try:
            root = ET.fromstring(s.svg)
        except ET.ParseError:
            slides.append({"index": s.index, "texts": texts})
            continue
        ns_strip = re.compile(r"^\{[^}]*\}")

        def _walk(el, shape_id=None, table_id=None, cell=None):
            tag = ns_strip.sub("", el.tag)
            if tag == "g":
                if el.get("data-e2p-shape"):
                    shape_id = int(el.get("data-e2p-shape"))
                    table_id, cell = None, None
                elif el.get("data-e2p-table"):
                    table_id = int(el.get("data-e2p-table"))
                    shape_id = None
                elif el.get("data-e2p-cell") and table_id is not None:
                    r, c = el.get("data-e2p-cell").split(",")
                    cell = (int(r), int(c))
            elif tag == "text" and el.get("data-e2p-para") is not None:
                entry: dict = {
                    "para": int(el.get("data-e2p-para")),
                    "text": el.get("data-e2p-text", ""),
                }
                if shape_id is not None:
                    entry["shape_id"] = shape_id
                    texts.append(entry)
                elif table_id is not None and cell is not None:
                    entry["table_id"] = table_id
                    entry["row"], entry["col"] = cell
                    texts.append(entry)
            for child in el:
                _walk(child, shape_id, table_id, cell)

        _walk(root)
        slides.append({"index": s.index, "texts": texts})

    theme = manifest.get("theme") or {}
    return {
        "page_count": preview.page_count,
        "width_px": preview.width_px,
        "height_px": preview.height_px,
        "theme": {
            "colors": dict(theme.get("colors") or {}),
            "fonts": dict(theme.get("fonts") or {}),
        },
        "slides": slides,
    }


# ---------------------------------------------------------------------------
# Unified document verbs — dispatch by file extension (.pptx/.docx/.xlsx)
# ---------------------------------------------------------------------------

_DOC_FORMATS = ("pptx", "docx", "xlsx")


def _fmt_of(path: str | Path) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix not in _DOC_FORMATS:
        raise ValueError(
            f"Unsupported document format: {Path(path).name} "
            f"(supported: {', '.join('.' + f for f in _DOC_FORMATS)})"
        )
    return suffix


def render_doc(
    doc: str | Path,
    *,
    to: str = "png",
    out_dir: str | Path | None = None,
    dpi: float = 144.0,
) -> RenderResult:
    """Render a document to page images or a PDF — no LibreOffice.

    Native raster pipeline (docs/native-render-plan.md): per-page SVG →
    resvg PNG → (for ``to="pdf"``) PyMuPDF assembly. Deterministic, no
    LLM, no subprocess.

    Args:
        doc: Source document — ``.pptx`` / ``.docx`` / ``.xlsx`` all
            render end-to-end (per-format native page engines; no
            LibreOffice, no subprocess).
        to: ``"png"`` (page-1.png … page-N.png, pdftoppm-compatible
            naming), ``"pdf"`` (single ``<stem>.pdf``), ``"svg"``
            (page-1.svg … — the raw vector pages), or ``"md"`` (readable
            content: ``preview.md`` for docx/xlsx, per-slide SVGs for pptx
            — the :func:`preview_doc` surface folded into one verb).
        out_dir: Output directory (default ``<doc dir>/render``).
        dpi: Raster resolution for png/pdf (SVG px are 96/inch).

    Returns:
        :class:`RenderResult` with the written file paths.
    """
    fmt = _fmt_of(doc)
    to = (to or "png").strip().lower()
    if to not in ("png", "pdf", "svg", "md"):
        raise ValueError(f"Unsupported render target: {to!r} (use png / pdf / svg / md)")
    src = Path(doc)
    out = Path(out_dir) if out_dir is not None else src.parent / "render"
    out.mkdir(parents=True, exist_ok=True)

    if to == "md":
        rendered = preview_doc(src, out_dir=out)
        paths = list(rendered) if isinstance(rendered, list) else [Path(rendered)]
        return RenderResult(paths=paths, page_count=len(paths), format=fmt, to=to)

    if fmt == "docx":
        from .documents.docx_pages import docx_to_page_svgs

        svgs = docx_to_page_svgs(src.read_bytes())
    elif fmt == "xlsx":
        from .documents.xlsx_pages import xlsx_to_page_svgs

        svgs = xlsx_to_page_svgs(src.read_bytes())
    else:
        svgs = preview_pptx(src)  # list[str] — per-slide self-contained SVG

    from .render import svgs_to_pdf, svgs_to_pngs

    if to == "svg":
        for old in out.glob("page-*.svg"):
            old.unlink(missing_ok=True)
        paths: list[Path] = []
        for i, svg in enumerate(svgs, 1):
            p = out / f"page-{i}.svg"
            p.write_text(svg, encoding="utf-8")
            paths.append(p)
        return RenderResult(paths=paths, page_count=len(paths), format=fmt, to=to)

    if to == "png":
        paths = svgs_to_pngs(svgs, out, dpi=dpi)
        return RenderResult(paths=paths, page_count=len(paths), format=fmt, to=to)

    pdf_path = out / f"{src.stem}.pdf"
    pdf_path.write_bytes(svgs_to_pdf(svgs, dpi=dpi))
    return RenderResult(paths=[pdf_path], page_count=len(svgs), format=fmt, to=to)


async def _sources_markdown(sources: list[str | Path] | None) -> list[str]:
    from .tools.convert import convert_to_markdown

    reqs = _convert_requests(list(sources or []))
    if not reqs:
        return []
    results = await asyncio.gather(
        *(asyncio.to_thread(convert_to_markdown, r) for r in reqs)
    )
    return [r.markdown for r in results]


async def async_generate_doc(
    intent: str,
    *,
    output: str | Path,
    api_key: str | None = None,
    sources: list[str | Path] | None = None,
    template: str | Path | None = None,
    deck_mode: str = "new",
    pages: tuple[int, int] = (8, 12),
    lang: str = "en-US",
    model: str = DEFAULT_MODEL,
) -> GenerateResult:
    """Generate a document; the OUTPUT EXTENSION picks the engine.

    .pptx -> full deck pipeline (template/deck_mode/pages apply)
    .docx -> document writer (markdown -> Word)
    .xlsx -> sheet designer (spec -> Excel)
    """
    fmt = _fmt_of(output)
    if fmt == "pptx":
        return await async_generate_pptx(
            intent, output=output, api_key=api_key, sources=sources,
            template=template, deck_mode=deck_mode, pages=pages,
            lang=lang, model=model,
        )
    from .tools.generate_doc import GenerateDocRequest, generate_document

    resp = await generate_document(
        GenerateDocRequest(
            intent=intent,
            fmt=fmt,  # type: ignore[arg-type]
            sources_markdown=await _sources_markdown(sources),
            lang=lang,  # type: ignore[arg-type]
            model=model,
            anthropic_api_key=_resolve_api_key(api_key),
        )
    )
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return GenerateResult(
        path=out,
        page_count=0,
        design_spec=resp.artifact,
        warnings=[{"code": w.code, "message": w.message} for w in resp.warnings],
    )


def generate_doc(intent: str, **kwargs) -> GenerateResult:
    """Sync wrapper for :func:`async_generate_doc`."""
    return asyncio.run(async_generate_doc(intent, **kwargs))


async def async_edit_doc(
    doc: str | Path,
    instruction: str,
    *,
    output: str | Path | None = None,
    api_key: str | None = None,
    sources: list[str | Path] | None = None,
    chat_history: list[dict] | None = None,
    lang: str = "en-US",
    model: str = DEFAULT_MODEL,
) -> EditResult:
    """One natural-language edit turn; the INPUT EXTENSION picks the engine."""
    fmt = _fmt_of(doc)
    if fmt == "pptx":
        return await async_edit_pptx(
            doc, instruction, output=output, api_key=api_key, sources=sources,
            chat_history=chat_history, lang=lang, model=model,
        )
    from .tools.edit_deck import ChatTurn
    from .tools.edit_doc import EditDocRequest, edit_document

    resp = await edit_document(
        EditDocRequest(
            content=_read_pptx(doc),
            fmt=fmt,  # type: ignore[arg-type]
            instruction=instruction,
            sources_markdown=await _sources_markdown(sources),
            chat_history=[
                ChatTurn(role=t["role"], content=str(t.get("content", "")))
                for t in (chat_history or [])
                if isinstance(t, dict) and t.get("role") in ("user", "assistant")
            ],
            lang=lang,  # type: ignore[arg-type]
            model=model,
            anthropic_api_key=_resolve_api_key(api_key),
        )
    )
    out = Path(output) if output is not None else _default_output(doc, "_edited")
    if resp.changed:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)
    else:
        out = Path(doc)
    return EditResult(
        path=out,
        changed=resp.changed,
        reply=resp.reply,
        page_count=0,
        operations=resp.operations,
        warnings=[{"code": w.code, "message": w.message} for w in resp.warnings],
    )


def edit_doc(doc: str | Path, instruction: str, **kwargs) -> EditResult:
    """Sync wrapper for :func:`async_edit_doc`."""
    return asyncio.run(async_edit_doc(doc, instruction, **kwargs))


def preview_doc(
    doc: str | Path,
    *,
    out_dir: str | Path | None = None,
) -> list[str] | list[Path] | str | Path:
    """Human-viewable preview.

    .pptx -> list of self-contained slide SVGs (strings, or files with
    ``out_dir``) · .docx / .xlsx -> a markdown rendering (string, or a
    ``preview.md`` file with ``out_dir``).
    """
    fmt = _fmt_of(doc)
    if fmt == "pptx":
        return preview_pptx(doc, out_dir=out_dir)
    if fmt == "docx":
        from .documents.docx_engine import docx_to_markdown

        markdown = docx_to_markdown(_read_pptx(doc))
    else:
        from .documents.xlsx_engine import xlsx_to_markdown

        markdown = xlsx_to_markdown(_read_pptx(doc))
    if out_dir is None:
        return markdown
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def analyze_doc(doc: str | Path) -> dict:
    """Structure outline with the addresses :func:`set_doc_text` needs.

    .pptx -> slides/shape ids (see :func:`analyze_pptx`) · .docx ->
    paragraph/table-cell outline · .xlsx -> sheets + sample rows. Every
    format also carries a ``"charts"`` list (the addresses
    :func:`edit_chart` takes) so a planner sees the charts it must edit
    or avoid clobbering.
    """
    from .documents.chart_edit import list_charts

    fmt = _fmt_of(doc)
    content = _read_pptx(doc)
    if fmt == "pptx":
        info = {"format": "pptx", **analyze_pptx(doc)}
        info.setdefault("charts", list_charts(content, "pptx"))
        return info
    if fmt == "docx":
        from .documents.docx_engine import docx_outline

        return {
            "format": "docx",
            "outline": docx_outline(content),
            "charts": list_charts(content, "docx"),
        }
    from .documents.xlsx_engine import xlsx_outline

    return {"format": "xlsx", **xlsx_outline(content)}


def list_charts(doc: str | Path) -> list[dict]:
    """Read-only chart summaries — kinds, titles, series data — for any
    of the three formats. Each entry's ``chart`` index is the address
    :func:`edit_chart` takes."""
    from .documents.chart_edit import list_charts as _list

    return _list(_read_pptx(doc), _fmt_of(doc))


def edit_chart(
    doc: str | Path,
    edits: list[dict],
    *,
    output: str | Path | None = None,
) -> TextEditsResult:
    """Deterministically edit native charts (no LLM), any format.

    Each edit addresses a chart by ``{"chart": i}`` (from
    :func:`list_charts` / :func:`analyze_doc`):

    * ``{"chart": 0, "title": "Q3 Sales"}`` — retitle.
    * ``{"chart": 0, "categories": ["Q1", "Q2"],
      "series": [{"name": "Sales", "values": [120, 135]}]}`` — set data
      (rewrites the chart caches AND its embedded workbook, so Office's
      double-click-edit shows the same numbers).

    Untouched package parts stay byte-identical.
    """
    from .documents.chart_edit import _coerce_edit, apply_chart_edits

    fmt = _fmt_of(doc)
    content = _read_pptx(doc)
    typed = [_coerce_edit(e) for e in edits]
    new_content, results = apply_chart_edits(content, fmt, typed)
    dumped = [
        {"action": r.action, "chart": r.chart, "status": r.status, "message": r.message}
        for r in results
    ]
    applied = sum(1 for r in results if r.status == "applied")
    out = Path(output) if output is not None else _default_output(doc, "_chart")
    if applied > 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new_content)
    else:
        out = Path(doc)
    return TextEditsResult(path=out, applied=applied, results=dumped)


def set_doc_text(
    doc: str | Path,
    edits: list[dict],
    *,
    output: str | Path | None = None,
) -> TextEditsResult:
    """Deterministic targeted edits (no LLM), format-dispatched.

    .pptx -> ``{slide, shape_id, para, new_text, ...}`` ·
    .docx -> ``{action: replace|insert_after|delete, para|table/row/col, ...}``
    (action defaults to ``replace``) · .xlsx -> ``{action: set_cell|
    append_rows|add_sheet, sheet, cell, value, ...}`` (defaults to
    ``set_cell``).
    """
    fmt = _fmt_of(doc)
    if fmt == "pptx":
        return set_pptx_text(doc, edits, output=output)

    content = _read_pptx(doc)
    if fmt == "docx":
        from .documents.docx_engine import DocxEdit, apply_docx_edits

        typed = [DocxEdit(**{"action": "replace", **e}) for e in edits]
        new_content, results = apply_docx_edits(content, typed)
        dumped = [
            {"action": r.action, "status": r.status, "message": r.message}
            for r in results
        ]
    else:
        from .documents.xlsx_engine import XlsxEdit, apply_xlsx_edits

        typed = [XlsxEdit(**{"action": "set_cell", **e}) for e in edits]
        new_content, results = apply_xlsx_edits(content, typed)
        dumped = [
            {"action": r.action, "status": r.status, "message": r.message}
            for r in results
        ]

    applied = sum(1 for r in results if r.status == "applied")
    out = Path(output) if output is not None else _default_output(doc, "_edited")
    if applied > 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new_content)
    else:
        out = Path(doc)
    return TextEditsResult(path=out, applied=applied, results=dumped)


def list_doc_parts(doc: str | Path) -> list[dict]:
    """Every XML/binary part in the OOXML package — the addresses
    :func:`get_doc_xml` / :func:`set_doc_xml` take. Deterministic, no LLM.

    DOCX/XLSX/PPTX are zips of XML parts; this is the map of the whole
    document (``ppt/slides/slide1.xml``, ``ppt/charts/chart1.xml``,
    ``word/document.xml``, ``xl/worksheets/sheet1.xml``, ...).
    """
    from .documents.xml_edit import list_parts

    _fmt_of(doc)  # gate to the supported formats
    return list_parts(_read_pptx(doc))


def get_doc_xml(doc: str | Path, part: str) -> str:
    """One part's XML text, exactly as stored. Deterministic, no LLM.

    Read this, copy exact substrings, and hand them to :func:`set_doc_xml`
    as find/replace edits — that covers every edit OOXML can express
    (colors, fills, fonts, geometry, chart styling, ...).
    """
    from .documents.xml_edit import get_xml

    _fmt_of(doc)
    return get_xml(_read_pptx(doc), part)


def set_doc_xml(
    doc: str | Path,
    part: str,
    edits: list[dict] | None = None,
    *,
    xml: str | None = None,
    content_type: str | None = None,
    delete: bool = False,
    output: str | Path | None = None,
) -> TextEditsResult:
    """Patch, create or delete one XML part. Deterministic, no LLM — the
    universal escape hatch for edits the structured verbs don't cover.

    * ``edits`` — ``{"find", "replace", "count" (0=all)}`` against the text
      from :func:`get_doc_xml`; substrings must match exactly.
    * ``xml`` — whole-part replacement; a missing part is CREATED (pass
      ``content_type`` to register its ``[Content_Types].xml`` Override —
      needed for new slides/charts). This is how you add a slide: create
      ``ppt/slides/slideN.xml`` + its ``.rels``, then patch
      ``presentation.xml`` / its rels.
    * ``delete=True`` — remove the part (also patch referencing rels).

    The result must stay well-formed XML or nothing is written. Untouched
    parts stay byte-identical.
    """
    from .documents.xml_edit import apply_xml_edits, coerce_edit

    _fmt_of(doc)
    content = _read_pptx(doc)
    typed = [coerce_edit(e) for e in edits] if edits is not None else None
    new_content, results = apply_xml_edits(
        content, part, typed, xml=xml, content_type=content_type, delete=delete
    )
    dumped = [
        {
            "find": r.find[:120],
            "status": r.status,
            "occurrences": r.occurrences,
            "message": r.message,
        }
        for r in results
    ]
    applied = sum(1 for r in results if r.status == "applied")
    out = Path(output) if output is not None else _default_output(doc, "_edited")
    if applied > 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new_content)
    else:
        out = Path(doc)
    return TextEditsResult(path=out, applied=applied, results=dumped)


def arrange_doc(
    doc: str | Path,
    ops: list[dict],
    *,
    output: str | Path | None = None,
) -> ArrangeResult:
    """Deterministic STRUCTURAL edits — whole slides/sheets as objects.
    No LLM, no key; byte-preserving (untouched parts stay byte-identical).

    ``ops`` apply in sequence, each resolving ``target`` against the
    CURRENT state (after prior ops):

    * ``{"op":"duplicate","target":i,"to":k}`` — pptx: copy slide ``i``
      (charts/notes cloned, images shared) at position ``k`` (default:
      after ``i``). xlsx: needs ``"name"`` for the new sheet.
    * ``{"op":"move","target":i,"to":k}`` — reorder a slide/sheet.
    * ``{"op":"delete","target":i}`` — remove a slide/sheet (orphan-swept).
    * ``{"op":"rename","target":s,"name":"New"}`` — xlsx sheet only.

    ``target`` is a slide index (pptx) or a sheet name/index (xlsx). The
    batch is best-effort: each op gets a ``status`` (applied / invalid /
    not_found / refused); one bad op never aborts the rest."""
    from .documents.arrange import apply_arrange

    fmt = _fmt_of(doc)
    content = _read_pptx(doc)
    new_content, results, warnings = apply_arrange(content, fmt, list(ops))
    applied = sum(1 for r in results if r["status"] == "applied")
    out = Path(output) if output is not None else _default_output(doc, "_arranged")
    if applied > 0:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(new_content)
    else:
        out = Path(doc)
    return ArrangeResult(path=out, applied=applied, results=results, warnings=warnings)


def build_doc(
    spec: str | dict[str, Any],
    output: str | Path,
    *,
    lang: str | None = None,
) -> GenerateResult:
    """Deterministically build a NEW document from a structured spec — no LLM,
    no API key. The **output file extension picks the engine** (mirrors
    :func:`generate_doc`, but the caller supplies the interchange artifact the
    writer LLM would otherwise produce):

    * ``.docx`` ← *spec* is a **markdown string** (the ``docx_from_markdown``
      subset: headings, paragraphs, lists, tables, bold/italic).
    * ``.xlsx`` ← *spec* is a **dict** ``{"sheets": [{"name", "headers",
      "rows"}, ...]}`` (see :func:`~xgen_edit2docs.documents.xlsx_engine.xlsx_from_spec`).
    * ``.pptx`` ← *spec* is a **dict** ``{"slides": [{"layout", "title",
      "subtitle"/"bullets", "notes"}, ...]}`` (see
      :func:`~xgen_edit2docs.documents.pptx_build.pptx_from_spec`).

    This is the deterministic generation primitive: an agent (or the
    ``generate_doc`` pipeline) does the thinking to produce *spec*, and this
    renders the file with zero model calls. Raises ``ValueError`` on a spec
    that doesn't match the target format.
    """
    fmt = _fmt_of(output)
    out = Path(output)

    page_count = 0
    if fmt == "docx":
        if not isinstance(spec, str):
            raise ValueError(
                "docx build spec must be a markdown string. "
                "docx 빌드 스펙은 markdown 문자열이어야 합니다."
            )
        from .documents.docx_engine import docx_from_markdown

        data = docx_from_markdown(spec, lang=lang)
    elif fmt == "xlsx":
        if not isinstance(spec, dict):
            raise ValueError(
                "xlsx build spec must be a {'sheets': [...]} dict. "
                "xlsx 빌드 스펙은 {'sheets': [...]} 딕셔너리여야 합니다."
            )
        from .documents.xlsx_engine import xlsx_from_spec

        data = xlsx_from_spec(spec)
        page_count = len(spec.get("sheets") or [])
    elif fmt == "pptx":
        if not isinstance(spec, dict):
            raise ValueError(
                "pptx build spec must be a {'slides': [...]} dict. "
                "pptx 빌드 스펙은 {'slides': [...]} 딕셔너리여야 합니다."
            )
        from .documents.pptx_build import pptx_from_spec

        data = pptx_from_spec(spec, lang=lang)
        page_count = len(spec.get("slides") or [])
    else:  # pragma: no cover — _fmt_of already gates the format set
        raise ValueError(f"unsupported build format: {fmt}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return GenerateResult(path=out, page_count=page_count, design_spec="", warnings=[])
