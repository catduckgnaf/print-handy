"""#981 step 4: Wrapped video presentation, music bed and TTS narration.

Fake seams only: no real Piper binary, no network. The fake runner
records every ffmpeg/piper-shaped command and writes the output file
its last argument names, so command construction, the slide timeline,
the audio assembly and every named note are pinned offline.
"""
import io
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from print_handy import settings
from print_handy.webui import catalog, data as data_module, wrapped_video


WRAPPED = {
    "year": 2026,
    "headline": {"total_prints": 12, "total_hours": 40.0,
                 "total_filament_used_g": 2500.0, "total_filament_used_kg": 2.5,
                 "success_rate": 0.75, "failed": 2, "stopped": 1, "pasta": 1},
    "highlights": {
        "top_color": "Black", "top_material": "PLA",
        "busiest_month": "2026-07", "busiest_month_prints": 5,
        "most_reprinted": {"title": "Bracket", "count": 4},
        "longest_print": {"title": "Dragon", "seconds": 7200},
        "failed_print_numbers": [9], "pasta_print_numbers": [9]},
    "archetype": {"name": "Night Owl", "fact": "Most prints ran after midnight."},
    "script": ["You printed a lot this year.", "The dragon took two hours."],
}


class FakeRunner:
    """Records commands; writes the file named by the last argument."""

    def __init__(self):
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        target = Path(command[-1])
        if target.suffix in (".mp4", ".wav", ".jpg", ".mp3"):
            target.parent.mkdir(parents=True, exist_ok=True)
            # A jpg above the near-uniform floor, so background frames
            # built through this runner count as usable images.
            target.write_bytes(b"x" * 4096 if target.suffix == ".jpg" else b"fake")
        return SimpleNamespace(returncode=0)

    def flat(self):
        return "\n".join(" ".join(cmd) for cmd in self.commands)


def _build(tmp, wrapped=WRAPPED, clips=(), data=None, **kwargs):
    kwargs.setdefault("ffmpeg", "ffmpeg")
    kwargs.setdefault("runner", FakeRunner())
    kwargs.setdefault("find_clips", lambda year: list(clips))
    result = wrapped_video.build_wrapped_video(
        wrapped["year"], wrapped, Path(tmp) / "out", data=data, **kwargs)
    return result, kwargs["runner"]


class SlideArcTests(unittest.TestCase):
    def test_arc_order_counts_durations_and_word_cap(self):
        slides = wrapped_video.build_slides(WRAPPED)
        roles = [slide["role"] for slide in slides]
        self.assertEqual(roles[0], "hook")
        self.assertEqual(roles[-1], "finale")
        self.assertIn("failures", roles)
        # Failures are mid-late: after personality, never last.
        self.assertLess(roles.index("personality"), roles.index("failures"))
        self.assertLess(roles.index("failures"), len(roles) - 1)
        self.assertIn("footage", wrapped_video.timeline_roles(WRAPPED, clips=1))
        self.assertTrue(11 <= len(slides) <= 14, roles)
        for slide in slides:
            self.assertTrue(5 <= slide["duration"] <= 7 or slide["role"] == "finale",
                            slide)
            words = len((slide["eyebrow"] + " " + slide["hero"] + " "
                         + slide["line"]).split())
            self.assertLessEqual(words, 20, slide)
            self.assertTrue(slide["eyebrow"] and slide["hero"], slide)
        finale = slides[-1]
        self.assertGreater(finale["duration"],
                           max(s["duration"] for s in slides[:-1]))
        total = sum(s["duration"] for s in slides)
        self.assertTrue(60 <= total + wrapped_video.FOOTAGE_SECONDS <= 95, total)

    def test_slides_are_data_gated(self):
        thin = {"year": 2026, "headline": {}, "highlights": {}, "script": []}
        roles = [s["role"] for s in wrapped_video.build_slides(thin)]
        self.assertEqual(roles, ["hook", "finale"])
        no_fail = dict(WRAPPED, headline={**WRAPPED["headline"], "failed": 0,
                                          "stopped": 0, "pasta": None})
        roles = [s["role"] for s in wrapped_video.build_slides(no_fail)]
        self.assertNotIn("failures", roles)

    def test_three_type_levels_in_rendered_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp)
            self.assertTrue(result["ok"], result)
        flat = runner.flat()
        self.assertIn("fontsize=28", flat)   # eyebrow
        self.assertIn("fontsize=64", flat)   # hero
        self.assertIn("fontsize=36", flat)   # explanation


class BackgroundTests(unittest.TestCase):
    def _clip(self, tmp, name="20260301-1200-Widget-print-1.mp4"):
        clip = Path(tmp) / name
        clip.write_bytes(b"fake-clip")
        return {"path": str(clip), "print_number": 1}

    def test_frame_grab_blur_and_scrim(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, clips=[self._clip(tmp)])
            self.assertTrue(result["ok"], result)
        flat = runner.flat()
        self.assertIn("-frames:v", flat)                    # frame grab
        self.assertIn("gblur", flat)                        # blur when busy
        self.assertIn("black@0.62", flat)                   # >=54% overlay
        self.assertFalse(any("backgrounds: no operator images" in n
                             for n in result["notes"]))

    def test_no_images_falls_back_to_solid_with_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, clips=[])
            self.assertTrue(result["ok"], result)
        self.assertIn("backgrounds: no operator images found", result["notes"])
        self.assertIn("color=c=0x12110F", runner.flat())

    def test_failed_extraction_uses_solid_inset_fallback(self):
        class NoJpgRunner(FakeRunner):
            def __call__(self, command, **kwargs):
                self.commands.append([str(p) for p in command])
                target = Path(command[-1])
                if target.suffix == ".mp4":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"fake")
                return SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, clips=[self._clip(tmp)],
                                    runner=NoJpgRunner())
            self.assertTrue(result["ok"], result)
        self.assertIn("backgrounds: no operator images found", result["notes"])


class MusicTests(unittest.TestCase):
    def test_synthesized_bed_and_sad_beat_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp)
            self.assertTrue(result["ok"], result)
        flat = runner.flat()
        self.assertIn("aevalsrc", flat)                     # synthesized, no assets
        audio = [cmd for cmd in runner.commands if "-c:a" in cmd]
        self.assertEqual(len(audio), 1)
        mix = " ".join(audio[0])
        self.assertIn("volume=0.16", mix)                   # upbeat bed
        self.assertIn("volume=0.22", mix)                   # slow minor pad
        self.assertIn("volume=0.30", mix)                   # descending sad sting
        self.assertIn("volume=0.04", mix)                   # bed ducked on failures
        self.assertTrue(any("music: synthesized bed" in n for n in result["notes"]))

    def test_operator_music_file_and_missing_file_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "track.mp3"
            track.write_bytes(b"fake-music")
            data = {"timelapse": {"wrapped_music_file": str(track)}}
            result, runner = _build(tmp, data=data)
            self.assertTrue(result["ok"], result)
            self.assertIn(str(track), runner.flat())
            self.assertTrue(any("music: operator file" in n for n in result["notes"]))
            missing = {"timelapse": {"wrapped_music_file": str(Path(tmp) / "gone.mp3")}}
            result2, runner2 = _build(tmp, data=missing)
            self.assertTrue(result2["ok"], result2)
            self.assertTrue(any("music: operator file not found" in n
                                for n in result2["notes"]))
            self.assertIn("aevalsrc", runner2.flat())       # synthesized fallback

    def test_export_silent_has_no_audio_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(
                tmp, export_silent=True,
                data={"timelapse": {"wrapped_tts_engine": "piper"}},
                tts_call=lambda text, out: Path(out).write_bytes(b"wav"))
            self.assertTrue(result["ok"], result)
        self.assertFalse(any("-c:a" in cmd for cmd in runner.commands))
        self.assertTrue(any("audio: silent export" in n for n in result["notes"]))


class PiperTests(unittest.TestCase):
    DATA = {"timelapse": {"wrapped_tts_engine": "piper"},
            "paths": {"piper": "/fake/piper", "piper_model": "/fake/voice.onnx"}}

    def test_piper_command_construction(self):
        command = wrapped_video.piper_command("/fake/piper", "/fake/voice.onnx",
                                              "/tmp/line.wav")
        self.assertEqual(command, ["/fake/piper", "--model", "/fake/voice.onnx",
                                   "--output_file", "/tmp/line.wav"])

    def test_per_line_synthesis_and_mux_timing(self):
        calls = []

        def fake_tts(text, out_path):
            calls.append((text, str(out_path)))
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"wav")
            return out_path

        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, data=self.DATA, tts_call=fake_tts)
            self.assertTrue(result["ok"], result)
        slides = wrapped_video.build_slides(WRAPPED)
        self.assertEqual(len(calls), len(slides))
        audio = [cmd for cmd in runner.commands if "-c:a" in cmd]
        self.assertEqual(len(audio), 1)
        mix = " ".join(audio[0])
        self.assertIn("adelay=", mix)                       # timed to slides
        self.assertIn("volume=1.0", mix)                    # narration primary
        self.assertIn("volume=0.10", mix)                   # bed ducked under it
        self.assertTrue(any("narration: piper" in n for n in result["notes"]))

    def test_missing_binary_is_named_and_video_still_produced(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, data=self.DATA)
            self.assertTrue(result["ok"], result)
        self.assertIn("narration: piper binary not found at /fake/piper",
                      result["notes"])
        self.assertTrue(any("-c:a" in cmd for cmd in runner.commands))  # music still on

    def test_missing_model_is_named(self):
        data = {"timelapse": {"wrapped_tts_engine": "piper"},
                "paths": {"piper": "piper", "piper_model": ""}}
        with tempfile.TemporaryDirectory() as tmp:
            # "piper" may not exist on PATH either; either named note is
            # the pinned behavior, and the binary note wins when both go.
            result, _runner = _build(tmp, data=data)
            self.assertTrue(result["ok"], result)
        self.assertTrue(any(n.startswith("narration: piper") for n in result["notes"]))

    def test_engine_none_keeps_music_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = _build(tmp, data={"timelapse": {}})
            self.assertTrue(result["ok"], result)
        self.assertFalse(any("narration:" in n for n in result["notes"]))


class NoCloudNarrationTests(unittest.TestCase):
    """Narration is local Piper or nothing: no AI model, no cloud service."""

    def test_cloud_engine_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "cloud.yaml"
            bad.write_text("timelapse:\n  wrapped_tts_engine: cloud\n")
            with self.assertRaises(settings.SettingsError):
                settings.load(local=bad, environ={})

    def test_no_speech_client_or_speech_table(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("print_handy.wrapped_speech"))
        data = {}
        settings._validate_ai(data)
        self.assertNotIn("speech", data["ai"])

    def test_an_unknown_engine_reaching_the_compiler_is_named_and_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _runner = _build(tmp, data={"timelapse": {"wrapped_tts_engine": "cloud"}})
            self.assertTrue(result["ok"], result)
        self.assertTrue(any(n.startswith("narration: unknown engine") for n in result["notes"]))


class SettingsSurfaceTests(unittest.TestCase):
    def test_new_settings_defaults_env_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = settings.load(local=False, environ={})
            self.assertEqual(data["timelapse"]["wrapped_tts_engine"], "none")
            self.assertEqual(data["timelapse"]["wrapped_music_file"], "")
            self.assertEqual(str(data["paths"]["piper"]), "piper")
            self.assertEqual(str(data["paths"]["piper_model"]), "")
            env = settings.load(
                local=False,
                environ={"PRINT_HANDY__TIMELAPSE__WRAPPED_TTS_ENGINE": "piper",
                         "PRINT_HANDY__PATHS__PIPER": "/opt/piper/piper"})
            self.assertEqual(env["timelapse"]["wrapped_tts_engine"], "piper")
            self.assertEqual(str(env["paths"]["piper"]), "/opt/piper/piper")
            legacy = settings.load(local=False, environ={"PIPER_BIN": "/legacy/piper"})
            self.assertEqual(str(legacy["paths"]["piper"]), "/legacy/piper")
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("timelapse:\n  wrapped_tts_engine: loud\n")
            with self.assertRaises(settings.SettingsError):
                settings.load(local=bad, environ={})

    def test_catalog_fields_for_timelapse_settings(self):
        engine = catalog.field_by_id("timelapse.wrapped_tts_engine")
        self.assertIsNotNone(engine)
        self.assertEqual(engine.kind, "picker")
        self.assertEqual(list(engine.choices), ["none", "piper"])
        music = catalog.field_by_id("timelapse.wrapped_music_file")
        self.assertIsNotNone(music)
        # Path settings mirror paths.ffmpeg: no catalog field.
        self.assertIsNone(catalog.field_by_id("paths.piper"))
        self.assertIsNone(catalog.field_by_id("paths.piper_model"))


class EmbedRouteTests(unittest.TestCase):
    DATA = {"timelapse": {"wrapped_enabled": True},
            "paths": {"records_vault": ""}}

    def test_video_file_resolution_is_pinned_to_the_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            out = vault / "wrapped"
            out.mkdir()
            (out / "wrapped-2026.mp4").write_bytes(b"video")
            (out / "other.mp4").write_bytes(b"nope")
            found = data_module.wrapped_video_path(self.DATA, 2026,
                                                   records_vault=vault)
            self.assertEqual(found, out / "wrapped-2026.mp4")
            self.assertIsNone(data_module.wrapped_video_path(
                self.DATA, 2025, records_vault=vault))
            self.assertIsNone(data_module.wrapped_video_path(
                {"timelapse": {}}, 2026, records_vault=vault))
            # Traversal-shaped years never resolve to a file: the name
            # is built from the integer year, never from request text.
            for bad_year in ("../2026", "2026/../../etc/passwd", "wrapped-2026"):
                self.assertIsNone(data_module.wrapped_video_path(
                    self.DATA, bad_year, records_vault=vault))

    def test_wrapped_page_embeds_video_and_discloses_narration(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            out = vault / "wrapped"
            out.mkdir()
            (out / "wrapped-2026.mp4").write_bytes(b"video")
            from print_handy.core import print_stats
            wrapped = print_stats.build_wrapped([], 2026)
            payload = {"ok": True, **wrapped, "ai_video_enabled": True,
                       "video_available": True}
            page = data_module.render_wrapped_page(
                {**self.DATA, "paths": {"records_vault": str(vault)}},
                payload, form_token="tok")
            self.assertIn("<video controls", page)
            self.assertIn("/wrapped/video-file?year=2026", page)
            self.assertIn("narration", page.lower())
            self.assertNotIn("speech route", page.lower())
            self.assertIn('name="export_silent"', page)


if __name__ == "__main__":
    unittest.main()
