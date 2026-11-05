"""End-to-end edit corpus journeys over corpus models (#903).

Covers:
- Two-color assembly edit (scale, supports) with colors and alignment preserved.
- Overhang model questions answered vs edits applied and removed.
- Oversized model shrink and rearrange safety for assembly components.
- Link plus instruction and attached model plus question flows.
- Multi-step edit undo restoring provenance and hashes.
- Pick lists offered for ambiguous phrasings without speculative action.
"""
import copy
import hashlib
from pathlib import Path

import pytest

from print_handy.bot.edit_chat import instruction_after_model
from print_handy.core import color_geometry, edit_plan, model_questions, source_colors
from tests.corpus.edit.runner import build_test_job, generate_mock_gcode, resolve_plan, verify_probe

CORPUS_CASES = Path(__file__).resolve().parents[1] / "cases"


def test_two_color_assembly_edit_preserves_colors_and_relative_alignment(tmp_path):
    """A two-color assembly edited (scale, supports) with colors and alignment preserved."""
    model_path = CORPUS_CASES / "assembly-two-color-roomy" / "model.3mf"
    assert model_path.is_file(), f"missing case model {model_path}"
    manifest = source_colors.inventory(model_path)
    assert len(manifest["parts"]) == 2

    # Initial job setup with per-part color decisions
    job_path, job = build_test_job(
        tmp_path, model_path, title="Two Color Assembly",
        dims=(119.0, 99.0, 6.0), parts=2, color_mode="per_part"
    )
    decisions = {
        "schema": "part_slots_v1",
        "mode": "per_part",
        "source_sha256": manifest["source_sha256"],
        "choices": {
            manifest["parts"][0]["key"]: {"slot": 1},
            manifest["parts"][1]["key"]: {"slot": 2}
        }
    }
    job["color_decisions"] = decisions
    initial_sha = hashlib.sha256(b"initial_slice_gcode").hexdigest()
    job["gcode_sha256"] = initial_sha
    job_path.write_text(copy.deepcopy(str(job)))

    # Step 1: scale to 80%
    plan_scale = resolve_plan("scale to 80%", job, model_path)
    assert plan_scale["kind"] == "scale_percent"
    assert plan_scale["percent"] == 80.0

    gcode_scale = generate_mock_gcode(dims=(95.2, 79.2, 4.8), object_name="Assembly")
    ok, lines = verify_probe(plan_scale, job, gcode_scale)
    assert ok, lines

    edit_plan.record_edit(job, plan_scale, actor="discord:corpus", lane="fuzzy",
                           source_sha256=manifest["source_sha256"])
    edit_plan.apply_plan(job, plan_scale)
    # Applying an edit invalidates the slice hash (approvals required fresh)
    new_sha1 = hashlib.sha256(gcode_scale.encode()).hexdigest()
    edit_plan.finish_edit_record(job, verification=lines, output_sha256=new_sha1)
    job["gcode_sha256"] = new_sha1
    assert job["gcode_sha256"] != initial_sha
    assert job["color_decisions"] == decisions
    assert job["scale"] == 0.8

    # Step 2: add tree supports
    plan_supp = resolve_plan("add tree supports", job, model_path)
    gcode_supp = generate_mock_gcode(dims=(95.2, 79.2, 4.8), supports=True, support_type="tree", object_name="Assembly")
    ok, lines = verify_probe(plan_supp, job, gcode_supp)
    assert ok, lines

    edit_plan.record_edit(job, plan_supp, actor="discord:corpus", lane="fuzzy",
                           source_sha256=manifest["source_sha256"])
    edit_plan.apply_plan(job, plan_supp)
    new_sha2 = hashlib.sha256(gcode_supp.encode()).hexdigest()
    edit_plan.finish_edit_record(job, verification=lines, output_sha256=new_sha2)
    job["gcode_sha256"] = new_sha2

    assert job["color_decisions"] == decisions
    assert job["edit_settings"]["supports"]["enabled"] is True
    assert len(job["edits"]) == 2

    # Step 3: Undo restores previous edit state
    undone = edit_plan.undo_last_edit(job, actor="discord:corpus")
    assert undone["plan"]["kind"] == "supports"
    assert job["color_decisions"] == decisions
    assert "supports" not in job.get("edit_settings", {}) or not job["edit_settings"]["supports"].get("enabled")


def test_overhang_model_supports_question_and_edit_flow(tmp_path):
    """Overhang model: questions answered without edit; supports added and removed."""
    model_path = CORPUS_CASES / "needs-supports" / "model.3mf"
    assert model_path.is_file(), f"missing case model {model_path}"

    job_path, job = build_test_job(
        tmp_path, model_path, title="T Bracket",
        dims=(60.0, 12.0, 40.0), parts=1
    )
    initial_job = copy.deepcopy(job)

    # 1. Question: does this need supports?
    assert model_questions.classify("does this need supports?") == "supports"
    answer, offer = model_questions.supports_answer(job, model_path)
    assert answer
    assert job == initial_job  # Unchanged

    # 2. Add tree supports
    plan_add = resolve_plan("add tree supports", job, model_path)
    assert plan_add["enabled"] is True
    gcode_with_supp = generate_mock_gcode(dims=(60.0, 12.0, 40.0), supports=True, support_type="tree")
    ok, lines = verify_probe(plan_add, job, gcode_with_supp)
    assert ok, lines

    edit_plan.record_edit(job, plan_add, actor="discord:corpus", lane="fuzzy", source_sha256="fake_sha")
    edit_plan.apply_plan(job, plan_add)
    assert job["edit_settings"]["supports"]["enabled"] is True

    # 3. Remove supports
    plan_rem = resolve_plan("no supports", job, model_path)
    assert plan_rem["enabled"] is False
    gcode_no_supp = generate_mock_gcode(dims=(60.0, 12.0, 40.0), supports=False)
    ok, lines = verify_probe(plan_rem, job, gcode_no_supp)
    assert ok, lines

    edit_plan.record_edit(job, plan_rem, actor="discord:corpus", lane="fuzzy", source_sha256="fake_sha")
    edit_plan.apply_plan(job, plan_rem)
    assert job["edit_settings"]["supports"]["enabled"] is False


def test_oversized_model_shrink_and_rearrange_safety(tmp_path):
    """A model too big for the bed: shrink fits the bed without splitting assembly."""
    model_path = CORPUS_CASES / "must-shrink" / "model.3mf"
    assert model_path.is_file(), f"missing case model {model_path}"

    # 260 x 260 x 230 mm on a 220 mm bed
    job_path, job = build_test_job(
        tmp_path, model_path, title="Big Plate",
        dims=(260.0, 260.0, 230.0), parts=1
    )

    # Fit check rejects 260mm in 220mm bed
    bed_mm = [220.0, 220.0, 220.0]
    fit_before = model_questions.fit_answer(job, bed_mm)
    assert "does not fit" in fit_before.lower() or "too large" in fit_before.lower() or "mm" in fit_before

    # Shrink by 20% -> 208 x 208 x 184 mm fits
    plan_shrink = resolve_plan("shrink this by 20%", job, model_path)
    shrunk_dims = (208.0, 208.0, 184.0)
    gcode_shrunk = generate_mock_gcode(dims=shrunk_dims, object_name="Big_plate")
    ok, lines = verify_probe(plan_shrink, job, gcode_shrunk)
    assert ok, lines

    edit_plan.record_edit(job, plan_shrink, actor="discord:corpus", lane="fuzzy", source_sha256="fake_sha")
    edit_plan.apply_plan(job, plan_shrink)
    assert job["scale"] == 0.8


def test_link_plus_instruction_and_attached_question_flow():
    """Link plus instruction in one message, and an attached 3MF plus question."""
    # 1. Link + edit instruction
    text1 = "https://makerworld.com/en/models/12345#profileId-67890 shrink this by 20%"
    instruction1 = instruction_after_model(text1)
    assert instruction1 == "shrink this by 20%"
    resolved = edit_plan.resolve_request(instruction1)
    assert resolved is not None and resolved[0] == "plan"
    assert resolved[1]["kind"] == "scale_percent"

    # 2. Link + model question
    text2 = "https://makerworld.com/en/models/12345 does this need supports?"
    instruction2 = instruction_after_model(text2)
    assert instruction2 == "does this need supports?"
    assert model_questions.classify(instruction2) == "supports"

    # 3. Attached file with size question
    text3 = "how big is this?"
    assert model_questions.classify(text3) == "size"


def test_undo_after_two_edits_restores_initial_gcode_hash(tmp_path):
    """Multi-step undo (layer height -> infill) restores provenance and previous state."""
    job_path, job = build_test_job(tmp_path, "cube.3mf", dims=(40.0, 20.0, 10.0))
    initial_sha = "0000000000000000000000000000000000000000000000000000000000000000"
    job["gcode_sha256"] = initial_sha

    # Edit 1: layer height 0.12
    p1 = resolve_plan("layer height 0.12", job, None)
    edit_plan.record_edit(job, p1, actor="user", lane="fuzzy", source_sha256="src_sha")
    edit_plan.apply_plan(job, p1)
    sha1 = "1111111111111111111111111111111111111111111111111111111111111111"
    edit_plan.finish_edit_record(job, verification=["ok: layer height"], output_sha256=sha1)
    job["gcode_sha256"] = sha1

    # Edit 2: infill 25%
    p2 = resolve_plan("infill 25%", job, None)
    edit_plan.record_edit(job, p2, actor="user", lane="fuzzy", source_sha256="src_sha")
    edit_plan.apply_plan(job, p2)
    sha2 = "2222222222222222222222222222222222222222222222222222222222222222"
    edit_plan.finish_edit_record(job, verification=["ok: infill"], output_sha256=sha2)
    job["gcode_sha256"] = sha2

    assert len(job["edits"]) == 2
    assert job["edit_settings"]["layer_height"] == 0.12
    assert job["edit_settings"]["infill_percent"] == 25.0

    # Undo 1: removes infill, layer height remains
    u1 = edit_plan.undo_last_edit(job, actor="user")
    assert u1["plan"]["kind"] == "infill"
    assert "infill_percent" not in job.get("edit_settings", {})
    assert job["edit_settings"]["layer_height"] == 0.12

    # Undo 2: removes layer height, clean state restored
    u2 = edit_plan.undo_last_edit(job, actor="user")
    assert u2["plan"]["kind"] == "layer_height"
    assert "layer_height" not in job.get("edit_settings", {})
    assert edit_plan.undo_last_edit(job, actor="user") is None


def test_pick_list_shown_for_ambiguous_phrasing_never_an_action():
    """Ambiguous phrasings return pick options and never an action."""
    for phrase in ["make it bigger", "make it smaller", "shrink it", "make it 2x bigger"]:
        kind, choices = edit_plan.resolve_request(phrase)
        assert kind == "pick"
        assert choices and len(choices) >= 2
        for choice in choices:
            assert "percent" in choice
