"""print_control: resume, print again, force restart. Offline; no printer traffic."""
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from print_handy.runtime import print_control
from print_handy.core import model_job

OLD = "G4-Instant-Desk-Stand_plate_1_" + "e5" * 32 + ".gcode"
NEW = "G4-Instant-Desk-Stand_plate_1_" + "10" * 32 + ".gcode"


def _job(tmp_path, *, current="new"):
    """The 2026-10-10 desk stand shape: an old launched slice, then a re-slice bound in JOB.json."""
    run = tmp_path / "g4-instant-desk-stand--abc"
    old = run / "sliced/orca/a15cf"
    new = run / "sliced/orca/c13c"
    for d, name in ((old, OLD), (new, NEW)):
        d.mkdir(parents=True)
        (d / "plate_1.gcode").write_text(f";{name}\nG1 X1\n")
    (old / "plate_1.upload.json").write_text(json.dumps({"item": {"path": OLD, "root": "gcodes"}}))
    (old / "plate_1.launch.json").write_text(json.dumps({"file": OLD, "artifact": str(old / "plate_1.gcode")}))
    cur = new if current == "new" else old
    (run / "JOB.json").write_text(json.dumps({"title": "G4 Instant Desk Stand", "gcode": str(cur / "plate_1.gcode")}))
    past = time.time() - 3600
    for p in (old / "plate_1.upload.json", old / "plate_1.launch.json"):
        os.utime(p, (past, past))
    return run, old, new


def _st(state="paused", klippy="ready", filename="foo.gcode"):
    return {"state": state, "klippy": klippy, "filename": filename, "layer": 10, "total_layer": 50, "progress": 0.2}


def test_resume_refuses_after_emergency_stop_without_sending_or_restarting():
    call = MagicMock()
    with patch("print_handy.runtime.print_control.status", return_value=_st(klippy="shutdown")), patch("print_handy.runtime.print_control._call", call):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok
    call.assert_not_called()
    assert "printer screen offers Resume" in msg and "Don't restart" in msg


def test_resume_not_paused():
    with patch("print_handy.runtime.print_control.status", return_value=_st(state="printing")):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok and "Nothing to resume" in msg


def test_resume_paused_mismatched_filename():
    with patch("print_handy.runtime.print_control.status", return_value=_st(filename="other.gcode")):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok and "A different print is paused" in msg


def test_resume_still_paused_reports_failure_and_sends_one_resume():
    """#837 (TESTING-AUDIT #4): a resume the printer ignores is not a success.

    The old code returned ok=True on this branch, so callers that trust
    the boolean reported a resume that never happened.
    """
    call = MagicMock(return_value={"result": "ok"})
    st = MagicMock(return_value=_st())  # paused before the POST and at every readback
    with patch("print_handy.runtime.print_control._call", call), \
            patch("print_handy.runtime.print_control.status", st):
        ok, msg = print_control.resume("foo.gcode", wait_s=0.03, poll_s=0.01)
    assert ok is False
    assert "still reports paused" in msg
    call.assert_called_once_with("/printer/print/resume", method="POST", timeout=15)


def test_resume_success_sends_one_resume():
    call = MagicMock(return_value={"result": "ok"})
    st = MagicMock(side_effect=[_st(), _st(state="printing")])
    with patch("print_handy.runtime.print_control._call", call), patch("print_handy.runtime.print_control.status", st):
        ok, msg = print_control.resume("foo.gcode", wait_s=2, poll_s=0.01)
    assert ok and "Resumed" in msg and "layer 10/50" in msg
    call.assert_called_once_with("/printer/print/resume", method="POST", timeout=15)


def test_bare_printer_name_resolves_to_archived_artifact(tmp_path):
    _run, old, _new = _job(tmp_path)
    assert model_job.resolve_print_target(OLD, archive=tmp_path) == str(old / "plate_1.gcode")


def test_bare_unknown_name_is_refused(tmp_path):
    _job(tmp_path)
    with pytest.raises(SystemExit, match="printer file name, not a local path"):
        model_job.resolve_print_target("mystery.gcode", archive=tmp_path)


def test_paths_pass_through_unchanged(tmp_path):
    p = str(tmp_path / "x" / "JOB.json")
    assert model_job.resolve_print_target(p, archive=tmp_path) == p


def test_print_now_old_slice_points_at_current_job(tmp_path):
    run, _old, _new = _job(tmp_path)
    with patch.object(model_job, "ARCHIVE", tmp_path), pytest.raises(SystemExit) as exc:
        model_job.print_now(OLD)
    msg = str(exc.value)
    assert "older slice" in msg and str(run / "JOB.json") in msg


def test_last_print_uses_current_reslice_when_launched_slice_is_stale(tmp_path):
    _run, _old, new = _job(tmp_path)
    t = print_control.resolve_last_print(tmp_path)
    assert t["gcode"] == str((new / "plate_1.gcode").resolve())
    assert t["resliced"] is True and t["reprint"] is True
    assert "re-sliced" in print_control.again_question(t)


def test_last_print_reuses_launched_slice_when_current(tmp_path):
    _run, old, _new = _job(tmp_path, current="old")
    t = print_control.resolve_last_print(tmp_path)
    assert t["gcode"] == str((old / "plate_1.gcode").resolve()) and t["resliced"] is False


def test_last_print_with_no_records(tmp_path):
    from print_handy.runtime.print_next import NextPlateError
    with pytest.raises(NextPlateError):
        print_control.resolve_last_print(tmp_path)


def test_force_reboot_refuses_mid_print():
    sender = MagicMock()
    ok, msg = print_control.force_reboot(gcode_sender=sender, state_reader=lambda: _st(state="printing"))
    assert not ok and "Refused" in msg
    sender.assert_not_called()


def test_force_reboot_waits_for_safe_idle():
    sender = MagicMock()
    with patch("print_handy.core.clear_job.safe_idle", side_effect=[False, True]):
        ok, msg = print_control.force_reboot(wait_grace_s=0, max_wait_s=2, poll_s=0.01, gcode_sender=sender,
                                             moonraker_reader=lambda: {}, native_reader=lambda: {},
                                             state_reader=lambda: _st(state="cancelled"))
    assert ok and "idle" in msg
    sender.assert_called_once_with("REBOOT")


def test_force_reboot_timeout_does_not_retry():
    sender = MagicMock()
    with patch("print_handy.core.clear_job.safe_idle", return_value=False):
        ok, msg = print_control.force_reboot(wait_grace_s=0, max_wait_s=0.05, poll_s=0.01, gcode_sender=sender,
                                             moonraker_reader=lambda: {}, native_reader=lambda: {},
                                             state_reader=lambda: _st(state="standby"))
    assert not ok and "Not retrying" in msg
    sender.assert_called_once()


def _printed(tmp_path, slug, title, age_s):
    """One archived job with one launch record, launched age_s seconds ago."""
    d = tmp_path / slug / "sliced/orca/x1"
    d.mkdir(parents=True)
    (d / "plate_1.gcode").write_text(f";{slug}\nG1 X1\n")
    (tmp_path / slug / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(d / "plate_1.gcode")}))
    rec = d / "plate_1.launch.json"
    rec.write_text(json.dumps({"file": f"{slug}.gcode"}))
    t = time.time() - age_s
    os.utime(rec, (t, t))
    return d / "plate_1.gcode"


@pytest.fixture(autouse=True)
def _isolate_numbers_and_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(print_control, "PRINT_NUMBERS", tmp_path / "print_numbers.json")
    monkeypatch.setattr(print_control, "REPRINT_QUEUE", tmp_path / "reprint_queue.json")


def _three(tmp_path):
    return (_printed(tmp_path, "funko-shelf-hc--a", "FUNKO Shelf HC", 100),
            _printed(tmp_path, "desk-stand--b", "G4 Instant Desk Stand", 200),
            _printed(tmp_path, "funko-pop-riser--c", "Funko Pop Riser", 300))


def test_printed_jobs_numbered_chronologically_permanent(tmp_path):
    _three(tmp_path)
    rows = print_control.printed_jobs(tmp_path)
    # Newest print is listed first, but its permanent launch number is #3 (oldest is #1).
    assert [(r["index"], r["title"]) for r in rows] == [
        (3, "FUNKO Shelf HC"), (2, "G4 Instant Desk Stand"), (1, "Funko Pop Riser")]
    text = print_control.printed_list_text(rows)
    assert "3. FUNKO Shelf HC" in text and "1. Funko Pop Riser" in text


def test_printed_jobs_dedupes_relaunches_of_one_job(tmp_path):
    g = _printed(tmp_path, "job--a", "Thing", 100)
    (g.parent / "plate_1.upload.json").write_text("{}")
    assert len(print_control.printed_jobs(tmp_path)) == 1


# ------------------------------------------- per-launch print numbers

def test_relaunch_minted_at_start_gets_its_own_number(tmp_path):
    """A reprint launched through print-now mints a new number at start time:
    the /print-again list then shows BOTH prints, each with its own number,
    and both numbers resolve to the same job's G-code."""
    g = _printed(tmp_path, "job--a", "Thing", 100)
    rows = print_control.printed_jobs(tmp_path)
    assert [(r["index"], r["title"]) for r in rows] == [(1, "Thing")]
    job_json = tmp_path / "job--a" / "JOB.json"
    minted = print_control.mint_launch_number(job_json.resolve(), artifact=g)
    assert minted == 2
    rows = print_control.printed_jobs(tmp_path)
    assert [r["index"] for r in rows] == [2, 1]
    assert print_control.resolve_print("1", tmp_path)["gcode"] == str(g.resolve())
    assert print_control.resolve_print("2", tmp_path)["gcode"] == str(g.resolve())
    # The legacy per-job entry still names the FIRST print.
    registry = json.loads((tmp_path / "print_numbers.json").read_text())
    assert registry["numbers"][str(job_json.resolve())] == 1


def test_unminted_relaunch_seen_by_record_time_gets_its_own_number(tmp_path):
    """A launch nobody minted (printer screen start) rewrites the artifact's
    launch record; when the list is next built, that newer record is a new
    print with the next number, and the first print keeps number 1."""
    g = _printed(tmp_path, "job--a", "Thing", 7200)
    assert [r["index"] for r in print_control.printed_jobs(tmp_path)] == [1]
    rec = g.parent / "plate_1.launch.json"
    now = time.time()
    os.utime(rec, (now, now))
    rows = print_control.printed_jobs(tmp_path)
    assert [r["index"] for r in rows] == [2, 1]
    assert print_control.resolve_print("1", tmp_path)["gcode"] == str(g.resolve())
    assert print_control.resolve_print("2", tmp_path)["gcode"] == str(g.resolve())


def test_each_plate_launch_of_one_job_gets_its_own_number(tmp_path):
    """Two launched plates of one job are two prints, numbered in launch order."""
    run = tmp_path / "multi--a"
    gcodes = []
    for i, age in enumerate((300, 100)):
        d = run / f"sliced/orca/p{i}"
        d.mkdir(parents=True)
        (d / "plate_1.gcode").write_text("G1 X1\n")
        rec = d / "plate_1.launch.json"
        rec.write_text(json.dumps({"file": f"plate-{i}.gcode"}))
        t = time.time() - age
        os.utime(rec, (t, t))
        gcodes.append(d / "plate_1.gcode")
    (run / "JOB.json").write_text(json.dumps({"title": "Multi", "gcode": str(gcodes[-1])}))
    rows = print_control.printed_jobs(tmp_path)
    assert [(r["index"], r["title"]) for r in rows] == [(2, "Multi"), (1, "Multi")]
    # Resolving print #1 re-slices from the job's current artifact, as before.
    assert print_control.resolve_print("1", tmp_path)["gcode"] == str(gcodes[-1].resolve())


def test_old_numbers_from_the_per_job_registry_keep_working(tmp_path):
    """The 'print 84' pin: a registry written by the per-job numbering keeps
    its numbers — job A's first print is still #84 and resolves to job A."""
    a = _printed(tmp_path, "job-a--a", "Alpha", 200)
    b = _printed(tmp_path, "job-b--b", "Beta", 100)
    registry = {"numbers": {str((tmp_path / "job-a--a" / "JOB.json").resolve()): 84,
                            str((tmp_path / "job-b--b" / "JOB.json").resolve()): 85}}
    (tmp_path / "print_numbers.json").write_text(json.dumps(registry))
    rows = print_control.printed_jobs(tmp_path)
    assert [(r["index"], r["title"]) for r in rows] == [(85, "Beta"), (84, "Alpha")]
    assert print_control.resolve_print("84", tmp_path)["gcode"] == str(a.resolve())
    assert print_control.resolve_print("85", tmp_path)["gcode"] == str(b.resolve())
    # A later launch of Alpha takes the NEXT number after the highest old one.
    assert print_control.mint_launch_number((tmp_path / "job-a--a" / "JOB.json").resolve(),
                                            artifact=a) == 86


def test_mint_launch_number_never_reuses_and_sets_first_number(tmp_path):
    job_a = tmp_path / "a" / "JOB.json"
    job_b = tmp_path / "b" / "JOB.json"
    assert print_control.mint_launch_number(job_a) == 1
    assert print_control.mint_launch_number(job_b) == 2
    assert print_control.mint_launch_number(job_a) == 3
    registry = print_control._load_number_registry()
    assert registry["numbers"] == {str(job_a): 1, str(job_b): 2}
    assert [e["number"] for e in registry["launches"]] == [1, 2, 3]


def test_resolve_by_number(tmp_path):
    _a, b, _c = _three(tmp_path)
    t = print_control.resolve_print("2", tmp_path)
    assert t["gcode"] == str(b.resolve()) and t["title"] == "G4 Instant Desk Stand"
    assert print_control.resolve_print("#2", tmp_path)["gcode"] == t["gcode"]


def test_resolve_number_out_of_range(tmp_path):
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError, match="no print #9"):
        print_control.resolve_print("9", tmp_path)


def test_resolve_by_name_case_insensitive(tmp_path):
    _a, b, _c = _three(tmp_path)
    assert print_control.resolve_print("desk STAND", tmp_path)["gcode"] == str(b.resolve())
    assert print_control.resolve_print("g4-instant", tmp_path)["gcode"] == str(b.resolve())


def test_resolve_exact_title_beats_substring(tmp_path):
    a, _b, _c = _three(tmp_path)
    _printed(tmp_path, "funko-shelf-hc-v2--d", "Funko Shelf HC v2", 400)
    assert print_control.resolve_print("funko shelf hc", tmp_path)["gcode"] == str(a.resolve())


def test_resolve_ambiguous_name_lists_numbers_and_refuses(tmp_path):
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("funko", tmp_path)
    msg = str(exc.value)
    assert "matches 2" in msg and "3. FUNKO Shelf HC" in msg and "1. Funko Pop Riser" in msg


def test_resolve_unknown_name(tmp_path):
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError, match="no printed job matches"):
        print_control.resolve_print("benchy", tmp_path)


def test_resolve_empty_query_is_last_print(tmp_path):
    a, _b, _c = _three(tmp_path)
    assert print_control.resolve_print("", tmp_path)["gcode"] == str(a.resolve())


# ------------------------------------------------------- #468 stage 2 corpus

def test_resolve_typo_suggests_a_pick_list_and_refuses(tmp_path):
    """A typo'd name gets a numbered did-you-mean list in the same shape as
    the ambiguous-name refusal — and still refuses. The follow-up is the
    existing exact path: /print-again <number>."""
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("funco shelf", tmp_path)
    msg = str(exc.value)
    assert "Did you mean one of these?" in msg and "3. FUNKO Shelf HC" in msg
    assert "Pick one with /print-again <number>" in msg


def test_resolve_typo_in_part_of_a_long_title_suggests_it(tmp_path):
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("desk stadn", tmp_path)
    assert "2. G4 Instant Desk Stand" in str(exc.value)


def test_resolve_single_strong_fuzzy_hit_still_never_resolves(tmp_path):
    """The safety pin for stage 2: even one candidate at 97/100 similarity
    ("funko pop riserr") is offered, never resolved — a fuzzy match may
    suggest, it may not pick the print."""
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("funko pop riserr", tmp_path)
    assert "1. Funko Pop Riser" in str(exc.value)


def test_fuzzy_printed_orders_best_score_first(tmp_path):
    _printed(tmp_path, "desk-stand--a", "Desk Stand", 100)
    _printed(tmp_path, "desk-stand-pro--b", "Desk Stand Pro Max", 200)
    rows = print_control.printed_jobs(tmp_path)
    near = print_control.fuzzy_printed("desk stadn pro", rows)
    assert [r["title"] for r in near] == ["Desk Stand Pro Max", "Desk Stand"]
    assert print_control.fuzzy_printed("benchy", rows) == []


def test_resolve_unrelated_name_gets_no_suggestions(tmp_path):
    """No near match -> the pre-#468 refusal, byte-identical: no did-you-mean
    is attached when nothing is close enough to mean."""
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("benchy", tmp_path)
    assert str(exc.value) == 'no printed job matches "benchy". Type /print-again list'


def test_resolve_number_like_query_never_goes_fuzzy(tmp_path):
    """Print numbers stay exact (#436): an out-of-range number is a number
    error, never a fuzzy suggestion, whatever the titles look like."""
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError, match="no print #22"):
        print_control.resolve_print("22", tmp_path)


# ------------------------------------------- number words + fold hardening

def test_resolve_number_word_query_suggests_the_spelled_title(tmp_path):
    """Number equivalence is fuzzy-scorer-only: "mark 2" finds the print
    titled "Mark Two" — but as a did-you-mean pick-list, never a resolve."""
    from print_handy.runtime.print_next import NextPlateError
    _printed(tmp_path, "mark-two--a", "Mark Two Bracket", 100)
    _printed(tmp_path, "benchy--b", "Benchy", 200)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("mark 2", tmp_path)
    msg = str(exc.value)
    assert "Did you mean one of these?" in msg and "2. Mark Two Bracket" in msg


def test_resolve_number_word_never_selects_a_print_number(tmp_path):
    """The other half of the boundary: the word "two" is not print #2.
    It is a name query, and with no title anywhere near it the answer is
    the plain no-match refusal — no target is ever resolved from it."""
    from print_handy.runtime.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("two", tmp_path)
    assert str(exc.value) == 'no printed job matches "two". Type /print-again list'
    # ...while the digit form still resolves print #2 exactly.
    assert print_control.resolve_print("2", tmp_path)["title"] == "G4 Instant Desk Stand"


def test_resolve_cjk_title_matches_by_name(tmp_path):
    """The Unicode fold keeps CJK: a Chinese-titled print is findable by
    its name (and resolvable on an exact title match), not only by number."""
    g = _printed(tmp_path, "nozzle-plug--a", "喷嘴堵塞工具", 100)
    _printed(tmp_path, "benchy--b", "Benchy", 200)
    assert print_control.resolve_print("喷嘴堵塞工具", tmp_path)["gcode"] == str(g.resolve())


def test_fuzzy_printed_scans_only_the_newest_rows(tmp_path):
    """The archive is unbounded; the fuzzy pass scores at most
    FUZZY_SCAN_LIMIT newest rows (exact/substring matching still see all)."""
    for i in range(print_control.FUZZY_SCAN_LIMIT + 5):
        _printed(tmp_path, f"job-{i:04d}--x", "Benchy", i + 1)
    _printed(tmp_path, "old-zebra--z", "Zebra Hook", print_control.FUZZY_SCAN_LIMIT + 100)
    rows = print_control.printed_jobs(tmp_path)
    assert print_control.fuzzy_printed("zebra hok", rows) == []  # beyond the scan cap
    assert print_control.match_printed("zebra hook", rows)      # exact path unaffected


def _copy_gcode(tmp_path, duration="1h", grams=12.0):
    g = tmp_path / "copy.gcode"
    g.write_text(f"; estimated printing time (normal mode) = {duration}\n"
                 f"; total filament used [g] = {grams}\nG1 X1\n")
    return str(g)


def test_again_question_quotes_all_copies_for_a_sequential_reprint(tmp_path):
    """#438: the xN confirmation quotes each copy and the all-copies totals, not one copy."""
    t = {"title": "Hook", "gcode": _copy_gcode(tmp_path), "copies": 3, "resliced": False}
    q = print_control.again_question(t)
    assert "Copy 1 of 3" in q
    assert "Each copy: ~1h 0m printing, ~12 g filament." in q
    assert "All 3 copies: ~3h 0m printing (plus ~5m warmup per copy), ~36 g filament." in q


def test_again_question_single_copy_and_unknown_figures_unchanged(tmp_path):
    g = _copy_gcode(tmp_path)
    one = {"title": "Hook", "gcode": g, "copies": 1, "resliced": False}
    assert print_control.again_question(one) == "Print Hook again from the start?\nConfirm the bed is clear."
    missing = {"title": "Hook", "gcode": str(tmp_path / "gone.gcode"), "copies": 3, "resliced": False}
    q = print_control.again_question(missing)
    assert "Copy 1 of 3" in q and "Each copy" not in q and "All 3 copies" not in q


def test_start_estimate_text_labels_sequential_copies_and_keeps_single_copy_wording():
    single = "estimated print time ~1h 0m, ~1h 5m total including warmup"
    assert print_control.start_estimate_text({"copies": 1}, 3600, 12.0) == single
    assert print_control.start_estimate_text({"copies": 1}, None, None) == "estimated print time unknown"
    assert print_control.start_estimate_text({"copies": 3}, 3600, 12.0) == (
        single + " for this copy; all 3 copies: ~3h 0m printing (plus ~5m warmup per copy), ~36 g filament")
    second = print_control.start_estimate_text({"copies": 3, "copy_number": 2}, 3600, 12.0)
    assert "all 2 remaining copies: ~2h 0m printing" in second and "~24 g filament" in second
    # The last copy's figures are the remaining job's figures; nothing extra is added.
    assert print_control.start_estimate_text({"copies": 3, "copy_number": 3}, 3600, 12.0) == single


def test_next_copy_question_and_prompt_show_remaining_figures(tmp_path):
    import hashlib
    from pathlib import Path

    g = _copy_gcode(tmp_path)
    sha = hashlib.sha256(Path(g).read_bytes()).hexdigest()
    target = {"title": "Hook", "gcode": g, "job_json": str(tmp_path / "JOB.json"),
              "gcode_sha256": sha, "copies": 3}
    queue = tmp_path / "reprint_queue.json"
    print_control.queue_copies(target, queue)
    prompt = print_control.next_copy_prompt(f"Hook_{sha}.gcode", queue)
    assert prompt is not None and prompt.startswith(
        "Copy 1 of 3 of Hook is done. Still to print: 2 copies "
        "(~2h 0m printing (plus ~5m warmup per copy), ~24 g filament).")
    assert "for copy 2" in prompt
    question = print_control.next_copy_question(
        {"copy_number": 2, "copies": 3, "title": "Hook", "gcode": g})
    assert question == ("Print copy 2 of 3 of Hook?\n"
                        "This copy: ~1h 0m printing, ~12 g filament. "
                        "All 2 remaining copies: ~2h 0m printing (plus ~5m warmup per copy), ~24 g filament.\n"
                        "Confirm the bed is clear.")
    unknown = print_control.next_copy_question(
        {"copy_number": 2, "copies": 3, "title": "Hook", "gcode": str(tmp_path / "gone.gcode")})
    assert unknown == "Print copy 2 of 3 of Hook?\nConfirm the bed is clear."


def test_start_targets_text_reads_the_uploaded_gcode():
    gcode = "; h\nM190 S60\nM104 S210 T0\nSTART_PRINT BED_TEMP=60 EXTRUDER_TEMP=220 MESH=auto\nG1 X1\n"
    assert print_control.start_targets_text(gcode) == "Nozzle 220 °C · Bed 60 °C"
    assert print_control.start_targets_text("M140 S65\nM104 S235\nG28\nG1 X1\n") == "Nozzle 235 °C · Bed 65 °C"
    assert print_control.start_targets_text("G28\nG1 X1\n") == ""


def test_reprint_queues_are_isolated_per_printer(tmp_path, monkeypatch):
    """Printer B's /print-again must never disturb printer A's pending
    copies: the queue is stored per printer, not in one global file (P2-135)."""
    import hashlib
    from pathlib import Path

    monkeypatch.setattr(print_control, "REPRINT_QUEUE", tmp_path / "reprint_queue.json")
    monkeypatch.setattr(print_control, "ARCHIVE", tmp_path)
    monkeypatch.setattr(print_control._config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(print_control._config, "cfg",
                        lambda: {"printers": [{"id": "printer-a"}, {"id": "printer-b"}]})

    def make_target(name):
        folder = tmp_path / name
        folder.mkdir()
        gcode = folder / "job.gcode"
        gcode.write_text("G1 X1\n")
        job = folder / "JOB.json"
        job.write_text("{}")
        return {"title": name, "gcode": str(gcode), "job_json": str(job),
                "gcode_sha256": hashlib.sha256(gcode.read_bytes()).hexdigest(), "copies": 3}

    target_a, target_b = make_target("Alpha"), make_target("Beta")
    print_control.queue_copies(target_a, printer_id="printer-a")
    # A one-copy print on B deletes only B's (empty) queue, not A's.
    print_control.queue_copies({**target_b, "copies": 1}, printer_id="printer-b")
    pending_a = print_control.pending_copy(printer_id="printer-a")
    assert pending_a is not None and pending_a["title"] == "Alpha"
    assert print_control.pending_copy(printer_id="printer-b") is None
    # B queues its own multi-copy run; A's queue is untouched, and vice versa.
    print_control.queue_copies(target_b, printer_id="printer-b")
    assert print_control.pending_copy(printer_id="printer-a")["title"] == "Alpha"
    assert print_control.pending_copy(printer_id="printer-b")["title"] == "Beta"
    nxt = print_control.next_copy_target(printer_id="printer-a")
    assert nxt["title"] == "Alpha" and nxt["copy_number"] == 2
    print_control.mark_copy_started(printer_id="printer-b")
    assert print_control.pending_copy(printer_id="printer-b")["started"] == 2
    assert print_control.pending_copy(printer_id="printer-a")["started"] == 1
    # Cancelling B's copies leaves A's alone.
    assert print_control.cancel_copies(printer_id="printer-b") is True
    assert print_control.pending_copy(printer_id="printer-b") is None
    assert print_control.pending_copy(printer_id="printer-a") is not None


# 2026-11-04: three resumes went out while the AD5X head sensor still read
# empty after a runout; each paused again at once. Resume now checks first.
_AD5X_OBJECTS = ["toolhead", "extruder", "filament_switch_sensor head_switch_sensor",
                 "filament_motion_sensor ifs_motion_sensor", "zmod_ifs"]


def _sensor_call(head_detected, *, enabled=True, resume_reply=None):
    def call(path, method="GET", data=None, timeout=10, base=None):
        if path == "/printer/objects/list":
            return {"result": {"objects": _AD5X_OBJECTS}}
        if path.startswith("/printer/objects/query?"):
            assert "ifs_motion_sensor" not in path  # motion sensors read empty while paused
            return {"result": {"status": {"filament_switch_sensor head_switch_sensor":
                                          {"filament_detected": head_detected, "enabled": enabled}}}}
        if path == "/printer/print/resume":
            return resume_reply or {"result": "ok"}
        raise AssertionError(path)
    return MagicMock(side_effect=call)


@pytest.mark.real_head_sensor
def test_resume_refused_when_head_sensor_reads_empty():
    call = _sensor_call(False)
    with patch("print_handy.runtime.print_control._call", call), \
            patch("print_handy.runtime.print_control.status", return_value=_st()):
        ok, msg = print_control.resume("foo.gcode")
    assert ok is False and msg == print_control.HEAD_EMPTY_TEXT
    assert "/printer/print/resume" not in [c.args[0] for c in call.call_args_list]


@pytest.mark.real_head_sensor
def test_resume_sent_when_head_sensor_reads_loaded():
    call = _sensor_call(True)
    st = MagicMock(side_effect=[_st(), _st(state="printing")])
    with patch("print_handy.runtime.print_control._call", call), \
            patch("print_handy.runtime.print_control.status", st):
        ok, _msg = print_control.resume("foo.gcode", wait_s=2, poll_s=0.01)
    assert ok
    assert [c.args[0] for c in call.call_args_list].count("/printer/print/resume") == 1


@pytest.mark.real_head_sensor
def test_disabled_head_sensor_does_not_block_resume():
    call = _sensor_call(False, enabled=False)
    st = MagicMock(side_effect=[_st(), _st(state="printing")])
    with patch("print_handy.runtime.print_control._call", call), \
            patch("print_handy.runtime.print_control.status", st):
        ok, _msg = print_control.resume("foo.gcode", wait_s=2, poll_s=0.01)
    assert ok


def test_head_switch_sensors_selection():
    assert print_control.head_switch_sensors(_AD5X_OBJECTS) == ["filament_switch_sensor head_switch_sensor"]
    assert print_control.head_switch_sensors(["extruder", "filament_switch_sensor runout"]) == \
        ["filament_switch_sensor runout"]
    # multi-extruder printer with no head-named sensor: no guess
    assert print_control.head_switch_sensors(["extruder", "extruder1", "filament_switch_sensor runout"]) == []
    assert print_control.head_switch_sensors(None) == []


def test_active_head_sensors_follow_the_active_extruder():
    # Review of #878: an idle tool's empty head sensor must not block resume.
    names = ["filament_switch_sensor head_extruder", "filament_switch_sensor head_extruder1"]
    assert print_control.active_head_sensors(names, "extruder1") == ["filament_switch_sensor head_extruder1"]
    assert print_control.active_head_sensors(names, "extruder") == ["filament_switch_sensor head_extruder"]
    assert print_control.active_head_sensors(names, None) == []


def test_copies_queue_pins_hash_and_claim_for_repeated_confirmations(tmp_path):
    import hashlib
    from print_handy.runtime.print_next import NextPlateError

    run = tmp_path / "archive" / "job"
    run.mkdir(parents=True)
    job = run / "JOB.json"
    job.write_text('{"title":"Test"}')
    gcode = run / "plate.gcode"
    gcode.write_bytes(b"G1 X1\n")
    expected = hashlib.sha256(gcode.read_bytes()).hexdigest()
    queue = tmp_path / "q.json"
    print_control.queue_copies({"job_json": str(job), "gcode": str(gcode),
                                "title": "Test", "gcode_sha256": expected, "copies": 2},
                               path=queue)
    first = print_control.next_copy_target(queue, run.parent)
    assert first["gcode_sha256"] == expected
    assert print_control.next_copy_target(queue, run.parent)["copy"] == first["copy"]
    gcode.write_bytes(b"G1 X999\n")
    with pytest.raises(NextPlateError, match="changed since it was queued"):
        print_control.next_copy_target(queue, run.parent)
    assert json.loads(queue.read_text())["gcode_sha256"] == expected


def test_legacy_queue_without_nonce_has_stable_claim_and_missing_hash_refuses(tmp_path):
    import hashlib
    from print_handy.runtime.print_next import NextPlateError

    run = tmp_path / "archive" / "job"
    run.mkdir(parents=True)
    job = run / "JOB.json"
    job.write_text("{}")
    gcode = run / "plate.gcode"
    gcode.write_bytes(b"G1 X1\n")
    queue = tmp_path / "old-queue.json"
    legacy = {"job_json": str(job), "gcode": str(gcode),
              "title": "Old queue", "gcode_sha256": hashlib.sha256(gcode.read_bytes()).hexdigest(),
              "total": 3, "started": 1, "queued_at": 12345.0}
    queue.write_text(json.dumps(legacy))
    first = print_control.next_copy_target(queue, run.parent)
    with patch("print_handy.runtime.print_control.time.time", return_value=123456789):
        second = print_control.next_copy_target(queue, run.parent)
    assert first["copy"] == second["copy"]
    assert first["gcode_sha256"] == legacy["gcode_sha256"]
    legacy.pop("gcode_sha256")
    queue.write_text(json.dumps(legacy))
    with pytest.raises(NextPlateError, match="no valid G-code fingerprint"):
        print_control.next_copy_target(queue, run.parent)
