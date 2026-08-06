"""Bilingual message catalog (en / ko) for xgen_edit2docs user-facing strings.

English-first: en-US is the default and fallback locale; ko-KR is a complete,
first-class translation (not a subset).

See ppt-master-analysis/06-bilingual-conventions.md for the policy:
- Track A (filesystem, code, DB, API path): English ASCII only
- Track B (UI, errors, MCP descriptions): bilingual ko + en via this catalog
- Track C (user content): preserved as-is
"""

from .catalog import (
    DEFAULT_LOCALE,
    FALLBACK_LOCALE,
    MessageCatalog,
    default_catalog,
    normalize_locale,
    t,
)

__all__ = [
    "DEFAULT_LOCALE",
    "FALLBACK_LOCALE",
    "MessageCatalog",
    "default_catalog",
    "normalize_locale",
    "t",
]
