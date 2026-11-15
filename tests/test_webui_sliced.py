"""#1443: the Web UI Sliced files page and its downloads.

A sliced job's plate files are downloadable from the browser — plain
.gcode and ZMod .gcode.3mf alike — resolved exactly like the Layers
page resolves its plates: only files the job's own JOB.json names,
inside that job's archive folder. The Sources page gains the link form
that hands a pasted model link to web chat for slicing.
"""
from __future__ import annotations

import asyncio
import http.client
import json
import threading
from pathlib import Path

from print_handy import settings
from print_handy.bot import web as web_module
from print_handy.webui import server, sliced, sources

GCODE = "G28\nG1 X10 Y10 E1\n"
PACKAGE = b"PK\x03\x04 fake 3mf package bytes"


def _job(archive: Path, job_id: str, payload: dict, files: dict) -> Path:
    job = archive / job_id
    job.mkdir(parents=True)
    for name, content in files.items():
        path = job / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    (job / "JOB.json").write_text(json.dumps(payload), encoding="utf-8")
    return job


def _archive(tmp_path: Path) -> Path:
    archive = tmp_path / "archive"
    _job(archive, "job-1", {"title": "Duplo swing", "gcode": "plate_1.gcode",
                            "derived_plates": [{"gcode": "bambu/plate_1.gcode.3mf"}]},
         {"plate_1.gcode": GCODE, "bambu/plate_1.gcode.3mf": PACKAGE})
    return archive


def test_list_and_resolve_cover_gcode_and_the_3mf_package(tmp_path):
    archive = _archive(tmp_path)
    rows = sliced.list_sliced(archive)
    assert len(rows) == 1 and rows[0]["job_id"] == "job-1" and rows[0]["title"] == "Duplo swing"
    assert [(p["index"], p["name"]) for p in rows[0]["plates"]] == [
        (0, "plate_1.gcode"), (1, "plate_1.gcode.3mf")]
    assert sliced.resolve_download(archive, "job-1", "0") == (archive / "job-1" / "plate_1.gcode").resolve()
    assert sliced.resolve_download(archive, "job-1", "1") == (
        archive / "job-1" / "bambu" / "plate_1.gcode.3mf").resolve()
    assert sliced.content_type(archive / "job-1" / "plate_1.gcode").startswith("text/plain")
    assert sliced.content_type(archive / "job-1" / "bambu" / "plate_1.gcode.3mf") == "application/octet-stream"


def test_downloads_stay_inside_the_jobs_own_folder(tmp_path):
    archive = _archive(tmp_path)
    (tmp_path / "secret.gcode").write_text(GCODE, encoding="utf-8")
    _job(archive, "job-evil", {"title": "Evil", "gcode": "../secret.gcode"},
         {})
    assert sliced.resolve_download(archive, "job-evil", "0") is None
    assert sliced.resolve_download(archive, "..", "0") is None
    assert sliced.resolve_download(archive, "job-1", "2") is None
    assert sliced.resolve_download(archive, "job-1", "plate_1.gcode") is None
    assert sliced.resolve_download(archive, "job-1", "../0") is None
    rows = sliced.list_sliced(archive)
    assert [row["job_id"] for row in rows] == ["job-1"]  # job-evil has no servable file


def test_page_lists_downloads_and_the_empty_state(tmp_path):
    page = sliced.render_sliced_page(sliced.list_sliced(_archive(tmp_path)))
    assert "Duplo swing" in page and "plate_1.gcode" in page and "plate_1.gcode.3mf" in page
    assert 'href="/sliced/job-1/0"' in page and 'href="/sliced/job-1/1"' in page
    assert "Nothing on this page starts a print" in page
    empty = sliced.render_sliced_page(sliced.list_sliced(tmp_path / "nope"))
    assert "No sliced files are in the archive yet" in empty


def test_sources_page_offers_the_start_with_a_link_form_only_when_chat_can_take_it():
    # #1443's Slice a link form handed the link to web chat; the #1450
    # workflow page supersedes it in the combined PR, so the Sources
    # form now starts the workflow, which ends in the same download.
    page = sources.render_sources_page(form_token="tok123", chat_enabled=True)
    assert '<form method="post" action="/workflow/link">' in page
    assert 'name="_csrf" value="tok123"' in page and 'name="url"' in page
    assert "Start with a link" in page and 'href="/workflow"' in page
    page = sources.render_sources_page(form_token="tok123", chat_enabled=False)
    assert "<form" not in page
    assert "Web chat is not enabled" in page


def test_web_chat_note_points_a_sliced_file_at_the_downloads_page(tmp_path):
    class RecordingOutbox:
        def __init__(self):
            self.entries = []

        def append(self, entry, scope=""):
            self.entries.append((entry, scope))

    outbox = RecordingOutbox()
    adapter = web_module.WebAdapter(outbox)
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text(GCODE, encoding="utf-8")
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    asyncio.run(adapter.send("webui", "Done", files=[gcode]))
    asyncio.run(adapter.send("webui", "Done", files=[clip]))
    notes = [entry["content"] for entry, _scope in outbox.entries if "📎" in entry["content"]]
    assert notes[0] == "📎 plate_1.gcode — download it from the Web UI's Sliced files page."
    assert notes[1] == "📎 clip.mp4"


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_server_lists_and_downloads_sliced_files(tmp_path):
    archive = _archive(tmp_path)
    local = tmp_path / "local.yaml"
    local.write_text(
        "paths:\n"
        f"  state_home: {tmp_path}/hermes\n"
        f"  archive: {archive}\n"
        "webui:\n  enabled: true\n  port: 8765\n",
        encoding="utf-8")
    data = settings.load(local=local, environ={"PRINT_HANDY_CONFIG": str(local)})
    httpd = server.make_server(
        data, environ={"PRINT_HANDY_CONFIG": str(local)}, port=0,
        snapshot_func=lambda: {
            "version": "0.test", "host": "testhost", "ok": True,
            "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                        "vendor": "flashforge", "connection": "moonraker"},
            "bot": {"platform": "discord", "mode": "standalone", "ok": True,
                    "reason": "connected", "heartbeat": {"connected": True}},
            "listener": {"required": True, "ok": True, "reason": "alive"},
            "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                         "detail": "HTTP 200"},
            "services": {},
            "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
        })
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        status, _headers, body = _get(port, "/")
        assert status == 200 and b'href="/sliced"' in body
        status, _headers, body = _get(port, "/sliced")
        assert status == 200 and b"Duplo swing" in body and b"plate_1.gcode.3mf" in body
        status, headers, body = _get(port, "/sliced/job-1/0")
        assert status == 200 and body == GCODE.encode()
        assert headers["Content-Disposition"] == 'attachment; filename="plate_1.gcode"'
        status, headers, body = _get(port, "/sliced/job-1/1")
        assert status == 200 and body == PACKAGE
        assert headers["Content-Disposition"] == 'attachment; filename="plate_1.gcode.3mf"'
        status, _headers, _body = _get(port, "/sliced/job-1/9")
        assert status == 404
        status, _headers, _body = _get(port, "/sliced/../secret.gcode")
        assert status == 404
        # job-1 recorded no conversion provenance, so no report link and a 404.
        assert b"/settings-report/job-1" not in _get(port, "/sliced")[2]
        status, _headers, _body = _get(port, "/settings-report/job-1")
        assert status == 404
        job_json = archive / "job-1" / "JOB.json"
        record = json.loads(job_json.read_text(encoding="utf-8"))
        record["provenance"] = {"conversion_provenance": {"source_format": "bambu", "settings": {
            "layer_height": {"project_value": 0.2, "selected_value": 0.2, "selected_reason": "designer"},
            "nozzle_temperature": {"project_value": 230, "selected_value": 220,
                                   "selected_reason": "material_profile"}}}}
        job_json.write_text(json.dumps(record), encoding="utf-8")
        status, _headers, body = _get(port, "/sliced")
        assert status == 200 and b'href="/settings-report/job-1"' in body
        status, headers, body = _get(port, "/settings-report/job-1")
        assert status == 200
        assert headers["Content-Disposition"] == 'attachment; filename="job-1-settings-decisions.json"'
        report = json.loads(body)
        assert [(row["setting"], row["status"]) for row in report["settings"]] == [
            ("layer_height", "kept"), ("nozzle_temperature", "replaced")]
        status, _headers, _body = _get(port, "/settings-report/../job-1")
        assert status == 404
        status, _headers, body = _get(port, "/sources")
        assert status == 200 and b"Start with a link" in body and b'action="/workflow/link"' in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_list_sliced_lists_only_the_newest_jobs_up_to_the_limit(tmp_path):
    """The Sliced files page has a row limit: older jobs past it are not read."""
    import os
    archive = tmp_path / "archive"
    for n in range(5):
        job = _job(archive, f"job-{n}", {"title": f"Job {n}", "gcode": "plate_1.gcode"},
                   {"plate_1.gcode": GCODE})
        os.utime(job / "JOB.json", (1_000_000 + n, 1_000_000 + n))
    rows = sliced.list_sliced(archive, limit=2)
    assert [row["job_id"] for row in rows] == ["job-4", "job-3"]
