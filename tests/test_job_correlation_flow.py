"""#1414: one job's slice, launch and listener lines carry the same correlation id.

Drives the three production entry points for one archived job:
``jobs.slice.slice_job``, ``launch.print_now.record_start`` and the
Moonraker ``Listener.on_status`` printing transition. A log handler captures
the bound ``job_correlation_id`` on every record each one emits. Two jobs
started back to back keep separate ids.
"""
import asyncio
import json
import logging

import pytest

from print_handy import logs
from print_handy.core import job_correlation
from print_handy.jobs import slice as slice_mod
from print_handy.launch import print_now
from print_handy.runtime import moonraker_event_listener as mel


class _Capture(logging.Handler):
    """Record the correlation id bound when each line is logged."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.ids = []

    def emit(self, record):
        self.ids.append(logs.bound_context().get("job_correlation_id"))


@pytest.fixture
def capture():
    handler = _Capture()
    root = logging.getLogger("print_handy")
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    logs.clear_context()
    yield handler
    root.removeHandler(handler)
    root.setLevel(old_level)
    logs.clear_context()


def _job(tmp_path, name, intake_id):
    folder = tmp_path / "archive" / name
    folder.mkdir(parents=True)
    gcode = folder / f"{name}.gcode"
    gcode.write_text("G1 X1\n")
    job = {"title": name, "intake_id": intake_id, "gcode": str(gcode.resolve())}
    (folder / "JOB.json").write_text(json.dumps(job))
    return folder / "JOB.json", gcode


def _slice(job_path, monkeypatch):
    """slice_job's wrapper binds the id; the slice body itself is stubbed."""
    seen = []

    def body(path, **kw):
        logging.getLogger("print_handy.jobs.slice").info("slicing %s", path)
        seen.append(logs.bound_context().get("job_correlation_id"))
        return {"ok": True}

    monkeypatch.setattr(slice_mod, "_slice_job", body)
    monkeypatch.setattr(slice_mod, "_accepted_slice_abrasive", lambda path: False)
    slice_mod.slice_job(job_path)
    return seen


def _launch(job_json, gcode, monkeypatch):
    """record_start binds the id around the real start-record write."""
    seen = []

    def body(gcode_path, *a, **kw):
        logging.getLogger("print_handy.core.model_job").info("start recorded %s", gcode_path)
        seen.append(logs.bound_context().get("job_correlation_id"))
        return {"ok": True}

    monkeypatch.setattr(print_now, "_record_start_with_context", body)
    job = json.loads(job_json.read_text())
    monkeypatch.setattr(print_now, "_job_record_for_gcode", lambda g: (job, job_json))
    print_now.record_start(str(gcode))
    return seen


def _listener(gcode, job, monkeypatch, tmp_path):
    """The printing transition binds the id from the job; the print end clears it."""
    context_path = tmp_path / f"ctx-{gcode.stem}.json"
    context_path.write_text(json.dumps({"jobs": {gcode.name: {"job_key": gcode.name, **(
        {"job_correlation_id": job["job_correlation_id"]} if job.get("job_correlation_id") else {})}}}))
    monkeypatch.setenv("PRINT_HANDY_CONTEXT_PATH", str(context_path))
    for name in ("post", "post_alert"):
        monkeypatch.setattr(mel, name, lambda *a, **kw: True)
    monkeypatch.setattr(mel, "run_announcer", lambda reason: None)
    monkeypatch.setattr(mel, "start_claimed", lambda name: False)
    monkeypatch.setattr(mel, "_buttonless_standalone", lambda: None)
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", tmp_path / "runout_pause.json")
    monkeypatch.setattr(mel, "load_runout_pause", lambda *a, **kw: None)
    monkeypatch.setattr(mel.announcer, "get_json", lambda path, timeout=10: {"result": {}})

    async def _no_reminder():
        return None
    monkeypatch.setattr(mel, "maintenance_cancel_reminder", _no_reminder)
    listener = mel.Listener()
    seen = []

    async def run():
        await listener.on_status({"state": "standby", "filename": gcode.name}, "test")
        await listener.on_status({"state": "printing", "filename": gcode.name}, "test")
        logging.getLogger("print_handy.runtime.moonraker_event_listener").info("printing %s", gcode.name)
        seen.append(logs.bound_context().get("job_correlation_id"))
        await asyncio.gather(*listener.background_tasks)
    asyncio.run(run())
    return seen


def test_one_jobs_slice_launch_and_listener_lines_share_one_id(tmp_path, monkeypatch, capture):
    job_json, gcode = _job(tmp_path, "rack", "a" * 64)
    sliced = _slice(job_json, monkeypatch)
    # The id the slice minted is in JOB.json, so the launch reuses it.
    stored = json.loads(job_json.read_text())["job_correlation_id"]
    launched = _launch(job_json, gcode, monkeypatch)
    listened = _listener(gcode, json.loads(job_json.read_text()), monkeypatch, tmp_path)
    assert stored == "a" * 64  # reused from intake, not a new value
    assert sliced == launched == listened == [stored]
    assert stored in capture.ids


def test_two_jobs_keep_separate_ids_through_slice_and_launch(tmp_path, monkeypatch, capture):
    first_json, first_gcode = _job(tmp_path, "rack", "a" * 64)
    second_json, second_gcode = _job(tmp_path, "lamp", None)  # no intake id: a fresh random one
    ids = []
    for job_json, gcode in ((first_json, first_gcode), (second_json, second_gcode)):
        ids += _slice(job_json, monkeypatch) + _launch(job_json, gcode, monkeypatch)
    assert ids[0] == ids[1] == "a" * 64
    assert ids[2] == ids[3] and ids[2] != ids[0]
    assert job_correlation.from_context({"job_correlation_id": ids[2]}) == ids[2]
    # Nothing stays bound after each entry point returns.
    assert "job_correlation_id" not in logs.bound_context()
