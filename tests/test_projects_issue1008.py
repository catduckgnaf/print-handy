"""Project identity and per-plate print-record regressions for #1008.

No network, no actual printer, no model endpoint. These tests intentionally
distinguish *component*, *plate*, *job number* and *physical print attempt*.
"""
import json

import pytest

from print_handy.core import projects
from print_handy.runtime import print_records


@pytest.mark.parametrize("index,expected", [
    (0, "A"), (1, "B"), (25, "Z"), (26, "AA"),
    (27, "AB"), (51, "AZ"), (52, "BA"), (701, "ZZ"), (702, "AAA"),
])
def test_spreadsheet_style_plate_suffix_never_wraps(index, expected):
    assert projects.plate_suffix(index) == expected


@pytest.mark.parametrize("invalid", [-1, -9, 0.5, True, None, "1"])
def test_plate_suffix_rejects_invalid_index(invalid):
    with pytest.raises(ValueError):
        projects.plate_suffix(invalid)


def test_project_refs_require_real_numeric_job_number_and_valid_suffix():
    assert projects.project_ref(82, "A") == "82A"
    assert projects.project_ref(82, "AA") == "82AA"
    for number, suffix in [(None, "A"), (0, "A"), (-1, "A"), (True, "A"),
                           (82, "a"), (82, ""), (82, "../A"), (82, "A1")]:
        assert projects.project_ref(number, suffix) is None


def test_source_neutral_project_manifest_preserves_both_arms_even_with_same_name():
    a = [
        {"name": "arm.stl", "downloaded_file": "/tmp/one/arm.stl"},
        {"name": "arm.stl", "downloaded_file": "/tmp/two/arm.stl"},
        {"name": "center.stl", "downloaded_file": "/tmp/three/center.stl"},
    ]
    upload = projects.from_files("intake-123", "uploaded_files", a, "Rack mount")
    link = projects.from_files("intake-123", "linked_files", a, "Rack mount")
    assert upload["schema"] == projects.SCHEMA
    assert upload["project_id"] == link["project_id"] == "project-intake-123"
    assert [part["component_id"] for part in upload["components"]] == ["source1", "source2", "source3"]
    assert [p["name"] for p in upload["components"]] == ["arm.stl", "arm.stl", "center.stl"]
    assert link["source_kind"] == "linked_files" and upload["source_kind"] == "uploaded_files"


def test_intake_ids_are_validated_and_project_requires_sources():
    assert projects.project_id_for_intake("intake:ABC") == "project-intake-ABC"
    with pytest.raises(ValueError):
        projects.project_id_for_intake("")
    with pytest.raises(ValueError):
        projects.project_id_for_intake("x" * 101)
    with pytest.raises(ValueError):
        projects.from_files("intake-123", "uploaded_files", [])


def test_plate_info_tracks_slicer_keys_but_does_not_invent_source_ids():
    row = projects.plate_info(1, ["part-key-1", "part-key-2"])
    assert row == {"project_plate_id": "plate2", "project_plate_suffix": "B",
                   "component_keys": ["part-key-1", "part-key-2"]}


def test_reprint_scope_candidates_do_not_authorize_a_print():
    assert projects.scope_choices(False) == ("This print only", "Cancel")
    assert projects.scope_choices(True) == (
        "This plate only", "Entire project", "Choose a plate", "Cancel")
    assert "Remaining unprinted plates" in projects.scope_choices(True, True)


def test_record_schema_preserves_numeric_print_number_and_adds_project_alias(monkeypatch):
    monkeypatch.setattr(print_records, "_registry_print_number", lambda _: 82)
    context = {
        "job_key": "plate-1.gcode", "started_at": "2026-11-07T07:00:00Z",
        "slicer_settings": {"local_gcode": "/archive/plate1.gcode"},
        "project_id": "project-intake-123", "project_run_id": "original",
        "project_plate_id": "plate1", "project_plate_suffix": "A",
        "project_source_kind": "uploaded_files", "project_component_ids": ["source1"],
    }
    data = print_records.build_record_data("plate-1.gcode", context=context, stats={"state": "printing"})
    assert data["print_number"] == data["project_number"] == 82
    assert data["project_plate_ref"] == "82A"
    assert data["project_id"] == "project-intake-123"
    out = print_records._record_json(data)
    assert out["print_number"] == 82 and out["project_plate_ref"] == "82A"
    assert out["project_component_ids"] == ["source1"]
    assert out["record_id"] == out["print_attempt_id"]


def test_single_print_record_still_uses_old_integer_name(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path)
    data = {"print_number": 82, "started_at": "2026-11-07T07:00:00Z",
            "gcode_file": "part.gcode", "job_key": "part.gcode"}
    assert print_records._record_path_for(data, print_records._record_id(data)).name == "print-0082.json"
    output = print_records._record_json(data)
    assert output["print_number"] == 82
    assert "project_id" not in output and "print_attempt_id" not in output


def test_plates_keep_one_file_per_print_number_and_share_the_project_label(tmp_path, monkeypatch):
    """Since #988 every plate launch has its own print number (#82, #83):
    records stay print-NNNN.json, one per print, and the readable plate
    label uses the PROJECT's number (82A, 82B), not each plate's own."""
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path)
    monkeypatch.setattr(print_records, "_registry_print_number", lambda _: None)
    base = {"project_id": "project-intake-123", "project_run_id": "original", "project_number": 82,
            "project_source_kind": "uploaded_files"}
    a = print_records.build_record_data("center.gcode", stats={"state": "complete"}, context=dict(
        base, job_key="center.gcode", print_number=82, project_plate_suffix="A",
        started_at="2026-11-07T07:00:00Z"))
    b = print_records.build_record_data("arm.gcode", stats={"state": "complete"}, context=dict(
        base, job_key="arm.gcode", print_number=83, project_plate_suffix="B",
        started_at="2026-11-07T08:00:00Z"))
    assert (a["print_number"], a["project_number"], a["project_plate_ref"]) == (82, 82, "82A")
    assert (b["print_number"], b["project_number"], b["project_plate_ref"]) == (83, 82, "82B")
    assert print_records._record_path_for(a, print_records._record_id(a)).name == "print-0082.json"
    assert print_records._record_path_for(b, print_records._record_id(b)).name == "print-0083.json"
    # A reprint of plate A is a new launch with its own number; it keeps the 82A label.
    again = print_records.build_record_data("center.gcode", stats={"state": "complete"}, context=dict(
        base, job_key="center.gcode", print_number=90, project_plate_suffix="A",
        started_at="2026-11-09T07:00:00Z"))
    assert (again["print_number"], again["project_plate_ref"]) == (90, "82A")
    assert print_records._record_path_for(again, print_records._record_id(again)).name == "print-0090.json"
    assert print_records._record_json(again)["print_attempt_id"] != print_records._record_json(a)["print_attempt_id"]


def test_the_first_plate_claims_the_project_number_once(tmp_path):
    job = tmp_path / "JOB.json"
    job.write_text(json.dumps({"title": "Rack", "project_id": "project-intake-123"}))
    assert print_records.claim_project_number(job, 82) == 82      # plate A starts as #82
    assert print_records.claim_project_number(job, 83) == 82      # plate B (#83) reads it back
    assert print_records.claim_project_number(job, 90) == 82      # a reprint or re-slice too
    assert print_records.claim_project_number(job, None) == 82    # a failed mint still gets the label
    saved = json.loads(job.read_text())
    assert saved["project_number"] == 82 and saved["title"] == "Rack"   # nothing else touched
    assert not job.with_suffix(".json.lock").exists()
    # No file, or nothing to claim: nothing is invented.
    assert print_records.claim_project_number(tmp_path / "missing.json", 91) == 91
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"title": "x"}))
    assert print_records.claim_project_number(other, None) is None
    assert "project_number" not in json.loads(other.read_text())


def test_concurrent_first_plates_claim_one_number(tmp_path):
    import threading
    job = tmp_path / "JOB.json"
    job.write_text(json.dumps({"project_id": "project-intake-123"}))
    got = []
    threads = [threading.Thread(target=lambda n=n: got.append(print_records.claim_project_number(job, n)))
               for n in range(100, 108)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1 and got[0] == json.loads(job.read_text())["project_number"]


def test_a_later_plate_completion_never_renumbers_the_project(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path)
    (tmp_path / "plan-z.json").write_text(json.dumps({
        "plan_id": "plan-z", "project_id": "project-intake-123", "project_number": 82,
        "parts": [{"id": "plate1", "label": "Center", "quantity": 1, "completed_quantity": 1,
                   "status": "completed", "gcode_file": "center.gcode"},
                  {"id": "plate2", "label": "Arm", "quantity": 1, "completed_quantity": 0,
                   "gcode_file": "arm.gcode"}]}))
    name, done = print_records.update_multipart_plan({
        "multipart_plan_id": "plan-z", "part_id": "plate2", "project_id": "project-intake-123",
        "project_number": 83, "gcode_file": "arm.gcode", "job_key": "arm.gcode",
        "started_at": "2026-11-07T08:00:00Z", "completed_at": "2026-11-07T09:00:00Z", "success": True})
    saved = json.loads((tmp_path / "plan-z.json").read_text())
    assert (name, done) == ("plan-z", True)
    assert saved["project_number"] == 82
    assert [p["display_ref"] for p in saved["parts"]] == ["82A", "82B"]


def test_completed_plan_keeps_project_metadata_and_adds_82a_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path)
    plan_path = tmp_path / "plan-test.json"
    plan_path.write_text(json.dumps({
        "plan_id": "plan-test", "title": "Mac mini",
        "project_id": "project-intake-123", "source_kind": "uploaded_files",
        "project_run_id": "original",
        "parts": [{"id": "plate1", "label": "Center", "quantity": 1,
                   "completed_quantity": 0, "gcode_file": "center.gcode"},
                  {"id": "plate2", "label": "Arm", "quantity": 1,
                   "completed_quantity": 0, "gcode_file": "arm.gcode"}],
    }))
    name, all_done = print_records.update_multipart_plan({
        "multipart_plan_id": "plan-test", "part_id": "plate1",
        "project_id": "project-intake-123", "project_number": 82,
        "project_run_id": "original", "project_source_kind": "uploaded_files",
        "gcode_file": "center.gcode", "completed_at": "2026-11-07T08:00:00Z",
    })
    saved = json.loads(plan_path.read_text())
    assert name == "plan-test" and all_done is False
    assert saved["project_id"] == "project-intake-123"
    assert saved["project_number"] == 82
    assert saved["project_run_id"] == "original"
    assert [part["display_ref"] for part in saved["parts"]] == ["82A", "82B"]
    assert [part["completed_quantity"] for part in saved["parts"]] == [1, 0]


@pytest.mark.parametrize("outcome", [
    {"success": False, "status": "cancelled"},
    {"success": False, "status": "failed"},
    {"success": None, "status": "printing"},
    {"success": None, "status": "unknown"},
])
def test_failed_cancelled_or_uncertain_plate_never_advances_project(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path)
    p = tmp_path / "plan.json"
    p.write_text(json.dumps({"plan_id": "plan", "parts": [
        {"id": "plate1", "label": "Arm", "quantity": 2,
         "completed_quantity": 0, "status": "planned", "gcode_file": "arm.gcode"}]}))
    result = print_records.update_multipart_plan({
        "multipart_plan_id": "plan", "part_id": "plate1",
        "gcode_file": "arm.gcode", "job_key": "arm.gcode",
        "started_at": "2026-11-07T07:00:00Z", **outcome,
    })
    assert result == ("plan", False)
    assert json.loads(p.read_text())["parts"][0]["completed_quantity"] == 0


def test_duplicate_successful_completion_does_not_count_as_two_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path)
    p = tmp_path / "copies.json"
    p.write_text(json.dumps({"plan_id": "copies", "parts": [
        {"id": "plate1", "label": "Mounting nut", "quantity": 2,
         "completed_quantity": 0, "status": "planned", "gcode_file": "nut.gcode"}]}))
    data = {"multipart_plan_id": "copies", "part_id": "plate1",
            "gcode_file": "nut.gcode", "job_key": "nut.gcode",
            "started_at": "2026-11-07T07:00:00Z",
            "completed_at": "2026-11-07T07:15:00Z", "success": True}
    assert print_records.update_multipart_plan(data) == ("copies", False)
    assert print_records.update_multipart_plan(data) == ("copies", False)
    after = json.loads(p.read_text())["parts"][0]
    assert after["completed_quantity"] == 1
    assert len(after["completed_attempt_ids"]) == 1
    next_attempt = {**data, "started_at": "2026-11-07T08:00:00Z",
                    "completed_at": "2026-11-07T08:15:00Z"}
    assert print_records.update_multipart_plan(next_attempt) == ("copies", True)
    after2 = json.loads(p.read_text())["parts"][0]
    assert after2["completed_quantity"] == 2
    assert len(after2["completed_attempt_ids"]) == 2
    assert print_records.update_multipart_plan(next_attempt) == ("copies", True)
    assert json.loads(p.read_text())["parts"][0]["completed_quantity"] == 2


def test_one_success_followed_by_failure_does_not_finish_project(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path)
    p = tmp_path / "mixed.json"
    p.write_text(json.dumps({"plan_id": "mixed", "parts": [
        {"id": "plate1", "label": "Center", "quantity": 1, "completed_quantity": 0},
        {"id": "plate2", "label": "Right", "quantity": 1, "completed_quantity": 0}]}))
    first = {"multipart_plan_id": "mixed", "part_id": "plate1", "gcode_file": "center.gcode",
             "job_key": "center.gcode", "started_at": "2026-11-07T09:00:00Z", "success": True}
    second = {"multipart_plan_id": "mixed", "part_id": "plate2", "gcode_file": "right.gcode",
              "job_key": "right.gcode", "started_at": "2026-11-07T10:00:00Z", "success": False}
    assert print_records.update_multipart_plan(first) == ("mixed", False)
    assert print_records.update_multipart_plan(second) == ("mixed", False)
    saved = json.loads(p.read_text())["parts"]
    assert [r["completed_quantity"] for r in saved] == [1, 0]


def test_record_start_gives_every_plate_the_first_plates_project_number(tmp_path, monkeypatch):
    """End to end through record_start: plate A starts as print #82 and
    claims the project number; plate B starts as #83 and gets 82 too, so
    its records read 82B. The claim lives in the job's JOB.json."""
    from unittest import mock
    from print_handy.core import model_job

    job_dir = tmp_path / "archive" / "rack"
    job_dir.mkdir(parents=True)
    plates = []
    for name in ("center.gcode", "arm.gcode"):
        g = job_dir / name
        g.write_text("G1 X1\n")
        plates.append(g)
    plan = {"plan_id": "plan-rack", "project_id": "project-intake-123", "parts": [
        {"id": "plate1", "gcode_path": str(plates[0].resolve()), "project_plate_suffix": "A"},
        {"id": "plate2", "gcode_path": str(plates[1].resolve()), "project_plate_suffix": "B"}]}
    job = {"title": "Rack", "project_id": "project-intake-123", "multipart_plan": plan}
    job_json = job_dir / "JOB.json"
    job_json.write_text(json.dumps(job))
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(print_records, "PLANS_DIR", tmp_path / "Plans")
    context_path = tmp_path / "print_handy_context.json"
    state = {"printer": "Test", "filament_slots": {},
             "spool": {"spoolman_id": None, "name": "PLA", "material": "PLA",
                       "diameter_mm": 1.75, "density": 1.24}}
    with mock.patch.object(model_job, "_job_for_gcode", return_value=job), \
         mock.patch.object(model_job, "_job_record_for_gcode", return_value=(job, job_json)), \
         mock.patch.object(model_job.printer_state, "load", return_value=state), \
         mock.patch.object(model_job.printer_state, "summary", return_value="test printer"), \
         mock.patch.object(model_job.printers, "current",
                           return_value=mock.Mock(multi_slot=False, native_or_none=None)), \
         mock.patch.object(model_job, "CONTEXT", context_path):
        model_job.record_start(str(plates[0]), started_at="2026-11-07T07:00:00Z", print_number=82)
        model_job.record_start(str(plates[1]), started_at="2026-11-07T09:00:00Z", print_number=83)
    jobs = json.loads(context_path.read_text())["jobs"]
    assert (jobs["center.gcode"]["print_number"], jobs["center.gcode"]["project_number"]) == (82, 82)
    assert (jobs["arm.gcode"]["print_number"], jobs["arm.gcode"]["project_number"]) == (83, 82)
    assert json.loads(job_json.read_text())["project_number"] == 82
    starts = {p.name for p in (tmp_path / "Prints").glob("*.json")}
    assert starts == {"print-0082.json", "print-0083.json"}
    assert json.loads((tmp_path / "Prints" / "print-0083.json").read_text())["project_plate_ref"] == "82B"
