"""arrange — deterministic structural operations on a document.

Whole slides (.pptx) and sheets (.xlsx) treated as movable, copyable,
deletable objects, on top of xgen_contextifier's raw byte-preserving layer.
No LLM, no Anthropic key, no rendering — pure package surgery.

One op vocabulary, applicability per format:

* ``duplicate`` — pptx slide (independent copy, charts/notes cloned,
  images shared) / xlsx sheet (needs ``name``).
* ``move``      — pptx slide / xlsx sheet, to tab/position ``to``.
* ``delete``    — pptx slide / xlsx sheet (orphan-swept).
* ``rename``    — xlsx sheet only (needs ``name``).

Ops apply IN SEQUENCE; each ``target`` / ``to`` resolves against the
CURRENT document state (after prior ops). The batch is best-effort: one
bad op records a per-op ``status`` and the rest continue (mirrors
``apply_chart_edits``), so a single typo never aborts the whole call.
"""

from __future__ import annotations

import re

# per-op status values
_APPLIED = "applied"
_INVALID = "invalid"  # malformed op (bad/missing field, wrong format)
_NOT_FOUND = "not_found"  # target slide/sheet does not exist
_REFUSED = "refused"  # valid but disallowed (e.g. delete the last sheet)

_PPTX_OPS = {"duplicate", "move", "delete"}
_XLSX_OPS = {"duplicate", "move", "delete", "rename"}


def apply_arrange(
    content: bytes, fmt: str, ops: list[dict]
) -> tuple[bytes, list[dict], list[dict]]:
    """Apply structural *ops* to *content* (a ``fmt`` document).

    Returns ``(new_bytes, results, warnings)``. ``new_bytes`` is the
    serialized package after every successful op; ``results`` is one dict
    per op ``{op, target, to?, name?, status, message}``; ``warnings`` is
    a list of ``{code, message}`` (e.g. a rename that leaves formula
    references dangling)."""
    from xgen_contextifier import open_raw

    results: list[dict] = []
    warnings: list[dict] = []

    if fmt == "docx":
        # DOCX is a flow document — no part-per-page structure. (Phase 2:
        # arrange over heading-delimited block ranges.)
        for op in ops:
            results.append(
                _result(op, _INVALID, "docx has no slide/sheet structure to arrange")
            )
        return content, results, warnings

    raw = open_raw(content, extension=fmt)
    for op in ops:
        if fmt == "pptx":
            results.append(_apply_pptx(raw, op))
        else:  # xlsx
            results.append(_apply_xlsx(raw, op, warnings))

    applied = any(r["status"] == _APPLIED for r in results)
    return (raw.to_bytes() if applied else content), results, warnings


# -- pptx ---------------------------------------------------------------------


def _apply_pptx(raw, op: dict) -> dict:
    action = str(op.get("op") or "").strip().lower()
    if action not in _PPTX_OPS:
        return _result(op, _INVALID, f"unknown pptx op {action!r} (use: {sorted(_PPTX_OPS)})")
    target = op.get("target")
    if not isinstance(target, int) or isinstance(target, bool):
        return _result(op, _INVALID, "pptx target must be an integer slide index")
    try:
        if action == "duplicate":
            new_index = raw.duplicate_slide(target, at=op.get("to"))
            return _result(op, _APPLIED, f"duplicated slide {target} -> index {new_index}")
        if action == "move":
            to = op.get("to")
            if not isinstance(to, int) or isinstance(to, bool):
                return _result(op, _INVALID, "move requires an integer 'to' position")
            raw.move_slide(target, to)
            return _result(op, _APPLIED, f"moved slide {target} -> {to}")
        # delete
        raw.remove_slide(target)
        return _result(op, _APPLIED, f"deleted slide {target}")
    except IndexError as exc:
        return _result(op, _NOT_FOUND, str(exc))
    except ValueError as exc:
        return _result(op, _REFUSED, str(exc))


# -- xlsx ---------------------------------------------------------------------


def _apply_xlsx(raw, op: dict, warnings: list[dict]) -> dict:
    action = str(op.get("op") or "").strip().lower()
    if action not in _XLSX_OPS:
        return _result(op, _INVALID, f"unknown xlsx op {action!r} (use: {sorted(_XLSX_OPS)})")
    target = op.get("target")
    if not isinstance(target, (int, str)) or isinstance(target, bool):
        return _result(op, _INVALID, "xlsx target must be a sheet name or index")
    try:
        if action == "duplicate":
            name = op.get("name")
            if not isinstance(name, str) or not name:
                return _result(op, _INVALID, "duplicate requires a 'name' for the new sheet")
            raw.copy_sheet(target, name, at=op.get("to"))
            return _result(op, _APPLIED, f"copied sheet {target!r} -> {name!r}")
        if action == "move":
            to = op.get("to")
            if not isinstance(to, int) or isinstance(to, bool):
                return _result(op, _INVALID, "move requires an integer 'to' position")
            raw.move_sheet(target, to)
            return _result(op, _APPLIED, f"moved sheet {target!r} -> {to}")
        if action == "rename":
            name = op.get("name")
            if not isinstance(name, str) or not name:
                return _result(op, _INVALID, "rename requires a 'name'")
            old = _sheet_name(raw, target)
            raw.rename_sheet(target, name)
            if old is not None and _name_referenced(raw, old):
                warnings.append(
                    {
                        "code": "rename_refs_not_rewritten",
                        "message": (
                            f"formulas or defined names still reference {old!r}; "
                            "v1 renames the tab only (references are not rewritten)"
                        ),
                    }
                )
            return _result(op, _APPLIED, f"renamed sheet {target!r} -> {name!r}")
        # delete
        raw.delete_sheet(target)
        return _result(op, _APPLIED, f"deleted sheet {target!r}")
    except (IndexError, KeyError) as exc:
        return _result(op, _NOT_FOUND, str(exc))
    except ValueError as exc:
        return _result(op, _REFUSED, str(exc))


def _sheet_name(raw, target) -> str | None:
    try:
        name, _ = raw._resolve_sheet(target)
        return name
    except (IndexError, KeyError):
        return None


def _name_referenced(raw, name: str) -> bool:
    """Best-effort: does any defined name or worksheet formula mention the
    (now-renamed) sheet, as ``Name!`` or ``'Name'!``?"""
    needles = (f"{name}!".encode(), f"'{name}'!".encode())
    for value in raw.defined_names.values():
        if f"{name}!" in value or f"'{name}'!" in value:
            return True
    for part in raw.package.part_names:
        if part.startswith("xl/worksheets/") and part.endswith(".xml"):
            data = raw.package.get_part(part).read()
            if any(n in data for n in needles):
                return True
    return False


# -- shared -------------------------------------------------------------------

_SAFE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _result(op: dict, status: str, message: str = "") -> dict:
    out = {"op": op.get("op"), "target": op.get("target"), "status": status}
    if "to" in op:
        out["to"] = op["to"]
    if "name" in op:
        out["name"] = op["name"]
    if message:
        out["message"] = _SAFE.sub("", message)
    return out
