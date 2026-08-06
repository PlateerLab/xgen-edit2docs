"""Console entry point for the zero-infra local MCP server.

    xgen_edit2docs-mcp                      # stdio transport (Claude Desktop 등)
    uvx --from xgen_edit2docs xgen_edit2docs-mcp  # no-install run

Claude Desktop / Cursor config:

    {
      "mcpServers": {
        "xgen_edit2docs": {
          "command": "xgen_edit2docs-mcp",
          "env": {"ANTHROPIC_API_KEY": "sk-ant-..."}
        }
      }
    }
"""

from __future__ import annotations

from .local_server import build_local_mcp_server


def main() -> None:
    build_local_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
