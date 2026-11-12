"""Read-only sliced layers (#624, stage 1)."""
import http.client
import json
import threading
from pathlib import Path

from print_handy import settings, webui_gate
from print_handy.webui import layers, server

PLATE = """G90
M83
G0 X10 Y10 Z0.2
;TYPE:Outer wall
;LAYER_CHANGE
G1 X20 Y10 E0.02
G1 X20 Y20 E0.02
;TYPE:Internal infill
G1 X12 Y12 E0.02
G1 X18 Y18 E0.02
;TYPE:Support
G1 X30 Y10 E0.02
G1 X36 Y10 E0.02
G0 X8 Y8
;LAYER_CHANGE
;TYPE:Inner wall
G1 Z0.4
G1 X11 Y11 E0.02
G1 X19 Y11 E0.02
"""

NOTE = "this file is a note, not a sliced plate\n"


def test_roles_colors_and_layer_range():
    parsed = layers.parse_gcode(PLATE)
    assert parsed["ok"]
    assert len(parsed["layers"]) == 2
    roles = {move["role"] for move in parsed["layers"][0]["moves"]}
    assert roles == {"perimeter", "infill", "support", "travel"}
    page = layers.render_plate(parsed, 0, title="swing")
    for role, color in layers.COLORS.items():
        if role == "other":
            continue
        assert f'data-role="{role}"' in page
        assert color in page
    assert 'stroke="#3d8bfd"' in page
    assert "Layer 1 of 2" in page
    assert '<a href="?layer=' in page and "Previous layer (this is the first layer)" in page
    assert '<a href="?layer=1">Next layer</a>' in page
    last = layers.render_plate(parsed, 99, title="swing")
    assert "Layer 2 of 2" in last
    assert "Next layer (this is the last layer)" in last
    assert ">Next layer</a>" not in last
    assert "Previous layer" in last
    assert layers.clamp_layer(2, -4) == 0
    assert "Pause" not in page and "Exclude" not in page and 'method="post"' not in page  # view tools only, no printer controls
    source = Path(layers.__file__).read_text(encoding="utf-8")
    assert "prepared_intake" not in source
    assert "print_control" not in source


BAMBU_PLATE = """G90
M83
G0 X10 Y10 Z0.2
; FEATURE: Outer wall
G1 X20 Y10 E0.02
G1 X20 Y20 E0.02
; CHANGE_LAYER
; FEATURE: Sparse infill
G1 X12 Y12 E0.02
; FEATURE: Support
G1 X30 Y10 E0.02
"""


def test_bambu_dialect_markers_split_layers_and_roles():
    # P2-259: Bambu profiles emit "; CHANGE_LAYER" and "; FEATURE:" rather
    # than the Prusa-dialect markers; both dialects must parse.
    parsed = layers.parse_gcode(BAMBU_PLATE)
    assert parsed["ok"]
    assert len(parsed["layers"]) == 2
    assert {move["role"] for move in parsed["layers"][0]["moves"]} == {"perimeter"}
    assert {move["role"] for move in parsed["layers"][1]["moves"]} == {"infill", "support"}


def test_intake_generated_job_ids_are_accepted(tmp_path):
    # P2-257: intake_folder() names a chat job slug[:70] + "--" + 64 hex
    # (up to 136 chars); the layers catalog must accept the full length
    # while still refusing traversal-shaped ids.
    job_id = "a-fairly-long-model-slug-from-a-chat-link--" + "a" * 64
    job = tmp_path / job_id
    job.mkdir()
    (job / "plate_1.gcode").write_text(PLATE, encoding="utf-8")
    (job / "JOB.json").write_text(json.dumps({
        "title": "Long slug job", "gcode": "plate_1.gcode",
    }), encoding="utf-8")
    rows = layers.list_plates(tmp_path)
    assert [row["job_id"] for row in rows] == [job_id]
    assert layers.resolve_plate(tmp_path, job_id, "0") == (job / "plate_1.gcode").resolve()
    code, _ctype, _page = layers.respond(tmp_path, f"{job_id}/0", "0")
    assert code == 200
    assert layers.resolve_plate(tmp_path, "..", "0") is None
    assert layers.resolve_plate(tmp_path, "a/b", "0") is None


def test_unreadable_file_is_an_error_with_no_drawing():
    parsed = layers.parse_gcode(NOTE)
    assert parsed == {"ok": False, "error": "this G-code has no extrusion to draw"}
    assert "layers" not in parsed
    page = layers.render_plate(parsed, 0, title="bad")
    assert "<svg" not in page
    assert "this G-code has no extrusion to draw" in page
    assert "Pause" not in page and "<button" not in page


def test_archive_paths_stay_inside_the_named_plate(tmp_path):
    job = tmp_path / "job-1"
    job.mkdir()
    (job / "plate_1.gcode").write_text(PLATE, encoding="utf-8")
    (job / "note.gcode").write_text(NOTE, encoding="utf-8")
    (tmp_path / "secret.gcode").write_text(PLATE, encoding="utf-8")
    (job / "JOB.json").write_text(json.dumps({
        "title": "Duplo swing",
        "gcode": "plate_1.gcode",
    }), encoding="utf-8")
    assert layers.resolve_plate(tmp_path, "job-1", "0") == (job / "plate_1.gcode").resolve()
    assert layers.resolve_plate(tmp_path, "job-1", "1") is None
    assert layers.resolve_plate(tmp_path, "job-1", "plate_1.gcode") is None
    assert layers.resolve_plate(tmp_path, "job-1", "../secret.gcode") is None
    assert layers.resolve_plate(tmp_path, "..", "0") is None
    rows = layers.list_plates(tmp_path)
    assert rows == [{"job_id": "job-1", "index": 0, "file": "plate_1.gcode", "title": "Duplo swing"}]
    code, _ctype, body = layers.respond(tmp_path, "job-1/note.gcode", "0")
    assert code == 404
    code, ctype, page = layers.respond(tmp_path, "job-1/0", "0")
    assert code == 200 and ctype.startswith("text/html")
    assert "Layer 1 of 2" in page and "#3d8bfd" in page
    code, _ctype, bad = layers.respond(tmp_path, "", "0")
    assert code == 200 and "Duplo swing" in bad


def test_every_plate_of_a_job_is_listed_and_opens_by_position(tmp_path):
    # Each plate is plate_1.gcode in its own folder, so the name cannot tell them apart.
    job = tmp_path / "job-1"
    for name in ("a", "b", "c"):
        (job / name).mkdir(parents=True)
        (job / name / "plate_1.gcode").write_text(PLATE, encoding="utf-8")
    (job / "a" / "plate_1.gcode").write_text(PLATE + "G1 X9 Y9 E1\n", encoding="utf-8")
    (job / "JOB.json").write_text(json.dumps({
        "title": "Trio", "gcode": "a/plate_1.gcode",
        "derived_plates": [{"gcode": "b/plate_1.gcode"}, {"gcode": "c/plate_1.gcode"},
                           {"gcode": "../outside/plate_1.gcode"}]}), encoding="utf-8")
    rows = layers.list_plates(tmp_path)
    assert [(r["index"], r["file"]) for r in rows] == [
        (0, "a/plate_1.gcode"), (1, "b/plate_1.gcode"), (2, "c/plate_1.gcode")]
    for row in rows:
        assert layers.resolve_plate(tmp_path, "job-1", str(row["index"])) == (job / row["file"]).resolve()
    assert layers.resolve_plate(tmp_path, "job-1", "3") is None
    page = layers.respond(tmp_path, "", "0")[2]
    assert "/layers/job-1/0" in page and "/layers/job-1/1" in page and "/layers/job-1/2" in page


def test_server_serves_layers_and_a_disabled_webui_does_not_start(tmp_path):
    archive = tmp_path / "archive"
    job = archive / "job-1"
    job.mkdir(parents=True)
    (job / "plate_1.gcode").write_text(PLATE, encoding="utf-8")
    (job / "JOB.json").write_text(json.dumps({"title": "Duplo swing", "gcode": "plate_1.gcode"}),
                                  encoding="utf-8")
    local = tmp_path / "local.yaml"
    local.write_text(
        "paths:\n"
        f"  state_home: {tmp_path}/hermes\n"
        f"  archive: {archive}\n"
        "webui:\n  enabled: true\n  port: 8765\n",
        encoding="utf-8",
    )
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
        },
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        status, body = _get(port, "/")
        assert status == 200 and 'href="/layers"' in body
        status, body = _get(port, "/layers")
        assert status == 200 and "Duplo swing" in body and "plate_1.gcode" in body
        status, body = _get(port, "/layers/job-1/0?layer=5")
        assert status == 200 and "Layer 2 of 2" in body and "<svg" in body
        status, body = _get(port, "/layers/job-1/../secret.gcode")
        assert status == 404
        assert webui_gate.decide({"webui": {"enabled": False, "host": "127.0.0.1", "port": 8080,
                                             "access_token_env": "WEBUI_ACCESS_TOKEN"}}) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()
