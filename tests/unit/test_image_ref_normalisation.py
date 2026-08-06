"""SVG `<image>` normalisation at the Executor boundary.

Production deck failures (quality stage):

  * `Detected forbidden <image opacity> (use overlay mask approach)`
  * `Image file not found: ../images/chapter_storm.png`

Both are upstream symptoms. The LLM emits image references with a
`../images/` prefix that doesn't match the workspace layout, sometimes
glued onto an `opacity` attribute that PPTX can't honour. The boundary
normaliser rewrites href to bare basename, strips opacity, and drops
references that don't have a bundled file. Quality then sees a clean
SVG and no retry round is wasted on the model's CSS quirks.
"""

from __future__ import annotations

from dataclasses import dataclass

from xgen_edit2docs.tools.execute import _normalise_image_refs


@dataclass
class _Img:
    placeholder: str
    url: str
    description: str | None = None


def test_href_with_path_prefix_rewritten_to_basename():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image x="0" y="0" width="100" height="100" href="../images/cover_bg.png"/>'
        '</svg>'
    )
    out, refs = _normalise_image_refs(svg, [_Img("cover_bg", "cover_bg.png")])
    assert 'href="cover_bg.png"' in out
    assert "../images/" not in out
    assert refs == {"cover_bg.png"}


def test_xlink_href_also_normalised():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
        '<image x="0" y="0" width="100" height="100" xlink:href="../images/cover_bg.png"/>'
        '</svg>'
    )
    out, refs = _normalise_image_refs(svg, [_Img("cover_bg", "cover_bg.png")])
    assert "cover_bg.png" in out
    assert "../images/" not in out


def test_opacity_attribute_stripped():
    """PPTX can't honour image opacity; the legacy quality rule bans it
    outright. Strip the attribute so the slide still builds — the visual
    mute is lost but the deck survives."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image x="0" y="0" width="100" height="100" '
        'href="cover_bg.png" opacity="0.4"/>'
        '</svg>'
    )
    out, _refs = _normalise_image_refs(svg, [_Img("cover_bg", "cover_bg.png")])
    assert "opacity" not in out


def test_dangling_image_ref_dropped():
    """When the basename isn't in the executor's image bundle, the
    `<image>` element is removed entirely — the slide just loses that
    decoration instead of crashing the converter."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image x="0" y="0" width="100" height="100" href="../images/chapter_storm.png"/>'
        '<text x="10" y="10">retained</text>'
        '</svg>'
    )
    out, _refs = _normalise_image_refs(svg, [_Img("cover_bg", "cover_bg.png")])
    # The dangling <image> is dropped.
    assert "chapter_storm" not in out
    # The text element survives.
    assert "retained" in out


def test_no_bundle_means_no_drop_decision():
    """When the caller didn't supply a bundle, we can't tell what's
    dangling — pass everything through but still strip opacity / fix
    paths. This protects callers that haven't been updated yet."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image href="../images/whatever.png" opacity="0.5"/>'
        '</svg>'
    )
    out, _refs = _normalise_image_refs(svg, [])
    # Path normalised, opacity stripped, but the image element stays.
    assert "whatever.png" in out
    assert "../images/" not in out
    assert "opacity" not in out


def test_data_url_passthrough():
    """Inline data URLs must not be rewritten — they're not file paths."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<image href="data:image/png;base64,iVBORw0KGgo="/>'
        '</svg>'
    )
    out, _refs = _normalise_image_refs(svg, [])
    assert "data:image/png" in out


def test_svg_without_image_passes_through():
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    out, refs = _normalise_image_refs(svg, [])
    assert out == svg
    assert refs == set()


def test_malformed_svg_returns_input():
    bad = "<svg><unclosed>"
    out, refs = _normalise_image_refs(bad, [])
    assert out == bad
    assert refs == set()
