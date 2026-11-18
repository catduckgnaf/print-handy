"""Pasta Wrapped's bundled music track and the pasta reel.

The bundled track is the default music bed (an operator file wins, the
synthesized pad is the last resort). The pasta reel is a second video:
every clip the name join ties to a failed or pasta print in the year,
each behind a label card with a local roast line, cut around the failure
and fitted to the track. Fake seams only: no real ffmpeg, no network.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from print_handy.webui import data as data_module
from print_handy.webui import pasta_reel, wrapped_video


class FakeRunner:
    """Records commands; writes the file named by the last argument."""

    def __init__(self):
        self.commands = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        self.kwargs.append(kwargs)
        target = Path(command[-1])
        if target.suffix in (".mp4", ".jpg"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake")
        return SimpleNamespace(returncode=0, stdout="")

    def flat(self):
        return "\n".join(" ".join(cmd) for cmd in self.commands)


def rec(number, status="failed", started="2026-03-04T10:00:00Z", seconds=3600, grams=20.0,
        **extra):
    return {"print_number": number, "status": status, "title": f"Widget {number}",
            "started_at": started, "print_duration_seconds": seconds,
            "filament_used_g": grams, "gcode_file": f"widget-{number}.gcode", **extra}


def wrapped_for(failed=(), pasta=(), pasta_state="recorded", year=2026):
    return {"year": year,
            "headline": {"total_prints": 10, "failed": len(failed), "pasta": len(pasta),
                         "pasta_state": pasta_state, "stopped": 0},
            "highlights": {"failed_print_numbers": sorted(failed),
                           "pasta_print_numbers": sorted(pasta)},
            "script": []}


def clip(tmp, number, name_year=2026):
    path = Path(tmp) / f"{name_year}0304-1000-Printer-print-{number}.mp4"
    path.write_bytes(b"clip")
    return {"path": str(path), "print_number": number}


class MusicPrecedenceTests(unittest.TestCase):
    def test_the_bundled_track_ships_inside_the_package(self):
        self.assertTrue(wrapped_video.BUNDLED_MUSIC.is_file())
        self.assertEqual(wrapped_video.BUNDLED_MUSIC.suffix, ".mp3")
        package = Path(wrapped_video.__file__).resolve().parents[1]
        self.assertEqual(wrapped_video.BUNDLED_MUSIC.resolve().parent,
                         package / "data" / "audio")

    def test_package_data_and_manifest_carry_the_track(self):
        root = Path(wrapped_video.__file__).resolve().parents[2]
        self.assertIn('"data/audio/*.mp3"', (root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn("*.mp3", (root / "MANIFEST.in").read_text(encoding="utf-8"))

    def test_operator_file_wins_when_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            track = Path(tmp) / "mine.mp3"
            track.write_bytes(b"music")
            music = wrapped_video.resolve_music({"timelapse": {"wrapped_music_file": str(track)}})
        self.assertEqual(music["kind"], "operator")
        self.assertEqual(Path(music["path"]), track.resolve())
        self.assertTrue(any("music: operator file" in n for n in music["notes"]))

    def test_missing_operator_file_falls_back_to_the_bundled_track_by_name(self):
        music = wrapped_video.resolve_music(
            {"timelapse": {"wrapped_music_file": "/nowhere/gone.mp3"}})
        self.assertEqual(music["kind"], "bundled")
        self.assertEqual(Path(music["path"]), wrapped_video.BUNDLED_MUSIC)
        self.assertTrue(any("operator file not found at /nowhere/gone.mp3" in n
                            for n in music["notes"]))

    def test_no_setting_uses_the_bundled_track(self):
        music = wrapped_video.resolve_music({})
        self.assertEqual(music["kind"], "bundled")
        self.assertTrue(any("music: bundled track" in n for n in music["notes"]))

    def test_missing_bundled_track_falls_back_to_the_synthesized_bed(self):
        with mock.patch.object(wrapped_video, "BUNDLED_MUSIC", Path("/nowhere/track.mp3")):
            music = wrapped_video.resolve_music({})
        self.assertEqual(music["kind"], "synthesized")
        self.assertIsNone(music["path"])
        self.assertTrue(any("synthesized bed" in n for n in music["notes"]))


class MusicMixTests(unittest.TestCase):
    WRAPPED = {"year": 2026, "headline": {"total_prints": 3, "failed": 1},
               "highlights": {}, "script": ["One line."]}

    def _build(self, tmp, probe=None):
        runner = FakeRunner()
        result = wrapped_video.build_wrapped_video(
            2026, self.WRAPPED, Path(tmp) / "out", ffmpeg="ffmpeg", runner=runner,
            find_clips=lambda year: [], probe=probe)
        audio = [cmd for cmd in runner.commands if "-c:a" in cmd]
        return result, runner, audio

    def test_the_wrapped_video_uses_the_bundled_track_and_fades_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _runner, audio = self._build(tmp)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(audio), 1)
        mix = " ".join(audio[0])
        self.assertIn(str(wrapped_video.BUNDLED_MUSIC), mix)
        total = sum(s["duration"] for s in wrapped_video.build_slides(self.WRAPPED))
        start = total - wrapped_video.MUSIC_FADE_SECONDS
        self.assertIn(f"afade=t=out:st={start:g}:d={wrapped_video.MUSIC_FADE_SECONDS:g}", mix)
        # The failures slide keeps its ducking and sad beat over a real track.
        self.assertIn("volume=0.04", mix)
        self.assertIn("volume=0.22", mix)
        self.assertTrue(any("music: bundled track" in n for n in result["notes"]))

    def test_a_track_shorter_than_the_video_repeats_with_a_fade_at_each_seam(self):
        with tempfile.TemporaryDirectory() as tmp:
            _result, _runner, audio = self._build(tmp, probe=lambda path: 10.0)
        mix = " ".join(audio[0])
        total = sum(s["duration"] for s in wrapped_video.build_slides(self.WRAPPED))
        copies = -(-total // 10)
        self.assertEqual(audio[0].count(str(wrapped_video.BUNDLED_MUSIC)), copies)
        self.assertIn(f"concat=n={copies}:v=0:a=1", mix)
        self.assertNotIn("-stream_loop", audio[0])
        self.assertIn("afade=t=out:st=8:d=2", mix)   # each copy fades before its seam


class ReelSelectionTests(unittest.TestCase):
    def test_only_failed_and_pasta_clips_one_per_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            clips = [clip(tmp, 1), clip(tmp, 2), clip(tmp, 3), clip(tmp, 2),
                     {"path": str(Path(tmp) / "unjoined.mp4"), "print_number": None}]
            records = [rec(1, "completed"), rec(2), rec(3, "completed")]
            entries = pasta_reel.reel_entries(clips, wrapped_for(failed=[2], pasta=[3]), records)
        self.assertEqual([e["print_number"] for e in entries], [2, 3])
        self.assertEqual([e["kind"] for e in entries], ["failed", "pasta"])

    def test_plan_keeps_every_clip_at_the_longest_cut_when_they_fit(self):
        entries = [{"print_number": n, "seconds": 60, "streak": 0, "order": n} for n in range(3)]
        plan = pasta_reel.plan_reel(entries, 127)
        self.assertEqual(len(plan["kept"]), 3)
        self.assertEqual(plan["dropped"], 0)
        self.assertEqual(plan["cut"], pasta_reel.CUT_MAX_SECONDS)

    def test_plan_shortens_cuts_to_fit_the_track(self):
        entries = [{"print_number": n, "seconds": 60, "streak": 0, "order": n} for n in range(12)]
        plan = pasta_reel.plan_reel(entries, 127)
        self.assertEqual(len(plan["kept"]), 12)
        self.assertGreaterEqual(plan["cut"], pasta_reel.CUT_MIN_SECONDS)
        self.assertLess(plan["cut"], pasta_reel.CUT_MAX_SECONDS)
        self.assertLessEqual(pasta_reel.reel_seconds(12, plan["cut"]), 127)

    def test_plan_keeps_the_worst_when_even_minimum_cuts_do_not_fit(self):
        entries = [{"print_number": n, "seconds": n * 100, "streak": 0, "order": n}
                   for n in range(1, 41)]
        entries[0]["streak"] = 9  # a recorded pasta streak makes the cut too
        plan = pasta_reel.plan_reel(entries, 127)
        kept = [e["print_number"] for e in plan["kept"]]
        self.assertEqual(plan["cut"], pasta_reel.CUT_MIN_SECONDS)
        self.assertEqual(plan["dropped"], 40 - len(kept))
        self.assertGreater(plan["dropped"], 0)
        self.assertIn(1, kept)
        self.assertIn(40, kept)
        self.assertNotIn(2, kept)
        self.assertEqual(kept, sorted(kept))  # shown in the year's order
        self.assertLessEqual(pasta_reel.reel_seconds(len(kept), plan["cut"]), 127)


class CutWindowTests(unittest.TestCase):
    def test_a_watchdog_marker_maps_into_the_clip_and_centers_the_cut(self):
        record = rec(5, seconds=1000)
        started = pasta_reel._epoch(record["started_at"])
        markers = [{"event": "start", "wall_s": started, "elapsed_s": 0.0},
                   {"event": "watchdog_alert", "wall_s": started + 500, "elapsed_s": None}]
        moment = pasta_reel.failure_moment(record, markers, clip_seconds=20)
        self.assertAlmostEqual(moment, 10.0)
        self.assertAlmostEqual(pasta_reel.cut_start(20, 6, moment), 7.0)

    def test_markers_from_another_run_of_the_same_file_are_ignored(self):
        record = rec(5, seconds=1000)
        started = pasta_reel._epoch(record["started_at"])
        markers = [{"event": "cancel", "wall_s": started - 86400, "elapsed_s": 100.0}]
        self.assertIsNone(pasta_reel.failure_moment(record, markers, clip_seconds=20))

    def test_the_cut_stays_inside_the_clip(self):
        self.assertEqual(pasta_reel.cut_start(20, 6, 19.5), 14)
        self.assertEqual(pasta_reel.cut_start(20, 6, 0.5), 0)
        self.assertIsNone(pasta_reel.cut_start(None, 6, None))   # unknown length: the end
        self.assertEqual(pasta_reel.cut_start(20, 6, None), 14)  # no marker: the end


class ReelBuildTests(unittest.TestCase):
    def _build(self, tmp, clips, records, wrapped, probe=None, markers=None, **kwargs):
        runner = FakeRunner()
        result = pasta_reel.build_pasta_reel(
            wrapped["year"], wrapped, Path(tmp) / "out", ffmpeg="ffmpeg", runner=runner,
            find_clips=lambda year: clips, records=records, probe=probe,
            markers_for=markers or (lambda record: []), **kwargs)
        return result, runner

    def test_shape_open_label_clip_close_and_honest_closing_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            clips = [clip(tmp, 1), clip(tmp, 2)]
            records = [rec(1, seconds=7200, grams=100.0), rec(2, "completed", grams=50.0)]
            wrapped = wrapped_for(failed=[1], pasta=[2])
            result, runner = self._build(tmp, clips, records, wrapped)
            self.assertTrue(result["ok"], result)
            self.assertEqual(Path(result["path"]).name, "pasta-reel-2026.mp4")
            self.assertTrue(Path(result["path"]).is_file())
            texts = " ".join(p.read_text(encoding="utf-8")
                             for p in (Path(tmp) / "out").glob("**/text-*.txt"))
        self.assertEqual(result["roles"], ["open", "label", "clip", "label", "clip", "close"])
        cards = result["cards"]
        self.assertIn("Your 2026 pasta reel", cards[0]["hero"])
        self.assertIn("Widget 1", cards[1]["hero"])
        self.assertIn("FAILED", cards[1]["eyebrow"])
        self.assertIn("PASTA", cards[2]["eyebrow"])
        self.assertTrue(cards[1]["line"])  # a local roast line
        close = cards[-1]
        self.assertIn("1 failed print", close["hero"])
        self.assertIn("3 hours", close["line"])
        self.assertIn("150 g", close["line"])
        self.assertIn("1 pasta print", close["line"])
        self.assertEqual(texts, "")  # the work folder is gone
        self.assertTrue(any("-c:a" in cmd for cmd in runner.commands))
        self.assertIn(str(wrapped_video.BUNDLED_MUSIC), runner.flat())

    def test_pasta_never_reads_zero_when_it_was_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _runner = self._build(tmp, [clip(tmp, 1)], [rec(1)],
                                          wrapped_for(failed=[1], pasta_state="not_recorded"))
        line = result["cards"][-1]["line"]
        self.assertNotIn("0 pasta", line)
        self.assertIn("not recorded", line)

    def test_too_many_failures_names_the_rest_on_the_closing_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            numbers = list(range(1, 41))
            clips = [clip(tmp, n) for n in numbers]
            records = [rec(n, seconds=n * 60) for n in numbers]
            result, _runner = self._build(tmp, clips, records, wrapped_for(failed=numbers))
        self.assertTrue(result["ok"], result)
        kept = result["roles"].count("clip")
        self.assertLess(kept, 40)
        self.assertIn(f"{40 - kept} more", result["cards"][-1]["line"])

    def test_the_cut_is_centered_on_the_mapped_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = rec(1, seconds=1000)
            started = pasta_reel._epoch(record["started_at"])
            markers = [{"event": "pause", "wall_s": started + 600, "elapsed_s": 600.0}]
            result, runner = self._build(tmp, [clip(tmp, 1)], [record], wrapped_for(failed=[1]),
                                         probe=lambda path: 30.0,
                                         markers=lambda r: markers)
        cut = [cmd for cmd in runner.commands if cmd[-1].endswith(".mp4") and "reel-clip-" in cmd[-1]]
        self.assertEqual(len(cut), 1)
        expected = 18.0 - pasta_reel.CUT_MAX_SECONDS / 2
        self.assertEqual(cut[0][cut[0].index("-ss") + 1], f"{expected:g}")

    def test_no_marker_and_unknown_length_takes_the_end_of_the_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = self._build(tmp, [clip(tmp, 1)], [rec(1)], wrapped_for(failed=[1]))
        cut = [cmd for cmd in runner.commands if "reel-clip-" in cmd[-1]]
        self.assertIn("-sseof", cut[0])
        self.assertIn("tpad=stop_mode=clone", " ".join(cut[0]))

    def test_the_reel_fits_the_track_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            numbers = list(range(1, 16))
            clips = [clip(tmp, n) for n in numbers]
            records = [rec(n) for n in numbers]
            result, _runner = self._build(tmp, clips, records, wrapped_for(failed=numbers),
                                          probe=lambda path: 60.0)
        self.assertLessEqual(result["seconds"], 60.0)

    def test_export_silent_has_no_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = self._build(tmp, [clip(tmp, 1)], [rec(1)], wrapped_for(failed=[1]),
                                         export_silent=True)
        self.assertTrue(result["ok"], result)
        self.assertFalse(any("-c:a" in cmd for cmd in runner.commands))

    def test_zero_failure_clips_is_no_reel_and_a_joke_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, runner = self._build(tmp, [clip(tmp, 1)], [rec(1, "completed")],
                                         wrapped_for())
        self.assertFalse(result["ok"])
        self.assertTrue(result["empty"])
        self.assertEqual(result["reason"], pasta_reel.EMPTY_REASON)
        self.assertEqual(runner.commands, [])

    def test_every_subprocess_has_a_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            _result, runner = self._build(tmp, [clip(tmp, 1)], [rec(1)], wrapped_for(failed=[1]))
        for command, kwargs in zip(runner.commands, runner.kwargs, strict=True):
            self.assertIsInstance(kwargs.get("timeout"), (int, float), command)

    def test_roast_lines_are_local_and_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = self._build(tmp, [clip(tmp, 1)], [rec(1)], wrapped_for(failed=[1]))
            second, _ = self._build(tmp, [clip(tmp, 1)], [rec(1)], wrapped_for(failed=[1]))
        self.assertEqual(first["cards"][1]["line"], second["cards"][1]["line"])


class ReelDataLayerTests(unittest.TestCase):
    def settings(self, tmp, video=True):
        return {"timelapse": {"wrapped_enabled": True, "wrapped_video": video},
                "paths": {"records_vault": str(tmp)}}

    def test_make_reel_waits_and_serves_only_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            clips = [clip(tmp, 1)]
            data = self.settings(tmp)
            with mock.patch.object(data_module, "_reel_inputs",
                                   return_value=(wrapped_for(failed=[1]), [rec(1)], [], None)):
                code, body = data_module.make_reel(
                    data, 2026, wait=True, jobs=wrapped_video.VideoJobs(),
                    runner=FakeRunner(), find_clips=lambda year: clips, ffmpeg="ffmpeg",
                    markers_for=lambda record: [])
            self.assertEqual(code, 200, body)
            self.assertEqual(data_module.wrapped_reel_path(data, 2026), Path(body["path"]))
            self.assertIsNone(data_module.wrapped_reel_path(data, "../2026"))

    def test_gates_refuse_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = wrapped_video.VideoJobs()
            self.assertEqual(data_module.make_reel(self.settings(tmp, video=False), 2026,
                                                   jobs=jobs)[0], 403)
            self.assertEqual(data_module.make_reel(self.settings(tmp), "x", jobs=jobs)[0], 400)
            self.assertEqual(data_module.reel_status(self.settings(tmp), "x", jobs=jobs)[0], 400)
            self.assertEqual(jobs.status(2026)["state"], "idle")

    def test_a_year_without_failure_clips_refuses_with_the_joke(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = wrapped_video.VideoJobs()
            with mock.patch.object(data_module, "_reel_inputs",
                                   return_value=(wrapped_for(), [rec(1, "completed")], [], None)):
                code, body = data_module.make_reel(
                    self.settings(tmp), 2026, jobs=jobs, find_clips=lambda year: [])
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], pasta_reel.EMPTY_REASON)
        self.assertEqual(jobs.status(2026)["state"], "idle")

    def test_the_reveal_helper_is_the_one_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(data_module, "wrapped_revealed", create=True,
                                   side_effect=lambda data, year, now=None: False):
                self.assertFalse(data_module.reel_revealed(self.settings(tmp), 2026))
                code, _body = data_module.make_reel(self.settings(tmp), 2026,
                                                    jobs=wrapped_video.VideoJobs())
                self.assertEqual(code, 403)
            self.assertTrue(data_module.reel_revealed(self.settings(tmp), 2026))


class ReelPageTests(unittest.TestCase):
    PAYLOAD = {"year": 2026, "headline": {"total_prints": 1, "total_hours": 1,
                                          "total_filament_used_kg": 0.1, "success_rate": None},
               "highlights": {"night_prints": 0, "failed": 0, "risk_flagged": 0,
                              "worst_failure_streak": 0},
               "script": [], "ai_video_enabled": True, "years_available": [2026],
               "reel_revealed": True}

    def test_the_reel_form_sits_beside_the_video(self):
        page = data_module.render_wrapped_page(
            {}, {**self.PAYLOAD, "reel_clips": 3}, form_token="t")
        self.assertIn("Make my pasta reel", page)
        self.assertIn('action="/wrapped/reel"', page)
        self.assertLess(page.index('action="/wrapped/video"'), page.index('action="/wrapped/reel"'))

    def test_a_year_without_failures_on_camera_gets_the_joke(self):
        page = data_module.render_wrapped_page(
            {}, {**self.PAYLOAD, "reel_clips": 0}, form_token="t")
        self.assertNotIn('action="/wrapped/reel"', page)
        self.assertIn(pasta_reel.EMPTY_REASON, page)

    def test_progress_polls_the_reel_status_and_the_player_embeds_the_reel(self):
        running = {**self.PAYLOAD, "reel_clips": 2,
                   "reel_job": {"state": "running", "step": 2, "steps": 7}}
        page = data_module.render_wrapped_page({}, running, form_token="t")
        self.assertIn("/api/wrapped/reel-status", page)
        self.assertNotIn('action="/wrapped/reel"', page)
        done = {**self.PAYLOAD, "reel_clips": 2, "reel_available": True}
        page = data_module.render_wrapped_page({}, done, form_token="t")
        self.assertIn('src="/wrapped/reel-file?year=2026"', page)

    def test_an_unrevealed_year_shows_no_reel_section(self):
        page = data_module.render_wrapped_page(
            {}, {**self.PAYLOAD, "reel_clips": 3, "reel_revealed": False}, form_token="t")
        self.assertNotIn("pasta reel", page.lower())


if __name__ == "__main__":
    unittest.main()
