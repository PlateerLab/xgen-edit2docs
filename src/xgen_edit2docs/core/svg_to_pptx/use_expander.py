"""In-memory expansion of ``<use data-icon="lib/name">`` elements.

The icon placeholder ``<use data-icon="...">`` is a project-internal SVG
extension; standard renderers (browsers, PowerPoint's SVG parser) and our
own DrawingML dispatcher do not understand it. ``finalize_svg`` already
expands it on disk into ``svg_final/``; this module provides the same
expansion in memory so ``svg_to_pptx`` can consume ``svg_output/`` directly
without first running the on-disk finalize step.

Public API:
    expand_use_data_icons(root, icons_dir, fallback_dir=None) -> int
        Walk the SVG element tree, replace every ``<use data-icon="...">``
        with its expanded ``<g>`` group of primitive shapes, and return
        the number of replacements made.

The heavy lifting (icon resolution, color application, scaling) is
delegated to ``svg_finalize.embed_icons`` so the two pipelines stay
behaviourally aligned.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET


SVG_NS = 'http://www.w3.org/2000/svg'


def _import_embed_icons():
    """Lazy import so svg_to_pptx doesn't hard-require svg_finalize at import time."""
    scripts_dir = Path(__file__).resolve().parent.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from svg_finalize import embed_icons  # type: ignore
    return embed_icons


def _build_replacement_g(
    use_elem: ET.Element,
    icons_dir: Path,
    fallback_dir: Path | None,
    embed_icons_mod,
) -> ET.Element | None:
    """Resolve a single ``<use data-icon="...">`` into an expanded ``<g>``.

    Returns None when the icon name is missing, unresolved, or the icon
    file cannot be parsed. Callers should leave the original ``<use>`` in
    place in that case (matching the on-disk finalize_svg behaviour, which
    also leaves unresolvable placeholders untouched).
    """
    use_str = ET.tostring(use_elem, encoding='unicode')
    attrs = embed_icons_mod.parse_use_element(use_str)
    if 'icon' not in attrs:
        return None

    try:
        icon_path, _base_size = embed_icons_mod.resolve_icon_path(
            attrs['icon'], icons_dir, fallback_dir,
        )
    except TypeError:
        # This fork's svg_finalize.embed_icons predates the fallback-dir
        # parameter (a16d9c3f's embed_icons hunks are out of this port's
        # scope). Resolve against the primary library only.
        icon_path, _base_size = embed_icons_mod.resolve_icon_path(
            attrs['icon'], icons_dir,
        )
    if not icon_path.exists():
        # The Strategist sometimes invents icon names that don't quite
        # match the bundled library (`trending-up` vs `arrow-trend-up`,
        # `brain` vs `brain-2`, etc.). Before giving up, try the closest
        # filename in the resolved library directory — keeps the deck
        # visually intact instead of dropping the glyph.
        substitute = _fuzzy_resolve(attrs['icon'], icons_dir)
        if substitute is None:
            return None
        icon_path = substitute

    color = attrs.get('fill', '#000000')
    elements, style, base_size = embed_icons_mod.extract_paths_from_icon(
        icon_path, color,
    )
    if not elements:
        return None

    g_xml = embed_icons_mod.generate_icon_group(attrs, elements, style, base_size)

    # Wrap with a namespaced root so the parsed subtree carries the SVG
    # namespace through to every primitive (path/circle/...).
    wrapped = f'<svg xmlns="{SVG_NS}">{g_xml}</svg>'
    try:
        parsed_root = ET.fromstring(wrapped)
    except ET.ParseError:
        return None

    for child in parsed_root:
        local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
        if local == 'g':
            return child
    return None


def _fuzzy_resolve(icon_name: str, icons_dir: Path) -> Path | None:
    """Best-effort match for a Strategist-invented icon name.

    `icon_name` is in `<library>/<name>` form (e.g. ``chunk-filled/trending-up``).
    We look at the actual files in ``icons_dir/<library>/`` and pick the
    closest by edit distance, biased toward substring matches so common
    synonyms resolve naturally:
      ``trending-up`` → ``arrow-trend-up`` (substring of `-trend-up`)
      ``brain``       → ``brain-2``         (substring)
      ``chevron``     → ``chevron-right``   (substring)

    Returns the matched Path, or None if the library directory is
    missing or no file is reasonably close.
    """
    import difflib

    if '/' not in icon_name:
        return None
    library, name = icon_name.split('/', 1)
    lib_dir = icons_dir / library
    if not lib_dir.is_dir():
        return None

    name_lower = name.lower()
    if len(name_lower) < 3:
        return None

    # Score each candidate by the length of the longest contiguous match
    # with the requested name. We require the match to cover at least
    # half of the shorter string (and at least 3 chars) so accidental
    # short-substring hits like `e.svg` inside `trending-up` don't win.
    candidates: list[tuple[int, Path]] = []
    for path in lib_dir.iterdir():
        if path.suffix != '.svg':
            continue
        stem = path.stem.lower()
        if len(stem) < 3:
            continue
        m = difflib.SequenceMatcher(None, name_lower, stem).find_longest_match(
            0, len(name_lower), 0, len(stem)
        )
        if m.size < 3:
            continue
        if m.size < max(3, min(len(name_lower), len(stem)) // 2):
            continue
        candidates.append((m.size, path))

    if candidates:
        # Prefer the longest contiguous match; on ties take the stem with
        # the length closest to the original (so `target` beats
        # `target-arrow` when the request was just `target`).
        candidates.sort(
            key=lambda pair: (-pair[0], abs(len(pair[1].stem) - len(name_lower)))
        )
        return candidates[0][1]

    # Fall back to difflib similarity ratio across the whole library.
    all_stems = [p.stem for p in lib_dir.iterdir() if p.suffix == '.svg']
    matches = difflib.get_close_matches(name_lower, all_stems, n=1, cutoff=0.7)
    if matches:
        return lib_dir / f'{matches[0]}.svg'
    return None


def expand_use_data_icons(
    root: ET.Element,
    icons_dir: Path,
    fallback_dir: Path | None = None,
) -> int:
    """Replace every ``<use data-icon="...">`` in *root* with its expansion.

    Walks the tree, finds use elements that carry a ``data-icon`` attribute,
    builds a new ``<g>`` subtree from the project icon library (falling back to
    the global library when supplied), and swaps it into the parent element at
    the same position.

    Returns the number of placeholders successfully expanded. Unresolvable
    placeholders are left in place so callers can decide whether to warn.
    """
    if not icons_dir.exists():
        return 0

    embed_icons_mod = _import_embed_icons()

    # ElementTree elements don't carry a parent reference, so build a map.
    parent_of: dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    targets: list[ET.Element] = []
    for elem in root.iter():
        local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
        if local == 'use' and elem.get('data-icon'):
            targets.append(elem)

    expanded = 0
    for use_elem in targets:
        parent = parent_of.get(use_elem)
        if parent is None:
            continue
        replacement = _build_replacement_g(use_elem, icons_dir, fallback_dir, embed_icons_mod)
        if replacement is None:
            continue
        idx = list(parent).index(use_elem)
        parent.remove(use_elem)
        parent.insert(idx, replacement)
        expanded += 1

    return expanded
