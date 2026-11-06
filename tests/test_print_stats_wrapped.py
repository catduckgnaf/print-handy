"""#956: print stats, Wrapped, the Data/Wrapped Web UI surface, and the video builder.

Fixture records only, written as JSON files in a temporary Prints
folder; the video builder runs with a fake ffmpeg runner. No network,
no real ffmpeg, no printer.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from print_handy import settings
from print_handy.core import print_stats
from print_handy.webui import catalog, data as data_module, server, shell
from print_handy.webui import wrapped_video

TOKEN = "wrapped-" + "test-token"

RECORDS = [
    {"print_number": 1, "status": "completed", "success": True, "title": "Bracket",
     "started_at": "2025-03-01T02:30:00Z", "completed_at": "2025-03-01T04:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "bracket.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 100.0, "filament_used_mm": 30000,
     "estimated_filament_g": 90.0, "estimated_time": 7000,
     "print_duration_seconds": 7200, "slicer_settings": {"nozzle_diameter": 0.4},
     "watch_mode": "normal", "source_platform": "discord"},
    {"print_number": 2, "status": "completed", "success": True, "title": "Clip",
     "started_at": "2025-03-15T12:00:00Z", "completed_at": "2025-03-15T13:00:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "clip.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 50.0, "estimated_filament_g": 55.0,
     "print_duration_seconds": 3600, "slicer_settings": {"nozzle_diameter": 0.4}},
    {"print_number": 3, "status": "failed", "success": False, "title": "Vase",
     "started_at": "2025-07-04T18:00:00Z", "completed_at": "2025-07-04T18:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "vase.gcode",
     "filament_vendor": "eSun", "filament_material": "PETG", "filament_color": "Red",
     "spoolman_spool_id": 7, "filament_used_g": 20.0,
     "print_duration_seconds": 1800, "slicer_settings": '{"nozzle_diameter": 0.6}',
     "watch_mode": "early_layer", "watch_risk": "slicer finding",
     "stop_reason": "spaghetti detected"},
    {"print_number": 4, "status": "cancelled", "success": False, "title": "Gear",
     "started_at": "2025-07-05T18:00:00Z", "completed_at": "2025-07-05T18:10:00Z",
     "printer": "AD5X", "gcode_file": "gear.gcode",
     "filament_used_g": 10.0, "print_duration_seconds": 600, "slicer_settings": None},
    {"print_number": 5, "status": "printing", "success": None, "title": "Running",
     "started_at": "2025-07-06T18:00:00Z",
     "printer": "AD5X", "gcode_file": "running.gcode",
     "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 5.0, "print_duration_seconds": 999},
    {"print_number": 6, "status": "completed", "success": True, "title": "Old Big",
     "started_at": "2024-01-10T12:00:00Z", "completed_at": "2024-01-10T22:00:00Z",
     "printer": "AD5X", "gcode_file": "old.gcode",
     "filament_vendor": "Sunlu", "filament_material": "ABS", "filament_color": "White",
     "spoolman_spool_id": 9, "filament_used_g": 500.0,
     "print_duration_seconds": 36000, "slicer_settings": {"nozzle_diameter": 0.4}},
]

FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}


def write_vault(tmp):
    vault = Path(tmp) / "vault"
    prints = vault / "Prints"
    prints.mkdir(parents=True)
    for index, record in enumerate(RECORDS, start=1):
        (prints / f"print-{index:04d}.json").write_text(json.dumps(record))
    (prints / "print-9999.json").write_text("{half-written")
    return vault


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     f"  records_vault: {tmp}/vault\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


class AggregateTests(unittest.TestCase):
    def test_totals_rates_and_unknown_buckets(self):
        stats = print_stats.aggregate(RECORDS, year=2025)
        self.assertEqual(stats["total_prints"], 5)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["canceled"], 1)
        self.assertEqual(stats["in_progress"], 1)
        self.assertEqual(stats["success_rate"], 0.5)  # printing excluded
        self.assertEqual(stats["total_print_duration_seconds"], 14199.0)
        self.assertEqual(stats["total_hours"], 3.94)
        self.assertEqual(stats["longest_print"]["title"], "Bracket")
        self.assertEqual(stats["longest_print"]["seconds"], 7200.0)
        self.assertEqual(stats["total_filament_used_g"], 185.0)
        self.assertEqual(stats["total_filament_used_kg"], 0.185)
        self.assertEqual(stats["total_estimated_filament_g"], 145.0)
        by_color = stats["breakdowns"]["by_color"]
        self.assertEqual(by_color["Black"]["count"], 3)
        self.assertEqual(by_color["unknown"]["count"], 1)
        self.assertEqual(stats["breakdowns"]["by_material"]["unknown"]["count"], 1)
        self.assertEqual(stats["breakdowns"]["by_vendor"]["unknown"]["count"], 2)
        self.assertEqual(stats["breakdowns"]["by_spool"]["unknown"]["count"], 2)
        by_nozzle = stats["breakdowns"]["by_nozzle"]
        self.assertEqual(by_nozzle["0.4"]["count"], 2)
        self.assertEqual(by_nozzle["0.6"]["count"], 1)  # string slicer_settings parsed
        self.assertEqual(by_nozzle["unknown"]["count"], 2)
        self.assertEqual(stats["breakdowns"]["by_printer"]["AD5X"]["count"], 5)
        self.assertEqual(stats["failures"]["stop_reasons"], {"spaghetti detected": 1})
        # watch_risk is a non-empty string naming the risk; that truthy shape is the flag.
        self.assertEqual(stats["watchdog_flagged"], 1)
        self.assertEqual(stats["monthly"], {"2025-03": 2, "2025-07": 3})
        self.assertEqual(stats["top_by_duration"][0]["title"], "Bracket")
        self.assertEqual(stats["top_by_filament"][0]["filament_used_g"], 100.0)
        self.assertEqual(stats["years_available"], [2024, 2025])

    def test_loader_skips_and_names_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = write_vault(tmp)
            records, skipped = print_stats.load_records(vault)
        self.assertEqual(len(records), 6)
        self.assertEqual(skipped, ["print-9999.json"])

    def test_missing_dir_is_empty(self):
        records, skipped = print_stats.load_records("/no/such/vault")
        self.assertEqual((records, skipped), ([], []))
        self.assertIsNone(print_stats.aggregate([])["success_rate"])


class WrappedTests(unittest.TestCase):
    def test_highlights_and_data_driven_script(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        self.assertEqual(wrapped["headline"]["total_prints"], 5)
        highlights = wrapped["highlights"]
        self.assertEqual(highlights["top_color"], "Black")
        self.assertEqual(highlights["top_material"], "PLA")
        self.assertEqual(highlights["top_printer"], "AD5X")
        self.assertEqual(highlights["busiest_month"], "2025-07")
        self.assertEqual(highlights["longest_print"]["title"], "Bracket")
        self.assertEqual(highlights["biggest_by_filament"]["title"], "Bracket")
        self.assertEqual(highlights["watchdog_flagged"], 1)
        self.assertEqual(highlights["worst_failure_streak"], 2)
        script = "\n".join(wrapped["script"])
        self.assertIn("top color was Black", script)       # 3 of 5 prints: over 50%
        self.assertIn("spaghetti was not always dinner", script)  # failures > 0
        self.assertIn("midnight and 5 a.m.", script)       # print 1 started 02:30 UTC
        self.assertNotIn("worst run was", script)          # streak is 2, under the 3 gate
        self.assertNotIn("Zero failed prints", script)

    def test_zero_failure_year_has_no_failure_roast(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2024)
        script = "\n".join(wrapped["script"])
        self.assertIn("Zero failed prints", script)
        self.assertNotIn("spaghetti was not always dinner", script)
        self.assertNotIn("midnight and 5 a.m.", script)  # noon start only


class SettingsTests(unittest.TestCase):
    def test_defaults_validation_catalog_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            self.assertIs(data["timelapse"]["wrapped_enabled"], False)
            self.assertIs(data["timelapse"]["wrapped_ai_video"], False)
            env_data = load_settings(
                tmp, environ={"PRINT_HANDY__TIMELAPSE__WRAPPED_ENABLED": "true",
                              "PRINT_TIMELAPSE_WRAPPED_AI_VIDEO": "true"})
            self.assertIs(env_data["timelapse"]["wrapped_enabled"], True)
            self.assertIs(env_data["timelapse"]["wrapped_ai_video"], True)
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("timelapse:\n  wrapped_enabled: sometimes\n")
            with self.assertRaises(settings.SettingsError):
                settings.load(local=bad, environ={})
        for field_id in ("timelapse.wrapped_enabled", "timelapse.wrapped_ai_video"):
            field = catalog.field_by_id(field_id)
            self.assertIsNotNone(field)
            self.assertEqual(field.kind, "toggle")
            self.assertEqual(field.group, "timelapse")


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        target = Path(command[-1])
        if target.suffix == ".mp4":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-video")
        return SimpleNamespace(returncode=0)


class VideoTests(unittest.TestCase):
    def test_fake_ffmpeg_build_names_missing_pieces(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=runner, find_clips=lambda year: [],
                ai_call=lambda lines: (_ for _ in ()).throw(RuntimeError("no route")))
            self.assertTrue(result["ok"])
            self.assertTrue(Path(result["path"]).is_file())
            script = json.loads(Path(result["script_path"]).read_text())
            self.assertEqual(script["year"], 2025)
            self.assertTrue(script["script"])
            self.assertIn("timelapses: none found", result["notes"])
            self.assertTrue(any(n.startswith("ai_commentary: unavailable") for n in result["notes"]))
            flat = " ".join(" ".join(cmd) for cmd in runner.commands)
            self.assertIn("drawtext", flat)
            self.assertIn("concat", flat)

    def test_ai_lines_are_used_when_the_call_works(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped", ffmpeg="ffmpeg",
                runner=FakeRunner(), find_clips=lambda year: [],
                ai_call=lambda lines: ["A roastier line."])
            self.assertTrue(result["ok"])
            script = json.loads(Path(result["script_path"]).read_text())
            self.assertEqual(script["script"], ["A roastier line."])
            self.assertFalse(any("ai_commentary" in n for n in result["notes"]))

    def test_no_ffmpeg_is_a_named_refusal_with_sidecar(self):
        wrapped = print_stats.build_wrapped(RECORDS, 2025)
        with tempfile.TemporaryDirectory() as tmp:
            result = wrapped_video.build_wrapped_video(
                2025, wrapped, Path(tmp) / "wrapped",
                ffmpeg="wrapped-no-such-ffmpeg", runner=FakeRunner(),
                find_clips=lambda year: [], ai_call=None, data=None)
            self.assertFalse(result["ok"])
            self.assertIn("ffmpeg", result["reason"])
            self.assertTrue(Path(result["script_path"]).is_file())
            self.assertTrue(any("ffmpeg: unavailable" in n for n in result["notes"]))

    def test_drawtext_escaping(self):
        self.assertEqual(wrapped_video.escape_drawtext("a:b'c%"), "a\\:b\\'c\\%")


class RouteTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN} if environ is None else environ
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: FAKE_SNAPSHOT)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd

    def request(self, port, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body,
                         headers=headers or {server.TOKEN_HEADER: TOKEN})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def test_disabled_refusals_and_no_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp)
            port = httpd.server_address[1]
            for path in ("/data", "/wrapped", "/api/data", "/api/wrapped"):
                code, raw = self.request(port, "GET", path)
                self.assertEqual(code, 403, path)
                self.assertIn("wrapped_enabled", raw)
            code, raw = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertNotIn('href="/data"', raw)
            self.assertNotIn('href="/wrapped"', raw)
        self.assertNotIn("/data", [target for target, _label in shell.NAV])
        code, payload = data_module.data_view({"timelapse": {}}, records_vault="/nope")
        self.assertEqual(code, 403)

    def test_enabled_pages_api_and_nav(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n"
                                    "  wrapped_ai_video: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/api/data?year=2025")
            self.assertEqual(code, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["total_prints"], 5)
            self.assertEqual(payload["skipped_records"], ["print-9999.json"])
            code, raw = self.request(port, "GET", "/data")
            self.assertEqual(code, 200)
            self.assertIn("Black", raw)
            self.assertIn('href="/data"', raw)
            code, raw = self.request(port, "GET", "/api/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(raw)["script"])
            code, raw = self.request(port, "GET", "/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertIn("Make my Wrapped video", raw)
            code, raw = self.request(port, "GET", "/")
            self.assertEqual(code, 200)
            self.assertIn('href="/wrapped"', raw)

    def test_wrapped_page_names_off_video_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/wrapped?year=2025")
            self.assertEqual(code, 200)
            self.assertIn("wrapped_ai_video", raw)
            self.assertNotIn("Make my Wrapped video", raw)

    def test_video_post_requires_form_token_and_ai_toggle(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            body = "_csrf=" + httpd.form_token + "&year=2025"
            code, raw = self.request(
                port, "POST", "/wrapped/video", body=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)
            self.assertIn("wrapped_ai_video", raw)


if __name__ == "__main__":
    unittest.main()
