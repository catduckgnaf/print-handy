"""#1321: the Pasta Wrapped share cards are redacted by default.

A default export draws its 9:16 and 1:1 cards (and the link preview)
with no print title and no printer name. That is checked at every level
a name could leak through: the exact strings handed to Pillow's
``ImageDraw.text`` while the cards render, the PNG chunks (no text
metadata at all), the raw PNG bytes, the bundle's page, the
``manifest.json`` and the exports registry. An explicit opt-in puts the
names back, on the cards too, and the choice is recorded per share.
"""
import http.client
import json
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from print_handy import settings
from print_handy.core import print_stats
from print_handy.webui import data as data_module, server
from print_handy.webui import share_export

TITLE = "Zanzibar Bracket"
OTHER_TITLE = "Quokka Hinge"
PRINTER = "Basement Voron"
PROFILE = "Voron Profile Q"
NAMES = (TITLE, OTHER_TITLE, PRINTER, PROFILE)


def _record(number, title, day):
    return {"print_number": number, "status": "completed", "success": True,
            "title": title, "started_at": f"2025-04-{day:02d}T10:00:00Z",
            "completed_at": f"2025-04-{day:02d}T12:00:00Z",
            "printer": PRINTER, "printer_profile": PROFILE,
            "gcode_file": f"{title.lower().replace(' ', '_')}.gcode",
            "filament_material": "PLA", "filament_color": "Teal",
            "filament_used_g": 40.0, "print_duration_seconds": 7200}


RECORDS = ([_record(n, TITLE, n) for n in range(1, 5)]
           + [_record(5, OTHER_TITLE, 9)])


def _payload():
    payload = print_stats.build_wrapped(RECORDS, 2025)
    # The fixture is only useful if the payload really carries the names.
    flat = json.dumps(payload)
    for name in (TITLE, PRINTER):
        assert name in flat, name
    return payload


def _png_chunks(path):
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunks.append((kind.decode("ascii"), data[pos + 8:pos + 8 + length]))
        pos += 12 + length
    return chunks


def _export_recording_draws(payload, out, **kwargs):
    """Run one export and return (result, every string drawn on a card)."""
    drawn = []
    real_text = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *args, **kw):
        drawn.append(str(text))
        return real_text(self, xy, text, *args, **kw)

    with mock.patch.object(ImageDraw.ImageDraw, "text", spy):
        result = share_export.export_bundle(payload, out, **kwargs)
    return result, drawn


class DefaultShareCardsCarryNoNames(unittest.TestCase):
    def test_default_cards_draw_no_title_or_printer_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, drawn = _export_recording_draws(_payload(), Path(tmp) / "w")
            self.assertTrue(result["ok"], result)
            self.assertTrue(drawn, "no card text was drawn")
            joined = "\n".join(drawn)
            for name in NAMES:
                self.assertNotIn(name, joined)
            # The safe card still says something worth posting.
            self.assertIn("PASTA WRAPPED 2025", joined)
            self.assertIn("Top color Teal", joined)

    def test_default_bundle_has_story_and_square_cards_at_named_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(_payload(), Path(tmp) / "w")
            folder = Path(result["path"])
            for shape, size in (("story", (1080, 1920)), ("square", (1080, 1080))):
                (expected_size, name) = share_export.CARD_SHAPES[shape]
                self.assertEqual(expected_size, size)
                with Image.open(folder / name) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertEqual(image.size, size)
            shapes = {entry.get("shape"): entry for entry in result["manifest"]["files"]
                      if entry["kind"] == "share-card"}
            self.assertEqual(set(shapes), {"story", "square", "landscape"})
            self.assertEqual(shapes["story"]["size"], [1080, 1920])
            self.assertEqual(shapes["square"]["size"], [1080, 1080])

    def test_default_card_pngs_carry_no_text_metadata_and_no_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(_payload(), Path(tmp) / "w")
            folder = Path(result["path"])
            for (_size, name) in share_export.CARD_SHAPES.values():
                path = folder / name
                kinds = [kind for kind, _body in _png_chunks(path)]
                for text_chunk in ("tEXt", "zTXt", "iTXt", "eXIf"):
                    self.assertNotIn(text_chunk, kinds, name)
                with Image.open(path) as image:
                    self.assertEqual(
                        {k: v for k, v in image.info.items()
                         if isinstance(v, (str, bytes))}, {}, name)
                raw = path.read_bytes()
                pixels = zlib.decompress(b"".join(
                    body for kind, body in _png_chunks(path) if kind == "IDAT"))
                for needle in NAMES:
                    self.assertNotIn(needle.encode(), raw)
                    self.assertNotIn(needle.encode(), pixels)

    def test_default_page_manifest_and_registry_carry_no_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "w"
            result = share_export.export_bundle(_payload(), out,
                                                destination_note="my static host")
            folder = Path(result["path"])
            texts = {"index.html": (folder / "index.html").read_text(encoding="utf-8"),
                     "manifest.json": (folder / "manifest.json").read_text(encoding="utf-8"),
                     "exports.json": (out / "exports.json").read_text(encoding="utf-8")}
            for label, text in texts.items():
                for name in NAMES:
                    self.assertNotIn(name, text, label)
            manifest = json.loads(texts["manifest.json"])
            self.assertFalse(manifest["titles_included"])
            self.assertFalse(manifest["printer_names_included"])

    def test_card_lines_drop_a_withheld_name_that_slipped_past_redaction(self):
        # Second guard: even an unredacted payload renders no withheld name.
        raw = _payload()
        withheld = share_export.withheld_names(raw)
        self.assertIn(TITLE, withheld)
        self.assertIn(PRINTER, withheld)
        lines = " ".join(text for _role, text in
                         share_export.share_card_lines(raw, withheld=withheld))
        for name in (TITLE, PRINTER):
            self.assertNotIn(name, lines)


class OptInRestoresNamesAndIsRecorded(unittest.TestCase):
    def test_opt_in_puts_the_names_on_the_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, drawn = _export_recording_draws(
                _payload(), Path(tmp) / "w", include_titles=True, include_printers=True)
            joined = "\n".join(drawn)
            self.assertIn(f"Busiest printer {PRINTER}", joined)
            self.assertIn(f"Most reprinted {TITLE}", joined)
            page = (Path(result["path"]) / "index.html").read_text(encoding="utf-8")
            self.assertIn(TITLE, page)
            self.assertIn(PRINTER, page)

    def test_each_category_opts_in_on_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            _result, drawn = _export_recording_draws(
                _payload(), Path(tmp) / "w", include_printers=True)
            joined = "\n".join(drawn)
            self.assertIn(PRINTER, joined)
            self.assertNotIn(TITLE, joined)

    def test_the_choice_is_recorded_per_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "w"
            safe = share_export.export_bundle(_payload(), out)
            named = share_export.export_bundle(_payload(), out, include_titles=True,
                                               include_printers=True)
            entries = {e["folder"]: e for e in share_export.load_exports(out)[0]}
            self.assertFalse(entries[safe["folder"]]["titles_included"])
            self.assertFalse(entries[safe["folder"]]["printer_names_included"])
            self.assertTrue(entries[named["folder"]]["titles_included"])
            self.assertTrue(entries[named["folder"]]["printer_names_included"])
            self.assertTrue(named["manifest"]["titles_included"])
            self.assertIn("you opted in", " ".join(named["manifest"]["notes"]))


class WebUiDefaults(unittest.TestCase):
    def test_share_form_leaves_both_boxes_unticked_and_says_so(self):
        view = {"ok": True, **_payload(), "ai_video_enabled": False,
                "video_available": False, "share_exports": [],
                "share_registry_note": None,
                "share_preview": share_export.preview(_payload())}
        page = data_module.render_wrapped_page({}, view, form_token="t")
        self.assertIn('name="include_titles" value="1">', page)
        self.assertIn('name="include_printers" value="1">', page)
        self.assertNotIn('name="include_titles" value="1" checked', page)
        self.assertNotIn('name="include_printers" value="1" checked', page)
        self.assertIn("left out by default", page)

    def test_data_layer_default_export_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = _write_vault(tmp)
            data = {"timelapse": {"wrapped_enabled": True},
                    "paths": {"records_vault": str(vault)}}
            code, body = data_module.export_share(data, 2025, records_vault=vault)
            self.assertEqual(code, 200, body)
            self.assertFalse(body["manifest"]["titles_included"])
            self.assertFalse(body["manifest"]["printer_names_included"])
            page = (Path(body["path"]) / "index.html").read_text(encoding="utf-8")
            for name in NAMES:
                self.assertNotIn(name, page)


TOKEN = "share-card-redaction-token"


def _write_vault(tmp):
    prints = Path(tmp) / "vault" / "Prints"
    prints.mkdir(parents=True)
    for index, record in enumerate(RECORDS, start=1):
        (prints / f"print-{index:04d}.json").write_text(json.dumps(record))
    return prints.parent


class ShareRouteDefaults(unittest.TestCase):
    """POST /wrapped/share: only a ticked box on the explicit form opts in."""

    def start(self, tmp):
        _write_vault(tmp)
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                         f"  records_vault: {tmp}/vault\n"
                         "webui:\n  enabled: true\n  port: 8765\n"
                         "timelapse:\n  wrapped_enabled: true\n")
        data = settings.load(local=local, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
        httpd = server.make_server(data, environ={"WEBUI_ACCESS_TOKEN": TOKEN}, port=0,
                                   snapshot_func=lambda: {"ok": True, "doctor": []})
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd

    def post(self, httpd, fields):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        try:
            body = "&".join([f"_csrf={httpd.form_token}", "year=2025",
                             "visibility=public", *fields])
            conn.request("POST", "/wrapped/share", body=body.encode(),
                         headers={"Content-Type": "application/x-www-form-urlencoded",
                                  server.TOKEN_HEADER: TOKEN})
            response = conn.getresponse()
            return response.status, response.read().decode()
        finally:
            conn.close()

    def manifest(self, tmp):
        path = Path(tmp) / "vault" / "wrapped" / "exports" / "wrapped-2025" / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_a_post_without_the_options_marker_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, raw = self.post(self.start(tmp), [])
            self.assertEqual(code, 200, raw[:400])
            manifest = self.manifest(tmp)
            self.assertFalse(manifest["titles_included"])
            self.assertFalse(manifest["printer_names_included"])
            self.assertIn("Print titles: left out", raw)

    def test_the_explicit_form_with_no_box_ticked_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _raw = self.post(self.start(tmp), ["options=explicit"])
            self.assertEqual(code, 200)
            self.assertFalse(self.manifest(tmp)["titles_included"])

    def test_ticked_boxes_opt_in_and_the_result_page_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, raw = self.post(self.start(tmp), [
                "options=explicit", "include_titles=1", "include_printers=1"])
            self.assertEqual(code, 200, raw[:400])
            manifest = self.manifest(tmp)
            self.assertTrue(manifest["titles_included"])
            self.assertTrue(manifest["printer_names_included"])
            self.assertIn("Print titles: included (you opted in)", raw)


if __name__ == "__main__":
    unittest.main()
