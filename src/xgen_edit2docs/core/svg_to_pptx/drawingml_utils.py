"""Coordinate helpers, color parsing, and font utilities for DrawingML conversion."""

from __future__ import annotations

import re
import math
from xml.etree import ElementTree as ET

from .drawingml_context import AffineMatrix, ConvertContext, IDENTITY_MATRIX

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_NS = 'http://www.w3.org/1999/xlink'

EMU_PER_PX = 9525  # 1 SVG px = 9525 EMU (96 DPI)
FONT_PX_TO_HUNDREDTHS_PT = 75  # 1px = 0.75pt -> 75 hundredths-of-a-point
ANGLE_UNIT = 60000  # DrawingML angle: 60000ths of a degree

# SVG attributes inheritable from parent <g>
INHERITABLE_ATTRS = [
    'fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap',
    'stroke-linejoin', 'opacity', 'fill-opacity', 'stroke-opacity',
    'font-family', 'font-size', 'font-weight', 'font-style',
    'text-anchor', 'letter-spacing', 'text-decoration',
]

# Known East Asian fonts
EA_FONTS = {
    'PingFang SC', 'PingFang TC', 'PingFang HK',
    'Microsoft YaHei', 'Microsoft JhengHei',
    'SimSun', 'SimHei', 'FangSong', 'KaiTi', 'STKaiti',
    'STHeiti', 'STSong', 'STFangsong', 'STXihei', 'STZhongsong',
    'Hiragino Sans', 'Hiragino Sans GB', 'Hiragino Mincho ProN',
    'Hiragino Kaku Gothic ProN', 'Hiragino Kaku Gothic Pro',
    'Hiragino Mincho Pro',
    'Noto Sans SC', 'Noto Sans TC', 'Noto Serif SC', 'Noto Serif TC',
    'Noto Sans JP', 'Noto Serif JP', 'Noto Sans CJK JP',
    'Source Han Sans SC', 'Source Han Sans TC',
    'Source Han Serif SC', 'Source Han Serif TC',
    'Source Han Sans JP', 'Source Han Serif JP',
    'WenQuanYi Micro Hei', 'WenQuanYi Zen Hei',
    'YouYuan', 'LiSu', 'HuaWenKaiTi',
    'Songti SC', 'Songti TC',
    # Japanese fonts (Windows-available)
    'Yu Gothic', 'Yu Gothic UI', 'Yu Mincho',
    'Meiryo', 'Meiryo UI', 'メイリオ',
    'MS Gothic', 'MS Mincho', 'MS PGothic', 'MS PMincho', 'MS UI Gothic',
    # Korean (G3 expanded)
    'Malgun Gothic', 'Gulim', 'Dotum', 'Batang', 'BatangChe', 'GulimChe', 'DotumChe', 'GungsuhChe',
    'Noto Sans KR', 'Noto Serif KR', 'Noto Sans CJK KR', 'Noto Serif CJK KR',
    'Apple SD Gothic Neo', 'Apple SD산돌고딕 Neo',
    'Pretendard', 'Pretendard Variable',
    'Spoqa Han Sans', 'Spoqa Han Sans Neo',
    'Nanum Gothic', 'Nanum Myeongjo', 'Nanum Barun Gothic', 'Nanum Square',
    'NanumGothic', 'NanumMyeongjo',
    'Source Han Sans KR', 'Source Han Serif KR',
    'IBM Plex Sans KR', 'IBM Plex Serif KR',
}
SYSTEM_FONTS = {'system-ui', '-apple-system', 'BlinkMacSystemFont'}

# macOS/Linux-only fonts -> Windows equivalents
FONT_FALLBACK_WIN = {
    'PingFang SC': 'Microsoft YaHei',
    'PingFang TC': 'Microsoft JhengHei',
    'PingFang HK': 'Microsoft JhengHei',
    'Hiragino Sans': 'Microsoft YaHei',
    'Hiragino Sans GB': 'Microsoft YaHei',
    'Hiragino Mincho ProN': 'SimSun',
    'STHeiti': 'SimHei',
    'STSong': 'SimSun',
    'STKaiti': 'KaiTi',
    'STFangsong': 'FangSong',
    'STXihei': 'Microsoft YaHei',
    'STZhongsong': 'SimSun',
    'Songti SC': 'SimSun',
    'Songti TC': 'SimSun',
    'Noto Sans SC': 'Microsoft YaHei',
    'Noto Sans TC': 'Microsoft JhengHei',
    'Noto Serif SC': 'SimSun',
    'Noto Serif TC': 'SimSun',
    # Japanese: keep as-is if user specified (PowerPoint will fallback if uninstalled)
    # 'Noto Sans JP': → keep as 'Noto Sans JP' (do not map)
    # 'メイリオ': → keep as 'メイリオ' (Meiryo alias)
    'メイリオ': 'Meiryo',
    'Source Han Sans SC': 'Microsoft YaHei',
    'Source Han Sans TC': 'Microsoft JhengHei',
    'Source Han Serif SC': 'SimSun',
    'Source Han Serif TC': 'SimSun',
    'Source Han Sans JP': 'Noto Sans JP',
    'Source Han Serif JP': 'Noto Serif JP',
    'WenQuanYi Micro Hei': 'Microsoft YaHei',
    'WenQuanYi Zen Hei': 'Microsoft YaHei',
    # Korean: macOS-only / web fonts -> Windows-available equivalents (G3)
    'Apple SD Gothic Neo': 'Malgun Gothic',
    'Apple SD산돌고딕 Neo': 'Malgun Gothic',
    'Pretendard': 'Malgun Gothic',
    'Pretendard Variable': 'Malgun Gothic',
    'Spoqa Han Sans': 'Malgun Gothic',
    'Spoqa Han Sans Neo': 'Malgun Gothic',
    'Source Han Sans KR': 'Malgun Gothic',
    'Source Han Serif KR': 'Batang',
    'Noto Sans CJK KR': 'Malgun Gothic',
    'Noto Serif CJK KR': 'Batang',
    'Noto Sans KR': 'Malgun Gothic',
    'Noto Serif KR': 'Batang',
    # Latin fonts (macOS / Linux / Web -> Windows)
    'SF Pro': 'Segoe UI',
    'SF Pro Display': 'Segoe UI',
    'SF Pro Text': 'Segoe UI',
    'SF Mono': 'Consolas',
    'Menlo': 'Consolas',
    'Monaco': 'Consolas',
    'Helvetica Neue': 'Arial',
    'Helvetica': 'Arial',
    'Roboto': 'Segoe UI',
    'Ubuntu': 'Segoe UI',
    'Liberation Sans': 'Arial',
    'Liberation Serif': 'Times New Roman',
    'Liberation Mono': 'Consolas',
    'DejaVu Sans': 'Segoe UI',
    'DejaVu Serif': 'Times New Roman',
    'DejaVu Sans Mono': 'Consolas',
}

GENERIC_FONT_MAP = {
    'monospace': 'Consolas',
    'sans-serif': 'Segoe UI',
    'serif': 'Times New Roman',
}

# When the latin font is serif and no EA font is specified,
# prefer SimSun (serif CJK) over Microsoft YaHei (sans-serif CJK).
_SERIF_LATIN = {
    'Times New Roman', 'Georgia', 'Garamond', 'Palatino', 'Palatino Linotype',
    'Book Antiqua', 'Cambria', 'SimSun', 'Liberation Serif', 'DejaVu Serif',
}

# SVG stroke-dasharray -> DrawingML prstDash
DASH_PRESETS = {
    '4,4': 'dash',  '4 4': 'dash',
    '6,3': 'dash',  '6 3': 'dash',
    '2,2': 'sysDot', '2 2': 'sysDot',
    '8,4': 'lgDash', '8 4': 'lgDash',
    '8,4,2,4': 'lgDashDot', '8 4 2 4': 'lgDashDot',
}


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def px_to_emu(px: float) -> int:
    """Convert SVG pixels to EMU."""
    return round(px * EMU_PER_PX)


def _f(val: str | None, default: float = 0.0) -> float:
    """Parse a float attribute value, returning default if missing."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


_LENGTH_RE = re.compile(r'^\s*([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)\s*([A-Za-z%]*)\s*$')


def parse_svg_length(
    val: str | None,
    default: float = 0.0,
    *,
    percent_base: float | None = None,
    font_size: float = 16.0,
) -> float:
    """Parse SVG/CSS length values into SVG px.

    Unitless and ``px`` values are already SVG px. Percentages need a caller
    supplied reference length because SVG uses different bases for x, y,
    width, height, and radii.
    """
    if val is None:
        return default
    match = _LENGTH_RE.match(str(val))
    if not match:
        return default

    number = float(match.group(1))
    unit = match.group(2).lower() or 'px'
    if unit == '%':
        if percent_base is None:
            return default
        return percent_base * number / 100.0
    if unit in ('', 'px'):
        return number
    if unit == 'pt':
        return number * 96.0 / 72.0
    if unit in ('pc', 'pica'):
        return number * 16.0
    if unit == 'in':
        return number * 96.0
    if unit == 'cm':
        return number * 96.0 / 2.54
    if unit == 'mm':
        return number * 96.0 / 25.4
    if unit == 'q':
        return number * 96.0 / 101.6
    if unit in ('em', 'rem'):
        return number * font_size
    return default


def svg_length_x(val: str | None, ctx: ConvertContext, default: float = 0.0) -> float:
    return parse_svg_length(val, default, percent_base=ctx.viewport_width)


def svg_length_y(val: str | None, ctx: ConvertContext, default: float = 0.0) -> float:
    return parse_svg_length(val, default, percent_base=ctx.viewport_height)


def svg_length_size(val: str | None, ctx: ConvertContext, default: float = 0.0) -> float:
    base = min(ctx.viewport_width, ctx.viewport_height)
    return parse_svg_length(val, default, percent_base=base)


# ---------------------------------------------------------------------------
# SVG transform matrix helpers
# ---------------------------------------------------------------------------

_TRANSFORM_RE = re.compile(r'([a-zA-Z]+)\(([^)]*)\)')
_NUMBER_RE = re.compile(r'[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?')


def matrix_multiply(left: AffineMatrix, right: AffineMatrix) -> AffineMatrix:
    """Compose two SVG affine matrices, applying ``right`` before ``left``."""
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _translate_matrix(tx: float, ty: float = 0.0) -> AffineMatrix:
    return (1.0, 0.0, 0.0, 1.0, tx, ty)


def _scale_matrix(sx: float, sy: float | None = None) -> AffineMatrix:
    return (sx, 0.0, 0.0, sx if sy is None else sy, 0.0, 0.0)


def _rotate_matrix(angle_deg: float, cx: float | None = None, cy: float | None = None) -> AffineMatrix:
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    rot = (cos_a, sin_a, -sin_a, cos_a, 0.0, 0.0)
    if cx is None or cy is None:
        return rot
    return matrix_multiply(
        matrix_multiply(_translate_matrix(cx, cy), rot),
        _translate_matrix(-cx, -cy),
    )


def parse_transform_matrix(transform_str: str) -> AffineMatrix:
    """Parse an SVG transform list into one affine matrix."""
    if not transform_str:
        return IDENTITY_MATRIX

    matrix = IDENTITY_MATRIX
    for name, raw_args in _TRANSFORM_RE.findall(transform_str):
        args = [float(n) for n in _NUMBER_RE.findall(raw_args)]
        name = name.lower()
        local = IDENTITY_MATRIX

        if name == 'matrix' and len(args) >= 6:
            local = (args[0], args[1], args[2], args[3], args[4], args[5])
        elif name == 'translate' and args:
            local = _translate_matrix(args[0], args[1] if len(args) > 1 else 0.0)
        elif name == 'scale' and args:
            local = _scale_matrix(args[0], args[1] if len(args) > 1 else None)
        elif name == 'rotate' and args:
            local = _rotate_matrix(
                args[0],
                args[1] if len(args) > 2 else None,
                args[2] if len(args) > 2 else None,
            )

        matrix = matrix_multiply(matrix, local)

    return matrix


def transform_point(matrix: AffineMatrix, x: float, y: float) -> tuple[float, float]:
    """Apply an SVG affine matrix to a point."""
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def rect_to_dml_xfrm(
    x: float,
    y: float,
    w: float,
    h: float,
    matrix: AffineMatrix,
) -> tuple[str, int, int, int, int, tuple[int, int, int, int]]:
    """Map a transformed SVG rectangle to DrawingML xfrm attributes.

    DrawingML can represent rotated/flipped rectangles, but not arbitrary
    shear. Template-import picture wrappers only use translate/rotate/scale,
    so decomposing the transformed local X/Y axes is sufficient here.
    """
    p0 = transform_point(matrix, x, y)
    p1 = transform_point(matrix, x + w, y)
    p2 = transform_point(matrix, x + w, y + h)
    p3 = transform_point(matrix, x, y + h)

    ux = p1[0] - p0[0]
    uy = p1[1] - p0[1]
    vx = p3[0] - p0[0]
    vy = p3[1] - p0[1]

    rect_w = max(math.hypot(ux, uy), 0.001)
    rect_h = max(math.hypot(vx, vy), 0.001)
    cross = ux * vy - uy * vx

    if cross < 0:
        angle_deg = math.degrees(math.atan2(-uy, -ux))
        flip_attr = ' flipH="1"'
    else:
        angle_deg = math.degrees(math.atan2(uy, ux))
        flip_attr = ''

    rot = round(angle_deg * ANGLE_UNIT)
    rot_attr = f' rot="{rot}"' if rot else ''

    center_x = (p0[0] + p2[0]) / 2
    center_y = (p0[1] + p2[1]) / 2
    off_x = px_to_emu(center_x - rect_w / 2)
    off_y = px_to_emu(center_y - rect_h / 2)
    ext_cx = px_to_emu(rect_w)
    ext_cy = px_to_emu(rect_h)

    xs = [p0[0], p1[0], p2[0], p3[0]]
    ys = [p0[1], p1[1], p2[1], p3[1]]
    bounds = (
        px_to_emu(min(xs)),
        px_to_emu(min(ys)),
        px_to_emu(max(xs)),
        px_to_emu(max(ys)),
    )

    return f'{flip_attr}{rot_attr}', off_x, off_y, ext_cx, ext_cy, bounds


def _extract_inheritable_styles(elem: ET.Element) -> dict[str, str]:
    """Extract all SVG-inheritable presentation attributes from an element."""
    styles: dict[str, str] = {}
    for attr in INHERITABLE_ATTRS:
        val = elem.get(attr)
        if val is not None:
            styles[attr] = val
    styles.update({
        attr: val
        for attr, val in parse_inline_style(elem.get('style')).items()
        if attr in INHERITABLE_ATTRS
    })
    return styles


def _get_attr(elem: ET.Element, attr: str, ctx: ConvertContext) -> str | None:
    """Get effective attribute: element's own value first, then inherited."""
    style_val = parse_inline_style(elem.get('style')).get(attr)
    if style_val is not None:
        return style_val
    val = elem.get(attr)
    if val is not None:
        return val
    return ctx.inherited_styles.get(attr)


def ctx_x(val: float, ctx: ConvertContext) -> float:
    """Apply context scale + translate to an X coordinate."""
    return val * ctx.scale_x + ctx.translate_x


def ctx_y(val: float, ctx: ConvertContext) -> float:
    """Apply context scale + translate to a Y coordinate."""
    return val * ctx.scale_y + ctx.translate_y


def ctx_w(val: float, ctx: ConvertContext) -> float:
    """Apply context scale to a width value."""
    return val * ctx.scale_x


def ctx_h(val: float, ctx: ConvertContext) -> float:
    """Apply context scale to a height value."""
    return val * ctx.scale_y


# ---------------------------------------------------------------------------
# Color / style parsing
# ---------------------------------------------------------------------------

_CSS_NAMED_COLORS = {
    'black': '000000',
    'silver': 'C0C0C0',
    'gray': '808080',
    'grey': '808080',
    'white': 'FFFFFF',
    'maroon': '800000',
    'red': 'FF0000',
    'purple': '800080',
    'fuchsia': 'FF00FF',
    'magenta': 'FF00FF',
    'green': '008000',
    'lime': '00FF00',
    'olive': '808000',
    'yellow': 'FFFF00',
    'navy': '000080',
    'blue': '0000FF',
    'teal': '008080',
    'aqua': '00FFFF',
    'cyan': '00FFFF',
    'orange': 'FFA500',
    'brown': 'A52A2A',
    'pink': 'FFC0CB',
    'gold': 'FFD700',
    'transparent': None,
    'lightgray': 'D3D3D3',
    'lightgrey': 'D3D3D3',
    'darkgray': 'A9A9A9',
    'darkgrey': 'A9A9A9',
}


def parse_inline_style(style_str: str | None) -> dict[str, str]:
    """Parse an SVG inline style declaration into ``property: value`` pairs."""
    styles: dict[str, str] = {}
    if not style_str:
        return styles
    for part in style_str.split(';'):
        if ':' not in part:
            continue
        name, value = part.split(':', 1)
        name = name.strip().lower()
        value = value.strip()
        if name and value:
            styles[name] = value
    return styles


def _parse_color_channel(raw: str) -> int:
    raw = raw.strip()
    if raw.endswith('%'):
        value = float(raw[:-1]) * 255.0 / 100.0
    else:
        value = float(raw)
    return max(0, min(255, int(round(value))))


def parse_hex_color(color_str: str) -> str | None:
    """Parse SVG color values to 'RRGGBB'. Returns None on failure."""
    if not color_str:
        return None
    color_str = color_str.strip()
    named = _CSS_NAMED_COLORS.get(color_str.lower())
    if named is not None or color_str.lower() in _CSS_NAMED_COLORS:
        return named

    rgb_match = re.match(r'rgba?\((.+)\)$', color_str, flags=re.IGNORECASE)
    if rgb_match:
        channels = re.findall(r'[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?%?', rgb_match.group(1))
        if len(channels) >= 3:
            try:
                r, g, b = (_parse_color_channel(ch) for ch in channels[:3])
                return f'{r:02X}{g:02X}{b:02X}'
            except ValueError:
                return None

    if color_str.startswith('#'):
        color_str = color_str[1:]
    if len(color_str) == 3:
        color_str = ''.join(c * 2 for c in color_str)
    if len(color_str) == 6 and all(c in '0123456789abcdefABCDEF' for c in color_str):
        return color_str.upper()
    return None


def parse_stop_style(style_str: str) -> tuple[str | None, float]:
    """Parse a gradient stop's style attribute.

    Args:
        style_str: Style string like 'stop-color:#XXX;stop-opacity:N'.

    Returns:
        (color, opacity) tuple.
    """
    color = None
    opacity = 1.0
    if not style_str:
        return color, opacity

    for part in style_str.split(';'):
        part = part.strip()
        if part.startswith('stop-color:'):
            color = parse_hex_color(part.split(':', 1)[1].strip())
        elif part.startswith('stop-opacity:'):
            try:
                opacity = float(part.split(':', 1)[1].strip())
            except ValueError:
                pass

    return color, opacity


def resolve_url_id(url_str: str) -> str | None:
    """Extract ID from 'url(#someId)' reference."""
    if not url_str:
        return None
    m = re.match(r'url\(#([^)]+)\)', url_str.strip())
    return m.group(1) if m else None


def get_effective_filter_id(elem: ET.Element, ctx: ConvertContext) -> str | None:
    """Get the effective filter ID for an element, including inherited context."""
    filt = elem.get('filter')
    if filt:
        return resolve_url_id(filt)
    return ctx.filter_id


# ---------------------------------------------------------------------------
# Font parsing
# ---------------------------------------------------------------------------

import re as _re

# Trailing numeric weight glued onto a CSS font-family value
# (`font-family="Pretendard 700"`). PowerPoint then looks up a literal
# font called "Pretendard 700", fails, and falls back to the system
# default — which on Windows means Korean titles render in Calibri.
# Strip the suffix before the lookup so the bare family name hits the
# EA_FONTS / FONT_FALLBACK_WIN tables the way the user meant it to.
#
# Only numeric tokens are stripped because word-form weight names
# legitimately appear inside real family names (`Arial Black`,
# `Helvetica Neue Light`, `Lucida Bright`, ...). The CSS spec allows
# numeric weights only on `font-weight`, never inside `font-family`,
# so a trailing `100`-`900` is unambiguously a model error.
_NUMERIC_WEIGHT_SUFFIX_RE = _re.compile(r"\s+[1-9]00$")


def _strip_weight_suffix(font_name: str) -> str:
    """Strip a trailing numeric CSS-weight token from *font_name*.

    >>> _strip_weight_suffix('Pretendard 700')
    'Pretendard'
    >>> _strip_weight_suffix('Pretendard Variable 900')
    'Pretendard Variable'
    >>> _strip_weight_suffix('Arial Black')   # genuine family name
    'Arial Black'
    >>> _strip_weight_suffix('Inter 400 Display')   # mid-string, leave it
    'Inter 400 Display'
    """
    if not font_name:
        return font_name
    return _NUMERIC_WEIGHT_SUFFIX_RE.sub('', font_name).strip()


def parse_font_family(font_family_str: str) -> dict[str, str]:
    """Parse CSS font-family into latin/ea typeface names.

    Prioritizes Windows-available fonts since PPTX is primarily opened on
    Windows. macOS/Linux-only fonts are mapped via FONT_FALLBACK_WIN.

    Robustness: tolerates `font-family="Pretendard 700"` (weight glued
    into the family name) by stripping recognised weight tokens before
    the typeface lookup. The font-weight is still expected to arrive via
    the SVG `font-weight` attribute — this function only normalises the
    NAME used in the OOXML latin/ea elements.
    """
    if not font_family_str:
        return {'latin': 'Segoe UI', 'ea': 'Microsoft YaHei'}

    fonts = [f.strip().strip("'\"") for f in font_family_str.split(',')]
    latin_font = None
    ea_font = None
    # Track the generic fallback separately so it only applies when no
    # explicit family resolved — `sans-serif` at the end of a Korean
    # stack must NOT preempt the Pretendard → Malgun Gothic mapping.
    generic_fallback = None

    for font in fonts:
        font = _strip_weight_suffix(font)
        if font in SYSTEM_FONTS:
            continue
        if font in GENERIC_FONT_MAP:
            generic_fallback = generic_fallback or GENERIC_FONT_MAP[font]
            continue

        win_font = FONT_FALLBACK_WIN.get(font, font)
        if font in EA_FONTS:
            ea_font = ea_font or win_font
        else:
            latin_font = latin_font or win_font

    # PPT renders CJK text via the latin typeface when ea doesn't match,
    # so when the stack named only CJK families (Pretendard, Malgun
    # Gothic, ...) mirror the ea pick into latin too. Otherwise the
    # Korean run would render in whichever generic the stack ended on
    # (`sans-serif` → Segoe UI), losing Hangul glyph weighting.
    if not latin_font and ea_font:
        latin_font = ea_font

    final_latin = latin_font or generic_fallback or 'Segoe UI'

    # EA must always be a CJK-capable font
    if not ea_font:
        ea_font = 'SimSun' if final_latin in _SERIF_LATIN else 'Microsoft YaHei'

    return {'latin': final_latin, 'ea': ea_font}


def is_cjk_char(ch: str) -> bool:
    """Check if a character is CJK (Chinese/Japanese/Korean).

    Hangul ranges were missing from the original ppt-master implementation,
    causing estimate_text_width() to under-estimate Korean text widths.
    See ppt-master-analysis/03-korean-gaps.md G1.
    """
    cp = ord(ch)
    return (
        # CJK ideographs (Chinese, kanji)
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
        0x2E80 <= cp <= 0x2EFF or 0x3000 <= cp <= 0x303F or
        0xFF00 <= cp <= 0xFFEF or 0xF900 <= cp <= 0xFAFF or
        0x20000 <= cp <= 0x2A6DF or
        # Japanese kana
        0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF or
        # Korean Hangul (G1 fix)
        0xAC00 <= cp <= 0xD7A3 or       # Hangul Syllables
        0x1100 <= cp <= 0x11FF or       # Hangul Jamo
        0x3130 <= cp <= 0x318F or       # Hangul Compatibility Jamo
        0xA960 <= cp <= 0xA97F or       # Hangul Jamo Extended-A
        0xD7B0 <= cp <= 0xD7FF          # Hangul Jamo Extended-B
    )


def is_hangul_char(ch: str) -> bool:
    cp = ord(ch)
    return (0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF or
            0x3130 <= cp <= 0x318F or 0xA960 <= cp <= 0xA97F or
            0xD7B0 <= cp <= 0xD7FF)


def is_han_char(ch: str) -> bool:
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
            0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2A6DF)


def is_kana_char(ch: str) -> bool:
    cp = ord(ch)
    return 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF


def detect_lang(text: str, default: str = "en-US") -> str:
    """Return a BCP-47 locale code for the dominant script in *text*.

    Priority order: Korean Hangul > Japanese kana > CJK Han ideographs > default.
    Used to set OOXML `lang` attributes correctly so PowerPoint applies the right
    proofing/spell-check rules. See ppt-master-analysis/03-korean-gaps.md G2.
    """
    if not text:
        return default
    has_hangul = False
    has_kana = False
    has_han = False
    for ch in text:
        if is_hangul_char(ch):
            has_hangul = True
        elif is_kana_char(ch):
            has_kana = True
        elif is_han_char(ch):
            has_han = True
    if has_hangul:
        return "ko-KR"
    if has_kana:
        return "ja-JP"
    if has_han:
        return "zh-CN"
    return default


def estimate_text_width(text: str, font_size: float, font_weight: str = '400') -> float:
    """Estimate text width in SVG pixels."""
    width = 0.0
    for ch in text:
        if is_cjk_char(ch):
            width += font_size
        elif ch == ' ':
            width += font_size * 0.3
        elif ch in 'mMwWOQ%':
            width += font_size * 0.75
        elif ch in 'iIlj!|':
            width += font_size * 0.3
        elif ch.isdigit():
            # digits are tabular (uniform ~0.55em) in most UI fonts, including
            # '1' — classing it with 'il|' under-sizes the box and makes
            # renderers that ignore wrap="none" (LibreOffice) wrap the line
            width += font_size * 0.55
        else:
            width += font_size * 0.55

    if font_weight in ('bold', '600', '700', '800', '900'):
        width *= 1.05

    return width


def _xml_escape(text: str) -> str:
    """Escape XML special characters."""
    return (text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;'))
