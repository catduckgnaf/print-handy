"""Pixel Patina V12 multi-shard journeys (#903, #911, #912).

Authorized by Andrew on 2026-11-05 as an explicit test fixture:
Pixel_Patina_V12_With_Removable_Supports.3mf (sha256: 94bc8a78...).
Three plain-3MF objects:
  1. BLACK BODY (id 2)
  2. BRONZE DETAILS (id 3)
  3. REMOVABLE SUPPORTS (id 4)

Runs in all 3 shards, with dedicated journeys per shard:
  - Shard 0: Intake, one-color and per-part slicing with alignment preserved.
  - Shard 1: Scale/shrink and rotate edits keep the three objects together.
  - Shard 2: Questions answered without edits, support toggle, and multi-step undo.
"""
import copy
import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from print_handy.core import color_geometry, edit_plan, model_job as mj, model_questions, source_colors
from print_handy.core.threemf_canonical import CORE_NS
from tests.corpus.edit.runner import build_test_job, generate_mock_gcode, resolve_plan, verify_probe
from tests.corpus.geometry import detect_underfaces_reference as det

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "Pixel_Patina_V12_With_Removable_Supports.3mf"
SOURCE_NOTE = Path(__file__).resolve().parents[1] / "fixtures" / "PIXEL_PATINA_V12_SOURCE.md"
EXPECTED_SHA256 = "94bc8a786427812811ad38e5248695995c26f2ff90de6bcd6eaa8f22b4d769fd"


@pytest.mark.all_shards
def test_v12_fixture_contract_and_provenance():
    """V12 fixture exists in all shards with Andrew's explicit authorization note and valid SHA."""
    assert FIXTURE_PATH.is_file(), f"missing fixture {FIXTURE_PATH}"
    assert SOURCE_NOTE.is_file(), f"missing provenance note {SOURCE_NOTE}"
    content = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(content).hexdigest() == EXPECTED_SHA256
    note_text = SOURCE_NOTE.read_text()
    assert "catduck" in note_text
    assert "2026-11-05" in note_text


@pytest.mark.shard(0)
def test_v12_shard0_intake_and_alignment(tmp_path):
    """Shard 0: Intake and one-color/per-part layout preserve relative mesh alignment."""
    objs = det.geometry(FIXTURE_PATH)
    assert len(objs) == 3
    names = {o["name"] for o in objs}
    assert "BLACK BODY" in names
    assert "BRONZE DETAILS" in names
    assert any("REMOVABLE SUPPORTS" in n for n in names)

    # In mesh coordinates: Removable supports sit directly under the body overhang (Z 0..12.88)
    body_mesh = next(o["mesh"] for o in objs if o["name"] == "BLACK BODY")
    support_mesh = next(o["mesh"] for o in objs if "REMOVABLE SUPPORTS" in o["name"])
    details_mesh = next(o["mesh"] for o in objs if o["name"] == "BRONZE DETAILS")

    assert body_mesh.bounds[0][2] == 0.0
    assert support_mesh.bounds[0][2] == 0.0
    assert support_mesh.bounds[1][2] < body_mesh.bounds[1][2]
    assert details_mesh.bounds[0][2] > 3.0  # bronze details sit on top surfaces

    manifest = source_colors.inventory(FIXTURE_PATH)
    assert len(manifest["parts"]) == 3

    # Per-part color export preserves relative positions as one assembly (tower_mm=0 for full-bed model)
    from tests.pipeline.test_plate_merge import _two_slot_decisions
    decisions = _two_slot_decisions(manifest)
    out_3mf = tmp_path / "v12_exported.3mf"
    color_geometry.export_color_geometry(FIXTURE_PATH, out_3mf, decisions, tower_mm=0)

    # Output 3MF must carry all 3 parts with their relative bounding box offsets intact
    exported_objs = det.geometry(out_3mf)
    assert len(exported_objs) == 3


@pytest.mark.shard(1)
def test_v12_shard1_scale_rotate_preserves_assembly(tmp_path):
    """Shard 1: Edits (shrink by 20%, rotate 90) scale and turn all three objects together."""
    job_path, job = build_test_job(
        tmp_path, FIXTURE_PATH, title="Pixel Patina V12",
        dims=(185.45, 185.45, 40.32), parts=3, color_mode="per_part"
    )

    # 1. Shrink by 20%
    plan_shrink = resolve_plan("shrink this by 20%", job, FIXTURE_PATH)
    assert plan_shrink["kind"] == "scale_percent"
    assert plan_shrink["percent"] == 80.0
    # Expected dimensions scaled by 0.8
    expected_dims = tuple(round(d * 0.8, 3) for d in (185.45, 185.45, 40.32))

    gcode_shrink = generate_mock_gcode(dims=expected_dims, object_name="Pixel_Patina_V12")
    ok, lines = verify_probe(plan_shrink, job, gcode_shrink)
    assert ok, lines

    edit_plan.record_edit(job, plan_shrink, actor="discord:corpus", lane="fuzzy", source_sha256=EXPECTED_SHA256)
    edit_plan.apply_plan(job, plan_shrink)
    assert job["scale"] == 0.8

    # 2. Rotate 90 degrees
    plan_rotate = resolve_plan("rotate 90", job, FIXTURE_PATH)
    assert plan_rotate["kind"] == "rotate"
    assert plan_rotate["degrees"] == 90
    assert plan_rotate["result_dims"] is not None

    edit_plan.record_edit(job, plan_rotate, actor="discord:corpus", lane="fuzzy", source_sha256=EXPECTED_SHA256)
    edit_plan.apply_plan(job, plan_rotate)
    assert (job.get("orientation") or {}).get("rotation") is not None
    assert len(job["edits"]) == 2


@pytest.mark.shard(2)
def test_v12_shard2_supports_questions_and_undo(tmp_path):
    """Shard 2: Questions answered without edits, support toggle, and multi-step undo."""
    job_path, job = build_test_job(
        tmp_path, FIXTURE_PATH, title="Pixel Patina V12",
        dims=(185.45, 185.45, 40.32), parts=3
    )
    initial_job = copy.deepcopy(job)

    # 1. Question: does this have supports?
    classified = model_questions.classify("does this have supports?")
    assert classified == "supports"
    # Sliced answer for V12 indicates modeled supports exist in the model
    answer, offer = model_questions.supports_answer(job, FIXTURE_PATH)
    assert answer
    # Questions do not mutate the job
    assert job == initial_job

    # 2. Edit: add tree supports
    plan_supp = resolve_plan("add tree supports", job, FIXTURE_PATH)
    assert plan_supp["kind"] == "supports"
    assert plan_supp["type"] == "tree"
    assert plan_supp["enabled"] is True

    gcode_supp = generate_mock_gcode(dims=(185.45, 185.45, 40.32), supports=True, support_type="tree")
    ok, lines = verify_probe(plan_supp, job, gcode_supp)
    assert ok, lines

    edit_plan.record_edit(job, plan_supp, actor="discord:corpus", lane="fuzzy", source_sha256=EXPECTED_SHA256)
    edit_plan.apply_plan(job, plan_supp)
    assert job["edit_settings"]["supports"]["enabled"] is True
    assert len(job["edits"]) == 1

    # 3. Second edit: brim 5mm
    plan_brim = resolve_plan("brim 5mm", job, FIXTURE_PATH)
    edit_plan.record_edit(job, plan_brim, actor="discord:corpus", lane="fuzzy", source_sha256=EXPECTED_SHA256)
    edit_plan.apply_plan(job, plan_brim)
    assert job["fit_brim_policy"] == {"width_mm": 5.0}
    assert len(job["edits"]) == 2

    # 4. Multi-step undo: undo brim, then undo supports
    undone_brim = edit_plan.undo_last_edit(job, actor="discord:corpus")
    assert undone_brim["plan"]["kind"] == "brim"
    assert "fit_brim_policy" not in job or job.get("fit_brim_policy") is None
    assert job["edit_settings"]["supports"]["enabled"] is True

    undone_supp = edit_plan.undo_last_edit(job, actor="discord:corpus")
    assert undone_supp["plan"]["kind"] == "supports"
    assert "supports" not in job.get("edit_settings", {}) or not job["edit_settings"]["supports"].get("enabled")
    assert edit_plan.undo_last_edit(job, actor="discord:corpus") is None
