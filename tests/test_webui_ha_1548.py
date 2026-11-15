"""#1548: the Home Assistant API additions.

Three contracts, all offline with fake reads and a temp state folder:

- ``POST /api/upload`` takes one multipart file under the
  ``X-WebUI-Token`` header alone and lands it in the Web UI inbox
  under the upload page's own intake rules, answering in the API
  JSON shape;
- ``GET /api/printers/<id>/status`` carries a ``hardware`` object
  with the dashboard's nozzle and bed facts, fail-soft;
- ``POST /api/printers/<id>/nozzle`` records a nozzle change through
  the guided flow's record step, behind the controls gate and the
  header token, and sends nothing to the printer.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.core import intake_resources, printer_state
from print_handy.runtime import nozzle_change as nc
from print_handy.webui import controls, server, status, upload

TOKEN = "test-token-1548"
PRINTER = "flashforge-ad5x"

RECORD = {"nozzle": {"diameter_mm": 0.6, "type": "hardened_steel",
                     "installed": "2026-11-10"},
          "z_offset_mm": -0.125, "z_offset_verified": True,
          "calibration_pending": ["z_offset"],
          "bed_mesh_stale": True}


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def enabled(tmp, extra=""):
    return load_settings(tmp, "webui:\n  controls_enabled: true\n" + extra)


def multipart_body(filename, content, field_name="file"):
    boundary = "----ph-1548-test"
    chunks = []
    if filename is not None:
        chunks.append((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
            "Content-Type: model/stl\r\n\r\n").encode() + content + b"\r\n")
    else:
        chunks.append((
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="note"\r\n\r\n'
            "hello\r\n").encode())
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class HardwareStatusTests(unittest.TestCase):
    def test_status_carries_the_dashboard_hardware_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            code, body = status.printer_status(
                data, PRINTER,
                read=lambda adapter, view: {"state": "idle"},
                hardware_for=lambda printer_id: RECORD)
        self.assertEqual(code, 200)
        facts = body["hardware"]
        self.assertEqual(facts["diameter_mm"], 0.6)
        self.assertEqual(facts["type"], "hardened_steel")
        self.assertEqual(facts["installed"], "2026-11-10")
        self.assertEqual(facts["z_offset_mm"], -0.125)
        self.assertTrue(facts["z_offset_verified"])
        self.assertEqual(facts["temp_offset_c"], 5.0)
        self.assertIn("max_c", facts)
        self.assertEqual(facts["pending"], ["z_offset"])
        self.assertTrue(facts["mesh_stale"])
        families = {row["family"] for row in facts["materials"]}
        self.assertIn("PLA", families)
        pla = next(row for row in facts["materials"] if row["family"] == "PLA")
        self.assertEqual(pla["nozzle_c"], pla["base_nozzle_c"] + 5.0)
        self.assertIn("bed_c", pla)

    def test_status_without_a_record_falls_back_to_settings_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            code, body = status.printer_status(
                data, PRINTER,
                read=lambda adapter, view: {"state": "idle"},
                hardware_for=lambda printer_id: None)
        self.assertEqual(code, 200)
        self.assertEqual(body["hardware"]["source"], "settings")
        self.assertEqual(body["hardware"]["diameter_mm"], 0.4)

    def test_a_failing_hardware_read_omits_the_key_and_keeps_status(self):
        def boom(printer_id):
            raise RuntimeError("record exploded")
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            code, body = status.printer_status(
                data, PRINTER,
                read=lambda adapter, view: {"state": "printing"},
                hardware_for=boom)
        self.assertEqual(code, 200)
        self.assertNotIn("hardware", body)
        self.assertEqual(body["status"]["state"], "printing")

    def test_a_failing_facts_build_omits_the_key(self):
        def boom(view, record):
            raise RuntimeError("facts exploded")
        with tempfile.TemporaryDirectory() as tmp:
            data = load_settings(tmp)
            code, body = status.printer_status(
                data, PRINTER,
                read=lambda adapter, view: {"state": "idle"},
                hardware_for=lambda printer_id: RECORD,
                hardware_facts_func=boom)
        self.assertEqual(code, 200)
        self.assertNotIn("hardware", body)


class NozzleRecordUnitTests(unittest.TestCase):
    def fake_record(self, calls):
        def record(printer_id, view, diameter_mm, nozzle_type,
                   supported_sizes, supported_types):
            calls.append((printer_id, diameter_mm, nozzle_type,
                          supported_sizes, supported_types))
            state = {"nozzle": {"diameter_mm": 0.4, "type": "brass"}}
            return nc.record_nozzle_change(
                state, diameter_mm, nozzle_type,
                supported_sizes=supported_sizes,
                supported_types=supported_types, today="2026-11-15")
        return record

    def test_record_success_normalizes_and_reports(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.record_nozzle(
                enabled(tmp), PRINTER,
                {"diameter_mm": 0.6, "type": "hardened steel"},
                record=self.fake_record(calls))
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["diameter_mm"], 0.6)
        self.assertEqual(body["type"], "hardened_steel")
        self.assertEqual(calls[0][0], PRINTER)

    def test_controls_off_is_403_and_records_nothing(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.record_nozzle(
                load_settings(tmp), PRINTER,
                {"diameter_mm": 0.6, "type": "brass"},
                record=self.fake_record(calls))
        self.assertEqual(code, 403)
        self.assertIn("controls_enabled", body["reason"])
        self.assertEqual(calls, [])

    def test_unknown_printer_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, body = controls.record_nozzle(
                enabled(tmp), "nope",
                {"diameter_mm": 0.6, "type": "brass"},
                record=self.fake_record([]))
        self.assertEqual(code, 404)
        self.assertFalse(body["ok"])

    def test_validation_refusals_record_nothing(self):
        calls = []
        bad_bodies = [
            None, {}, {"type": "brass"},
            {"diameter_mm": "0.6", "type": "brass"},
            {"diameter_mm": True, "type": "brass"},
            {"diameter_mm": 0.6},
            {"diameter_mm": 0.6, "type": ""},
            {"diameter_mm": 0.6, "type": 7},
            {"diameter_mm": 0.3, "type": "brass"},
            {"diameter_mm": 0.4, "type": "unobtanium"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            for body in bad_bodies:
                code, out = controls.record_nozzle(
                    data, PRINTER, body, record=self.fake_record(calls))
                self.assertEqual(code, 400, body)
                self.assertFalse(out["ok"])
                self.assertIn("reason", out)
        # The unsupported diameter/type cases reach the record step,
        # whose own validation refuses before any mutation; the shape
        # refusals above never reach it at all.
        self.assertEqual(len(calls), 2)

    def test_supported_sets_come_from_the_adapter(self):
        calls = []

        class Adapter:
            supported_nozzle_sizes = (0.4,)
            supported_nozzle_types = ("brass",)

        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            code, _ = controls.record_nozzle(
                data, PRINTER, {"diameter_mm": 0.6, "type": "brass"},
                adapter=Adapter(), record=self.fake_record(calls))
            self.assertEqual(code, 400)  # 0.6 not in this printer's sizes
            code, body = controls.record_nozzle(
                data, PRINTER, {"diameter_mm": 0.4, "type": "brass"},
                adapter=Adapter(), record=self.fake_record(calls))
            self.assertEqual(code, 200)

    def test_the_default_record_writes_the_hardware_record(self):
        # The production path: a real record file on disk, written
        # through printer_state + record_nozzle_change, with the
        # calibration invalidation the guided flow applies.
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            state_file = Path(tmp) / "printer_hardware.json"
            seed = {"nozzle": {"diameter_mm": 0.4, "type": "brass",
                               "installed": "2026-01-01"},
                    "z_offset_mm": 1.23, "z_offset_verified": True,
                    "pressure_advance": {"value": 0.04}}
            target = (Path(tmp) / "printers" / PRINTER
                      / "printer_hardware.json")
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(seed))
            with mock.patch.object(printer_state, "STATE", str(state_file)):
                code, body = controls.record_nozzle(
                    data, PRINTER,
                    {"diameter_mm": 0.6, "type": "hardened_steel"})
            self.assertEqual(code, 200, body)
            saved = json.loads(target.read_text())
        self.assertEqual(saved["nozzle"]["diameter_mm"], 0.6)
        self.assertEqual(saved["nozzle"]["type"], "hardened_steel")
        self.assertIsNone(saved["z_offset_mm"])
        self.assertFalse(saved["z_offset_verified"])
        self.assertTrue(saved["bed_mesh_stale"])
        self.assertEqual(saved["calibration_pending"],
                         ["z_offset", "bed_mesh", "pressure_advance_flow"])
        self.assertTrue(saved["pressure_advance"]["stale"])
        self.assertEqual(saved["nozzle_change"]["previous"],
                         {"diameter_mm": 0.4, "type": "brass"})

    def test_no_printer_contact_on_the_record_path(self):
        # The record step must not heat, move or otherwise contact the
        # printer: the default send path and the Klipper sync are the
        # two printer-touching seams near it, and neither may run.
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            data = enabled(tmp)
            with mock.patch.object(controls, "default_send") as send, \
                    mock.patch.object(printer_state, "sync_klipper_nozzle") as sync:
                code, _ = controls.record_nozzle(
                    data, PRINTER, {"diameter_mm": 0.4, "type": "hardened_steel"},
                    record=self.fake_record(calls))
        self.assertEqual(code, 200)
        send.assert_not_called()
        sync.assert_not_called()


class ApiRouteTests(unittest.TestCase):
    """The three contracts over real HTTP on a loopback server."""

    def start(self, tmp, extra="", printer_control_func=None):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
        data = load_settings(
            tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
            environ=environ)
        httpd = server.make_server(
            data, environ=environ, port=0, snapshot_func=lambda: {},
            printer_control_func=printer_control_func)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def post_json(self, port, path, payload, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[server.TOKEN_HEADER] = token
        return self.request(port, "POST", path,
                            json.dumps(payload).encode(), headers)

    # -- upload ------------------------------------------------------

    def test_upload_with_header_token_lands_in_the_inbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body("benchy.stl", b"solid x\n")
            code, raw = self.request(
                port, "POST", "/api/upload", body,
                {"Content-Type": content_type, server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200, raw)
            payload = json.loads(raw)
            self.assertEqual(payload, {"filename": "benchy.stl", "ok": True})
            rows = upload.list_inbox(httpd.settings_data)
            self.assertEqual([row[0] for row in rows], ["benchy.stl"])
            saved = upload.inbox_dir(httpd.settings_data) / "benchy.stl"
            self.assertEqual(saved.read_bytes(), b"solid x\n")

    def test_upload_without_or_with_a_wrong_token_is_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body("a.stl", b"solid x\n")
            for headers in ({"Content-Type": content_type},
                            {"Content-Type": content_type,
                             server.TOKEN_HEADER: "wrong"}):
                code, raw = self.request(port, "POST", "/api/upload", body, headers)
                self.assertEqual(code, 401, raw)
                self.assertFalse(json.loads(raw)["ok"])
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_upload_session_cookie_alone_does_not_authorize(self):
        # The header policy: a login session is not the API credential.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body("a.stl", b"solid x\n")
            code, _ = self.request(
                port, "POST", "/api/upload", body,
                {"Content-Type": content_type, "Cookie": "ph-session=nope"})
            self.assertEqual(code, 401)
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_upload_refusals_are_json_with_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            auth = {"Content-Type": None, server.TOKEN_HEADER: TOKEN}
            # A bad extension.
            body, content_type = multipart_body("notes.txt", b"hi")
            code, raw = self.request(port, "POST", "/api/upload", body,
                                     {**auth, "Content-Type": content_type})
            self.assertEqual(code, 400)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertIn(".stl", payload["reason"])
            # No file part at all.
            body, content_type = multipart_body(None, b"")
            code, raw = self.request(port, "POST", "/api/upload", body,
                                     {**auth, "Content-Type": content_type})
            self.assertEqual(code, 400)
            self.assertIn("no file", json.loads(raw)["reason"])
            # An empty file.
            body, content_type = multipart_body("a.stl", b"")
            code, raw = self.request(port, "POST", "/api/upload", body,
                                     {**auth, "Content-Type": content_type})
            self.assertEqual(code, 400)
            self.assertIn("empty", json.loads(raw)["reason"])
            # A path in the name is cleaned, never followed.
            body, content_type = multipart_body("../../evil.stl", b"solid\n")
            code, raw = self.request(port, "POST", "/api/upload", body,
                                     {**auth, "Content-Type": content_type})
            self.assertEqual(code, 200, raw)
            self.assertEqual(json.loads(raw)["filename"], "evil.stl")
            rows = [row[0] for row in upload.list_inbox(httpd.settings_data)]
            self.assertEqual(rows, ["evil.stl"])

    def test_upload_stl_over_the_intake_limit_is_refused(self):
        # The intake limit is patched small so the route test stays
        # fast; the real 32 MiB limit is pinned by the upload tests
        # against save_upload directly.
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body("big.stl", b"x" * 64)
            with mock.patch.object(intake_resources, "MAX_STL_BYTES", 8):
                code, raw = self.request(
                    port, "POST", "/api/upload", body,
                    {"Content-Type": content_type, server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 400, raw[:200])
            self.assertIn("intake limit", json.loads(raw)["reason"])
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_upload_inbox_quota_is_refused_with_507(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body("a.stl", b"solid x\n")
            with mock.patch.object(upload, "INBOX_QUOTA_BYTES", 1):
                code, raw = self.request(
                    port, "POST", "/api/upload", body,
                    {"Content-Type": content_type, server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 507, raw)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])
            self.assertIn(upload.INBOX_FULL, payload["reason"])
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_upload_non_multipart_is_415_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            code, raw = self.request(
                port, "POST", "/api/upload", b"plain",
                {"Content-Type": "text/plain", server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 415)
            self.assertFalse(json.loads(raw)["ok"])

    # -- status hardware over HTTP ------------------------------------

    def test_status_route_carries_hardware_with_the_real_wiring(self):
        # No printer_status_func: the route runs status.printer_status
        # on the loaded settings. No hardware record exists in the
        # temp state folder, so the facts fall back to settings, and
        # the unreachable printer still answers 200 unavailable.
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp)
            code, raw = self.request(
                port, "GET", f"/api/printers/{PRINTER}/status",
                headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200, raw)
            payload = json.loads(raw)
            self.assertEqual(payload["status"]["state"], "unavailable")
            self.assertEqual(payload["hardware"]["diameter_mm"], 0.4)
            self.assertEqual(payload["hardware"]["source"], "settings")
            self.assertIn("materials", payload["hardware"])

    # -- nozzle over HTTP ----------------------------------------------

    def test_nozzle_route_records_through_the_control_dispatch(self):
        seen = []

        def fake_control(printer_id, verb, body):
            seen.append((printer_id, verb, body))
            if verb == "nozzle":
                return 200, {"ok": True, "id": printer_id, "action": "nozzle"}
            return 404, {"ok": False, "reason": "nope"}

        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, printer_control_func=fake_control)
            code, raw = self.post_json(
                port, f"/api/printers/{PRINTER}/nozzle",
                {"diameter_mm": 0.6, "type": "hardened_steel"})
            self.assertEqual(code, 200, raw)
            self.assertEqual(seen, [(PRINTER, "nozzle",
                                     {"diameter_mm": 0.6,
                                      "type": "hardened_steel"})])
            # Without the header token the route refuses before dispatch.
            code, raw = self.post_json(
                port, f"/api/printers/{PRINTER}/nozzle",
                {"diameter_mm": 0.6, "type": "hardened_steel"}, token=None)
            self.assertEqual(code, 401)
            self.assertEqual(len(seen), 1)

    def test_nozzle_route_with_the_real_wiring_and_controls_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp)
            code, raw = self.post_json(
                port, f"/api/printers/{PRINTER}/nozzle",
                {"diameter_mm": 0.6, "type": "hardened_steel"})
            self.assertEqual(code, 403, raw)
            self.assertIn("controls_enabled", json.loads(raw)["reason"])

    def test_nozzle_route_records_and_status_then_shows_it(self):
        # End to end: controls on, a seeded record on disk, the POST
        # records the change, and the status hardware facts then show
        # the new nozzle.
        with tempfile.TemporaryDirectory() as tmp:
            _httpd, port = self.start(tmp, extra="  controls_enabled: true\n")
            state_file = Path(tmp) / "printer_hardware.json"
            target = (Path(tmp) / "printers" / PRINTER
                      / "printer_hardware.json")
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps(
                {"nozzle": {"diameter_mm": 0.4, "type": "brass",
                            "installed": "2026-01-01"}}))
            with mock.patch.object(printer_state, "STATE", str(state_file)):
                code, raw = self.post_json(
                    port, f"/api/printers/{PRINTER}/nozzle",
                    {"diameter_mm": 0.6, "type": "hardened steel"})
                self.assertEqual(code, 200, raw)
                payload = json.loads(raw)
                self.assertEqual(payload["diameter_mm"], 0.6)
                self.assertEqual(payload["type"], "hardened_steel")
                code, raw = self.request(
                    port, "GET", f"/api/printers/{PRINTER}/status",
                    headers={server.TOKEN_HEADER: TOKEN})
                self.assertEqual(code, 200, raw)
                hardware = json.loads(raw)["hardware"]
            self.assertEqual(hardware["diameter_mm"], 0.6)
            self.assertEqual(hardware["type"], "hardened_steel")
            saved = json.loads(target.read_text())
            self.assertEqual(saved["nozzle"]["diameter_mm"], 0.6)


if __name__ == "__main__":
    unittest.main()
