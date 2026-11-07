"""Integration tests for print naming (B), deterministic confirmations (C) and the receipt send fix (D).

Each test drives a real entry point: Listener.on_status, the announcer main(), and the print-feedback
plugin hook. The adapter mirrors DiscordAdapter.send(chat_id, content, reply_to, metadata) exactly.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace as S

import pytest

HERE = Path(__file__).resolve().parent
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import moonraker_event_listener as mel

HEX = re.compile(r"[0-9a-f]{32,}", re.I)
TITLE = "Stand for iPad with thick case"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


from print_handy.hermes import print_records as records  # noqa: E402
from print_handy.hermes import moonraker_discord_announcements as watcher  # noqa: E402
PLUGIN = HERE.parents[1] / "plugins/print-feedback/__init__.py"


@pytest.fixture
def job(tmp_path, monkeypatch):
    """Archive with a sliced two plate job, plus the hashed name Moonraker would report."""
    folder = tmp_path / "archive" / "stand--abc"
    out = folder / "sliced" / "orca" / "run1"
    out.mkdir(parents=True)
    rows = []
    for n, (parts, slot, est) in enumerate((("Stand", 1, "1h 30m 0s"), ("Clip", 2, "40m 0s")), 1):
        gcode = out / f"plate_{n}.gcode"
        gcode.write_text(f"; estimated printing time (normal mode) = {est}\n; total filament used [g] = 12.3\nG1 X1\n")
        rows.append({"gcode": str(gcode), "plate_id": n, "slot": slot, "part_keys": [f"k{n}"]})
    data = {"title": TITLE, "gcode": rows[0]["gcode"], "derived_plates": rows,
            "color_manifest": {"parts": [{"key": "k1", "name": "Stand"}, {"key": "k2", "name": "Clip"}]},
            "color_decisions": {"mode": "per_part", "choices": {
                "k1": {"slot": 1, "color": "Black", "material": "PLA"},
                "k2": {"slot": 2, "color": "White", "material": "PLA"}}}}
    (folder / "JOB.json").write_text(json.dumps(data))
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
    first = Path(rows[0]["gcode"])
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    return S(hashed=f"plate_1_{digest}.gcode", data=data, rows=rows)


def _drive_listener(monkeypatch, filename, transitions, *, claimed=False):
    posted = []
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: claimed)
    monkeypatch.setattr(mel, "log", lambda line: None)
    from print_handy.hermes import klipper_report
    monkeypatch.setattr(klipper_report, "report", lambda filename: "Klipper report stub")
    listener = mel.Listener()

    async def run():
        for state in transitions:
            await listener.on_status({"state": state, "filename": filename}, "test")
        await asyncio.gather(*listener.background_tasks)  # the cancel report posts in the background
    asyncio.run(run())
    return posted


def test_start_claim_matches_exact_moonraker_filename(tmp_path, monkeypatch, job):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({job.hashed: 1000}))
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    assert mel.start_claimed(f"gcodes/{job.hashed}", now=1001)
    assert not mel.start_claimed("other_" + "a" * 64 + ".gcode", now=1001)


def test_print_now_start_claim_normalizes_moonraker_filename(tmp_path, monkeypatch):
    from print_handy.core import model_job
    claims = tmp_path / "claims.json"
    monkeypatch.setattr(model_job, "START_CLAIMS", claims)
    remote = "plate_1_" + "a" * 64 + ".gcode"
    model_job.claim_start_announcement(remote, now=1000)
    assert json.loads(claims.read_text()) == {remote: 1000}
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    assert mel.start_claimed("gcodes/" + remote, now=1001)


def test_find_job_accepts_title_prefixed_moonraker_name(job):
    job_row, plate = summary.find_job("My-Stand_" + job.hashed)
    assert job_row["title"] == TITLE and plate["gcode"] == job.rows[0]["gcode"]
    assert summary.find_job("My-Stand_" + job.hashed.replace(job.hashed[9], "f" if job.hashed[9] != "f" else "e", 1))[0] is None


def test_find_job_accepts_3mf_package_name(job):
    """#564: a .gcode.3mf upload reports the package name; the embedded digest is the
    inner G-code's, so it binds to the same job and plate as the plain name."""
    packaged = "My-Stand_" + job.hashed[:-len(".gcode")] + ".gcode.3mf"
    job_row, plate = summary.find_job(packaged)
    assert job_row["title"] == TITLE and plate["gcode"] == job.rows[0]["gcode"]


def test_find_job_uses_exact_local_gcode_not_another_jobs_same_plate_name(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    stale = archive / "a-stale" / "sliced" / "orca" / "run"
    current = archive / "z-current" / "sliced" / "orca" / "run"
    for folder, title in ((stale, "Barista Buddy"), (current, "FUNKO Honeycomb Shelf")):
        folder.mkdir(parents=True)
        gcode = folder / "plate_1.gcode"
        gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n")
        (folder.parents[2] / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(gcode)}))
    monkeypatch.setattr(summary, "ARCHIVE", archive)
    job_row, plate = summary.find_job(str(current / "plate_1.gcode"))
    assert job_row["title"] == "FUNKO Honeycomb Shelf"
    assert plate["gcode"] == str(current / "plate_1.gcode")
    # Preparation must use the already-bound job/plate, never repeat a lookup by
    # generic `plate_1.gcode` and resurrect a prior model's Discord details.
    preparing = summary.format_preparation(job_row, plate)
    assert "FUNKO Honeycomb Shelf" in preparing
    assert "Barista Buddy" not in preparing


def test_listener_start_uses_model_title_not_hash(job, monkeypatch):
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"])
    assert len(posted) == 1
    text = posted[0]
    assert TITLE in text
    assert "Parts: Stand" in text and "Plate 1 of 2" in text
    assert "Slot 1: Black PLA" in text
    assert f"Estimated total: ~1h 35m (printing: 1h 30m)" in text and "Approx. finish:" in text and "ET" in text
    assert not HEX.search(text)


def test_listener_start_post_has_no_roast(job, monkeypatch):
    """The roast moved to the completion post: the start post never calls the generator."""
    calls = []
    monkeypatch.setattr(mel, "generate_roast", lambda *a, **kw: calls.append(a) or "Roast")
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"])
    assert len(posted) == 1 and "Roast" not in posted[0] and calls == []


def test_generate_roast_runs_by_default_and_stops_when_the_switch_is_off(monkeypatch, tmp_path):
    """[vision] roast_enabled: on by default, and the roast is local (#1002):
    no LLM call is made, endpoint or not; off means no roast at all."""
    calls = []
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: calls.append(a) or None)
    assert mel.roast_enabled() is True
    line = mel.generate_roast(TITLE)
    assert line and len(line.split()) <= 20 and '"' not in line
    assert calls == []  # never a model call
    monkeypatch.setitem(mel._config.cfg()["vision"], "roast_enabled", False)
    assert mel.roast_enabled() is False
    assert mel.generate_roast(TITLE) == ""


def _claimed_listener(monkeypatch, tmp_path, job, *, message_id="777", edit_ok=True, claim_ts=1000):
    from print_handy.hermes import print_alert_buttons as pab, print_next
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "start_messages.json")
    if message_id:
        print_next.record_start_message(job.hashed, message_id)
    monkeypatch.setattr(mel, "start_claim_time", lambda name, now=None: claim_ts)
    monkeypatch.setattr(mel, "NOTIFICATION_STATE", tmp_path / "notifications.json")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "bot-token")
    monkeypatch.setattr(mel.announcer, "DISCORD_CHAT_ID", "chan1")
    edits = []
    monkeypatch.setattr(pab, "edit_message", lambda token, chan, mid, text: edits.append((mid, text)) or edit_ok)
    return edits


def test_claimed_start_edits_the_originating_channel(job, tmp_path, monkeypatch):
    """P2-11: an intake that started in another channel/thread gets its warmup
    edit there, not in the configured announcement channel."""
    from print_handy.hermes import print_alert_buttons as pab, print_next
    monkeypatch.setattr(print_next, "START_MESSAGES", tmp_path / "start_messages.json")
    print_next.record_start_message(job.hashed, "888", chat_id="chan-other", thread_id="thread-1")
    monkeypatch.setattr(mel, "start_claim_time", lambda name, now=None: 1000)
    monkeypatch.setattr(mel, "NOTIFICATION_STATE", tmp_path / "notifications.json")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "bot-token")
    monkeypatch.setattr(mel.announcer, "DISCORD_CHAT_ID", "chan1")
    edits = []
    monkeypatch.setattr(pab, "edit_message",
                        lambda token, chan, mid, text: edits.append((chan, mid, text)) or True)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert posted == []
    assert len(edits) == 1
    assert edits[0][0] == "thread-1" and edits[0][1] == "888"


def test_claimed_start_edits_existing_message_and_posts_no_second_start(job, tmp_path, monkeypatch):
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert posted == []
    assert len(edits) == 1 and edits[0][0] == "777"
    text = edits[0][1]
    assert TITLE in text and "Approx. finish:" in text and "Warmup complete, now printing." in text
    assert not HEX.search(text)


def test_claimed_start_edit_names_the_printing_objects(job, tmp_path, monkeypatch):
    """P2-244: the unclaimed start post names the job's objects (the
    exclude-object line); the claimed start's warmup edit built its text
    from the summary alone and silently dropped them."""
    gcode = tmp_path / "plate_with_objects.gcode"
    gcode.write_text("EXCLUDE_OBJECT_DEFINE NAME=hinge_left\n"
                     "EXCLUDE_OBJECT_DEFINE NAME=hinge_right\nG1 X1\n")
    plate = dict(job.rows[0], gcode=str(gcode))
    monkeypatch.setattr(summary, "find_job",
                        lambda filename, archive=None, context_path=None: (job.data, plate))
    monkeypatch.setattr(mel, "post_exclude_objects", lambda filename: False)
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert posted == []
    assert len(edits) == 1
    assert "Objects still printing: hinge_left, hinge_right" in edits[0][1]
    assert "Warmup complete, now printing." in edits[0][1]


def test_claimed_start_warmup_edit_has_no_roast(job, tmp_path, monkeypatch):
    """The warmup-to-printing edit keeps the canonical start details and adds the
    warmup-complete line; the roast now belongs to the completion post."""
    calls = []
    monkeypatch.setattr(mel, "generate_roast", lambda *a, **kw: calls.append(a) or "Roast")
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert posted == []
    assert len(edits) == 1 and edits[0][0] == "777"
    text = edits[0][1]
    assert TITLE in text and "Parts: Stand" in text and "Approx. finish:" in text
    assert "Warmup complete, now printing." in text
    assert "Roast" not in text and calls == []


def test_replayed_claimed_start_edits_once(job, tmp_path, monkeypatch):
    """A reconnect replay of the same claimed transition is deduped, and the
    recorded start-message id is consumed, so the warmup edit happens once."""
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    first = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    replay = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert first == [] and replay == [] and len(edits) == 1
    assert mel.announce_warmup_done(job.hashed) is False
    assert len(edits) == 1


def test_claimed_start_edit_failure_posts_distinct_short_line(job, tmp_path, monkeypatch):
    edits = _claimed_listener(monkeypatch, tmp_path, job, edit_ok=False)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    # The intake's start post already exists; a failed edit must not add a trailing status line.
    assert len(edits) == 1 and posted == []


def test_claimed_start_without_message_id_posts_short_line_not_second_start(job, tmp_path, monkeypatch):
    edits = _claimed_listener(monkeypatch, tmp_path, job, message_id=None)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert edits == [] and posted == []


def test_unclaimed_start_still_gets_the_normal_post(job, tmp_path, monkeypatch):
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    monkeypatch.setattr(mel, "start_claim_time", lambda name, now=None: None)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=False)
    assert edits == [] and len(posted) == 1 and posted[0].startswith("Print started:")
    assert "Parts: Stand" in posted[0] and "Approx. finish:" in posted[0]


def test_reconnect_replay_of_claimed_start_does_not_repost_or_reedit(job, tmp_path, monkeypatch):
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    first = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    # A fresh listener after a websocket reconnect replays standby -> printing for the same job.
    replay = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert first == [] and replay == [] and len(edits) == 1


def test_timelapse_selection_requires_plugin_filename_and_orders_newest(monkeypatch):
    job = "stand.gcode"
    remote = "Slim-12oz-Can-Holder-Red-Bull_plate_1_" + "ab" * 32 + ".gcode"
    plugin = f"timelapse_{job}_20261011_1230.mp4"
    older = f"timelapse_{job}_20261011_1200.mp4"
    remote_video = f"timelapse_{remote}_20261011_1356.mp4"
    monkeypatch.setattr(watcher, "get_json", lambda path, **kw: {"result": [
        {"path": "other.mp4", "modified": 99},
        {"path": "stand.mp4", "modified": 50},
        {"path": plugin, "modified": 5},
        {"path": older, "modified": 2},
        {"path": f"timelapse_{Path(job).stem}_20261011_1230.mp4", "modified": 6},
        {"path": "timelapse_stand_extra_20261011_1300.mp4", "modified": 8},
        {"path": f"../{plugin}", "modified": 9},
        {"path": "timelapse_standing.gcode_20261011_1500.mp4", "modified": 7},
        {"path": remote_video, "modified": 4},
    ]})
    assert watcher._timelapse_matches_job(plugin, job)
    assert not watcher._timelapse_matches_job(f"timelapse_{Path(job).stem}_20261011_1230.mp4", job)
    assert watcher._timelapse_matches_job(remote_video, remote)
    assert not watcher._timelapse_matches_job(f"timelapse_{Path(remote).stem}_20261011_1356.mp4", remote)
    assert watcher._timelapse_candidates(job) == [plugin, older]


def test_timelapse_archive_name_is_collision_resistant_and_job_bound():
    source = "timelapse_stand.gcode_20261011_1230.mp4"
    one = watcher.timelapse_archive_name("stand.gcode", source, "2026-10-11T12:30:00Z")
    two = watcher.timelapse_archive_name("stand.gcode", "timelapse_stand.gcode_20261011_1245.mp4", "2026-10-11T12:30:00Z")
    assert one.endswith(".mp4") and one != two and "stand" in one
    assert "20261011-083000-0400" in one


def test_archive_timelapse_rejects_wrong_job_before_any_copy(monkeypatch):
    called = []
    monkeypatch.setattr(watcher, "_timelapse_matches_job", lambda video, job: False)
    monkeypatch.setattr(watcher.subprocess, "run", lambda *args, **kwargs: called.append(args))
    monkeypatch.setattr(watcher.urllib.request, "urlopen", lambda *args, **kwargs: called.append("http"))
    with pytest.raises(ValueError):
        watcher.archive_timelapse("stand.gcode", "other.mp4")
    with pytest.raises(ValueError):
        watcher.archive_timelapse("stand.gcode", "../timelapse_stand.gcode_20261011_1230.mp4")
    assert called == []


def test_archive_timelapse_downloads_http_then_publishes_local_file(tmp_path, monkeypatch):
    calls = []
    source = "timelapse_stand.gcode_20261011_1230.mp4"
    monkeypatch.setattr(watcher, "MEDIA_DIR", tmp_path / "cache")
    monkeypatch.setattr(watcher, "TIMELAPSE_SSH_KEY", "")
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_HOST", "archive.test")
    monkeypatch.setattr(watcher.shutil, "which", lambda name: name)

    class Response:
        def read(self, size):
            if self._done:
                return b""
            self._done = True
            return b"mp4"
        def __enter__(self):
            self._done = False
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        calls.append(("http", request.full_url))
        return Response()

    monkeypatch.setattr(watcher.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_DIR", "/archive/timelapses")
    monkeypatch.setattr(watcher.subprocess, "run", lambda cmd, **kwargs: calls.append(tuple(cmd)))
    result = watcher.archive_timelapse("stand.gcode", source, "2026-10-11T12:30:00Z")
    assert calls[0][0] == "http"
    assert calls[0][1].endswith("/server/files/timelapse/" + source)
    assert calls[1][0] == "ssh" and calls[1][-2] == "archive.test"
    assert "cat > " in calls[1][-1] and "printer.test" not in " ".join(calls[1])
    assert calls[2][-2] == "archive.test" and " mv " in calls[2][-1]
    assert result.startswith("/archive/timelapses/")
    assert not result.endswith(".partial")
    assert not list((tmp_path / "cache").glob("source-*.mp4"))


def test_timelapse_wait_retries_then_archives(monkeypatch):
    attempts = iter(([], ["timelapse_stand.gcode_20261011_1230.mp4"]))
    monkeypatch.setattr(watcher, "_timelapse_candidates", lambda key: next(attempts))
    monkeypatch.setattr(watcher, "archive_timelapse", lambda key, path, completed: "/archive/stand.mp4")
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_WAIT_SECONDS", 2)
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_POLL_SECONDS", 0.1)
    outcome = watcher.archive_completed_timelapse("stand.gcode")
    assert outcome.state == watcher.ARCHIVED
    assert outcome.path == "/archive/stand.mp4"


def test_timelapse_wait_is_bounded_and_failure_safe(monkeypatch):
    archived = []
    monkeypatch.setattr(watcher, "_timelapse_candidates", lambda key: [])
    monkeypatch.setattr(watcher, "archive_timelapse", lambda *a: archived.append(a))
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_POLL_SECONDS", 0.1)
    started = time.monotonic()
    outcome = watcher.archive_completed_timelapse("stand.gcode")
    assert outcome.state == watcher.ARCHIVE_NOT_RENDERED
    assert outcome.path is None
    assert time.monotonic() - started < 2
    assert archived == []

    def broken(key):
        raise OSError("moonraker down")

    monkeypatch.setattr(watcher, "_timelapse_candidates", broken)
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_WAIT_SECONDS", 0.3)
    started = time.monotonic()
    outcome = watcher.archive_completed_timelapse("stand.gcode")
    assert outcome.state == watcher.ARCHIVE_NOT_RENDERED
    assert "moonraker down" in outcome.detail
    assert time.monotonic() - started < 2


def test_format_event_includes_persistent_print_number(tmp_path, monkeypatch):
    from print_handy.hermes import print_control
    archive = tmp_path / 'archive'
    job_dir = archive / 'ghost'
    job_dir.mkdir(parents=True)
    gcode = job_dir / 'plate.gcode'
    gcode.write_text('; estimated printing time (normal mode) = 1h 0m\n; total filament used [g] = 12\n')
    job_path = job_dir / 'JOB.json'
    job_path.write_text(json.dumps({'title': 'Ghost Stand', 'gcode': str(gcode),
                                    'filament_selection': {'slot': 1, 'color': 'white', 'material': 'PLA'}}))
    numbers = tmp_path / 'print_numbers.json'
    numbers.write_text(json.dumps({'numbers': {str(job_path.resolve()): 7}}))
    monkeypatch.setattr(print_control, 'PRINT_NUMBERS', numbers)
    assert 'Print #7' in summary.format_event('complete', gcode.name, archive=archive)


def test_timelapse_lookup_error_retries_instead_of_giving_up(monkeypatch):
    attempts = iter((OSError("timed out"), ["timelapse_stand.gcode_20261011_1230.mp4"]))

    def flaky(key):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(watcher, "_timelapse_candidates", flaky)
    monkeypatch.setattr(watcher, "archive_timelapse", lambda key, path, completed: "/archive/stand.mp4")
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_WAIT_SECONDS", 2)
    monkeypatch.setattr(watcher, "TIMELAPSE_RENDER_POLL_SECONDS", 0.1)
    outcome = watcher.archive_completed_timelapse("stand.gcode")
    assert outcome.state == watcher.ARCHIVED
    assert outcome.path == "/archive/stand.mp4"


def test_timelapse_archive_off_skips_the_lookup_without_waiting(monkeypatch, capsys):
    # #540: with the archive destination unset the lookup is skipped
    # outright — no candidate search, no bounded wait — and the outcome
    # says so, so the completion flow can never describe it as a timeout.
    for host, directory in (("", "/archive/timelapses"), ("archive.test", ""), ("", "")):
        monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_HOST", host)
        monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_DIR", directory)

        def forbidden(key):
            raise AssertionError("the lookup must not run while the archive is off")

        monkeypatch.setattr(watcher, "_timelapse_candidates", forbidden)
        started = time.monotonic()
        outcome = watcher.archive_completed_timelapse("stand.gcode")
        assert outcome.state == watcher.ARCHIVE_OFF
        assert outcome.path is None
        assert time.monotonic() - started < 1
    assert "Timelapse archive is off" in capsys.readouterr().err


def test_timelapse_archive_failure_is_its_own_state(monkeypatch):
    # #540: a rendered video that fails to archive is neither "off" nor
    # "not rendered" — the outcome names the delivery failure.
    monkeypatch.setattr(watcher, "_timelapse_candidates",
                        lambda key: ["timelapse_stand.gcode_20261011_1230.mp4"])

    def broken(key, path, completed):
        raise OSError("ssh down")

    monkeypatch.setattr(watcher, "archive_timelapse", broken)
    outcome = watcher.archive_completed_timelapse("stand.gcode")
    assert outcome.state == watcher.ARCHIVE_FAILED
    assert outcome.path is None
    assert "ssh down" in outcome.detail


def test_local_timelapse_cache_retention_keeps_recent_and_non_video(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(watcher, "MEDIA_DIR", cache)
    monkeypatch.setattr(watcher, "TIMELAPSE_LOCAL_RETENTION_DAYS", 14)
    now = 1_800_000_000
    old, fresh, photo = cache / "old.mp4", cache / "fresh.mp4", cache / "print-complete-x.jpg"
    for path in (old, fresh, photo):
        path.write_bytes(b"x")
    os.utime(old, (now - 15 * 86400,) * 2)
    os.utime(fresh, (now - 86400,) * 2)
    os.utime(photo, (now - 90 * 86400,) * 2)
    assert watcher.prune_local_timelapses(now=now) == [old]
    assert not old.exists() and fresh.exists() and photo.exists()
    monkeypatch.setattr(watcher, "TIMELAPSE_LOCAL_RETENTION_DAYS", 0)
    os.utime(fresh, (now - 900 * 86400,) * 2)
    assert watcher.prune_local_timelapses(now=now) == []


def test_timelapse_marker_carries_exact_job_key(tmp_path, monkeypatch):
    remote = "Slim-12oz-Can-Holder-Red-Bull_plate_1_" + "ab" * 32 + ".gcode"
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    watcher.save_state({"job_key": f"gcodes/{remote}"})
    marker = watcher._timelapse_marker(f"gcodes/{remote}")
    assert marker == remote
    assert "_" in marker and " " not in marker
    assert watcher._timelapse_job_key(marker) == f"gcodes/{remote}"
    assert watcher._timelapse_job_key(Path(remote).stem) == ""
    assert watcher._timelapse_job_key("stand") == ""


class _InlineThread:
    def __init__(self, target=None, args=(), **kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_listener_attaches_local_timelapse_only_when_available(monkeypatch):
    posted = []
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or True)
    monkeypatch.setattr(mel.announcer, "main", lambda: print("TIMELAPSE_POSTPROCESS:stand.gcode\nPrint complete: Stand"))
    monkeypatch.setattr(mel.announcer, "_timelapse_job_key", lambda token: "stand.gcode" if token == "stand.gcode" else "")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED, "/archive/timelapses/stand.mp4"))
    monkeypatch.setattr(mel.threading, "Thread", _InlineThread)
    monkeypatch.setattr(mel.announcer, "materialize_archived_timelapse", lambda path: "/cache/stand.mp4")
    mel.run_announcer("test")
    assert posted == ["Print complete: Stand\nMEDIA:/cache/stand.mp4"]
    assert "MEDIA:/volume1" not in posted[0]
    posted.clear()
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVE_NOT_RENDERED))
    mel.run_announcer("test")
    # Archiving is configured in the test settings and the bounded wait
    # really elapsed, so a missing video is announced in the post instead
    # of vanishing silently (#373).
    assert posted == ["Print complete: Stand\nTimelapse not attached: the printer's rendered video "
                      "didn't arrive within the wait (see the listener log)."]


def test_listener_combines_completion_and_timelapse(monkeypatch):
    posted = []
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or True)
    monkeypatch.setattr(mel.announcer, "main", lambda: print("TIMELAPSE_POSTPROCESS:stand.gcode\nPrint complete: Stand"))
    monkeypatch.setattr(mel.announcer, "_timelapse_job_key", lambda token: "stand.gcode" if token == "stand.gcode" else "")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED, "/archive/timelapses/stand.mp4"))
    monkeypatch.setattr(mel.threading, "Thread", _InlineThread)
    monkeypatch.setattr(mel.announcer, "materialize_archived_timelapse", lambda path: "/cache/stand.mp4")
    mel.run_announcer("test")
    assert posted == ["Print complete: Stand\nMEDIA:/cache/stand.mp4"]


def test_listener_shrinks_video_and_never_drops_completion_on_rejected_upload(monkeypatch):
    posted, results = [], [False, True]
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "post", lambda message: posted.append(message) or results.pop(0))
    monkeypatch.setattr(mel.announcer, "main", lambda: print("TIMELAPSE_POSTPROCESS:stand.gcode\nPrint complete: Stand"))
    monkeypatch.setattr(mel.announcer, "_timelapse_job_key", lambda token: "stand.gcode")
    monkeypatch.setattr(mel.announcer, "archive_completed_timelapse",
                        lambda key: mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED, "/volume1/x/stand.mp4"))
    monkeypatch.setattr(mel.threading, "Thread", _InlineThread)
    monkeypatch.setattr(mel.announcer, "materialize_archived_timelapse", lambda path: "/cache/stand.mp4")
    monkeypatch.setattr(mel, "platform_sized_video", lambda path, max_bytes=None: "/cache/stand.sized.mp4")
    mel.run_announcer("test")
    assert posted[0] == "Print complete: Stand\nMEDIA:/cache/stand.sized.mp4"
    assert posted[1].startswith("Print complete: Stand\n") and "MEDIA:" not in posted[1]


def test_platform_sized_video_passes_small_files_through(tmp_path):
    small = tmp_path / "a.mp4"
    small.write_bytes(b"x" * 10)
    assert mel.platform_sized_video(small, max_bytes=100) == small


def test_materialize_archived_timelapse_uses_local_media_cache(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(watcher, "MEDIA_DIR", tmp_path / "cache")
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_DIR", "/archive/timelapses")
    monkeypatch.setattr(watcher, "TIMELAPSE_SSH_KEY", "")
    monkeypatch.setattr(watcher, "TIMELAPSE_ARCHIVE_HOST", "archive.test")
    monkeypatch.setattr(watcher.shutil, "which", lambda name: name)

    def fake_run(command, **kwargs):
        calls.append(command)
        kwargs["stdout"].write(b"mp4")

    monkeypatch.setattr(watcher.subprocess, "run", fake_run)
    result = watcher.materialize_archived_timelapse("/archive/timelapses/stand.mp4")
    assert result == tmp_path / "cache" / "stand.mp4"
    assert result.read_bytes() == b"mp4"
    assert calls[0][-2] == "archive.test"
    assert calls[0][-1] == "cat /archive/timelapses/stand.mp4"
    with pytest.raises(ValueError):
        watcher.materialize_archived_timelapse("/other/stand.mp4")


@pytest.mark.parametrize("final,verb", [("cancelled", "Print cancelled"), ("error", "Print stopped with an error"), ("complete", "Print complete")])
def test_listener_terminal_event_has_single_notification_owner(job, monkeypatch, final, verb):
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing", final])
    # The separate cancel maintenance nudge is not a terminal notification.
    posted = [p for p in posted if not p.startswith("A print was cancelled.")]
    if final == "complete":
        assert len(posted) == 1 and posted[0].startswith("Print started:")
    # Cancellation is one post: the cancelled summary leads the detailed Klipper
    # report (no separate ack). The maintenance nudge is distinct.
    elif final == "cancelled":
        assert len(posted) == 2 and verb in posted[1] and TITLE in posted[1]
    else:
        assert len(posted) == 2 and verb in posted[1] and TITLE in posted[1]
    assert not any(HEX.search(p) for p in posted)


def test_listener_unknown_file_still_never_posts_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "ARCHIVE", tmp_path / "empty")
    monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "none.json")
    name = "mystery_part_" + "ab" * 32 + ".gcode"
    posted = _drive_listener(monkeypatch, name, ["standby", "printing"])
    assert posted and not HEX.search(posted[0]) and "mystery part" in posted[0]


def test_announcer_completion_uses_model_title_not_hash(job, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {job.hashed: {"selected_option": records.NATIVE_PRINT_OPTIONS[0],
                                                              "discord_session_id": "s1", "discord_chat_id": "1"}}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    file_row = {"path": job.hashed, "display": job.hashed}
    responses = iter((
        {"job": {"file": file_row}, "progress": {"completion": 25}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": file_row}, "progress": {"completion": 100}, "state": "Completed",
         "stats": {"state": "complete", "print_duration": 30}},
    ))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "_queued_jobs", lambda: [])  # the original asked the live printer's queue
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {"slicer_settings": {"layer_height": 0.2}})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher.time, "time", lambda: 12345.0)
    assert watcher.main() == 0
    capsys.readouterr()
    assert watcher.main() == 0
    out = capsys.readouterr().out
    posted = "\n".join(line for line in out.splitlines() if not line.startswith("TIMELAPSE_POSTPROCESS:"))
    marker = next(line for line in out.splitlines() if line.startswith("TIMELAPSE_POSTPROCESS:"))
    assert marker == f"TIMELAPSE_POSTPROCESS:{job.hashed}"
    assert "Print complete" in posted and TITLE in posted
    assert "Print timelapse archived" not in posted
    assert "MEDIA:" not in posted and "Expected model" not in posted
    state = json.loads((tmp_path / "state.json").read_text())
    # #524: a completion no longer arms any agent-session reset.
    assert "reset_armed_at" not in state
    assert not HEX.search(posted)
    record = json.loads(next((tmp_path / "Prints").glob("*.json")).read_text())
    assert record["title"] == TITLE and record["status"] == "completed"


def test_announcer_records_the_roast_subject_and_never_calls_the_generator(job, tmp_path, monkeypatch, capsys):
    """The announcer only records what to roast; the listener generates the one
    roast when it delivers the completion post, after the timelapse."""
    roast_calls = []

    def fake_roast(title, summary=""):
        roast_calls.append((title, summary))
        return "Completion roast that must not appear"
    monkeypatch.setattr(mel, "generate_roast", fake_roast)
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    monkeypatch.setattr(records, "PLANS_DIR", tmp_path / "Plans")
    monkeypatch.setattr(watcher, "STATE_PATH", tmp_path / "state.json")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps({"jobs": {job.hashed: {"selected_option": records.NATIVE_PRINT_OPTIONS[0],
                                                              "discord_session_id": "s1", "discord_chat_id": "1"}}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    file_row = {"path": job.hashed, "display": job.hashed}
    responses = iter((
        {"job": {"file": file_row}, "progress": {"completion": 25}, "state": "Printing", "stats": {"state": "printing"}},
        {"job": {"file": file_row}, "progress": {"completion": 100}, "state": "Completed",
         "stats": {"state": "complete", "print_duration": 30}},
    ))
    monkeypatch.setattr(watcher, "get_job", lambda: next(responses))
    monkeypatch.setattr(watcher, "_queued_jobs", lambda: [])
    monkeypatch.setattr(watcher, "get_file_metadata", lambda filename: {"slicer_settings": {"layer_height": 0.2}})
    monkeypatch.setattr(watcher, "get_active_spool", lambda: {})
    monkeypatch.setattr(watcher.time, "time", lambda: 12345.0)
    assert watcher.main() == 0
    capsys.readouterr()
    assert watcher.main() == 0
    out = capsys.readouterr().out
    posted = "\n".join(line for line in out.splitlines() if not line.startswith("TIMELAPSE_POSTPROCESS:"))
    assert "Print complete" in posted and TITLE in posted
    assert "Completion roast that must not appear" not in posted
    assert roast_calls == []
    saved = json.loads(watcher.STATE_PATH.read_text())
    title, _summary = mel.announcer.pending_roast_subject(saved)
    assert title == TITLE
    # Bound to the exact pending completion text, and refused for any other.
    pending = mel.announcer.pending_completion_message(saved)
    assert mel.announcer.pending_roast_subject(saved, pending)[0] == TITLE
    assert mel.announcer.pending_roast_subject(saved, "Print complete: Another")[0] == ""


def test_print_record_title_resolves_from_hashed_name(job, monkeypatch):
    assert records._resolved_title(job.hashed) == TITLE


def test_slice_summary_is_deterministic_and_complete(job):
    from datetime import datetime
    now = datetime(2026, 10, 6, 21, 0, tzinfo=summary.ET)
    first = summary.format_slice_summary(job.data, now=now)
    assert first == summary.format_slice_summary(job.data, now=now)
    lines = first.splitlines()
    assert lines[0] == f"Sliced: {TITLE}"
    assert lines[1] == "Plate 1: Stand | Black PLA, slot 1 | 12.3 g | 1h 30m"
    assert lines[2] == "Plate 2: Clip | White PLA, slot 2 | 12.3 g | 40m"
    assert lines[3] == "Total filament: 24.6 g"
    assert lines[4] == "Estimated total: ~2h 15m (printing: 2h 10m)"
    assert lines[5] == "Approx. finish if started now: 11:15 PM Tue ET"
    assert not HEX.search(first)
    assert summary.plate_payload("started", job.hashed) == {
        "model": TITLE, "plate": 1, "parts": ["Stand"], "slot": 1,
        "color": "Black", "material": "PLA", "filament_g": 12.3,
        "estimated_seconds": 5400,
    }


def test_slice_summary_includes_auto_scale(job):
    job.data['auto_scale'] = {'factor': 25.4, 'reason': 'inch units',
                              'original_mm': [2, 3, 4], 'final_mm': [50.8, 76.2, 101.6]}
    text = summary.format_slice_summary(job.data)
    assert 'Auto-scale: 2 x 3 x 4 mm treated as inches, scaled 25.4x -> 50.8 x 76.2 x 101.6 mm' in text


def test_slice_summary_includes_confirmed_mm_units(job):
    job.data['unit_provenance'] = {'units': 'mm', 'source': 'user_confirmation',
                                   'original_mm': [5, 2, 2], 'factor': 1.0}
    text = summary.format_slice_summary(job.data)
    assert 'Units: millimeters confirmed, printed at authored size 5 x 2 x 2 mm' in text
    assert 'Auto-scale' not in text


# ---- D: the print-feedback plugin against the real DiscordAdapter.send signature ----

class StrictAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)


def test_receipt_hook_sends_with_content_keyword(monkeypatch):
    plugin = _load("print_feedback_msgs", HERE.parents[1] / "plugins/print-feedback/__init__.py")
    adapter = StrictAdapter()
    src = S(platform=S(value="discord"), profile="3dprinting", user_id="42", chat_id="c", thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter)
    event = S(source=src, text="Approved. Bed is clear.", message_id="m1", internal=False)
    errors = []
    monkeypatch.setattr(plugin.logger, "exception", lambda *a, **k: errors.append(a))
    asyncio.run(plugin.receive_ack(event, gateway))
    assert errors == []
    assert adapter.sent and "bed-clear" in adapter.sent[0]


def test_every_plugin_and_intake_send_uses_content_keyword():
    """Static guard on the real sources: no adapter.send(text=...) anywhere in the call paths."""
    core = HERE.parents[1] / "print_handy/core"
    for path in (PLUGIN, core / "link_intake.py", core / "prepared_intake.py"):
        source = path.read_text()
        for call in re.findall(r"adapter\.send\((?:[^()]|\([^()]*\))*\)", source):
            assert "text=" not in call, f"{path.name}: {call[:80]}"


def test_model_link_receipt_is_sent_before_slow_intake(monkeypatch):
    """A dropped link gets no receipt; the preview and picker follow."""
    import types
    plugin = _load("print_feedback_ackfirst", HERE.parents[1] / "plugins/print-feedback/__init__.py")
    adapter = StrictAdapter()
    order = []

    async def fake_prepare(event, adapter_, state, root, reuse=None):
        order.append(("intake", list(adapter_.sent)))
        return {"action": "skip", "reason": "test"}

    fake = types.ModuleType("prepared_intake")
    fake.prepare_link = fake_prepare
    fake.model_file = lambda e: None
    fake.reprint_request = lambda t: False
    fake.last_printed_job = lambda printer_id=None: None
    import print_handy.core as core_pkg
    monkeypatch.setitem(sys.modules, "print_handy.core.prepared_intake", fake)
    monkeypatch.setattr(core_pkg, "prepared_intake", fake, raising=False)
    monkeypatch.setattr(core_pkg, "printer_state", types.SimpleNamespace(load=lambda: {}), raising=False)
    src = S(platform=S(value="discord"), profile="3dprinting", user_id="42", chat_id="c", thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: "k")
    event = S(source=src, text="https://makerworld.com/en/models/123", message_id="m9", internal=False, metadata={})
    asyncio.run(plugin.model_link_dispatch(event, gateway))
    # No "Got the link" receipt: the model preview and color picker are the acknowledgement.
    assert order and order[0][1] == []
    # receive_ack must not double-send for the same handled message.
    asyncio.run(plugin.receive_ack(event, gateway))
    assert adapter.sent == []


# --- #391 expansion, after #1002: generate_roast's fail-soft branches ---
#
# #374 made the roast best-effort: one roast per print, and a roast problem
# never breaks the post it decorates. Since #1002 the roast is local, so
# these pin the local refusals and that no branch ever reaches a model.


def _exploding_network(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("the roast must never make a network call")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    from print_handy import ai_routes
    monkeypatch.setattr(ai_routes, "call", boom)


def test_generate_roast_refuses_an_empty_title(monkeypatch, tmp_path):
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setattr(mel, "log", lambda line: None)
    _exploding_network(monkeypatch)
    assert mel.generate_roast("") == ""
    assert mel.generate_roast("   ") == ""
    assert mel.generate_roast(None) == ""


def test_generate_roast_needs_no_key_and_no_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "")
    monkeypatch.setattr(mel._config, "service_url", lambda name: "")
    _exploding_network(monkeypatch)
    assert mel.generate_roast(TITLE)


def test_generate_roast_with_a_configured_endpoint_still_never_calls_it(monkeypatch, tmp_path):
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-key")
    monkeypatch.setattr(mel._config, "service_url", lambda name: "http://llm.test/chat")
    _exploding_network(monkeypatch)
    assert mel.generate_roast(TITLE)


def test_generate_roast_survives_a_broken_state_file(monkeypatch, tmp_path):
    state = tmp_path / "roast.json"
    state.write_text("{not json")
    monkeypatch.setattr(mel, "ROAST_STATE", state)
    monkeypatch.setattr(mel, "log", lambda line: None)
    assert mel.generate_roast(TITLE)


def test_generate_roast_same_key_same_line(monkeypatch, tmp_path):
    """A retried or replayed delivery of one print repeats its line."""
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    monkeypatch.setattr(mel, "log", lambda line: None)
    first = mel.generate_roast(TITLE, facts={"duration_s": 3600}, key="job@1")
    assert first and all(mel.generate_roast(TITLE, key="job@1") == first for _ in range(5))
