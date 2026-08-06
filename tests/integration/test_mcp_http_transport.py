"""Smoke tests for the MCP HTTP transports mounted on the FastAPI app.

A full MCP protocol exchange (initialize → tools/list → tools/call) requires
a real MCP HTTP client. Those tests live alongside the M5 milestone once we
wire the official mcp-client into CI. For M4.4 we verify the lighter property:
the routes are mounted (FastAPI advertises them) and a basic protocol probe
doesn't crash.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from xgen_edit2docs.api.main import app


class TestRoutesMounted:
    def test_mcp_streamable_route_in_app(self):
        """The Streamable HTTP transport is mounted under /mcp."""
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        # /mcp is mounted as a sub-app, so the FastAPI route table shows the
        # mount prefix itself.
        assert any(p.startswith("/mcp") and "sse" not in p for p in paths), (
            f"/mcp Streamable HTTP not mounted. Routes: {sorted(paths)}"
        )

    def test_mcp_sse_route_in_app(self):
        """The legacy SSE transport is mounted under /mcp-sse."""
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert any(p.startswith("/mcp-sse") for p in paths), (
            f"/mcp-sse not mounted. Routes: {sorted(paths)}"
        )


@pytest_asyncio.fixture
async def client():
    """httpx.AsyncClient configured with a host the MCP DNS-rebinding guard
    accepts (matches the FastMCP `allowed_hosts` glob: 127.0.0.1:*)."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8000",
        timeout=httpx.Timeout(5.0),
    ) as ac:
        yield ac


class TestStreamableHttpProtocol:
    """Smoke-tests against the Streamable HTTP endpoint with malformed input
    so we don't have to mock a full MCP client. The endpoint must respond
    (rather than crash) for any of these probes."""

    @pytest.mark.asyncio
    async def test_get_without_session_does_not_crash(self, client: httpx.AsyncClient):
        # MCP Streamable HTTP returns 400/404/405/406 for bare GETs without a
        # session id; the mount on /mcp may also issue a 307 to /mcp/.
        # Either way the response must make it back without the server crashing.
        resp = await client.get("/mcp", follow_redirects=True)
        assert resp.status_code in (200, 400, 404, 405, 406, 307), (
            f"unexpected status {resp.status_code}: {resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_post_with_empty_body_is_rejected_cleanly(self, client: httpx.AsyncClient):
        resp = await client.post("/mcp", content=b"", follow_redirects=True)
        # MCP returns 400 / 404 / 406 / 415 / 422 / 200 depending on whether the
        # post hits the protocol handler or the mount root. The important
        # property: the server doesn't crash and the response makes it back.
        assert resp.status_code < 500
