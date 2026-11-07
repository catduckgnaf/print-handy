"""A print number names one print, not a job (#979 review).

Before this, every start of an archived job reused the job's number: a
"/print-again 84" was recorded, announced and summarized as #84 a second
time, and the history (records, Wrapped, the Data tab) carried duplicates.
These pin per-print numbering end to end:

- every start gets a new number; existing numbers never change and the
  counter continues from the highest ever issued;
- one start asks for its number from several places and gets one number;
- the /print-again list shows each print with its own number, "print 85"
  and "print 84" both reprint the job, and a name search is not ambiguous;
- a list taken while a job's first start is in flight cannot give that one
  print two numbers;
- records carry the print's own number, and readers that only know the job
  get its newest print (the one in progress or just finished);
- a dry-run-first repair gives already-duplicated history records new numbers.
"""
import json
import os
import threading
import time
from pathlib import Path

import pytest

from print_handy.core import model_job
from print_handy.hermes import discord_print_summary as summary
from print_handy.hermes import print_control as pc
from print_handy.hermes import print_records as records


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    path = tmp_path / "print_numbers.json"
    monkeypatch.setattr(pc, "PRINT_NUMBERS", path)
    return path


def _job(arch, folder, title, launch_age_s=600):
    d = arch / folder
    sd = d / "sliced" / "orca" / "run1"
    sd.mkdir(parents=True)
    gcode = sd / "plate_1.gcode"
    gcode.write_text("; gcode\nG1 X1\n")
    (d / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(gcode)}))
    rec = sd / "plate_1.launch.json"
    rec.write_text(json.dumps({"file": "plate_1.gcode"}))
    at = time.time() - launch_age_s
    os.utime(rec, (at, at))
    return d / "JOB.json"


# ---------------------------------------------------------------- allocation

def test_every_start_gets_a_new_number_and_old_numbers_never_change(registry, tmp_path):
    job = tmp_path / "a" / "JOB.json"
    other = tmp_path / "b" / "JOB.json"
    registry.write_text(json.dumps({"numbers": {str(job.resolve()): 84}}))  # the pre-#979 shape
    assert pc.allocate_print_number(job) == 85      # "print 84 again" is print #85
    assert pc.allocate_print_number(other) == 86
    assert pc.allocate_print_number(job) == 87
    assert pc.numbers_for_job(job) == [84, 85, 87]
    assert pc.latest_number_for_job(job) == 87
    # The legacy key still names the job's FIRST print.
    assert json.loads(registry.read_text())["numbers"][str(job.resolve())] == 84


def test_concurrent_starts_never_share_a_number(registry, tmp_path):
    got = []
    jobs = [tmp_path / f"j{i}" / "JOB.json" for i in range(8)]

    def start(job):
        got.append(pc.allocate_print_number(job))
    threads = [threading.Thread(target=start, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 9))


def test_one_start_asks_several_times_and_gets_one_number(registry, tmp_path, monkeypatch):
    arch = tmp_path / "archive"
    job = _job(arch, "ghost--a", "Ghost")
    monkeypatch.setattr(pc, "ARCHIVE", arch)
    model_job._LAUNCH_PRINT_NUMBERS.clear()
    first = model_job._assign_print_number(job)
    assert model_job._assign_print_number(job) == first          # the same start
    assert model_job._assign_print_number(job.parent) == first   # folder or JOB.json: one key
    model_job._LAUNCH_PRINT_NUMBERS.clear()                      # print_now: a new start
    assert model_job._assign_print_number(job) == first + 1


# ---------------------------------------------------------------- listing and lookup

def test_the_list_shows_each_print_and_numbers_resolve_to_their_job(registry, tmp_path):
    arch = tmp_path / "archive"
    panel = _job(arch, "panel--a", "Panel A Lower V21", launch_age_s=7200)
    _job(arch, "shield--b", "Shield Holder", launch_age_s=3600)
    rows = pc.printed_jobs(arch)            # backfills the two first prints: #1 Panel, #2 Shield
    assert [(r["index"], r["title"]) for r in rows] == [(2, "Shield Holder"), (1, "Panel A Lower V21")]
    pc.allocate_print_number(panel)         # Panel printed again: #3, its own number
    rows = pc.printed_jobs(arch)
    assert [(r["index"], r["title"]) for r in rows] == [
        (3, "Panel A Lower V21"), (2, "Shield Holder"), (1, "Panel A Lower V21")]
    assert "3. Panel A Lower V21" in pc.printed_list_text(rows)
    for n in ("1", "3", "#3"):
        assert pc.resolve_print(n, arch)["title"] == "Panel A Lower V21"
    # Two prints of one model are one answer to a name search, not an ambiguity.
    assert pc.resolve_print("panel a", arch)["title"] == "Panel A Lower V21"


def test_a_list_during_a_first_start_cannot_give_one_print_two_numbers(registry, tmp_path):
    arch = tmp_path / "archive"
    job = _job(arch, "new--a", "New Thing", launch_age_s=5)  # its launch record was just written
    assert [r["index"] for r in pc.printed_jobs(arch)] == [1]  # a list mid-start backfills #1
    assert pc.allocate_print_number(job) == 1                  # the start adopts it
    assert pc.allocate_print_number(job) == 2                  # the next start is a new print
    assert pc.numbers_for_job(job) == [1, 2]


# ---------------------------------------------------------------- records and readers

def test_a_record_carries_its_own_number_and_job_readers_get_the_newest(registry, tmp_path, monkeypatch):
    monkeypatch.setattr(records, "PRINTS_DIR", tmp_path / "Prints")
    job_dir = tmp_path / "archive" / "clip"
    job_dir.mkdir(parents=True)
    job = job_dir / "JOB.json"
    job.write_text(json.dumps({"title": "Clip"}))
    registry.write_text(json.dumps({"numbers": {str(job.resolve()): 84}}))
    assert pc.allocate_print_number(job) == 85
    ctx = {"job_key": "clip.gcode", "started_at": "2026-11-07T03:00:00Z",
           "slicer_settings": {"local_gcode": str(job_dir / "clip.gcode")}}
    # Without an explicit number a record names the job's NEWEST print, never its first.
    assert records.build_record_data("clip.gcode", stats={"state": "printing"}, context=ctx)["print_number"] == 85
    # record_start carries the exact number, which wins.
    ctx["print_number"] = 85
    path = records.write_start_record(ctx)
    assert Path(path).name == "print-0085.json"
    # The summary reader accepts the folder or the JOB.json (the folder form
    # used to miss every time: "Print #unknown").
    assert summary._print_number({"_job_path": str(job_dir)}) == 85
    assert summary._print_number({"_job_path": str(job)}) == 85


# ---------------------------------------------------------------- history repair

def _record(prints_dir, name, number, started, rid, status="completed", job_dir=None):
    data = {"print_number": number, "title": "Panel A Lower V21", "status": status,
            "started_at": started, "record_id": rid}
    if job_dir is not None:
        data["slicer_settings"] = {"local_gcode": str(job_dir / "plate_1.gcode")}
    (prints_dir / name).write_text(json.dumps(data))


def test_duplicate_numbers_in_history_are_found_and_renumbered_on_apply(registry, tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    prints.mkdir()
    monkeypatch.setattr(records, "PRINTS_DIR", prints)
    job_dir = tmp_path / "archive" / "panel"
    job_dir.mkdir(parents=True)
    (job_dir / "JOB.json").write_text("{}")
    registry.write_text(json.dumps({"numbers": {str((job_dir / "JOB.json").resolve()): 84}}))
    _record(prints, "print-0084.json", 84, "2026-11-06T02:42:00Z", "aaaa1111", job_dir=job_dir)
    _record(prints, "print-0084-bbbb2222.json", 84, "2026-11-07T03:28:00Z", "bbbb2222", job_dir=job_dir)
    _record(prints, "print-0084-cccc3333.json", 84, "2026-11-07T05:00:00Z", "cccc3333",
            status="printing", job_dir=job_dir)
    plan = records.renumber_duplicate_prints(apply=False)
    assert [(e["record_id"], e.get("skipped")) for e in plan] == [
        ("bbbb2222", None), ("cccc3333", "still printing; run again after it finishes")]
    assert all(e["new_number"] is None for e in plan)
    assert (prints / "print-0084-bbbb2222.json").exists()  # a dry run changes nothing

    plan = records.renumber_duplicate_prints(apply=True)
    fixed = next(e for e in plan if e["record_id"] == "bbbb2222")
    assert fixed["new_number"] == 85
    data = json.loads((prints / "print-0085.json").read_text())
    assert data["print_number"] == 85 and data["print_number_was"] == 84
    assert not (prints / "print-0084-bbbb2222.json").exists()
    assert json.loads((prints / "print-0084.json").read_text())["print_number"] == 84  # the first keeps it
    assert (prints / "print-0084-cccc3333.json").exists()                            # in flight: untouched
    assert pc.numbers_for_job(job_dir / "JOB.json") == [84, 85]
    assert records.renumber_duplicate_prints(apply=False)[0]["record_id"] == "cccc3333"
