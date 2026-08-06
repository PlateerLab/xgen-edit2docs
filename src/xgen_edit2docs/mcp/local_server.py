"""Zero-infra MCP server: xgen_edit2docs tools over LOCAL files, stdio transport.

Needs nothing but the pip package — tools read and write .docx/.xlsx/.pptx
files on the local filesystem directly:

    { "mcpServers": { "xgen_edit2docs": {
        "command": "xgen_edit2docs-mcp",
        "env": {"ANTHROPIC_API_KEY": "sk-ant-..."}
    } } }

The tool set is hierarchical with progressive disclosure: compact
descriptions up front (pulled from ``agent_tools.ANTHROPIC_TOOLS`` — one
source of truth across every backend), the GENERATE|EDIT family map behind
``doc_guide()``, deep per-task guides behind ``doc_guide(topic)``.
``generate_doc`` / ``edit_doc`` take the key from ``api_key`` or the
``ANTHROPIC_API_KEY`` env var; the other seven tools are deterministic and
keyless.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..agent_tools import ANTHROPIC_TOOLS

# One source of truth for tool descriptions across all surfaces.
_DESC = {t["name"]: t["description"] for t in ANTHROPIC_TOOLS}


def build_local_mcp_server() -> FastMCP:
    mcp = FastMCP(
        "xgen_edit2docs",
        instructions=(
            "Office documents (.docx/.xlsx/.pptx) as deterministic tools. "
            "Two families: GENERATE (build_doc/generate_doc) and EDIT "
            "(analyze_doc first, then set_doc_text / set_doc_xml / "
            "edit_doc). Call doc_guide() for the map and doc_guide(topic) "
            "for shapes and recipes."
        ),
    )

    from .. import simple

    @mcp.tool(name="doc_guide", description=_DESC["doc_guide"])
    async def doc_guide_tool(topic: str | None = None) -> dict[str, Any]:
        from ..agent_guide import doc_guide

        return doc_guide(topic)

    @mcp.tool(name="generate_doc", description=_DESC["generate_doc"])
    async def generate_doc_tool(
        intent: str,
        output: str,
        sources: list[str] | None = None,
        template: str | None = None,
        deck_mode: str = "new",
        pages: tuple[int, int] = (8, 12),
        lang: str = "en-US",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        result = await simple.async_generate_doc(
            intent, output=output, api_key=api_key, sources=sources,
            template=template, deck_mode=deck_mode, pages=pages, lang=lang,
        )
        return {
            "path": str(result.path),
            "page_count": result.page_count,
            "warnings": result.warnings,
        }

    @mcp.tool(name="edit_doc", description=_DESC["edit_doc"])
    async def edit_doc_tool(
        doc: str,
        instruction: str,
        output: str | None = None,
        sources: list[str] | None = None,
        lang: str = "en-US",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        result = await simple.async_edit_doc(
            doc, instruction, output=output, api_key=api_key,
            sources=sources, lang=lang,
        )
        return {
            "path": str(result.path),
            "changed": result.changed,
            "reply": result.reply,
            "operations": result.operations,
        }

    @mcp.tool(name="render_doc", description=_DESC["render_doc"])
    async def render_doc_tool(
        doc: str,
        to: str = "png",
        out_dir: str | None = None,
        dpi: float = 144.0,
    ) -> dict[str, Any]:
        result = simple.render_doc(doc, to=to, out_dir=out_dir, dpi=dpi)
        return {
            "paths": [str(p) for p in result.paths],
            "page_count": result.page_count,
            "format": result.format,
            "to": result.to,
        }

    @mcp.tool(name="set_doc_text", description=_DESC["set_doc_text"])
    async def set_doc_text_tool(
        doc: str,
        edits: list[dict],
        output: str | None = None,
    ) -> dict[str, Any]:
        from ..agent_tools import run_tool_async

        return await run_tool_async(
            "set_doc_text", {"doc": doc, "edits": edits, "output": output}
        )

    @mcp.tool(name="analyze_doc", description=_DESC["analyze_doc"])
    async def analyze_doc_tool(doc: str) -> dict[str, Any]:
        return simple.analyze_doc(doc)

    @mcp.tool(name="arrange_doc", description=_DESC["arrange_doc"])
    async def arrange_doc_tool(
        doc: str,
        ops: list[dict],
        output: str | None = None,
    ) -> dict[str, Any]:
        from ..agent_tools import run_tool_async

        return await run_tool_async(
            "arrange_doc", {"doc": doc, "ops": ops, "output": output}
        )

    @mcp.tool(name="build_doc", description=_DESC["build_doc"])
    async def build_doc_tool(
        spec: str | dict[str, Any],
        output: str,
        lang: str | None = None,
    ) -> dict[str, Any]:
        result = simple.build_doc(spec, output, lang=lang)
        return {
            "path": str(result.path),
            "page_count": result.page_count,
            "warnings": result.warnings,
        }

    @mcp.tool(name="read_doc_xml", description=_DESC["read_doc_xml"])
    async def read_doc_xml_tool(
        doc: str, part: str | None = None
    ) -> dict[str, Any]:
        if not part:
            return {"parts": simple.list_doc_parts(doc)}
        return {"part": part, "xml": simple.get_doc_xml(doc, part)}

    @mcp.tool(name="set_doc_xml", description=_DESC["set_doc_xml"])
    async def set_doc_xml_tool(
        doc: str,
        part: str,
        edits: list[dict] | None = None,
        xml: str | None = None,
        content_type: str | None = None,
        delete: bool = False,
        output: str | None = None,
    ) -> dict[str, Any]:
        result = simple.set_doc_xml(
            doc, part, edits,
            xml=xml, content_type=content_type, delete=delete, output=output,
        )
        return {
            "path": str(result.path),
            "applied": result.applied,
            "results": result.results,
        }

    return mcp
