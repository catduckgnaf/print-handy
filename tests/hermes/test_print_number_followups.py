"""Per-launch print numbers, follow-ups after #988.

- A reader that only knows the job (a listener render, a confirmation) gets
  the job's NEWEST print, and accepts the job folder or its JOB.json (the
  folder form used to miss every time: "Print number unknown").
- A record without its own number takes the number minted for ITS launch,
  never the job's first print's (a reprint is not the original again).
- Concurrent starts, and a /print-again list during a start, never share or
  drop a number: the registry is locked.
- A dry-run-first repair gives history records that were duplicated before
  per-launch numbering their own numbers, reusing one the registry already
  holds for that launch.
"""
import json
import threading
from datetime import datetime
from pathlib import Path

import pytest

from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import print_control as pc
from print_handy.hermes import print_records as records


def _ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    path = tmp_path / "print_numbers.json"
    monkeypatch.setattr(pc, "PRINT_NUMBERS", path)
    return path


@pytest.fixture()
def job(tmp_path):
    d = tmp_path / "archive" / "panel"
    d.mkdir(parents=True)
    (d / "JOB.json").write_text(json.dumps({"title": "Panel"}))
    return d / "JOB.json"


# ---------------------------------------------------------------- readers

def test_job_readers_get_the_newest_print_from_folder_or_job_json(registry, job):
    registry.write_text(json.dumps({"numbers": {str(job.resolve()): 84}}))
    assert summary._print_number({"_job_path": str(job.parent)}) == 84   # legacy: first print
    pc.mint_launch_number(job.resolve(), at=_ts("2026-11-07T03:00:00Z"))   # "print 84 again"
    assert pc.latest_number_for_job(job) == 85
    assert summary._print_number({"_job_path": str(job.parent)}) == 85
    assert summary._print_number({"_job_path": str(job)}) == 85
    assert summary._print_number({"_job_path": str(job), "print_number": 84}) == 84  # its own wins


def test_a_record_without_its_own_number_takes_its_launchs_number(registry, job, tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    registry.write_text(json.dumps({"numbers": {str(job.resolve()): 84}}))
    pc.mint_launch_number(job.resolve(), at=_ts("2026-11-06T02:42:00Z"))  # first launch mints #85
    pc.mint_launch_number(job.resolve(), at=_ts("2026-11-07T03:28:00Z"))  # reprint mints #86

    def number(started):
        ctx = {"job_key": "panel.gcode", "started_at": started,
               "slicer_settings": {"local_gcode": str(job.parent / "panel.gcode")}}
        return records.build_record_data("panel.gcode", stats={"state": "printing"}, context=ctx)["print_number"]
    assert number("2026-11-07T03:28:30Z") == 86
    assert number("2026-11-06T02:42:10Z") == 85
    assert number("2026-11-09T00:00:00Z") is None  # a launch nobody minted: never another print's number


# ---------------------------------------------------------------- locking

def test_concurrent_starts_never_share_or_drop_a_number(registry, tmp_path):
    jobs = [tmp_path / f"j{i}" / "JOB.json" for i in range(12)]
    got = []

    def start(path):
        got.append(pc.mint_launch_number(path))
    threads = [threading.Thread(target=start, args=(p,)) for p in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 13))
    launches = json.loads(registry.read_text())["launches"]
    assert sorted(e["number"] for e in launches) == list(range(1, 13))
    assert not registry.with_suffix(".json.lock").exists()


def test_a_held_lock_blocks_another_holder_and_a_stale_one_is_broken(registry):
    lock = registry.with_suffix(".json.lock")
    with pc._RegistryLock(registry):
        with pc._RegistryLock(registry):  # re-entrant in one thread
            assert lock.exists()
        assert lock.exists()
    assert not lock.exists()
    lock.write_text("")
    with pytest.raises(TimeoutError):
        with pc._RegistryLock(registry, wait_s=0.2):
            pass
    with pc._RegistryLock(registry, wait_s=0.2, stale_s=0.0):  # a crashed holder's lock
        pass
    assert not lock.exists()


# ---------------------------------------------------------------- history repair

def _record(prints, name, number, started, rid, job, status="completed"):
    (prints / name).write_text(json.dumps({
        "print_number": number, "title": "Panel", "status": status, "started_at": started,
        "record_id": rid, "slicer_settings": {"local_gcode": str(job.parent / "panel.gcode")}}))


def test_duplicate_history_is_listed_then_renumbered_on_apply(registry, job, tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    prints.mkdir()
    monkeypatch.setattr(records, "PRINTS_DIR", prints)
    registry.write_text(json.dumps({"numbers": {str(job.resolve()): 84}, "launches": [
        {"number": 84, "job": str(job.resolve()), "artifact": "", "at": _ts("2026-11-06T02:42:00Z")},
        # a /print-again list already backfilled the reprint as #85
        {"number": 85, "job": str(job.resolve()), "artifact": "", "at": _ts("2026-11-07T03:28:00Z")},
        {"number": 86, "job": "/elsewhere/JOB.json", "artifact": "", "at": _ts("2026-11-07T04:00:00Z")},
    ]}))
    _record(prints, "print-0084.json", 84, "2026-11-06T02:42:00Z", "aaaa1111", job)
    _record(prints, "print-0084-bbbb2222.json", 84, "2026-11-07T03:28:05Z", "bbbb2222", job)
    _record(prints, "print-0084-cccc3333.json", 84, "2026-11-07T09:00:00Z", "cccc3333", job)
    _record(prints, "print-0084-dddd4444.json", 84, "2026-11-07T10:00:00Z", "dddd4444", job, status="printing")

    plan = records.renumber_duplicate_prints(apply=False)
    assert [e["record_id"] for e in plan] == ["bbbb2222", "cccc3333", "dddd4444"]
    assert plan[2]["skipped"].startswith("still printing")
    assert all(e["new_number"] is None for e in plan)
    assert (prints / "print-0084-bbbb2222.json").exists()  # a dry run changes nothing

    plan = records.renumber_duplicate_prints(apply=True)
    assert [e["new_number"] for e in plan] == [85, 87, None]  # #85 reused; #87 newly minted
    data = json.loads((prints / "print-0085.json").read_text())
    assert data["print_number"] == 85 and data["print_number_was"] == 84
    assert json.loads((prints / "print-0087.json").read_text())["print_number_was"] == 84
    assert json.loads((prints / "print-0084.json").read_text())["print_number"] == 84
    assert not (prints / "print-0084-bbbb2222.json").exists()
    assert (prints / "print-0084-dddd4444.json").exists()  # in flight: untouched
    assert 87 in [e["number"] for e in json.loads(registry.read_text())["launches"]]
    # Re-running only finds the in-flight print.
    assert [e["record_id"] for e in records.renumber_duplicate_prints(apply=False)] == ["dddd4444"]


def test_repair_cli_dry_run(registry, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    (tmp_path / "Prints").mkdir()
    assert records._renumber_cli(["renumber-duplicates"]) == 0
    assert "No duplicate print numbers." in capsys.readouterr().out
