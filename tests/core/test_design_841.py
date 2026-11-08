"""Tests for the #841 design preview / parameter-edit fixes (Fable audit gaps 1-4).

No real OpenSCAD and no network: the render is an injected runner that
writes a real cube STL, the model is a scripted fake, and the OpenSCAD
preview renderer is faked at the subprocess seam where a test needs it.
"""
import asyncio
import io
import logging
import subprocess
from pathlib import Path

import pytest

from print_handy.core import design, plate_thumbnail, render_preview

GOOD = ("plate_z = 3;\n$fn = 48;\ndifference() {\n"
        "  translate([-20, -10, 0]) cube([40, 20, plate_z]);\n"
        "  for (x = [-15, 15]) translate([x, 0, -1]) cylinder(h = plate_z + 2, d = 4);\n}\n")
THICKER = GOOD.replace("plate_z = 3", "plate_z = 5")
TALL = "height = 3;\n$fn = 32;\ncube([40, 20, height]);\n"
CFG = {"design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                  "timeout_s": 180, "repair_attempts": 0},
       "printer": {"bed_mm": [220, 220]}}


def _box_triangles(dx, dy, dz, ox=0.0, oy=0.0, oz=0.0):
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz),
         (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def cube_runner(cmd, **kwargs):
    """A fake openscad: writes a real 40x20x3 cube STL where -o points."""
    from print_handy.core.geometry_layout import write_binary_stl
    out = Path(cmd[cmd.index("-o") + 1])
    write_binary_stl(_box_triangles(40, 20, 3, -20, -10, 0), out)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def scripted(replies):
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


def engine(tmp_path, complete, **kw):
    kw.setdefault("runner", cube_runner)
    return design.DesignEngine(tmp_path / "cache", complete, cfg=CFG, **kw)


def run(coro):
    return asyncio.run(coro)


def _plate_stl(tmp_path, name="plate.stl"):
    from print_handy.core.geometry_layout import write_binary_stl
    path = tmp_path / name
    write_binary_stl(_box_triangles(40, 20, 3, -20, -10, 0), path)
    return path


# --------------------------------------------------------------------- gap 1: preview fallback

def test_preview_falls_back_to_plate_rasterizer(tmp_path, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("renderer exploded")

    monkeypatch.setattr(render_preview, "render_sheet", boom)
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("o", "c", "a plate")
    with caplog.at_level(logging.WARNING):
        result = run(eng.run_turn(session, "a plate"))
    assert result["preview_path"].endswith("turn-1.png")
    png = Path(result["preview_path"])
    assert png.is_file()
    from PIL import Image, ImageStat
    image = Image.open(io.BytesIO(png.read_bytes()))
    assert image.format == "PNG"
    assert max(ImageStat.Stat(image.convert("RGB")).stddev) > 2  # non-blank
    assert result["preview_error"] is None  # the fallback produced an image
    assert any("preview" in rec.message and rec.levelno == logging.WARNING
               for rec in caplog.records)


def test_preview_error_is_surfaced_when_even_the_fallback_fails(tmp_path, monkeypatch, caplog):
    def boom(*a, **kw):
        raise RuntimeError("renderer exploded")

    def boom_plate(*a, **kw):
        raise RuntimeError("rasterizer exploded")

    monkeypatch.setattr(render_preview, "render_sheet", boom)
    monkeypatch.setattr(plate_thumbnail, "render_plate", boom_plate)
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("o", "c", "a plate")
    with caplog.at_level(logging.WARNING):
        result = run(eng.run_turn(session, "a plate"))
    assert result["preview_path"] is None
    assert "rasterizer exploded" in result["preview_error"]
    saved = design.load_session(eng.root, session["id"])
    assert saved["preview_error"] == result["preview_error"]
    assert any(rec.levelno == logging.WARNING for rec in caplog.records)


# --------------------------------------------------------------------- gap 1: binary lookup

def _fake_scad_run(calls):
    from PIL import Image
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = Path(cmd[cmd.index("-o") + 1])
        image = Image.new("RGB", (60, 40))
        px = image.load()
        for x in range(60):
            for y in range(40):
                px[x, y] = (x * 4 % 256, y * 6 % 256, (x + y) * 3 % 256)
        image.save(out)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return fake_run


def test_render_sheet_resolves_binaries_with_shutil_which(tmp_path, monkeypatch):
    stl = _plate_stl(tmp_path)
    calls = []
    monkeypatch.setattr(render_preview.subprocess, "run", _fake_scad_run(calls))
    monkeypatch.setattr(render_preview.shutil, "which",
                        lambda name, path=None: {"openscad": "/opt/bin/openscad",
                                      "xvfb-run": "/opt/bin/xvfb-run"}.get(name))
    monkeypatch.delenv("DISPLAY", raising=False)
    out = render_preview.render_sheet(str(stl), dest=tmp_path / "sheet.png",
                                      bed=[220, 220], dimensions=[40, 20, 3])
    assert Path(out).is_file()
    assert len(calls) == 4  # iso, front, top, right
    for cmd in calls:
        assert cmd[0] == "/opt/bin/xvfb-run" and cmd[2] == "/opt/bin/openscad"
        assert "--projection=o" in cmd
    cameras = {next(a for a in cmd if a.startswith("--camera=")) for cmd in calls}
    assert len(cameras) == 4  # four distinct views


def test_render_sheet_sheet_has_expected_size_and_labels(tmp_path, monkeypatch):
    stl = _plate_stl(tmp_path)
    calls = []
    monkeypatch.setattr(render_preview.subprocess, "run", _fake_scad_run(calls))
    monkeypatch.setattr(render_preview.shutil, "which",
                        lambda name, path=None: "/usr/bin/" + name)
    out = render_preview.render_sheet(str(stl), dest=tmp_path / "sheet.png",
                                      bed=[220, 220], dimensions=[40, 20, 3])
    from PIL import Image
    image = Image.open(out)
    assert image.size == (900, 700)  # 2x2 tiles of 450x350


def test_render_sheet_without_openscad_names_the_gap(tmp_path, monkeypatch):
    stl = _plate_stl(tmp_path)
    monkeypatch.setattr(render_preview.shutil, "which", lambda name, path=None: None)
    with pytest.raises(RuntimeError, match="openscad not on PATH"):
        render_preview.render_sheet(str(stl), dest=tmp_path / "sheet.png")


# --------------------------------------------------------------------- gap 2: scad_params

PARAM_SOURCE = (
    "// width = 999;\n"
    "width = 40; /* height = 1; */\n"
    "height = 3;\n"
    "$fn = 48;\n"
    "hole_d = 4.5;\n"
    "drop = -2;\n"
    "module thing() { inner = 5; cube([width, 10, height]); }\n"
    "thing();\n")


def test_scad_params_parses_top_level_numbers_and_skips_comments():
    assert design.scad_params(PARAM_SOURCE) == {
        "width": 40.0, "height": 3.0, "$fn": 48.0, "hole_d": 4.5, "drop": -2.0}


def test_scad_params_ignores_strings_and_call_arguments():
    source = 'label = "x = 9;";\ncube([1, 2, 3]);\nwall = 2;\n'
    assert design.scad_params(source) == {"wall": 2.0}


# --------------------------------------------------------------------- gap 2: deterministic edits

def test_deterministic_named_edit_makes_no_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    fake = scripted([GOOD])
    eng = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    assert len(fake.calls) == 1
    result = run(eng.run_turn(session, "make plate_z to 5"))
    assert len(fake.calls) == 1  # no model call for the parameter edit
    assert result["param_changes"] == [("plate_z", 3.0, 5.0)]
    turn2 = Path(result["scad_path"]).read_text()
    assert "plate_z = 5;" in turn2
    assert turn2.replace("plate_z = 5;", "plate_z = 3;") == GOOD.strip()  # only that value moved


def test_deterministic_relative_edit_maps_to_the_height_variable(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    fake = scripted([TALL])
    eng = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a block")
    run(eng.run_turn(session, "a block"))
    result = run(eng.run_turn(session, "make it 5 mm taller"))
    assert len(fake.calls) == 1
    assert result["param_changes"] == [("height", 3.0, 8.0)]
    assert "height = 8;" in Path(result["scad_path"]).read_text()


@pytest.mark.parametrize("source,instruction,expected", [
    ("height = 20;\ncube([40, 20, height]);\n", "make it 5 mm taller",
     ("height", 20.0, 25.0)),
    ("width = 40;\ncube([width, 20, 3]);\n", "make it 5 mm wider",
     ("width", 40.0, 45.0)),
    ("length = 10;\ncube([length, 20, 3]);\n", "make it 2.5 mm longer",
     ("length", 10.0, 12.5)),
    ("height = 20;\ncube([40, 20, height]);\n", "5 mm taller",
     ("height", 20.0, 25.0)),
])
def test_deterministic_relative_edit_adds_to_existing_value(source, instruction, expected):
    edit = design.deterministic_param_edit(source, instruction)
    assert edit is not None
    new_source, changes = edit
    assert changes == [expected]
    name, _old, new = expected
    assert f"{name} = {new:g};" in new_source


def test_llm_turn_reports_the_params_it_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    fake = scripted([GOOD, THICKER])
    eng = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    result = run(eng.run_turn(session, "make it thicker please"))
    assert len(fake.calls) == 2  # a real model turn
    assert result["param_changes"] == [("plate_z", 3.0, 5.0)]
    assert design.format_param_changes(result["param_changes"]) == ["plate_z: 3 → 5"]


# --------------------------------------------------------------------- gap 3: before/after

def test_second_turn_preview_includes_the_previous_turn(tmp_path, monkeypatch):
    sheets = []

    def fake_sheet(path, **kw):
        from PIL import Image
        dest = Path(path).with_name(f"sheet-{len(sheets)}.png")
        Image.new("RGB", (90, 70), (200, 30, 30) if not sheets else (30, 30, 200)).save(dest)
        sheets.append(dest)
        return str(dest)

    monkeypatch.setattr(render_preview, "render_sheet", fake_sheet)
    eng = engine(tmp_path, scripted([GOOD, THICKER]))
    session = eng.start_session("o", "c", "a plate")
    first = run(eng.run_turn(session, "a plate"))
    assert Path(first["preview_path"]).name == "turn-1.png"
    second = run(eng.run_turn(session, "make it thicker please"))
    compare = Path(second["preview_path"])
    assert compare.name == "turn-2-compare.png"
    from PIL import Image
    assert Image.open(compare).size == (180, 70)  # previous + current side by side
    folder = Path(first["preview_path"]).parent
    assert (folder / "turn-2.png").is_file()  # the clean sheet stays for the next turn


# --------------------------------------------------------------------- gap 4: baseline turn

def test_baseline_turn_mesh_makes_no_model_call(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))

    def forbidden(messages):
        raise AssertionError("baseline turn must not call the model")

    eng = engine(tmp_path, forbidden)
    session = eng.start_edit_session("o", "c", "edit", _plate_stl(tmp_path))
    result = eng.baseline_turn(session)
    assert session["turns"] == 1
    assert result["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]
    body = Path(result["scad_path"]).with_name("turn-1.body.scad")
    assert body.read_text() == "base();"
    assert result["preview_path"] and Path(result["preview_path"]).is_file()


def test_baseline_turn_scad_uses_the_source_as_is(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))

    def forbidden(messages):
        raise AssertionError("baseline turn must not call the model")

    source = tmp_path / "widget.scad"
    source.write_text(GOOD)
    eng = engine(tmp_path, forbidden)
    session = eng.start_edit_session("o", "c", "edit", source)
    result = eng.baseline_turn(session)
    assert Path(result["scad_path"]).read_text() == GOOD
    assert result["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]


@pytest.mark.parametrize("evil", [
    "include </etc/passwd.scad>\ncube([40, 20, 3]);\n",
    "import('/etc/passwd');\ncube([40, 20, 3]);\n",
    "use <lib.scad>\ncube([40, 20, 3]);\n",
    "surface(file = 'height.png');\n",
])
def test_baseline_turn_scad_scans_dropped_source_before_rendering(
        tmp_path, monkeypatch, evil):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    rendered = []

    def recording_runner(cmd, **kwargs):
        rendered.append(cmd)
        return cube_runner(cmd, **kwargs)

    def forbidden(messages):
        raise AssertionError("baseline turn must not call the model")

    source = tmp_path / "evil.scad"
    source.write_text(evil)
    eng = engine(tmp_path, forbidden, runner=recording_runner)
    session = eng.start_edit_session("o", "c", "edit", source)
    with pytest.raises(design.DesignError, match="may not use"):
        eng.baseline_turn(session)
    assert rendered == []  # nothing was rendered / run
    assert session["turns"] == 0


# --------------------------------------------------------------------- extra (a): first ERROR line

def test_render_scad_error_keeps_the_first_error_line(tmp_path):
    noise = "\n".join(f"WARNING: filler line {i}" for i in range(12))

    def noisy_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, "", f"ERROR: the real problem is here\n{noise}\n")

    ok, error = design.render_scad(tmp_path / "a.scad", tmp_path / "a.stl",
                                   runner=noisy_runner)
    assert ok is False
    assert "ERROR: the real problem is here" in error


def test_render_scad_error_keeps_the_first_warning_line(tmp_path):
    # #1046: under --hardwarnings the WARNING line is the actual cause, but
    # the statistics tail pushes it out of the last-6 window. The output
    # below is the real OpenSCAD 2021.01 output for
    # cube([missing_var, 10, 10]); captured verbatim (exit code 1, no STL).
    output = (
        "WARNING: Ignoring unknown variable 'missing_var' in file a.scad, line 1\n"
        "TRACE: called by 'cube' in file a.scad, line 1\n"
        "Geometries in cache: 1\n"
        "Geometry cache size in bytes: 0\n"
        "CGAL Polyhedrons in cache: 0\n"
        "CGAL cache size in bytes: 0\n"
        "Total rendering time: 0:00:00.000\n"
        "Current top level object is empty.\n")

    def warn_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", output)

    ok, error = design.render_scad(tmp_path / "a.scad", tmp_path / "a.stl",
                                   runner=warn_runner)
    assert ok is False
    assert "WARNING: Ignoring unknown variable 'missing_var'" in error
    assert "Current top level object is empty." in error


# --------------------------------------------------------------------- review fixes

def test_first_model_turn_reports_no_parameter_changes(tmp_path, monkeypatch):
    # Turn 1 has no earlier source, so a diff would list every parameter as
    # "(new)"; that is noise, not a change the user needs to check.
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("o", "c", "a plate")
    result = run(eng.run_turn(session, "a plate"))
    assert result["param_changes"] == []


@pytest.mark.parametrize("value,written", [
    ("1234.5678", "1234.5678"), ("0.125", "0.125"), ("5", "5"), ("123456789", "123456789"),
])
def test_named_edit_writes_the_value_without_rounding(value, written):
    source = "width = 10;\ncube([width, 1, 1]);\n"
    new_source, changes = design.deterministic_param_edit(source, f"set width to {value}")
    assert f"width = {written};" in new_source
    assert design.format_param_changes(changes) == [f"width: 10 → {written}"]


@pytest.mark.parametrize("source,instruction", [
    ("hole_height = 4;\nbase = 30;\ncube([10, 10, base]);\n", "make it 5 mm taller"),
    ("wall_width = 2;\ncube([40, 20, 3]);\n", "make it 5 mm wider"),
    ("screw_length = 12;\ncube([40, 20, 3]);\n", "make it 5 mm longer"),
])
def test_relative_edit_needs_an_exact_parameter_name(source, instruction):
    # A parameter that merely contains the word is a feature, not the part's
    # overall size: no deterministic edit, the model turn decides.
    assert design.deterministic_param_edit(source, instruction) is None


def test_relative_edit_without_exact_name_falls_back_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(render_preview, "render_sheet",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no renderer")))
    feature = "hole_height = 4;\nbase = 30;\ncube([10, 10, base]);\n"
    taller = feature.replace("base = 30", "base = 35")
    fake = scripted([feature, taller])
    eng = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a block")
    run(eng.run_turn(session, "a block"))
    result = run(eng.run_turn(session, "make it 5 mm taller"))
    assert len(fake.calls) == 2  # the model was asked
    assert result["param_changes"] == [("base", 30.0, 35.0)]
