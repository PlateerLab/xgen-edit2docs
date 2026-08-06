"""Strategize tool: source markdown + intent -> design spec + spec_lock + page plan.

Wraps the Strategist role from `core/prompts/strategist.{lang}.md`. The Strategist
is the first LLM stage in the pipeline ([02-pipeline.md] §2.1 step 4) — it
decides colors / fonts / page count / per-page content outline.

The output design_spec/spec_lock are markdown + YAML strings that downstream
tools (executor, export) consume directly. We don't parse them in this tool —
the Strategist's contract is "structured markdown" and the next stage trusts it.
"""

from __future__ import annotations

import time
from typing import Protocol

from pydantic import Field

from ..config import get_settings, resolve_model
from ..llm import AnthropicClient, DEFAULT_MODEL, build_output_lang_directive, load_prompt
from ..llm.anthropic_client import LLMResult, LLMUsage
from .types import (
    CanvasFormat,
    CostBreakdown,
    DEFAULT_CANVAS,
    DEFAULT_LANG,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)


class StrategizeRequest(ToolRequest):
    # 0 or more source documents (markdown). When empty, the Strategist works
    # from `user_intent` alone — useful for "just generate a deck about X"
    # chat-style flows.
    sources_markdown: list[str] = Field(default_factory=list)
    user_intent: str = Field(..., min_length=1)
    template_name: str | None = None
    # Deterministic digest of a user-provided template PPTX (theme colors,
    # fonts, canvas, tone samples) produced by tools.analyze_template. When
    # set, the Strategist is instructed to adopt that visual identity.
    template_context: str | None = None
    target_pages: tuple[int, int] = Field(default=(8, 12))
    canvas_format: CanvasFormat = DEFAULT_CANVAS
    style: str = Field(
        default="general",
        description="general | consultant | consultant-top — selects executor variant downstream.",
    )
    lang: LangCode = DEFAULT_LANG
    model: str = DEFAULT_MODEL
    anthropic_api_key: str = Field(
        ...,
        description="BYOK Anthropic key. Never persisted; only used for this call.",
    )


class StrategizeResponse(ToolResponse):
    raw_output: str = Field(..., description="Full LLM text output (markdown).")
    design_spec: str = Field(..., description="Human-readable design specification.")
    spec_lock: str = Field(..., description="Machine-readable execution contract (YAML).")
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


class LLMCallable(Protocol):
    """Minimal interface the tool needs from the LLM client (for test stubs).

    Mirrors :meth:`AnthropicClient.complete`. ``system_suffix`` /
    ``user_suffix`` / ``stream`` are the token-cost levers the Executor
    uses (spec_lock cached as a shared system suffix); they are optional
    so a stub only needs to accept ``**kwargs``.
    """

    async def complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_output_tokens: int = ...,
        temperature: float = ...,
        cache_system: bool = ...,
        model: str | None = ...,
        system_suffix: str | None = ...,
        user_suffix: str = ...,
        stream: bool | None = ...,
    ) -> LLMResult: ...


async def strategize(
    req: StrategizeRequest,
    *,
    client: LLMCallable | None = None,
) -> StrategizeResponse:
    """Run the Strategist on the given sources.

    `client` is injected for tests; production code lets the tool construct
    a real `AnthropicClient` from `req.anthropic_api_key`.
    """
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    # English single-source prompt + runtime language directive. The directive
    # tells the model to emit user-facing strings in req.lang while keeping
    # YAML / JSON keys English (Track A).
    system_prompt = build_output_lang_directive(req.lang) + "\n\n" + load_prompt("strategist")
    user_message = _build_user_message(req, warnings)

    llm = client or AnthropicClient(api_key=req.anthropic_api_key, model=req.model)
    result = await llm.complete(
        system_prompt=system_prompt,
        user_message=user_message,
        # 16384 (was 8192): the Strategist emits design_spec + spec_lock +
        # a full page outline; 8192 truncated large decks, triggering a
        # recovery+retry cascade that cost far more than the extra budget.
        max_output_tokens=16384,
        cache_system=True,
        model=resolve_model("strategist", req.model),
    )

    design_spec, spec_lock = _split_output(result.text, warnings)

    # Deterministic post-Strategist validation: normalise hex colors,
    # fuzzy-resolve icon names, surface missing fields. The validator
    # rewrites spec_lock when it can fix something deterministically;
    # everything else lands in `warnings` for the operator.
    from pathlib import Path as _Path
    from ._spec_validator import validate_spec_lock as _validate_spec_lock
    _icons_dir = (
        _Path(__file__).resolve().parent.parent
        / "core" / "templates" / "icons"
    )
    _validation = _validate_spec_lock(spec_lock, icons_dir=_icons_dir)
    spec_lock = _validation.spec_lock
    for _v in _validation.warnings:
        warnings.append(
            WarningEntry(code=_v.code, message=_v.message, detail=_v.detail)
        )

    return StrategizeResponse(
        raw_output=result.text,
        design_spec=design_spec,
        spec_lock=spec_lock,
        cost=_cost_from_usage(result.usage, time.perf_counter() - started),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_user_message(
    req: StrategizeRequest,
    warnings: list[WarningEntry] | None = None,
) -> str:
    # Cap each source's markdown before it's embedded. An untruncated PDF
    # can dwarf the whole deck's token spend; the cap mirrors the 6k/12k
    # source caps in edit_doc/generate_doc. 0 disables the cap.
    source_cap = get_settings().strategist_source_char_cap
    lines: list[str] = []
    lines.append("# Inputs")
    lines.append(f"- Language: {req.lang}")
    lines.append(f"- Canvas format: {req.canvas_format}")
    lines.append(f"- Style: {req.style}")
    lines.append(f"- Target pages: {req.target_pages[0]}-{req.target_pages[1]}")
    if req.template_name:
        lines.append(f"- Template: {req.template_name}")
    lines.append("")
    lines.append("# User intent")
    lines.append(req.user_intent.strip())
    lines.append("")
    if req.template_context:
        lines.append("# Template analysis (from the user's uploaded PPTX)")
        lines.append(req.template_context.strip())
        lines.append("")
    if req.sources_markdown:
        lines.append("# Sources")
        for i, src in enumerate(req.sources_markdown, start=1):
            body = src.strip()
            if source_cap and len(body) > source_cap:
                removed = len(body) - source_cap
                body = body[:source_cap] + f"\n\n…(truncated {removed} chars)"
                if warnings is not None:
                    warnings.append(
                        WarningEntry(
                            code="strategist_source_truncated",
                            message=(
                                f"Source {i} markdown was capped to "
                                f"{source_cap} chars ({removed} dropped) before "
                                "strategizing to bound token spend."
                            ),
                            detail={
                                "source_index": i,
                                "cap": source_cap,
                                "removed_chars": removed,
                            },
                        )
                    )
            lines.append(f"## Source {i}")
            lines.append("```markdown")
            lines.append(body)
            lines.append("```")
        lines.append("")
    else:
        # "Topic-only" / chat mode: no source documents — design entirely
        # from the user_intent above. Mirrors ppt-master's standalone
        # topic-research workflow but inline.
        lines.append("# Source material")
        lines.append(
            "No source document was provided. Design this deck from the "
            "User intent above. Use your general knowledge to expand the "
            "topic into a coherent slide-by-slide outline. Do not refuse "
            "or stall waiting for sources — produce a usable design_spec "
            "and spec_lock based on the intent alone."
        )
        lines.append("")
    lines.append("# Output format")
    lines.append(
        "Produce two clearly-fenced sections in this exact order:\n"
        "1. A fenced block labelled `design_spec` containing the human-readable design spec (markdown).\n"
        "2. A fenced block labelled `spec_lock` containing the YAML execution contract."
    )
    return "\n".join(lines)


_DESIGN_SPEC_LABELS = ("design_spec", "design-spec")
_SPEC_LOCK_LABELS = ("spec_lock", "spec-lock", "yaml")


def _split_output(text: str, warnings: list[WarningEntry]) -> tuple[str, str]:
    """Pull the two fenced blocks out of the LLM response.

    The Strategist prompt asks for ```design_spec ... ``` and ```spec_lock ... ```
    fenced sections. We tolerate small label variations and fall back to the
    raw text if a block is missing (with a warning).

    Robustness: the design_spec body legitimately contains its own fenced
    code blocks (SVG samples, palette swatches, YAML examples). A naive
    "first ``` after the opener" closer would truncate the block mid-way
    and lose every later section — including §IX Content Outline, which
    the page-plan parser needs. We instead pair openers with the **last**
    fence before the next labeled opener (or EOF).
    """
    pair = _extract_paired_blocks(text)
    if pair is not None:
        design_spec, spec_lock = pair
        return design_spec.strip(), spec_lock.strip()

    # Single-block degradation paths — try each label independently.
    design_spec = _extract_block_single(text, _DESIGN_SPEC_LABELS)
    spec_lock = _extract_block_single(text, _SPEC_LOCK_LABELS)

    if design_spec is None:
        warnings.append(
            WarningEntry(
                code="missing_design_spec_block",
                message="Strategist output did not contain a `design_spec` fenced block; returning full text.",
            )
        )
        design_spec = text
    if spec_lock is None:
        warnings.append(
            WarningEntry(
                code="missing_spec_lock_block",
                message="Strategist output did not contain a `spec_lock` fenced block; downstream tools may fail.",
            )
        )
        spec_lock = ""

    return design_spec.strip(), spec_lock.strip()


def _find_labeled_opener(text: str, labels: tuple[str, ...], start: int = 0) -> int | None:
    """Return the index of the first ```<label> opener in *text*, or None."""
    fence = "```"
    pos = start
    while True:
        idx = text.find(fence, pos)
        if idx == -1:
            return None
        header_end = text.find("\n", idx + len(fence))
        if header_end == -1:
            return None
        label = text[idx + len(fence) : header_end].strip().lower()
        if any(label == lbl or label.startswith(lbl) for lbl in labels):
            return idx
        pos = header_end + 1


def _extract_paired_blocks(text: str) -> tuple[str, str] | None:
    """Locate the design_spec + spec_lock openers in order and return both
    bodies. Closing fences are the **last** ``` before each next opener
    (or EOF for the trailing block), so nested code blocks inside the
    design_spec don't truncate it.
    """
    design_open = _find_labeled_opener(text, _DESIGN_SPEC_LABELS, 0)
    if design_open is None:
        return None
    design_header_end = text.find("\n", design_open + 3)
    if design_header_end == -1:
        return None

    spec_open = _find_labeled_opener(text, _SPEC_LOCK_LABELS, design_header_end + 1)
    if spec_open is None:
        return None
    spec_header_end = text.find("\n", spec_open + 3)
    if spec_header_end == -1:
        return None

    # design_spec body ends at the LAST ``` before the spec_lock opener.
    design_close = text.rfind("```", design_header_end + 1, spec_open)
    design_body = text[design_header_end + 1 : design_close] if design_close != -1 else text[design_header_end + 1 : spec_open]

    # spec_lock body ends at the LAST ``` before EOF, or runs to EOF if
    # the model forgot the closing fence.
    spec_close = text.rfind("```", spec_header_end + 1)
    if spec_close == -1 or spec_close <= spec_header_end:
        spec_body = text[spec_header_end + 1 :]
    else:
        spec_body = text[spec_header_end + 1 : spec_close]

    return design_body, spec_body


def _extract_block_single(text: str, labels: tuple[str, ...]) -> str | None:
    """Single-block extractor — used only when paired extraction fails."""
    fence = "```"
    pos = 0
    while True:
        start = text.find(fence, pos)
        if start == -1:
            return None
        header_end = text.find("\n", start + len(fence))
        if header_end == -1:
            return None
        label = text[start + len(fence) : header_end].strip().lower()
        end = text.find(fence, header_end + 1)
        if end == -1:
            return None
        if any(label == lbl or label.startswith(lbl) for lbl in labels):
            return text[header_end + 1 : end]
        pos = end + len(fence)


# Legacy alias for callers that imported the old symbol.
_extract_block = _extract_block_single


def _cost_from_usage(usage: LLMUsage, duration_seconds: float) -> CostBreakdown:
    return CostBreakdown(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        duration_seconds=duration_seconds,
    )
