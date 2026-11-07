"""Project identity and per-plate print-record regressions for #1008.

No network, no actual printer, no model endpoint. These tests intentionally
distinguish *component*, *plate*, *job number* and *physical print attempt*.
"""
import json
from pathlib import Path

import pytest

from print_handy.core import projects
from print_handy.hermes import print_records


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


def test_two_plates_and_reprinted_plate_get_distinct_receipt_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(print_records, "PRINTS_DIR", tmp_path)
    base = {"print_number": 82, "project_id": "project-intake-123",
            "project_number": 82, "project_run_id": "original"}
    a = dict(base, gcode_file="center.gcode", job_key="center.gcode",
             started_at="2026-11-07T07:00:00Z", project_plate_suffix="A")
    b = dict(base, gcode_file="arm.gcode", job_key="arm.gcode",
             started_at="2026-11-07T08:00:00Z", project_plate_suffix="B")
    assert print_records._record_path_for(a, print_records._record_id(a)).name == "print-0082-A.json"
    assert print_records._record_path_for(b, print_records._record_id(b)).name == "print-0082-B.json"
    print_records._write_json_record(print_records._record_path_for(a, print_records._record_id(a)), a)
    reprinted = dict(a, started_at="2026-11-08T07:00:00Z")
    path = print_records._avoid_collision(
        print_records._record_path_for(reprinted, print_records._record_id(reprinted)),
        print_records._record_id(reprinted))
    assert path.name.startswith("print-0082-A-") and path.name.endswith(".json")


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
