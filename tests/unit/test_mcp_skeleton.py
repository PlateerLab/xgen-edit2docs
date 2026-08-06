"""Unit tests for the M4.1 MCP server skeleton.

We exercise the server through MCP's in-memory ClientSession so the tools
are invoked end-to-end (request/response shape, tool listing) without
opening a real transport.
"""

from __future__ import annotations

import json

import pytest

from mcp.server.fastmcp import FastMCP

from xgen_edit2docs.mcp import build_mcp_server
from xgen_edit2docs.mcp.catalog import list_templates, list_voices


# ---------------------------------------------------------------------------
# Catalog (pure-function) tests — no MCP needed
# ---------------------------------------------------------------------------


class TestTemplateCatalog:
    def test_at_least_one_template(self):
        items = list_templates()
        assert len(items) >= 10  # we ship ~19 layouts
        for item in items:
            assert isinstance(item["name"], str)
            # Track A: all names are ASCII (G13 enforced).
            item["name"].encode("ascii")

    def test_chongqing_university_renamed(self):
        names = {item["name"] for item in list_templates()}
        # G13 verification: Chinese names are gone, English names are present.
        assert "重庆大学" not in names
        assert "chongqing_university" in names

    def test_korean_locale_smoke(self):
        # Locale doesn't change content yet (M5 will add per-locale summaries)
        # but the call should not error.
        items = list_templates(locale="ko-KR")
        assert items


class TestVoiceCatalog:
    def test_ko_kr_voices_exposed(self):
        voices = list_voices(lang="ko-KR")
        assert voices, "Korean voices must be in the catalog"
        ids = {v["voice_id"] for v in voices}
        assert "ko-KR-SunHiNeural" in ids
        # SunHi is the default — surface that to clients.
        sunhi = next(v for v in voices if v["voice_id"] == "ko-KR-SunHiNeural")
        assert sunhi["is_default_for_locale"] is True

    def test_filter_by_two_letter_prefix(self):
        voices = list_voices(lang="ko")
        assert all(v["locale"].startswith("ko-") for v in voices)

    def test_no_filter_returns_all(self):
        voices = list_voices()
        # Multi-locale: includes ko + zh + en at minimum.
        locales = {v["locale"] for v in voices}
        assert {"ko-KR", "en-US"} <= locales


# ---------------------------------------------------------------------------
# MCP server tool listing + direct invocation
# ---------------------------------------------------------------------------


@pytest.fixture
def server() -> FastMCP:
    return build_mcp_server()


class TestMCPServer:
    @pytest.mark.asyncio
    async def test_lists_expected_tools(self, server: FastMCP):
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert {"hello", "list_templates", "list_voices"} <= names

    @pytest.mark.asyncio
    async def test_hello_tool_returns_service_identity(self, server: FastMCP):
        result = await server.call_tool("hello", {})
        # FastMCP returns (content_list, structured_dict) in 1.x.
        contents, structured = result
        assert structured["service"] == "xgen_edit2docs"
        assert structured["ok"] is True
        assert "hello" in structured["tools"]

    @pytest.mark.asyncio
    async def test_list_templates_via_mcp(self, server: FastMCP):
        contents, structured = await server.call_tool("list_templates", {})
        items = structured["templates"]
        assert items
        assert all("name" in t and "keywords" in t for t in items)

    @pytest.mark.asyncio
    async def test_list_voices_with_filter(self, server: FastMCP):
        contents, structured = await server.call_tool("list_voices", {"lang": "ko-KR"})
        voices = structured["voices"]
        assert voices
        assert all(v["locale"].startswith("ko-") for v in voices)

    @pytest.mark.asyncio
    async def test_korean_metadata_in_voice_notes(self, server: FastMCP):
        """Voice 'notes' field carries human-readable descriptions; the
        Korean defaults from the G10 patch should be reflected here."""
        contents, structured = await server.call_tool("list_voices", {"lang": "ko-KR"})
        # SunHi note text should mention 'Korean'.
        sunhi = next(v for v in structured["voices"] if v["voice_id"] == "ko-KR-SunHiNeural")
        assert "Korean" in sunhi["notes"]
