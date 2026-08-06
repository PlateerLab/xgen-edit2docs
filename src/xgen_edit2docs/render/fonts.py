"""Font discovery + real text metrics (fontTools).

The pptx SVG converter historically estimated run widths with fixed
per-character multipliers (``txbody_to_svg._char_width``: CJK=1em,
space=0.3em, …). That is fine for CJK-heavy text but drifts on
proportional Latin, which moves line breaks relative to PowerPoint.
:class:`FontResolver` provides measured advance widths from the fonts
actually installed on the machine (the same fonts resvg rasterizes
with), so layout and raster agree.

Design constraints:
- Pure python (fontTools), lazy: nothing is scanned until first use.
- TTC-aware (Noto CJK ships as .ttc collections).
- Never raises out of :meth:`text_width` — unknown families and broken
  font files fall back to the legacy heuristic so callers can adopt it
  wholesale (M2 wires this into txbody_to_svg).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

_FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")

# PowerPoint/Word theme aliases → concrete families commonly installed.
_ALIASES = {
    "+mn-lt": "arial",
    "+mj-lt": "arial",
    "+mn-ea": "noto sans cjk kr",
    "+mj-ea": "noto sans cjk kr",
    "맑은 고딕": "malgun gothic",
}

_FALLBACK_FAMILIES = (
    "noto sans cjk kr",
    "noto sans cjk sc",
    "noto sans",
    "dejavu sans",
    "liberation sans",
)


def _default_font_dirs() -> List[Path]:
    dirs = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
        Path("/Library/Fonts"),
        Path("/System/Library/Fonts"),
        Path("C:/Windows/Fonts"),
    ]
    for raw in os.environ.get("E2D_FONT_DIRS", "").split(os.pathsep):
        if raw.strip():
            dirs.append(Path(raw))
    return [d for d in dirs if d.is_dir()]


def _heuristic_width(text: str, size: float) -> float:
    """The legacy fixed-multiplier estimate (kept as the safety net)."""
    total = 0.0
    for ch in text:
        code = ord(ch)
        if code > 0x2E80:
            total += 1.0
        elif ch == " ":
            total += 0.3
        elif ch in "mMwWOQ":
            total += 0.75
        else:
            total += 0.55
    return total * size


class _FontMetrics:
    """Advance widths for one (file, ttc-index) face, lazily loaded."""

    def __init__(self, path: Path, index: int) -> None:
        self.path = path
        self.index = index
        self._upem: Optional[int] = None
        self._cmap: Optional[dict] = None
        self._hmtx = None

    def _load(self) -> bool:
        if self._cmap is not None:
            return True
        try:
            from fontTools.ttLib import TTFont

            font = TTFont(
                str(self.path), fontNumber=self.index if self.index >= 0 else 0,
                lazy=True,
            )
            self._upem = font["head"].unitsPerEm or 1000
            self._cmap = font.getBestCmap()
            self._hmtx = font["hmtx"]
            return True
        except Exception:  # noqa: BLE001 — broken font file → heuristic
            self._cmap = {}
            return False

    def width(self, text: str, size: float) -> Optional[float]:
        if not self._load() or not self._cmap:
            return None
        total = 0
        missing = 0
        for ch in text:
            glyph = self._cmap.get(ord(ch))
            if glyph is None:
                missing += 1
                total += (self._upem or 1000) * 0.55
                continue
            try:
                total += self._hmtx[glyph][0]
            except Exception:  # noqa: BLE001
                total += (self._upem or 1000) * 0.55
        # A face that misses most of the text (e.g. Latin-only font
        # measuring Korean) is the wrong face — let the caller fall
        # through to the next candidate.
        if text and missing / len(text) > 0.5:
            return None
        return total / float(self._upem or 1000) * size


class FontResolver:
    """family name → font file → measured text widths."""

    def __init__(self, font_dirs: Optional[Iterable[os.PathLike]] = None) -> None:
        self._dirs = [Path(d) for d in font_dirs] if font_dirs else _default_font_dirs()
        self._index: Optional[Dict[str, Tuple[Path, int]]] = None
        self._metrics: Dict[Tuple[Path, int], _FontMetrics] = {}

    # ── discovery ────────────────────────────────────────────

    def _family_names(self, path: Path) -> List[Tuple[str, int]]:
        """(family, ttc_index) pairs declared by a font file."""
        from fontTools.ttLib import TTCollection, TTFont

        out: List[Tuple[str, int]] = []

        def names_of(font, idx: int) -> None:
            try:
                name = font["name"]
                for nid in (16, 1):  # typographic family first
                    rec = name.getDebugName(nid)
                    if rec:
                        out.append((rec.strip().lower(), idx))
            except Exception:  # noqa: BLE001
                pass

        try:
            if path.suffix.lower() in (".ttc", ".otc"):
                coll = TTCollection(str(path), lazy=True)
                for i, font in enumerate(coll.fonts):
                    names_of(font, i)
            else:
                names_of(TTFont(str(path), lazy=True), -1)
        except Exception:  # noqa: BLE001 — unreadable file → skip
            pass
        return out

    def _build_index(self) -> Dict[str, Tuple[Path, int]]:
        index: Dict[str, Tuple[Path, int]] = {}
        for root in self._dirs:
            for path in sorted(root.rglob("*")):
                if path.suffix.lower() not in _FONT_EXTS:
                    continue
                for family, idx in self._family_names(path):
                    # First hit wins — earlier dirs take precedence and
                    # Regular tends to sort before Bold variants.
                    index.setdefault(family, (path, idx))
        return index

    @property
    def families(self) -> Dict[str, Tuple[Path, int]]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def resolve(self, family: Optional[str]) -> Optional[Tuple[Path, int]]:
        """Find a font file for *family*, walking aliases + fallbacks."""
        candidates: List[str] = []
        if family:
            fam = family.strip().lower()
            candidates.append(_ALIASES.get(fam, fam))
        candidates.extend(_FALLBACK_FAMILIES)
        for cand in candidates:
            hit = self.families.get(cand)
            if hit:
                return hit
        return None

    def char_advance(
        self, ch: str, *, family: Optional[str], size: float
    ) -> Optional[float]:
        """Measured advance width of one character, or ``None`` when no
        installed face covers it (callers keep their own heuristics)."""
        for fam in self._candidates(family):
            hit = self.families.get(fam)
            if not hit:
                continue
            metrics = self._metrics.get(hit)
            if metrics is None:
                metrics = self._metrics[hit] = _FontMetrics(*hit)
            width = metrics.width(ch, size)
            if width is not None:
                return width
        return None

    def _candidates(self, family: Optional[str]) -> List[str]:
        out: List[str] = []
        if family:
            fam = family.strip().lower()
            out.append(_ALIASES.get(fam, fam))
        out.extend(_FALLBACK_FAMILIES)
        seen: set = set()
        uniq = []
        for f in out:
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        return uniq

    # ── metrics ──────────────────────────────────────────────

    def text_width(self, text: str, *, family: Optional[str] = None, size: float) -> float:
        """Measured width in the same unit as *size* (pt in, pt out).

        Falls back through alias → fallback families → the legacy
        heuristic; never raises.
        """
        if not text:
            return 0.0
        seen: set = set()
        candidates: List[str] = []
        if family:
            fam = family.strip().lower()
            candidates.append(_ALIASES.get(fam, fam))
        candidates.extend(_FALLBACK_FAMILIES)
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            hit = self.families.get(cand)
            if not hit:
                continue
            metrics = self._metrics.get(hit)
            if metrics is None:
                metrics = self._metrics[hit] = _FontMetrics(*hit)
            width = metrics.width(text, size)
            if width is not None:
                return width
        return _heuristic_width(text, size)


@lru_cache(maxsize=1)
def default_font_resolver() -> FontResolver:
    """Process-wide resolver over the default directories."""
    return FontResolver()
