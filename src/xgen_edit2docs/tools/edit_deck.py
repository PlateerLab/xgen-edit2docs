"""Chat-edit orchestrator: PPTX + instruction -> edited PPTX bytes.

Powers the web studio's "PPT 같이 만들기" chat. One call = one chat turn:

  1. analyzing_deck   — render every slide to a flat SVG (deterministic)
  2. planning_edits   — LLM planner turns the instruction + deck outline
                        into slide-level operations (edit / add / delete)
                        plus a natural-language chat reply
  3. editing_slides   — one LLM call per edit/add produces the new slide SVG
                        (image data is stubbed out of the prompt and
                        restored afterwards, so photos don't eat the context)
  4. applying_edits   — deterministic recompose of the package
                        (kept slides keep their identity, notes, animations)

A question-only instruction yields an empty plan: the original bytes are
returned untouched together with the planner's reply, so the chat can
answer without forging a new deck revision.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Literal
from xml.etree import ElementTree as ET

import yaml
from pydantic import Field

from ..config import resolve_model
from ..core.svg_to_pptx.layout_repair import repair_layout
from ..core.svg_to_pptx.pptx_edit import KeepSlide, NewSlide, recompose_pptx
from ..core.svg_to_pptx.svg_scale import scale_svg_to_viewbox
from ..llm import DEFAULT_MODEL, AnthropicClient, build_output_lang_directive, load_prompt
from ..llm.anthropic_client import LLMUsage
from ._reply_texts import reply_text
from ._workspace import temp_workspace
from .convert import ConvertRequest, convert_to_markdown
from .generate_deck import EventCallback, StageEvent, _emit, _merge_cost
from .render_preview import RenderPreviewRequest, render_preview
from .types import (
    DEFAULT_LANG,
    CostBreakdown,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)


class ChatTurn(ToolRequest):
    role: Literal["user", "assistant"]
    content: str


class EditDeckRequest(ToolRequest):
    pptx: bytes = Field(..., description="Current deck revision (PPTX bytes).")
    instruction: str = Field(..., min_length=1, description="This chat turn's request.")
    chat_history: list[ChatTurn] = Field(
        default_factory=list,
        description="Prior turns, oldest first. Only the last 12 are used.",
    )
    # Reference documents attached to THIS turn (PDF/DOCX/...) — converted to
    # markdown and handed to the planner so instructions like "이 문서 내용
    # 반영해서 3번 슬라이드 고쳐줘" have the material in context.
    sources: list[ConvertRequest] = Field(default_factory=list)
    lang: LangCode = DEFAULT_LANG
    model: str = DEFAULT_MODEL
    anthropic_api_key: str = Field(..., description="BYOK; never persisted.")
    # "제목 전부 바꿔줘" on a 20-slide deck legitimately needs ~20 ops; the
    # cap only guards against runaway plans. Truncation is reported in the
    # chat reply so the user knows to re-run for the remainder.
    max_operations: int = Field(default=20, ge=1, le=40)
    # A slide the LLM regenerates from SVG can only *draw* — native charts,
    # tables and diagrams would flatten into shapes (and their chart parts
    # orphan). With this on (default) the recompose carries those native
    # objects into the regenerated slide instead. Opt out to accept the old
    # flatten-everything behaviour.
    preserve_native: bool = Field(default=True)


class EditDeckResponse(ToolResponse):
    pptx: bytes = Field(..., description="New revision; original bytes if no ops.")
    changed: bool = Field(..., description="False for question-only turns.")
    page_count: int
    reply: str = Field(..., description="Planner's chat reply (user language).")
    operations: list[dict] = Field(default_factory=list)
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


# Retry reminder for a plan-missing first response. Passed as the LLM
# call's ``user_suffix`` (the volatile tail) so the large ``planner_user``
# prefix — deck outline + sources + history, up to ~19k tokens — stays
# byte-identical between the first call and the retry and is READ from cache
# on the retry instead of re-sent at full price.
_PLAN_REMINDER = (
    "\n\n# REMINDER\nYour previous answer was missing the "
    "```edit_plan fenced block. Respond again following the "
    "output format EXACTLY: a ```reply block, then a "
    "```edit_plan block (operations: [] only if truly no "
    "change is needed)."
)


async def edit_deck(
    req: EditDeckRequest,
    *,
    on_event: EventCallback = None,
) -> EditDeckResponse:
    """Run one chat-edit turn. Raises on unrecoverable errors."""
    started = time.perf_counter()
    warnings: list[WarningEntry] = []
    cost = CostBreakdown()

    await _emit(on_event, StageEvent(stage="queued", progress=0.0, message_key="stages.queued"))

    # Stage 1: render the current deck (deterministic) and convert any
    # attached reference documents to markdown, in parallel.
    await _emit(
        on_event,
        StageEvent(stage="analyzing_deck", progress=0.08, message_key="stages.analyzing_deck"),
    )
    preview_task = asyncio.to_thread(render_preview, RenderPreviewRequest(pptx=req.pptx))
    convert_tasks = [
        asyncio.to_thread(convert_to_markdown, src) for src in req.sources
    ]
    preview, *convert_results = await asyncio.gather(preview_task, *convert_tasks)
    cost = _merge_cost(cost, preview.cost, *[c.cost for c in convert_results])
    warnings.extend(preview.warnings)
    for c in convert_results:
        warnings.extend(c.warnings)
    sources_markdown = [c.markdown for c in convert_results]
    slide_svgs = [s.svg for s in preview.slides]
    canvas_w, canvas_h = preview.width_px, preview.height_px

    # Native-content inventory (charts / tables / diagrams) per slide. The
    # planner needs to know which slides carry native objects so it prefers
    # text-level edits there; the recompose will preserve them either way.
    slide_natives = _native_inventory(req.pptx) if req.preserve_native else []

    # Stage 2: plan (LLM).
    await _emit(
        on_event,
        StageEvent(stage="planning_edits", progress=0.20, message_key="stages.planning_edits"),
    )
    client = AnthropicClient(api_key=req.anthropic_api_key, model=req.model)
    planner_system = build_output_lang_directive(req.lang) + "\n\n" + load_prompt("editor-planner")
    planner_user = _build_planner_message(
        req, slide_svgs, canvas_w, canvas_h,
        sources_markdown=sources_markdown,
        slide_natives=slide_natives,
    )
    planner_model = resolve_model("planner", req.model)
    result = await client.complete(
        system_prompt=planner_system,
        user_message=planner_user,
        max_output_tokens=16384,
        cache_system=True,
        model=planner_model,
    )
    cost = _merge_cost(cost, _cost_from_usage(result.usage))
    reply, operations, plan_missing = _parse_plan(result.text, warnings, lang=req.lang)

    # The model occasionally answers conversationally ("...하겠습니다") and
    # forgets the edit_plan block entirely. Without a retry the user sees a
    # promise followed by zero changes — retry once with a hard reminder.
    # The retry re-sends the SAME ``planner_user`` (cache read) and carries
    # the reminder in ``user_suffix`` so we don't re-pay for the whole prompt.
    if plan_missing:
        retry_result = await client.complete(
            system_prompt=planner_system,
            user_message=planner_user,
            user_suffix=_PLAN_REMINDER,
            max_output_tokens=16384,
            cache_system=True,
            model=planner_model,
        )
        cost = _merge_cost(cost, _cost_from_usage(retry_result.usage))
        reply, operations, plan_missing = _parse_plan(retry_result.text, warnings, lang=req.lang)
        if plan_missing:
            # Be honest in the chat instead of promising changes that never
            # happened (production case: "레이아웃 재구성하겠습니다" + no-op).
            reply = reply.rstrip() + reply_text("plan_failed", req.lang)

    operations = _validate_operations(
        operations, page_count=len(slide_svgs), cap=req.max_operations, warnings=warnings
    )
    truncated = next(
        (w for w in warnings if w.code == "edit_plan_truncated"), None
    )
    if truncated is not None and truncated.detail:
        reply = reply.rstrip() + reply_text(
            "plan_truncated", req.lang,
            emitted=truncated.detail["emitted"], cap=truncated.detail["cap"],
        )

    if not operations:
        await _emit(on_event, StageEvent(stage="done", progress=1.0, message_key="stages.done"))
        return EditDeckResponse(
            pptx=req.pptx,
            changed=False,
            page_count=len(slide_svgs),
            reply=reply,
            operations=[],
            cost=_finalise_cost(cost, started),
            warnings=warnings,
        )

    # Stage 3: generate SVGs for edit/add ops (LLM, parallel).
    # Announce the whole plan up front so the studio can render the todo
    # list and highlight each target as it streams.
    from ._edit_events import op_event_vars, op_summary, plan_event_vars

    op_summaries = {
        id(op): op_summary("pptx", op, index=i, total=len(operations), lang=req.lang)
        for i, op in enumerate(operations)
    }
    # Per-op honesty: an edit that regenerates a slide carrying native
    # objects will preserve them; surface that additively in the op event
    # detail so the studio can show "chart kept" alongside "slide edited".
    for op in operations:
        if op["action"] == "edit":
            idx = op["slide"] - 1
            if 0 <= idx < len(slide_natives) and slide_natives[idx].kinds:
                op_summaries[id(op)]["preserved"] = list(slide_natives[idx].kinds)
    await _emit(
        on_event,
        StageEvent(
            stage="planning_edits",
            progress=0.38,
            message_key="stages.planning_edits",
            message_vars=plan_event_vars("pptx", operations, lang=req.lang),
        ),
    )
    await _emit(
        on_event,
        StageEvent(stage="editing_slides", progress=0.40, message_key="stages.editing_slides"),
    )
    slide_system = build_output_lang_directive(req.lang) + "\n\n" + load_prompt("editor-slide")

    async def _gen(op: dict) -> tuple[dict, str | None]:
        await _emit(
            on_event,
            StageEvent(
                stage="editing_slides",
                progress=0.5,
                message_key="stages.editing_slides",
                message_vars=op_event_vars(op_summaries[id(op)], phase="start"),
            ),
        )
        if op["action"] == "edit":
            base_svg = slide_svgs[op["slide"] - 1]
            task = f"Edit this slide according to the brief.\n\n## Brief\n{op['brief']}"
        else:  # add
            ref_idx = min(max(op["after"], 1), len(slide_svgs)) - 1
            base_svg = slide_svgs[ref_idx]
            task = (
                "Create a NEW slide. The SVG below is a neighbouring slide — "
                "match its visual style (colors, fonts, margins) but replace "
                f"the content per the brief.\n\n## Brief\n{op['brief']}"
            )
        stubbed, image_map = _stub_images(base_svg)
        user_message = (
            f"# Canvas\nviewBox: 0 0 {canvas_w:g} {canvas_h:g}\n\n"
            f"# Task\n{task}\n\n# Current slide SVG\n```svg\n{stubbed}\n```"
        )
        r = await client.complete(
            system_prompt=slide_system,
            user_message=user_message,
            max_output_tokens=16384,
            cache_system=True,
            model=req.model,
        )
        nonlocal cost
        cost = _merge_cost(cost, _cost_from_usage(r.usage))
        svg = _extract_svg_block(r.text)
        if svg is None:
            warnings.append(
                WarningEntry(
                    code="edit_slide_svg_missing",
                    message=(
                        f"Slide editor returned no SVG for op {op}; the "
                        "operation was skipped."
                    ),
                )
            )
            await _emit(
                on_event,
                StageEvent(
                    stage="editing_slides", progress=0.6,
                    message_key="stages.editing_slides",
                    message_vars=op_event_vars(
                        op_summaries[id(op)], phase="done", status="failed"
                    ),
                ),
            )
            return op, None
        svg = _restore_images(svg, image_map)
        svg = scale_svg_to_viewbox(svg, canvas_w, canvas_h)
        repaired = repair_layout(svg, canvas=(int(canvas_w), int(canvas_h)))
        await _emit(
            on_event,
            StageEvent(
                stage="editing_slides", progress=0.6,
                message_key="stages.editing_slides",
                message_vars=op_event_vars(
                    op_summaries[id(op)], phase="done", status="applied"
                ),
            ),
        )
        return op, repaired.repaired_svg

    svg_ops = [op for op in operations if op["action"] in ("edit", "add")]
    # Emit delete ops (no LLM step) as their own done events for the UI.
    for op in operations:
        if op["action"] == "delete":
            await _emit(
                on_event,
                StageEvent(
                    stage="editing_slides", progress=0.55,
                    message_key="stages.editing_slides",
                    message_vars=op_event_vars(
                        op_summaries[id(op)], phase="done", status="applied"
                    ),
                ),
            )
    generated = await asyncio.gather(*(_gen(op) for op in svg_ops))
    svg_by_op_id = {id(op): svg for op, svg in generated}

    # Stage 4: apply (deterministic recompose).
    await _emit(
        on_event,
        StageEvent(stage="applying_edits", progress=0.85, message_key="stages.applying_edits"),
    )
    applied: list[dict] = []
    with temp_workspace(prefix="xgen_edit2docs-editdeck-") as ws:
        sequence: list[KeepSlide | NewSlide | None] = [
            KeepSlide(i) for i in range(len(slide_svgs))
        ]
        inserts: dict[int, list[NewSlide]] = {}  # position (0=start) -> new slides
        counter = 0
        for op in operations:
            svg = svg_by_op_id.get(id(op))
            if op["action"] == "delete":
                sequence[op["slide"] - 1] = None
                applied.append({"action": "delete", "slide": op["slide"]})
            elif op["action"] == "edit":
                if svg is None:
                    continue
                path = ws / f"edit_{counter}.svg"
                counter += 1
                path.write_text(svg, encoding="utf-8")
                # replaces=index enables native-content preservation for
                # this regenerated slide in the recompose.
                sequence[op["slide"] - 1] = NewSlide(path, replaces=op["slide"] - 1)
                applied.append({"action": "edit", "slide": op["slide"]})
            else:  # add
                if svg is None:
                    continue
                path = ws / f"add_{counter}.svg"
                counter += 1
                path.write_text(svg, encoding="utf-8")
                inserts.setdefault(op["after"], []).append(NewSlide(path))
                applied.append({"action": "add", "after": op["after"]})

        final_sequence: list[KeepSlide | NewSlide] = list(inserts.get(0, []))
        for i, entry in enumerate(sequence):
            if entry is not None:
                final_sequence.append(entry)
            final_sequence.extend(inserts.get(i + 1, []))

        if not final_sequence:
            raise ValueError(
                "The requested edits would delete every slide. "
                "요청된 편집은 모든 슬라이드를 삭제하게 되어 적용할 수 없습니다."
            )
        if not applied:
            # Every generation failed — surface as unchanged turn.
            await _emit(on_event, StageEvent(stage="done", progress=1.0, message_key="stages.done"))
            return EditDeckResponse(
                pptx=req.pptx,
                changed=False,
                page_count=len(slide_svgs),
                reply=reply,
                operations=[],
                cost=_finalise_cost(cost, started),
                warnings=warnings,
            )

        host_path = ws / "host.pptx"
        host_path.write_bytes(req.pptx)
        out_path = ws / "out.pptx"
        recompose_warnings = await asyncio.to_thread(
            recompose_pptx,
            host_path,
            final_sequence,
            out_path,
            preserve_native=req.preserve_native,
        )
        for w in recompose_warnings:
            warnings.append(
                WarningEntry(
                    code=w["code"], message=w["message"], detail=w.get("detail")
                )
            )
        new_pptx = out_path.read_bytes()
        new_page_count = len(final_sequence)

    await _emit(on_event, StageEvent(stage="done", progress=1.0, message_key="stages.done"))
    return EditDeckResponse(
        pptx=new_pptx,
        changed=True,
        page_count=new_page_count,
        reply=reply,
        operations=applied,
        cost=_finalise_cost(cost, started),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Native-content inventory
# ---------------------------------------------------------------------------


@dataclass
class _SlideNatives:
    """Native objects on one slide, for planner visibility + op honesty.

    ``labels`` are human strings for the deck outline (e.g.
    ``'bar chart "Sales by Quarter"'``); ``kinds`` are the deduped
    coarse kinds (``"chart"`` / ``"table"`` / ``"diagram"``) that the
    recompose actually preserves — used to annotate op events.
    """

    labels: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)


def _native_inventory(pptx: bytes) -> list[_SlideNatives]:
    """Per-slide (0-based) inventory of native charts / tables / diagrams.

    Uses xgen_contextifier's raw layer to read the deck losslessly. Chart kind
    and title come cheaply from :class:`ChartModel`. Any failure (missing
    dependency, malformed package) degrades to an empty inventory — the
    planner simply loses the annotation, never the turn.
    """
    try:
        from xgen_contextifier import open_raw
    except Exception:  # pragma: no cover - xgen_contextifier is a hard dep
        return []
    try:
        raw = open_raw(pptx, extension="pptx")
    except Exception:
        return []
    out: list[_SlideNatives] = []
    try:
        for slide in raw.slides:
            info = _SlideNatives()
            try:
                shapes = slide.shapes
            except Exception:
                out.append(info)
                continue
            # Charts: pair each chart graphicFrame with a ChartModel for
            # its kind + title (order matches document order well enough
            # for a human-readable annotation).
            try:
                charts = slide.charts
            except Exception:
                charts = []
            chart_i = 0
            for sh in shapes:
                if sh.kind == "chart":
                    label = "chart"
                    if chart_i < len(charts):
                        cm = charts[chart_i]
                        chart_i += 1
                        try:
                            kind = cm.kind
                            title = cm.title
                        except Exception:
                            kind, title = None, None
                        label = f"{kind} chart" if kind else "chart"
                        if title:
                            label += f' "{title}"'
                    info.labels.append(label)
                    if "chart" not in info.kinds:
                        info.kinds.append("chart")
                elif sh.kind == "diagram":
                    info.labels.append("diagram (SmartArt)")
                    if "diagram" not in info.kinds:
                        info.kinds.append("diagram")
            # Tables: dimensions from the raw table facade.
            try:
                tables = slide.tables
            except Exception:
                tables = []
            for tbl in tables:
                try:
                    info.labels.append(f"table {tbl.n_rows}x{tbl.n_cols}")
                except Exception:
                    info.labels.append("table")
                if "table" not in info.kinds:
                    info.kinds.append("table")
            out.append(info)
    finally:
        try:
            raw.close()
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _build_planner_message(
    req: EditDeckRequest,
    slide_svgs: list[str],
    canvas_w: float,
    canvas_h: float,
    *,
    sources_markdown: list[str] | None = None,
    slide_natives: list[_SlideNatives] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# Deck outline")
    lines.append(f"Canvas: {canvas_w:g} x {canvas_h:g} px · {len(slide_svgs)} slides")
    natives = slide_natives or []
    for i, svg in enumerate(slide_svgs, start=1):
        text = _extract_slide_text(svg, limit=280)
        line = f"- Slide {i}: {text or '(no text)'}"
        labels = natives[i - 1].labels if i - 1 < len(natives) else []
        if labels:
            line += f"  [native: {', '.join(labels)}]"
        lines.append(line)
    lines.append("")
    if sources_markdown:
        lines.append("# Reference documents (attached to this turn)")
        lines.append(
            "Use these as source material when the instruction refers to "
            "attached files. Quote concrete facts/text from them in your "
            "briefs so the slide editor can write real content."
        )
        for i, md in enumerate(sources_markdown, start=1):
            body = md.strip()
            if len(body) > 6000:
                body = body[:6000] + "\n…(truncated)"
            lines.append(f"## Document {i}")
            lines.append("```markdown")
            lines.append(body)
            lines.append("```")
        lines.append("")
    if req.chat_history:
        lines.append("# Chat history (most recent last)")
        for turn in req.chat_history[-12:]:
            lines.append(f"[{turn.role}] {turn.content.strip()[:500]}")
        lines.append("")
    lines.append("# Instruction (this turn)")
    lines.append(req.instruction.strip())
    return "\n".join(lines)


_TEXT_TAG = re.compile(r"[{].*[}]text$|^text$")


def _extract_slide_text(svg: str, limit: int = 280) -> str:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return ""
    pieces: list[str] = []
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag in ("text", "tspan") and el.text and el.text.strip():
            pieces.append(el.text.strip())
    out = " · ".join(pieces)
    return out[:limit]


# ---------------------------------------------------------------------------
# Plan parsing / validation
# ---------------------------------------------------------------------------


def _extract_block(text: str, label: str) -> str | None:
    match = re.search(rf"```{label}\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Tolerate an unclosed fence: long plans can hit the output-token limit
    # mid-block, losing the trailing ``` — take everything to EOF and let
    # YAML parsing decide how much of it survives.
    match = re.search(rf"```{label}\s*\n(.*)$", text, re.DOTALL)
    return match.group(1).strip() if match else None


def _parse_plan(
    text: str, warnings: list[WarningEntry], *, lang: str = "en-US"
) -> tuple[str, list[dict], bool]:
    """Return ``(reply, operations, plan_missing)``.

    ``plan_missing`` is True only when the edit_plan block is absent or
    unparseable — an explicit ``operations: []`` is a valid "no changes
    needed" answer, not a failure.
    """
    reply = _extract_block(text, "reply") or ""
    plan_raw = _extract_block(text, "edit_plan")
    if not reply:
        # Fall back to any prose before the first fence.
        prefix = text.split("```", 1)[0].strip()
        reply = prefix or reply_text("request_done", lang)
    if plan_raw is None:
        warnings.append(
            WarningEntry(
                code="edit_plan_block_missing",
                message="Planner output had no edit_plan block.",
            )
        )
        return reply, [], True
    try:
        data = yaml.safe_load(plan_raw) or {}
    except yaml.YAMLError:
        # Unclosed-fence recovery can leave a truncated final entry; drop
        # lines from the tail until the YAML parses (keeps complete ops).
        data = _parse_yaml_prefix(plan_raw)
        if data is None:
            warnings.append(
                WarningEntry(
                    code="edit_plan_yaml_invalid",
                    message="Planner YAML failed to parse.",
                )
            )
            return reply, [], True
        warnings.append(
            WarningEntry(
                code="edit_plan_yaml_truncated_recovered",
                message="Planner YAML was truncated; recovered the parseable prefix.",
            )
        )
    ops = data.get("operations") if isinstance(data, dict) else None
    return reply, ops if isinstance(ops, list) else [], False


def _parse_yaml_prefix(raw: str) -> dict | None:
    """Best-effort parse of a truncated YAML document, longest prefix first."""
    lines = raw.splitlines()
    for end in range(len(lines) - 1, 0, -1):
        try:
            data = yaml.safe_load("\n".join(lines[:end]))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("operations"), list):
            return data
    return None


def _validate_operations(
    raw_ops: list, *, page_count: int, cap: int, warnings: list[WarningEntry]
) -> list[dict]:
    ops: list[dict] = []
    edited: set[int] = set()
    for raw in raw_ops:
        if not isinstance(raw, dict):
            continue
        action = raw.get("action")
        if action == "edit" or action == "delete":
            slide = raw.get("slide")
            if not isinstance(slide, int) or slide < 1 or slide > page_count:
                warnings.append(
                    WarningEntry(
                        code="edit_op_slide_out_of_range",
                        message=f"Skipped op with invalid slide number: {raw}",
                    )
                )
                continue
            if slide in edited:
                warnings.append(
                    WarningEntry(
                        code="edit_op_duplicate_slide",
                        message=f"Skipped second op targeting slide {slide}.",
                    )
                )
                continue
            edited.add(slide)
            op = {"action": action, "slide": slide}
            if action == "edit":
                op["brief"] = str(raw.get("brief") or "").strip()
                if not op["brief"]:
                    warnings.append(
                        WarningEntry(
                            code="edit_op_brief_missing",
                            message=f"Skipped edit op without a brief: {raw}",
                        )
                    )
                    edited.discard(slide)
                    continue
            ops.append(op)
        elif action == "add":
            after = raw.get("after", page_count)
            if not isinstance(after, int) or after < 0 or after > page_count:
                after = page_count
            brief = str(raw.get("brief") or "").strip()
            if not brief:
                warnings.append(
                    WarningEntry(
                        code="edit_op_brief_missing",
                        message=f"Skipped add op without a brief: {raw}",
                    )
                )
                continue
            ops.append({"action": "add", "after": after, "brief": brief})
        else:
            warnings.append(
                WarningEntry(
                    code="edit_op_unknown_action",
                    message=f"Skipped op with unknown action: {raw}",
                )
            )
    if len(ops) > cap:
        warnings.append(
            WarningEntry(
                code="edit_plan_truncated",
                message=f"Planner emitted {len(ops)} operations; applying the first {cap}.",
                detail={"emitted": len(ops), "cap": cap},
            )
        )
        ops = ops[:cap]
    return ops


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

_DATA_URI = re.compile(r"(href=[\"'])(data:[^\"']+)([\"'])")


def _stub_images(svg: str) -> tuple[str, dict[str, str]]:
    """Replace data-URI image payloads with ``asset:IMG_n`` placeholders.

    A single embedded photo can be hundreds of KB of base64 — far past any
    sensible prompt budget. The mapping restores the originals afterwards.
    """
    mapping: dict[str, str] = {}
    counter = 0

    def _swap(match: re.Match) -> str:
        nonlocal counter
        counter += 1
        key = f"asset:IMG_{counter}"
        mapping[key] = match.group(2)
        return f"{match.group(1)}{key}{match.group(3)}"

    return _DATA_URI.sub(_swap, svg), mapping


def _restore_images(svg: str, mapping: dict[str, str]) -> str:
    for key, data in mapping.items():
        svg = svg.replace(key, data)
    return svg


def _extract_svg_block(text: str) -> str | None:
    block = _extract_block(text, "svg")
    if block and "<svg" in block:
        return block
    # Tolerate a bare <svg> document without the fence.
    match = re.search(r"<svg\b.*</svg>", text, re.DOTALL)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Cost helpers
# ---------------------------------------------------------------------------


def _cost_from_usage(usage: LLMUsage) -> CostBreakdown:
    return CostBreakdown(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def _finalise_cost(cost: CostBreakdown, started: float) -> CostBreakdown:
    return CostBreakdown(
        input_tokens=cost.input_tokens,
        output_tokens=cost.output_tokens,
        cache_read_tokens=cost.cache_read_tokens,
        cache_write_tokens=cost.cache_write_tokens,
        image_count=cost.image_count,
        audio_seconds=cost.audio_seconds,
        duration_seconds=time.perf_counter() - started,
    )
