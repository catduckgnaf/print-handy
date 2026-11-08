"""Scope-aware /print-again for multi-plate projects (#1008, stage D).

No network, no printer. The fixture is one archived 3-plate project job
whose JOB.json carries slice 1's embedded multipart_plan; the live plan
file (not the embedded copy) is the completion truth, exactly as in
production. The project's number is anchored at 82 by the seeded registry,
so the plates are 82A / 82B / 82C.
"""
import hashlib
import json
import os
import time

import pytest

from print_handy.hermes import print_control
from print_handy.hermes.print_next import NextPlateError

PLAN_ID = "plan-rack"
LABELS = ["Center tray", "Right arm", "Left arm"]


def _project(tmp_path, *, live_completed=(1, 1, 0), launched=(True, True, True)):
    """One archived project job; returns (archive, job_json, [gcode paths])."""
    archive = tmp_path / "archive"
    run = archive / "rack-mount--a"
    gcodes = []
    parts = []
    for i in range(3):
        d = run / f"sliced/orca/p{i}"
        d.mkdir(parents=True)
        g = d / "plate_1.gcode"
        g.write_text(f"; plate {i}\nG1 X{i}\n")
        if launched[i]:
            rec = d / "plate_1.launch.json"
            rec.write_text(json.dumps({"file": f"plate-{i}.gcode"}))
            t = time.time() - (300 - 100 * i)
            os.utime(rec, (t, t))
        gcodes.append(g)
        parts.append({"id": f"plate{i + 1}", "label": LABELS[i], "plate": i + 1,
                      "quantity": 1, "completed_quantity": 0, "status": "planned",
                      "gcode_file": "plate_1.gcode", "job_json": str(run / "JOB.json"),
                      "gcode_path": str(g), "project_plate_id": f"plate{i + 1}",
                      "project_plate_suffix": "ABC"[i], "component_keys": []})
    plan = {"plan_id": PLAN_ID, "title": "Rack Mount", "parts": parts,
            "project_id": "project-intake-1", "source_kind": "uploaded_files",
            "project_run_id": "original"}
    job = {"title": "Rack Mount", "gcode": str(gcodes[-1]),
           "derived_plates": [{"gcode": str(g)} for g in gcodes],
           "project_id": "project-intake-1", "multipart_plan": plan}
    (run / "JOB.json").write_text(json.dumps(job))
    plans = tmp_path / "plans"
    plans.mkdir()
    live_parts = [dict(p, completed_quantity=c) for p, c in zip(parts, live_completed)]
    (plans / f"{PLAN_ID}.json").write_text(json.dumps(
        dict(plan, parts=live_parts, project_number=82)))
    registry = {"numbers": {str((run / "JOB.json").resolve()): 82}}
    (tmp_path / "print_numbers.json").write_text(json.dumps(registry))
    return archive, run / "JOB.json", gcodes, plans


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(print_control, "REPRINT_QUEUE", tmp_path / "reprint_queue.json")
    monkeypatch.setattr(print_control, "PRINT_NUMBERS", tmp_path / "print_numbers.json")


def _info(tmp_path, **kw):
    archive, job_json, gcodes, plans = _project(tmp_path, **kw)
    info = print_control.project_for_job(job_json, archive, tmp_path / "print_numbers.json", plans)
    assert info is not None
    return archive, job_json, gcodes, plans, info


def test_parse_plate_ref():
    assert print_control.parse_plate_ref("82A") == (82, "A")
    assert print_control.parse_plate_ref("#82aa") == (82, "AA")
    assert print_control.parse_plate_ref("82") is None
    assert print_control.parse_plate_ref("benchy") is None
    assert print_control.parse_plate_ref("") is None
    assert print_control.parse_plate_ref("8A2") is None


def test_project_for_job_uses_live_plan_counts_and_anchored_number(tmp_path):
    _, _, _, _, info = _info(tmp_path)
    assert info["project_number"] == 82
    assert [p["ref"] for p in info["plates"]] == ["82A", "82B", "82C"]
    # The JOB.json copy says nothing is complete; the live plan wins.
    assert [p["completed_quantity"] for p in info["plates"]] == [1, 1, 0]


def test_project_for_job_rejects_single_plate_and_plain_jobs(tmp_path):
    archive, job_json, _, _ = _project(tmp_path)
    job = json.loads(job_json.read_text())
    job["multipart_plan"]["parts"] = job["multipart_plan"]["parts"][:1]
    job_json.write_text(json.dumps(job))
    assert print_control.project_for_job(
        job_json, archive, tmp_path / "print_numbers.json", tmp_path / "plans") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "JOB.json").write_text(json.dumps({"title": "Solo"}))
    assert print_control.project_for_job(plain / "JOB.json", archive) is None


def test_resolve_plate_ref_targets_exact_plate(tmp_path):
    archive, _, gcodes, plans = _project(tmp_path)
    target = print_control.resolve_plate_ref(
        "82B", archive, tmp_path / "print_numbers.json", plans)
    assert target["gcode"] == str(gcodes[1].resolve())
    assert target["project_plate_ref"] == "82B"
    assert target["project_number"] == 82


def test_resolve_print_accepts_alias_but_keeps_legacy_integer(tmp_path, monkeypatch):
    from print_handy.hermes import print_records

    archive, _, gcodes, plans = _project(tmp_path)
    monkeypatch.setattr(print_records, "PLANS_DIR", plans)
    alias = print_control.resolve_print("82C", archive)
    assert alias["gcode"] == str(gcodes[2].resolve())
    assert alias["project_plate_ref"] == "82C"
    legacy = print_control.resolve_print("82", archive)
    assert legacy["gcode"] == str(gcodes[0].resolve())


def test_resolve_plate_ref_unknown_alias_or_suffix_raises(tmp_path):
    archive, _, _, plans = _project(tmp_path)
    numbers = tmp_path / "print_numbers.json"
    with pytest.raises(NextPlateError):
        print_control.resolve_plate_ref("82D", archive, numbers, plans)
    with pytest.raises(NextPlateError):
        print_control.resolve_plate_ref("99A", archive, numbers, plans)


def test_scope_targets_entire_and_remaining(tmp_path):
    archive, _, gcodes, plans, info = _info(tmp_path)
    base = print_control.resolve_plate_ref("82A", archive, tmp_path / "print_numbers.json", plans)
    entire = print_control.project_scope_targets(
        base, "entire", archive, tmp_path / "print_numbers.json", plans)
    assert [t["project_plate_ref"] for t in entire] == ["82A", "82B", "82C"]
    assert [t["gcode"] for t in entire] == [str(g.resolve()) for g in gcodes]
    assert "entire project 82" in entire[0]["scope_note"]
    assert "1 of 3" in entire[0]["scope_note"]
    remaining = print_control.project_scope_targets(
        base, "remaining", archive, tmp_path / "print_numbers.json", plans)
    assert [t["project_plate_ref"] for t in remaining] == ["82C"]


def test_scope_targets_remaining_none_left_raises(tmp_path):
    archive, _, _, plans, _ = _info(tmp_path, live_completed=(1, 1, 1))
    base = print_control.resolve_plate_ref("82A", archive, tmp_path / "print_numbers.json", plans)
    with pytest.raises(NextPlateError):
        print_control.project_scope_targets(
            base, "remaining", archive, tmp_path / "print_numbers.json", plans)


def test_scope_targets_skip_never_launched_plates(tmp_path):
    archive, _, _, plans, _ = _info(tmp_path, launched=(True, True, False))
    base = print_control.resolve_plate_ref("82A", archive, tmp_path / "print_numbers.json", plans)
    entire = print_control.project_scope_targets(
        base, "entire", archive, tmp_path / "print_numbers.json", plans)
    assert [t["project_plate_ref"] for t in entire] == ["82A", "82B"]


def test_scope_question_and_plate_list_name_refs(tmp_path):
    archive, _, _, plans, info = _info(tmp_path)
    target = print_control.resolve_plate_ref("82B", archive, tmp_path / "print_numbers.json", plans)
    question = print_control.scope_question(target, info)
    assert "82" in question and "82B" in question and "3 plates" in question
    text = print_control.project_plate_list_text(info)
    assert "82A: Center tray — printed" in text
    assert "82C: Left arm — not finished" in text
    assert "/print-again 82C" in text


def test_project_sequence_queue_walks_plates_in_order(tmp_path):
    archive, _, gcodes, plans, _ = _info(tmp_path)
    base = print_control.resolve_plate_ref("82A", archive, tmp_path / "print_numbers.json", plans)
    targets = print_control.project_scope_targets(
        base, "entire", archive, tmp_path / "print_numbers.json", plans)
    queue = tmp_path / "queue.json"
    print_control.queue_project_sequence(targets, path=queue)
    # After plate A started, /print-again next offers B, then C.
    nxt = print_control.next_copy_target(queue, archive)
    assert nxt["project_sequence"] is True
    assert nxt["project_plate_ref"] == "82B"
    assert nxt["gcode"] == str(gcodes[1].resolve())
    assert "82B" in print_control.next_copy_question(nxt)
    # While plate A is the current entry, only A's completion prompts, and
    # it names B as next; B's file finishing early prompts nothing.
    sha_a = hashlib.sha256(gcodes[0].read_bytes()).hexdigest()
    sha_b = hashlib.sha256(gcodes[1].read_bytes()).hexdigest()
    prompt = print_control.next_copy_prompt(f"upload-{sha_a}.gcode", queue)
    assert prompt is not None and "82A" in prompt and "82B" in prompt
    assert print_control.next_copy_prompt(f"upload-{sha_b}.gcode", queue) is None
    print_control.mark_copy_started(queue)
    nxt = print_control.next_copy_target(queue, archive)
    assert nxt["project_plate_ref"] == "82C"
    prompt = print_control.next_copy_prompt(f"upload-{sha_b}.gcode", queue)
    assert prompt is not None and "82B" in prompt and "82C" in prompt


def test_copies_queue_shape_unchanged(tmp_path):
    queue = tmp_path / "queue.json"
    target = {"job_json": "/j/JOB.json", "gcode": "/j/p.gcode", "title": "Solo",
              "gcode_sha256": "x", "copies": 3}
    print_control.queue_copies(target, path=queue)
    data = json.loads(queue.read_text())
    assert "kind" not in data and data["total"] == 3 and data["started"] == 1
