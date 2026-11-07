"""#978 follow-ups (formerly #982, #983, #984) for "Fix this for my printer".

Mesh repair depth: designed openings are told apart from damage holes and
are only closed when asked; mixed face orientation is found and fixed; hole
rims through a pinch vertex are split into simple loops; a repair that
changes nothing, leaves non-manifold edges, or changes the volume a lot is
refused; wall thickness is measured and thin walls are named.

Attempt accounting: one fix budget per job across the chat flow and every
intake question, each request attributed to who asked.

Housekeeping: fixes are built in a uniquely named staging folder that is
deleted on decline and committed on apply; the source is re-checked before
applying; "fix it and <edit>" applies the edit too or says plainly that it
did not.
"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from print_handy.core import ai_fix, edit_plan, mesh_repair, support_clearance

from test_ai_fix_chat_978 import _event, fix_env, plugin  # noqa: F401  (shared harness; tests/ is on sys.path)

trimesh = pytest.importorskip("trimesh")


def _write(path, tris):
    support_clearance._write_binary_stl(path, np.asarray(tris, dtype=float))
    return path


def _sphere(radius=10.0, subdivisions=3):
    s = trimesh.creation.icosphere(subdivisions=subdivisions, radius=radius)
    return s.vertices[s.faces], s.volume


def _open_tube(tmp_path):
    cyl = trimesh.creation.cylinder(radius=10, height=30, sections=48)
    tris = cyl.vertices[cyl.faces]
    caps = np.isclose(np.ptp(tris[:, :, 2], axis=1), 0)
    return _write(tmp_path / "tube.stl", tris[~caps])


# ---------------------------------------------------------------- openings

def test_designed_openings_are_named_and_closed_only_when_asked(tmp_path):
    src = _open_tube(tmp_path)
    closed = mesh_repair.repair(src, tmp_path / "closed.stl")
    assert len(closed["openings"]) == 2 and closed["openings_closed"] == 2
    assert closed["holes_filled"] == 0  # openings are not counted as damage
    assert closed["after"]["sound"] is True
    with pytest.raises(mesh_repair.FixError, match="opening"):
        mesh_repair.repair(src, tmp_path / "kept.stl", fill_openings=False)
    assert not (tmp_path / "kept.stl").exists()


def test_a_small_damage_hole_is_filled_without_asking(tmp_path):
    tris, volume = _sphere()
    src = _write(tmp_path / "holed.stl", tris[1:])  # one missing triangle
    report = mesh_repair.repair(src, tmp_path / "fixed.stl", fill_openings=False)
    assert report["holes_filled"] == 1 and report["openings"] == []
    assert report["after"]["sound"] is True
    assert report["after"]["signed_volume_mm3"] == pytest.approx(volume, rel=0.01)


def test_a_rim_through_a_pinch_vertex_is_split_into_simple_loops(tmp_path):
    tris, _volume = _sphere()
    src = _write(tmp_path / "pinch.stl", tris[3:])  # three faces around one vertex
    report = mesh_repair.repair(src, tmp_path / "fixed.stl")
    assert report["after"]["sound"] is True
    assert report["branched_loops_skipped"] == 0


# ---------------------------------------------------------------- orientation

def test_mixed_face_orientation_is_found_and_fixed(tmp_path):
    tris, volume = _sphere()
    mixed = tris.copy()
    mixed[::2] = mixed[::2][:, [0, 2, 1]]
    src = _write(tmp_path / "mixed.stl", mixed)
    facts = mesh_repair.mesh_facts(src)
    # Closed, but its faces disagree: the old facts called this watertight and fine.
    assert facts["watertight"] is True and facts["consistent"] is False
    assert facts["sound"] is False and facts["inconsistent_edges"] > 0
    report = mesh_repair.repair(src, tmp_path / "fixed.stl")
    assert report["faces_reoriented"] > 0
    assert report["after"]["sound"] is True
    assert report["after"]["signed_volume_mm3"] == pytest.approx(volume, rel=0.01)


# ---------------------------------------------------------------- refusals

def test_a_repair_that_changes_nothing_is_refused(tmp_path):
    tris, _volume = _sphere()
    src = _write(tmp_path / "clean.stl", tris)
    with pytest.raises(mesh_repair.FixError, match="nothing"):
        mesh_repair.repair(src, tmp_path / "out.stl")
    assert not (tmp_path / "out.stl").exists()


def test_a_fill_that_changes_the_volume_a_lot_is_refused(tmp_path):
    tris, _volume = _sphere()
    bowl = tris[~np.any(tris[:, :, 2] > -5, axis=1)]  # a ragged, non-flat rim: not an opening
    src = _write(tmp_path / "bowl.stl", bowl)
    with pytest.raises(mesh_repair.FixError, match="volume"):
        mesh_repair.repair(src, tmp_path / "out.stl")


def test_a_result_with_non_manifold_edges_is_refused(tmp_path):
    box = trimesh.creation.box(extents=(10, 10, 10))
    bt = box.vertices[box.faces]
    fin = np.array([[bt[0][0], bt[0][1], bt[0][0] + np.array([0, 0, 30.0])]])
    degenerate = np.array([[[0, 0, 0], [0, 0, 0], [1, 1, 1]]], dtype=float)
    src = _write(tmp_path / "fin.stl", np.vstack([bt, fin, degenerate]))
    with pytest.raises(mesh_repair.FixError, match="non-manifold"):
        mesh_repair.repair(src, tmp_path / "out.stl")
    assert not (tmp_path / "out.stl").exists()


# ---------------------------------------------------------------- wall thickness

def test_thin_walls_are_measured_and_named(tmp_path):
    thin = trimesh.creation.annulus(r_min=9.7, r_max=10, height=30)
    thick = trimesh.creation.annulus(r_min=8, r_max=10, height=30)
    thin_walls = mesh_repair.wall_thickness(_write(tmp_path / "thin.stl", thin.vertices[thin.faces]))
    thick_walls = mesh_repair.wall_thickness(_write(tmp_path / "thick.stl", thick.vertices[thick.faces]))
    assert thin_walls["measured"] and thin_walls["min_mm"] == pytest.approx(0.3, abs=0.05)
    assert thin_walls["thin_fraction"] > 0.2
    assert "thin walls" in mesh_repair.wall_note(thin_walls)
    assert thick_walls["min_mm"] == pytest.approx(2.0, abs=0.05)
    assert mesh_repair.wall_note(thick_walls) is None
    # The same file gives the same numbers (seeded sampling).
    again = mesh_repair.wall_thickness(tmp_path / "thin.stl")
    assert again == thin_walls


def test_wall_measurement_declines_a_mesh_past_its_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(mesh_repair, "MAX_WALL_TRIANGLES", 10)
    tris, _volume = _sphere()
    walls = mesh_repair.wall_thickness(_write(tmp_path / "s.stl", tris))
    assert walls["measured"] is False and "triangles" in walls["reason"]


def test_mesh_report_carries_the_wall_measurement(tmp_path):
    tris, _volume = _sphere()
    report = ai_fix.mesh_report(_write(tmp_path / "s.stl", tris))
    assert report["sound"] is True and report["walls"]["measured"] is True


# ---------------------------------------------------------------- staging

def test_fix_copies_are_staged_uniquely_and_cleaned_up(tmp_path):
    tris, _volume = _sphere()
    src = _write(tmp_path / "model.stl", tris[1:])
    proposal = ai_fix.FixProposal(mesh_repair=True)
    first, report = ai_fix.build_fixed_copy(src, proposal, tmp_path)
    second, _other = ai_fix.build_fixed_copy(src, proposal, tmp_path)
    assert first != second and first.parent != second.parent  # concurrent flows never collide
    assert first.parent.name.startswith(ai_fix.STAGING_PREFIX)
    ai_fix.discard_fixed_copy(second)
    assert not second.parent.exists()
    final = ai_fix.commit_fixed_copy(first, report)
    assert final.parent == tmp_path and final.is_file()
    assert not first.parent.exists()
    assert report["mesh"]["output_path"] == str(final) and "staging" not in report
    assert not list(tmp_path.glob(ai_fix.STAGING_PREFIX + "*"))


def test_a_failed_build_leaves_no_staging_behind(tmp_path):
    tris, _volume = _sphere()
    src = _write(tmp_path / "clean.stl", tris)
    with pytest.raises(ai_fix.FixError):
        ai_fix.build_fixed_copy(src, ai_fix.FixProposal(mesh_repair=True), tmp_path)
    assert not list(tmp_path.glob(ai_fix.STAGING_PREFIX + "*"))


def test_source_unchanged(tmp_path):
    src = tmp_path / "m.stl"
    src.write_bytes(b"one")
    sha = edit_plan.file_sha256(src)
    assert ai_fix.source_unchanged(src, sha, edit_plan.file_sha256) is True
    src.write_bytes(b"two")
    assert ai_fix.source_unchanged(src, sha, edit_plan.file_sha256) is False
    assert ai_fix.source_unchanged(tmp_path / "gone.stl", sha, edit_plan.file_sha256) is False


# ---------------------------------------------------------------- intents

@pytest.mark.parametrize("text, want", [
    ("fix this", (True, None)),
    ("fix it and make it 20% bigger", (True, "make it 20% bigger")),
    ("repair the mesh, then add a brim", (True, "add a brim")),
    ("make it printable and add supports", (True, "add supports")),
    ("fix the infill so it's 20%", (False, None)),
])
def test_split_repair_request(text, want):
    assert ai_fix.split_repair_request(text) == want


def test_a_clear_edit_rides_along_and_an_unclear_one_is_named():
    base = ai_fix.FixProposal(mesh_repair=True,
                              plans=[edit_plan.validate_plan({"kind": "scale_percent", "percent": 90})])
    merged, note = ai_fix.merge_requested_edit(base, "make it 20% bigger")
    assert note is None
    scales = [p for p in merged.plans if p["kind"] == "scale_percent"]
    assert len(scales) == 1 and scales[0]["percent"] == pytest.approx(120)  # the operator's words win
    same, note = ai_fix.merge_requested_edit(base, "do the thing with the stuff")
    assert same is base and "Only the repair was taken" in note


# ---------------------------------------------------------------- budget

def test_the_fix_budget_is_per_job_and_attributed():
    job = {}
    assert ai_fix.attempts_left(job) == ai_fix.MAX_JOB_FIX_ATTEMPTS
    ai_fix.note_attempt(job, actor="discord:1", surface="chat")
    ai_fix.note_attempt(job, actor="discord:2", surface="intake")
    assert ai_fix.attempts_used(job) == 2
    assert [a["actor"] for a in job["ai_fix_attempts"]] == ["discord:1", "discord:2"]
    assert [a["surface"] for a in job["ai_fix_attempts"]] == ["chat", "intake"]


# ---------------------------------------------------------------- chat flow

def test_chat_fix_and_edit_applies_both(fix_env):
    asyncio.run(plugin.edit_dispatch(_event("fix it and make it 20% bigger"), fix_env.gateway))
    question, _choices, _approval = fix_env.confirms[0]
    assert "repair the mesh" in question and "120%" in question
    job = json.loads(fix_env.job_path.read_text())
    assert job["scale"] == pytest.approx(1.2)
    assert job["ai_fix_attempts"][0]["surface"] == "chat"


def test_chat_decline_leaves_no_copy_or_preview_behind(fix_env):
    fix_env.answers = ["Cancel"]
    asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    leftovers = [p.name for p in fix_env.folder.iterdir() if p.name not in ("model.stl", "JOB.json")]
    assert leftovers == []


def test_chat_keep_openings_on_an_open_box_applies_nothing(fix_env):
    fix_env.answers = [ai_fix.APPLY_KEEP_OPENINGS_LABEL]
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result["reason"] == "repair: nothing left with openings kept"
    assert json.loads(fix_env.job_path.read_text())["downloaded_file"].endswith("model.stl")
    assert not list(fix_env.folder.glob(ai_fix.STAGING_PREFIX + "*"))


def test_chat_out_of_budget_asks_nothing(fix_env, monkeypatch):
    calls = []
    monkeypatch.setattr(ai_fix, "MAX_JOB_FIX_ATTEMPTS", 0)
    monkeypatch.setattr(ai_fix, "propose_fix", lambda *a, **k: calls.append(a))
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result["reason"] == "repair: out of fix attempts"
    assert calls == [] and fix_env.confirms == []


def test_chat_source_changed_before_apply_is_not_applied(fix_env, monkeypatch):
    model = fix_env.folder / "model.stl"
    real_confirm = plugin._confirm

    async def confirm_after_edit(*args, **kwargs):
        model.write_bytes(model.read_bytes() + b"\0")  # someone replaced the file meanwhile
        return await real_confirm(*args, **kwargs)
    monkeypatch.setattr(plugin, "_confirm", confirm_after_edit)
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result["reason"] == "repair: source changed"
    assert json.loads(fix_env.job_path.read_text())["downloaded_file"] == str(model)
    assert not list(fix_env.folder.glob(ai_fix.STAGING_PREFIX + "*"))
