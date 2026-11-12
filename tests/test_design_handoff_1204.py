"""Offline tests for the multi-part AI design handoff (#1204 item 4).

A design that declares several parts is rendered and checked part by part,
and Slice it hands the parts to prepared intake together, the way several
files dropped in one message are handed over. A single-part design is
handed over exactly as before. The model is a scripted fake and the
renderer an injected runner that writes real box meshes from the source's
own parameters; no OpenSCAD, network, chat platform or printer is used.
The real intake run on these handoffs is in
tests/pipeline/test_design_project_handoff_1204.py.
"""
import asyncio
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import design, prepared_intake, printer_state, projects
from print_handy.core import prompts as core_prompts
from print_handy.runtime import _config

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("print_feedback_design_1204",
                                              ROOT / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

CFG = {"design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                  "timeout_s": 180, "repair_attempts": 2},
       "printer": {"bed_mm": [220, 220], "max_print_height_mm": 250}}


def two_part(w1=60, w2=50, *, parts_line="base plate, top arm", selector=True, extra=""):
    lines = ["// Part: Two-piece shelf bracket", f"// Size: {w1 + w2 + 10} x 40 x 10 mm",
             "// Assumes: M4 screws join the parts", f"// Parts: {parts_line}"]
    if selector:
        lines.append("part = 0; // 0 = all parts side by side, N = part N alone")
    lines += [f"w1 = {w1}; // base width", f"w2 = {w2}; // arm width", "d = 40; // depth",
              "height = 10; // thickness", extra,
              "if (part == 0 || part == 1) cube([w1, d, height]);",
              "if (part == 0 || part == 2) translate([part == 0 ? w1 + 10 : 0, 0, 0]) "
              "cube([w2, d, height]);", ""]
    return "\n".join(lines)


SINGLE = ("// Part: Test plate\n// Size: 40 x 20 x 3 mm\n"
          "w1 = 40; // width\nd = 20; // depth\nheight = 3; // thickness\ncube([w1, d, height]);\n")


def _box(dx, dy, dz, ox=0.0):
    v = [(ox, 0, 0), (ox + dx, 0, 0), (ox + dx, dy, 0), (ox, dy, 0),
         (ox, 0, dz), (ox + dx, 0, dz), (ox + dx, dy, dz), (ox, dy, dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


class PartRunner:
    """A fake openscad: part = 0 (or no selector) renders every box side by
    side, part = N renders box N alone at the origin. BROKEN<N> in the source
    makes part N fail like a parser error."""

    def __init__(self):
        self.parts_rendered = []

    def __call__(self, cmd, **kwargs):
        from print_handy.core.geometry_layout import write_binary_stl
        scad = Path(cmd[-1]).read_text(encoding="utf-8")
        p = design.scad_params(scad)
        part = int(p.get("part", 0))
        self.parts_rendered.append(part)
        if f"BROKEN{part}" in scad:
            return subprocess.CompletedProcess(
                cmd, 1, "", f"ERROR: Parser error in part {part}, line 9: syntax error")
        widths = [p[k] for k in ("w1", "w2") if k in p]
        d, h = p.get("d", 20), p.get("height", 3)
        if part:
            tris = _box(widths[part - 1], d, h)
        else:
            tris, x = [], 0.0
            for w in widths:
                tris += _box(w, d, h, ox=x)
                x += w + 10
        write_binary_stl(tris, Path(cmd[cmd.index("-o") + 1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")


def scripted(replies):
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


def engine(tmp_path, complete):
    runner = PartRunner()
    return design.DesignEngine(tmp_path / "cache", complete, cfg=CFG, runner=runner,
                               preview=lambda stl: None), runner


# ------------------------------------------------------------- the engine

def test_a_declared_multi_part_design_renders_and_checks_each_part(tmp_path):
    eng, runner = engine(tmp_path, scripted([two_part()]))
    session = eng.start_session("o", "c", "a two-piece shelf bracket")
    result = asyncio.run(eng.run_turn(session, "a two-piece shelf bracket"))
    assert runner.parts_rendered == [0, 1, 2]  # the layout for the preview, then each part
    assert [(p["index"], p["name"]) for p in result["parts"]] == [(1, "base plate"),
                                                                   (2, "top arm")]
    assert [p["geometry"]["dimensions_mm"] for p in result["parts"]] == [[60.0, 40.0, 10.0],
                                                                          [50.0, 40.0, 10.0]]
    # Two side-by-side pieces in the layout are the design, not a defect.
    assert result["findings"] == []
    folder = eng.root / session["id"]
    saved = design.load_session(eng.root, session["id"])
    assert [p["stl"] for p in saved["parts"]] == ["turn-1.part1.stl", "turn-1.part2.stl"]
    assert all((folder / p["stl"]).is_file() for p in saved["parts"])
    assert not list(folder.glob("turn-1-try*"))
    lines = "\n".join(design.reply_lines(result))
    assert ("2 parts, each checked and printed on its own: 1. base plate 60 × 40 × 10 mm; "
            "2. top arm 50 × 40 × 10 mm.") in lines
    assert "part = 0" not in lines  # the selector is not a design parameter to tune


def test_the_prompt_says_how_to_declare_several_parts(tmp_path):
    fake = scripted([SINGLE])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    asyncio.run(eng.run_turn(session, "a plate"))
    system = fake.calls[0][0]["content"]
    assert "// Parts:" in system and "part = 0;" in system and "if (part == 0 || part == N)" in system


def test_a_part_that_does_not_render_is_repaired_by_name(tmp_path):
    fake = scripted([two_part(extra="// BROKEN2"), two_part()])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a two-piece shelf bracket")
    result = asyncio.run(eng.run_turn(session, "a two-piece shelf bracket"))
    assert len(fake.calls) == 2
    assert "part 2 (top arm) did not render" in fake.calls[1][1]["content"]
    assert len(result["parts"]) == 2


def test_a_part_that_does_not_fit_is_named_in_the_findings(tmp_path):
    def oversized(_messages):
        return two_part(w2=300)
    eng, _ = engine(tmp_path, oversized)
    session = eng.start_session("o", "c", "a bracket")
    result = asyncio.run(eng.run_turn(session, "a bracket"))
    assert result["findings"] and all(text.startswith("part 2 (top arm): it does not fit")
                                      for _level, text in result["findings"])


@pytest.mark.parametrize("source,needle", [
    (two_part(selector=False), "no top-level selector 'part = 0;'"),
    (two_part(parts_line="arm, Arm"), "names the same part twice"),
])
def test_a_malformed_parts_declaration_goes_back_to_the_model(tmp_path, source, needle):
    fake = scripted([source, two_part()])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a bracket")
    result = asyncio.run(eng.run_turn(session, "a bracket"))
    assert needle in fake.calls[1][1]["content"]
    assert len(result["parts"]) == 2


def test_one_name_or_no_parts_line_is_a_single_part_design(tmp_path):
    assert design.design_parts(two_part(parts_line="just one")) == []
    assert design.design_parts(SINGLE) == []
    eng, runner = engine(tmp_path, scripted([SINGLE]))
    session = eng.start_session("o", "c", "a plate")
    result = asyncio.run(eng.run_turn(session, "a plate"))
    assert runner.parts_rendered == [0] and result["parts"] == []
    assert design.load_session(eng.root, session["id"])["parts"] is None
    assert design.multipart_handoff(eng.root, session) == ([], None)
    assert any(line.startswith("Measured: 40 × 20 × 3 mm") for line in design.reply_lines(result))


def test_undo_restores_the_earlier_parts_without_rendering_them_again(tmp_path):
    eng, runner = engine(tmp_path, scripted([two_part(), two_part(w1=70)]))
    session = eng.start_session("o", "c", "a bracket")
    asyncio.run(eng.run_turn(session, "a bracket"))
    asyncio.run(eng.run_turn(session, "make the base wider"))
    renders = len(runner.parts_rendered)
    back = asyncio.run(eng.run_turn(session, "undo"))
    assert len(runner.parts_rendered) == renders  # every file came from the hash cache
    assert back["parts"][0]["geometry"]["dimensions_mm"] == [60.0, 40.0, 10.0]


def test_multipart_handoff_copies_named_parts_and_records_their_origin(tmp_path):
    eng, _ = engine(tmp_path, scripted([two_part()]))
    session = eng.start_session("o", "c", "a two-piece shelf bracket")
    asyncio.run(eng.run_turn(session, "a two-piece shelf bracket"))
    paths, record = design.multipart_handoff(eng.root, session)
    assert [p.name for p in paths] == ["base-plate.stl", "top-arm.stl"]
    assert all(p.is_file() and p.parent.name == "handoff-turn-1" for p in paths)
    assert record == {"session_id": session["id"], "turn": 1,
                      "title": "Two-piece shelf bracket",
                      "parts": [{"file": "base-plate.stl", "part": "base plate", "index": 1},
                                {"file": "top-arm.stl", "part": "top arm", "index": 2}]}
    (eng.root / session["id"] / "turn-1.part2.stl").unlink()
    with pytest.raises(design.DesignError, match="part 2 \\(top arm\\).*no longer on disk"):
        design.multipart_handoff(eng.root, session)


# ------------------------------------------------- the project builder entry point

def test_from_design_is_from_files_plus_design_provenance():
    rows = [{"name": "base-plate.stl", "downloaded_file": "/a/base-plate.stl"},
            {"name": "top-arm.stl", "downloaded_file": "/b/top-arm.stl"}]
    record = {"session_id": "abc123", "turn": 2, "title": "Bracket",
              "parts": [{"file": "base-plate.stl", "part": "base plate", "index": 1},
                        {"file": "top-arm.stl", "part": "top arm", "index": 2}]}
    project = projects.from_design("intake-9", rows, record)
    upload = projects.from_files("intake-9", "uploaded_files", rows, title="Bracket")
    assert project["schema"] == projects.SCHEMA == "project_v1"
    assert project["source_kind"] == projects.DESIGN_SOURCE_KIND == "ai_design"
    assert project["project_id"] == upload["project_id"] and project["title"] == "Bracket"
    assert [c["design_part"] for c in project["components"]] == ["base plate", "top arm"]
    for mine, theirs in zip(project["components"], upload["components"]):
        assert {k: v for k, v in mine.items() if k != "design_part"} == theirs
    assert project["design"] == {"session_id": "abc123", "turn": 2}
    with pytest.raises(ValueError):
        projects.from_design("intake-9", rows, {})


def test_with_design_origin_only_applies_to_exactly_the_handed_files(tmp_path):
    files = [tmp_path / "base-plate.stl", tmp_path / "top-arm.stl"]
    for f in files:
        f.write_bytes(b"solid x\nendsolid x\n")
    record = {"session_id": "abc", "turn": 1, "title": "Bracket",
              "parts": [{"file": f.name, "part": f.stem, "index": i}
                        for i, f in enumerate(files, 1)]}
    job = prepared_intake.with_design_origin(prepared_intake.upload_job_set(files), record)
    assert job["design"] == record and job["title"] == "Bracket"
    plain = prepared_intake.upload_job_set(files)
    assert prepared_intake.with_design_origin(dict(plain), None) == plain
    other = dict(record, parts=record["parts"][:1])
    assert "design" not in prepared_intake.with_design_origin(prepared_intake.upload_job_set(files),
                                                              other)


@pytest.mark.parametrize("record", [
    ["x"], "abc", 7, {"session_id": "abc", "parts": "base-plate.stl"},
    {"session_id": "abc", "parts": ["base-plate.stl", "top-arm.stl"]},
    {"session_id": "abc", "parts": [{"file": "base-plate.stl"}, None]},
])
def test_malformed_design_handoff_metadata_is_ignored_not_a_crash(tmp_path, record):
    """Review of #1223: a truthy non-dict handoff (or non-dict part rows) raised
    AttributeError and aborted the whole multi-file intake."""
    files = [tmp_path / "base-plate.stl", tmp_path / "top-arm.stl"]
    for f in files:
        f.write_bytes(b"solid x\nendsolid x\n")
    plain = prepared_intake.upload_job_set(files)
    before = dict(plain)
    job = prepared_intake.with_design_origin(plain, record)
    assert "design" not in job and job == before


def test_from_design_skips_non_dict_part_rows():
    rows = [{"name": "base-plate.stl", "file": "base-plate.stl"}]
    project = projects.from_design("intake-9", rows, {"session_id": "abc", "parts": [None, "x"]})
    assert project["design"]["session_id"] == "abc"


# ------------------------------------------------------ Slice it, through dispatch

class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        return S(success=True)


@pytest.fixture
def design_env(tmp_path):
    cfg = {"bot": {"mode": "standalone"},
           "design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                      "timeout_s": 180, "repair_attempts": 3},
           "llm": {"chat_url": "", "model": "", "api_key_env": "LLM_API_KEY", "timeout_s": 20},
           "paths": {"cache": tmp_path / "cache", "state_home": tmp_path},
           "printer": {"bed_mm": [220, 220]},
           "discord": {"question_timeout_s": 5}}
    previous = _config.cfg()
    _config.set_override(cfg)
    real_init = design.DesignEngine.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("runner", PartRunner())
        kwargs.setdefault("preview", lambda stl: None)
        real_init(self, *args, **kwargs)

    with mock.patch.object(design.DesignEngine, "__init__", init):
        yield cfg
    _config.set_override(previous)


def _slice_it(text, reply):
    """Run one design turn, press Slice it, and return what intake was handed."""
    adapter = FakeAdapter()
    handed = []

    async def fake_prepare(event, adapter_, state, root, **kw):
        handed.append(event)
        return {"action": "skip", "reason": "intake handled"}

    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="42", chat_id="c", thread_id=None)
    event = S(source=source, text=text, internal=False, message_id="m1", metadata={})
    gateway = S(_is_user_authorized_for_source=lambda s: True,
                _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: "session-key")
    with mock.patch.object(plugin, "_design_transport", return_value=(scripted([reply]), "http")), \
            mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
            mock.patch.object(core_prompts, "wait_for_response", return_value=design.SLICE_LABEL), \
            mock.patch.object(plugin, "_routed_printer_id", return_value=None), \
            mock.patch.object(plugin, "_undecided_printer_ids", return_value=None), \
            mock.patch.object(prepared_intake, "prepare_link", side_effect=fake_prepare), \
            mock.patch.object(printer_state, "load", return_value=S()):
        out = asyncio.run(plugin.design_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "intake handled"}
    assert len(handed) == 1  # one handoff; the design flow never slices or starts anything
    return adapter, handed[0]


def test_slice_it_hands_every_part_to_intake_as_one_upload(design_env):
    adapter, event = _slice_it("/print-design a two-piece shelf bracket", two_part())
    assert [Path(p).name for p in event.media_urls] == ["base-plate.stl", "top-arm.stl"]
    assert all(Path(p).is_file() for p in event.media_urls)
    record = event.metadata["design_handoff"]
    assert record["title"] == "Two-piece shelf bracket"
    assert [row["part"] for row in record["parts"]] == ["base plate", "top arm"]
    # Exactly the shape of a multi-file drop: no text, so no yolo and no approval rides along.
    assert event.text == "" and event.message_id.startswith("design-")
    assert prepared_intake.model_files(event) == [Path(p) for p in event.media_urls]
    assert any("Handing the 2 parts of the design" in (m or "") for m in adapter.sent)
    root = design.sessions_root(design_env["paths"]["cache"])
    session = design.load_session(root, record["session_id"])
    assert session["status"] == "handed_off"


def test_slice_it_on_a_single_part_design_is_unchanged(design_env):
    adapter, event = _slice_it("/print-design a plate", SINGLE)
    root = design.sessions_root(design_env["paths"]["cache"])
    folder = next(p for p in root.iterdir() if p.is_dir())
    assert event.media_urls == [str(folder / "turn-1.stl")]
    assert "design_handoff" not in event.metadata
    assert not list(folder.glob("handoff-turn-*"))
    assert ("Handing the design to the normal intake — it asks the usual questions before "
            "anything is sliced.") in adapter.sent
