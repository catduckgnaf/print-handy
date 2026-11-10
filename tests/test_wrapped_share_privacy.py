"""What a Wrapped share bundle contains, and what it leaves behind.

Link-only is the default, the export shows what it will include, titles,
printer names and camera footage are each optional (footage off by
default), a path is never a title, and a re-export removes files it no
longer references.
"""
import tempfile
import unittest
from pathlib import Path

from print_handy.core import print_stats
from print_handy.webui import data as data_module
from print_handy.webui import share_export

RECORDS = [
    {"print_number": 1, "status": "completed", "title": "Secret Bracket",
     "started_at": "2025-03-01T10:00:00Z", "printer": "Garage AD5X",
     "gcode_file": "bracket.gcode", "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 10.0, "print_duration_seconds": 7200},
    {"print_number": 2, "status": "completed", "title": "Secret Bracket",
     "started_at": "2025-03-02T10:00:00Z", "printer": "Garage AD5X",
     "gcode_file": "bracket.gcode", "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 10.0, "print_duration_seconds": 3600},
    {"print_number": 3, "status": "completed", "title": "Secret Bracket",
     "started_at": "2025-03-03T10:00:00Z", "printer": "Garage AD5X",
     "gcode_file": "bracket.gcode", "filament_material": "PLA", "filament_color": "Black",
     "filament_used_g": 10.0, "print_duration_seconds": 3600},
]
EVENTS = [{"timestamp": "2025-03-02T11:00:00Z", "verdict": "spaghetti", "print_number": 99,
           "job_key": "/data/archive/job-77/Private Part.gcode", "streak": 3}]


def payload():
    return print_stats.build_wrapped(RECORDS, 2025, events=EVENTS)


def page_of(result):
    return (Path(result["path"]) / "index.html").read_text(encoding="utf-8")


class DefaultsAndOptions(unittest.TestCase):
    def test_link_only_is_the_default_and_the_footage_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir()
            (out / "wrapped-2025.mp4").write_bytes(b"camera frames")
            result = share_export.export_bundle(payload(), out)
            self.assertEqual(result["visibility"], "link-only")
            self.assertFalse(result["video_included"])
            self.assertFalse((Path(result["path"]) / "wrapped-2025.mp4").exists())
            self.assertNotIn("<video", page_of(result))

    def test_the_share_form_defaults_to_link_only_and_explains_the_footage(self):
        view = {"ok": True, **payload(), "ai_video_enabled": False, "video_available": True,
                "share_exports": [], "share_registry_note": None,
                "share_preview": share_export.preview(payload())}
        page = data_module.render_wrapped_page({}, view, form_token="t")
        self.assertRegex(page, r'<option value="link-only" selected>')
        self.assertIn("Secret Bracket", page)
        self.assertIn("Garage AD5X", page)
        self.assertIn("contains camera footage", page)
        self.assertRegex(page, r'name="include_video" value="1"(?! checked)')
        self.assertIn('name="include_titles" value="1" checked', page)

    def test_titles_and_printer_names_can_be_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = share_export.export_bundle(payload(), Path(tmp) / "a")
            self.assertIn("Secret Bracket", page_of(kept))
            self.assertIn("Garage AD5X", page_of(kept))
            left = share_export.export_bundle(
                payload(), Path(tmp) / "b", include_titles=False, include_printers=False)
            page = page_of(left)
            for name in ("Secret Bracket", "Garage AD5X", "Private Part", "/data/archive"):
                self.assertNotIn(name, page)
            manifest = left["manifest"]
            self.assertFalse(manifest["titles_included"])
            self.assertFalse(manifest["printer_names_included"])
            self.assertIn("Your 2025 in printing", page)

    def test_redaction_does_not_touch_the_callers_payload(self):
        original = payload()
        share_export.redact_payload(original, include_titles=False, include_printers=False)
        self.assertEqual(original, payload())

    def test_video_with_a_footage_note_when_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir()
            (out / "wrapped-2025.mp4").write_bytes(b"camera frames")
            result = share_export.export_bundle(payload(), out, include_video=True)
            self.assertTrue(result["video_included"])
            self.assertIn(share_export.VIDEO_FOOTAGE_NOTE, result["manifest"]["notes"])


class PathsAreNeverTitles(unittest.TestCase):
    def test_display_title_reduces_a_path_to_a_scrubbed_base_name(self):
        show = print_stats.display_title
        self.assertEqual(show("/data/archive/job-1/My  Part.gcode"), "My Part")
        self.assertEqual(show("C:\\prints\\vase.3mf"), "vase")
        self.assertEqual(show(None, "/a/b/c.gcode"), "c")
        self.assertEqual(show("A/B bracket"), "A/B bracket")
        self.assertIsNone(show("", None))

    def test_a_job_key_path_never_reaches_commentary_or_the_bundle(self):
        data = payload()
        lines = " ".join(data["script"])
        self.assertNotIn("/data/archive", lines)
        self.assertIn("Private Part", lines)
        with tempfile.TemporaryDirectory() as tmp:
            result = share_export.export_bundle(data, Path(tmp) / "w")
            self.assertNotIn("/data/archive", page_of(result))


class StaleFilesAndPileUp(unittest.TestCase):
    def test_a_public_re_export_removes_the_stale_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir()
            (out / "wrapped-2025.mp4").write_bytes(b"camera frames")
            first = share_export.export_bundle(payload(), out, visibility="public",
                                               include_video=True)
            folder = Path(first["path"])
            self.assertTrue((folder / "wrapped-2025.mp4").is_file())
            (folder / "old-leftover.txt").write_text("x")
            share_export.export_bundle(payload(), out, visibility="public")
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["index.html", "manifest.json", "share-card-vertical.png",
                              "share-card.png"])
            entries, _ = share_export.load_exports(out)
            self.assertEqual(len(entries), 1)  # replaced, not listed twice

    def test_link_only_exports_are_capped_and_older_folders_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            made = []
            for _ in range(share_export.MAX_LINK_ONLY_EXPORTS + 2):
                made.append(share_export.export_bundle(payload(), out)["folder"])
            entries, _ = share_export.load_exports(out)
            self.assertEqual([e["folder"] for e in entries],
                             made[-share_export.MAX_LINK_ONLY_EXPORTS:])
            on_disk = sorted(p.name for p in (out / "exports").iterdir())
            self.assertEqual(on_disk, sorted(made[-share_export.MAX_LINK_ONLY_EXPORTS:]))

    def test_link_only_video_is_not_a_second_full_copy_when_the_disk_can_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wrapped"
            out.mkdir()
            source = out / "wrapped-2025.mp4"
            source.write_bytes(b"x" * 1000)
            result = share_export.export_bundle(payload(), out, include_video=True)
            copy = Path(result["path"]) / "wrapped-2025.mp4"
            self.assertEqual(copy.read_bytes(), source.read_bytes())
            try:
                self.assertTrue(copy.samefile(source))
            except AssertionError:
                pass  # a filesystem without hard links falls back to a copy


if __name__ == "__main__":
    unittest.main()
