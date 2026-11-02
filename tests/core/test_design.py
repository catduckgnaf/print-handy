"""Offline tests for the AI design engine (#388 Stage 1).

The model is always a scripted fake. Real-OpenSCAD renders run only when
openscad and xvfb-run are installed (the render_preview test pattern);
everything else — sandbox scan, extraction, transports, session store —
runs everywhere.
"""
import asyncio
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from print_handy.core import design

GOOD = ("plate_z = 3;\n$fn = 48;\ndifference() {\n"
        "  translate([-20, -10, 0]) cube([40, 20, plate_z]);\n"
        "  for (x = [-15, 15]) translate([x, 0, -1]) cylinder(h = plate_z + 2, d = 4);\n}\n")
THICKER = GOOD.replace("plate_z = 3", "plate_z = 5")
SYNTAX_BAD = "cube([10, 10, 10);\n"
EMPTY = "difference() { cube([10, 10, 10]); cube([20, 20, 20]); }\n"
CFG = {"design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                  "timeout_s": 180, "repair_attempts": 3},
       "printer": {"bed_mm": [220, 220]}}

needs_scad = pytest.mark.skipif(not (shutil.which("openscad") and shutil.which("xvfb-run")),
                                reason="openscad/xvfb-run not installed")


def scripted(replies):
    """A fake model transport: replies in order, recording every message list."""
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


def engine(tmp_path, complete, **kw):
    return design.DesignEngine(tmp_path / "cache", complete, cfg=CFG, **kw)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- source handling

def test_extract_scad_accepts_fences_and_bare_source():
    assert design.extract_scad(f"```scad\n{GOOD}```") == GOOD.strip()
    assert design.extract_scad(f"```\n{GOOD}```") == GOOD.strip()
    assert design.extract_scad(GOOD) == GOOD.strip()
    with pytest.raises(design.DesignError, match="did not return any OpenSCAD"):
        design.extract_scad("I cannot help with that.")
    with pytest.raises(design.DesignError):
        design.extract_scad("")


@pytest.mark.parametrize("source", [
    "import('/etc/passwd');\ncube([1, 1, 1]);",
    "IMPORT ('x.stl');",
    "translate([0, 0, 0]) import('a.stl');",
    "include <../outside/lib.scad>\ncube([1, 1, 1]);",
    "use <lib.scad>\ncube([1, 1, 1]);",
    "surface(file = 'height.png');",
    "Surface (file='x.png');",
    "import(str('a', '.stl'));",
])
def test_sandbox_scan_refuses_file_access_statements(source):
    with pytest.raises(design.DesignError, match="may not use"):
        design.scan_scad(source)


def test_sandbox_scan_allows_ordinary_design_words():
    design.scan_scad(GOOD)  # no exception
    design.scan_scad("// a plate, imported dimensions noted in comments\ncube([1, 1, 1]);\n")


@pytest.mark.parametrize("source", [
    # OpenSCAD treats comments as token separators, so a comment between a
    # file-access keyword and its delimiter is still the forbidden statement
    # (P1-92, P1-94).
    'import/**/("/tmp/p.stl");',
    'import/* gap */("/tmp/p.stl");',
    'import // c\n("/tmp/p.stl");',
    'cube([1, 1, 1]); import/**/("/tmp/private.stl");',
    'surface/* g */(file="/tmp/h.dat");',
    'surface /* comment */ (file="/tmp/private.dat");',
    'use /*x*/ <lib.scad>;',
    'include /*x*/ <lib.scad>;',
    'include// c\n<lib.scad>;',
])
def test_sandbox_scan_refuses_comment_separated_file_access(source):
    with pytest.raises(design.DesignError, match="may not use"):
        design.scan_scad(source)


def test_sandbox_scan_ignores_forbidden_words_in_comments_and_strings():
    # Forbidden words that never reach the token stream are not statements.
    design.scan_scad('// import("/tmp/p.stl");\ncube([1, 1, 1]);')
    design.scan_scad('/* surface(file="/tmp/h.dat"); */\ncube([1, 1, 1]);')
    design.scan_scad('echo("import(x) is not a real call");\ncube([1, 1, 1]);')


def test_external_refs_sees_comment_separated_statements():
    assert design.external_refs('import/**/("x.stl");') == ["import"]
    assert design.external_refs('surface/*c*/(file="h.dat");') == ["surface"]
    assert design.external_refs('// import("x.stl");\ncube([1, 1, 1]);') == []


def test_sandbox_scan_refuses_oversize_source():
    with pytest.raises(design.DesignError, match="limit"):
        design.scan_scad("cube([1,1,1]);\n" + "// pad\n" * (design.MAX_SCAD_BYTES // 6))


# --------------------------------------------------------------------- transports

def test_resolve_http_config_inherits_llm_then_prefers_design():
    cfg = {"design": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 180},
           "llm": {"chat_url": "http://llm/v1", "model": "luna", "api_key_env": "K1"}}
    assert design.resolve_http_config(cfg) == {
        "chat_url": "http://llm/v1", "model": "luna", "api_key_env": "K1", "timeout_s": 180}
    cfg["design"].update(chat_url="http://design/v1", model="sol", api_key_env="K2",
                         timeout_s=90)
    resolved = design.resolve_http_config(cfg)
    assert resolved["chat_url"] == "http://design/v1" and resolved["model"] == "sol"
    assert resolved["api_key_env"] == "K2" and resolved["timeout_s"] == 90
    assert design.resolve_http_config({"design": {}, "llm": {}}) is None
    assert design.resolve_http_config({"design": {"chat_url": "http://x"}, "llm": {}}) is None


def test_http_complete_sends_design_budgets_and_reads_the_reply():
    cfg = {"design": {"chat_url": "", "model": "sol", "api_key_env": "", "timeout_s": 90},
           "llm": {"chat_url": "http://llm/v1/chat", "model": "luna", "api_key_env": "LLM_API_KEY"}}
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "```scad\ncube([1,1,1]);\n```"}}]}).encode()

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        seen["body"] = json.loads(req.data)
        return Resp()

    out = design.http_complete(cfg, [{"role": "user", "content": "make a cube"}], opener=opener)
    assert "cube([1,1,1])" in out
    assert seen["url"] == "http://llm/v1/chat" and seen["timeout"] == 90.0
    assert seen["body"]["model"] == "sol"  # the design model, not the chat model
    assert seen["body"]["max_tokens"] == 4000


def test_http_complete_without_an_endpoint_is_a_plain_error():
    with pytest.raises(design.DesignError, match="no model endpoint"):
        design.http_complete({"design": {}, "llm": {}}, [])


def test_hermes_complete_uses_the_print_design_task():
    seen = {}

    class FakeLlm:
        async def acomplete(self, **kwargs):
            seen.update(kwargs)
            return type("R", (), {"text": "cube([1, 1, 1]);"})()

    out = run(design.hermes_complete(FakeLlm(), [{"role": "user", "content": "x"}],
                                     timeout_s=42))
    assert out == "cube([1, 1, 1]);"
    assert seen["task"] == "print_design" and seen["purpose"] == "print-design"
    assert seen["timeout"] == 42


def test_hermes_complete_trust_refusal_is_plain_words():
    class TrustError(Exception):
        pass

    class FakeLlm:
        async def acomplete(self, **kwargs):
            raise TrustError("PluginLlmTrustError: operator has not granted this")

    with pytest.raises(design.DesignError, match="trust gate"):
        run(design.hermes_complete(FakeLlm(), []))


# --------------------------------------------------------------------- sessions

def test_session_store_roundtrip_and_owner_lookup(tmp_path):
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("owner-1", "chan", "a plate")
    assert (eng.root / session["id"] / "session.json").is_file()
    assert design.find_session_for_owner(eng.root, "owner-1")["id"] == session["id"]
    assert design.find_session_for_owner(eng.root, "someone-else") is None
    eng.cancel(session)
    assert design.find_session_for_owner(eng.root, "owner-1") is None


def test_prune_drops_old_sessions(tmp_path):
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("o", "c", "old")
    folder = eng.root / session["id"]
    data = json.loads((folder / "session.json").read_text())
    data["updated_at"] = 1
    (folder / "session.json").write_text(json.dumps(data))
    assert design.prune_old_sessions(eng.root, now=10 ** 10) == 1
    assert not folder.exists()


# --------------------------------------------------------------------- the turn loop

@needs_scad
def test_turn_renders_validates_and_previews(tmp_path):
    eng = engine(tmp_path, scripted([f"```scad\n{GOOD}```"]))
    session = eng.start_session("o", "c", "a 40x20x3 plate with two 4 mm holes 30 mm apart")
    result = run(eng.run_turn(session, session["description"]))
    assert result["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]
    assert result["geometry"]["watertight"] is True
    assert result["geometry"]["shell_count"] == 1
    assert result["stl_path"].is_file() and result["scad_path"].is_file()
    assert result["preview_path"] and Path(result["preview_path"]).is_file()
    assert "40 × 20 × 3" in design.summary_text(result)


@needs_scad
def test_iteration_edits_the_same_session(tmp_path):
    eng = engine(tmp_path, scripted([GOOD, THICKER]))
    session = eng.start_session("o", "c", "a plate")
    first = run(eng.run_turn(session, "a plate"))
    second = run(eng.run_turn(first["session"], "make it 5 mm thick"))
    assert second["geometry"]["dimensions_mm"] == [40.0, 20.0, 5.0]
    folder = eng.root / session["id"]
    assert (folder / "turn-1.scad").is_file() and (folder / "turn-2.scad").is_file()
    assert (folder / "turn-1.stl").is_file() and (folder / "turn-2.stl").is_file()


@needs_scad
def test_broken_scad_is_repaired_with_the_real_error(tmp_path):
    fake = scripted([SYNTAX_BAD, GOOD])
    eng = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    result = run(eng.run_turn(session, "a plate"))
    assert result["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]
    assert len(fake.calls) == 2
    repair_prompt = fake.calls[1][1]["content"]
    assert "Parser error" in repair_prompt or "ERROR" in repair_prompt


@needs_scad
def test_unrepairable_scad_fails_honestly_and_hands_nothing_on(tmp_path):
    fake = scripted([SYNTAX_BAD])
    eng = engine(tmp_path, fake, )
    eng.repair_attempts = 2
    session = eng.start_session("o", "c", "a plate")
    with pytest.raises(design.DesignError, match="could not render"):
        run(eng.run_turn(session, "a plate"))
    assert len(fake.calls) == 3  # initial + 2 repairs
    assert design.load_session(eng.root, session["id"])["stl"] is None


@needs_scad
def test_empty_geometry_is_rejected(tmp_path):
    eng = engine(tmp_path, scripted([EMPTY]))
    eng.repair_attempts = 0
    session = eng.start_session("o", "c", "nothing at all")
    with pytest.raises(design.DesignError, match="could not render|no solid volume"):
        run(eng.run_turn(session, "nothing at all"))


def test_sandbox_refusal_happens_before_any_render(tmp_path):
    renders = []

    def counting_runner(cmd, **kwargs):
        renders.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    eng = engine(tmp_path, scripted(["import('/etc/passwd');\ncube([1, 1, 1]);"]),
                 runner=counting_runner)
    session = eng.start_session("o", "c", "evil")
    with pytest.raises(design.DesignError, match="may not use"):
        run(eng.run_turn(session, "evil"))
    assert renders == []


def test_model_returning_no_code_is_a_plain_failure(tmp_path):
    eng = engine(tmp_path, scripted(["Sorry, I cannot design that."]))
    session = eng.start_session("o", "c", "a plate")
    with pytest.raises(design.DesignError, match="did not return any OpenSCAD"):
        run(eng.run_turn(session, "a plate"))


def test_render_argv_always_carries_hardwarnings(tmp_path):
    seen = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        out = Path(cmd[cmd.index("-o") + 1])
        out.write_bytes(b"\0" * 200)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    ok, error = design.render_scad(tmp_path / "a.scad", tmp_path / "a.stl", runner=fake_runner)
    assert "--hardwarnings" in seen["cmd"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert "OPENSCADPATH" not in seen["kwargs"]["env"]


def test_turn_cap_stops_an_endless_session(tmp_path):
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_session("o", "c", "a plate")
    session["turns"] = design.MAX_TURNS
    session["scad"] = "turn-1.scad"
    with pytest.raises(design.DesignError, match="turns"):
        run(eng.run_turn(session, "one more"))


# --------------------------------------------------------------------- Stage 2: editing

EDIT_BODY = ("difference() {\n"
             "  scale([1, 1, 2]) base();\n"
             "  translate([0, 0, -1]) cylinder(h = 99, d = 4, $fn = 48);\n"
             "}\n")


def _box_triangles(dx, dy, dz, ox=0.0, oy=0.0, oz=0.0):
    """The 12 triangles of an axis-aligned box (the geometry corpus helper's winding)."""
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz),
         (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _plate_stl(tmp_path, name="plate.stl"):
    from print_handy.core.geometry_layout import write_binary_stl
    path = tmp_path / name
    write_binary_stl(_box_triangles(40, 20, 3, -20, -10, 0), path)
    return path


def _tiny_3mf(tmp_path, name="plate.3mf"):
    """A one-object 40x20x3 3MF, built the way the geometry corpus builds them."""
    import zipfile
    tris = _box_triangles(40, 20, 3, -20, -10, 0)
    verts, index = [], {}
    faces = []
    for tri in tris:
        face = []
        for point in tri:
            if point not in index:
                index[point] = len(verts)
                verts.append(point)
            face.append(index[point])
        faces.append(face)
    model = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
             '  <resources>', '    <object id="1" name="plate" type="model">',
             '      <mesh>', '        <vertices>']
    model += [f'          <vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts]
    model += ['        </vertices>', '        <triangles>']
    model += [f'          <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces]
    model += ['        </triangles>', '      </mesh>', '    </object>', '  </resources>',
              '  <build>', '    <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>',
              '  </build>', '</model>']
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                    '</Types>\n')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
                    '</Relationships>\n')
        zf.writestr("3D/3dmodel.model", "\n".join(model).encode())
    return target


@needs_scad
def test_stl_edit_session_scales_base_and_cuts_a_hole(tmp_path):
    eng = engine(tmp_path, scripted([EDIT_BODY]))
    session = eng.start_edit_session("o", "c", "double the height, centre hole",
                                     _plate_stl(tmp_path))
    assert session["kind"] == "edit" and session["edit_mode"] == "mesh"
    assert session["source"]["type"] == "stl" and session["source"]["origin"] == "file"
    result = run(eng.run_turn(session, "double the height and drill a 4 mm centre hole"))
    dims = result["geometry"]["dimensions_mm"]
    assert dims[0] == pytest.approx(40, abs=0.6)
    assert dims[1] == pytest.approx(20, abs=0.6)
    assert dims[2] == pytest.approx(6, abs=0.6)  # 3 mm plate doubled in Z
    assert "as a mesh" in design.source_note(session)
    # The render file is header + body: exactly one import, and it is the header's.
    composed = Path(result["scad_path"]).read_text()
    assert composed.count("import(") == 1
    assert composed.index("import(") < composed.index("difference()")
    body = Path(result["scad_path"]).with_name("turn-1.body.scad").read_text()
    assert "import(" not in body and "base()" in body


def test_edit_mode_sandbox_refuses_a_model_import_before_render(tmp_path):
    calls = []

    def counting_runner(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("openscad must not run")

    eng = engine(tmp_path, scripted(["cube([1, 1, 1]);\nimport('secret.stl');"]),
                 runner=counting_runner)
    session = eng.start_edit_session("o", "c", "edit", _plate_stl(tmp_path))
    with pytest.raises(design.DesignError, match="may not use"):
        run(eng.run_turn(session, "sneak a file in"))
    assert calls == []


def test_mesh_edit_needs_source_is_plain_words(tmp_path):
    eng = engine(tmp_path, scripted(
        ["NEEDS_SOURCE: the knurling is generated by the source code"]))
    session = eng.start_edit_session("o", "c", "edit", _plate_stl(tmp_path))
    with pytest.raises(design.DesignError, match="needs the original design source"):
        run(eng.run_turn(session, "make the knurling coarser"))


@needs_scad
def test_scad_edit_changes_the_copy_and_never_the_original(tmp_path):
    import hashlib
    source = tmp_path / "widget.scad"
    source.write_text(GOOD)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    cmds = []

    def spy_runner(cmd, **kwargs):
        cmds.append(cmd)
        return subprocess.run(cmd, **kwargs)

    eng = engine(tmp_path, scripted([THICKER]), runner=spy_runner)
    session = eng.start_edit_session("o", "c", "make it thicker", source)
    assert session["edit_mode"] == "scad" and session["scad_deps"] == []
    result = run(eng.run_turn(session, "make it 5 mm thick"))
    assert result["geometry"]["dimensions_mm"][2] == pytest.approx(5, abs=0.6)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert "plate_z = 5" in Path(result["scad_path"]).read_text()
    # The one parametric mechanism is editing values in the source: no -D overrides.
    assert cmds and all(not any(arg.startswith("-D") for arg in cmd) for cmd in cmds)
    assert "in source form" in design.source_note(session)


def test_scad_edit_with_external_references_is_flagged_plainly(tmp_path):
    source = tmp_path / "assembly.scad"
    source.write_text("use <lib.scad>\nimport('mesh.stl');\nwidget();\n")
    eng = engine(tmp_path, scripted([GOOD]))
    session = eng.start_edit_session("o", "c", "edit", source)
    assert session["scad_deps"] == ["import", "use"]
    note = design.source_note(session)
    assert "references other files" in note and "import" in note and "use" in note


def test_3mf_converts_through_the_geometry_loader(tmp_path):
    from print_handy.core.geometry_layout import describe_triangles
    from print_handy.core.orca_adapter import read_stl_triangles
    out = tmp_path / "converted.stl"
    design.convert_to_stl(_tiny_3mf(tmp_path), out)
    info = describe_triangles(read_stl_triangles(out))
    assert info["dimensions_mm"] == [40.0, 20.0, 3.0]


@needs_scad
def test_3mf_edit_session_renders_from_the_converted_stl(tmp_path):
    eng = engine(tmp_path, scripted([EDIT_BODY]))
    session = eng.start_edit_session("o", "c", "edit", _tiny_3mf(tmp_path))
    assert session["edit_mode"] == "mesh"
    assert session["source"]["converted_from"] == "3mf"
    folder = tmp_path / "cache" / "design-sessions" / session["id"]
    assert (folder / "source.stl").is_file()
    result = run(eng.run_turn(session, "double the height"))
    assert result["geometry"]["dimensions_mm"][2] == pytest.approx(6, abs=0.6)
    assert "converted from the 3MF" in design.source_note(session)


def test_link_edit_session_uses_the_injected_fetcher(tmp_path):
    seen = []

    def fake_fetcher(url, target_dir):
        seen.append(url)
        target = Path(target_dir) / "linked.stl"
        from print_handy.core.geometry_layout import write_binary_stl
        write_binary_stl(_box_triangles(10, 10, 2), target)
        return target

    eng = engine(tmp_path, scripted(["base();"]))
    session = eng.start_edit_session(
        "o", "c", "edit", link_url="https://www.printables.com/model/1-x",
        fetcher=fake_fetcher)
    assert seen == ["https://www.printables.com/model/1-x"]
    assert session["source"]["origin"] == "link" and session["edit_mode"] == "mesh"
    with pytest.raises(design.DesignError, match="exactly one source"):
        eng.start_edit_session("o", "c", "edit")
    with pytest.raises(design.DesignError, match="can edit"):
        eng.start_edit_session("o", "c", "edit", tmp_path / "notes.txt")


# ------------------------------------------------- configured render timeout


def test_render_timeout_comes_from_design_config(tmp_path):
    # P2-152/P2-157: [design] timeout_s bounds rendering, not only the model call.
    cfg = {"design": {"timeout_s": 42, "repair_attempts": 3}}
    eng = design.DesignEngine(tmp_path / "cache", scripted([]), cfg=cfg)
    assert eng.render_timeout_s == 42


def test_render_timeout_defaults_without_config(tmp_path):
    eng = design.DesignEngine(tmp_path / "cache", scripted([]), cfg={"design": {}})
    assert eng.render_timeout_s == design.RENDER_TIMEOUT_S


def test_render_timeout_explicit_override_wins_over_config(tmp_path):
    cfg = {"design": {"timeout_s": 42, "repair_attempts": 3}}
    eng = design.DesignEngine(tmp_path / "cache", scripted([]), cfg=cfg, render_timeout_s=7)
    assert eng.render_timeout_s == 7
