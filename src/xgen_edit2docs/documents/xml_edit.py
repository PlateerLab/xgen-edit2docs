"""Direct OOXML XML editing — the universal deterministic primitive (no LLM).

DOCX / XLSX / PPTX are all the same thing: a zip of XML parts. The structured
verbs (``set_doc_text``, ``edit_chart``) cover the common cases with stable
addresses; THIS module is the escape hatch that makes **every other edit**
possible through a tool — colors, fills, fonts, shape geometry, chart styling,
anything OOXML expresses — by reading and patching a part's XML directly.

Three operations, all on xgen_contextifier's byte-preserving raw layer:

* :func:`list_parts`     — every part in the package (name, type, size).
* :func:`get_xml`        — one part's XML text, exactly as stored.
* :func:`apply_xml_edits` — patch a part with exact find/replace edits (or
  replace the whole part), validate the result is still well-formed XML, and
  re-serialize. Untouched parts stay byte-identical.

Find/replace is deliberate: the agent reads the real XML with
:func:`get_xml`, copies exact substrings, and swaps them. No XPath dialect to
learn, no namespace-prefix ambiguity — what you read is what you match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "XmlEdit",
    "XmlEditResult",
    "list_parts",
    "get_xml",
    "apply_xml_edits",
]


@dataclass
class XmlEdit:
    """One exact-substring edit. ``count`` limits replacements (0 = all)."""

    find: str
    replace: str
    count: int = 0


@dataclass
class XmlEditResult:
    find: str
    status: str  # applied | not_found | invalid
    occurrences: int = 0
    message: str = ""


def _open_package(content: bytes):
    from xgen_contextifier.raw.opc import OpcPackage

    return OpcPackage.open(content)


def _is_xml_name(name: str) -> bool:
    return name.endswith(".xml") or name.endswith(".rels")


def list_parts(content: bytes) -> list[dict]:
    """Every part in the package, in stored order.

    Returns ``[{"part", "content_type", "size", "is_xml"}, ...]``. The
    ``part`` value is the address :func:`get_xml` / :func:`apply_xml_edits`
    take (e.g. ``ppt/slides/slide1.xml``, ``ppt/charts/chart1.xml``,
    ``word/document.xml``, ``xl/worksheets/sheet1.xml``).
    """
    pkg = _open_package(content)
    out: list[dict] = []
    for name in pkg.part_names:
        data = pkg.get_part(name).read()
        out.append(
            {
                "part": name,
                "content_type": pkg.content_type_of(name),
                "size": len(data),
                "is_xml": _is_xml_name(name),
            }
        )
    return out


def get_xml(content: bytes, part: str) -> str:
    """One XML part's text, exactly as stored (edits match against THIS).

    Raises ``ValueError`` for a missing part or a binary (non-XML) part —
    the message lists near-miss part names to help an agent self-correct.
    """
    pkg = _open_package(content)
    if not pkg.has_part(part):
        near = [n for n in pkg.part_names if part.rsplit("/", 1)[-1] in n]
        raise ValueError(
            f"no such part: {part!r}."
            + (f" Did you mean one of {near}?" if near else "")
            + " Use list_doc_parts for the full list."
        )
    if not _is_xml_name(part):
        raise ValueError(f"part {part!r} is not XML (binary parts are not editable)")
    return pkg.get_part(part).read().decode("utf-8")


def _validate_xml(text: str, part: str) -> str | None:
    """Return an error message if *text* is not well-formed XML, else None."""
    from lxml import etree

    try:
        etree.fromstring(text.encode("utf-8"))
    except etree.XMLSyntaxError as exc:
        return f"result for {part!r} is not well-formed XML: {exc}"
    return None


def apply_xml_edits(
    content: bytes,
    part: str,
    edits: list[XmlEdit] | None = None,
    *,
    xml: str | None = None,
    content_type: str | None = None,
    delete: bool = False,
) -> tuple[bytes, list[XmlEditResult]]:
    """Patch, create or delete one XML part; returns ``(new_bytes, results)``.

    Modes (exactly one):

    * *edits* — find/replace list against an EXISTING part's text.
    * *xml*   — whole-part replacement; if the part does not exist it is
      **created** (pass *content_type* to register its ``[Content_Types].xml``
      Override — required for new slides/charts so Office accepts the file).
    * *delete=True* — remove the part (remember to also patch the
      referencing ``.rels`` / ``[Content_Types].xml``, which are themselves
      XML parts).

    The final text must parse as XML or NOTHING is written (the package is
    returned unchanged with an ``invalid`` result). Untouched parts stay
    byte-identical (raw-layer contract).
    """
    modes = sum(1 for m in (edits is not None, xml is not None, delete) if m)
    if modes != 1:
        raise ValueError("pass exactly one of `edits`, `xml` or `delete=True`")

    pkg = _open_package(content)
    if not _is_xml_name(part):
        raise ValueError(f"part {part!r} is not XML (binary parts are not editable)")

    if delete:
        if not pkg.has_part(part):
            raise ValueError(f"no such part: {part!r} (use list_doc_parts)")
        pkg.remove_part(part)
        return pkg.to_bytes(), [
            XmlEditResult(find="<delete>", status="applied", occurrences=1)
        ]

    if edits is not None and not pkg.has_part(part):
        raise ValueError(
            f"no such part: {part!r} (use list_doc_parts; to CREATE a new "
            "part pass `xml` instead of `edits`)"
        )

    results: list[XmlEditResult] = []

    if xml is not None:
        err = _validate_xml(xml, part)
        if err:
            return content, [XmlEditResult(find="<xml>", status="invalid", message=err)]
        created = not pkg.has_part(part)
        if created:
            pkg.add_part(part, xml.encode("utf-8"))
        else:
            pkg.get_part(part).write(xml.encode("utf-8"))
        if content_type:
            pkg.set_content_type_override(part, content_type)
        return pkg.to_bytes(), [
            XmlEditResult(
                find="<xml>", status="applied", occurrences=1,
                message="created" if created else "",
            )
        ]

    text = pkg.get_part(part).read().decode("utf-8")
    new_text = text
    applied_any = False
    for edit in edits or []:
        if not edit.find:
            results.append(
                XmlEditResult(edit.find, "invalid", message="find must be non-empty")
            )
            continue
        occurrences = new_text.count(edit.find)
        if occurrences == 0:
            results.append(
                XmlEditResult(
                    edit.find, "not_found",
                    message="substring not found in the part's current text "
                            "(read it again with get_doc_xml — earlier edits "
                            "may have changed it)",
                )
            )
            continue
        limit = edit.count if edit.count and edit.count > 0 else occurrences
        new_text = new_text.replace(edit.find, edit.replace, limit)
        results.append(
            XmlEditResult(edit.find, "applied", occurrences=min(occurrences, limit))
        )
        applied_any = True

    if not applied_any:
        return content, results

    err = _validate_xml(new_text, part)
    if err:
        # Refuse to corrupt the document: report and keep the original bytes.
        return content, [
            *[r for r in results if r.status != "applied"],
            *[
                XmlEditResult(r.find, "invalid", message=err)
                for r in results
                if r.status == "applied"
            ],
        ]

    pkg.get_part(part).write(new_text.encode("utf-8"))
    return pkg.to_bytes(), results


def coerce_edit(e: dict[str, Any]) -> XmlEdit:
    """Dict → XmlEdit (tolerant of missing count)."""
    return XmlEdit(
        find=str(e.get("find", "")),
        replace=str(e.get("replace", "")),
        count=int(e.get("count", 0) or 0),
    )
