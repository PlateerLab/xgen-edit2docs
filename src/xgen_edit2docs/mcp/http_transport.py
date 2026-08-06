"""Mount the MCP server on the FastAPI app over HTTP transports.

Two transports are exposed so MCP clients with different specs can connect:

- /mcp                        Streamable HTTP transport (MCP spec 2025-03-26)
                              Recommended for new clients (Claude Desktop
                              recent versions, Cursor recent versions).
- /mcp/sse and /mcp/messages  Server-Sent Events transport (MCP spec 2024-11-05)
                              Kept for older clients.

Both surfaces wrap the same FastMCP server (built by `build_mcp_server()`)
so they expose identical tools and contexts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from .context import MCPContext
from .server import build_mcp_server


def mount_mcp(app: "FastAPI", *, context: MCPContext | None = None) -> None:
    """Mount Streamable HTTP + SSE MCP endpoints on *app*.

    Idempotent in the sense that calling it twice will mount twice — callers
    should invoke it exactly once during startup.
    """
    mcp = build_mcp_server(context=context)

    # Streamable HTTP (modern transport). Mount at /mcp so Claude Desktop's
    # `{"url": "https://.../mcp"}` config Just Works.
    app.mount("/mcp", mcp.streamable_http_app())

    # Legacy SSE transport. Two sub-routes:
    #   GET  /mcp/sse        — establishes the SSE stream
    #   POST /mcp/messages   — agent posts JSON-RPC messages here
    # FastMCP's sse_app handles both internally.
    app.mount("/mcp-sse", mcp.sse_app())
