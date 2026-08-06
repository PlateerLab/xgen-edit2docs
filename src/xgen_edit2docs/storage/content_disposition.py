"""Build Content-Disposition headers that survive Korean filenames intact.

Modern user-agents (browsers, curl, gh, AI agents) read `filename*=UTF-8''<encoded>`
per RFC 5987 and save the file under that name. Older user-agents fall back to
the plain `filename="..."` parameter, which must be ASCII. We always include
both: a sanitized ASCII fallback for the boomer parsers, plus the encoded
Unicode version for everyone else.

Examples:
    >>> build_content_disposition("Q3 영업보고서.pdf")
    "attachment; filename=\"Q3 ____.pdf\"; filename*=UTF-8''Q3%20%EC%98%81..."
"""

from __future__ import annotations

import urllib.parse


def build_content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """Compose a Content-Disposition value with safe Korean-aware encoding.

    Args:
        filename: The user-facing filename (may contain any Unicode).
        disposition: Either "attachment" (download dialog) or "inline" (render
            in the browser when possible).

    Returns:
        A string suitable for assignment to a Content-Disposition header.
    """
    if disposition not in ("attachment", "inline"):
        raise ValueError(f"disposition must be 'attachment' or 'inline', got {disposition!r}")

    # ASCII fallback: replace every non-ASCII character with '_'. Keeps the
    # extension visible so user agents that only honor `filename=` still pick a
    # reasonable name.
    ascii_fallback = "".join(ch if ord(ch) < 128 else "_" for ch in filename)
    # Quote double quotes (the only character ASCII filenames must escape).
    ascii_fallback = ascii_fallback.replace('"', '\\"')

    # RFC 5987 encoding: percent-encode everything except unreserved chars.
    encoded = urllib.parse.quote(filename, safe="")

    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
