"""1-shot orchestrator: sources + intent -> PPTX bytes.

Wires the M2 tool layer end-to-end so a caller can produce a deck with a
single function call:

    result = await generate_deck(GenerateDeckRequest(
        sources=[ConvertRequest(source_type="pdf", content=pdf_bytes)],
        user_intent="Executive briefing on Q3 sales results",
        target_pages=(8, 12),
        lang="en-US",  # any supported locale, e.g. "ko-KR"
        anthropic_api_key="sk-ant-...",
    ))

Pipeline stages:
  1. convert    — each source -> markdown (parallel)
  2. strategize — LLM produces design_spec + spec_lock + page_plan
  3. images     — (optional) per-page image acquisition (parallel)
  4. execute    — LLM produces per-page SVG (parallel)
  5. quality    — SVG quality checks (deterministic)
  6. export     — SVGs -> PPTX (deterministic)
  7. narrate    — (optional) speaker notes -> MP3 (parallel)

Each stage emits a `StageEvent` via the `on_event` callback so callers
(workers, MCP servers) can stream progress.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

logger = logging.getLogger(__name__)
from typing import Awaitable, Callable, Literal, Protocol

from pydantic import Field

from ..llm import AnthropicClient, DEFAULT_MODEL
from ._image_plan import ImagePlanItem, parse_image_plan
from .audio import NarrateRequest, NarrateSlide, narrate_async
from .convert import ConvertRequest, ConvertResponse, convert_to_markdown
from .execute import (
    ExecuteBatchRequest,
    ExecutePageRequest,
    ExecutorImage,
    ExecutorStyle,
    execute_batch,
)
from .export import ExportRequest, ExportResponse, SlideInput, export_pptx
from .images import (
    GenerateImageRequest,
    SearchImageRequest,
    generate_image,
    search_image,
)
from .quality import QualityCheckRequest, QualityCheckResponse, QualitySlide, check_svg_quality
from .strategize import StrategizeRequest, StrategizeResponse, strategize
from .types import (
    CanvasFormat,
    CostBreakdown,
    DEFAULT_CANVAS,
    DEFAULT_LANG,
    LangCode,
    QualityIssue,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)

StageName = Literal[
    "queued",
    "converting",
    "analyzing_template",
    "strategizing",
    "acquiring_images",
    "executing_pages",
    "checking_quality",
    "narrating",
    "exporting",
    # edit_deck (chat editing) stages — see tools/edit_deck.py
    "analyzing_deck",
    "planning_edits",
    "editing_slides",
    "applying_edits",
    "done",
    "failed",
]


class StageEvent(ToolResponse):
    """Progress event emitted by the orchestrator. Subscribers map to MCP/SSE."""

    stage: StageName
    progress: float = Field(..., ge=0.0, le=1.0)
    message_key: str  # i18n catalog key, e.g. "stages.executing_page"
    message_vars: dict = Field(default_factory=dict)
    page_index: int | None = None


EventCallback = Callable[[StageEvent], Awaitable[None]] | Callable[[StageEvent], None] | None


class GenerateDeckRequest(ToolRequest):
    # 0 or more source documents. When empty, the Strategist designs from
    # `user_intent` alone — the topic-only / "just chat" path.
    sources: list[ConvertRequest] = Field(default_factory=list)
    user_intent: str = Field(..., min_length=1)
    target_pages: tuple[int, int] = (8, 12)
    canvas_format: CanvasFormat = DEFAULT_CANVAS
    style: ExecutorStyle = "general"
    lang: LangCode = DEFAULT_LANG
    template_name: str | None = None

    # User-provided PPTX template (raw bytes) + how to use it:
    #   "new"              — ignore template_pptx; current from-scratch path.
    #   "template_restyle" — generate a fresh deck INSIDE the template
    #                        package (its masters/theme/layouts preserved,
    #                        original slides removed).
    #   "template_extend"  — append the generated slides to the template's
    #                        existing slides (deck grows).
    # In both template modes the Strategist receives a deterministic
    # analysis digest and canvas_format is overridden to match the
    # template's slide size (16:9 -> ppt169, 4:3 -> ppt43).
    template_pptx: bytes | None = None
    deck_mode: Literal["new", "template_restyle", "template_extend"] = "new"

    model: str = DEFAULT_MODEL
    anthropic_api_key: str = Field(..., description="BYOK; never persisted.")
    fail_on_quality_error: bool = True

    # When > 0, pages flagged as quality errors are re-run that many times
    # with an extra "the previous SVG had errors; emit something simpler"
    # hint appended to their page_summary. Pairs well with
    # fail_on_quality_error=True to attempt recovery before giving up.
    retry_pages_on_quality_error: int = Field(default=2, ge=0, le=3)

    # BYOK keys for image acquisition. Map of provider env-var names to keys,
    # e.g. {"OPENAI_API_KEY": "sk-...", "PEXELS_API_KEY": "..."}. The keys are
    # exported as env vars only for the duration of each image call.
    image_api_keys: dict[str, str] = Field(default_factory=dict)

    # Defaults for the image plan when the Strategist's spec_lock leaves them
    # implicit. Both can be overridden per-page via the spec_lock image entry.
    default_image_backend: str = "openai"
    default_search_providers: list[str] = Field(
        default_factory=lambda: ["pexels", "pixabay"]
    )

    # Skip the image acquisition stage entirely (text-only deck). Useful for
    # tests / low-cost runs.
    skip_images: bool = False

    # Narration / audio.
    narrate: bool = Field(
        default=False,
        description=(
            "When true, synthesize per-slide speaker notes with Edge-TTS and "
            "embed the resulting MP3s into the PPTX so PowerPoint auto-plays "
            "them on slide entry."
        ),
    )
    narration_voice: str | None = Field(
        default=None,
        description="Edge-TTS ShortName. None -> lang's default voice (ko-KR -> SunHi).",
    )
    narration_rate: str = Field(default="+0%", description='Speaking rate, e.g. "+0%", "-10%".')
    narration_use_timings: bool = Field(
        default=False,
        description=(
            "If true, slide auto-advance times derive from each MP3's duration "
            "(plus narration_padding). Pairs with `narrate=True`."
        ),
    )
    narration_padding: float = Field(default=0.5, ge=0.0)


class GenerateDeckResponse(ToolResponse):
    pptx: bytes
    page_count: int
    spec_lock: str
    design_spec: str
    detected_langs: list[LangCode]
    quality_issues: list[QualityIssue]
    cost: CostBreakdown
    # Per-stage token attribution so operators can see WHERE spend went
    # (strategize / execute / quality / export / …). Additive: the
    # aggregate `cost` field above is unchanged. Maps stage name ->
    # {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}.
    stage_costs: dict[str, dict] = Field(default_factory=dict)
    # Structural snapshot of the assembled deck (see
    # ``_export_metrics.ExportMetrics``). Optional so older callers
    # / tests that don't pass through the export stage still construct
    # cleanly.
    export_metrics: dict = Field(default_factory=dict)
    warnings: list[WarningEntry] = Field(default_factory=list)


async def generate_deck(
    req: GenerateDeckRequest,
    *,
    on_event: EventCallback = None,
) -> GenerateDeckResponse:
    """Run the full pipeline. Raises on unrecoverable errors."""
    started = time.perf_counter()
    warnings: list[WarningEntry] = []
    cost = CostBreakdown()
    # Per-stage token attribution (item 5). Populated alongside the running
    # aggregate `cost` so operators can see where spend accrued. Retry
    # execute/quality rounds fold into their base stage.
    stage_costs: dict[str, CostBreakdown] = {}

    await _emit(on_event, StageEvent(stage="queued", progress=0.0, message_key="stages.queued"))

    # Stage 1: convert sources (parallel). Skipped entirely when the
    # caller didn't supply any — the Strategist works from user_intent alone.
    sources_markdown: list[str] = []
    if req.sources:
        await _emit(
            on_event,
            StageEvent(stage="converting", progress=0.05, message_key="stages.converting"),
        )
        convert_results = await asyncio.gather(
            *(asyncio.to_thread(convert_to_markdown, src) for src in req.sources)
        )
        cost = _merge_cost(cost, *[r.cost for r in convert_results])
        _record_stage_cost(stage_costs, "convert", *[r.cost for r in convert_results])
        for r in convert_results:
            warnings.extend(r.warnings)
        sources_markdown = [r.markdown for r in convert_results]

    # Stage 1.5: analyze the user-provided template PPTX (deterministic).
    # Overrides canvas_format so downstream stages (layout brief, executor,
    # export scale) all speak the template's slide geometry.
    canvas_format = req.canvas_format
    deck_mode = req.deck_mode
    template_context: str | None = None
    template_analysis = None
    if req.template_pptx is not None and deck_mode == "new":
        deck_mode = "template_restyle"
        warnings.append(
            WarningEntry(
                code="deck_mode_defaulted_to_template_restyle",
                message=(
                    "template_pptx was provided with deck_mode='new'; "
                    "defaulting to 'template_restyle'."
                ),
            )
        )
    if deck_mode != "new" and req.template_pptx is None:
        raise ValueError(
            f"deck_mode={deck_mode!r} requires template_pptx. "
            "Template mode requires a template PPTX file. 템플릿 모드는 템플릿 PPTX 파일이 필요합니다."
        )
    if req.template_pptx is not None:
        await _emit(
            on_event,
            StageEvent(
                stage="analyzing_template",
                progress=0.12,
                message_key="stages.analyzing_template",
            ),
        )
        from .analyze_template import AnalyzeTemplateRequest, analyze_template

        template_analysis = await asyncio.to_thread(
            analyze_template,
            AnalyzeTemplateRequest(pptx=req.template_pptx, deck_mode=deck_mode),
        )
        cost = _merge_cost(cost, template_analysis.cost)
        _record_stage_cost(stage_costs, "analyze_template", template_analysis.cost)
        warnings.extend(template_analysis.warnings)
        template_context = template_analysis.template_context
        if template_analysis.canvas_format != canvas_format:
            warnings.append(
                WarningEntry(
                    code="canvas_format_overridden_by_template",
                    message=(
                        f"canvas_format {canvas_format!r} -> "
                        f"{template_analysis.canvas_format!r} to match the "
                        f"template's slide size "
                        f"({template_analysis.host_width_px}x{template_analysis.host_height_px}px)."
                    ),
                )
            )
            canvas_format = template_analysis.canvas_format

    # Stage 2: strategize (LLM)
    await _emit(
        on_event,
        StageEvent(stage="strategizing", progress=0.20, message_key="stages.strategizing"),
    )
    client = AnthropicClient(api_key=req.anthropic_api_key, model=req.model)
    strat: StrategizeResponse = await strategize(
        StrategizeRequest(
            sources_markdown=sources_markdown,
            user_intent=req.user_intent,
            template_name=req.template_name,
            template_context=template_context,
            target_pages=req.target_pages,
            canvas_format=canvas_format,
            style=req.style,
            lang=req.lang,
            model=req.model,
            anthropic_api_key=req.anthropic_api_key,
        ),
        client=client,
    )
    cost = _merge_cost(cost, strat.cost)
    _record_stage_cost(stage_costs, "strategize", strat.cost)
    warnings.extend(strat.warnings)

    page_summaries = _split_page_plan(
        strat.design_spec,
        strat.spec_lock,
        raw_output=strat.raw_output,
    )

    # Defence against template-reference noise. deck_4.pptx production
    # case: design_spec §VII listed chart-template references like
    # `- P03 · BAR CHART` that the page regex picked up as extra
    # pages, producing a 15-slide deck whose first 5 slides were
    # template descriptions instead of content. Two filters:
    #   1. Keep only the contiguous run starting at P01 (the first
    #      time the regex hits "1" as an index). Anything before is
    #      reference content; anything after a gap is noise.
    #   2. Truncate to the page count spec_lock declared, when it
    #      did. The Strategist's `project.pages_total: N` is the
    #      definitive contract — Executor SHOULD NOT exceed it.
    _before = len(page_summaries)
    page_summaries = _consecutive_run_starting_at_one(page_summaries)
    if len(page_summaries) != _before:
        warnings.append(
            WarningEntry(
                code="page_plan_trimmed_to_consecutive_run",
                message=(
                    f"{_before} candidate pages detected in design_spec but only "
                    f"the contiguous run of {len(page_summaries)} starting at P01 is "
                    "included in the deck; the rest look like chart/template references. "
                    f"(연속 구간 {len(page_summaries)}개만 포함, 나머지 제외)"
                ),
                detail={"detected": _before, "kept": len(page_summaries)},
            )
        )

    _expected = _expected_page_count(strat.spec_lock)
    if _expected is not None and len(page_summaries) > _expected:
        warnings.append(
            WarningEntry(
                code="page_plan_truncated_to_spec_lock",
                message=(
                    f"Detected {len(page_summaries)} pages, more than spec_lock's "
                    f"pages_total={_expected}; truncated to {_expected}. "
                    f"({_expected}개로 잘랐습니다)"
                ),
                detail={
                    "spec_lock_pages_total": _expected,
                    "detected": len(page_summaries),
                },
            )
        )
        page_summaries = page_summaries[:_expected]

    if not page_summaries:
        # Surface enough of the Strategist response to actually diagnose
        # the failure. Cap each section at ~4 KB to keep logs readable.
        headings = _all_markdown_headings(strat.design_spec)
        logger.error(
            "Strategist output did not yield any page summaries.\n"
            "design_spec length=%d, spec_lock length=%d.\n"
            "markdown headings in design_spec (truncated to first 40):\n%s\n"
            "spec_lock (first 2 KB):\n%s\n"
            "design_spec (last 2 KB):\n%s",
            len(strat.design_spec or ""),
            len(strat.spec_lock or ""),
            "\n".join(headings[:40]) or "<none>",
            (strat.spec_lock or "")[:2000],
            (strat.design_spec or "")[-2000:],
        )
        raise RuntimeError(
            "Strategist output did not yield any page summaries; "
            "cannot run executor. Inspect strat.raw_output."
        )

    # Stage 3: image acquisition. Parse the Strategist's image plan, fetch
    # each image (AI-generated or web-searched), and bundle the bytes for
    # both the Executor (so it can reference them by placeholder) and the
    # Export stage (so it can drop the files alongside the SVGs in the
    # workspace).
    images_by_page: dict[int, list[ExecutorImage]] = {}
    image_bytes_by_filename: dict[str, bytes] = {}
    if not req.skip_images:
        plan = parse_image_plan(strat.spec_lock)
        if plan:
            await _emit(
                on_event,
                StageEvent(
                    stage="acquiring_images",
                    progress=0.30,
                    message_key="stages.acquiring_images",
                ),
            )
            for item in plan:
                try:
                    image_bytes, mime, ack = await asyncio.to_thread(
                        _acquire_image,
                        item,
                        req,
                    )
                except Exception as exc:
                    warnings.append(
                        WarningEntry(
                            code="image_acquisition_failed",
                            message=(
                                f"Page {item.page_index} image "
                                f"{item.placeholder!r}: {exc}"
                            ),
                            detail={"page_index": item.page_index, "placeholder": item.placeholder},
                        )
                    )
                    continue

                ext = _ext_for_mime(mime)
                filename = f"{item.placeholder}{ext}"
                image_bytes_by_filename[filename] = image_bytes
                images_by_page.setdefault(item.page_index, []).append(
                    ExecutorImage(
                        placeholder=item.placeholder,
                        url=filename,  # relative path the SVG will reference
                        description=item.description or ack,
                    )
                )
                cost = CostBreakdown(
                    input_tokens=cost.input_tokens,
                    output_tokens=cost.output_tokens,
                    cache_read_tokens=cost.cache_read_tokens,
                    cache_write_tokens=cost.cache_write_tokens,
                    image_count=cost.image_count + 1,
                    audio_seconds=cost.audio_seconds,
                    duration_seconds=cost.duration_seconds,
                )

    # Stage 4: execute pages (parallel, LLM)
    await _emit(
        on_event,
        StageEvent(stage="executing_pages", progress=0.40, message_key="stages.executing_pages"),
    )

    # Generate deterministic per-page layout briefs (P2.1). The
    # Executor's user_message places each brief BEFORE the page
    # outline so the LLM treats the box geometry as hard constraints
    # — page-number / footer / chapter-label dimensions are stable
    # across pages and across runs.
    from ._layout_brief import build_layout_briefs, render_brief_yaml

    layout_briefs = build_layout_briefs(
        spec_lock=strat.spec_lock,
        page_count=len(page_summaries),
    )

    page_reqs: list[ExecutePageRequest] = [
        ExecutePageRequest(
            spec_lock=strat.spec_lock,
            page_index=i,
            page_summary=summary,
            images=images_by_page.get(i, []),
            layout_brief_yaml=(
                render_brief_yaml(layout_briefs[i]) if i < len(layout_briefs) else None
            ),
            style=req.style,
            lang=req.lang,
            model=req.model,
            anthropic_api_key=req.anthropic_api_key,
        )
        for i, summary in enumerate(page_summaries)
    ]
    exec_batch = await execute_batch(
        ExecuteBatchRequest(spec_lock=strat.spec_lock, pages=page_reqs),
        client=client,
    )
    cost = _merge_cost(cost, exec_batch.cost)
    _record_stage_cost(stage_costs, "execute", exec_batch.cost)
    warnings.extend(exec_batch.warnings)

    # Stage 5: quality check (with optional retry-on-error)
    await _emit(
        on_event,
        StageEvent(stage="checking_quality", progress=0.80, message_key="stages.checking_quality"),
    )
    page_results = {p.page_index: p for p in exec_batch.results}
    quality_resp = _run_quality_check(page_results, canvas_format, image_bytes_by_filename)
    cost = _merge_cost(cost, quality_resp.cost)
    _record_stage_cost(stage_costs, "quality", quality_resp.cost)

    # Layout-repair surfaced its findings on each page's `warnings`
    # list. Anything that couldn't be auto-fixed (`fix_applied=False`)
    # should drive a retry — promote those to quality errors here so
    # the retry loop sees them. Auto-fixed violations stay as warnings;
    # the model doesn't need to re-attempt them.
    _promote_layout_violations(page_results, quality_resp)

    # Per-page retry loop. Each round re-runs only the pages flagged as
    # `quality_error`. Stops when no errors remain or the retry budget is
    # exhausted. Failures are still reported as warnings on the final
    # response — `fail_on_quality_error=True` is checked AFTER retries.
    # Retry budget tracked two ways:
    #   * per_page_cap — max retries for any single page
    #   * total_remaining — global cap across all retries this deck
    # The legacy `retry_pages_on_quality_error: int` is interpreted as
    # the per-page cap; the total cap derives from page_count * per-page
    # with a hard floor of 6 so that a single misbehaving page can't
    # exhaust the total before others get any attempts.
    per_page_cap = req.retry_pages_on_quality_error
    total_remaining = max(6, len(page_summaries) * per_page_cap)
    per_page_attempts: dict[int, int] = {}
    round_n = 0

    while total_remaining > 0:
        round_n += 1
        # Group every error per page so the retry hint can name every
        # rule the previous attempt broke. A generic "make it simpler"
        # nudge isn't enough — the model has to know exactly which
        # element type the converter would reject.
        errors_by_page: dict[int, list[QualityIssueLike]] = {}
        for issue in quality_resp.issues:
            if issue.severity != "error" or issue.page_index is None:
                continue
            errors_by_page.setdefault(issue.page_index, []).append(issue)

        # Filter out pages that have exhausted their per-page budget.
        # Skipped pages keep their last quality state and surface a
        # warning so the operator can see they were dropped.
        failing_pages = sorted(
            i for i in errors_by_page.keys()
            if per_page_attempts.get(i, 0) < per_page_cap
        )
        exhausted = sorted(
            i for i in errors_by_page.keys()
            if per_page_attempts.get(i, 0) >= per_page_cap
        )
        if exhausted:
            warnings.append(
                WarningEntry(
                    code="retry_per_page_cap_reached",
                    message=(
                        f"{len(exhausted)} page(s) hit the per-page retry cap "
                        f"({per_page_cap}) and will not be retried further. "
                        f"(재시도 한도 {per_page_cap}회 도달)"
                    ),
                    detail={"pages": exhausted, "per_page_cap": per_page_cap},
                )
            )
        if not failing_pages:
            break

        # Cap the round to the remaining total budget.
        if len(failing_pages) > total_remaining:
            picked = failing_pages[:total_remaining]
            dropped = failing_pages[total_remaining:]
            warnings.append(
                WarningEntry(
                    code="retry_total_budget_reached",
                    message=(
                        f"Only {total_remaining} retries left in the total budget: "
                        f"retrying {len(picked)} page(s), deferring {len(dropped)}. "
                        f"(나머지 {len(dropped)}페이지 보류)"
                    ),
                    detail={
                        "picked": picked,
                        "dropped": dropped,
                        "total_remaining": total_remaining,
                    },
                )
            )
            failing_pages = picked

        warnings.append(
            WarningEntry(
                code="quality_retry",
                message=(
                    f"Retrying {len(failing_pages)} page(s) with quality errors "
                    f"(round {round_n}, per-page cap={per_page_cap}, "
                    f"total remaining={total_remaining})."
                ),
                detail={"pages": failing_pages, "round": round_n},
            )
        )
        retry_reqs = [
            ExecutePageRequest(
                spec_lock=strat.spec_lock,
                page_index=i,
                page_summary=(
                    page_summaries[i]
                    + "\n\n"
                    + _build_retry_hint(errors_by_page[i])
                ),
                images=images_by_page.get(i, []),
                layout_brief_yaml=(
                    render_brief_yaml(layout_briefs[i]) if i < len(layout_briefs) else None
                ),
                style=req.style,
                lang=req.lang,
                model=req.model,
                anthropic_api_key=req.anthropic_api_key,
            )
            for i in failing_pages
        ]
        retry_batch = await execute_batch(
            ExecuteBatchRequest(spec_lock=strat.spec_lock, pages=retry_reqs),
            client=client,
        )
        cost = _merge_cost(cost, retry_batch.cost)
        _record_stage_cost(stage_costs, "execute", retry_batch.cost)
        warnings.extend(retry_batch.warnings)
        for r in retry_batch.results:
            page_results[r.page_index] = r
        for i in failing_pages:
            per_page_attempts[i] = per_page_attempts.get(i, 0) + 1
            total_remaining -= 1

        quality_resp = _run_quality_check(page_results, canvas_format, image_bytes_by_filename)
        cost = _merge_cost(cost, quality_resp.cost)
        _record_stage_cost(stage_costs, "quality", quality_resp.cost)
        _promote_layout_violations(page_results, quality_resp)

    # Surface unresolved quality errors and (when configured) hard-fail.
    if not quality_resp.passed:
        errors = [i for i in quality_resp.issues if i.severity == "error"]
        if req.fail_on_quality_error:
            raise RuntimeError(
                f"Quality check failed with {len(errors)} error(s) after "
                f"{round_n} retry round(s) (per-page cap "
                f"{per_page_cap}). Set fail_on_quality_error=False to "
                "export anyway."
            )
        warnings.append(
            WarningEntry(
                code="quality_errors_present",
                message=(
                    f"Exporting with {len(errors)} unresolved quality error(s); "
                    "PPT may not render every element correctly."
                ),
                detail={"error_count": len(errors)},
            )
        )

    # Apply any retried page results back into the batch view.
    exec_batch.results[:] = sorted(page_results.values(), key=lambda r: r.page_index)

    # Stage 6: prepare slide inputs (shared by audio + export below).
    slides = [
        SlideInput(
            index=p.page_index,
            name=f"slide_{p.page_index:02d}",
            svg=p.svg,
            notes=p.speaker_notes or None,
        )
        for p in exec_batch.results
    ]

    # Stage 7: (optional) narration — synthesize MP3 per slide BEFORE export
    # so the engine can embed audio into the PPTX. Failures here flow to
    # warnings and the deck still exports without audio.
    narration_bytes_by_slide: dict[str, bytes] = {}
    if req.narrate:
        narratable = [
            NarrateSlide(
                index=s.index,
                name=s.name,
                notes_markdown=s.notes or "",
            )
            for s in slides
            if s.notes
        ]
        if narratable:
            await _emit(
                on_event,
                StageEvent(stage="narrating", progress=0.88, message_key="stages.narrating"),
            )
            try:
                narration_resp = await narrate_async(
                    NarrateRequest(
                        slides=narratable,
                        lang=req.lang,
                        voice=req.narration_voice,
                        rate=req.narration_rate,
                    )
                )
                for audio in narration_resp.audios:
                    narration_bytes_by_slide[audio.name] = audio.mp3
                cost = CostBreakdown(
                    input_tokens=cost.input_tokens,
                    output_tokens=cost.output_tokens,
                    cache_read_tokens=cost.cache_read_tokens,
                    cache_write_tokens=cost.cache_write_tokens,
                    image_count=cost.image_count,
                    audio_seconds=cost.audio_seconds + narration_resp.cost.audio_seconds,
                    duration_seconds=cost.duration_seconds,
                )
                warnings.extend(narration_resp.warnings)
            except Exception as exc:
                warnings.append(
                    WarningEntry(
                        code="narration_failed",
                        message=(
                            f"Narration synthesis failed: {exc}. "
                            "Deck exports without audio."
                        ),
                    )
                )

    # Stage 8: export
    await _emit(
        on_event,
        StageEvent(stage="exporting", progress=0.92, message_key="stages.exporting"),
    )
    export_resp: ExportResponse = export_pptx(
        ExportRequest(
            slides=slides,
            canvas_format=canvas_format,
            lang=req.lang,
            images=image_bytes_by_filename,
            narration_audio=narration_bytes_by_slide,
            narration_padding=req.narration_padding,
            use_narration_timings=req.narration_use_timings,
            # Template modes splice the generated slides into the user's
            # package instead of building a fresh deck.
            host_pptx=req.template_pptx if deck_mode != "new" else None,
            clear_existing_slides=(deck_mode == "template_restyle"),
            host_px=(
                (template_analysis.host_width_px, template_analysis.host_height_px)
                if deck_mode != "new" and template_analysis is not None
                else None
            ),
        )
    )
    cost = _merge_cost(cost, export_resp.cost)
    _record_stage_cost(stage_costs, "export", export_resp.cost)
    warnings.extend(export_resp.warnings)

    cost = CostBreakdown(
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_write_tokens=cost.cache_write_tokens,
        image_count=cost.image_count,
        audio_seconds=cost.audio_seconds,
        duration_seconds=time.perf_counter() - started,
    )

    # Post-export metrics: re-open the freshly-built pptx in memory and
    # collect slide-level structural statistics. Cheap (one zip+xml
    # pass) and gives the operator a "deck health" snapshot they can
    # see in the UI alongside the download button.
    from ._export_metrics import compute_export_metrics
    export_metrics = compute_export_metrics(export_resp.pptx)
    if export_metrics.placeholder_slides:
        warnings.append(
            WarningEntry(
                code="export_placeholder_slides",
                message=(
                    f"{export_metrics.placeholder_slides} slide(s) were replaced by "
                    "placeholders because their SVG conversion failed. "
                    f"({export_metrics.placeholder_slides}장 placeholder 대체됨)"
                ),
                detail={"placeholder_count": export_metrics.placeholder_slides},
            )
        )
    if export_metrics.color_palette_size > 12:
        warnings.append(
            WarningEntry(
                code="export_palette_too_large",
                message=(
                    f"Color palette has {export_metrics.color_palette_size} colors "
                    "(spec_lock recommends ~6) — consider consolidating for design "
                    f"consistency. (팔레트 {export_metrics.color_palette_size}개)"
                ),
                detail={"palette_size": export_metrics.color_palette_size},
            )
        )

    await _emit(on_event, StageEvent(stage="done", progress=1.0, message_key="stages.done"))

    return GenerateDeckResponse(
        pptx=export_resp.pptx,
        page_count=export_resp.page_count,
        spec_lock=strat.spec_lock,
        design_spec=strat.design_spec,
        detected_langs=export_resp.detected_langs,
        quality_issues=quality_resp.issues,
        export_metrics=export_metrics.to_dict(),
        cost=cost,
        stage_costs=_stage_costs_to_dict(stage_costs),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Lightweight Protocol so the retry-hint builder doesn't need to import
# `quality.QualityIssue` for type purposes only.
class QualityIssueLike(Protocol):
    code: str
    message: str
    severity: str


# Targeted hints by issue code. The dispatcher below picks the matching
# strings and assembles a per-page retry directive; when several codes
# fire on the same page, every applicable rule is enumerated so the
# model sees the full picture in one shot.
_RETRY_HINTS: dict[str, str] = {
    "forbidden_use_data_icon": (
        "Do NOT emit `<use data-icon=\"...\"/>`. Replace each icon with its "
        "primitive shapes (`<path>` / `<circle>` / `<rect>`). The Executor "
        "must output self-contained SVG — there is no icon-expansion pass."
    ),
    "forbidden_use_href": (
        "Do NOT emit `<use href=\"#...\"/>` or `<use xlink:href=\"#...\"/>`. "
        "Inline the referenced shape directly. The converter does not "
        "follow `<use>` references."
    ),
    "forbidden_use_bare": (
        "Remove the `<use>` element entirely or replace it with the "
        "primitives it was supposed to clone."
    ),
    "forbidden_foreign_object": (
        "Do NOT use `<foreignObject>`. Wrap multi-line text in `<text>` + "
        "multiple `<tspan>` elements (each `<tspan>` carries its own x/y)."
    ),
    "forbidden_script": (
        "Do NOT include `<script>` or event-handler attributes. SVG must "
        "be static."
    ),
    # Layout-repair violations the pass couldn't auto-fix. Each hint
    # tells the model the exact box and dimensions to reposition.
    "layout_overlap": (
        "Two visible boxes overlap. Re-emit the page so every text element "
        "occupies its own non-overlapping rectangle. Pay particular attention "
        "to caption text sitting INSIDE a hero number's bounding box — push "
        "the caption below the hero's bottom edge."
    ),
    "layout_text_overflow_x": (
        "A `<text>` element's content is wider than the box that holds it. "
        "Either widen the parent `<g>` / `<rect>` to fit the text, or move "
        "the text to a longer container, or break it across two `<tspan>` "
        "lines."
    ),
    "layout_off_canvas": (
        "An element extends past the 1280×720 canvas. Move it back inside "
        "the safe area (40 px margin on every edge, so x∈[40, 1240] and "
        "y∈[40, 680])."
    ),
    "layout_empty_decoration": (
        "An empty `<g>` / `<rect>` (no fill, no stroke, no children) was "
        "stripped. Either give the shape a fill / stroke or remove it."
    ),
}


# Layout-violation severity gate (token optimization). An unfixed layout
# violation only PROMOTES to a retry-worthy quality error when it's
# STRUCTURALLY broken — a full page re-generation is expensive (~70% of a
# retry's spend is wasted when the trigger is cosmetic). Sub-threshold
# violations stay as page warnings the operator can see, but they don't
# drive a retry:
#   * off_canvas          — always structural (content spills off the slide).
#   * text_overflow_x      — structural only when the text needs meaningfully
#                            more width than its box (required_w / box_w).
#   * overlap              — structural only when the boxes overlap heavily.
_OVERLAP_PROMOTE_RATIO = 0.5        # promote overlaps above 50% IoU
_TEXT_OVERFLOW_PROMOTE_RATIO = 1.15  # promote overflow needing >1.15x the box


def _layout_violation_is_structural(code: str, detail: dict) -> bool:
    """True when an unfixed layout violation is broken enough to justify a
    (costly) page re-generation, per the severity thresholds above.

    Missing measurements degrade to "promote" — we can't prove the
    violation is cosmetic, so the safe move is to let the retry fire.
    """
    kind = code[len("layout_"):] if code.startswith("layout_") else code
    actual = detail.get("actual") if isinstance(detail, dict) else None
    if not isinstance(actual, dict):
        actual = {}
    if kind == "off_canvas":
        return True
    if kind == "text_overflow_x":
        req_w = actual.get("required_w")
        box_w = actual.get("box_w")
        if req_w and box_w and box_w > 0:
            return (req_w / box_w) > _TEXT_OVERFLOW_PROMOTE_RATIO
        return True  # unmeasurable → treat as structural
    if kind == "overlap":
        ratio = actual.get("overlap_ratio")
        if ratio is None:
            return True  # unmeasurable → treat as structural
        return ratio > _OVERLAP_PROMOTE_RATIO
    # Anything else (e.g. empty_decoration) is cosmetic / already repaired.
    return False


def _promote_layout_violations(
    page_results: dict,
    quality_resp,
) -> None:
    """Convert unfixed *structural* layout violations from per-page
    warnings into quality errors so the retry loop targets them.

    Auto-fixed violations (the repair pass mutated the SVG) stay as
    informational warnings — the operator can see what we changed,
    but the model doesn't need to re-attempt. Of the unfixed ones, only
    STRUCTURALLY broken violations (off_canvas, heavy overflow/overlap;
    see `_layout_violation_is_structural`) promote to a retry-worthy
    quality error. Minor cosmetic violations (small overlaps,
    sub-threshold overflow) stay as page warnings — re-generating a whole
    page to nudge a barely-visible overlap is not worth the tokens.

    The measured coordinates from `detail.actual` are embedded into
    the message string so the downstream retry hint can quote them
    back to the model verbatim. QualityIssue has no `detail` field,
    so message is the carrier.
    """
    from .quality import QualityIssue

    for page_index, page in page_results.items():
        for w in getattr(page, "warnings", []) or []:
            code = getattr(w, "code", "")
            if not code.startswith("layout_"):
                continue
            detail = getattr(w, "detail", None) or {}
            if detail.get("fix_applied") is True:
                continue
            # Severity gate: only structurally-broken violations promote.
            # Cosmetic ones remain as the page warning already surfaced.
            if not _layout_violation_is_structural(code, detail):
                continue
            quality_resp.issues.append(
                QualityIssue(
                    page_index=page_index,
                    severity="error",
                    code=code,
                    message=_format_layout_violation_message(code, detail),
                    location=f"slide_{page_index:02d}",
                )
            )
            # The quality_resp `passed` flag tracks error presence; any
            # new error means the overall pass is now failed.
            quality_resp.passed = False


def _format_layout_violation_message(code: str, detail: dict) -> str:
    """Render a layout-repair violation as a human + machine-friendly
    message with the measured coordinates baked in. The model reads
    this back on retry and can correct against the exact numbers."""
    actual = detail.get("actual") if isinstance(detail, dict) else None
    if not isinstance(actual, dict):
        return code.replace("_", " ")
    if code == "layout_overlap":
        small = actual.get("small_bbox")
        big = actual.get("big_bbox")
        ratio = actual.get("overlap_ratio")
        if small and big:
            sb = tuple(int(v) for v in small)
            bb = tuple(int(v) for v in big)
            r_pct = int((ratio or 0) * 100)
            return (
                f"Two elements overlap by {r_pct}%: the small box (x={sb[0]}, "
                f"y={sb[1]}, w={sb[2]}, h={sb[3]}) sits inside the big box "
                f"(x={bb[0]}, y={bb[1]}, w={bb[2]}, h={bb[3]}). Move the small "
                f"box below the big one (y >= {bb[1] + bb[3] + 8})."
            )
    if code == "layout_text_overflow_x":
        req_w = actual.get("required_w")
        box_w = actual.get("box_w")
        text = actual.get("text")
        if req_w and box_w:
            return (
                f"Text \"{text}\" is wider than its box ({int(box_w)}px; needs "
                f"at least {int(req_w)}px). Widen the container to ≥{int(req_w)}px "
                "or wrap the text onto two lines."
            )
    if code == "layout_off_canvas":
        bbox = actual.get("bbox")
        if bbox:
            b = tuple(int(v) for v in bbox)
            return (
                f"Element extends outside the 1280x720 canvas (x={b[0]}, "
                f"y={b[1]}, w={b[2]}, h={b[3]}). Reposition it inside the safe "
                "area (40..1240, 40..680)."
            )
    if code == "layout_empty_decoration":
        return (
            "Removed an empty <g>/<rect> (no fill, stroke, or children). "
            "If it was decoration, give it an explicit fill or stroke."
        )
    return code.replace("_", " ")


def _build_retry_hint(errors: list[QualityIssueLike]) -> str:
    """Compose a precise correction directive for one page's retry call.

    The hint enumerates every distinct rule violated, in stable order,
    plus a fallback "simplify if you can't comply" footer so the model
    always has a way forward. Generic feedback is the last resort —
    every targeted error gets its own actionable line.
    """
    if not errors:
        return ""
    codes_seen: list[str] = []
    for err in errors:
        if err.code in _RETRY_HINTS and err.code not in codes_seen:
            codes_seen.append(err.code)

    lines = [
        "> Retry hint: the previous SVG failed strict converter-parity "
        "validation. Re-emit this page following EVERY rule below:",
    ]
    if codes_seen:
        for code in codes_seen:
            lines.append(f"> - {_RETRY_HINTS[code]}")
    # Layout-* errors carry measured coordinates in their `.message`
    # (populated by `_format_layout_violation_message`). Surface those
    # alongside the directive so the model sees the exact numbers it
    # needs to correct. Without this the model only gets the generic
    # "fix the overlap" without knowing WHICH boxes overlap.
    layout_msgs = [
        e for e in errors
        if (e.code or "").startswith("layout_") and (e.message or "")
    ]
    if layout_msgs:
        lines.append("> Measurements from the previous attempt:")
        seen_msgs: set[str] = set()
        for err in layout_msgs:
            if err.message in seen_msgs:
                continue
            seen_msgs.add(err.message)
            lines.append(f">   • {err.message}")
    # Generic context — useful when the failure was from the legacy
    # quality checker (no machine-readable code).
    other = [
        e for e in errors
        if e.code not in _RETRY_HINTS and not (e.code or "").startswith("layout_")
    ]
    if other:
        lines.append("> Additional quality errors to fix:")
        for err in other[:6]:  # cap; the rest is repetitive
            lines.append(f"> - {err.message}")
    lines.append(
        "> When in doubt, prefer simpler output: fewer shapes, plain text + "
        "primitives, no `<use>` / `<foreignObject>` / `<script>` ever."
    )
    return "\n".join(lines)


def _run_quality_check(
    page_results: dict[int, "ExecutePageResponse"],
    canvas_format: CanvasFormat,
    images: dict[str, bytes] | None = None,
) -> QualityCheckResponse:
    """Run the SVG quality checker over the current page results.

    *images* is the same bundle the export stage uses (basename →
    bytes). Forwarded so quality's workspace has the files it needs
    to verify `<image href>` references — otherwise every cover_bg /
    chapter divider gets reported as missing even though the export
    will resolve them fine.
    """
    from .execute import ExecutePageResponse  # for the forward ref

    quality_slides = [
        QualitySlide(
            index=p.page_index,
            name=f"slide_{p.page_index:02d}",
            svg=p.svg,
        )
        for p in sorted(page_results.values(), key=lambda r: r.page_index)
    ]
    return check_svg_quality(
        QualityCheckRequest(
            slides=quality_slides,
            canvas_format=canvas_format,
            images=images or {},
        )
    )


def _acquire_image(
    item: ImagePlanItem,
    req: "GenerateDeckRequest",
) -> tuple[bytes, str, str | None]:
    """Resolve a single ImagePlanItem to (bytes, mime, ack-text).

    Generate path: dispatches to `tools.images.generate_image`.
    Search path:   dispatches to `tools.images.search_image`.

    The third return value is an acknowledgment / attribution string the
    Executor can show as a credit line ("Photo: …") when required.
    """
    if item.mode == "generate":
        prompt = item.prompt or item.description or ""
        if not prompt:
            raise ValueError(f"image plan item {item.placeholder!r} has no prompt")
        backend = item.backend or req.default_image_backend
        result = generate_image(
            GenerateImageRequest(
                prompt=prompt,
                backend=backend,
                aspect_ratio=item.aspect_ratio,
                api_keys=req.image_api_keys,
            )
        )
        return result.image, result.mime_type, None

    if item.mode == "search":
        query = item.query or item.description or ""
        if not query:
            raise ValueError(f"image plan item {item.placeholder!r} has no query")
        providers = item.providers or req.default_search_providers
        result = search_image(
            SearchImageRequest(
                query=query,
                providers=providers,
                aspect_ratio=item.aspect_ratio,
                api_keys=req.image_api_keys,
            )
        )
        ack = None
        if result.attribution:
            ack = f"사진: {result.attribution}" if "Korean" in str(item.description or "") or result.license == "CC BY" else f"Photo: {result.attribution}"
        return result.image, result.mime_type, ack

    raise ValueError(f"unknown image plan mode {item.mode!r}")


def _ext_for_mime(mime_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime_type, ".png")


# Heading patterns the Strategist might emit for per-page sections. Ordered
# so that the most specific (Page/Slide/페이지/슬라이드 + index) wins first;
# numbered fallback (`## 1.` / `## 1) Title`) catches templates that drop
# the keyword. All patterns are case-insensitive and anchored to a line start.
# The depth range 1–6 covers Markdown's full heading scale — the reference
# design_spec template emits page outlines as h4 (`#### Slide 01 - Cover`)
# under `## IX. Content Outline` / `### Part 1: Chapter`.
_PAGE_HEADING_PATTERNS = [
    # `## Page 1`, `## Page-1`, `## Page #1`, `## Page: 1`
    r"^#{1,6}\s+(?:Page|Slide|페이지|슬라이드|ページ|スライド)[\s\-:#]*\d+",
    # `## Slide 1: Title`  / `## 페이지 1 — 표지`
    r"^#{1,6}\s+(?:Page|Slide|페이지|슬라이드|ページ|スライド)\s+\d+[\s\-:—:]",
    # Numbered heading without a keyword: `## 1.`, `## 1)`, `## 1 - Title`
    r"^#{1,6}\s+\d+[\.\)\-]\s",
    # P-id with optional line-start marker. This single pattern subsumes
    # every observed Strategist variant — the markdown heading wraps
    # (`#### P01. 커버`), the bold wraps (`**P06 — 두 가지 길 (anchor)**`),
    # the list-item wraps (`- P01: anchor`), and the bare line-start form
    # (`P01 — Cover`). The optional prefix group means we don't have to
    # add a new alternative every time the model picks a new wrapper.
    r"^\s*(?:#{1,6}\s+|\*\*\s*|-\s+)?P\d{1,3}\b",
]
_PAGE_HEADING_RE = re.compile(
    "|".join(f"(?:{p})" for p in _PAGE_HEADING_PATTERNS),
    re.MULTILINE | re.IGNORECASE,
)

# Section markers for the Content Outline part of design_spec. When we can
# locate this section the scan focuses there, which kills the false-positive
# risk of a generic P-id pattern matching prose that happens to mention P01
# in body text on the page before the actual outline starts.
_OUTLINE_SECTION_RE = re.compile(
    r"^#{1,6}\s+(?:"
    r"(?:[IVX]+|9|IX)\s*[\.\):]\s*)?"
    r"(?:Content Outline|콘텐츠 아웃라인|콘텐츠 개요|콘텐츠 구성"
    r"|Outline|아웃라인|目次|アウトライン|大纲|大綱"
    r"|페이지 아웃라인|페이지\s*개요|페이지\s*구성|페이지\s*outline"
    r"|슬라이드\s*아웃라인|슬라이드\s*개요|슬라이드\s*구성|슬라이드\s*outline"
    r"|내용\s*개요|내용\s*구성|Pages?\s+Outline)",
    re.MULTILINE | re.IGNORECASE,
)


# How many pages does spec_lock say the deck has? The Strategist's
# reference template recommends an explicit `pages_total: N` /
# `page_count: N` row inside the `project:` block. When that's
# present we trust it as the upper bound — anything `_split_page_plan`
# returns above this count is template-reference noise.
_PAGE_TOTAL_RE = re.compile(
    r"(?:pages?_total|pages?_count|page_count|total_pages|deck_pages|slide_count)"
    r"\s*[:=]\s*(\d{1,3})",
    re.IGNORECASE,
)


def _expected_page_count(spec_lock: str) -> int | None:
    """Extract the declared page count from spec_lock when present.

    The Strategist puts ``pages_total: N`` in the project header of
    the spec_lock; we read that as the deck size. Returns None when
    spec_lock doesn't declare one.
    """
    if not spec_lock:
        return None
    m = _PAGE_TOTAL_RE.search(spec_lock)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 100 else None


_PAGE_ID_RE = re.compile(r"P0*([0-9]{1,3})\b", re.IGNORECASE)


def _consecutive_run_starting_at_one(summaries: list[str]) -> list[str]:
    """Filter ``summaries`` to the contiguous run of pages whose
    extracted P-id starts at 1 (or 01).

    Production failure (deck_4.pptx): Strategist's design_spec carried
    chart-template references like ``- P03 · BAR CHART`` in §VII before
    the real §IX outline. The generic P-id regex picked them up as
    extra pages, so the deck shipped 15 slides where 10 were real and
    5 were template descriptions. The fix is to find the first chunk
    that starts at index 1 and use only that. Summaries without an
    extractable P-id (cover pages titled by name) pass through as long
    as they're inside the run.
    """
    if not summaries:
        return summaries

    # Build a per-summary index extracted from the first P-id mention.
    indexes: list[int | None] = []
    for s in summaries:
        m = _PAGE_ID_RE.search(s)
        indexes.append(int(m.group(1)) if m else None)

    # Locate the first summary whose extracted index is 1.
    start = None
    for i, idx in enumerate(indexes):
        if idx == 1:
            start = i
            break

    if start is None:
        # No P01 anchor — fall back to the original list. The deck
        # might use a different numbering scheme (just `01.` / `Slide 1`).
        return summaries

    # From the anchor, accept summaries while they're either a
    # successor index or unnumbered (titles).
    out = [summaries[start]]
    expected_next = 2
    for s, idx in zip(summaries[start + 1 :], indexes[start + 1 :]):
        if idx is None:
            out.append(s)
            continue
        if idx == expected_next:
            out.append(s)
            expected_next += 1
            continue
        if idx == expected_next - 1:
            # Same index seen twice — the regex matched twice on one
            # page (e.g. P05 title plus P05 anchor reference). Tolerate.
            continue
        # Index jump → end of the run.
        break
    return out


def _split_page_plan(
    design_spec: str,
    spec_lock: str,
    *,
    raw_output: str | None = None,
) -> list[str]:
    """Extract per-page content summaries from the Strategist's output.

    Heading patterns supported (case-insensitive, line-start, h1-h6):
        Page / Slide / 페이지 / 슬라이드 / ページ / スライド  + index
        Page-1 / Slide#1 / Slide: 1 / Page 1: Title / 페이지 1 — 표지
        Numbered headings without keyword (`## 1.`, `## 1)`, `## 1 - Title`)

    Resolution order — each layer covers a different Strategist quirk:
      1a. Locate `## IX. Content Outline` (or translated equivalent) and
          scan ONLY inside it. Cuts down on false-positive P-id matches
          in body prose that mentions page numbers.
      1b. Heading scan over the entire `design_spec` (if 1a missed).
      2. YAML parse of `spec_lock` looking for `pages` / `page_rhythm` /
         `page_layouts` / `outline` / `slides` collections (list or map).
      3. Markdown-style `## page_rhythm` / `## page_layouts` sections
         inside `spec_lock` (the format the shipped spec_lock_reference
         uses — markdown headings, not YAML keys).
      4. Heading scan on `raw_output` (catches the case where fence
         extraction truncated design_spec mid-document).
      5. Legacy YAML-ish line-walker over a `pages:` block.

    Returns [] only if every layer comes up empty — the caller then
    logs a diagnostic dump and raises.
    """
    # Layer 1a: outline-scoped scan. If we can locate the §IX section,
    # confine page-heading matching there so prose elsewhere can't
    # produce spurious page boundaries.
    outline_match = _OUTLINE_SECTION_RE.search(design_spec)
    if outline_match:
        outline = design_spec[outline_match.end() :]
        scoped = list(_PAGE_HEADING_RE.finditer(outline))
        if scoped:
            positions = [m.start() for m in scoped] + [len(outline)]
            return [
                outline[positions[i] : positions[i + 1]].strip()
                for i in range(len(positions) - 1)
            ]

    # Layer 1b: page headings anywhere in design_spec.
    matches = list(_PAGE_HEADING_RE.finditer(design_spec))
    if matches:
        positions = [m.start() for m in matches] + [len(design_spec)]
        return [
            design_spec[positions[i] : positions[i + 1]].strip()
            for i in range(len(positions) - 1)
        ]

    # Layer 2: YAML-parsed spec_lock with a top-level list collection.
    yaml_chunks = _pages_from_spec_lock_yaml(spec_lock)
    if yaml_chunks:
        return yaml_chunks

    # Layer 3: markdown-style spec_lock — the shipped reference template
    # uses `## page_rhythm` / `## page_layouts` markdown sections with
    # `- P01: anchor` data lines. Count those to derive the page list.
    md_chunks = _pages_from_spec_lock_markdown(spec_lock)
    if md_chunks:
        return md_chunks

    # Layer 4: scan the entire raw_output. Triggers when an internal
    # ``` truncated design_spec mid-document and §IX Content Outline
    # spilled out into raw_output but not into our `design_spec` slice.
    if raw_output:
        raw_matches = list(_PAGE_HEADING_RE.finditer(raw_output))
        if raw_matches:
            positions = [m.start() for m in raw_matches] + [len(raw_output)]
            return [
                raw_output[positions[i] : positions[i + 1]].strip()
                for i in range(len(positions) - 1)
            ]

    # Layer 5: legacy line-walker over spec_lock's `pages:` block —
    # tolerates variants the YAML parser rejects (inline maps, weird
    # indentation).
    chunks: list[str] = []
    in_pages = False
    current: list[str] = []
    for line in spec_lock.splitlines():
        stripped = line.rstrip()
        if not in_pages and stripped.strip().lower().startswith("pages:"):
            in_pages = True
            continue
        if in_pages:
            if stripped and not stripped.startswith((" ", "\t", "-")):
                if current:
                    chunks.append("\n".join(current).strip())
                break
            if stripped.startswith("-"):
                if current:
                    chunks.append("\n".join(current).strip())
                current = [stripped]
            else:
                current.append(stripped)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


# Matches `- P01: <anything>` data lines under a markdown `## page_rhythm`
# / `## page_layouts` / `## page_charts` section in spec_lock.
_SPEC_LOCK_PAGE_ROW_RE = re.compile(
    r"^\s*-\s*P\d{1,3}\s*:",
    re.MULTILINE | re.IGNORECASE,
)


def _pages_from_spec_lock_markdown(spec_lock: str) -> list[str]:
    """Read the markdown-shaped spec_lock used by the shipped reference
    template, where pages are declared as `- P01: tag` rows under one
    of several `## <section>` markdown headings.

    Strategy: collect every `- P<NN>: ...` row across the document and
    deduplicate by index. Each unique index becomes one page summary
    carrying every row attribute that mentions it (rhythm tag, layout
    name, chart template, etc.) so the executor still has structured
    context to work from.
    """
    if not spec_lock.strip():
        return []
    if not _SPEC_LOCK_PAGE_ROW_RE.search(spec_lock):
        return []

    # Map: "P01" -> list of attribute snippets harvested from each section.
    rows: dict[str, list[str]] = {}
    order: list[str] = []
    current_section = ""
    for line in spec_lock.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            current_section = stripped.lstrip("# ").strip().lower()
            continue
        m = re.match(r"^\s*-\s*(P\d{1,3})\s*:\s*(.*)$", line, flags=re.IGNORECASE)
        if not m:
            continue
        key = m.group(1).upper()
        value = m.group(2).strip()
        snippet = f"{current_section or 'page'}: {value}" if value else current_section or "page"
        if key not in rows:
            rows[key] = []
            order.append(key)
        rows[key].append(snippet)

    if not order:
        return []

    return [
        f"# {key}\n" + "\n".join(f"- {snip}" for snip in rows[key])
        for key in order
    ]


def _all_markdown_headings(text: str) -> list[str]:
    """Return every markdown heading line in *text* (h1-h6).

    Used by the diagnostic logger so an operator looking at a parse
    failure can immediately see what shape the Strategist actually
    produced — no need to scroll through 10 KB of design_spec.
    """
    if not text:
        return []
    return [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^\s*#{1,6}\s+\S", line)
    ]


_PAGE_COLLECTION_KEYS = (
    "pages",
    "page_rhythm",
    "page_layouts",
    "page_charts",
    "outline",
    "slides",
)


def _pages_from_spec_lock_yaml(spec_lock: str) -> list[str]:
    """Locate a page-shaped collection inside spec_lock and return one
    summary per entry.

    Two collection shapes are accepted:

    * List form (Appendix-K style): top-level key with a list of entries.
      Each entry becomes one summary.
    * Map form (reference body style): top-level key with a dict whose
      keys are P-ids (``P01``, ``P02`` …). Each entry becomes one summary
      keyed by P-id. When the same P-id appears under multiple keys
      (``page_rhythm`` + ``page_layouts`` …), they are merged.

    First key with a usable value wins for the list shape. For the map
    shape we union across all matching keys so the executor receives
    every attribute the Strategist recorded per page (rhythm tag, layout,
    chart template, …).
    """
    if not spec_lock.strip():
        return []
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml is a hard dep
        return []
    try:
        doc = yaml.safe_load(spec_lock)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []

    # List form — first key wins.
    for key in _PAGE_COLLECTION_KEYS:
        value = doc.get(key)
        if isinstance(value, list) and value:
            return [_yaml_entry_to_summary(item) for item in value if item is not None]

    # Map form — union across every matching key, preserve P-id order.
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for key in _PAGE_COLLECTION_KEYS:
        value = doc.get(key)
        if not isinstance(value, dict) or not value:
            continue
        for pid, attr in value.items():
            pid_str = str(pid).strip()
            if not pid_str:
                continue
            snippet = _yaml_attr_snippet(key, attr)
            if pid_str not in merged:
                merged[pid_str] = []
                order.append(pid_str)
            merged[pid_str].append(snippet)

    if not order:
        return []
    return [
        f"# {pid}\n" + "\n".join(f"- {snip}" for snip in merged[pid])
        for pid in order
    ]


def _yaml_attr_snippet(section: str, value: object) -> str:
    """Render one P-id's attribute (rhythm tag, layout name, etc.) as a
    short bullet line for the merged page summary."""
    if value is None:
        return section
    if isinstance(value, (str, int, float, bool)):
        return f"{section}: {value}"
    import yaml
    try:
        return f"{section}: " + yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip()
    except yaml.YAMLError:
        return f"{section}: {value!r}"


def _yaml_entry_to_summary(item: object) -> str:
    """Render a YAML list entry as a markdown-ish summary the Executor
    can read. Scalars become themselves; dicts become a `key: value`
    bullet list."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        import yaml  # local import; cheap when the call site already
        # decided this path is in play.
        try:
            return yaml.safe_dump(item, allow_unicode=True, sort_keys=False).strip()
        except yaml.YAMLError:
            return repr(item)
    return str(item)


async def _emit(callback: EventCallback, event: StageEvent) -> None:
    if callback is None:
        return
    result = callback(event)
    if asyncio.iscoroutine(result):
        await result


def _record_stage_cost(
    stage_costs: dict[str, CostBreakdown],
    stage: str,
    *costs: CostBreakdown,
) -> None:
    """Fold *costs* into the running per-stage total for *stage*.

    Retry rounds re-call this for "execute" / "quality" so a stage's
    entry reflects total spend across the initial pass plus every retry.
    """
    stage_costs[stage] = _merge_cost(stage_costs.get(stage, CostBreakdown()), *costs)


def _stage_costs_to_dict(stage_costs: dict[str, CostBreakdown]) -> dict[str, dict]:
    """Project the per-stage CostBreakdowns to the token-only dicts the
    response exposes (the four billable token counters)."""
    return {
        stage: {
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "cache_read_tokens": c.cache_read_tokens,
            "cache_write_tokens": c.cache_write_tokens,
        }
        for stage, c in stage_costs.items()
    }


def _merge_cost(base: CostBreakdown, *others: CostBreakdown) -> CostBreakdown:
    inp = base.input_tokens
    out = base.output_tokens
    cr = base.cache_read_tokens
    cw = base.cache_write_tokens
    ic = base.image_count
    aud = base.audio_seconds
    dur = base.duration_seconds
    for c in others:
        inp += c.input_tokens
        out += c.output_tokens
        cr += c.cache_read_tokens
        cw += c.cache_write_tokens
        ic += c.image_count
        aud += c.audio_seconds
        # don't sum duration — orchestrator tracks wall-clock separately
    return CostBreakdown(
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        image_count=ic,
        audio_seconds=aud,
        duration_seconds=dur,
    )
