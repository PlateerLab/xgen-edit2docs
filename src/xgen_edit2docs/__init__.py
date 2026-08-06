"""xgen_edit2docs — AI-agent-native document engine. English-first, first-class Korean support.

Four ways to use it:

* **Library** — ``from xgen_edit2docs import generate_doc, edit_doc, ...``
  (zero infra, works on local files)
* **Agent tools** — ``from xgen_edit2docs.agent_tools import ANTHROPIC_TOOLS,
  run_tool`` (function-calling schemas + dispatcher)
* **Local MCP** — ``xgen_edit2docs-mcp`` console script (stdio, zero infra)
* **Hosted service** — ``pip install "xgen_edit2docs[server]"`` then
  ``xgen_edit2docs serve`` (FastAPI + jobs + MCP over HTTP; powers the studio)

Exports resolve lazily so ``import xgen_edit2docs`` stays fast and the base
install never touches server-only dependencies.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.15.0"

_LAZY: dict[str, str] = {
    # Unified, extension-dispatched verbs (docx / xlsx / pptx)
    "generate_doc": ".simple",
    "build_doc": ".simple",
    "edit_doc": ".simple",
    "preview_doc": ".simple",
    "set_doc_text": ".simple",
    "edit_chart": ".simple",
    "list_doc_parts": ".simple",
    "get_doc_xml": ".simple",
    "set_doc_xml": ".simple",
    "list_charts": ".simple",
    "analyze_doc": ".simple",
    "render_doc": ".simple",
    "RenderResult": ".simple",
    "async_generate_doc": ".simple",
    "async_edit_doc": ".simple",
    # PPTX-specific surface (full deck pipeline)
    "generate_pptx": ".simple",
    "edit_pptx": ".simple",
    "preview_pptx": ".simple",
    "set_pptx_text": ".simple",
    "analyze_pptx": ".simple",
    "async_generate_pptx": ".simple",
    "async_edit_pptx": ".simple",
    "GenerateResult": ".simple",
    "EditResult": ".simple",
    "TextEditsResult": ".simple",
    # Agent tool surface (hierarchical, progressive-disclosure)
    "ANTHROPIC_TOOLS": ".agent_tools",
    "OPENAI_TOOLS": ".agent_tools",
    "TOOL_NAMES": ".agent_tools",
    "tool_specs": ".agent_tools",
    "run_tool": ".agent_tools",
    "run_tool_async": ".agent_tools",
    "doc_guide": ".agent_guide",
}

__all__ = ["__version__", *sorted(_LAZY)]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache for subsequent lookups
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
