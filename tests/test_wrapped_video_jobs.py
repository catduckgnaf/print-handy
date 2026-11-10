"""The Wrapped video compile as one background job per year.

A compile is 15 to 40 ffmpeg runs plus an optional narration subprocess.
These tests pin the job contract (one job per year, a second request
joins it, a status the page can poll), the fresh work folder that is
always removed, and the timeout on every subprocess. No real ffmpeg.
"""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from print_handy.webui import data as data_module
from print_handy.webui import wrapped_video

WRAPPED = {"year": 2026, "headline": {"total_prints": 3}, "highlights": {},
           "script": ["One line."]}


class RecordingRunner:
    def __init__(self, fail_on=None, hang_on=None):
        self.commands = []
        self.kwargs = []
        self.work_dirs = set()
        self.fail_on = fail_on
        self.hang_on = hang_on

    def __call__(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        self.kwargs.append(kwargs)
        if kwargs.get("cwd"):
            self.work_dirs.add(kwargs["cwd"])
        if self.hang_on and self.hang_on in " ".join(map(str, command)):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        if self.fail_on and self.fail_on in " ".join(map(str, command)):
            return SimpleNamespace(returncode=1)
        target = Path(command[-1])
        if target.suffix == ".mp4":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-video")
        return SimpleNamespace(returncode=0)


def build(out, runner, **kwargs):
    return wrapped_video.build_wrapped_video(
        2026, WRAPPED, out, ffmpeg="ffmpeg", runner=runner,
        find_clips=lambda year: [], **kwargs)


class WorkFolderTests(unittest.TestCase):
    def test_work_folder_is_fresh_and_removed_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            first, second = RecordingRunner(), RecordingRunner()
            self.assertTrue(build(out, first)["ok"])
            self.assertTrue(build(out, second)["ok"])
            self.assertEqual(len(first.work_dirs), 1)
            self.assertEqual(len(second.work_dirs), 1)
            self.assertNotEqual(first.work_dirs, second.work_dirs)
            self.assertEqual([p.name for p in out.glob(".wrapped-*")], [])
            self.assertTrue((out / "wrapped-2026.mp4").is_file())

    def test_work_folder_is_removed_after_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = build(out, RecordingRunner(fail_on="concat"))
            self.assertFalse(result["ok"])
            self.assertEqual([p.name for p in out.glob(".wrapped-*")], [])
            self.assertFalse((out / "wrapped-2026.mp4").exists())

    def test_a_second_run_cannot_delete_the_first_runs_files(self):
        """The old fixed folder was shared: run B's cleanup deleted run A's
        pieces mid-compile. Each run now owns a folder nothing else touches."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            seen = {}

            class Nested(RecordingRunner):
                def __call__(inner, command, **kwargs):
                    if "concat" in command and "outer" not in seen:
                        seen["outer"] = Path(kwargs["cwd"])
                        build(out, RecordingRunner())  # a whole second run, inside the first
                        seen["alive"] = Path(kwargs["cwd"]).is_dir()
                    return super().__call__(command, **kwargs)

            self.assertTrue(build(out, Nested())["ok"])
            self.assertTrue(seen["alive"])

    def test_stale_work_folders_from_dead_runs_are_swept(self):
        import os
        import time
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            stale = out / ".wrapped-2026-parts"
            stale.mkdir(parents=True)
            old = time.time() - wrapped_video.STALE_WORK_SECONDS - 60
            os.utime(stale, (old, old))
            fresh = out / ".wrapped-2026-live"
            fresh.mkdir()
            build(out, RecordingRunner())
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())


class TimeoutTests(unittest.TestCase):
    def test_every_subprocess_has_a_timeout_scaled_to_its_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = RecordingRunner()
            self.assertTrue(build(Path(tmp) / "out", runner)["ok"])
        self.assertTrue(runner.kwargs)
        for command, kwargs in zip(runner.commands, runner.kwargs):
            self.assertIsInstance(kwargs.get("timeout"), (int, float), command)
        slide_timeouts = {kwargs["timeout"] for command, kwargs in
                          zip(runner.commands, runner.kwargs) if "slide-" in command[-1]}
        self.assertTrue(all(t > wrapped_video.STEP_TIMEOUT_BASE_S for t in slide_timeouts))
        self.assertGreater(wrapped_video.step_timeout(60), wrapped_video.step_timeout(5))

    def test_a_hung_ffmpeg_ends_the_compile_with_a_named_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = build(out, RecordingRunner(hang_on="slide-"))
            self.assertFalse(result["ok"])
            self.assertIn("timed out", result["reason"])
            self.assertEqual([p.name for p in out.glob(".wrapped-*")], [])

    def test_piper_runs_with_a_timeout(self):
        from unittest import mock
        with mock.patch.object(wrapped_video.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)) as run:
            wrapped_video._piper_synthesize("piper", "model.onnx", "hi", "out.wav")
        self.assertEqual(run.call_args.kwargs["timeout"], wrapped_video.PIPER_TIMEOUT_S)

    def test_progress_counts_steps_up_to_the_total(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp) / "out", RecordingRunner(),
                  progress=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual([d for d, _t in seen], list(range(1, len(seen) + 1)))


class JobTests(unittest.TestCase):
    def test_a_second_request_joins_the_running_job(self):
        jobs = wrapped_video.VideoJobs()
        gate, started, calls = threading.Event(), threading.Event(), []

        def work(progress):
            calls.append(1)
            progress(2, 5)
            started.set()
            gate.wait(5)
            return {"ok": True, "path": "/x/wrapped-2026.mp4", "notes": ["n"]}

        first, joined_first = jobs.start(2026, work)
        self.assertFalse(joined_first)
        self.assertTrue(started.wait(5))
        second, joined_second = jobs.start(2026, work)
        self.assertTrue(joined_second)
        self.assertEqual(second["state"], "running")
        self.assertEqual((second["step"], second["steps"]), (2, 5))
        self.assertEqual(jobs.status(2026)["state"], "running")
        gate.set()
        for _ in range(100):
            if jobs.status(2026)["state"] == "done":
                break
            threading.Event().wait(0.05)
        done = jobs.status(2026)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["path"], "/x/wrapped-2026.mp4")
        self.assertEqual(len(calls), 1)

    def test_a_failed_job_reports_a_short_reason_and_can_be_retried(self):
        jobs = wrapped_video.VideoJobs()
        status, _ = jobs.start(2026, lambda progress: {"ok": False, "reason": "ffmpeg exploded"},
                               wait=True)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["reason"], "ffmpeg exploded")
        status, joined = jobs.start(2026, lambda progress: {"ok": True, "path": "p"}, wait=True)
        self.assertFalse(joined)
        self.assertEqual(status["state"], "done")

    def test_an_exception_in_the_job_is_a_failed_state_not_a_dead_thread(self):
        jobs = wrapped_video.VideoJobs()

        def boom(progress):
            raise ValueError("secret detail /private/path")

        status, _ = jobs.start(2026, boom, wait=True)
        self.assertEqual(status["state"], "failed")
        self.assertIn("ValueError", status["reason"])
        self.assertNotIn("/private/path", status["reason"])

    def test_years_are_independent(self):
        jobs = wrapped_video.VideoJobs()
        self.assertEqual(jobs.status(2025)["state"], "idle")
        jobs.start(2026, lambda progress: {"ok": True, "path": "p"}, wait=True)
        self.assertEqual(jobs.status(2025)["state"], "idle")


class DataLayerTests(unittest.TestCase):
    def settings(self, tmp):
        return {"timelapse": {"wrapped_enabled": True, "wrapped_video": True},
                "paths": {"records_vault": str(tmp)}}

    def test_make_video_answers_202_and_joins_a_running_year(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = wrapped_video.VideoJobs()
            data = self.settings(tmp)
            gate = threading.Event()

            class Blocking(RecordingRunner):
                def __call__(self, command, **kwargs):
                    gate.wait(5)
                    return super().__call__(command, **kwargs)

            runner = Blocking()
            kwargs = dict(runner=runner, find_clips=lambda year: [], ffmpeg="ffmpeg", jobs=jobs)
            code, body = data_module.make_video(data, 2026, **kwargs)
            self.assertEqual(code, 202)
            self.assertFalse(body["joined"])
            code, body = data_module.make_video(data, 2026, **kwargs)
            self.assertEqual(code, 202)
            self.assertTrue(body["joined"])
            code, status = data_module.video_status(data, 2026, jobs=jobs)
            self.assertEqual(code, 200)
            self.assertIn(status["state"], ("queued", "running"))
            gate.set()
            for _ in range(200):
                if jobs.status(2026)["state"] in ("done", "failed"):
                    break
                threading.Event().wait(0.05)
            code, status = data_module.video_status(data, 2026, jobs=jobs)
            self.assertEqual(status["state"], "done")
            self.assertTrue(status["available"])
            self.assertEqual(len({d for d in runner.work_dirs}), 1)

    def test_wait_keeps_the_old_synchronous_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.settings(tmp)
            code, body = data_module.make_video(
                data, 2026, wait=True, jobs=wrapped_video.VideoJobs(),
                runner=RecordingRunner(), find_clips=lambda year: [], ffmpeg="ffmpeg")
            self.assertEqual(code, 200)
            self.assertTrue(Path(body["path"]).is_file())
            script = json.loads(Path(body["script_path"]).read_text())
            self.assertEqual(script["year"], 2026)

    def test_gates_and_bad_year_do_not_start_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = wrapped_video.VideoJobs()
            off = {"timelapse": {"wrapped_enabled": True, "wrapped_video": False},
                   "paths": {"records_vault": str(tmp)}}
            self.assertEqual(data_module.make_video(off, 2026, jobs=jobs)[0], 403)
            self.assertEqual(data_module.make_video(self.settings(tmp), "x", jobs=jobs)[0], 400)
            self.assertEqual(jobs.status(2026)["state"], "idle")
            self.assertEqual(data_module.video_status(self.settings(tmp), "x", jobs=jobs)[0], 400)

    def test_page_shows_progress_while_compiling_and_the_reason_after_a_failure(self):
        payload = {"year": 2026, "headline": {"total_prints": 1, "total_hours": 1,
                                              "total_filament_used_kg": 0.1,
                                              "success_rate": None},
                   "highlights": {"night_prints": 0, "failed": 0, "risk_flagged": 0,
                                  "worst_failure_streak": 0},
                   "script": [], "ai_video_enabled": True, "years_available": [2026]}
        running = {**payload, "video_job": {"state": "running", "step": 3, "steps": 12}}
        page = data_module.render_wrapped_page({}, running, form_token="t")
        self.assertIn("Step 3 of 12", page)
        self.assertIn("/api/wrapped/video-status", page)
        self.assertNotIn('action="/wrapped/video"', page)
        failed = {**payload, "video_job": {"state": "failed", "reason": "ffmpeg timed out"}}
        page = data_module.render_wrapped_page({}, failed, form_token="t")
        self.assertIn("ffmpeg timed out", page)
        self.assertIn('action="/wrapped/video"', page)


if __name__ == "__main__":
    unittest.main()
