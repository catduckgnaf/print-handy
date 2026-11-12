"""Web UI project readout (#1204 item 5): GET /projects and GET /api/projects.

Offline: settings come from a temp YAML file, the project is a fabricated
archived job (a ``project_v1`` manifest plus the embedded multipart plan,
the shapes slice 1 writes), the live plan file and print records sit in a
temp records vault, and the server binds an ephemeral loopback port. The
readout reads only those files; it keeps no store of its own.
"""
import http.client
import json
import threading
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.webui import projects, server

TOKEN = "webui-projects-test-token"
PROJECT_ID = "project-intake-7"
PLAN_ID = "plan-shelf"


def _settings(tmp, environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        "paths:\n"
        f"  state_home: {tmp}/hermes\n"
        f"  cache: {tmp}/cache\n"
        f"  archive: {tmp}/archive\n"
        f"  records_vault: {tmp}/vault\n"
        "webui:\n  enabled: true\n  port: 8765\n")
    return settings.load(local=local, environ={} if environ is None else environ)


def _seed(tmp, title="Shelf <b>kit</b>"):
    """One project: 3 components on 2 plates; plate A printed, plate B (x2) not finished."""
    run = Path(tmp) / "archive" / "shelf--a"
    run.mkdir(parents=True)
    parts = [
        {"id": "plate1", "label": "Left bracket, Right bracket", "plate": 1, "quantity": 1,
         "completed_quantity": 0, "status": "planned", "project_plate_id": "plate1",
         "project_plate_suffix": "A", "component_keys": ["source1", "source2"]},
        {"id": "plate2", "label": "Shelf board", "plate": 2, "quantity": 2,
         "completed_quantity": 0, "status": "planned", "project_plate_id": "plate2",
         "project_plate_suffix": "B", "component_keys": ["source3"]},
    ]
    plan = {"plan_id": PLAN_ID, "title": title, "parts": parts, "project_id": PROJECT_ID,
            "source_kind": "uploaded_files", "project_run_id": "original"}
    manifest = {"schema": "project_v1", "project_id": PROJECT_ID, "title": title,
                "source_kind": "uploaded_files", "components": [
                    {"component_id": "source1", "name": "left_bracket.stl", "required": True,
                     "quantity_required": 1},
                    {"component_id": "source2", "name": "right_bracket.stl", "required": True,
                     "quantity_required": 1},
                    {"component_id": "source3", "name": "shelf_board.stl", "required": True,
                     "quantity_required": 2}]}
    job = {"title": title, "project_id": PROJECT_ID, "project": manifest,
           "multipart_plan": plan, "project_number": 82}
    (run / "JOB.json").write_text(json.dumps(job))
    vault = Path(tmp) / "vault"
    (vault / "Plans").mkdir(parents=True)
    live = dict(plan, project_number=82,
                parts=[dict(parts[0], completed_quantity=1, status="completed"),
                       dict(parts[1], completed_quantity=1)])
    (vault / "Plans" / f"{PLAN_ID}.json").write_text(json.dumps(live))
    (vault / "Prints").mkdir()
    records = [
        (82, "plate1", "A", "original", True, "complete", "2026-11-08T10:00:00Z", "aaaa1111"),
        (83, "plate2", "B", "original", True, "complete", "2026-11-08T14:00:00Z", "bbbb2222"),
        (84, "plate2", "B", "original", False, "cancelled", "2026-11-08T18:00:00Z", "cccc3333"),
        (85, "plate1", "A", "run-abc123def456", True, "complete", "2026-11-09T09:00:00Z", "dddd4444"),
    ]
    for number, plate, suffix, run_id, ok, status, started, attempt in records:
        (vault / "Prints" / f"print-{number:04d}.json").write_text(json.dumps({
            "print_number": number, "title": title, "status": status, "success": ok,
            "started_at": started, "project_id": PROJECT_ID, "project_number": 82,
            "project_run_id": run_id, "project_plate_id": plate,
            "project_plate_suffix": suffix, "project_plate_ref": f"82{suffix}",
            "print_attempt_id": attempt, "record_id": attempt}))
    # A standalone print is not a project and is not shown.
    (vault / "Prints" / "print-0081.json").write_text(json.dumps(
        {"print_number": 81, "title": "Lone widget", "status": "complete", "success": True}))
    other = Path(tmp) / "archive" / "widget--b"
    other.mkdir()
    (other / "JOB.json").write_text(json.dumps({"title": "Lone widget"}))


@pytest.fixture
def start(tmp_path):
    servers = []

    def _start(environ=None):
        data = _settings(tmp_path, environ)
        httpd = server.make_server(data, environ={} if environ is None else environ, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    yield _start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read().decode()
    finally:
        conn.close()


def test_readout_renders_components_plates_quantities_and_attempts(tmp_path, start):
    _seed(tmp_path)
    port = start()
    code, headers, text = _get(port, "/projects")
    assert code == 200 and headers["Content-Type"].startswith("text/html")
    assert "Project 82: Shelf &lt;b&gt;kit&lt;/b&gt;" in text and "<b>kit</b>" not in text
    for name in ("left_bracket.stl", "right_bracket.stl", "shelf_board.stl"):
        assert name in text
    # Plates with exact aliases, live quantities and every attempt.
    assert "82A" in text and "82B" in text
    assert "1 of 1" in text and "1 of 2" in text
    for attempt in ("#82 · succeeded", "#83 · succeeded", "#84 · cancelled",
                    "#85 · succeeded · reprint run run-abc123def456"):
        assert attempt in text, attempt
    # Resumable completion state, naming what is left and how to continue.
    assert "Resumable: 1 of 2 plates left (82B)" in text
    assert f"/print-next {PLAN_ID}" in text
    assert "Lone widget" not in text
    # No script beyond the shell's shared form guard, and the CSP header.
    assert text.count("<script") == 1
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert '<a href="/projects" class="active">Projects</a>' in text


def test_api_readout_reports_runs_separately_from_the_original(tmp_path, start):
    _seed(tmp_path)
    port = start()
    code, _headers, body = _get(port, "/api/projects")
    assert code == 200
    view = json.loads(body)["projects"][0]
    assert view["project_id"] == PROJECT_ID and view["state"] == "resumable"
    assert [(c["name"], c["quantity"]) for c in view["components"]] == [
        ("left_bracket.stl", 1), ("right_bracket.stl", 1), ("shelf_board.stl", 2)]
    plates = {p["ref"]: p for p in view["plates"]}
    assert (plates["82A"]["quantity"], plates["82A"]["completed_quantity"]) == (1, 1)
    assert (plates["82B"]["quantity"], plates["82B"]["completed_quantity"]) == (2, 1)
    assert [a["attempt_id"] for a in plates["82B"]["attempts"]] == ["bbbb2222", "cccc3333"]
    runs = {r["run_id"]: r for r in view["runs"]}
    assert runs["original"]["attempts"] == 3 and runs["original"]["plates_succeeded"] == 2
    assert runs["run-abc123def456"]["attempts"] == 1
    assert runs["run-abc123def456"]["plates_succeeded"] == 1


def test_complete_project_and_empty_archive_states(tmp_path):
    _seed(tmp_path)
    plan_file = tmp_path / "vault" / "Plans" / f"{PLAN_ID}.json"
    live = json.loads(plan_file.read_text())
    live["parts"][1]["completed_quantity"] = 2
    plan_file.write_text(json.dumps(live))
    views = projects.project_views(tmp_path / "archive", tmp_path / "vault")
    assert views[0]["state"] == "complete" and views[0]["remaining"] == []
    assert "No projects yet" in projects.render_projects_page(
        projects.project_views(tmp_path / "nothing", tmp_path / "vault"))


def test_readout_requires_auth_on_a_claimed_install(tmp_path, start):
    _seed(tmp_path)
    port = start(environ={"WEBUI_ACCESS_TOKEN": TOKEN})
    assert _get(port, "/api/projects")[0] == 401
    assert _get(port, "/projects", headers={server.TOKEN_HEADER: "wrong"})[0] == 401
    code, headers, _ = _get(port, "/projects", headers={"Accept": "text/html"})
    assert code == 303 and headers["Location"] == "/login"
    code, _headers, text = _get(port, "/projects", headers={server.TOKEN_HEADER: TOKEN})
    assert code == 200 and "82A" in text and TOKEN not in text
