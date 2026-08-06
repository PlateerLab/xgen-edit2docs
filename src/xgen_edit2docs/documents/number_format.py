"""ECMA-376 number-format subset (native-render plan M4).

openpyxl returns raw values plus the cell's number-format *string*; it
never renders them. This module covers the formats that dominate real
spreadsheets — General, fixed decimals, thousands separators, percent,
currency prefixes (₩ $ € ¥ £), and common date/time patterns — so grid
previews show "12.5%" / "₩1,420,000" / "2026-07-05" instead of raw
floats and datetimes. Anything unrecognised falls back to a readable
General rendering (never raises).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

_CURRENCY_RE = re.compile(r"[₩$€¥£]")
_DECIMALS_RE = re.compile(r"0\.(0+)")
_DATE_TOKEN_RE = re.compile(r"(yyyy|yy|mm|m|dd|d|hh|h|ss|s)")

_DATE_TOKEN_MAP = {
    "yyyy": "%Y", "yy": "%y",
    "mm": "%m", "m": "%m",
    "dd": "%d", "d": "%d",
    "hh": "%H", "h": "%H",
    "ss": "%S", "s": "%S",
}


def _general(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def _strip_literals(fmt: str) -> str:
    """Drop quoted literals / color tags / escapes before token scans."""
    fmt = re.sub(r'"[^"]*"', "", fmt)
    fmt = re.sub(r"\[[^\]]*\]", "", fmt)
    return fmt.replace("\\", "")


def _looks_like_date(fmt: str) -> bool:
    bare = _strip_literals(fmt).lower()
    if "general" in bare:
        return False
    # '0' digits mean numeric; y/d (or standalone h:m) mean date/time
    return bool(re.search(r"[yd]", bare)) or (
        ":" in bare and re.search(r"[hms]", bare) is not None
    )


def _format_date(value: Any, fmt: str) -> str:
    if not isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return _general(value)
    bare = _strip_literals(fmt).lower()
    # mm after hh means minutes — handle the two most common time shapes
    if isinstance(value, _dt.time) or (
        isinstance(value, _dt.datetime)
        and re.search(r"h", bare)
        and not re.search(r"[yd]", bare)
    ):
        return value.strftime("%H:%M")
    has_time = isinstance(value, _dt.datetime) and re.search(r"h", bare)
    date_part = "%Y-%m-%d"
    if "yyyy" not in bare and "yy" in bare:
        date_part = "%y-%m-%d"
    out = value.strftime(date_part)
    if has_time:
        out += value.strftime(" %H:%M")
    return out


def format_cell_value(value: Any, number_format: str | None) -> str:
    """Render *value* the way its Excel number format intends (subset)."""
    if value is None:
        return ""
    fmt = (number_format or "General").strip()
    try:
        if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            return _format_date(value, fmt if _looks_like_date(fmt) else "yyyy-mm-dd")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return _general(value)
        if fmt.lower() in ("general", "@", ""):
            return _general(value)
        if _looks_like_date(fmt):
            return _general(value)  # date fmt on a bare number — punt

        bare = _strip_literals(fmt)
        # Percent
        if "%" in bare:
            m = _DECIMALS_RE.search(bare)
            decimals = len(m.group(1)) if m else 0
            return f"{value * 100:,.{decimals}f}%" if "," in bare else f"{value * 100:.{decimals}f}%"

        m = _DECIMALS_RE.search(bare)
        decimals = len(m.group(1)) if m else 0
        thousands = "," in bare
        if thousands:
            body = f"{value:,.{decimals}f}"
        elif decimals:
            body = f"{value:.{decimals}f}"
        elif "0" in bare or "#" in bare:
            body = _general(round(value) if abs(value - round(value)) < 1e-9 else value)
        else:
            return _general(value)

        cur = _CURRENCY_RE.search(fmt)
        if cur:
            sign = "-" if value < 0 else ""
            return f"{sign}{cur.group(0)}{body.lstrip('-')}"
        return body
    except Exception:  # noqa: BLE001 — formatting must never raise
        return _general(value)
