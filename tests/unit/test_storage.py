"""Unit tests for the object-storage layer.

We test against InMemoryStorage so the suite runs with no infrastructure.
The S3 adapter shares the same contract — integration tests against MinIO
land in a later milestone.
"""

from __future__ import annotations

import urllib.parse

import pytest

from xgen_edit2docs.storage import (
    InMemoryStorage,
    PresignedUrl,
    assert_ascii_key,
    build_content_disposition,
)


# ---------------------------------------------------------------------------
# assert_ascii_key — Track A enforcement
# ---------------------------------------------------------------------------


class TestAsciiKeyEnforcement:
    def test_ascii_key_accepted(self):
        assert_ascii_key("tenants/abc/sources/01h.pdf")  # no exception

    def test_korean_key_rejected(self):
        with pytest.raises(ValueError, match="ASCII"):
            assert_ascii_key("tenants/abc/sources/보고서.pdf")

    def test_empty_key_accepted(self):
        # Empty is a valid ASCII string; semantic validation lives elsewhere.
        assert_ascii_key("")


# ---------------------------------------------------------------------------
# Content-Disposition encoding (Korean filenames)
# ---------------------------------------------------------------------------


class TestContentDisposition:
    def test_ascii_filename(self):
        value = build_content_disposition("report.pdf")
        assert 'filename="report.pdf"' in value
        assert "filename*=UTF-8''report.pdf" in value
        assert value.startswith("attachment;")

    def test_korean_filename_round_trip(self):
        original = "Q3 영업보고서.pdf"
        value = build_content_disposition(original)
        # The ASCII fallback replaces each non-ASCII codepoint with '_'.
        non_ascii_count = sum(1 for ch in original if ord(ch) >= 128)
        expected_fallback = "".join("_" if ord(ch) >= 128 else ch for ch in original)
        assert non_ascii_count > 0
        assert f'filename="{expected_fallback}"' in value
        # The RFC 5987 segment percent-encodes the Korean characters.
        # Decode it back and compare to the original.
        rfc_segment = value.split("filename*=UTF-8''", 1)[1]
        decoded = urllib.parse.unquote(rfc_segment)
        assert decoded == original

    def test_inline_disposition(self):
        value = build_content_disposition("preview.png", disposition="inline")
        assert value.startswith("inline;")

    def test_invalid_disposition(self):
        with pytest.raises(ValueError):
            build_content_disposition("x", disposition="other")

    def test_quote_in_filename_escaped(self):
        value = build_content_disposition('weird"name.pdf')
        # ASCII fallback escapes the embedded quote so the header stays parseable.
        assert 'filename="weird\\"name.pdf"' in value


# ---------------------------------------------------------------------------
# InMemoryStorage — full ObjectStorage contract
# ---------------------------------------------------------------------------


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


class TestInMemoryStorage:
    @pytest.mark.asyncio
    async def test_put_then_get(self, storage: InMemoryStorage):
        await storage.put_bytes("tenants/x/file.txt", b"hello", content_type="text/plain")
        data = await storage.get_bytes("tenants/x/file.txt")
        assert data == b"hello"

    @pytest.mark.asyncio
    async def test_get_missing_raises_keyerror(self, storage: InMemoryStorage):
        with pytest.raises(KeyError):
            await storage.get_bytes("tenants/x/nope")

    @pytest.mark.asyncio
    async def test_overwrite(self, storage: InMemoryStorage):
        await storage.put_bytes("k", b"first")
        await storage.put_bytes("k", b"second")
        assert await storage.get_bytes("k") == b"second"

    @pytest.mark.asyncio
    async def test_delete_idempotent(self, storage: InMemoryStorage):
        await storage.put_bytes("k", b"x")
        await storage.delete("k")
        await storage.delete("k")  # second delete must not raise
        assert not await storage.exists("k")

    @pytest.mark.asyncio
    async def test_exists(self, storage: InMemoryStorage):
        assert not await storage.exists("k")
        await storage.put_bytes("k", b"x")
        assert await storage.exists("k")

    @pytest.mark.asyncio
    async def test_korean_key_rejected_on_put(self, storage: InMemoryStorage):
        with pytest.raises(ValueError, match="ASCII"):
            await storage.put_bytes("보고서.pdf", b"x")

    @pytest.mark.asyncio
    async def test_presigned_get_encodes_korean_filename(self, storage: InMemoryStorage):
        await storage.put_bytes("tenants/x/01h.pdf", b"x", content_type="application/pdf")
        url: PresignedUrl = await storage.presigned_get_url(
            "tenants/x/01h.pdf",
            expires_in_seconds=60,
            response_filename="Q3 영업보고서.pdf",
        )
        # The Korean filename round-trips through the URL query parameter.
        parsed = urllib.parse.urlparse(url.url)
        params = urllib.parse.parse_qs(parsed.query)
        disposition = params["X-Test-Content-Disposition"][0]
        # Decode RFC 5987.
        rfc_segment = disposition.split("filename*=UTF-8''", 1)[1]
        assert urllib.parse.unquote(rfc_segment) == "Q3 영업보고서.pdf"

    @pytest.mark.asyncio
    async def test_presigned_put_url(self, storage: InMemoryStorage):
        url = await storage.presigned_put_url(
            "tenants/x/upload.bin",
            expires_in_seconds=60,
            content_type="application/octet-stream",
        )
        assert url.expires_in_seconds == 60
        assert "upload" in url.url
