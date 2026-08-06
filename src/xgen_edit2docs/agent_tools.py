"""xgen_edit2docs as a hierarchical, progressively-disclosed tool set for LLM agents.

Organized like a Claude Skill:

* **frontmatter** — the tool list below: nine tools, each with an
  ultra-compact one/two-line description. This is all that sits in the
  model's context up front.
* **body** — ``doc_guide()`` (no topic): the family map. The hierarchy splits
  **GENERATE vs EDIT** first, then by mechanism.
* **resources** — ``doc_guide(topic)``: deep per-task guides
  (``build``, ``generate``, ``edit``, ``edit.text``, ``edit.chart``,
  ``edit.xml``, ``render``, ``recipes.slides``, ``recipes.colors``) loaded
  only when the agent walks that branch.

``ANTHROPIC_TOOLS`` is a ready-to-send ``tools=[...]`` list for the Anthropic
Messages API; ``OPENAI_TOOLS`` is the same set in OpenAI function-calling
format; ``tool_specs(fmt)`` returns either. ``run_tool`` / ``run_tool_async``
dispatch a call to the library facade and return a JSON-safe dict.

All tools operate on local file paths. ``generate_doc`` / ``edit_doc`` need
an Anthropic key (``api_key=`` on the dispatcher or ``ANTHROPIC_API_KEY``);
the other seven are deterministic and keyless.
"""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = [
    "ANTHROPIC_TOOLS",
    "OPENAI_TOOLS",
    "TOOL_NAMES",
    "tool_specs",
    "run_tool",
    "run_tool_async",
]

_DOC_PATH = {
    "type": "string",
    "description": "Path to a local document (.docx / .xlsx / .pptx).",
}

# Frontmatter tier: every description is intentionally compact (enforced by
# tests). The detailed shapes/recipes live behind doc_guide(topic).
ANTHROPIC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "doc_guide",
        "description": (
            "START HERE for .docx/.xlsx/.pptx work — the document skill. "
            "No topic: the GENERATE|EDIT|INSPECT map. topic: deep guide "
            "(build, generate, edit, edit.text, edit.chart, edit.xml, "
            "render, recipes.slides, recipes.colors). Free, instant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Optional topic or prefix (e.g. 'recipes').",
                },
            },
        },
    },
    {
        "name": "analyze_doc",
        "description": (
            "Outline + edit addresses + charts list for a document. "
            "Deterministic, no key. Run FIRST before any edit. "
            "Guide: doc_guide('edit')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"doc": _DOC_PATH},
            "required": ["doc"],
        },
    },
    {
        "name": "render_doc",
        "description": (
            "Render a document: to=md (read the content) | svg | png | pdf. "
            "Deterministic, no key. Guide: doc_guide('render')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PATH,
                "to": {
                    "type": "string",
                    "enum": ["png", "pdf", "svg", "md"],
                    "description": "Output kind (default png; md = readable).",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Output directory (default <doc dir>/render).",
                },
                "dpi": {"type": "number", "description": "Raster dpi (default 144)."},
            },
            "required": ["doc"],
        },
    },
    {
        "name": "set_doc_text",
        "description": (
            "Deterministic structured edits at analyze_doc addresses — "
            "text/table/cell values AND chart title/data ({chart: i, ...}). "
            "No key; byte-preserves the rest. Shapes: doc_guide('edit.text'), "
            "doc_guide('edit.chart')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PATH,
                "edits": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Edit objects; a `chart` key routes to the chart "
                        "engine. Shapes: doc_guide('edit.text')."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": "Output path (default: <input>_edited.<ext>).",
                },
            },
            "required": ["doc", "edits"],
        },
    },
    {
        "name": "read_doc_xml",
        "description": (
            "Documents are zips of XML. No part: the part map. With part: "
            "that part's exact XML text. Pair with set_doc_xml for ANY edit "
            "(colors, fonts, slides). Guide: doc_guide('edit.xml')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PATH,
                "part": {
                    "type": "string",
                    "description": "Part name to read. Omit to list all parts.",
                },
            },
            "required": ["doc"],
        },
    },
    {
        "name": "set_doc_xml",
        "description": (
            "Patch (find/replace), CREATE (xml + content_type) or DELETE one "
            "XML part. Well-formed-or-nothing; byte-preserving. The universal "
            "edit — recolor, fonts, add/remove slides. Recipes: "
            "doc_guide('recipes.slides'), doc_guide('recipes.colors')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PATH,
                "part": {
                    "type": "string",
                    "description": "Part, e.g. ppt/charts/chart1.xml.",
                },
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {"type": "string"},
                            "replace": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                        "required": ["find", "replace"],
                    },
                    "description": "Exact-substring edits (mode: edits|xml|delete).",
                },
                "xml": {
                    "type": "string",
                    "description": "Full part XML — replaces, or creates if missing.",
                },
                "content_type": {
                    "type": "string",
                    "description": "Content-type Override for a newly created part.",
                },
                "delete": {
                    "type": "boolean",
                    "description": "Remove the part (patch referencing rels too).",
                },
                "output": {
                    "type": "string",
                    "description": "Output path (default: <input>_edited.<ext>).",
                },
            },
            "required": ["doc", "part"],
        },
    },
    {
        "name": "build_doc",
        "description": (
            "GENERATE (deterministic): build a NEW document from YOUR spec — "
            ".docx←markdown, .xlsx←{sheets}, .pptx←{slides} + optional "
            "`theme` (bg/accent colors, rail, page numbers → a DESIGNED deck "
            "in one call, incl. stat/quote/comparison layouts). Instant, no "
            "key. Spec shapes: doc_guide('build')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": ["string", "object"],
                    "description": (
                        "docx: markdown string. xlsx: {sheets:[...]}. "
                        "pptx: {slides:[...]}. Must match the output format."
                    ),
                },
                "output": {
                    "type": "string",
                    "description": "Output path — extension selects the engine.",
                },
                "lang": {"type": "string", "description": "BCP-47, default en-US."},
            },
            "required": ["spec", "output"],
        },
    },
    {
        "name": "generate_doc",
        "description": (
            "GENERATE (LLM): a complete designed document from a one-line "
            "intent (Anthropic key; .pptx is slow — minutes). Options: "
            "doc_guide('generate'). Keyless alternative: build_doc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "What the document is for.",
                },
                "output": {
                    "type": "string",
                    "description": "Output path — extension selects the engine.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional grounding documents (paths).",
                },
                "template": {
                    "type": "string",
                    "description": "PPTX only: deck to inherit design from.",
                },
                "deck_mode": {
                    "type": "string",
                    "enum": ["new", "template_restyle", "template_extend"],
                    "description": "PPTX only (default new).",
                },
                "pages": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2,
                    "description": "PPTX only: [min, max] page count.",
                },
                "lang": {"type": "string", "description": "BCP-47, default en-US."},
            },
            "required": ["intent", "output"],
        },
    },
    {
        "name": "edit_doc",
        "description": (
            "EDIT (LLM): one natural-language edit turn (Anthropic key). "
            "Prefer the deterministic path: analyze_doc → set_doc_text / "
            "set_doc_xml. Guide: doc_guide('edit')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc": _DOC_PATH,
                "instruction": {"type": "string"},
                "output": {
                    "type": "string",
                    "description": "Output path (default: <input>_edited.<ext>).",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference documents for this edit.",
                },
                "lang": {"type": "string", "description": "BCP-47, default en-US."},
            },
            "required": ["doc", "instruction"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in ANTHROPIC_TOOLS]

# The same set in OpenAI function-calling format — every backend gets the
# identical hierarchy, names and compact descriptions.
OPENAI_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in ANTHROPIC_TOOLS
]


def tool_specs(fmt: str = "anthropic") -> list[dict[str, Any]]:
    """The tool list for a backend: ``fmt`` = ``anthropic`` | ``openai``."""
    fmt = (fmt or "anthropic").strip().lower()
    if fmt == "anthropic":
        return ANTHROPIC_TOOLS
    if fmt == "openai":
        return OPENAI_TOOLS
    raise ValueError(f"unknown tool-spec format: {fmt!r} (anthropic | openai)")


async def run_tool_async(
    name: str, tool_input: dict[str, Any], *, api_key: str | None = None
) -> dict[str, Any]:
    """Dispatch one tool call; returns a JSON-safe result dict."""
    from . import simple

    args = dict(tool_input)
    if name == "doc_guide":
        from .agent_guide import doc_guide

        return doc_guide(args.get("topic"))
    if name == "generate_doc":
        pages_raw = args.pop("pages", None)
        pages = tuple(pages_raw) if pages_raw and len(pages_raw) == 2 else (8, 12)
        result = await simple.async_generate_doc(
            args.pop("intent"),
            output=args.pop("output"),
            api_key=api_key,
            sources=args.pop("sources", None),
            template=args.pop("template", None),
            deck_mode=args.pop("deck_mode", "new") or "new",
            pages=pages,  # type: ignore[arg-type]
            lang=args.pop("lang", "en-US") or "en-US",
        )
        return {
            "path": str(result.path),
            "page_count": result.page_count,
            "warnings": result.warnings,
        }
    if name == "edit_doc":
        result = await simple.async_edit_doc(
            args.pop("doc"),
            args.pop("instruction"),
            output=args.pop("output", None),
            api_key=api_key,
            sources=args.pop("sources", None),
            lang=args.pop("lang", "en-US"),
        )
        return {
            "path": str(result.path),
            "changed": result.changed,
            "reply": result.reply,
            "operations": result.operations,
        }
    if name == "render_doc":
        result = simple.render_doc(
            args["doc"],
            to=args.get("to", "png"),
            out_dir=args.get("out_dir"),
            dpi=float(args.get("dpi", 144.0)),
        )
        return {
            "paths": [str(p) for p in result.paths],
            "page_count": result.page_count,
            "format": result.format,
            "to": result.to,
        }
    if name == "set_doc_text":
        # One structured-edit surface: dicts with a `chart` key go to the
        # chart engine, the rest to the text engine, chained on one output.
        all_edits = list(args["edits"])
        text_edits = [e for e in all_edits if "chart" not in e]
        chart_edits = [e for e in all_edits if "chart" in e]
        output = args.get("output")
        path, applied, results = args["doc"], 0, []
        if text_edits:
            r = simple.set_doc_text(path, text_edits, output=output)
            path, applied = str(r.path), applied + r.applied
            results.extend(r.results)
        if chart_edits:
            # Continue on the text-edit output when chaining; otherwise let
            # edit_chart use its own default (<input>_chart.<ext>).
            chart_out = output if output is not None else (
                path if path != str(args["doc"]) else None
            )
            r = simple.edit_chart(path, chart_edits, output=chart_out)
            path, applied = str(r.path), applied + r.applied
            results.extend(r.results)
        return {"path": path, "applied": applied, "results": results}
    if name == "analyze_doc":
        return simple.analyze_doc(args["doc"])
    if name == "read_doc_xml":
        part = args.get("part")
        if not part:
            return {"parts": simple.list_doc_parts(args["doc"])}
        return {"part": part, "xml": simple.get_doc_xml(args["doc"], part)}
    if name == "set_doc_xml":
        result = simple.set_doc_xml(
            args["doc"],
            args["part"],
            args.get("edits"),
            xml=args.get("xml"),
            content_type=args.get("content_type"),
            delete=bool(args.get("delete")),
            output=args.get("output"),
        )
        return {
            "path": str(result.path),
            "applied": result.applied,
            "results": result.results,
        }
    if name == "build_doc":
        result = simple.build_doc(
            args["spec"], args["output"], lang=args.get("lang")
        )
        return {
            "path": str(result.path),
            "page_count": result.page_count,
            "warnings": result.warnings,
        }
    raise ValueError(f"unknown xgen_edit2docs tool: {name!r} (known: {TOOL_NAMES})")


def run_tool(
    name: str, tool_input: dict[str, Any], *, api_key: str | None = None
) -> dict[str, Any]:
    """Sync wrapper for :func:`run_tool_async`."""
    return asyncio.run(run_tool_async(name, tool_input, api_key=api_key))
