"""The Status page dashboard (#1445): the Home Assistant sample screen.

Everything runs offline: settings come from temp YAML files, the live
printer reads and the Spoolman view are injected fakes in the
``(code, body)`` shape the server's own per-printer functions return,
print records are temp files, and the server binds an ephemeral
loopback port.
"""
import http.client
import json
import os
import tempfile
import threading
from pathlib import Path

from print_handy import settings
from print_handy.webui import dashboard, server

SNAP = {
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
    "components": {"ready": True},
}

IDLE = {"state": "idle", "filename": "", "progress": None, "hotend_c": 23.0,
        "bed_c": 21.0, "hotend_target_c": 0, "bed_target_c": 0, "layer": None,
        "total_layers": None, "remaining_min": None}
PRINTING = {"state": "printing", "filename": "benchy.gcode", "progress": 0.42,
            "hotend_c": 210.4, "bed_c": 60.2, "hotend_target_c": 210,
            "bed_target_c": 60, "layer": 30, "total_layers": 100,
            "remaining_min": 25}
UNAVAILABLE = {"state": "unavailable", "filename": "", "progress": None,
               "hotend_c": None, "bed_c": None,
               "detail": "ValueError: no Moonraker URL is configured"}

SPOOL = {"id": 3, "name": "Matte White", "color": "Matte White", "material": "PLA",
         "brand": "Sunlu", "color_hex": "#f2f2f2", "remaining_weight": 870.0,
         "initial_weight": 1000.0, "remaining_percent": 87.0,
         "archived": False, "location": None}


def load(tmp, extra="", vault=True):
    local = Path(tmp) / "local.yaml"
    vault_line = f"  records_vault: {tmp}/vault\n" if vault else ""
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n{vault_line}{extra}")
    return settings.load(local=local, environ={})


def status_fake(live, name="Garage AD5X"):
    def status_for(printer_id):
        return 200, {"ok": True, "id": printer_id, "name": name,
                     "model": "flashforge.ad5x", "status": dict(live)}
    return status_for


def spools_fake(slots=None, shelf=None):
    def spools_for(printer_id):
        return 200, {"ok": True, "id": printer_id,
                     "slots": slots if slots is not None
                     else [{"slot": 1, "spool": SPOOL}, {"slot": 2, "spool": None}],
                     "shelf": shelf if shelf is not None
                     else [dict(SPOOL, id=9, name="Natural", material="PLA+")],
                     "spools": [], "conflicts": []}
    return spools_for


def write_record(tmp, name, payload):
    prints = Path(tmp) / "vault" / "Prints"
    prints.mkdir(parents=True, exist_ok=True)
    (prints / name).write_text(json.dumps(payload), encoding="utf-8")


def test_idle_dashboard_renders_the_sample_screen():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "services:\n  camera_snapshot_url: http://cam.local/snap.jpg\n")
        write_record(tmp, "print-1.json", {
            "print_number": 7, "status": "completed", "title": "Lantern",
            "started_at": "2026-11-13T10:00:00Z", "completed_at": "2026-11-13T14:18:00Z",
            "filament_material": "PLA+", "filament_used_g": 62.2,
            "print_duration_seconds": 15480})
        clock = [1000.0]
        history = dashboard.TempHistory(clock=lambda: clock[0])
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(), history=history)
        clock[0] += 3600  # a second sample an hour later draws the graph
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(), history=history)
        page = server.render_status_html(SNAP, dashboard=dash)
    for needle in ("Garage AD5X", "Ready for the next print", ">0%<",
                   'src="/api/printers/flashforge-ad5x/camera/snapshot"',
                   "Matte White", "870 g", "87%", "Natural", "Shelf",
                   "Doctor failures", "Print error", "0.test",
                   "Recent prints", "Lantern", "4h 18m", "62 g",
                   '<polyline class="nozzle"'):
        assert needle in page, needle
    assert '<button type="button" class="danger"' not in page  # controls off: no E-stop


def test_printing_dashboard_shows_progress_temps_and_estop_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "webui:\n  controls_enabled: true\n")
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(PRINTING), spools_for=spools_fake(),
            history=dashboard.TempHistory())
        page = server.render_status_html(SNAP, dashboard=dash)
    for needle in ("benchy.gcode", ">42%<", "Layer 30 of 100", "about 25 min left",
                   "210°C", "60°C", "Printing"):
        assert needle in page, needle
    assert '<button type="button" class="danger" data-estop="flashforge-ad5x"' in page
    assert "/dashboard/estop/" in page  # session+csrf route: a browser has no token header


def test_offline_printer_card_says_unavailable_and_the_page_still_renders():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(UNAVAILABLE), spools_for=spools_fake(),
            history=dashboard.TempHistory())
        page = server.render_status_html(SNAP, dashboard=dash)
    assert "Printer unavailable" in page
    assert "no Moonraker URL is configured" in page
    assert "Unknown" in page  # the health row's print error state
    assert "All good" in page  # the snapshot sections render as before


def test_a_broken_section_never_breaks_the_dashboard():
    def boom_status(printer_id):
        raise RuntimeError("read exploded")

    def boom_spools(printer_id):
        raise RuntimeError("spoolman exploded")

    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=boom_status, spools_for=boom_spools,
            history=dashboard.TempHistory())
        page = server.render_status_html(SNAP, dashboard=dash)
    assert "Printer unavailable" in page and "read exploded" in page
    assert "Filament unavailable" in page


def test_multi_printer_roster_gets_one_card_per_printer():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "printers:\n  - id: first\n    name: First\n"
                         "    model: flashforge.ad5x\n"
                         "  - id: second\n    name: Second\n"
                         "    model: klipper.generic\n")
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(),
            history=dashboard.TempHistory())
    assert [card["id"] for card in dash["cards"]] == ["first", "second"]
    page = server.render_status_html(SNAP, dashboard=dash)
    assert page.count('class="card dash-card"') == 2


def test_temp_history_is_throttled_and_bounded():
    clock = [0.0]
    history = dashboard.TempHistory(clock=lambda: clock[0])
    history.sample("p", 200.0, 60.0)
    history.sample("p", 201.0, 61.0)  # inside the interval: dropped
    assert len(history.samples("p")) == 1
    history.sample("p", None, None)  # no reading at all: skipped
    clock[0] += 21
    history.sample("p", 202.0, 62.0)
    assert [row[1] for row in history.samples("p")] == [200.0, 202.0]
    for _ in range(dashboard.TempHistory.MAX_SAMPLES + 40):
        clock[0] += 21
        history.sample("p", 100.0, 50.0)
    assert len(history.samples("p")) == dashboard.TempHistory.MAX_SAMPLES


def test_recent_prints_newest_first_finished_only():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        write_record(tmp, "print-1.json", {
            "status": "completed", "title": "Older",
            "completed_at": "2026-11-10T10:00:00Z", "filament_material": "PLA",
            "filament_used_g": 10.0, "print_duration_seconds": 600})
        write_record(tmp, "print-2.json", {
            "status": "failed", "title": "Newer failure",
            "completed_at": "2026-11-12T10:00:00Z", "filament_material": "PETG",
            "filament_used_g": 3.5, "print_duration_seconds": 60})
        write_record(tmp, "print-3.json", {
            "status": "printing", "title": "Still running",
            "started_at": "2026-11-13T10:00:00Z"})
        rows = dashboard.recent_prints(data)
    assert [row["title"] for row in rows] == ["Newer failure", "Older"]
    assert rows[0]["status"] == "failed" and rows[1]["filament_g"] == 10.0


def test_latest_timelapse_picks_the_newest_clip():
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n  cache: {tmp}/cache\n")
        data = settings.load(local=local, environ={})
        clips = Path(tmp) / "cache" / "moonraker-announcements"
        clips.mkdir(parents=True)
        old = clips / "print-1_old.mp4"
        new = clips / "print-2_new.mp4"
        old.write_bytes(b"x" * 10)
        new.write_bytes(b"x" * 2048)
        os.utime(old, (1_000_000, 1_000_000))
        os.utime(new, (2_000_000, 2_000_000))
        found = dashboard.latest_timelapse(data)
    assert found["name"] == "print-2_new.mp4" and found["size"] == 2048


def test_no_dashboard_keeps_the_legacy_status_page():
    page = server.render_status_html(SNAP)
    assert 'class="progress"' not in page
    assert 'class="card dash-card"' not in page and '<div class="dash-health">' not in page


def test_server_status_page_serves_the_dashboard():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "webui:\n  enabled: true\n  port: 8765\n")
        httpd = server.make_server(
            data, environ={}, port=0, snapshot_func=lambda: SNAP,
            printer_status_func=status_fake(PRINTING),
            spools_view_func=spools_fake())
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection(
                "127.0.0.1", httpd.server_address[1], timeout=5)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                body = response.read().decode()
                assert response.status == 200
            finally:
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
    assert "dash-card" in body and "benchy.gcode" in body
    assert "Matte White" in body and "Recent prints" not in body  # no records written


def test_latest_timelapse_reuses_one_walk_within_its_ttl():
    """The Status page does not walk the clip roots on every load."""
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n  cache: {tmp}/cache\n")
        data = settings.load(local=local, environ={})
        clips = Path(tmp) / "cache" / "moonraker-announcements"
        clips.mkdir(parents=True)
        (clips / "print-1_first.mp4").write_bytes(b"x")
        first = dashboard.latest_timelapse(data, now=1000.0)
        later = clips / "print-2_later.mp4"
        later.write_bytes(b"xx")
        os.utime(later, (4_000_000_000, 4_000_000_000))
        cached = dashboard.latest_timelapse(data, now=1000.0 + dashboard._TIMELAPSE_TTL_S / 2)
        fresh = dashboard.latest_timelapse(data, now=1000.0 + dashboard._TIMELAPSE_TTL_S + 1)
    assert first["name"] == cached["name"] == "print-1_first.mp4"
    assert fresh["name"] == "print-2_later.mp4"


def test_estop_button_works_for_a_signed_in_browser_on_a_claimed_install():
    """The button used to post to the header-only API route, so on any
    install with a login every press got a 401. A signed-in browser now
    stops the printer with its session plus the page's _csrf, and neither
    a bare cookie nor a stale token sends anything."""
    import re
    from urllib.parse import urlencode
    token = "test-webui-token-12345"
    sent = []

    def control(printer_id, verb, body):
        sent.append((printer_id, verb, body))
        return 200, {"ok": True, "id": printer_id, "action": verb, "detail": "stop sent"}

    def call(port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    form = {"Content-Type": "application/x-www-form-urlencoded"}
    with tempfile.TemporaryDirectory() as tmp:
        environ = {"WEBUI_ACCESS_TOKEN": token}
        data = load(tmp, "webui:\n  enabled: true\n  port: 8765\n  controls_enabled: true\n")
        httpd = server.make_server(
            data, environ=environ, port=0, snapshot_func=lambda: SNAP,
            printer_status_func=status_fake(PRINTING), spools_view_func=spools_fake(),
            printer_control_func=control)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            code, headers, _ = call(port, "POST", "/login",
                                    body=urlencode({"token": token}), headers=form)
            assert code == 303
            cookie = {"Cookie": headers["set-cookie"].split(";", 1)[0].strip()}
            code, _, page = call(port, "GET", "/", headers=cookie)
            assert code == 200
            csrf = re.search(r'id="dash-csrf" value="([^"]*)"', page).group(1)
            assert csrf
            path = "/dashboard/estop/flashforge-ad5x"
            # The cookie alone, or a wrong token, sends nothing.
            for body in ("", urlencode({"_csrf": "stale"})):
                code, _, _ = call(port, "POST", path, body=body, headers={**form, **cookie})
                assert code == 403
            # No session at all sends nothing either.
            code, _, _ = call(port, "POST", path, body=urlencode({"_csrf": csrf}), headers=form)
            assert code in (401, 403)
            assert sent == []
            # Session plus the page's token: the API's own stop, confirmed.
            code, _, body = call(port, "POST", path, body=urlencode({"_csrf": csrf}),
                                 headers={**form, **cookie})
            assert code == 200 and json.loads(body)["ok"] is True
            assert sent == [("flashforge-ad5x", "emergency_stop", {"confirm": True})]
        finally:
            httpd.shutdown()
            httpd.server_close()


# ------------------------------------------------------- printer screen (#1622)

SCREEN_WEBCAMS = [
    {"name": "video", "service": "mjpegstreamer-adaptive",
     "stream_url": "/webcam/?action=stream",
     "snapshot_url": "/webcam/?action=snapshot"},
    {"name": "screen", "service": "iframe",
     "stream_url": "/screen/stream", "snapshot_url": "/screen/snapshot"},
]


def test_dashboard_shows_screen_panel_when_a_screen_is_published():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(),
            screen_for=lambda printer_id: True)
        page = server.render_status_html(SNAP, dashboard=dash)
    assert 'src="/api/printers/flashforge-ad5x/screen/snapshot"' in page
    assert "data-dash-screen src=" in page
    assert "<h3>Printer screen</h3>" in page
    # Labeled as the printer's own display, never as a camera, and
    # linked to the dedicated screen page.
    assert "not the bed camera" in page
    assert 'href="/printers/flashforge-ad5x/screen"' in page
    # The refresh script covers the screen still beside the camera's.
    assert "img[data-dash-cam],img[data-dash-screen]" in page


def test_dashboard_omits_screen_panel_when_no_screen_is_published():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(),
            screen_for=lambda printer_id: False)
        page = server.render_status_html(SNAP, dashboard=dash)
    assert "/screen/snapshot" not in page
    assert "data-dash-screen src=" not in page
    assert "<h3>Printer screen</h3>" not in page
    # The rest of the card renders exactly as before.
    assert "Garage AD5X" in page
    assert "Ready for the next print" in page


def test_dashboard_screen_check_failure_renders_the_card_without_a_panel():
    def broken_screen_for(printer_id):
        raise RuntimeError("discovery exploded")

    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(IDLE), spools_for=spools_fake(),
            screen_for=broken_screen_for)
        page = server.render_status_html(SNAP, dashboard=dash)
    assert dash["cards"][0]["screen"] is False
    assert "data-dash-screen src=" not in page
    assert "Garage AD5X" in page


def test_screen_availability_uses_the_cached_discovery(monkeypatch):
    from print_handy.webui import screen as screen_module

    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, "services:\n  moonraker_url: http://192.0.2.10:7125\n")
        printer_id = settings.default_printer_id(data)
        seen = []

        def fake_cached_discover(url):
            seen.append(url)
            return SCREEN_WEBCAMS

        monkeypatch.setattr(screen_module, "_cached_discover", fake_cached_discover)
        assert dashboard._screen_available(data, printer_id) is True
        assert seen == ["http://192.0.2.10:7125"]

        monkeypatch.setattr(
            screen_module, "_cached_discover",
            lambda url: [entry for entry in SCREEN_WEBCAMS
                         if entry["name"] != "screen"])
        assert dashboard._screen_available(data, printer_id) is False

        def exploding_discover(url):
            raise OSError("printer unreachable")

        monkeypatch.setattr(screen_module, "_cached_discover", exploding_discover)
        assert dashboard._screen_available(data, printer_id) is False


def test_screen_availability_is_false_without_a_moonraker_connection():
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        printer_id = settings.default_printer_id(data)
        assert dashboard._screen_available(data, printer_id) is False
