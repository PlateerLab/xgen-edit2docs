"""Unit tests for the i18n message catalog."""

from __future__ import annotations

import pytest

from xgen_edit2docs.i18n import (
    DEFAULT_LOCALE,
    FALLBACK_LOCALE,
    MessageCatalog,
    default_catalog,
    normalize_locale,
    t,
)


class TestCatalogLookup:
    def test_korean_error_message(self):
        msg = t("errors.invalid_source_format", "ko-KR", format="rtf", allowed="pdf, docx")
        assert "지원하지 않는" in msg
        assert "rtf" in msg

    def test_english_error_message(self):
        msg = t("errors.invalid_source_format", "en-US", format="rtf", allowed="pdf, docx")
        assert "Unsupported" in msg
        assert "rtf" in msg

    def test_stage_message_with_format(self):
        msg = t("stages.executing_page", "ko-KR", page=3, total=10)
        assert "3/10" in msg

    def test_unknown_locale_falls_back_to_english(self):
        msg = t("errors.unauthorized", "fr-FR")
        # Fallback chain ends at en-US.
        assert "API key" in msg or "invalid" in msg.lower()

    def test_short_locale_normalizes(self):
        # "ko" should resolve to "ko-KR".
        assert normalize_locale("ko") == "ko-KR"
        assert normalize_locale("en") == "en-US"

    def test_case_insensitive_normalize(self):
        assert normalize_locale("ko_kr") == "ko-KR"
        assert normalize_locale("KO-kr") == "ko-KR"

    def test_none_uses_default(self):
        assert normalize_locale(None) == DEFAULT_LOCALE

    def test_missing_key_raises(self):
        with pytest.raises(KeyError):
            t("nonexistent.key", "ko-KR")

    def test_get_pair_returns_both_locales(self):
        pair = default_catalog().get_pair("errors.forbidden")
        assert "ko" in pair and "en" in pair
        assert pair["ko"] != pair["en"]


class TestCatalogCoverage:
    """Every key present in ko.yaml must also be in en.yaml (en is the fallback)."""

    @pytest.fixture
    def catalog(self) -> MessageCatalog:
        return default_catalog()

    def test_all_ko_keys_have_en(self, catalog: MessageCatalog):
        def collect_keys(d, prefix=""):
            keys: list[str] = []
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    keys.extend(collect_keys(v, full))
                elif isinstance(v, str):
                    keys.append(full)
            return keys

        ko_keys = set(collect_keys(catalog._messages_by_locale["ko"]))  # noqa: SLF001
        en_keys = set(collect_keys(catalog._messages_by_locale["en"]))  # noqa: SLF001
        missing = ko_keys - en_keys
        assert not missing, f"Korean keys without English fallback: {sorted(missing)}"
