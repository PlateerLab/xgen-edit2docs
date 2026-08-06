"""Bilingual message catalog for xgen_edit2docs (English-first, full Korean).

Loads YAML message files under i18n/messages/ and looks up messages by
dot-separated key + locale. Used by FastAPI exception handlers and MCP tool
descriptions to produce localized user-facing strings.

Design: pure-Python, no global mutable state at import. The default catalog
is a module-level singleton constructed on first use; tests can build their
own MessageCatalog instances.

See ppt-master-analysis/06-bilingual-conventions.md.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Mapping

import yaml

_MESSAGES_DIR = Path(__file__).parent / "messages"

# Default locale + fallback chain. English-first: en-US is primary AND the
# universal fallback (every key must exist in en.yaml); ko.yaml is the
# complete Korean catalog.
DEFAULT_LOCALE = "en-US"
FALLBACK_LOCALE = "en-US"

# Locale code -> YAML filename (without extension)
_LOCALE_TO_FILE: dict[str, str] = {
    "ko-KR": "ko",
    "ko": "ko",
    "en-US": "en",
    "en": "en",
    # Future: zh-CN -> zh, ja-JP -> ja, etc.
}


class MessageCatalog:
    """Lookup table for localized message strings."""

    def __init__(self, messages_by_locale: Mapping[str, Mapping[str, Any]]):
        self._messages_by_locale = dict(messages_by_locale)

    @classmethod
    def load(cls, messages_dir: Path = _MESSAGES_DIR) -> "MessageCatalog":
        loaded: dict[str, Mapping[str, Any]] = {}
        for yaml_file in messages_dir.glob("*.yaml"):
            with yaml_file.open(encoding="utf-8") as fh:
                loaded[yaml_file.stem] = yaml.safe_load(fh) or {}
        return cls(loaded)

    def supported_locales(self) -> list[str]:
        """Return canonical BCP-47 locales (with region) backed by a loaded file.

        Excludes 2-letter aliases like 'ko' so callers always see the canonical
        'ko-KR' form. The aliases are still accepted as inputs by normalize_locale.
        """
        files = set(self._messages_by_locale.keys())
        return [
            code
            for code, file_stem in _LOCALE_TO_FILE.items()
            if file_stem in files and "-" in code
        ]

    def get(self, key: str, locale: str = DEFAULT_LOCALE, **vars: Any) -> str:
        """Look up *key* for *locale*, format with *vars*, fall back to en-US.

        Raises KeyError if the key does not exist in any locale (catches typos
        early).
        """
        file_stem = _LOCALE_TO_FILE.get(locale) or _LOCALE_TO_FILE.get(
            locale.split("-")[0], None
        )
        for stem in (file_stem, _LOCALE_TO_FILE[FALLBACK_LOCALE]):
            if stem is None or stem not in self._messages_by_locale:
                continue
            template = self._dig(self._messages_by_locale[stem], key)
            if template is not None:
                return self._format(template, vars)
        raise KeyError(f"i18n key not found in any locale: {key!r}")

    def get_pair(self, key: str, **vars: Any) -> dict[str, str]:
        """Return both Korean and English renderings (used in API error bodies)."""
        return {
            "ko": self.get(key, "ko-KR", **vars),
            "en": self.get(key, "en-US", **vars),
        }

    @staticmethod
    def _dig(d: Mapping[str, Any], dotted_key: str) -> Any:
        node: Any = d
        for part in dotted_key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    @staticmethod
    def _format(template: str, vars: Mapping[str, Any]) -> str:
        if not vars:
            return template
        try:
            return template.format(**vars)
        except (KeyError, IndexError):
            # If a placeholder is missing, return the raw template rather than
            # crashing the user request.
            return template


@functools.lru_cache(maxsize=1)
def default_catalog() -> MessageCatalog:
    """Process-wide singleton, loaded lazily."""
    return MessageCatalog.load()


def t(key: str, locale: str = DEFAULT_LOCALE, **vars: Any) -> str:
    """Shorthand: translate *key* for *locale* using the default catalog."""
    return default_catalog().get(key, locale, **vars)


def normalize_locale(value: str | None, default: str = DEFAULT_LOCALE) -> str:
    """Best-effort BCP-47 normalization.

    Accepts 'ko', 'ko-KR', 'ko_kr', 'KO-kr', or None. Always returns one of the
    catalog's supported locales, or *default* if nothing matches.
    """
    if not value:
        return default
    # Normalize separator and case: ko_KR / ko-KR / ko-kr -> ko-KR.
    cleaned = value.replace("_", "-").strip()
    if "-" in cleaned:
        lang, region = cleaned.split("-", 1)
        cleaned = f"{lang.lower()}-{region.upper()}"
    else:
        cleaned = cleaned.lower()
    catalog = default_catalog()
    supported = set(catalog.supported_locales())
    if cleaned in supported:
        return cleaned
    # Try 2-letter prefix.
    prefix = cleaned.split("-")[0]
    for code in supported:
        if code.startswith(prefix + "-"):
            return code
    return default
