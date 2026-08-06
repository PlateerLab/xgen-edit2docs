"""Recompose a PPTX: keep / replace / insert / delete / reorder slides.

The chat-editing pipeline (``tools/edit_deck``) reduces every edit to one
primitive: the final deck is an ordered *sequence* whose entries are either
an original slide (kept as-is, identity preserved) or a freshly generated
SVG slide. That single primitive covers:

* replace slide N   -> sequence swaps ``KeepSlide(N)`` for a ``NewSlide``
* insert after N    -> ``NewSlide`` spliced into the sequence
* delete slide N    -> ``KeepSlide(N)`` simply absent
* reorder           -> permuted ``KeepSlide`` entries

Original slides not present in the sequence are physically removed from
the package (parts, rels, notesSlides, content-type overrides). Kept
slides keep their part names, slide ids, notes and animations untouched.

Native-content protection (P0-1): a ``NewSlide`` that *replaces* an
original slide would historically flatten that slide's native charts /
tables / diagrams into drawn shapes (the SVG regen can only draw) and
leave the chart parts orphaned in the package. With
``preserve_native=True`` (default) the original slide's chart / table /
diagram graphicFrames are carried into the regenerated slide — original
geometry untouched, relationship ids renumbered past the new slide's own
rels — so native objects stay native. Parts reachable only from dropped
or replaced slides that nothing references afterwards are swept
(reference-counted, transitively — mirrors xgen_contextifier's
``remove_slide`` semantics), so deletes no longer leak orphan chart /
embedding parts.

Same raw-OOXML style as ``pptx_append`` (string/regex surgery on the
extracted package), and it reuses that module's helpers.
"""

from __future__ import annotations

import copy
import posixpath
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .pptx_append import (
    SLIDE_CONTENT_TYPE,
    SLIDE_REL_TYPE,
    AppendError,
    _add_default_content_type,
    _add_override,
    _existing_slides,
    _next_free_sld_id,
    _next_free_slide_number,
    _pick_blankest_layout,
    _remove_override,
    remove_slide_parts,
    write_slide_part_from_svg,
)
from .pptx_builder import _append_relationship, _content_type_for_extension

_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_PA_NS = {"p": _NS_P, "a": _NS_A}

#: graphicData/@uri tail -> native object kind carried through a regen
_NATIVE_GRAPHIC_KINDS = {
    "table": "table",
    "chart": "chart",
    "chartex": "chart",
    "diagram": "diagram",
}


@dataclass(frozen=True)
class KeepSlide:
    """An original slide, addressed by its 0-based position in the current deck."""

    index: int


@dataclass(frozen=True)
class NewSlide:
    """A freshly generated slide rendered from *svg_path*.

    ``replaces`` is the 0-based index of the original slide this one
    regenerates (an "edit" op), or ``None`` for a brand-new insertion.
    It drives native-content preservation: chart/table/diagram
    graphicFrames of the replaced slide are carried into this one.
    """

    svg_path: Path
    replaces: int | None = None


def recompose_pptx(
    host_pptx: Path,
    sequence: list[KeepSlide | NewSlide],
    output_path: Path,
    *,
    preserve_native: bool = True,
    transition: str | None = "fade",
    transition_duration: float = 0.5,
    verbose: bool = False,
) -> list[dict]:
    """Rebuild *host_pptx* so its slides are exactly *sequence*, in order.

    With ``preserve_native`` (default), a ``NewSlide`` whose ``replaces``
    points at an original slide carries that slide's native chart / table /
    diagram graphicFrames into the regenerated slide (see module docstring).

    Returns non-fatal warnings as ``{"code", "message"[, "detail"]}`` dicts.

    Raises:
        ValueError: empty sequence, out-of-range or duplicated KeepSlide
            index, or an out-of-range ``NewSlide.replaces``.
        AppendError: the host package is not a usable PPTX.
    """
    if not sequence:
        raise ValueError("recompose_pptx requires a non-empty slide sequence")

    warnings: list[dict] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="xgen_edit2docs-recompose-"))
    try:
        extract_dir = temp_dir / "pptx_content"
        with zipfile.ZipFile(host_pptx, "r") as zf:
            zf.extractall(extract_dir)

        presentation_path = extract_dir / "ppt" / "presentation.xml"
        presentation_rels_path = extract_dir / "ppt" / "_rels" / "presentation.xml.rels"
        content_types_path = extract_dir / "[Content_Types].xml"
        if not presentation_path.exists() or not presentation_rels_path.exists():
            raise AppendError("invalid PPTX: missing ppt/presentation.xml or its rels")

        presentation_xml = presentation_path.read_text(encoding="utf-8")
        originals = _ordered_slides(presentation_xml, presentation_rels_path)

        keep_indices = [e.index for e in sequence if isinstance(e, KeepSlide)]
        if len(set(keep_indices)) != len(keep_indices):
            raise ValueError("recompose_pptx: duplicate KeepSlide index in sequence")
        for idx in keep_indices:
            if idx < 0 or idx >= len(originals):
                raise ValueError(
                    f"recompose_pptx: KeepSlide({idx}) out of range — deck has "
                    f"{len(originals)} slides"
                )
        for e in sequence:
            if isinstance(e, NewSlide) and e.replaces is not None and not (
                0 <= e.replaces < len(originals)
            ):
                raise ValueError(
                    f"recompose_pptx: NewSlide(replaces={e.replaces}) out of range — "
                    f"deck has {len(originals)} slides"
                )

        # Everything transitively reachable from an original slide that is
        # about to be dropped or replaced is an orphan *candidate* —
        # collected up front, while every rels part is still on disk.
        # Reference counting after the rebuild decides what actually goes.
        kept = set(keep_indices)
        orphan_candidates: set[str] = set()
        for idx, (_sld_id, _rel_id, target) in enumerate(originals):
            if idx not in kept:
                part = posixpath.normpath(posixpath.join("ppt", target))
                orphan_candidates |= _transitive_internal_targets(extract_dir, part)

        layout_rel_target = _pick_blankest_layout(extract_dir)
        slides_dir = extract_dir / "ppt" / "slides"
        next_slide_num = _next_free_slide_number(
            [p.name for p in slides_dir.glob("slide*.xml")] if slides_dir.exists() else []
        )
        next_sld_id = _next_free_sld_id(presentation_xml)

        media_cache: dict[tuple[str, str], str] = {}
        image_exts_used: set[str] = set()

        # 1. Materialise every NewSlide as a package part + presentation rel.
        entries: list[str] = []  # final <p:sldId .../> entries, in order
        for item in sequence:
            if isinstance(item, KeepSlide):
                sld_id, rel_id, _target = originals[item.index]
                entries.append(f'<p:sldId id="{sld_id}" r:id="{rel_id}"/>')
                continue
            slide_num = next_slide_num
            next_slide_num += 1
            write_slide_part_from_svg(
                extract_dir,
                item.svg_path,
                slide_num=slide_num,
                layout_rel_target=layout_rel_target,
                media_cache=media_cache,
                image_exts_used=image_exts_used,
                transition=transition,
                transition_duration=transition_duration,
                verbose=verbose,
            )
            rid = _append_relationship(
                presentation_rels_path, SLIDE_REL_TYPE, f"slides/slide{slide_num}.xml"
            )
            _add_override(
                content_types_path, f"/ppt/slides/slide{slide_num}.xml", SLIDE_CONTENT_TYPE
            )
            entries.append(f'<p:sldId id="{next_sld_id}" r:id="{rid}"/>')
            next_sld_id += 1

            # Native-content protection: carry the replaced slide's chart /
            # table / diagram graphicFrames into the regenerated slide.
            if preserve_native and item.replaces is not None:
                preserved = _carry_native_frames(
                    extract_dir,
                    original_target=originals[item.replaces][2],
                    slide_num=slide_num,
                )
                if preserved:
                    warnings.append(
                        {
                            "code": "native_objects_preserved",
                            "message": (
                                f"Slide {item.replaces + 1}: kept native objects "
                                "through regeneration instead of flattening them: "
                                f"{', '.join(preserved)}."
                            ),
                            "detail": {
                                "slide": item.replaces + 1,
                                "preserved": preserved,
                            },
                        }
                    )

        # 2. Rewrite the whole sldIdLst in sequence order.
        presentation_xml = presentation_path.read_text(encoding="utf-8")
        presentation_path.write_text(
            _replace_sld_id_lst(presentation_xml, "".join(entries)), encoding="utf-8"
        )

        # 3. Physically remove originals that were dropped from the deck.
        for idx, (_sld_id, rel_id, target) in enumerate(originals):
            if idx not in kept:
                remove_slide_parts(extract_dir, rel_id=rel_id, target=target)

        # 3b. Sweep parts only the dropped/replaced slides pulled in and
        # nothing references any more (charts, embedded workbooks, media,
        # diagram data, ...). Carried-over frames re-anchor their parts via
        # the new slide's rels, so they survive the reference count.
        if orphan_candidates:
            _sweep_orphan_parts(extract_dir, orphan_candidates)

        # 4. Content-type defaults for any media the new slides brought in.
        if image_exts_used:
            content_types = content_types_path.read_text(encoding="utf-8")
            for ext in sorted(image_exts_used):
                content_types = _add_default_content_type(
                    content_types, ext, _content_type_for_extension(ext)
                )
            content_types_path.write_text(content_types, encoding="utf-8")

        # 5. Repackage atomically.
        temp_output = temp_dir / "result.pptx"
        with zipfile.ZipFile(temp_output, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in extract_dir.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, file_path.relative_to(extract_dir))
        shutil.move(str(temp_output), str(output_path))
        return warnings
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _ordered_slides(
    presentation_xml: str, presentation_rels_path: Path
) -> list[tuple[str, str, str]]:
    """Return ``(sld_id, rel_id, target)`` in sldIdLst (deck) order."""
    rel_targets = dict(
        (rel_id, target) for rel_id, target in _existing_slides(presentation_rels_path)
    )
    ordered: list[tuple[str, str, str]] = []
    for match in re.finditer(
        r"<p:sldId\b[^>]*\bid=\"(\d+)\"[^>]*\br:id=\"([^\"]+)\"[^>]*/>", presentation_xml
    ):
        sld_id, rel_id = match.group(1), match.group(2)
        target = rel_targets.get(rel_id)
        if target is not None:
            ordered.append((sld_id, rel_id, target))
    return ordered


def _native_graphic_frames(slide_root) -> list[tuple[object, str]]:
    """``(graphicFrame element, kind)`` for every native chart / table /
    diagram on the slide, in document order, descending into groups."""
    sp_tree = slide_root.find("p:cSld/p:spTree", _PA_NS)
    if sp_tree is None:
        return []
    out: list[tuple[object, str]] = []

    def walk(container) -> None:
        for child in container:
            if child.tag == f"{{{_NS_P}}}graphicFrame":
                data = child.find("a:graphic/a:graphicData", _PA_NS)
                uri_tail = (
                    (data.get("uri") or "").rsplit("/", 1)[-1] if data is not None else ""
                )
                kind = _NATIVE_GRAPHIC_KINDS.get(uri_tail)
                if kind:
                    out.append((child, kind))
            elif child.tag == f"{{{_NS_P}}}grpSp":
                walk(child)

    walk(sp_tree)
    return out


def _carry_native_frames(
    extract_dir: Path, *, original_target: str, slide_num: int
) -> list[str]:
    """Re-insert *original_target*'s native graphicFrames into the freshly
    written ``slide{slide_num}``.

    Frames are deep-copied with their geometry (``p:xfrm``) untouched, so
    they land exactly where they were. Chart/diagram relationship ids are
    renumbered past the new slide's own rels (the SVG conversion already
    allocated rIds for its images) and the original relationship entries
    are appended to the new slide's rels part under the new ids — both
    slides live in ``ppt/slides/``, so relative targets stay valid. Tables
    (``a:tbl``) carry no relationships and copy as pure XML.

    Returns descriptions of what was carried, e.g.
    ``["chart:chart1.xml", "table"]``; empty when the original slide had
    no native frames (the new slide's parts are then left byte-untouched).
    """
    orig_part = posixpath.normpath(posixpath.join("ppt", original_target))
    orig_path = extract_dir / orig_part
    if not orig_path.is_file():
        return []
    frames = _native_graphic_frames(etree.fromstring(orig_path.read_bytes()))
    if not frames:
        return []

    new_slide_path = extract_dir / "ppt" / "slides" / f"slide{slide_num}.xml"
    new_rels_path = (
        extract_dir / "ppt" / "slides" / "_rels" / f"slide{slide_num}.xml.rels"
    )
    new_root = etree.fromstring(new_slide_path.read_bytes())
    sp_tree = new_root.find("p:cSld/p:spTree", _PA_NS)
    if sp_tree is None:  # converter always writes one; belt and braces
        return []

    orig_rels: dict[str, dict] = {
        rel["id"]: rel
        for rel in _iter_rel_entries(_rels_path_for(extract_dir, orig_part))
    }
    rels_root = etree.fromstring(new_rels_path.read_bytes())
    used_rids = {rel.get("Id") for rel in rels_root}

    def next_rid() -> str:
        n = 1
        while f"rId{n}" in used_rids:
            n += 1
        rid = f"rId{n}"
        used_rids.add(rid)
        return rid

    next_shape_id = (
        max(
            (
                int(el.get("id"))
                for el in new_root.iter(f"{{{_NS_P}}}cNvPr")
                if (el.get("id") or "").isdigit()
            ),
            default=1,
        )
        + 1
    )

    rid_map: dict[str, str] = {}  # original rId -> rId in the new slide
    preserved: list[str] = []
    for frame, kind in frames:
        clone = copy.deepcopy(frame)
        desc = kind
        # Renumber every r:* relationship reference in the carried frame
        # (chart r:id, diagram relIds r:dm/r:lo/r:qs/r:cs, ...) and copy the
        # original relationship entries over under the new ids.
        for node in clone.iter():
            for attr, value in list(node.attrib.items()):
                if not attr.startswith(f"{{{_NS_R}}}"):
                    continue
                src = orig_rels.get(value)
                if src is None:
                    continue
                if kind == "chart" and desc == "chart" and (src["type"] or "").endswith(
                    "/chart"
                ):
                    desc = f"chart:{posixpath.basename(src['target'] or '')}"
                new_rid = rid_map.get(value)
                if new_rid is None:
                    new_rid = next_rid()
                    rid_map[value] = new_rid
                    rel_el = etree.SubElement(rels_root, f"{{{_NS_REL}}}Relationship")
                    rel_el.set("Id", new_rid)
                    rel_el.set("Type", src["type"])
                    rel_el.set("Target", src["target"])
                    if src["mode"] == "External":
                        rel_el.set("TargetMode", "External")
                node.set(attr, new_rid)
        for cnvpr in clone.iter(f"{{{_NS_P}}}cNvPr"):
            cnvpr.set("id", str(next_shape_id))
            next_shape_id += 1
        sp_tree.append(clone)
        preserved.append(desc)

    new_slide_path.write_bytes(
        etree.tostring(new_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    )
    new_rels_path.write_bytes(
        etree.tostring(rels_root, xml_declaration=True, encoding="UTF-8", standalone=True)
    )
    return preserved


# -- orphan sweep (reference-counted part removal) ---------------------------


def _rels_path_for(extract_dir: Path, part: str) -> Path:
    """The on-disk ``_rels`` file for a package part name."""
    directory, base = posixpath.split(part)
    return extract_dir / directory / "_rels" / f"{base}.rels"


def _iter_rel_entries(rels_file: Path) -> list[dict]:
    """Relationship dicts (id/type/target/mode) of one rels part, or []."""
    if not rels_file.is_file():
        return []
    try:
        root = etree.fromstring(rels_file.read_bytes())
    except etree.XMLSyntaxError:
        return []
    return [
        {
            "id": rel.get("Id"),
            "type": rel.get("Type"),
            "target": rel.get("Target"),
            "mode": rel.get("TargetMode", "Internal"),
        }
        for rel in root
        if rel.tag == f"{{{_NS_REL}}}Relationship"
    ]


def _resolve_rel_target(base_part: str, target: str) -> str:
    """Resolve a relationship target to an absolute part name."""
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _transitive_internal_targets(extract_dir: Path, start_part: str) -> set[str]:
    """Every part reachable from *start_part* through the rels graph."""
    seen = {start_part}
    stack = [start_part]
    reachable: set[str] = set()
    while stack:
        src = stack.pop()
        for rel in _iter_rel_entries(_rels_path_for(extract_dir, src)):
            if rel["mode"] == "External" or not rel["target"]:
                continue
            target = _resolve_rel_target(src, rel["target"])
            if target not in seen:
                seen.add(target)
                reachable.add(target)
                stack.append(target)
    return reachable


def _referenced_parts(extract_dir: Path) -> set[str]:
    """Internal targets of every rels part currently on disk."""
    referenced: set[str] = set()
    for rels_file in extract_dir.rglob("*.rels"):
        owner_dir = rels_file.parent.parent.relative_to(extract_dir).as_posix()
        owner_base = rels_file.name[: -len(".rels")]
        if owner_dir == ".":
            owner_dir = ""
        owner = posixpath.join(owner_dir, owner_base) if owner_base else owner_dir
        for rel in _iter_rel_entries(rels_file):
            if rel["mode"] == "External" or not rel["target"]:
                continue
            referenced.add(_resolve_rel_target(owner, rel["target"]))
    return referenced


def _sweep_orphan_parts(extract_dir: Path, candidates: set[str]) -> list[str]:
    """Delete *candidates* nothing references, to a fixpoint.

    Removing a chart un-anchors its embedded workbook / colors / style
    parts, so the reference count is recomputed until stable — the same
    semantics as xgen_contextifier's ``PptxRawDocument.remove_slide``. Deleted
    parts lose their rels file and their [Content_Types].xml override.
    """
    content_types_path = extract_dir / "[Content_Types].xml"
    removed: list[str] = []
    while True:
        referenced = _referenced_parts(extract_dir)
        orphans = [
            c
            for c in sorted(candidates)
            if c not in referenced and (extract_dir / c).is_file()
        ]
        if not orphans:
            break
        for name in orphans:
            (extract_dir / name).unlink()
            removed.append(name)
            rels_file = _rels_path_for(extract_dir, name)
            if rels_file.is_file():
                rels_file.unlink()
            candidates.discard(name)
    for name in removed:
        _remove_override(content_types_path, f"/{name}")
    return removed


def _replace_sld_id_lst(presentation_xml: str, inner: str) -> str:
    """Swap the sldIdLst body for *inner* (handles missing/self-closing lists)."""
    block = f"<p:sldIdLst>{inner}</p:sldIdLst>"
    if re.search(r"<p:sldIdLst\b[^>]*>.*?</p:sldIdLst>", presentation_xml, re.DOTALL):
        return re.sub(
            r"<p:sldIdLst\b[^>]*>.*?</p:sldIdLst>",
            block,
            presentation_xml,
            count=1,
            flags=re.DOTALL,
        )
    if re.search(r"<p:sldIdLst\s*/>", presentation_xml):
        return re.sub(r"<p:sldIdLst\s*/>", block, presentation_xml, count=1)
    if "<p:sldSz" in presentation_xml:
        return presentation_xml.replace("<p:sldSz", f"{block}<p:sldSz", 1)
    raise AppendError("invalid PPTX: presentation.xml has no sldIdLst/sldSz anchor")
