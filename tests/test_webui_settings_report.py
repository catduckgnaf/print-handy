"""Evidence-based settings-decision report downloads."""
import json
from pathlib import Path

from print_handy.webui import settings_report


def test_report_lists_only_supported_settings_and_never_claims_unknown_unchanged(tmp_path):
    archive = tmp_path / "archive"
    job = archive / "safe-job"
    job.mkdir(parents=True)
    payload = {"provenance": {"conversion_provenance": {
        "source_format": "prusaslicer", "intent_status": "unknown", "settings": {
            "same": {"project_value": 10, "selected_value": 10, "selected_reason": "designer"},
            "mapped": {"project_value": "old", "selected_value": "new", "selected_reason": "compatibility_fallback"},
            "profile": {"project_value": 1, "selected_value": 2, "selected_reason": "destination_profile"},
            "unclear": {"project_value": 3, "selected_value": 4, "selected_reason": "unattributed"},
            "missing": {"project_value": 5, "selected_value": None, "selected_reason": "designer"},
        }}}}
    (job / "JOB.json").write_text(json.dumps(payload), encoding="utf-8")
    report = settings_report.report_payload(archive, "safe-job")
    assert [(row["setting"], row["status"]) for row in report["settings"]] == [
        ("mapped", "replaced"), ("profile", "replaced"), ("same", "kept")]
    assert "not treated as unchanged" in report["limitations"]
    assert "unclear" not in settings_report.render_json(report).decode()


def test_report_requires_recorded_conversion_provenance_and_contained_job(tmp_path):
    archive = tmp_path / "archive"
    for job_id, data in (("missing", {"provenance": {}}),
                         ("bad", {"provenance": {"conversion_provenance": {"settings": []}}})):
        folder = archive / job_id
        folder.mkdir(parents=True)
        (folder / "JOB.json").write_text(json.dumps(data), encoding="utf-8")
    assert settings_report.report_payload(archive, "missing") is None
    assert settings_report.report_payload(archive, "bad") is None
    assert settings_report.report_payload(archive, "../outside") is None


def test_rendered_report_is_downloadable_json_bytes():
    payload = {"schema_version": 1, "settings": []}
    body = settings_report.render_json(payload)
    assert body.endswith(b"\n")
    assert json.loads(body) == payload


def test_each_recorded_layer_is_named_and_unattributed_is_omitted(tmp_path):
    archive = tmp_path / "archive"
    (archive / "j").mkdir(parents=True)
    rows = {layer: {"project_value": 1, "selected_value": 2, "selected_reason": layer}
            for layer in settings_report.LAYER_REASONS}
    rows["mystery"] = {"project_value": 1, "selected_value": 2, "selected_reason": "unattributed"}
    (archive / "j" / "JOB.json").write_text(json.dumps(
        {"provenance": {"conversion_provenance": {"settings": rows}}}), encoding="utf-8")
    report = settings_report.report_payload(archive, "j")
    got = {row["setting"]: row for row in report["settings"]}
    assert set(got) == set(settings_report.LAYER_REASONS)
    assert all(row["status"] == "replaced" and row["reason"] for row in got.values())


def test_sliced_page_links_the_report_only_for_jobs_that_recorded_one(tmp_path):
    from print_handy.webui import sliced
    rows = [{"job_id": "with", "title": "A", "when": "now", "plates": [{"index": 0, "name": "a.gcode", "size": 1}],
             "settings_report": True},
            {"job_id": "without", "title": "B", "when": "now", "plates": [{"index": 0, "name": "b.gcode", "size": 1}],
             "settings_report": False}]
    page = sliced.render_sliced_page(rows)
    assert 'href="/settings-report/with"' in page
    assert 'href="/settings-report/without"' not in page
