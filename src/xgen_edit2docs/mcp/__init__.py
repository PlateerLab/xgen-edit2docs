"""MCP (Model Context Protocol) server for xgen_edit2docs.

Lets AI agents (Claude Desktop, Cursor, custom clients) call our HTTP API
through the standard MCP tool interface. The agent registers our server URL
once; we expose a handful of tools that internally hit the same routes the
REST API uses.

Build order:
  M4.1 — skeleton + read-only tools (list_templates, list_voices)
  M4.2 — asset tools (upload_source, get_asset, download_url)
  M4.3 — generate_deck with progress notifications
  M4.4 — HTTP+SSE transport mounted on the FastAPI app
"""

from .server import build_mcp_server

__all__ = ["build_mcp_server"]
