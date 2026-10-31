"""Timelapse delivery contract (#373): a printer-rendered Moonraker timelapse
must never vanish silently from a completion post.

Print #57 ("Print in Place Bag Clip", AD5X on ZMod/Klipper, Print Handy
2.1.4) completed with a summary post and no video, although timelapses had
attached before. The marker/archive code is byte-identical across 2.1.2 ->
2.1.4 and the working path below passes on both, so the loss sits in the
video leg's failure routes -- and every one of them used to post the bare
completion text with the reason only in the log (or nowhere):

- the listener restarted during the render wait: the pending-completion
  retry posted the text bare and the video was never retried;
- archiving on but no rendered video arrived within the wait (render
  failure, render-late, or a filename that drifted from the convention the
  matcher expects): bare text;
- the marker didn't match the active job: bare text.

These tests drive the REAL announcer + listener + matcher + archive chain
with only the transport edges faked (Moonraker JSON, the MP4 download, the
ssh executable, the final post capture). The route tests fail against the
pre-fix code and pass with the fix; the working-path and archive-off tests
pin behaviour that must not change.

CI posture: the route tests are about the marker/attach/no-silent-drop
logic, not the encode, so they serve dummy video bytes and stub only the
platform sizing encode — they EXECUTE in the slim offline CI environment
(no ffmpeg there). The working-path test keeps the real encode end to end
and skips where ffmpeg/ffprobe are absent, the #377 fixture pattern.
"""
import contextlib
import io
import os
import shutil
import subprocess
import time

import pytest

from print_handy.hermes import moonraker_discord_announcements as watcher
from print_handy.hermes import moonraker_event_listener as mel

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(256)

SHA = "aabbccddeeff00112233445566778899" * 2
GCODE = f"Print-in-Place-Bag-Clip_plate_1_{SHA}.gcode"
RENDERED = f"timelapse_{GCODE}_20261026_1712.mp4"
DRIFTED = f"timelapse_{GCODE}.mp4"  # no render timestamp: the matcher must refuse it


@pytest.fixture(autouse=True)
def _fresh_postprocess_claims():
    mel._POSTPROCESS_ACTIVE.clear()
    yield
    mel._POSTPROCESS_ACTIVE.clear()


class InlineThread:
    """Run the postprocess synchronously, like a thread that finished."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class DeadThread:
    """A thread that never runs: the process died before it could."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        pass

    def start(self):
        pass


def _tiny_mp4(path):
    proc = subprocess.run(
        [mel.FFMPEG_BIN, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=black:s=320x240:d=1", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return path.read_bytes()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake Moonraker + archive host around the real announcer/listener.

    Serves dummy video bytes (state["mp4_bytes"], replaceable): the sizing
    encode is stubbed in the route tests, so no real MP4 is needed for the
    delivery logic to run end to end.
    """
    state = {"phase": "printing", "listing": [RENDERED], "mp4_bytes": FAKE_MP4}
    logs, sent = [], []

    def fake_get_json(path, timeout=10):
        if path.startswith("/printer/objects/query"):
            printing = state["phase"] == "printing"
            return {"result": {"status": {
                "print_stats": {"state": state["phase"], "filename": GCODE,
                                "print_duration": 3600.0},
                "virtual_sdcard": {"is_active": printing, "file_size": 1000,
                                   "progress": 0.5 if printing else 1.0}}}}
        if path.startswith("/server/files/directory"):
            return {"result": {"files": [{"path": name, "modified": 1760000000.0}
                                        for name in state["listing"]]}}
        return {"result": {}}

    class Response:
        def read(self, size=-1):
            if self._done:
                return b""
            self._done = True
            return state["mp4_bytes"]

        def __enter__(self):
            self._done = False
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(watcher, "get_json", fake_get_json)
    monkeypatch.setattr(watcher.urllib.request, "urlopen",
                        lambda req, timeout=None: Response())
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "ann-state.json")
    monkeypatch.setattr(watcher, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_HOST", "archive.test")
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_DIR", "/archive/timelapses")
    monkeypatch.setattr(watcher, "TIMELAPSE_SSH_KEY", "")
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_POLL_SECONDS", 0.05)

    sandbox = tmp_path / "archive-sandbox"
    sandbox.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "ssh"
    shim.write_text(
        "#!/bin/sh\n"
        "for a; do last=\"$a\"; done\n"
        f"cmd=$(printf '%s' \"$last\" | sed 's|/archive/timelapses|{sandbox}|g')\n"
        "exec sh -c \"$cmd\"\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    monkeypatch.setattr(mel, "post", lambda message: sent.append(message) or True)
    monkeypatch.setattr(mel, "log", lambda line: logs.append(line))
    return state, sent, logs


def _complete(monkeypatch, thread_cls):
    """Run the start pass, flip to complete, run the completion pass with
    the postprocess thread behaving as `thread_cls` throughout."""
    monkeypatch.setattr(mel.threading, "Thread", thread_cls)
    mel.run_announcer("start")


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe on PATH")
def test_completion_post_carries_printer_rendered_timelapse(world, tmp_path):
    """The working path (#57's shape): Print Handy job, printer-rendered
    timelapse present -> ONE completion post carrying the MEDIA attachment.

    The one genuinely encode-verifying test: a real tiny MP4 through the
    real platform sizing. Skipped where ffmpeg/ffprobe are absent (#377
    pattern); every other test in this module runs everywhere.
    """
    state, sent, logs = world
    state["mp4_bytes"] = _tiny_mp4(tmp_path / "rendered.mp4")
    mel.run_announcer("start")
    state["phase"] = "complete"
    mel.run_announcer("completion")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any("MEDIA:" in m for m in sent):
        time.sleep(0.1)
    media = [m for m in sent if "MEDIA:" in m and ".mp4" in m]
    assert media, f"no MEDIA post; sent={sent!r} logs={logs!r}"
    assert "Print complete" in media[0]


def test_restart_during_render_wait_still_attaches_timelapse(world, monkeypatch):
    """The listener dies after the completion is armed but before the video
    post: the next pass must re-drive the video leg for the persisted job,
    not post the pending text bare (#373's silent route)."""
    state, sent, logs = world
    # The sizing encode is not this test's subject (the retry logic is);
    # stubbing it lets the route execute where no ffmpeg exists (CI).
    monkeypatch.setattr(mel, "platform_sized_video", lambda path: path)
    _complete(monkeypatch, DeadThread)
    state["phase"] = "complete"
    mel.run_announcer("completion")  # arms pending + marker; thread never runs
    assert not any("MEDIA:" in m for m in sent)
    mel._POSTPROCESS_ACTIVE.clear()  # the restart is a new process
    monkeypatch.setattr(mel.threading, "Thread", InlineThread)
    mel.run_announcer("restart")
    media = [m for m in sent if "MEDIA:" in m and ".mp4" in m]
    assert media, f"video lost across the restart; sent={sent!r} logs={logs!r}"
    # And the completion text itself was delivered exactly once, with the video.
    completions = [m for m in sent if "Print complete" in m]
    assert completions == media


@pytest.mark.parametrize("listing", [[], [DRIFTED]],
                         ids=["no-render-yet", "filename-convention-drift"])
def test_no_video_within_wait_posts_the_reason(world, monkeypatch, listing):
    """Archiving is on but no attachable video arrives (render-late race or
    a rendered name that drifted from the expected convention): the
    completion post itself says the timelapse is missing, never silence."""
    state, sent, logs = world
    state["listing"] = listing
    _complete(monkeypatch, InlineThread)
    state["phase"] = "complete"
    mel.run_announcer("completion")
    assert len(sent) == 1, f"sent={sent!r} logs={logs!r}"
    assert "Print complete" in sent[0]
    assert "Timelapse not attached" in sent[0]
    assert any("timelapse not rendered" in line for line in logs)


def test_archive_off_posts_text_without_a_timelapse_note(world, monkeypatch):
    """Archiving off is a configuration choice, not a failure: the bare
    completion text stays exactly as it was, with no missing-video note."""
    state, sent, logs = world
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_HOST", "")
    _complete(monkeypatch, InlineThread)
    state["phase"] = "complete"
    mel.run_announcer("completion")
    assert len(sent) == 1
    assert "Print complete" in sent[0]
    assert "Timelapse not attached" not in sent[0]
    assert "MEDIA:" not in sent[0]


def test_mismatched_marker_posts_the_reason(world, monkeypatch):
    """A marker that doesn't match the persisted active job: log AND a note
    in the completion post — the video was expected, so it can't just
    vanish."""
    state, sent, logs = world
    watcher.save_state({"job_key": "someone-elses-job.gcode"})

    def fake_main():
        print(f"TIMELAPSE_POSTPROCESS:{GCODE}")
        print("Print complete: Stranger")

    monkeypatch.setattr(mel.announcer, "main", fake_main)
    mel.run_announcer("completion")
    assert any("no matching active job" in line for line in logs)
    assert len(sent) == 1
    assert "Print complete" in sent[0]
    assert "Timelapse not attached" in sent[0]
