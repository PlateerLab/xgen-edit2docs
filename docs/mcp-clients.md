# Connecting MCP clients to xgen_edit2docs

The xgen_edit2docs server exposes its tools through two MCP transports:

| Path | Transport | MCP spec | When to use |
|------|-----------|----------|-------------|
| `/mcp` | Streamable HTTP | 2025-03-26+ | Default for new clients (Claude Desktop ≥ April 2026, Cursor recent, custom MCP clients) |
| `/mcp-sse` | Server-Sent Events | 2024-11-05 | Older clients that haven't migrated to Streamable HTTP yet |
| stdio | child-process | both | Local-only setups where the agent spawns the server as a subprocess |

Tools advertised at every transport are identical:

- `hello` — health check + service identity
- `list_templates` — discover layout templates
- `list_voices` — discover narration voices (Korean defaults included)
- `upload_source` — inline base64 upload (small files)
- `request_upload_url` — presigned PUT URL (large files)
- `get_asset` — asset metadata lookup
- `download_url` — presigned download URL (Korean filename preserved)
- `generate_deck` — Strategist + Executor + Export, with progress notifications

## Local stdio (Claude Desktop / Cursor)

Use this when you run the xgen_edit2docs server on the same machine as the agent.
The agent launches the server as a subprocess; no network is involved.

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS, `%APPDATA%/Claude/claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "xgen_edit2docs": {
      "command": "/path/to/xgen_edit2docs/.venv/bin/python",
      "args": ["-m", "xgen_edit2docs.mcp.stdio_main"],
      "env": {
        "EDIT2DOCS_DATABASE_URL": "postgresql+asyncpg://xgen_edit2docs:xgen_edit2docs@localhost:5432/xgen_edit2docs",
        "EDIT2DOCS_S3_ENDPOINT_URL": "http://localhost:9000",
        "EDIT2DOCS_S3_BUCKET": "xgen_edit2docs-local",
        "EDIT2DOCS_S3_ACCESS_KEY_ID": "xgen_edit2docs",
        "EDIT2DOCS_S3_SECRET_ACCESS_KEY": "xgen_edit2docs-local-secret"
      }
    }
  }
}
```

Restart Claude Desktop. The `xgen_edit2docs` server should appear in the
**Connectors** panel; tools like `generate_deck` show up under it.

**Cursor** — `~/.cursor/mcp.json` follows the same `mcpServers` schema.

**MCP Inspector** (for debugging):

```bash
npx -y @modelcontextprotocol/inspector \
  /path/to/xgen_edit2docs/.venv/bin/python -m xgen_edit2docs.mcp.stdio_main
```

## Remote HTTP (Streamable HTTP)

Use this when xgen_edit2docs runs on a server and the agent connects over the
network. This is the recommended path for production deployments.

```json
{
  "mcpServers": {
    "xgen_edit2docs": {
      "url": "https://xgen_edit2docs.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ek_live_..."
      }
    }
  }
}
```

Notes:
- The trailing `/mcp` matters; Streamable HTTP listens at the mount root.
- For local dev against `docker compose up`, the URL is
  `http://localhost:8000/mcp`. The DNS-rebinding guard in FastMCP accepts
  `localhost` and `127.0.0.1` by default.
- Auth header is forwarded to the underlying REST API endpoints once M6
  ships proper tenant key validation; until then the dev API key from
  `EDIT2DOCS_AUTH_DEV_API_KEY` is honored.

## Remote HTTP (SSE — legacy)

For older Claude Desktop or Cursor versions that don't yet support
Streamable HTTP:

```json
{
  "mcpServers": {
    "xgen_edit2docs": {
      "url": "https://xgen_edit2docs.example.com/mcp-sse/sse"
    }
  }
}
```

The SSE transport exposes two routes:
- `GET /mcp-sse/sse` — opens the SSE stream
- `POST /mcp-sse/messages` — the client posts JSON-RPC frames here

The client picks them up from MCP's negotiation handshake automatically.

## Verifying the connection

After registering xgen_edit2docs, ask the agent something like:

> "Use xgen_edit2docs to list available templates."

The agent should call `list_templates` and surface the catalog. If you
provide your own Anthropic API key, you can then ask:

> "Make a Korean PowerPoint about Q3 sales from this PDF" (with the file
> attached).

The agent should:
1. Call `upload_source` to register the PDF
2. Call `generate_deck` with `lang="ko-KR"` and your `anthropic_api_key`
3. Stream stage progress (you'll see "Generating page 3/10…" etc.)
4. Call `download_url` to give you a link to the resulting `.pptx`

The downloaded file preserves your original Korean filename — that's the
G13 + Track A/C round-trip in action.

## Troubleshooting

- **Tools don't appear in Claude Desktop** — check the server logs at
  `~/Library/Logs/Claude/mcp-server-xgen_edit2docs.log` (macOS) or
  `%APPDATA%/Claude/Logs/mcp-server-xgen_edit2docs.log` (Windows). Most failures
  are missing env vars or unreachable Postgres / Redis / MinIO.
- **`Request validation failed`** — FastMCP rejects requests whose `Host`
  header isn't on its allow-list. For dev, use `localhost:*` or `127.0.0.1:*`.
  For prod, set `EDIT2DOCS_MCP_ALLOWED_HOSTS` (M6 wires this up).
- **`anthropic_api_key is required`** — `generate_deck` is BYOK; pass your
  own key on each call.
- **Korean filename mojibake** — confirm the response carries
  `Content-Disposition: attachment; filename*=UTF-8''…` and the agent UI
  honors RFC 5987 (Claude Desktop and Cursor both do).
