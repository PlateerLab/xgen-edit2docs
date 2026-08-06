"""Per-slide failure tolerance in the PPTX builder.

The builder used to re-raise on any per-slide conversion failure in
native-shapes mode, which meant a single broken SVG poisoned the
entire deck. The user's repeated production failures all looked the
same: 9 good slides + 1 bad SVG → the entire job died with no PPTX.

The fix is two-pronged:

* The drawingml converter now strips unresolvable <use> elements so
  most bad slides convert cleanly (covered in test_use_safety_net.py).
* The builder catches any remaining per-slide exception and inserts a
  placeholder slide (Korean + English "Slide N could not be rendered"
  + the error text) so the operator always gets a deck. This file
  covers that second path.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

from xgen_edit2docs.core.svg_to_pptx.pptx_builder import (
    _placeholder_slide_xml,
    create_pptx_with_native_svg,
)


def test_placeholder_xml_has_required_ooxml_structure():
    xml, media, rels, anim = _placeholder_slide_xml(
        slide_num=3,
        svg_name="slide_02.svg",
        error=RuntimeError("simulated render failure"),
    )
    assert xml.startswith("<?xml")
    assert "<p:sld" in xml
    assert "</p:sld>" in xml
    # Carries the failure number in Korean for the operator.
    assert "슬라이드 3 렌더링 실패" in xml
    # And the English copy too.
    assert "Slide 3" in xml
    assert "simulated render failure" in xml
    # No media / rels / animation targets — the placeholder is text-only.
    assert media == {}
    assert rels == []
    assert anim == []


def test_placeholder_xml_escapes_xml_special_chars_in_error():
    """An error message with `<`, `>`, `&` must not produce invalid XML."""
    xml, _media, _rels, _anim = _placeholder_slide_xml(
        slide_num=1,
        svg_name="x.svg",
        error=ValueError("token <foo> & broken"),
    )
    assert "&lt;foo&gt;" in xml
    assert "&amp;" in xml
    # Raw `<foo>` MUST NOT survive (would break OOXML parser).
    assert "<foo>" not in xml


def test_builder_inserts_placeholder_when_one_slide_fails(tmp_path):
    """One broken SVG out of three → output PPTX has three slides, with
    the middle slide replaced by the placeholder."""
    good = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        '<text x="100" y="100" font-size="40" fill="black">정상 슬라이드</text>'
        '</svg>'
    )
    # `<foreignObject>` is on the converter's forbidden list and can't be
    # rescued by the safety net — perfect for forcing a per-slide failure
    # that exercises the placeholder path.
    bad = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<foreignObject width="100" height="100"><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>'
        '</svg>'
    )
    p1 = tmp_path / "slide_01.svg"
    p2 = tmp_path / "slide_02.svg"
    p3 = tmp_path / "slide_03.svg"
    p1.write_text(good, encoding="utf-8")
    p2.write_text(bad, encoding="utf-8")
    p3.write_text(good, encoding="utf-8")

    output = tmp_path / "out.pptx"
    ok = create_pptx_with_native_svg(
        svg_files=[p1, p2, p3],
        output_path=output,
        verbose=False,
        use_native_shapes=True,
    )
    # The build either fully succeeded (3/3) or partially succeeded
    # (2/3 — placeholder counts as "rendered" in success_count); in
    # either case the PPTX file must exist with three slides.
    assert output.exists()
    with zipfile.ZipFile(output) as zf:
        slide_names = sorted(
            n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")
        )
        assert len(slide_names) == 3
        # The placeholder text must appear somewhere in the deck.
        deck_xml = "\n".join(zf.read(n).decode("utf-8") for n in slide_names)
        assert "렌더링 실패" in deck_xml or "could not be rendered" in deck_xml
