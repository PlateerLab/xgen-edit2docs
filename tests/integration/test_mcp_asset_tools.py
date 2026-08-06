"""Integration tests for the M4.2 MCP asset tools.

The MCP server is constructed in-process with a SQLite + InMemoryStorage
MCPContext, and tools are invoked through FastMCP's built-in call_tool().
This proves the full plumbing:
- MCPContext.scope() commits per-call
- AssetError surfaces as a tool error
- Korean filenames round-trip through upload_source -> get_asset -> download_url
"""

from __future__ import annotations

import base64
import urllib.parse
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from xgen_edit2docs.db.models import Base
from xgen_edit2docs.mcp import build_mcp_server
from xgen_edit2docs.mcp.context import MCPContext
from xgen_edit2docs.storage import InMemoryStorage


@pytest_asyncio.fixture
async def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest_asyncio.fixture
async def context(storage: InMemoryStorage):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield MCPContext(sessionmaker=sessionmaker, storage=storage)
    await engine.dispose()


@pytest_asyncio.fixture
async def server(context: MCPContext):
    return build_mcp_server(context=context)


# ---------------------------------------------------------------------------
# upload_source
# ---------------------------------------------------------------------------


class TestUploadSource:
    @pytest.mark.asyncio
    async def test_korean_filename_inline_upload(self, server, storage):
        content = b"PDF DATA"
        contents, structured = await server.call_tool(
            "upload_source",
            {
                "filename": "Q3 영업보고서.pdf",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "mime_type": "application/pdf",
            },
        )
        assert structured["original_filename"] == "Q3 영업보고서.pdf"
        assert structured["mime_type"] == "application/pdf"
        assert structured["size"] == len(content)
        # Storage key is ASCII (Track A).
        structured["storage_key"].encode("ascii")
        # Object actually landed in storage.
        assert await storage.get_bytes(structured["storage_key"]) == content

    @pytest.mark.asyncio
    async def test_invalid_base64_surfaces_error(self, server):
        with pytest.raises(Exception, match="base64"):
            await server.call_tool(
                "upload_source",
                {
                    "filename": "x.bin",
                    "content_base64": "not-valid-base64!!",
                },
            )

    @pytest.mark.asyncio
    async def test_invalid_kind_surfaces_error(self, server):
        with pytest.raises(Exception, match="Unknown asset kind"):
            await server.call_tool(
                "upload_source",
                {
                    "filename": "x.txt",
                    "content_base64": base64.b64encode(b"x").decode("ascii"),
                    "kind": "totally-bogus-kind",
                },
            )


# ---------------------------------------------------------------------------
# request_upload_url
# ---------------------------------------------------------------------------


class TestRequestUploadUrl:
    @pytest.mark.asyncio
    async def test_returns_presigned_put_url(self, server):
        contents, structured = await server.call_tool(
            "request_upload_url",
            {
                "filename": "원본자료.pdf",
                "mime_type": "application/pdf",
            },
        )
        assert structured["upload_url"].startswith("memory://upload/")
        structured["storage_key"].encode("ascii")
        # Asset row exists (size=0 placeholder until the PUT completes).
        get_meta = await server.call_tool("get_asset", {"asset_id": structured["asset_id"]})
        assert get_meta[1]["original_filename"] == "원본자료.pdf"

    @pytest.mark.asyncio
    async def test_rejects_short_or_long_expiry(self, server):
        with pytest.raises(Exception):
            await server.call_tool(
                "request_upload_url",
                {"filename": "x.bin", "expires_in_seconds": 1},
            )
        with pytest.raises(Exception):
            await server.call_tool(
                "request_upload_url",
                {"filename": "x.bin", "expires_in_seconds": 999999},
            )


# ---------------------------------------------------------------------------
# get_asset
# ---------------------------------------------------------------------------


class TestGetAsset:
    @pytest.mark.asyncio
    async def test_unknown_asset_id_errors(self, server):
        unknown = str(uuid.uuid4())
        with pytest.raises(Exception, match="not found"):
            await server.call_tool("get_asset", {"asset_id": unknown})

    @pytest.mark.asyncio
    async def test_invalid_uuid_errors(self, server):
        with pytest.raises(Exception, match="UUID"):
            await server.call_tool("get_asset", {"asset_id": "not-a-uuid"})


# ---------------------------------------------------------------------------
# download_url (Korean filename roundtrip via Content-Disposition)
# ---------------------------------------------------------------------------


class TestDownloadUrl:
    @pytest.mark.asyncio
    async def test_korean_filename_in_content_disposition(self, server):
        upload_resp = await server.call_tool(
            "upload_source",
            {
                "filename": "Q3 영업보고서.pdf",
                "content_base64": base64.b64encode(b"PDF").decode("ascii"),
                "mime_type": "application/pdf",
            },
        )
        asset_id = upload_resp[1]["asset_id"]

        dl_contents, dl_structured = await server.call_tool(
            "download_url", {"asset_id": asset_id, "expires_in_seconds": 60}
        )
        assert dl_structured["filename"] == "Q3 영업보고서.pdf"
        parsed = urllib.parse.urlparse(dl_structured["download_url"])
        params = urllib.parse.parse_qs(parsed.query)
        disposition = params["X-Test-Content-Disposition"][0]
        rfc = disposition.split("filename*=UTF-8''", 1)[1]
        assert urllib.parse.unquote(rfc) == "Q3 영업보고서.pdf"

    @pytest.mark.asyncio
    async def test_rejects_invalid_expiry(self, server):
        upload_resp = await server.call_tool(
            "upload_source",
            {
                "filename": "x.txt",
                "content_base64": base64.b64encode(b"x").decode("ascii"),
            },
        )
        asset_id = upload_resp[1]["asset_id"]
        with pytest.raises(Exception):
            await server.call_tool(
                "download_url", {"asset_id": asset_id, "expires_in_seconds": 1}
            )


# ---------------------------------------------------------------------------
# Tool listing — make sure the new tools show up
# ---------------------------------------------------------------------------


class TestToolListing:
    @pytest.mark.asyncio
    async def test_asset_tools_advertised(self, server):
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert {
            "upload_source",
            "request_upload_url",
            "get_asset",
            "download_url",
        } <= names
