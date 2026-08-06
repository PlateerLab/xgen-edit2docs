"""Deterministic native-chart editing across DOCX / XLSX / PPTX (no LLM).

Charts are the same DrawingML object regardless of host format, so this
sits directly on xgen_contextifier's raw ``ChartModel`` — one code path edits
a bar chart's data whether it lives in a Word report, an Excel workbook
or a PowerPoint slide. Untouched parts of the package stay byte-identical
(the raw layer's byte-preservation contract); only the chart XML and its
embedded workbook change.

Two operations:

* ``set_chart_title`` — ``{"chart": i, "title": "..."}``
* ``set_chart_data``  — ``{"chart": i, "categories": [...],
  "series": [{"name": "...", "values": [...]}, ...]}``

Chart addresses (``chart: i``) come from :func:`list_charts`, which the
``analyze_doc`` outline also surfaces.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ChartEdit",
    "ChartEditResult",
    "list_charts",
    "apply_chart_edits",
]


@dataclass
class ChartEdit:
    """One chart operation. ``action`` = ``set_data`` | ``set_title``."""

    action: str
    chart: int = 0
    title: str | None = None
    categories: list[str] | None = None
    series: list[dict] | None = None  # [{"name": str, "values": [num, ...]}, ...]


@dataclass
class ChartEditResult:
    action: str
    chart: int
    status: str  # applied | not_found | invalid
    message: str = ""


def _charts_of(raw, fmt: str) -> list:
    """Every ChartModel in the document, in a stable order.

    XLSX exposes ``raw.charts`` directly; DOCX likewise; PPTX charts live
    per-slide, flattened here in slide order so a single ``chart`` index
    addresses the whole deck.
    """
    if fmt == "pptx":
        charts: list = []
        for slide in raw.slides:
            charts.extend(slide.charts)
        return charts
    return list(raw.charts)


def list_charts(content: bytes, fmt: str) -> list[dict]:
    """Read-only chart summaries (best-effort; never raises).

    Returns ``[{"chart": i, "kind", "title", "series": [{"name",
    "categories", "values"}...]}, ...]``. Used by ``analyze_doc`` and as
    the address source for :func:`apply_chart_edits`.
    """
    try:
        from xgen_contextifier import open_raw

        raw = open_raw(content, extension=fmt)
        out: list[dict] = []
        for i, chart in enumerate(_charts_of(raw, fmt)):
            try:
                out.append(
                    {
                        "chart": i,
                        "kind": chart.kind,
                        "title": chart.title,
                        "series": [
                            {
                                "name": s.name,
                                "categories": list(s.categories),
                                "values": list(s.values),
                            }
                            for s in chart.series
                        ],
                    }
                )
            except Exception:
                out.append({"chart": i, "kind": None, "title": None, "series": []})
        return out
    except Exception:
        return []


def apply_chart_edits(
    content: bytes, fmt: str, edits: Iterable[ChartEdit]
) -> tuple[bytes, list[ChartEditResult]]:
    """Apply chart edits; returns ``(new_bytes, results)``.

    Per-edit soft failures (``not_found`` / ``invalid``) mirror the other
    deterministic editors. The package is only re-serialized when at least
    one edit applied; untouched parts stay byte-identical.
    """
    from xgen_contextifier import open_raw
    from xgen_contextifier.raw.opc import RawUnsupportedError

    raw = open_raw(content, extension=fmt)
    charts = _charts_of(raw, fmt)
    results: list[ChartEditResult] = []
    applied_any = False

    for edit in edits:
        if not (0 <= edit.chart < len(charts)):
            results.append(
                ChartEditResult(
                    edit.action, edit.chart, "not_found",
                    f"chart {edit.chart} out of range (0..{len(charts) - 1})",
                )
            )
            continue
        chart = charts[edit.chart]
        try:
            if edit.action == "set_title":
                if not edit.title:
                    results.append(
                        ChartEditResult(edit.action, edit.chart, "invalid",
                                        "set_title needs a non-empty title")
                    )
                    continue
                chart.set_title(edit.title)
            elif edit.action == "set_data":
                if edit.categories is None or edit.series is None:
                    results.append(
                        ChartEditResult(edit.action, edit.chart, "invalid",
                                        "set_data needs categories and series")
                    )
                    continue
                series = [
                    (s.get("name"), list(s.get("values") or []))
                    for s in edit.series
                ]
                chart.set_data(categories=list(edit.categories), series=series)
            else:
                results.append(
                    ChartEditResult(edit.action, edit.chart, "invalid",
                                    f"unknown action {edit.action!r}")
                )
                continue
        except RawUnsupportedError as exc:
            results.append(
                ChartEditResult(edit.action, edit.chart, "invalid",
                                f"unsupported for this chart: {exc}")
            )
            continue
        except ValueError as exc:
            results.append(
                ChartEditResult(edit.action, edit.chart, "invalid", str(exc))
            )
            continue
        results.append(ChartEditResult(edit.action, edit.chart, "applied"))
        applied_any = True

    new_content = raw.to_bytes() if applied_any else content
    return new_content, results


def _coerce_edit(e: dict[str, Any]) -> ChartEdit:
    """Dict -> ChartEdit, defaulting action from the fields present."""
    action = e.get("action")
    if action is None:
        action = "set_title" if "title" in e and "categories" not in e else "set_data"
    return ChartEdit(
        action=action,
        chart=int(e.get("chart", 0)),
        title=e.get("title"),
        categories=e.get("categories"),
        series=e.get("series"),
    )
