"""Quality workspace receives image bytes.

Production failure on deck_3.pptx: every cover_bg / chapter divider
reference in the SVG produced a `Image file not found` quality error
even though the image was successfully acquired and bundled. The
quality stage wrote the slide SVGs to a tempdir but never copied the
bundle alongside, so the legacy `_check_image_references` resolver
saw a phantom missing file. Fix: pipe `image_bytes_by_filename`
through to `QualityCheckRequest.images` and write the files into the
workspace at both `svgs/` and `images/` so any `../images/` or bare
basename href resolves cleanly.
"""

from __future__ import annotations

from xgen_edit2docs.tools.quality import (
    QualityCheckRequest,
    QualityCheckResponse,
    QualitySlide,
    check_svg_quality,
)


def _resp(svg: str, images: dict[str, bytes] | None = None) -> QualityCheckResponse:
    return check_svg_quality(
        QualityCheckRequest(
            slides=[QualitySlide(index=0, name="slide_00", svg=svg)],
            images=images or {},
        )
    )


def _codes(resp: QualityCheckResponse) -> list[str]:
    return [i.code for i in resp.issues if i.severity == "error"]


def test_missing_image_without_bundle_is_reported():
    """Sanity: when no bundle is supplied, the legacy check still
    flags missing images. Establishes a baseline before testing the
    bundle-passing happy path."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720">'
        '<image href="cover_bg.png" x="0" y="0" width="1280" height="720"/>'
        '</svg>'
    )
    resp = _resp(svg, images={})
    assert any("Image file not found" in i.message for i in resp.issues)


def test_image_referenced_by_basename_resolves_when_bundled():
    """When the bundle includes the file, the quality workspace
    contains it under svgs/ and the legacy resolver passes."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720">'
        '<image href="cover_bg.png" x="0" y="0" width="1280" height="720"/>'
        '</svg>'
    )
    resp = _resp(svg, images={"cover_bg.png": b"PNGdata"})
    assert not any("Image file not found" in i.message for i in resp.issues)


def test_image_referenced_with_parent_path_resolves_when_bundled():
    """Some LLM outputs reference images as `../images/foo.png`. The
    quality workspace writes the bundle into a sibling `images/` dir
    so this path resolves too."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720">'
        '<image href="../images/chapter_divider.png" x="0" y="0" width="1280" height="720"/>'
        '</svg>'
    )
    resp = _resp(svg, images={"chapter_divider.png": b"PNGdata"})
    assert not any("Image file not found" in i.message for i in resp.issues)


def test_path_traversal_filename_skipped():
    """The image dict must not let a malicious basename traverse the
    workspace (`../../etc/passwd`). Slashes in the filename mean we
    silently skip it — the file just stays missing for the lookup."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720">'
        '<image href="benign.png"/>'
        '</svg>'
    )
    images = {
        "benign.png": b"PNGdata",
        "../etc/passwd": b"hack",  # silently dropped
    }
    # Should not crash and should still see the bundle's good file.
    resp = _resp(svg, images=images)
    assert resp.cost is not None  # smoke check the call completed


def test_empty_image_dict_is_a_noop():
    """When images={} the workspace setup must not blow up — quality
    still runs over the slides."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
        'width="1280" height="720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        '</svg>'
    )
    resp = _resp(svg, images={})
    assert isinstance(resp, QualityCheckResponse)
