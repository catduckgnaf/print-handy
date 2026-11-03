"""The opt-in start recorder (#843, #842) replayed against the recorded AD5X.

PRINT_HANDY_RECORD_STARTS=1 records the printer from the SET_ZCOLOR POST until the start settles.
These run the real print-now path over HTTP (tests/replay/conftest.py) with the recorder off and on,
and check that recording never changes the start, never posts, and exports fixture-shaped JSON.
No fixture file is written into the fixture tree: exports go to tmp_path.
"""
import json
import re
import time

import pytest

from print_handy import settings
from print_handy.core import launch_state, model_job, start_recorder

from .helpers import intake_outcome, make_job, recorded, run_print_now
from .test_fixtures_scrubbed import FORBIDDEN
from .test_start_replay import printer_heats_then_gives_up, printer_starts

QUERY_MARK = "zmod_color&idle_timeout&virtual_sdcard&webhooks"  # only the recorder asks for this set


def leases():
    return launch_state.pending(model_job._job_printer_id(None))


def recordings():
    folder = settings.state_dir() / "start-recordings"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def lines_of(path):
    return start_recorder.read_recording(path)


def last_phase(tail):
    line = [ln for ln in tail.splitlines() if "MAPPED START READBACK " in ln][-1]
    return json.loads(line.split("MAPPED START READBACK ", 1)[1])["phase"]


@pytest.fixture
def spawned(monkeypatch):
    """Followers the recorder would have spawned (none really are in tests)."""
    calls = []
    monkeypatch.setattr(start_recorder, "_spawn_follower", lambda path: calls.append(path))
    monkeypatch.setattr(start_recorder, "INTERVAL_S", 0.05)  # 2 printer s at SCALE 1/20 would be 0.1 s
    return calls


@pytest.fixture
def recording_on(monkeypatch, spawned):
    monkeypatch.setenv(start_recorder.ENV, "1")
    return spawned


def recorder_requests(fake):
    return [(m, p, q) for m, p, q, _b in fake.requests
            if p == "/printer/objects/query" and QUERY_MARK in "&".join(n for n, _v in q)]


# --- off by default ---------------------------------------------------------------------------------

def test_the_recorder_is_off_by_default_and_writes_nothing(ad5x, tmp_path, monkeypatch, spawned):
    monkeypatch.delenv(start_recorder.ENV, raising=False)
    assert start_recorder.enabled() is False
    assert start_recorder.enabled({start_recorder.ENV: "0"}) is False
    assert start_recorder.enabled({start_recorder.ENV: "1"}) is True
    ad5x.script_hook = printer_starts(after_s=2)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert intake_outcome(ok, tail)[0] == "STARTED", tail
    assert recordings() == [] and not (settings.state_dir() / "start-recordings").exists()
    assert recorder_requests(ad5x) == [] and "START RECORDING" not in tail
    assert spawned == []


# --- on: a successful start --------------------------------------------------------------------------

def test_a_recorded_successful_start_covers_post_samples_store_lease_and_outcome(ad5x, tmp_path, recording_on):
    ad5x.gcode_store.append({"message": "// before the start", "time": time.time()})
    starts = printer_starts(after_s=4, heat_after_s=1)

    def hook(fake, script):
        if script.startswith("SET_ZCOLOR"):
            fake.gcode_store.append({"message": "// SET_ZCOLOR mapping applied", "time": time.time()})
        return starts(fake, script)
    ad5x.script_hook = hook
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert intake_outcome(ok, tail)[0] == "STARTED" and last_phase(tail) == "executing", tail
    (path,) = recordings()
    assert f"START RECORDING: {path}" in tail
    lines = lines_of(path)
    header = lines[0]
    assert header["kind"] == "header" and header["method"] == "SET_ZCOLOR" and header["native"] is True
    assert header["command"] == ad5x.zcolor()[0] and header["remote"] == ad5x.uploads[0]
    assert header["delivery_suffix"] == ".gcode" and header["display_off"] == 1
    assert "zmod_color" in header["query"] and all(k in line for line in lines for k in ("mono", "wall", "t_s"))
    kinds = [line["kind"] for line in lines]
    assert kinds.index("post_sent") < kinds.index("receipt")
    receipt = next(line for line in lines if line["kind"] == "receipt")
    assert receipt["http_status"] == 200 and receipt["receipt"] == {"result": "ok"}
    samples = [line for line in lines if line["kind"] == "sample" and line["ok"]]
    assert samples and all("zmod_color" in s["response"]["result"]["status"] for s in samples)
    monos = [s["mono"] for s in samples]
    assert monos == sorted(monos)
    # #848 watermark: only what the store gained after the POST is recorded.
    store = [e["message"] for line in lines if line["kind"] == "gcode_store" for e in line["entries"]]
    assert "// SET_ZCOLOR mapping applied" in store and "// before the start" not in store
    assert [line["phase"] for line in lines if line["kind"] == "lease"] == ["submitting", "released"]
    assert next(line for line in lines if line["kind"] == "outcome")["outcome"] == "printing"
    assert kinds[-1] == "end" and recording_on == []  # settled in-process: no follower
    summary = json.loads(path.with_name(path.name.replace(".jsonl", ".summary.json")).read_text())
    assert summary["outcome"] == "printing" and summary["start_path_outcome"] == "printing"
    assert summary["handed_off"] is False and summary["bang_lines"] == []
    assert summary["lease_phases"] == ["submitting", "released"]


# --- on: the abandoned start, and the same outcome with the recorder off -----------------------------

def run_abandoned(ad5x, tmp_path, name):
    ad5x.script_hook = printer_heats_then_gives_up(prep_s=60)
    job, _ = make_job(tmp_path, name, title=f"Replay {name} Part")
    before = len(ad5x.requests)
    ok, tail = run_print_now(job, wait_s=6)
    posts = [(m, p) for m, p, _q, _b in ad5x.requests[before:] if m != "GET"]
    return intake_outcome(ok, tail), posts, [row["phase"] for row in leases()], tail


def test_an_abandoned_start_records_the_same_outcome_as_with_the_recorder_off(ad5x, tmp_path, monkeypatch, spawned):
    monkeypatch.delenv(start_recorder.ENV, raising=False)
    off_outcome, off_posts, off_leases, off_tail = run_abandoned(ad5x, tmp_path, "off")
    assert recordings() == []

    monkeypatch.setenv(start_recorder.ENV, "1")
    on_outcome, on_posts, on_leases, on_tail = run_abandoned(ad5x, tmp_path, "on")

    assert off_outcome[0] == "START_REFUSED" and "went back to standby without printing" in off_tail
    assert on_outcome == off_outcome and on_leases == off_leases == []
    # The recorder added GETs only: the POSTs of both runs are the same requests.
    assert [p for _m, p in on_posts] == [p for _m, p in off_posts]
    assert len(ad5x.zcolor()) == 2
    (path,) = recordings()
    lines = lines_of(path)
    assert next(line for line in lines if line["kind"] == "outcome")["outcome"] == "abandoned"
    heated = [s for s in lines if s["kind"] == "sample" and s["ok"]
              and s["response"]["result"]["status"]["extruder"]["target"] > 0]
    assert heated, "the recording holds the preparation the printer gave up on"
    summary = start_recorder.summarize(lines)
    assert summary["outcome"] == "abandoned" and summary["first_heater_target_s"] is not None
    assert summary["lease_phases"] == ["submitting", "released"]
    # The start path can see standby between two polls; the follower then captures the settled frame.
    assert spawned in ([], [path])


@pytest.mark.parametrize("record", [False, True], ids=["off", "on"])
def test_a_successful_start_reads_the_same_with_the_recorder_off_and_on(ad5x, tmp_path, monkeypatch, spawned, record):
    if record:
        monkeypatch.setenv(start_recorder.ENV, "1")
    else:
        monkeypatch.delenv(start_recorder.ENV, raising=False)
    ad5x.script_hook = printer_starts(after_s=2)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert ok and intake_outcome(ok, tail) == ("STARTED", "") and last_phase(tail) == "executing", tail
    assert leases() == [] and len(ad5x.zcolor()) == 1 and len(ad5x.uploads) == 1
    assert len(recordings()) == (1 if record else 0)
    posts = [p for m, p, _q, _b in ad5x.requests if m != "GET"]
    assert posts.count("/printer/gcode/script") == len(ad5x.scripts)


# --- never a POST; hand-off to the follower -----------------------------------------------------------

def test_the_recorder_and_its_follower_only_ever_send_gets(moonraker, tmp_path, monkeypatch, spawned):
    monkeypatch.setenv(start_recorder.ENV, "1")
    monkeypatch.setattr(settings, "state_dir", lambda data=None: tmp_path / "state")
    st = {"moonraker_url": moonraker.url, "display_off": 1}
    rec = start_recorder.begin(st, "Part_abc.gcode", command="SET_ZCOLOR FILENAME=Part_abc.gcode", method="SET_ZCOLOR",
                               native=True, watermark=(0, None), store_after=model_job._entries_after_watermark)
    time.sleep(0.3)
    rec.finish("preparing", phase="preparing")             # unsettled: handed to a follower
    assert spawned == [rec.path]
    moonraker.heat()
    moonraker.after(0, lambda: moonraker.start_printing("Part_abc.gcode"))
    follower = start_recorder.follow(rec.path, interval_s=0.05)

    assert follower.observed == "printing"
    assert moonraker.requests and {m for m, _p, _q, _b in moonraker.requests} == {"GET"}
    summary = json.loads(rec.summary_path.read_text())
    assert summary["handed_off"] is True and summary["outcome"] == "printing"
    assert summary["start_path_outcome"] == "preparing" and summary["samples"] >= 2
    kinds = [line["kind"] for line in lines_of(rec.path)]
    assert kinds.index("handoff") < kinds.index("follower") < kinds.index("end")


def test_a_start_still_preparing_when_print_now_returns_is_handed_to_the_follower(ad5x, tmp_path, recording_on):
    def hook(fake, script):
        if script.startswith("SET_ZCOLOR"):
            fake.heat()  # keeps heating past the wait: print-now returns "preparing"
        return 0
    ad5x.script_hook = hook
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job, wait_s=3)

    assert last_phase(tail) == "preparing", tail
    (path,) = recordings()
    assert recording_on == [path]
    lines = lines_of(path)
    handoff = next(line for line in lines if line["kind"] == "handoff")
    assert handoff["state"]["prep_seen"] is True and handoff["state"]["outcome"] == "preparing"
    assert [line["phase"] for line in lines if line["kind"] == "lease"] == ["submitting", "uncertain"]


def test_a_recorder_failure_never_reaches_the_start_path(ad5x, tmp_path, recording_on, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(start_recorder.StartRecorder, "_write", broken)
    ad5x.script_hook = printer_starts(after_s=2)
    job, _ = make_job(tmp_path)

    ok, tail = run_print_now(job)

    assert intake_outcome(ok, tail)[0] == "STARTED", tail


# --- export ----------------------------------------------------------------------------------------

def test_export_writes_fixture_shaped_scrubbed_json(ad5x, tmp_path, recording_on):
    ad5x.script_hook = printer_starts(after_s=4, heat_after_s=1)
    job, _ = make_job(tmp_path)
    ok, tail = run_print_now(job)
    assert ok, tail
    (path,) = recordings()
    out_dir = tmp_path / "export"

    with pytest.raises(SystemExit, match="not 'abandoned'"):
        start_recorder.export_fixture(path, "prep_aborted", out_dir)
    out = start_recorder.export_fixture(path, "preparing", out_dir)

    assert out == out_dir / "objects_query.preparing.json"
    doc = json.loads(out.read_text())
    assert doc["recorded"]["outcome"] == "printing" and doc["recorded"]["display_off"] == 1
    assert doc["recorded"]["start_command"].startswith("SET_ZCOLOR FILENAME=Replay-Part-")
    # Each frame is one response in the recorded snapshot's own shape, plus its time offset.
    snapshot = recorded("objects_query.printing.json")
    assert set(snapshot) == {"result"} and set(snapshot["result"]) == {"eventtime", "status"}
    for frame in doc["frames"]:
        assert set(frame) == {"t_s", "result"} and set(frame["result"]) == set(snapshot["result"])
        status = frame["result"]["status"]
        assert {"print_stats", "extruder", "heater_bed", "zmod_color", "idle_timeout"} <= set(status)
        for key in ("print_stats", "extruder", "heater_bed", "zmod_color", "idle_timeout"):
            assert set(status[key]) == set(snapshot["result"]["status"][key]), key
        assert status["print_stats"]["filename"] in ("", None) or "Replay-Part-" in status["print_stats"]["filename"]
    printing = [f for f in doc["frames"] if f["result"]["status"]["print_stats"]["state"] == "printing"]
    assert printing and printing[0]["result"]["status"]["virtual_sdcard"]["file_path"].startswith("/usr/data/gcodes/Replay-Part-")
    text = out.read_text()
    real_sha = re.search(r"_([0-9a-f]{64})\.gcode", ad5x.uploads[0]).group(1)
    assert real_sha not in text
    for pattern, what in FORBIDDEN:
        assert not pattern.search(text), what
    # The CLI prints the README row the issue asks for.
    start_recorder.main(["export", str(path), "--fixture", "preparing", "--out", str(tmp_path / "cli")])
    assert (tmp_path / "cli" / "objects_query.preparing.json").is_file()


def _status(state, filename="", target=0.0):
    return {"print_stats": {"state": state, "filename": filename},
            "extruder": {"target": target}, "heater_bed": {"target": 0.0}}


def test_a_stale_terminal_job_before_the_start_is_not_this_starts_outcome():
    seen = start_recorder._Observer("new.gcode")
    # The previous job was cancelled; the generic route allows a start after it.
    assert seen.see(_status("cancelled", "old.gcode")) is None
    assert seen.see(_status("cancelled", "old.gcode", target=200)) is None
    assert seen.see(_status("error", "new.gcode")) == "error"
    again = start_recorder._Observer("new.gcode")
    assert again.see(_status("cancelled", "old.gcode")) is None
    assert again.see(_status("standby")) is None
    assert again.see(_status("cancelled", "old.gcode")) == "cancelled"  # reached after leaving it


def test_a_settled_outcome_without_a_settled_frame_is_handed_to_the_follower(tmp_path):
    spawned = []
    rec = start_recorder.StartRecorder(tmp_path / "r.jsonl", "http://printer.invalid", "a.gcode",
                                       native=False, header={"command": "print/start a.gcode"},
                                       spawn=lambda path: spawned.append(path))
    rec.finish("printing")
    kinds = [line["kind"] for line in lines_of(rec.path)]
    assert spawned == [rec.path] and "handoff" in kinds
    # Its frames never reached printing, so it is no "preparing" fixture.
    with pytest.raises(SystemExit, match="frames reach None"):
        start_recorder.export_fixture(rec.path, "preparing", tmp_path / "out")


def test_a_generic_start_command_is_scrubbed():
    names = start_recorder._Names()
    text = start_recorder._scrub_command("print/start Confidential-Project.gcode", names)
    assert "Confidential" not in text and text.startswith("print/start Replay-Part-1_plate_1_")
    native = start_recorder._scrub_command("SET_ZCOLOR FILENAME=3mf_print/Secret.gcode.3mf", names)
    assert "Secret" not in native and "FILENAME=3mf_print/Replay-Part-2_plate_1_" in native
