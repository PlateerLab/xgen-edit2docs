"""Append generated slides into a user-provided PPTX (template modes).

``create_pptx_with_native_svg`` (pptx_builder) always builds a fresh
package from python-pptx's default template. Template modes instead keep
the *user's* package — slide masters, layouts, theme, fonts, media — and
splice newly generated slides into it at the OOXML level:

* ``template_extend``  — original slides stay; new slides are appended.
* ``template_restyle`` — original slides are removed after the append,
  yielding a fresh deck that still inherits the template's masters (so
  background chrome / logos render behind every generated slide).

Everything is done with zipfile + string/regex surgery on the package
parts — the same style pptx_builder uses — rather than python-pptx
mutation. python-pptx's ``add_slide`` allocates part names by slide
*count*, which collides on decks with numbering gaps (PowerPoint keeps
``slide1.xml, slide3.xml`` after a deletion); raw allocation from the
max existing part number is gap-safe.

docProps/app.xml statistics (slide count, titles) are intentionally left
stale — PowerPoint recomputes them on save and never validates them.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from .drawingml_converter import convert_svg_to_slide_shapes
from .pptx_builder import (
    _add_default_content_type,
    _append_relationship,
    _content_type_for_extension,
    _ensure_notes_master,
)
from .pptx_notes import create_notes_slide_rels_xml, create_notes_slide_xml

try:
    from pptx_animations import create_transition_xml
except ImportError:  # optional dependency, mirrors pptx_builder
    create_transition_xml = None

SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
NOTES_SLIDE_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
)
SLIDE_LAYOUT_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
)
SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
)
NOTES_SLIDE_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
)


class AppendError(RuntimeError):
    """The host package cannot accept appended slides."""


def append_svg_slides_to_pptx(
    host_pptx: Path,
    svg_files: list[Path],
    output_path: Path,
    *,
    clear_existing: bool = False,
    notes: dict[str, str] | None = None,
    enable_notes: bool = True,
    lang: str | None = None,
    transition: str | None = "fade",
    transition_duration: float = 0.5,
    verbose: bool = False,
) -> list[dict]:
    """Splice *svg_files* into *host_pptx* as native-shape slides.

    Args:
        host_pptx: The user-provided template/deck package.
        svg_files: Rendered slide SVGs, in deck order.
        output_path: Where the resulting package is written.
        clear_existing: Remove the host's original slides after appending
            (template_restyle). Their media stay — layouts/masters may
            share them.
        notes: Speaker notes keyed by SVG stem (markdown-ish plain text).
        enable_notes: Master switch for notes embedding.
        lang: BCP-47 locale threaded into notes rPr blocks.
        transition: Slide transition effect applied to appended slides.
        transition_duration: Transition duration in seconds.
        verbose: Print per-slide progress.

    Returns:
        Non-fatal warnings as ``{"code": ..., "message": ...}`` dicts.

    Raises:
        AppendError: invalid host package (no presentation.xml / layouts).
        ValueError: *svg_files* is empty.
    """
    if not svg_files:
        raise ValueError("append_svg_slides_to_pptx requires at least one slide")

    warnings: list[dict] = []
    temp_dir = Path(tempfile.mkdtemp(prefix="xgen_edit2docs-append-"))
    try:
        extract_dir = temp_dir / "pptx_content"
        with zipfile.ZipFile(host_pptx, "r") as zf:
            zf.extractall(extract_dir)

        presentation_path = extract_dir / "ppt" / "presentation.xml"
        presentation_rels_path = extract_dir / "ppt" / "_rels" / "presentation.xml.rels"
        content_types_path = extract_dir / "[Content_Types].xml"
        if not presentation_path.exists() or not presentation_rels_path.exists():
            raise AppendError("invalid PPTX: missing ppt/presentation.xml or its rels")

        slides_dir = extract_dir / "ppt" / "slides"
        slides_dir.mkdir(exist_ok=True)
        (slides_dir / "_rels").mkdir(exist_ok=True)
        media_dir = extract_dir / "ppt" / "media"
        media_dir.mkdir(exist_ok=True)

        original_slides = _existing_slides(presentation_rels_path)
        layout_rel_target = _pick_blankest_layout(extract_dir)

        next_slide_num = _next_free_slide_number(
            [p.name for p in slides_dir.glob("slide*.xml")]
        )
        next_notes_num = _next_free_slide_number(
            [
                p.name
                for p in (extract_dir / "ppt" / "notesSlides").glob("notesSlide*.xml")
            ]
            if (extract_dir / "ppt" / "notesSlides").exists()
            else [],
            prefix="notesSlide",
        )
        next_sld_id = _next_free_sld_id(presentation_path.read_text(encoding="utf-8"))

        has_notes_master = (
            extract_dir / "ppt" / "notesMasters" / "notesMaster1.xml"
        ).exists()
        notes = notes or {}
        if enable_notes and notes and not has_notes_master:
            # Upstream f43e8644/767332d1 taught the builder to materialize a
            # PowerPoint-compatible notesMaster (+ notes theme) on demand.
            # Do the same here instead of silently dropping the speaker notes.
            try:
                _ensure_notes_master(extract_dir)
                _add_override(
                    content_types_path,
                    "/ppt/notesMasters/notesMaster1.xml",
                    "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml",
                )
                _add_override(
                    content_types_path,
                    "/ppt/theme/theme2.xml",
                    "application/vnd.openxmlformats-officedocument.theme+xml",
                )
                has_notes_master = True
            except Exception:
                warnings.append(
                    {
                        "code": "template_notes_skipped_no_notes_master",
                        "message": (
                            "Host deck has no notesMaster part and one could not "
                            "be created; speaker notes were not embedded. 템플릿에 "
                            "노트 마스터가 없어 발표자 노트를 생략했습니다."
                        ),
                    }
                )

        media_cache: dict[tuple[str, str], str] = {}
        image_exts_used: set[str] = set()
        appended_parts: list[str] = []

        for offset, svg_path in enumerate(svg_files):
            slide_num = next_slide_num + offset
            rels_path = write_slide_part_from_svg(
                extract_dir,
                svg_path,
                slide_num=slide_num,
                layout_rel_target=layout_rel_target,
                media_cache=media_cache,
                image_exts_used=image_exts_used,
                transition=transition,
                transition_duration=transition_duration,
                verbose=verbose,
            )

            # Speaker notes.
            notes_text = notes.get(svg_path.stem, "") if enable_notes else ""
            if notes_text and has_notes_master:
                notes_num = next_notes_num
                next_notes_num += 1
                notes_dir = extract_dir / "ppt" / "notesSlides"
                notes_dir.mkdir(exist_ok=True)
                (notes_dir / "_rels").mkdir(exist_ok=True)
                (notes_dir / f"notesSlide{notes_num}.xml").write_text(
                    create_notes_slide_xml(slide_num, notes_text, lang=lang),
                    encoding="utf-8",
                )
                (notes_dir / "_rels" / f"notesSlide{notes_num}.xml.rels").write_text(
                    create_notes_slide_rels_xml(slide_num),
                    encoding="utf-8",
                )
                _append_relationship(
                    rels_path,
                    NOTES_SLIDE_REL_TYPE,
                    f"../notesSlides/notesSlide{notes_num}.xml",
                )
                _add_override(
                    content_types_path,
                    f"/ppt/notesSlides/notesSlide{notes_num}.xml",
                    NOTES_SLIDE_CONTENT_TYPE,
                )

            # Wire the slide into the presentation.
            rid = _append_relationship(
                presentation_rels_path, SLIDE_REL_TYPE, f"slides/slide{slide_num}.xml"
            )
            _append_sld_id(presentation_path, sld_id=next_sld_id, r_id=rid)
            next_sld_id += 1
            _add_override(
                content_types_path, f"/ppt/slides/slide{slide_num}.xml", SLIDE_CONTENT_TYPE
            )
            appended_parts.append(f"slides/slide{slide_num}.xml")

            if verbose:
                print(f"  [append {offset + 1}/{len(svg_files)}] {svg_path.name} -> slide{slide_num}.xml")

        # Image content-type defaults for the media we just added.
        if image_exts_used:
            content_types = content_types_path.read_text(encoding="utf-8")
            for ext in sorted(image_exts_used):
                content_types = _add_default_content_type(
                    content_types, ext, _content_type_for_extension(ext)
                )
            content_types_path.write_text(content_types, encoding="utf-8")

        if clear_existing:
            for rel_id, target in original_slides:
                _remove_slide(extract_dir, rel_id=rel_id, target=target)

        # Repackage atomically: zip to a temp file first.
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
# Shared slide-part writer (used by append + pptx_edit.recompose)
# ---------------------------------------------------------------------------


def write_slide_part_from_svg(
    extract_dir: Path,
    svg_path: Path,
    *,
    slide_num: int,
    layout_rel_target: str,
    media_cache: dict[tuple[str, str], str],
    image_exts_used: set[str],
    transition: str | None = "fade",
    transition_duration: float = 0.5,
    verbose: bool = False,
) -> Path:
    """Write ``slide{slide_num}.xml`` (+ rels + media) into the package.

    Does NOT touch presentation.xml / [Content_Types].xml — callers wire the
    part in themselves. Returns the slide's rels path (so callers can append
    further relationships, e.g. notes).
    """
    slides_dir = extract_dir / "ppt" / "slides"
    slides_dir.mkdir(exist_ok=True)
    (slides_dir / "_rels").mkdir(exist_ok=True)
    media_dir = extract_dir / "ppt" / "media"
    media_dir.mkdir(exist_ok=True)

    # Appended slides never enable native_objects, so the trailing
    # package_files / content_type_overrides registries stay empty here.
    slide_xml, media_files_dict, rel_entries, _anim_targets, _pkg, _cto = (
        convert_svg_to_slide_shapes(svg_path, slide_num=slide_num, verbose=verbose)
    )

    if transition and create_transition_xml is not None:
        transition_xml = "\n" + create_transition_xml(
            effect=transition,
            duration=transition_duration,
            advance_after=None,
        )
        slide_xml = slide_xml.replace("</p:sld>", transition_xml + "\n</p:sld>")

    (slides_dir / f"slide{slide_num}.xml").write_text(slide_xml, encoding="utf-8")

    # Media: hash-deduplicated names, shared across the whole package.
    media_name_map: dict[str, str] = {}
    for media_name, media_data in media_files_dict.items():
        ext = media_name.rsplit(".", 1)[-1].lower()
        media_hash = hashlib.sha256(media_data).hexdigest()
        cache_key = (ext, media_hash)
        cached_name = media_cache.get(cache_key)
        if cached_name is None:
            cached_name = f"image_{media_hash[:16]}.{ext}"
            media_cache[cache_key] = cached_name
            (media_dir / cached_name).write_bytes(media_data)
        media_name_map[media_name] = cached_name
        image_exts_used.add(ext)

    for rel in rel_entries:
        target = rel.get("target", "")
        if not target.startswith("../media/"):
            continue
        media_name = target.split("../media/", 1)[1]
        mapped_name = media_name_map.get(media_name)
        if mapped_name:
            rel["target"] = f"../media/{mapped_name}"

    # Slide rels: rId1 -> host layout; converter entries start at rId2.
    extra_rels = "".join(
        f'\n  <Relationship Id="{rel["id"]}" '
        f'Type="{rel["type"]}" Target="{rel["target"]}"/>'
        for rel in rel_entries
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        f'  <Relationship Id="rId1" Type="{SLIDE_LAYOUT_REL_TYPE}" '
        f'Target="{layout_rel_target}"/>{extra_rels}\n'
        "</Relationships>"
    )
    rels_path = slides_dir / "_rels" / f"slide{slide_num}.xml.rels"
    rels_path.write_text(rels_xml, encoding="utf-8")
    return rels_path


# ---------------------------------------------------------------------------
# Package inspection helpers
# ---------------------------------------------------------------------------


def _existing_slides(presentation_rels_path: Path) -> list[tuple[str, str]]:
    """Return ``(rel_id, target)`` for every slide the presentation references."""
    content = presentation_rels_path.read_text(encoding="utf-8")
    slides: list[tuple[str, str]] = []
    for match in re.finditer(r"<Relationship\b[^>]*/?>", content):
        entry = match.group(0)
        if f'Type="{SLIDE_REL_TYPE}"' not in entry:
            continue
        rel_id = re.search(r'Id="([^"]+)"', entry)
        target = re.search(r'Target="([^"]+)"', entry)
        if rel_id and target:
            slides.append((rel_id.group(1), target.group(1)))
    return slides


def _pick_blankest_layout(extract_dir: Path) -> str:
    """Choose the layout for appended slides; returns a slide-relative target.

    Preference order: an explicit ``type="blank"`` layout, then the layout
    with the fewest ``<p:ph`` placeholders (ties -> lowest part number).
    Generated slides paint their full content themselves, so the emptier
    the layout, the less template furniture collides with it.
    """
    layouts_dir = extract_dir / "ppt" / "slideLayouts"
    candidates = sorted(
        layouts_dir.glob("slideLayout*.xml"),
        key=lambda p: _part_number(p.name, "slideLayout"),
    )
    if not candidates:
        raise AppendError("invalid PPTX: package has no slide layouts")

    best: tuple[int, int] | None = None  # (placeholder_count, part_number)
    best_path: Path | None = None
    for path in candidates:
        content = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"<p:sldLayout\b[^>]*\btype=\"blank\"", content):
            best_path = path
            break
        ph_count = content.count("<p:ph ") + content.count("<p:ph/")
        key = (ph_count, _part_number(path.name, "slideLayout"))
        if best is None or key < best:
            best = key
            best_path = path
    assert best_path is not None
    return f"../slideLayouts/{best_path.name}"


def _part_number(filename: str, prefix: str) -> int:
    match = re.match(rf"{prefix}(\d+)\.xml$", filename)
    return int(match.group(1)) if match else 0


def _next_free_slide_number(filenames: list[str], prefix: str = "slide") -> int:
    """Gap-safe allocation: one past the max existing part number."""
    numbers = [_part_number(name, prefix) for name in filenames]
    return max(numbers, default=0) + 1


def _next_free_sld_id(presentation_xml: str) -> int:
    """Slide ids live in [256, 2147483647]; allocate past the current max."""
    ids = [int(m) for m in re.findall(r"<p:sldId\b[^>]*\bid=\"(\d+)\"", presentation_xml)]
    return max(ids + [255]) + 1


# ---------------------------------------------------------------------------
# presentation.xml surgery
# ---------------------------------------------------------------------------


def _append_sld_id(presentation_path: Path, *, sld_id: int, r_id: str) -> None:
    content = presentation_path.read_text(encoding="utf-8")
    entry = f'<p:sldId id="{sld_id}" r:id="{r_id}"/>'

    if re.search(r"</p:sldIdLst>", content):
        content = content.replace("</p:sldIdLst>", f"{entry}</p:sldIdLst>", 1)
    elif re.search(r"<p:sldIdLst\s*/>", content):
        content = re.sub(
            r"<p:sldIdLst\s*/>", f"<p:sldIdLst>{entry}</p:sldIdLst>", content, count=1
        )
    elif "<p:sldSz" in content:
        # Deck with zero slides and no list element: schema places
        # sldIdLst immediately before sldSz.
        content = content.replace(
            "<p:sldSz", f"<p:sldIdLst>{entry}</p:sldIdLst><p:sldSz", 1
        )
    else:
        raise AppendError("invalid PPTX: presentation.xml has no sldIdLst/sldSz anchor")
    presentation_path.write_text(content, encoding="utf-8")


def _remove_slide(extract_dir: Path, *, rel_id: str, target: str) -> None:
    """Detach and delete one original slide (restyle mode).

    Removes: its ``<p:sldId>`` entry, its presentation.xml.rels entry, the
    slide part + rels, any notesSlide it references (+ rels), and the
    [Content_Types].xml overrides for the deleted parts. Media are kept —
    layouts/masters may share them, and orphans are harmless.
    """
    presentation_path = extract_dir / "ppt" / "presentation.xml"

    content = presentation_path.read_text(encoding="utf-8")
    content = re.sub(
        rf"<p:sldId\b[^>]*\br:id=\"{re.escape(rel_id)}\"[^>]*/>\s*", "", content
    )
    presentation_path.write_text(content, encoding="utf-8")

    remove_slide_parts(extract_dir, rel_id=rel_id, target=target)


def remove_slide_parts(extract_dir: Path, *, rel_id: str, target: str) -> None:
    """Delete a slide's parts without touching presentation.xml's sldIdLst.

    Used directly by ``pptx_edit.recompose_pptx``, which rebuilds the whole
    sldIdLst itself and only needs the presentation rel entry, part files,
    notesSlides and content-type overrides gone.
    """
    presentation_rels_path = extract_dir / "ppt" / "_rels" / "presentation.xml.rels"
    content_types_path = extract_dir / "[Content_Types].xml"

    rels = presentation_rels_path.read_text(encoding="utf-8")
    rels = re.sub(rf"[ \t]*<Relationship\b[^>]*\bId=\"{re.escape(rel_id)}\"[^>]*/>\n?", "", rels)
    presentation_rels_path.write_text(rels, encoding="utf-8")

    slide_path = extract_dir / "ppt" / Path(target)
    slide_rels_path = slide_path.parent / "_rels" / f"{slide_path.name}.rels"

    if slide_rels_path.exists():
        slide_rels = slide_rels_path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"<Relationship\b[^>]*/>", slide_rels):
            entry = match.group(0)
            if f'Type="{NOTES_SLIDE_REL_TYPE}"' not in entry:
                continue
            notes_target = re.search(r'Target="([^"]+)"', entry)
            if not notes_target:
                continue
            notes_path = (slide_path.parent / notes_target.group(1)).resolve()
            notes_rels = notes_path.parent / "_rels" / f"{notes_path.name}.rels"
            _remove_override(
                content_types_path,
                "/" + notes_path.relative_to(extract_dir.resolve()).as_posix(),
            )
            notes_path.unlink(missing_ok=True)
            notes_rels.unlink(missing_ok=True)
        slide_rels_path.unlink(missing_ok=True)

    _remove_override(content_types_path, f"/ppt/{target}")
    slide_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# [Content_Types].xml surgery
# ---------------------------------------------------------------------------


def _add_override(content_types_path: Path, part_name: str, content_type: str) -> None:
    content = content_types_path.read_text(encoding="utf-8")
    if f'PartName="{part_name}"' in content:
        return
    entry = f'  <Override PartName="{part_name}" ContentType="{content_type}"/>'
    content = content.replace("</Types>", entry + "\n</Types>")
    content_types_path.write_text(content, encoding="utf-8")


def _remove_override(content_types_path: Path, part_name: str) -> None:
    content = content_types_path.read_text(encoding="utf-8")
    content = re.sub(
        rf"[ \t]*<Override\b[^>]*\bPartName=\"{re.escape(part_name)}\"[^>]*/>\n?",
        "",
        content,
    )
    content_types_path.write_text(content, encoding="utf-8")
