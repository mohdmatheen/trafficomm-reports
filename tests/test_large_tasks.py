"""Large tasks (ZSP, Nov 2026: a rolling deck plus 75 screenshots failed with 'too large to process
within the memory limit'). Decks are read without their pictures and LibreOffice renders a copy with
scaled-down pictures, so memory no longer grows with every full-size screenshot."""
import io
import zipfile

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from app.engine.pptx_inspect import inspect_deck
from app.engine.pptx_light import light_copy, open_light, render_copy
from tests.test_row_table_decks import OCT, _shots, make_rolling_deck, oct_dataset


def _big_png(path, size=(1179, 2556)):
    Image.effect_noise(size, 50).convert("RGB").save(path)
    return str(path)


def _deck_with_big_pictures(tmp_path, n=4):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Facebook"
    for k in range(n):
        s.shapes.add_picture(_big_png(tmp_path / f"p{k}.png"), Inches(0.3 + 2.4 * k), Inches(1.5), Inches(2), Inches(4.3))
    path = str(tmp_path / "deck.pptx")
    prs.save(path)
    return path


def _media_bytes(path):
    with zipfile.ZipFile(path) as z:
        return sum(i.file_size for i in z.infolist() if i.filename.startswith("ppt/media/"))


def test_light_copy_drops_pictures_but_keeps_slides(tmp_path):
    src = _deck_with_big_pictures(tmp_path)
    assert _media_bytes(src) > 5_000_000
    light = light_copy(src)
    assert _media_bytes(light) == 0
    assert light_copy(src) == light  # cached per file version
    prs = open_light(src)
    pics = [sh for sh in prs.slides[0].shapes if sh.shape_type == 13]
    assert len(pics) == 4 and prs.slides[0].shapes.title.text == "Facebook"
    info = inspect_deck(src)
    assert info["slides"][0]["pictures"] == 4


def test_render_copy_scales_pictures_down_and_keeps_name(tmp_path):
    src = _deck_with_big_pictures(tmp_path, n=2)
    out = render_copy(src, str(tmp_path / "render"))
    assert out.endswith("/deck.pptx") and out != src
    with zipfile.ZipFile(out) as z:
        for n in z.namelist():
            if n.startswith("ppt/media/"):
                with Image.open(io.BytesIO(z.read(n))) as im:
                    assert max(im.size) <= 1600
    assert len(Presentation(out).slides) == 1
    # the deck the employee downloads keeps full-size pictures
    with zipfile.ZipFile(src) as z:
        n = next(x for x in z.namelist() if x.startswith("ppt/media/"))
        with Image.open(io.BytesIO(z.read(n))) as im:
            assert im.size == (1179, 2556)


def test_rolling_deck_still_updates_with_light_reads(tmp_path):
    from app.engine.pptx_update import DeckUpdater
    from app.profiles import ATC_PROFILE
    src = make_rolling_deck(str(tmp_path / "deck.pptx"), tmp_path)
    up = DeckUpdater(src, oct_dataset(), OCT, ATC_PROFILE, {},
                     {"screenshots": _shots(tmp_path), "instructions": "Remove August completely. Keep September unchanged. Add October."})
    out = str(tmp_path / "out.pptx")
    res = up.run(out)
    assert not res["blocked"]
    prs = Presentation(out)
    grid = [s for s in prs.slides if any(sh.has_text_frame and sh.text_frame.text == "Facebook" for sh in s.shapes)][-1]
    pics = [sh for sh in grid.shapes if sh.shape_type == 13]
    assert len(pics) == 2 and all(len(p.image.blob) > 1000 for p in pics)  # real pictures, not light placeholders
    # September's grid pictures (copied from the original deck) are intact too
    sep = next(s for s in prs.slides if any(sh.has_text_frame and sh.text_frame.text == "Facebook" for sh in s.shapes))
    assert all(len(p.image.blob) > 1000 for p in sep.shapes if p.shape_type == 13)
