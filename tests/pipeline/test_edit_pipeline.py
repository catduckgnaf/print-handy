"""Edit-plan slice plumbing (#903): slice_job carries an edit's supports
choice without a rotation (the standalone channel that did not exist),
the process-level edit settings reach slice_model on the generic and
per-part paths, the slice recipe (v3) treats edit settings as an input,
and a two-color job's assignments survive a job edit untouched."""
import asyncio
import json
from pathlib import Path

import pytest

from print_handy.core import edit_plan as ep
from print_handy.core import model_job as mj
from print_handy.core import orca_adapter
from .test_color_intake import source, state


def _gcode_for(kwargs):
    """Fake slicer output that reflects the kwargs it was called with."""
    lines = ["G1 X0 Y0 Z0", "G1 X40 Y10 Z1"]
    if kwargs.get("supports"):
        lines.append(";TYPE:Support")
        lines.append("G1 X5 Y5 E1")
    lines.append("EXCLUDE_OBJECT_DEFINE NAME=object1")
    lines.append(f"; enable_support = {'1' if kwargs.get('supports') else '0'}")
    lines.append("; support_type = tree(auto)")
    lines.append("; layer_height = 0.2")
    lines.append("; sparse_infill_density = 15%")
    lines.append("; brim_width = 0")
    return "\n".join(lines) + "\n"


def _recording_fake(calls):
    def slice_fake(src, folder, st, brim=None, **kwargs):
        calls.append(dict(kwargs))
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / "plate_1.gcode"
        out.write_text(_gcode_for(kwargs))
        return out, {"offline_test": True}, []
    return slice_fake


def _edited_job(tmp_path, monkeypatch, edit_settings=None, orientation=None):
    """setup_job's preparation (test_artifact_binding) without its first
    slice: slice_job will not re-slice an already-current artifact, and
    these tests observe the one slice an edit produces."""
    st = state()
    st.update(bed_mm=[220, 220], max_print_height_mm=220, moonraker_url="offline://fake")
    for slot in st["filament_slots"].values():
        slot["physical_color_verified"] = True
    from print_handy.core import source_colors
    p = source(tmp_path)
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps({"downloaded_file": str(p), "color_schema": "part_slots_v1",
                              "color_manifest": source_colors.inventory(p),
                              "audit": {"plates": 1, "size_x": 40, "size_y": 10, "size_z": 1}}))
    mj.intake_color_choices(jp, st, "single_color", [1])
    monkeypatch.setattr(mj.printer_state, "load", lambda: st)
    monkeypatch.setattr(mj.printer_state, "check", lambda _: (True, [], {}))
    monkeypatch.setattr(mj, "verify_gcode", lambda _: None)
    from print_handy.printers.flashforge.zmod import multicolor_contract
    monkeypatch.setattr(multicolor_contract, "finalize", lambda *a, **k: None)
    job = json.loads(jp.read_text())
    if edit_settings is not None:
        job["edit_settings"] = edit_settings
    if orientation is not None:
        job["orientation"] = orientation
    jp.write_text(json.dumps(job))
    return jp, st


def test_standalone_supports_edit_reaches_slice_model_without_rotation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(orca_adapter, "slice_model", _recording_fake(calls))
    jp, _st = _edited_job(tmp_path, monkeypatch,
                          edit_settings={"supports": {"enabled": True, "type": "tree",
                                                      "plate_only": None}})
    mj.slice_job(jp)
    assert calls, "slice_model was never called"
    assert all(call.get("supports") is True for call in calls)
    assert all(call.get("edits", {}).get("support_type") == "tree" for call in calls)
    # And the result verifies against the plan, end to end.
    job = json.loads(jp.read_text())
    ok, lines = ep.verify_slice({"kind": "supports", "enabled": True, "type": "tree"},
                                gcode_text=Path(job["gcode"]).read_text(), before_dims=None)
    assert ok, lines


def test_no_edit_means_no_supports_override(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(orca_adapter, "slice_model", _recording_fake(calls))
    jp, _st = _edited_job(tmp_path, monkeypatch)
    mj.slice_job(jp)
    assert calls and all("supports" not in call or call["supports"] is None
                         or call.get("rotation") for call in calls)
    assert all("edits" not in call for call in calls)


def test_layer_height_infill_and_mirror_ride_as_edits(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(orca_adapter, "slice_model", _recording_fake(calls))
    mirror = ep.mirror_matrix("x")
    jp, _st = _edited_job(
        tmp_path, monkeypatch,
        edit_settings={"layer_height": 0.12, "infill_percent": 25},
        orientation={"mirror": mirror})
    mj.slice_job(jp)
    edits = calls[0]["edits"]
    assert edits["layer_height"] == 0.12 and edits["infill_percent"] == 25.0
    assert edits["mirror"] == mirror


def test_recipe_v3_records_edit_settings_as_an_input(tmp_path, monkeypatch):
    monkeypatch.setattr(orca_adapter, "slice_model", _recording_fake([]))
    jp, _st = _edited_job(tmp_path, monkeypatch,
                          edit_settings={"infill_percent": 25})
    mj.slice_job(jp)
    job = json.loads(jp.read_text())
    assert job["slice_recipe"]["version"] == 3
    assert job["slice_recipe"]["recipe"]["edits"] == {"infill_percent": 25}
    first_sha = job["slice_recipe"]["sha256"]
    job["edit_settings"] = {"infill_percent": 40}
    jp.write_text(json.dumps(job))
    mj.slice_job(jp)
    changed = json.loads(jp.read_text())
    assert changed["slice_recipe"]["sha256"] != first_sha  # an edit never reuses an old slice


def test_two_color_assignments_survive_a_job_edit(tmp_path, monkeypatch):
    """The multi-part rule at the job level: applying edits never touches
    the source bytes or the per-part decisions."""
    p = source(tmp_path)
    before = p.read_bytes()
    decisions = {"mode": "per_part",
                 "choices": {"part:a": {"slot": 1}, "part:b": {"slot": 3}}}
    job = {"downloaded_file": str(p), "title": "Two color",
           "audit": {"size_x": 40, "size_y": 10, "size_z": 1, "parts": 2},
           "color_decisions": decisions, "orientation": {}}
    for plan in ({"kind": "supports", "enabled": True, "type": "tree"},
                 {"kind": "infill", "percent": 30},
                 {"kind": "brim", "enabled": True, "width_mm": 6}):
        record = ep.record_edit(job, plan, actor="discord:42", lane="fuzzy",
                                source_sha256=ep.file_sha256(p))
        ep.apply_plan(job, plan)
    assert p.read_bytes() == before
    assert job["color_decisions"] == decisions
    assert job["edit_settings"]["supports"]["type"] == "tree"
    assert job["edit_settings"]["infill_percent"] == 30
    assert job["fit_brim_policy"] == {"width_mm": 6}
    assert [r["plan"]["kind"] for r in job["edits"]] == ["supports", "infill", "brim"]


def test_per_part_plates_receive_the_edit_settings(tmp_path, monkeypatch):
    """The IFS per-part path: every derived plate is sliced with the
    edit's supports choice and process edits."""
    from print_handy.core import color_geometry
    from print_handy.core.discord_color_intake import collect
    from .test_multicolor_contract import artifact
    from .test_discord_color_intake import Transport
    from print_handy.core import source_colors as _sc
    import copy as _copy

    # The real loaded state (four slots), like the artifact-binding tests:
    # the two-slot fixture state changes the collect question flow.
    st = _copy.deepcopy(mj.printer_state.load())
    p = source(tmp_path)
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps({"downloaded_file": str(p), "color_schema": "part_slots_v1",
                              "color_manifest": _sc.inventory(p),
                              "audit": {"plates": 1, "size_x": 40, "size_y": 10, "size_z": 1}}))
    monkeypatch.setattr(mj.printer_state, "load", lambda: st)
    monkeypatch.setattr(mj.printer_state, "check", lambda _: (True, [], {}))
    monkeypatch.setattr(mj, "verify_gcode_offline", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mj, "finalize_multicolor", lambda *a, **k: None)
    monkeypatch.setattr(mj, "refuse_assembly_split", lambda *a, **k: None)

    def plate_fake(src, target, decisions, part_keys, bed=None, slot_colors=None, **kw):
        Path(target).write_text(json.dumps(part_keys))
        return {"offline_test": True}

    monkeypatch.setattr(color_geometry, "export_single_color_plate", plate_fake)
    setup_calls = []

    def setup_slice(plate, folder, st_, brim, preserve_layout=True, filament_slot=None, **kw):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / "plate_1.gcode"
        out.write_text(artifact(1))
        return out, {"offline_test": True}, []

    monkeypatch.setattr(orca_adapter, "slice_model", setup_slice)
    asyncio.run(collect(jp, st, Transport([0, 0, 3]), "offline", "first", timeout=1))
    job = json.loads(jp.read_text())
    assert job["color_decisions"]["mode"] == "per_part"

    calls = []
    monkeypatch.setattr(orca_adapter, "slice_model", _recording_fake(calls))
    job["edit_settings"] = {"supports": {"enabled": True, "type": "tree", "plate_only": None},
                            "infill_percent": 30}
    jp.write_text(json.dumps(job))
    mj.slice_job(jp)
    assert len(calls) >= 2  # one call per derived plate
    assert all(call.get("supports") is True for call in calls)
    assert all(call.get("edits", {}).get("support_type") == "tree" for call in calls)
    assert all(call.get("edits", {}).get("infill_percent") == 30.0 for call in calls)
    after = json.loads(jp.read_text())
    assert after["color_decisions"] == job["color_decisions"]  # assignments untouched
