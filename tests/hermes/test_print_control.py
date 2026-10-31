"""print_control: resume, print again, force restart. Offline; no printer traffic."""
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from print_handy.hermes import print_control
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
    with patch("print_handy.hermes.print_control.status", return_value=_st(klippy="shutdown")), patch("print_handy.hermes.print_control._call", call):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok
    call.assert_not_called()
    assert "printer screen offers Resume" in msg and "Don't restart" in msg


def test_resume_not_paused():
    with patch("print_handy.hermes.print_control.status", return_value=_st(state="printing")):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok and "Nothing to resume" in msg


def test_resume_paused_mismatched_filename():
    with patch("print_handy.hermes.print_control.status", return_value=_st(filename="other.gcode")):
        ok, msg = print_control.resume("foo.gcode")
    assert not ok and "A different print is paused" in msg


def test_resume_success_sends_one_resume():
    call = MagicMock(return_value={"result": "ok"})
    st = MagicMock(side_effect=[_st(), _st(state="printing")])
    with patch("print_handy.hermes.print_control._call", call), patch("print_handy.hermes.print_control.status", st):
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
    from print_handy.hermes.print_next import NextPlateError
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


def test_resolve_by_number(tmp_path):
    _a, b, _c = _three(tmp_path)
    t = print_control.resolve_print("2", tmp_path)
    assert t["gcode"] == str(b.resolve()) and t["title"] == "G4 Instant Desk Stand"
    assert print_control.resolve_print("#2", tmp_path)["gcode"] == t["gcode"]


def test_resolve_number_out_of_range(tmp_path):
    from print_handy.hermes.print_next import NextPlateError
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
    from print_handy.hermes.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("funko", tmp_path)
    msg = str(exc.value)
    assert "matches 2" in msg and "3. FUNKO Shelf HC" in msg and "1. Funko Pop Riser" in msg


def test_resolve_unknown_name(tmp_path):
    from print_handy.hermes.print_next import NextPlateError
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
    from print_handy.hermes.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("funco shelf", tmp_path)
    msg = str(exc.value)
    assert "Did you mean one of these?" in msg and "3. FUNKO Shelf HC" in msg
    assert "Pick one with /print-again <number>" in msg


def test_resolve_typo_in_part_of_a_long_title_suggests_it(tmp_path):
    from print_handy.hermes.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("desk stadn", tmp_path)
    assert "2. G4 Instant Desk Stand" in str(exc.value)


def test_resolve_single_strong_fuzzy_hit_still_never_resolves(tmp_path):
    """The safety pin for stage 2: even one candidate at 97/100 similarity
    ("funko pop riserr") is offered, never resolved — a fuzzy match may
    suggest, it may not pick the print."""
    from print_handy.hermes.print_next import NextPlateError
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
    from print_handy.hermes.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError) as exc:
        print_control.resolve_print("benchy", tmp_path)
    assert str(exc.value) == 'no printed job matches "benchy". Type /print-again list'


def test_resolve_number_like_query_never_goes_fuzzy(tmp_path):
    """Print numbers stay exact (#436): an out-of-range number is a number
    error, never a fuzzy suggestion, whatever the titles look like."""
    from print_handy.hermes.print_next import NextPlateError
    _three(tmp_path)
    with pytest.raises(NextPlateError, match="no print #22"):
        print_control.resolve_print("22", tmp_path)


# ------------------------------------------- number words + fold hardening

def test_resolve_number_word_query_suggests_the_spelled_title(tmp_path):
    """Number equivalence is fuzzy-scorer-only: "mark 2" finds the print
    titled "Mark Two" — but as a did-you-mean pick-list, never a resolve."""
    from print_handy.hermes.print_next import NextPlateError
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
    from print_handy.hermes.print_next import NextPlateError
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
    single = "estimated print time ~1h 0m, plus warmup ~1h 5m total"
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
