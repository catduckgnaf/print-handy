"""A multi-part AI design through the real intake and next-plate flow (#1204 item 4).

The design's Slice it runs the plugin's real handoff into the real
prepared intake (the offline fixture of test_prepared_intake: fake slicer,
no site, no printer), and the project it records continues plate by plate
through the same plan, completion summary and /print-next gate an upload
uses. The design model is a scripted fake and the renderer writes real box
meshes; no chat platform, network, printer or paid API is touched.
"""
import asyncio
import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy.core import design, prepared_intake, printer_state
from print_handy.hermes import _config
from .helpers import external
from .test_prepared_intake import Adapter, env, state  # noqa: F401  (env is the fixture)

print_next = external("print_next")
print_records = external("print_records")

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("print_feedback_design_project_1204",
                                              ROOT / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

THREE_PART = "\n".join([
    "// Part: Mac mini rack cradle",
    "// Size: 200 x 40 x 10 mm",
    "// Parts: center tray, left arm, right arm",
    "part = 0; // 0 = all parts side by side, N = part N alone",
    "w1 = 60; // tray width", "w2 = 50; // left arm width", "w3 = 50; // right arm width",
    "d = 40; // depth", "height = 10; // thickness",
    "if (part == 0 || part == 1) cube([w1, d, height]);",
    "if (part == 0 || part == 2) translate([part == 0 ? 70 : 0, 0, 0]) cube([w2, d, height]);",
    "if (part == 0 || part == 3) translate([part == 0 ? 130 : 0, 0, 0]) cube([w3, d, height]);",
    ""])


def _box(dx, dy, dz, ox=0.0):
    v = [(ox, 0, 0), (ox + dx, 0, 0), (ox + dx, dy, 0), (ox, dy, 0),
         (ox, 0, dz), (ox + dx, 0, dz), (ox + dx, dy, dz), (ox, dy, dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _runner(cmd, **kwargs):
    """Fake openscad: part = N renders box N alone, part = 0 all of them in a row."""
    from print_handy.core.geometry_layout import write_binary_stl
    p = design.scad_params(Path(cmd[-1]).read_text(encoding="utf-8"))
    widths = [p[k] for k in ("w1", "w2", "w3") if k in p]
    part = int(p.get("part", 0))
    tris, x = [], 0.0
    for n, w in enumerate(widths, 1):
        if part in (0, n):
            tris += _box(w, p["d"], p["height"], ox=x if part == 0 else 0.0)
        x += w + 10
    write_binary_stl(tris, Path(cmd[cmd.index("-o") + 1]))
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _designed(tmp_path, reply):
    cfg = {"paths": {"cache": tmp_path / "cache"},
           "printer": {"bed_mm": [220, 220], "max_print_height_mm": 250}}
    eng = design.DesignEngine(tmp_path / "cache", lambda messages: reply, cfg=cfg,
                              runner=_runner, preview=lambda stl: None)
    session = eng.start_session("c1::u", "c1", "a rack cradle for a mac mini")
    result = asyncio.run(eng.run_turn(session, "a rack cradle for a mac mini"))
    eng.mark_handed_off(result["session"])
    return cfg, result


def _hand_off(env, cfg, result, picks):
    """The plugin's real Slice it handoff into the real prepared intake."""
    adapter = Adapter(picks)
    source = S(platform=S(value="discord"), profile="3dprinting", chat_id="c1", thread_id=None,
               user_id="u")
    event = S(source=source, text="/print-design a rack cradle for a mac mini",
              message_id="design-msg", metadata={})
    gateway = S(_session_key_for_source=lambda s: "session-key")
    real_prepare = prepared_intake.prepare_link

    async def prepare(event_, adapter_, state_, root, **kw):
        return await real_prepare(event_, adapter_, state_, env.root, timeout=1, **kw)

    previous = _config.cfg()
    _config.set_override(dict(previous, paths=dict(previous.get("paths") or {},
                                                   cache=cfg["paths"]["cache"])))
    try:
        with mock.patch.object(plugin, "_routed_printer_id", return_value=None), \
                mock.patch.object(plugin, "_undecided_printer_ids", return_value=None), \
                mock.patch.object(printer_state, "load", return_value=state()), \
                mock.patch.object(prepared_intake, "prepare_link", side_effect=prepare):
            out = asyncio.run(plugin._design_handoff(event, gateway, adapter, result["session"],
                                                     result["stl_path"]))
    finally:
        _config.set_override(previous)
    jobs = list((env.tmp / "archive").glob("*/JOB.json"))
    return adapter, out, (json.loads(jobs[0].read_text()) if jobs else None), jobs


def test_a_multi_part_design_becomes_one_project_v1_project(env, tmp_path):
    cfg, result = _designed(tmp_path, THREE_PART)
    adapter, _out, job, _jobs = _hand_off(env, cfg, result, [0, 1, 0, 3])
    # The upload path's own first question, for the design's three parts.
    assert adapter.questions[0]["choices"][:2] == ["Print all 3", "Choose files"]
    assert "center tray, left arm and right arm" in adapter.questions[0]["question"]
    project = job["project"]
    assert project["schema"] == "project_v1"
    assert project["source_kind"] == job["project_source_kind"] == "ai_design"
    assert project["project_id"] == job["project_id"] == f"project-{job['intake_id']}"
    assert project["title"] == job["title"] == "Mac mini rack cradle"
    assert [c["component_id"] for c in project["components"]] == ["source1", "source2", "source3"]
    assert [c["design_part"] for c in project["components"]] == [
        "center tray", "left arm", "right arm"]
    assert all(c["required"] and c["quantity_required"] == 1 for c in project["components"])
    assert project["design"] == {"session_id": result["session"]["id"], "turn": 1}
    assert len(job["combined_objects"]) == 3
    assert all(Path(s["downloaded_file"]).is_file() for s in job["selected_sources"])
    # Slice it was not an approval: intake still asked its own colour and
    # start questions, the start question was answered Cancel, and nothing
    # was started.
    assert adapter.questions[-1]["question"].startswith("Ready to slice")
    assert adapter.questions[-1]["choices"][-1] == "Cancel print request"
    assert not env.starts


def _split(env, job, plan_id):
    """Record the job as the slicer's three-plate split, the way intake does."""
    jp = Path(job["downloaded_file"]).parents[1] / "JOB.json"
    sliced = jp.parent / "sliced"
    sliced.mkdir(exist_ok=True)
    gcodes, rows = [], []
    for n in range(1, 4):
        g = sliced / f"plate_{n}.gcode"
        g.write_text(f"; plate {n}\nG1 X1\n")
        gcodes.append(g)
        rows.append({"gcode": str(g), "plate_id": n, "part_keys": [f"source{n}"]})
    job = dict(job, derived_plates=rows)
    jp.write_text(json.dumps(job))
    manifest = {"parts": [{"key": f"source{n}", "name": c["name"]}
                          for n, c in enumerate(job["project"]["components"], 1)]}
    plans = env.tmp / "Plans"
    plans.mkdir(exist_ok=True)
    with mock.patch.object(prepared_intake._external.load("discord_print_summary"),
                           "plate_estimates", return_value=[]):
        plan = prepared_intake.write_split_plan(jp, job, manifest, gcodes, plan_id,
                                                plans_dir=plans)
    return plan, plans, gcodes


def _dispatch_next(plans, pick, archive):
    """/print-next through the plugin hook; returns (questions, started gcodes)."""
    questions, started = [], []

    class Transport:
        async def send_clarify(self, **kwargs):
            questions.append(kwargs)
            import tools.clarify_gateway as cm
            cm.resolve_gateway_clarify(kwargs["clarify_id"], kwargs["choices"][pick])
            return S(success=True)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return S(success=True)

    adapter = Transport()
    src = S(platform=S(value="discord"), profile="3dprinting", user_id="42", chat_id="c",
            thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True,
                _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: "session-key")

    async def fake_start(target):
        started.append(target["gcode"])
        return True, "START REQUEST ACCEPTED"

    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, "load_plan", lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, "start", fake_start), \
            mock.patch.object(print_next, "resolve_next",
                              lambda plan, part_id=None: real_resolve(plan, archive, part_id)):
        asyncio.run(plugin.print_next_dispatch(
            S(source=src, text="/print-next design-plan", internal=False), gateway))
    return questions, started


def _complete(plans, plate, gcode):
    with mock.patch.object(print_records, "PLANS_DIR", plans):
        return print_records.update_multipart_plan({
            "multipart_plan_id": "design-plan", "part_id": f"plate{plate}",
            "gcode_file": Path(gcode).name, "job_key": Path(gcode).name,
            "started_at": f"2026-11-10T0{plate}:00:00Z",
            "completed_at": f"2026-11-10T0{plate}:30:00Z", "success": True})


def test_design_plates_continue_through_next_plate_and_each_start_is_gated(env, tmp_path):
    cfg, result = _designed(tmp_path, THREE_PART)
    _adapter, _out, job, _jobs = _hand_off(env, cfg, result, [0, 1, 0, 3])
    plan, plans, gcodes = _split(env, job, "design-plan")
    # The same plan shape an upload's split gets, labelled as the design's project.
    assert plan["project_id"] == job["project_id"] and plan["source_kind"] == "ai_design"
    assert plan["project_run_id"] == "original"
    assert [p["project_plate_suffix"] for p in plan["parts"]] == ["A", "B", "C"]
    assert [p["label"] for p in plan["parts"]] == ["center-tray", "left-arm",
                                                   "right-arm"]
    archive = env.tmp / "archive"

    # Plate A prints and completes: the summary offers the remaining plates,
    # and nothing starts by itself.
    assert _complete(plans, 1, gcodes[0]) == ("design-plan", False)
    saved = print_next.load_plan("design-plan", plans)
    assert [p["id"] for p in print_records.remaining_plate_choices(saved)] == ["plate2", "plate3"]
    summary = print_records.format_multipart_summary(saved, "plate1")
    assert "/print-next design-plan" in summary
    assert not env.starts

    # The next plate asks its own start question; Cancel starts nothing.
    questions, started = _dispatch_next(plans, pick=1, archive=archive)
    assert len(questions) == 1 and started == []
    assert questions[0]["choices"] == [print_next.START_LABEL, print_next.CANCEL_LABEL]

    # Start answered for plate B starts exactly plate B.
    questions, started = _dispatch_next(plans, pick=0, archive=archive)
    assert len(questions) == 1 and started == [str(gcodes[1].resolve())]

    # Plate B completes; plate C does not inherit plate B's approval: it
    # asks again, and Cancel there starts nothing.
    assert _complete(plans, 2, gcodes[1]) == ("design-plan", False)
    questions, started = _dispatch_next(plans, pick=1, archive=archive)
    assert len(questions) == 1 and started == []
    assert _complete(plans, 3, gcodes[2]) == ("design-plan", True)


def test_a_single_part_design_goes_through_intake_exactly_as_before(env, tmp_path):
    single = ("// Part: Test plate\n// Size: 40 x 20 x 3 mm\n"
              "w1 = 40; // width\nd = 20; // depth\nheight = 3; // thickness\n"
              "cube([w1, d, height]);\n")
    cfg, result = _designed(tmp_path, single)
    assert result["parts"] == []
    adapter, _out, job, _jobs = _hand_off(env, cfg, result, [0, 3])
    assert job["site"] == "upload" and "design" not in job
    assert "project" not in job and "combined_objects" not in job
    assert Path(job["downloaded_file"]).name.endswith("turn-1.stl")
    assert [q["question"].split(" ")[0] for q in adapter.questions] == ["Choose", "Ready"]
    assert adapter.questions[0]["question"] == "Choose the color for turn-1.stl"
    assert not env.starts

