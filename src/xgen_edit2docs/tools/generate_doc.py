"""Generate a DOCX or XLSX from an intent (single LLM call + deterministic render).

The PPTX path keeps its own multi-stage pipeline (tools/generate_deck.py);
Word and Excel are simpler: one writer/designer LLM call produces an
interchange artifact (markdown / sheet spec) and a deterministic engine
renders it. Render failures (invalid spec) feed back into one retry.
"""

from __future__ import annotations

import time
from typing import Literal

import yaml
from pydantic import Field

from ..config import resolve_model
from ..documents.docx_engine import docx_from_markdown
from ..documents.xlsx_engine import xlsx_from_spec
from ..llm import DEFAULT_MODEL, AnthropicClient, build_output_lang_directive, load_prompt
from .edit_deck import _cost_from_usage, _extract_block
from .generate_deck import _merge_cost
from .types import (
    DEFAULT_LANG,
    CostBreakdown,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)

DocFormat = Literal["docx", "xlsx"]


class GenerateDocRequest(ToolRequest):
    intent: str = Field(..., min_length=1)
    fmt: DocFormat
    sources_markdown: list[str] = Field(default_factory=list)
    lang: LangCode = DEFAULT_LANG
    model: str = DEFAULT_MODEL
    anthropic_api_key: str = Field(..., description="BYOK; never persisted.")


class GenerateDocResponse(ToolResponse):
    content: bytes
    fmt: DocFormat
    # The LLM's interchange artifact — markdown (docx) or YAML (xlsx).
    artifact: str
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


_ROLES: dict[str, tuple[str, str]] = {
    # fmt -> (prompt role, fenced block label)
    "docx": ("document-writer", "document"),
    "xlsx": ("sheet-designer", "sheet_spec"),
}


async def generate_document(req: GenerateDocRequest) -> GenerateDocResponse:
    """One writer call -> deterministic render, with one repair retry."""
    started = time.perf_counter()
    warnings: list[WarningEntry] = []
    cost = CostBreakdown()

    role, label = _ROLES[req.fmt]
    client = AnthropicClient(api_key=req.anthropic_api_key, model=req.model)
    # Writer-tier model (env EDIT2DOCS_MODEL_WRITER), falling back to the
    # request's BYOK model when no override is set.
    writer_model = resolve_model("writer", req.model)
    system = build_output_lang_directive(req.lang) + "\n\n" + load_prompt(role)
    user = _build_user_message(req)

    artifact, render_error = "", ""
    content: bytes | None = None
    for attempt in range(2):
        message = user if attempt == 0 else (
            user
            + "\n\n# REMINDER\nYour previous answer could not be rendered "
            + (f"({render_error}). " if render_error else "")
            + f"Respond again with exactly one fenced ```{label} block "
            "following the output format."
        )
        result = await client.complete(
            system_prompt=system,
            user_message=message,
            max_output_tokens=16384,
            cache_system=True,
            model=writer_model,
        )
        cost = _merge_cost(cost, _cost_from_usage(result.usage))
        block = _extract_block(result.text, label)
        if block is None and req.fmt == "docx" and "#" in result.text:
            # Writer sometimes skips the fence but emits valid markdown.
            block = result.text.strip()
        if block is None:
            render_error = f"missing ```{label} block"
            continue
        artifact = block
        try:
            content = _render(req.fmt, block, lang=req.lang)
            break
        except ValueError as exc:
            render_error = str(exc)
            continue

    if content is None:
        raise ValueError(
            f"Document generation failed after retry: {render_error}. "
            "문서 생성에 실패했습니다 — 요청을 조금 더 구체적으로 다시 시도해 주세요."
        )
    if render_error:
        warnings.append(
            WarningEntry(
                code="generate_doc_retried",
                message=f"First writer output was unusable ({render_error}); retry succeeded.",
            )
        )

    final = CostBreakdown(
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_write_tokens=cost.cache_write_tokens,
        duration_seconds=time.perf_counter() - started,
    )
    return GenerateDocResponse(
        content=content, fmt=req.fmt, artifact=artifact, cost=final, warnings=warnings
    )


def _render(fmt: str, artifact: str, *, lang: str | None = None) -> bytes:
    if fmt == "docx":
        if not artifact.strip():
            raise ValueError("empty document body")
        return docx_from_markdown(artifact, lang=lang)
    try:
        spec = yaml.safe_load(artifact)
    except yaml.YAMLError as exc:
        # yaml errors are NOT ValueError subclasses — normalize so the
        # writer-retry loop catches them (unquoted Korean colons are a
        # common LLM failure mode).
        raise ValueError(f"invalid sheet_spec YAML: {exc}") from exc
    if not isinstance(spec, dict):
        raise ValueError("sheet_spec must be a YAML mapping")
    return xlsx_from_spec(spec)


def _build_user_message(req: GenerateDocRequest) -> str:
    lines = ["# Request", req.intent.strip(), ""]
    if req.sources_markdown:
        lines.append("# Source documents")
        for i, md in enumerate(req.sources_markdown, start=1):
            body = md.strip()
            if len(body) > 12000:
                body = body[:12000] + "\n…(truncated)"
            lines.append(f"## Source {i}")
            lines.append("```markdown")
            lines.append(body)
            lines.append("```")
        lines.append("")
    return "\n".join(lines)
