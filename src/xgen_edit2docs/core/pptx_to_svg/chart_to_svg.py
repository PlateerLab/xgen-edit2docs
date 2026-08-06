"""Native OOXML chart renderer — c:chart XML → SVG (native-render plan M2).

Replaces the dashed "[chart]" placeholder with a real, deterministic
render of the common chart types:

    barChart (col/bar × clustered/stacked/percentStacked), lineChart,
    areaChart, pieChart, doughnutChart, scatterChart

Scope is presentation-preview fidelity, not Excel parity: series
colors honour explicit ``c:spPr`` solid fills and otherwise cycle the
theme accents; the value axis gets "nice" ticks + light gridlines;
category labels truncate rather than rotate; unsupported subtypes fall
back to the first supported plot found in the plotArea (the caller
keeps its placeholder path for charts we cannot read at all).
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .color_resolver import ColorPalette

C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = {"c": C_NS, "a": A_NS}

# Office default accent cycle (used when the theme palette is absent).
_DEFAULT_ACCENTS = ["4472C4", "ED7D31", "A5A5A5", "FFC000", "5B9BD5", "70AD47"]

_PLOT_TAGS = (
    "barChart", "lineChart", "areaChart",
    "pieChart", "doughnutChart", "scatterChart",
)

_AXIS_FONT = 10.0
_LABEL_FONT = 10.0
_TITLE_FONT = 14.0
_LEGEND_FONT = 10.0


@dataclass
class ChartResult:
    svg: str = ""
    defs: list[str] = field(default_factory=list)


@dataclass
class _Series:
    name: str
    values: list[Optional[float]]
    color: str
    x_values: list[Optional[float]] = field(default_factory=list)  # scatter
    show_values: bool = False


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _f(v: float) -> str:
    out = f"{v:.2f}".rstrip("0").rstrip(".")
    return out or "0"


def _texts(el: Optional[ET.Element], path: str) -> list[str]:
    if el is None:
        return []
    return ["".join(v.itertext()) for v in el.findall(path, NS)]


def _pt_list(container: Optional[ET.Element]) -> list[tuple[int, str]]:
    """(idx, value) pairs from any c:strRef/c:numRef/c:strLit/c:numLit."""
    if container is None:
        return []
    out: list[tuple[int, str]] = []
    for pt in container.findall(".//c:pt", NS):
        v = pt.find("c:v", NS)
        try:
            idx = int(pt.attrib.get("idx", len(out)))
        except ValueError:
            idx = len(out)
        out.append((idx, "".join(v.itertext()) if v is not None else ""))
    return out


def _dense(pairs: list[tuple[int, str]], *, numeric: bool) -> list:
    """idx-addressed points → dense list (holes become None/'')."""
    size = (max((i for i, _ in pairs), default=-1)) + 1
    dense: list = [None if numeric else ""] * size
    for idx, raw in pairs:
        if 0 <= idx < size:
            if numeric:
                try:
                    dense[idx] = float(raw)
                except (TypeError, ValueError):
                    dense[idx] = None
            else:
                dense[idx] = raw
    return dense


def _series_color(ser: ET.Element, idx: int, palette: Optional[ColorPalette]) -> str:
    srgb = ser.find("c:spPr/a:solidFill/a:srgbClr", NS)
    if srgb is not None and srgb.attrib.get("val"):
        return "#" + srgb.attrib["val"]
    scheme = ser.find("c:spPr/a:solidFill/a:schemeClr", NS)
    if scheme is not None and palette is not None:
        hexv = palette.resolve_scheme(scheme.attrib.get("val", ""))
        if hexv:
            return "#" + hexv
    if palette is not None:
        hexv = palette.resolve_scheme(f"accent{(idx % 6) + 1}")
        if hexv:
            return "#" + hexv
    return "#" + _DEFAULT_ACCENTS[idx % len(_DEFAULT_ACCENTS)]


def _parse_series(plot: ET.Element, palette: Optional[ColorPalette]) -> tuple[list[_Series], list[str]]:
    """All c:ser of one plot → (series, category labels)."""
    series: list[_Series] = []
    categories: list[str] = []
    show_val_plot = _bool(plot.find("c:dLbls/c:showVal", NS))
    for i, ser in enumerate(plot.findall("c:ser", NS)):
        name_texts = _texts(ser.find("c:tx", NS), ".//c:v") or _texts(ser.find("c:tx", NS), ".//a:t")
        name = name_texts[0] if name_texts else f"Series {i + 1}"
        cats = _dense(_pt_list(ser.find("c:cat", NS)), numeric=False)
        vals = _dense(_pt_list(ser.find("c:val", NS)), numeric=True)
        xvals = _dense(_pt_list(ser.find("c:xVal", NS)), numeric=True)
        yvals = _dense(_pt_list(ser.find("c:yVal", NS)), numeric=True)
        if yvals:  # scatter stores values in xVal/yVal
            vals = yvals
        if cats and len(cats) > len(categories):
            categories = [str(c) for c in cats]
        show_val = show_val_plot or _bool(ser.find("c:dLbls/c:showVal", NS))
        series.append(
            _Series(
                name=name,
                values=vals,
                color=_series_color(ser, i, palette),
                x_values=xvals,
                show_values=show_val,
            )
        )
    n = max((len(s.values) for s in series), default=0)
    if not categories:
        categories = [str(i + 1) for i in range(n)]
    for s in series:
        s.values += [None] * (n - len(s.values))
    return series, categories[:n] if n else categories


def _bool(el: Optional[ET.Element]) -> bool:
    return el is not None and el.attrib.get("val", "1") not in ("0", "false")


def _nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1.0
    span = hi - lo
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if span / step <= target:
            break
    start = math.floor(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _fmt_val(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        i = int(round(v))
        return f"{i:,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: max(limit - 1, 1)] + "…"


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def convert_chart(
    chart_root: ET.Element,
    x: float,
    y: float,
    w: float,
    h: float,
    palette: Optional[ColorPalette],
    *,
    id_prefix: str = "chart",
) -> ChartResult:
    """Render the first supported plot in c:chartSpace/c:chart/c:plotArea."""
    chart = chart_root.find("c:chart", NS)
    if chart is None and chart_root.tag == f"{{{C_NS}}}chart":
        chart = chart_root
    if chart is None:
        return ChartResult()
    plot_area = chart.find("c:plotArea", NS)
    if plot_area is None:
        return ChartResult()

    plot = None
    kind = ""
    for tag in _PLOT_TAGS:
        el = plot_area.find(f"c:{tag}", NS)
        if el is not None and el.find("c:ser", NS) is not None:
            plot, kind = el, tag
            break
    if plot is None:
        return ChartResult()

    series, categories = _parse_series(plot, palette)
    if not series:
        return ChartResult()

    title_texts = _texts(chart.find("c:title", NS), ".//a:t")
    title = " ".join(t for t in title_texts if t).strip()
    # PowerPoint shows a legend for multi-series charts by default even
    # when authoring tools omit the explicit <c:legend> element.
    show_legend = len(series) > 1 or chart.find("c:legend", NS) is not None

    pad = max(6.0, min(w, h) * 0.02)
    top = y + pad
    if title:
        top += _TITLE_FONT + pad * 0.5
    bottom = y + h - pad
    if show_legend:
        bottom -= _LEGEND_FONT + pad

    parts: list[str] = []
    if title:
        parts.append(
            f'<text x="{_f(x + w / 2)}" y="{_f(y + pad + _TITLE_FONT * 0.8)}" '
            f'text-anchor="middle" font-size="{_TITLE_FONT}" font-weight="bold" '
            f'fill="#404040">{_esc(_truncate(title, 60))}</text>'
        )

    body_kwargs = dict(series=series, categories=categories)
    if kind in ("pieChart", "doughnutChart"):
        parts.append(
            _render_pie(
                x, top, w, bottom - top, series[0], categories,
                doughnut=(kind == "doughnutChart"),
            )
        )
    elif kind == "scatterChart":
        parts.append(_render_scatter(x, top, w, bottom - top, series))
    else:
        grouping_el = plot.find("c:grouping", NS)
        grouping = grouping_el.attrib.get("val", "clustered") if grouping_el is not None else "clustered"
        bar_dir_el = plot.find("c:barDir", NS)
        horizontal = kind == "barChart" and bar_dir_el is not None and bar_dir_el.attrib.get("val") == "bar"
        parts.append(
            _render_cartesian(
                x, top, w, bottom - top,
                kind=kind, grouping=grouping, horizontal=horizontal,
                **body_kwargs,
            )
        )

    if show_legend:
        parts.append(_render_legend(x, y + h - pad - _LEGEND_FONT, w, series))

    return ChartResult(svg="\n".join(p for p in parts if p))


# ---------------------------------------------------------------------------
# Cartesian family (bar / column / line / area)
# ---------------------------------------------------------------------------


def _value_range(series: list[_Series], grouping: str, kind: str) -> tuple[float, float]:
    lo, hi = 0.0, 0.0
    n = max((len(s.values) for s in series), default=0)
    if grouping in ("stacked", "percentStacked") and kind in ("barChart", "areaChart"):
        for i in range(n):
            pos = sum(s.values[i] for s in series if s.values[i] is not None and s.values[i] > 0)
            neg = sum(s.values[i] for s in series if s.values[i] is not None and s.values[i] < 0)
            hi = max(hi, pos)
            lo = min(lo, neg)
        if grouping == "percentStacked":
            return 0.0, 100.0
    else:
        for s in series:
            for v in s.values:
                if v is None:
                    continue
                hi = max(hi, v)
                lo = min(lo, v)
    if lo > 0:
        lo = 0.0
    if hi < 0:
        hi = 0.0
    if hi == lo:
        hi = lo + 1.0
    return lo, hi


def _render_cartesian(
    x: float, y: float, w: float, h: float, *,
    series: list[_Series], categories: list[str],
    kind: str, grouping: str, horizontal: bool,
) -> str:
    # Axis gutters
    left = x + 44.0
    bottom = y + h - 18.0
    plot_w = max(w - (left - x) - 8.0, 10.0)
    plot_h = max(bottom - y - 4.0, 10.0)
    top = bottom - plot_h

    lo, hi = _value_range(series, grouping, kind)
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(ticks[0], lo), max(ticks[-1], hi)
    span = hi - lo or 1.0

    n = len(categories)
    stacked = grouping in ("stacked", "percentStacked") and kind in ("barChart", "areaChart")
    pct = grouping == "percentStacked"

    def vx(frac: float) -> float:  # category fraction → px along category axis
        return left + frac * plot_w

    def vy(val: float) -> float:  # value → px (vertical charts)
        return bottom - (val - lo) / span * plot_h

    parts: list[str] = []
    # Gridlines + value labels (always on the value axis)
    for t in ticks:
        if horizontal:
            gx = left + (t - lo) / span * plot_w
            parts.append(
                f'<line x1="{_f(gx)}" y1="{_f(top)}" x2="{_f(gx)}" y2="{_f(bottom)}" '
                f'stroke="#E0E0E0" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{_f(gx)}" y="{_f(bottom + 12)}" text-anchor="middle" '
                f'font-size="{_AXIS_FONT}" fill="#808080">{_esc(_fmt_val(t))}</text>'
            )
        else:
            gy = vy(t)
            parts.append(
                f'<line x1="{_f(left)}" y1="{_f(gy)}" x2="{_f(left + plot_w)}" y2="{_f(gy)}" '
                f'stroke="#E0E0E0" stroke-width="1"/>'
            )
            label = f"{_fmt_val(t)}%" if pct else _fmt_val(t)
            parts.append(
                f'<text x="{_f(left - 4)}" y="{_f(gy + 3)}" text-anchor="end" '
                f'font-size="{_AXIS_FONT}" fill="#808080">{_esc(label)}</text>'
            )
    # Axis lines
    parts.append(
        f'<line x1="{_f(left)}" y1="{_f(top)}" x2="{_f(left)}" y2="{_f(bottom)}" '
        f'stroke="#B0B0B0" stroke-width="1"/>'
    )
    parts.append(
        f'<line x1="{_f(left)}" y1="{_f(bottom)}" x2="{_f(left + plot_w)}" y2="{_f(bottom)}" '
        f'stroke="#B0B0B0" stroke-width="1"/>'
    )

    slot = plot_w / max(n, 1)
    if horizontal:
        slot = plot_h / max(n, 1)

    # Category labels
    max_chars = max(int(slot / (_AXIS_FONT * 0.6)), 4)
    for i, cat in enumerate(categories):
        label = _esc(_truncate(str(cat), max_chars))
        if horizontal:
            cy = top + (i + 0.5) * slot
            parts.append(
                f'<text x="{_f(left - 4)}" y="{_f(cy + 3)}" text-anchor="end" '
                f'font-size="{_AXIS_FONT}" fill="#606060">{label}</text>'
            )
        else:
            cx = vx((i + 0.5) / max(n, 1))
            parts.append(
                f'<text x="{_f(cx)}" y="{_f(bottom + 12)}" text-anchor="middle" '
                f'font-size="{_AXIS_FONT}" fill="#606060">{label}</text>'
            )

    if kind == "barChart":
        parts.extend(
            _render_bars(
                series, n, left, top, bottom, plot_w, plot_h, slot,
                lo, span, stacked=stacked, pct=pct, horizontal=horizontal,
            )
        )
    elif kind == "areaChart":
        parts.extend(
            _render_area(series, n, vx, vy, bottom, stacked=stacked, pct=pct)
        )
    else:  # lineChart
        parts.extend(_render_lines(series, n, vx, vy))
    return "\n".join(parts)


def _stack_offsets(series: list[_Series], n: int, pct: bool) -> list[list[tuple[float, float]]]:
    """Per series, per category: (base, value) after stacking."""
    pos = [0.0] * n
    neg = [0.0] * n
    totals = [
        sum(abs(s.values[i]) for s in series if s.values[i] is not None) or 1.0
        for i in range(n)
    ]
    out: list[list[tuple[float, float]]] = []
    for s in series:
        row: list[tuple[float, float]] = []
        for i in range(n):
            v = s.values[i]
            if v is None:
                row.append((pos[i], 0.0))
                continue
            val = (v / totals[i] * 100.0) if pct else v
            if val >= 0:
                row.append((pos[i], val))
                pos[i] += val
            else:
                row.append((neg[i], val))
                neg[i] += val
        out.append(row)
    return out


def _render_bars(
    series, n, left, top, bottom, plot_w, plot_h, slot,
    lo, span, *, stacked: bool, pct: bool, horizontal: bool,
) -> list[str]:
    parts: list[str] = []
    group_frac = 0.72
    if stacked:
        offsets = _stack_offsets(series, n, pct)
        bar_thick = slot * group_frac
        for si, s in enumerate(series):
            for i in range(n):
                base, val = offsets[si][i]
                if val == 0.0 and s.values[i] is None:
                    continue
                lo_v, hi_v = sorted((base, base + val))
                if horizontal:
                    x0 = left + (lo_v - lo) / span * plot_w
                    x1 = left + (hi_v - lo) / span * plot_w
                    cy = top + (i + 0.5) * slot
                    parts.append(
                        f'<rect x="{_f(x0)}" y="{_f(cy - bar_thick / 2)}" '
                        f'width="{_f(max(x1 - x0, 0.5))}" height="{_f(bar_thick)}" '
                        f'fill="{s.color}"/>'
                    )
                else:
                    y0 = bottom - (hi_v - lo) / span * plot_h
                    y1 = bottom - (lo_v - lo) / span * plot_h
                    cx = left + (i + 0.5) * slot
                    parts.append(
                        f'<rect x="{_f(cx - bar_thick / 2)}" y="{_f(y0)}" '
                        f'width="{_f(bar_thick)}" height="{_f(max(y1 - y0, 0.5))}" '
                        f'fill="{s.color}"/>'
                    )
        return parts

    per = max(len(series), 1)
    bar_thick = slot * group_frac / per
    for si, s in enumerate(series):
        for i in range(n):
            v = s.values[i]
            if v is None:
                continue
            lo_v, hi_v = sorted((0.0, v))
            off = (i + 0.5) * slot - slot * group_frac / 2 + si * bar_thick
            if horizontal:
                x0 = left + (lo_v - lo) / span * plot_w
                x1 = left + (hi_v - lo) / span * plot_w
                parts.append(
                    f'<rect x="{_f(x0)}" y="{_f(top + off)}" '
                    f'width="{_f(max(x1 - x0, 0.5))}" height="{_f(bar_thick * 0.92)}" '
                    f'fill="{s.color}"/>'
                )
                if s.show_values:
                    parts.append(
                        f'<text x="{_f(x1 + 3)}" y="{_f(top + off + bar_thick * 0.7)}" '
                        f'font-size="{_LABEL_FONT}" fill="#404040">{_esc(_fmt_val(v))}</text>'
                    )
            else:
                y0 = bottom - (hi_v - lo) / span * plot_h
                y1 = bottom - (lo_v - lo) / span * plot_h
                parts.append(
                    f'<rect x="{_f(left + off)}" y="{_f(y0)}" '
                    f'width="{_f(bar_thick * 0.92)}" height="{_f(max(y1 - y0, 0.5))}" '
                    f'fill="{s.color}"/>'
                )
                if s.show_values:
                    parts.append(
                        f'<text x="{_f(left + off + bar_thick * 0.46)}" y="{_f(y0 - 3)}" '
                        f'text-anchor="middle" font-size="{_LABEL_FONT}" '
                        f'fill="#404040">{_esc(_fmt_val(v))}</text>'
                    )
    return parts


def _render_lines(series, n, vx, vy) -> list[str]:
    parts: list[str] = []
    for s in series:
        pts = [
            (vx((i + 0.5) / max(n, 1)), vy(v))
            for i, v in enumerate(s.values)
            if v is not None
        ]
        if len(pts) >= 2:
            path = " ".join(f"{_f(px)},{_f(py)}" for px, py in pts)
            parts.append(
                f'<polyline points="{path}" fill="none" stroke="{s.color}" '
                f'stroke-width="2.2" stroke-linejoin="round"/>'
            )
        for px, py in pts:
            parts.append(f'<circle cx="{_f(px)}" cy="{_f(py)}" r="2.6" fill="{s.color}"/>')
        if s.show_values:
            for i, v in enumerate(s.values):
                if v is None:
                    continue
                parts.append(
                    f'<text x="{_f(vx((i + 0.5) / max(n, 1)))}" y="{_f(vy(v) - 5)}" '
                    f'text-anchor="middle" font-size="{_LABEL_FONT}" '
                    f'fill="#404040">{_esc(_fmt_val(v))}</text>'
                )
    return parts


def _render_area(series, n, vx, vy, bottom, *, stacked: bool, pct: bool) -> list[str]:
    parts: list[str] = []
    if stacked:
        offsets = _stack_offsets(series, n, pct)
        for si, s in enumerate(series):
            top_pts = [
                (vx((i + 0.5) / max(n, 1)), vy(offsets[si][i][0] + offsets[si][i][1]))
                for i in range(n)
            ]
            base_pts = [
                (vx((i + 0.5) / max(n, 1)), vy(offsets[si][i][0]))
                for i in range(n)
            ][::-1]
            all_pts = top_pts + base_pts
            path = " ".join(f"{_f(px)},{_f(py)}" for px, py in all_pts)
            parts.append(
                f'<polygon points="{path}" fill="{s.color}" fill-opacity="0.75"/>'
            )
        return parts
    for s in series:
        pts = [
            (vx((i + 0.5) / max(n, 1)), vy(v))
            for i, v in enumerate(s.values)
            if v is not None
        ]
        if len(pts) < 2:
            continue
        path = (
            " ".join(f"{_f(px)},{_f(py)}" for px, py in pts)
            + f" {_f(pts[-1][0])},{_f(bottom)} {_f(pts[0][0])},{_f(bottom)}"
        )
        parts.append(f'<polygon points="{path}" fill="{s.color}" fill-opacity="0.55"/>')
        parts.append(
            "<polyline points=\""
            + " ".join(f"{_f(px)},{_f(py)}" for px, py in pts)
            + f'" fill="none" stroke="{s.color}" stroke-width="2"/>'
        )
    return parts


# ---------------------------------------------------------------------------
# Pie / doughnut
# ---------------------------------------------------------------------------


def _render_pie(
    x, y, w, h, series: _Series, categories: list[str], *, doughnut: bool,
) -> str:
    values = [(i, v) for i, v in enumerate(series.values) if v is not None and v > 0]
    total = sum(v for _, v in values)
    if not values or total <= 0:
        return ""
    cx, cy = x + w / 2, y + h / 2
    radius = max(min(w, h) / 2 - 14.0, 8.0)
    hole = radius * 0.55 if doughnut else 0.0
    parts: list[str] = []
    angle = -math.pi / 2  # 12 o'clock, clockwise
    for slice_i, (i, v) in enumerate(values):
        frac = v / total
        a0, a1 = angle, angle + frac * 2 * math.pi
        angle = a1
        color = "#" + _DEFAULT_ACCENTS[slice_i % len(_DEFAULT_ACCENTS)]
        large = 1 if (a1 - a0) > math.pi else 0
        x0, y0 = cx + radius * math.cos(a0), cy + radius * math.sin(a0)
        x1, y1 = cx + radius * math.cos(a1), cy + radius * math.sin(a1)
        if frac >= 0.999999:  # full circle — arc path degenerates
            parts.append(
                f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(radius)}" fill="{color}"/>'
            )
        elif doughnut:
            xi1, yi1 = cx + hole * math.cos(a1), cy + hole * math.sin(a1)
            xi0, yi0 = cx + hole * math.cos(a0), cy + hole * math.sin(a0)
            parts.append(
                f'<path d="M {_f(x0)} {_f(y0)} '
                f'A {_f(radius)} {_f(radius)} 0 {large} 1 {_f(x1)} {_f(y1)} '
                f'L {_f(xi1)} {_f(yi1)} '
                f'A {_f(hole)} {_f(hole)} 0 {large} 0 {_f(xi0)} {_f(yi0)} Z" '
                f'fill="{color}"/>'
            )
        else:
            parts.append(
                f'<path d="M {_f(cx)} {_f(cy)} L {_f(x0)} {_f(y0)} '
                f'A {_f(radius)} {_f(radius)} 0 {large} 1 {_f(x1)} {_f(y1)} Z" '
                f'fill="{color}"/>'
            )
        # Percentage label outside the slice midpoint
        mid = (a0 + a1) / 2
        lr = radius + 10
        label = f"{frac * 100:.0f}%"
        cat = categories[i] if i < len(categories) else ""
        if cat:
            label = f"{_truncate(str(cat), 12)} {label}"
        anchor = "start" if math.cos(mid) >= 0 else "end"
        parts.append(
            f'<text x="{_f(cx + lr * math.cos(mid))}" y="{_f(cy + lr * math.sin(mid) + 3)}" '
            f'text-anchor="{anchor}" font-size="{_LABEL_FONT}" '
            f'fill="#606060">{_esc(label)}</text>'
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Scatter
# ---------------------------------------------------------------------------


def _render_scatter(x, y, w, h, series: list[_Series]) -> str:
    xs = [v for s in series for v in s.x_values if v is not None]
    ys = [v for s in series for v in s.values if v is not None]
    if not xs or not ys:
        return ""
    x_ticks = _nice_ticks(min(xs), max(xs))
    y_ticks = _nice_ticks(min(ys), max(ys))
    x_lo, x_hi = x_ticks[0], x_ticks[-1]
    y_lo, y_hi = y_ticks[0], y_ticks[-1]
    left = x + 44.0
    bottom = y + h - 18.0
    plot_w = max(w - (left - x) - 8.0, 10.0)
    plot_h = max(bottom - y - 4.0, 10.0)
    top = bottom - plot_h

    def px(v: float) -> float:
        return left + (v - x_lo) / ((x_hi - x_lo) or 1.0) * plot_w

    def py(v: float) -> float:
        return bottom - (v - y_lo) / ((y_hi - y_lo) or 1.0) * plot_h

    parts: list[str] = []
    for t in y_ticks:
        parts.append(
            f'<line x1="{_f(left)}" y1="{_f(py(t))}" x2="{_f(left + plot_w)}" '
            f'y2="{_f(py(t))}" stroke="#E0E0E0"/>'
        )
        parts.append(
            f'<text x="{_f(left - 4)}" y="{_f(py(t) + 3)}" text-anchor="end" '
            f'font-size="{_AXIS_FONT}" fill="#808080">{_esc(_fmt_val(t))}</text>'
        )
    for t in x_ticks:
        parts.append(
            f'<text x="{_f(px(t))}" y="{_f(bottom + 12)}" text-anchor="middle" '
            f'font-size="{_AXIS_FONT}" fill="#808080">{_esc(_fmt_val(t))}</text>'
        )
    parts.append(
        f'<line x1="{_f(left)}" y1="{_f(top)}" x2="{_f(left)}" y2="{_f(bottom)}" stroke="#B0B0B0"/>'
    )
    parts.append(
        f'<line x1="{_f(left)}" y1="{_f(bottom)}" x2="{_f(left + plot_w)}" y2="{_f(bottom)}" stroke="#B0B0B0"/>'
    )
    for s in series:
        for xv, yv in zip(s.x_values, s.values):
            if xv is None or yv is None:
                continue
            parts.append(
                f'<circle cx="{_f(px(xv))}" cy="{_f(py(yv))}" r="3" fill="{s.color}"/>'
            )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------


def _render_legend(x: float, y: float, w: float, series: list[_Series]) -> str:
    parts: list[str] = []
    est = sum(len(s.name) * _LEGEND_FONT * 0.6 + 26 for s in series)
    cur = x + max((w - est) / 2, 0)
    for s in series:
        parts.append(
            f'<rect x="{_f(cur)}" y="{_f(y)}" width="9" height="9" fill="{s.color}"/>'
        )
        label = _truncate(s.name, 24)
        parts.append(
            f'<text x="{_f(cur + 13)}" y="{_f(y + 8)}" font-size="{_LEGEND_FONT}" '
            f'fill="#606060">{_esc(label)}</text>'
        )
        cur += len(label) * _LEGEND_FONT * 0.6 + 26
    return "\n".join(parts)
