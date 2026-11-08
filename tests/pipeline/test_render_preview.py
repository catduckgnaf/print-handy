"""Preview must show the model centered on the bed, like a slicer would place it."""
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from print_handy.core import render_preview  # noqa: E402

# Per-test gate, matching test_design.py / test_design_dispatch.py: only the
# tests that shell out to the binary carry it. The non-STL refusal test below
# is deliberately ungated — render() validates the suffix in _scene before
# _openscad_cmd is ever reached, so it runs on every machine (issue #1060).
needs_scad = pytest.mark.skipif(not (shutil.which('openscad') and shutil.which('xvfb-run')),
                                reason='openscad/xvfb-run not installed')


def _offset_box(tmp_path, size, origin):
    import trimesh
    box = trimesh.creation.box(extents=size)
    box.apply_translation([o + s / 2 for o, s in zip(origin, size)])
    path = tmp_path / 'box.stl'
    box.export(path)
    return path


@needs_scad
def test_far_offset_model_is_centered_and_rendered(tmp_path, monkeypatch):
    # Author origin far from the bed reproduced the "model floating off the bed" preview.
    stl = _offset_box(tmp_path, (140, 210, 20), (500, -300, 40))
    seen = {}
    real_run = render_preview.subprocess.run

    def spy(cmd, **kw):
        seen['scad'] = Path(cmd[-1]).read_text()
        seen['cmd'] = cmd
        return real_run(cmd, **kw)
    monkeypatch.setattr(render_preview.subprocess, 'run', spy)
    out = render_preview.render(stl)
    assert Path(out).is_file()
    assert '--autocenter' not in seen['cmd'] and '--viewall' not in seen['cmd']
    # Translation column centers X/Y on the 220 bed and drops Z to 0.
    assert ',-460.0]' in seen['scad'] and ',305.0]' in seen['scad'] and ',-40.0]' in seen['scad']
    assert '[0.2,0.55,0.9]' in seen['scad']  # fits -> blue


@needs_scad
def test_oversized_model_is_flagged_red(tmp_path, monkeypatch):
    stl = _offset_box(tmp_path, (140, 210, 20), (0, 0, 0))
    seen = {}
    real_run = render_preview.subprocess.run

    def spy(cmd, **kw):
        seen['scad'] = Path(cmd[-1]).read_text()
        return real_run(cmd, **kw)
    monkeypatch.setattr(render_preview.subprocess, 'run', spy)
    render_preview.render(stl, scale=1.2)
    assert '[0.9,0.25,0.2]' in seen['scad']


def test_render_refuses_a_non_stl_file(tmp_path):
    text = tmp_path / 'notes.txt'
    text.write_text('not a model', encoding='utf-8')
    with pytest.raises(ValueError, match='STL models only'):
        render_preview.render(text)


# ------------------------------------------- real OpenSCAD binary (expansion)


@needs_scad
def test_sheet_is_a_real_labelled_four_view_png(tmp_path):
    from PIL import Image, ImageStat
    stl = _offset_box(tmp_path, (40, 20, 10), (0, 0, 0))
    dest = tmp_path / 'sheet.png'
    out = render_preview.render_sheet(stl, dimensions=[40, 20, 10], dest=dest)
    assert Path(out) == dest and dest.is_file()
    image = Image.open(dest).convert('RGB')
    assert image.size == (900, 700)  # 2x2 tiles of 450x350
    colors = image.getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) > 100
    # Every quadrant carries a real rendered view, not a blank tile.
    for x in (0, 450):
        for y in (0, 350):
            tile = image.crop((x, y, x + 450, y + 350))
            assert max(ImageStat.Stat(tile).stddev) > 2.0


@needs_scad
def test_render_from_a_unicode_and_space_path(tmp_path):
    from PIL import Image
    folder = tmp_path / 'ü plate'
    folder.mkdir()
    stl = _offset_box(folder, (30, 30, 30), (0, 0, 0))
    out = render_preview.render(stl)
    assert Path(out).is_file()
    assert Image.open(out).size == (900, 700)
