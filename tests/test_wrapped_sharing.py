"""#981 step 3: Wrapped sharing — the static export bundle, the
visibility modes, the exports registry, and the share panel / POST.

Fixture payloads and temporary vaults only; the card renderer uses
Pillow (a runtime dependency) locally, and the route tests run a
local test server on an ephemeral port. No network, no printer, no
real static host.

Private (encrypted) exports are pinned as a NAMED REFUSAL in this
phase: the repo dependency set carries no established AES-GCM
library, so the exporter must refuse by name and write nothing.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.core import print_stats
from print_handy.webui import data as data_module, server
from print_handy.webui import share_export

TOKEN = "wrapped-sharing-token"
DISTINCTIVE_SCRIPT = "DISTINCTIVE roast line about the year 97731 prints of glory."
DECOY_TOKEN = "DECOY-TOKEN-97731"
DECOY_PATH = "/decoy/secret/vault-path"

RECORDS = [
    {"print_number": 1, "status": "completed", "success": True, "title": "Bracket",
     "started_at": "2025-03-01T02:30:00Z", "completed_at": "2025-03-01T04:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "bracket.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 100.0,
     "print_duration_seconds": 7200, "slicer_settings": {"nozzle_diameter": 0.4}},
    {"print_number": 2, "status": "completed", "success": True, "title": "Clip",
     "started_at": "2025-03-15T12:00:00Z", "completed_at": "2025-03-15T13:00:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "clip.gcode",
     "filament_vendor": "Sunlu", "filament_material": "PLA", "filament_color": "Black",
     "spoolman_spool_id": 3, "filament_used_g": 50.0,
     "print_duration_seconds": 3600, "slicer_settings": {"nozzle_diameter": 0.4}},
    {"print_number": 3, "status": "failed", "success": False, "title": "Vase",
     "started_at": "2025-07-04T18:00:00Z", "completed_at": "2025-07-04T18:30:00Z",
     "printer": "AD5X", "printer_profile": "Profile A", "gcode_file": "vase.gcode",
     "filament_vendor": "eSun", "filament_material": "PETG", "filament_color": "Red",
     "spoolman_spool_id": 7, "filament_used_g": 20.0,
     "print_duration_seconds": 1800, "slicer_settings": {"nozzle_diameter": 0.6},
     "watch_mode": "early_layer", "watch_risk": "slicer finding",
     "stop_reason": "spaghetti detected"},
]


def _payload():
    payload = print_stats.build_wrapped(RECORDS, 2025)
    payload["script"] = list(payload["script"]) + [DISTINCTIVE_SCRIPT]
    return payload


def _bundle_bytes(folder):
    return {path.name: path.read_bytes()
            for path in Path(folder).rglob("*") if path.is_file()}


class ExporterTests(unittest.TestCase):
    def test_public_bundle_shape_and_page_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            result = share_export.export_bundle(_payload(), out, visibility="public")
            self.assertTrue(result["ok"], result)
            folder = Path(result["path"])
            self.assertEqual(folder.name, "wrapped-2025")
            names = set(_bundle_bytes(folder))
            self.assertIn("index.html", names)
            self.assertIn("share-card.png", names)
            self.assertIn("share-card-vertical.png", names)
            self.assertIn("manifest.json", names)
            self.assertNotIn("robots.txt", names)
            page = (folder / "index.html").read_text(encoding="utf-8")
            markers = ["Your 2025 in printing", "By the numbers",
                       "Colors and materials", "Time patterns", "Repeats",
                       "Your printing personality", "Pasta, failures and stops",
                       "Commentary", 'class="card share-card"']
            positions = [page.index(marker) for marker in markers]
            self.assertEqual(positions, sorted(positions))
            self.assertIn(DISTINCTIVE_SCRIPT, page)
            self.assertNotIn("noindex", page)
            # Self-contained: no external assets, no Web UI routes.
            self.assertNotIn('src="http', page)
            self.assertNotIn('href="http', page)
            self.assertNotIn("/login", page)

    def test_share_card_pngs_are_real_pngs_at_named_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(
                _payload(), Path(tmp) / "wrapped", visibility="public")
            folder = Path(result["path"])
            from PIL import Image
            with Image.open(folder / "share-card.png") as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, share_export.CARD_LANDSCAPE_SIZE)
            with Image.open(folder / "share-card-vertical.png") as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, share_export.CARD_VERTICAL_SIZE)

    def test_manifest_contents_public_no_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(
                _payload(), Path(tmp) / "wrapped", visibility="public",
                destination_note="my GitHub Pages repo")
            folder = Path(result["path"])
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["year"], 2025)
            self.assertEqual(manifest["visibility"], "public")
            self.assertFalse(manifest["video_included"])
            self.assertIn("Print Handy", manifest["generator"])
            self.assertIn("locally", manifest["generator"])
            self.assertTrue(manifest["exported_at"])
            kinds = {entry["name"]: entry["kind"] for entry in manifest["files"]}
            self.assertEqual(kinds["index.html"], "page")
            self.assertEqual(kinds["share-card.png"], "share-card")
            self.assertTrue(any("left out" in note for note in manifest["notes"]))

    def test_video_copied_and_embedded_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir(parents=True)
            (out / "wrapped-2025.mp4").write_bytes(b"fake-mp4-bytes")
            result = share_export.export_bundle(_payload(), out, visibility="public",
                                                include_video=True)
            folder = Path(result["path"])
            self.assertTrue(result["video_included"])
            self.assertEqual((folder / "wrapped-2025.mp4").read_bytes(), b"fake-mp4-bytes")
            page = (folder / "index.html").read_text(encoding="utf-8")
            self.assertIn("<video", page)
            self.assertIn('src="wrapped-2025.mp4"', page)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["video_included"])

    def test_link_only_slug_noindex_and_robots(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            (out / "wrapped-2025.mp4").parent.mkdir(parents=True, exist_ok=True)
            result = share_export.export_bundle(_payload(), out, visibility="link-only")
            self.assertTrue(result["ok"], result)
            folder = Path(result["path"])
            self.assertNotEqual(folder.name, "wrapped-2025")
            self.assertRegex(folder.name, r"^[A-Za-z0-9_-]{20,}$")
            page = (folder / "index.html").read_text(encoding="utf-8")
            self.assertIn("noindex", page)
            robots = (folder / "robots.txt").read_text(encoding="utf-8")
            self.assertIn("Disallow: /", robots)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["visibility"], "link-only")
            self.assertTrue(any("obscurity" in note for note in manifest["notes"]))

    def test_private_is_a_named_refusal_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            result = share_export.export_bundle(
                _payload(), out, visibility="private", passphrase="hunter2")
            self.assertFalse(result["ok"])
            self.assertIn("AES-GCM", result["reason"])
            self.assertIn("cryptography", result["reason"])
            self.assertFalse(share_export.private_available())
            # No bundle anywhere: nothing plaintext was written.
            written = [p for p in Path(tmp).rglob("*") if p.is_file()]
            self.assertEqual(written, [])

    def test_unknown_visibility_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(
                _payload(), Path(tmp) / "wrapped", visibility="friends")
            self.assertFalse(result["ok"])
            self.assertIn("visibility", result["reason"])

    def test_decoy_fields_appear_nowhere_in_the_bundle(self):
        payload = _payload()
        payload["operator_settings"] = {"access_token": DECOY_TOKEN,
                                        "records_vault": DECOY_PATH}
        payload["highlights"]["operator_token"] = DECOY_TOKEN
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(
                payload, Path(tmp) / "wrapped", visibility="public")
            self.assertTrue(result["ok"], result)
            for name, blob in _bundle_bytes(result["path"]).items():
                text = blob.decode("utf-8", errors="ignore")
                self.assertNotIn(DECOY_TOKEN, text, name)
                self.assertNotIn(DECOY_PATH, text, name)
                self.assertNotIn("operator_settings", text, name)


class RegistryTests(unittest.TestCase):
    def test_append_list_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            share_export.export_bundle(_payload(), out, visibility="public",
                                       destination_note="GitHub Pages")
            share_export.export_bundle(_payload(), out, visibility="link-only",
                                       destination_note="Cloudflare Pages")
            entries, note = share_export.load_exports(out)
            self.assertIsNone(note)
            self.assertEqual(len(entries), 2)
            first, second = entries
            self.assertEqual(first["folder"], "wrapped-2025")
            self.assertEqual(first["year"], 2025)
            self.assertEqual(first["visibility"], "public")
            self.assertEqual(first["destination"], "GitHub Pages")
            self.assertFalse(first["video_included"])
            self.assertTrue(first["exported_at"])
            self.assertEqual(second["visibility"], "link-only")
            self.assertEqual(second["destination"], "Cloudflare Pages")

    def test_corrupt_registry_fails_soft_and_exports_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir(parents=True)
            (out / "exports.json").write_text("{not json", encoding="utf-8")
            entries, note = share_export.load_exports(out)
            self.assertEqual(entries, [])
            self.assertIn("exports.json", note)
            result = share_export.export_bundle(_payload(), out, visibility="public")
            self.assertTrue(result["ok"], result)
            self.assertIn("exports.json", result["registry_note"])


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


def _write_vault(tmp):
    vault = Path(tmp) / "vault"
    prints = vault / "Prints"
    prints.mkdir(parents=True)
    for index, record in enumerate(RECORDS, start=1):
        (prints / f"print-{index:04d}.json").write_text(json.dumps(record))
    return vault


def _load_settings(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     f"  records_vault: {tmp}/vault\n{extra}")
    return settings.load(local=local, environ={"WEBUI_ACCESS_TOKEN": TOKEN})


class SharePanelAndRouteTests(unittest.TestCase):
    def test_export_share_gated_by_wrapped_enabled(self):
        code, body = data_module.export_share(
            {"timelapse": {"wrapped_enabled": False}}, 2025, "public",
            records_vault="/nope")
        self.assertEqual(code, 403)
        self.assertIn("wrapped_enabled", body["reason"])

    def test_export_share_end_to_end_via_data_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _write_vault(tmp)
            data = {"timelapse": {"wrapped_enabled": True},
                    "paths": {"records_vault": str(vault)}}
            code, body = data_module.export_share(
                data, 2025, "link-only", records_vault=vault,
                destination_note="Cloudflare Pages")
            self.assertEqual(code, 200, body)
            self.assertTrue(body["ok"])
            folder = Path(body["path"])
            self.assertTrue((folder / "index.html").is_file())
            entries, note = share_export.load_exports(vault / "wrapped")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["destination"], "Cloudflare Pages")

    def test_wrapped_page_carries_share_panel_and_past_exports(self):
        payload = {"ok": True, **_payload(), "ai_video_enabled": False,
                   "share_exports": [{"folder": "wrapped-2025", "year": 2025,
                                      "visibility": "public",
                                      "destination": "GitHub Pages",
                                      "exported_at": "2026-11-07T00:00:00Z",
                                      "video_included": False}],
                   "share_registry_note": None}
        data = {"timelapse": {"wrapped_enabled": True}}
        page = data_module.render_wrapped_page(data, payload, form_token="tok")
        self.assertIn("Share this Wrapped", page)
        self.assertIn('action="/wrapped/share"', page)
        self.assertIn("GitHub Pages", page)
        self.assertIn("Cloudflare Pages", page)
        self.assertIn("obscurity", page)
        self.assertIn("AES-GCM", page)
        self.assertIn("GitHub Pages", page.split("What you have shared")[1])

    def start(self, tmp, extra=""):
        data = _load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra)
        httpd = server.make_server(data, environ={"WEBUI_ACCESS_TOKEN": TOKEN},
                                   port=0, snapshot_func=lambda: FAKE_SNAPSHOT)
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

    def test_share_post_route_creates_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            body = ("_csrf=" + httpd.form_token +
                    "&year=2025&visibility=public&destination=GitHub+Pages")
            code, raw = self.request(
                port, "POST", "/wrapped/share", body=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 200, raw[:400])
            self.assertIn("wrapped-2025", raw)
            self.assertTrue((vault / "wrapped" / "exports" /
                             "wrapped-2025" / "index.html").is_file())

    def test_share_post_refused_when_wrapped_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: false\n")
            port = httpd.server_address[1]
            body = "_csrf=" + httpd.form_token + "&year=2025&visibility=public"
            code, raw = self.request(
                port, "POST", "/wrapped/share", body=body.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)
            self.assertIn("wrapped_enabled", raw)

    def test_share_post_requires_form_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_vault(tmp)
            httpd = self.start(tmp, "timelapse:\n  wrapped_enabled: true\n")
            port = httpd.server_address[1]
            code, raw = self.request(
                port, "POST", "/wrapped/share",
                body=b"year=2025&visibility=public",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
