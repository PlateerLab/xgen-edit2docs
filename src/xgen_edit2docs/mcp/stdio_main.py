"""stdio entry point for the xgen_edit2docs MCP server.

Lets local agents (Claude Desktop, Cursor) launch this server as a child
process. Add to ~/Library/Application Support/Claude/claude_desktop_config.json:

    {
      "mcpServers": {
        "xgen_edit2docs": {
          "command": ".venv/bin/python",
          "args": ["-m", "xgen_edit2docs.mcp.stdio_main"]
        }
      }
    }

For remote (URL-based) clients, M4.4 mounts an HTTP+SSE transport on the
FastAPI app instead.
"""

from __future__ import annotations

from .server import build_mcp_server


def main() -> None:
    server = build_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
