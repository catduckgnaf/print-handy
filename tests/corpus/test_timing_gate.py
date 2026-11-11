"""The Discord timing gate fails on slowdowns, missing runs and unexplained baseline raises."""
from pathlib import Path

from tests.corpus.timing_gate import check, record

BASE = {"tolerance": 0.25, "slack_s": 1.0,
        "cases": {"single-part/direct": {"wall_s": 4.0, "first_prompt_s": 2.0}}}


def row(wall, prompt=2.0, result="PASS"):
    return {"case": "single-part", "transport": "direct", "result": result,
            "wall_s": wall, "first_prompt_s": prompt}


def test_within_tolerance_passes():
    assert check([row(5.9)], BASE, absolute_slack=True) == []  # limit 4 * 1.25 + 1 = 6.0


def test_slower_than_baseline_fails():
    problems = check([row(6.1)], BASE, absolute_slack=True)
    assert len(problems) == 1 and "wall_s 6.10s > limit 6.00s" in problems[0]


def test_slow_first_prompt_fails_even_if_total_is_fine():
    assert any("first_prompt_s" in p for p in check([row(4.0, prompt=3.6)], BASE))


def test_failed_flow_and_missing_case_are_reported():
    assert "flow result FAIL" in check([row(1.0, result="FAIL")], BASE)[0]
    assert "was not run" in check([], BASE)[0]


def test_new_case_needs_a_baseline():
    other = dict(row(1.0), case="new-case")
    assert any("no baseline" in p for p in check([row(4.0), other], BASE))


def test_raise_without_reason_is_refused_and_reason_accepts_it():
    proposed = record([row(5.0)], BASE)
    entry = proposed["cases"]["single-part/direct"]
    assert entry["raised_from"] == {"wall_s": 4.0, "first_prompt_s": 2.0}
    assert any("without a reason" in p for p in check([row(5.0)], proposed))
    entry["reason"] = "full mesh validation added, accepted"
    assert check([row(5.0)], proposed) == []


def test_median_of_repeats_ignores_one_noisy_sample():
    from tests.corpus.timing_gate import median_rows
    merged = median_rows([[row(4.0)], [row(30.0)], [row(4.2)]])
    assert merged[0]["wall_s"] == 4.2 and check(merged, BASE) == []
    assert median_rows([[row(4.0)], [row(4.0, result="FAIL")]])[0]["result"] == "FAIL"


def fit_row(fit, wall=4.0):
    return dict(row(wall), fit_check_s=fit)


def test_fit_check_budget_is_enforced_separately_from_total_time():
    budget = dict(BASE, fit_check_budget_s=2.0)
    assert check([fit_row(1.9)], budget) == []
    problems = check([fit_row(2.1)], budget)  # wall 4.0 is fine; only the fit check is over
    assert len(problems) == 1 and "fit-check budget 2.00s" in problems[0]


def test_fit_check_budget_applies_to_cases_without_a_baseline_entry_and_survives_record():
    budget = dict(BASE, fit_check_budget_s=2.0)
    other = dict(fit_row(5.0), case="new-case")
    assert any("fit-check budget" in p for p in check([row(4.0), other], budget))
    assert record([row(4.0)], budget)["fit_check_budget_s"] == 2.0


def test_per_case_fit_check_baseline_uses_tolerance():
    base = {"tolerance": 0.25, "slack_s": 1.0,
            "cases": {"single-part/direct": {"wall_s": 4.0, "first_prompt_s": 2.0, "fit_check_s": 1.0}}}
    assert check([fit_row(2.2)], base, absolute_slack=True) == []  # 1 * 1.25 + 1 = 2.25
    assert any("fit_check_s" in p for p in check([fit_row(2.3)], base, absolute_slack=True))


def test_fit_check_is_measured_from_the_real_intake_flow(tmp_path):
    from tests.corpus import harness, time_corpus
    case = next(harness.load_case(f) for f in harness.case_dirs() if f.name == "must-shrink")
    row_ = time_corpus.run_one(case, "direct", False, tmp_path)
    assert row_["result"] == "PASS", row_
    assert row_["fit_check_s"] is not None and 0 <= row_["fit_check_s"] <= row_["wall_s"]


def test_missing_fit_measurement_fails_for_required_case_only():
    req = dict(BASE, fit_check_required=["single-part/direct"])
    assert any("not measured" in p for p in check([row(4.0)], req))
    assert check([fit_row(0.5)], req) == []
    assert check([row(4.0)], BASE) == []  # not required: no measurement is fine
    assert record([row(4.0)], req)["fit_check_required"] == ["single-part/direct"]
    from tests.corpus.timing_gate import median_rows
    merged = median_rows([[fit_row(0.5)], [row(4.0)], [fit_row(0.6)]])
    assert any("not measured" in p for p in check(merged, req))


# ----------------------------------------------------------------------------- #734: +10%, reporting leg, release step

def test_default_limits_are_the_issue_ten_percent_with_relative_slack():
    from tests.corpus.timing_gate import DEFAULT_SLACK_S, DEFAULT_TOLERANCE, SLACK_FLOOR_S, SLACK_REL, limits
    assert (DEFAULT_TOLERANCE, DEFAULT_SLACK_S) == (0.10, 0.5)
    assert (SLACK_FLOOR_S, SLACK_REL) == (0.35, 0.10)
    bare = {"cases": {"single-part/direct": {"wall_s": 10.0}}}
    assert limits(bare) == (0.10, 0.5)
    assert check([row(12.0)], bare) == []  # 10 * 1.10 + max(0.35, 10% of 10) = 12.0
    assert any("wall_s 12.10s > limit 12.00s" in p for p in check([row(12.1)], bare))
    # The legacy flat allowance is still available for the reporting-only real-Orca leg.
    assert check([row(11.5)], bare, absolute_slack=True) == []  # 10 * 1.10 + 0.5
    assert any("limit 11.50s" in p for p in check([row(11.6)], bare, absolute_slack=True))


def test_checked_in_baselines_use_ten_percent():
    import json
    from pathlib import Path
    here = Path(__file__).parent
    mock = json.loads((here / "timing_baseline.json").read_text())
    real = json.loads((here / "timing_baseline_real_slicer.json").read_text())
    assert mock["tolerance"] == real["tolerance"] == 0.10
    assert mock["slack_s"] == 0.5
    for name, data in (("mock", mock), ("real", real)):
        for k, entry in data["cases"].items():
            assert not entry.get("raised_from") or entry.get("reason"), f"{name} {k} raised without a reason"
    assert mock["recorded_from"], "the mock baseline names the CI run it was recorded from"


def test_a_newly_measured_metric_is_not_a_raise():
    proposed = record([fit_row(0.01)], BASE)  # BASE has no fit_check_s for this case
    assert "raised_from" not in proposed["cases"]["single-part/direct"]


def test_record_stores_provenance_and_does_not_carry_the_old_one():
    old = dict(BASE, recorded_from="CI run 1", description="mock leg")
    out = record([row(4.0)], old, "CI run 2 (abc)")
    assert out["recorded_from"] == "CI run 2 (abc)" and out["description"] == "mock leg"
    assert record([row(4.0)], old)["recorded_from"] == ""


def _results(tmp_path, walls):
    import json
    paths = []
    for i, wall in enumerate(walls):
        p = tmp_path / f"run-{i}.json"
        p.write_text(json.dumps({"rows": [row(wall)]}))
        paths.append(str(p))
    return paths


def test_warn_only_reports_but_exits_zero_and_summary_lists_each_case(tmp_path, capsys):
    import json
    from tests.corpus.timing_gate import main
    base = tmp_path / "base.json"
    base.write_text(json.dumps(BASE))
    runs = _results(tmp_path, [9.0, 9.1, 9.2])  # well over 4 * 1.25 + 1
    assert main([*runs, "--baseline", str(base), "--absolute-slack"]) == 1
    summary = tmp_path / "summary.md"
    assert main([*runs, "--baseline", str(base), "--warn-only", "--absolute-slack",
                 "--summary", str(summary), "--title", "T"]) == 0
    out = capsys.readouterr().out
    assert "::warning title=Timing (reporting only)::single-part/direct: wall_s 9.10s" in out
    text = summary.read_text()
    assert text.startswith("### T") and "| single-part/direct | PASS | wall_s | 9.10 | 4.00 | 6.00 | +12" in text and "% OVER |" in text


# ----------------------------------------------------------------------------- relative slack, fresh reasons, drift

def small(wall, case="tiny"):
    return {"case": case, "transport": "direct", "result": "PASS", "wall_s": wall}


TINY = {"tolerance": 0.10, "slack_s": 0.5,
        "cases": {"tiny/direct": {"wall_s": 0.02}, "mid/direct": {"wall_s": 2.0}, "big/direct": {"wall_s": 20.0}}}


def test_slack_is_relative_with_a_floor_and_ignores_the_flat_slack_s():
    from tests.corpus.timing_gate import slack_for
    assert slack_for(0.02, TINY) == 0.35  # the floor
    assert slack_for(2.0, TINY) == 0.35
    assert slack_for(20.0, TINY) == 2.0  # 10% of the baseline
    assert slack_for(20.0, TINY, absolute=True) == 0.5
    rows = lambda *w: [small(w[0], "tiny"), small(w[1], "mid"), small(w[2], "big")]  # noqa: E731
    assert check(rows(0.37, 2.55, 24.0), TINY) == []  # 0.02*1.1+0.35, 2*1.1+0.35, 20*1.1+2.0
    assert len(check(rows(0.38, 2.56, 24.1), TINY)) == 3


def test_the_floor_is_wide_enough_for_the_runner_noise_measured_on_unchanged_main():
    # Medians read on green main runs against a 0.38 s baseline (assembly-2c-tight): up to 0.75 s.
    # 0.75 - 0.38 * 1.1 = 0.332 s of slack was needed, so the floor must cover it.
    from tests.corpus.timing_gate import SLACK_FLOOR_S
    assert SLACK_FLOOR_S >= 0.75 - 0.38 * 1.10


def test_checked_in_mock_baseline_passes_its_own_gate():
    import json
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "timing_baseline.json").read_text())
    rows = []
    for k, entry in data["cases"].items():
        case, transport = k.split("/")
        rows.append({"case": case, "transport": transport, "result": "PASS",
                     **{m: v for m, v in entry.items() if m in ("wall_s", "first_prompt_s", "fit_check_s")}})
    assert check(rows, data) == []


def raised(prev_reason="Because the intake does more work now."):
    old = {"cases": {"tiny/direct": {"wall_s": 1.0, "reason": prev_reason}}}
    return old, record([small(1.3)], old)


def test_record_does_not_copy_the_previous_reason_into_a_raise():
    old, proposed = raised()
    entry = proposed["cases"]["tiny/direct"]
    assert entry["raised_from"] == {"wall_s": 1.0}
    assert entry["reason"] == ""
    assert entry["previous_reason"] == "Because the intake does more work now."
    assert any("without a reason" in p for p in check([small(1.3)], proposed))


def test_a_raise_whose_reason_is_the_old_one_copied_back_fails():
    _, proposed = raised()
    entry = proposed["cases"]["tiny/direct"]
    entry["reason"] = entry["previous_reason"]
    assert any("copied over" in p for p in check([small(1.3)], proposed))
    entry["reason"] = "x"  # too short to be an explanation
    assert any("without a reason" in p for p in check([small(1.3)], proposed))
    entry["reason"] = "The second pass now also validates every plate before the first prompt."
    assert check([small(1.3)], proposed) == []


def test_original_is_the_first_recording_and_survives_later_recordings():
    first = record([small(1.0)], {"cases": {}})
    assert first["cases"]["tiny/direct"]["original"] == {"wall_s": 1.0}
    entry = first["cases"]["tiny/direct"]
    entry["reason"] = "n/a"
    second = record([small(1.2)], first)
    assert second["cases"]["tiny/direct"]["original"] == {"wall_s": 1.0}
    second["cases"]["tiny/direct"]["reason"] = "The scan now reads every part before asking."
    third = record([small(1.5)], second)
    assert third["cases"]["tiny/direct"]["original"] == {"wall_s": 1.0}
    assert third["cases"]["tiny/direct"]["raised_from"] == {"wall_s": 1.2}  # one step only; original is the memory


def test_legacy_entries_seed_original_from_raised_from():
    legacy = {"cases": {"tiny/direct": {"wall_s": 0.9, "raised_from": {"wall_s": 0.5}, "reason": "Old raise explained."}}}
    assert record([small(0.9)], legacy)["cases"]["tiny/direct"]["original"] == {"wall_s": 0.5}
    assert record([small(0.4)], {"cases": {"tiny/direct": {"wall_s": 0.5}}})["cases"]["tiny/direct"]["original"] == {"wall_s": 0.5}


def test_cumulative_drift_fails_even_when_each_step_was_small():
    base = {"cases": {"mid/direct": {"wall_s": 3.0, "original": {"wall_s": 2.0}, "raised_from": {"wall_s": 2.6},
                                     "reason": "A long enough reason to count as an explanation.",
                                     "previous_reason": "Something else entirely."}}}
    # Within +10% + slack of the baseline (3.0 -> 3.65) but past original 2.0 * 1.5 + 0.35 = 3.35.
    problems = check([small(3.4, "mid")], base)
    assert len(problems) == 1 and "cumulative limit 3.35s" in problems[0] and "first recorded 2.00s" in problems[0]
    assert check([small(3.3, "mid")], base) == []


def test_drift_past_the_warning_tolerance_is_reported_but_does_not_fail():
    from tests.corpus.timing_gate import drift_notes
    base = {"cases": {"mid/direct": {"wall_s": 3.0, "original": {"wall_s": 2.0}}}}
    assert drift_notes([small(2.8, "mid")], base) == []  # under 2.0 * 1.25 + 0.35 = 2.85
    notes = drift_notes([small(3.0, "mid")], base)
    assert len(notes) == 1 and "+50%" in notes[0] and "warns past 2.85s" in notes[0]
    assert check([small(3.0, "mid")], base) == []
    assert drift_notes([small(3.4, "mid")], base) == []  # past the blocking limit it is a problem instead


def test_no_case_stands_past_the_drift_warning_on_main():
    import json
    from tests.corpus.timing_gate import drift_notes
    data = json.loads((Path(__file__).parent / "timing_baseline.json").read_text())
    rows = [{"case": k.split("/")[0], "transport": k.split("/")[1], "result": "PASS",
             **{m: v for m, v in e.items() if m in ("wall_s", "first_prompt_s", "fit_check_s")}}
            for k, e in data["cases"].items()]
    assert check(rows, data) == []
    # The parts-512 pair used to be pinned here as the known +38% drift; that figure came
    # from a raised_from (3.77 s) no recorded baseline in the file's history supports.
    assert drift_notes(rows, data) == []


def test_parts_512_original_is_the_first_recording_not_the_unsubstantiated_raise():
    import json
    data = json.loads((Path(__file__).parent / "timing_baseline.json").read_text())
    for transport, first in (("direct", 4.74), ("standalone", 4.75)):
        entry = data["cases"][f"parts-512-four-color/{transport}"]
        assert entry["original"]["wall_s"] == first
        assert entry["raised_from"]["wall_s"] == first
        assert "3.77" not in entry["reason"] or "appears nowhere" in entry["reason"]


def test_every_checked_in_entry_stores_its_original_explicitly():
    import json
    for name in ("timing_baseline.json", "timing_baseline_real_slicer.json"):
        data = json.loads((Path(__file__).parent / name).read_text())
        for k, entry in data["cases"].items():
            assert entry.get("original"), f"{name} {k} has no explicit original"


def test_the_issue_1142_synthetic_regression_fails_and_the_long_case_passes():
    base = {"tolerance": 0.10, "slack_s": 0.5,
            "cases": {"tiny/direct": {"wall_s": 0.01}, "big/direct": {"wall_s": 10.0}}}
    rows = lambda t, b: [small(t, "tiny"), small(b, "big")]  # noqa: E731
    # A 0.01 s case measured at 0.45 s (45x slower) must fail: the floored slack is 0.35 s,
    # so the limit is 0.01 * 1.1 + 0.35 = 0.361 s. Under the old flat 0.5 s slack it passed.
    assert any("wall_s 0.45s > limit 0.36s" in p for p in check(rows(0.45, 10.4), base))
    # A 10 s case measured at 10.4 s is inside +10% plus 10% relative slack (limit 12.0 s).
    assert check(rows(0.36, 10.4), base) == []


def test_main_wires_absolute_slack_only_for_the_real_orca_leg():
    ci = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    mock = ci.split("Timing gate", 1)[1].split("- name:", 1)[0]
    assert "--absolute-slack" not in mock
    real = ci.split("Compare with the real-slicer baseline", 1)[1].split("- name:", 1)[0]
    assert "--absolute-slack" in real


def _ci_jobs():
    from pathlib import Path

    import yaml
    path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    return yaml.safe_load(path.read_text())["jobs"]


def test_mock_leg_stays_a_blocking_gate():
    job = _ci_jobs()["discord-timing"]
    assert not job.get("continue-on-error")
    gate = next(s["run"] for s in job["steps"] if s.get("name", "").startswith("Timing gate"))
    assert "--warn-only" not in gate and "timing/run-3.json" in gate


def test_real_slicer_leg_is_reporting_only_and_skips_the_slow_case():
    import json
    from pathlib import Path
    job = _ci_jobs()["slicer-timing-real"]
    assert job["continue-on-error"] is True and "reporting only" in job["name"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "--warn-only" in runs and "--baseline tests/corpus/timing_baseline_real_slicer.json" in runs
    assert "--real-slicer" in runs and "for i in 1 2 3" in runs
    cases = job["env"]["TIMING_CASES"].split("--case")
    names = {c.strip() for c in cases if c.strip()}
    assert names and "must-shrink" not in names and "stl-must-shrink" not in names
    # The large-plate case (#952, decision D4) stays in the leg: removing it silently would
    # reopen the gap where no timed case exercises a 50 MB class slice.
    assert "stl-large-plate" in names
    real = json.loads((Path(__file__).parent / "timing_baseline_real_slicer.json").read_text())
    assert {k.split("/")[0] for k in real["cases"]} <= names
    from tests.corpus import harness
    assert names <= {f.name for f in harness.case_dirs()}


def test_real_leg_stays_reporting_only_until_the_variance_record_allows_blocking():
    """Promotion is decided by measured CI noise (timing_variance_real_slicer.json), not by hand.

    The record holds a leave-one-out over green main runs: each run recorded as the release baseline and every
    other run gated against it. The real leg may block only when that shows zero failing pairings at the
    baseline's own tolerance and slack_s, and the record must have been measured at those limits."""
    import json
    from pathlib import Path
    here = Path(__file__).parent
    real = json.loads((here / "timing_baseline_real_slicer.json").read_text())
    noise = json.loads((here / "timing_variance_real_slicer.json").read_text())
    assert noise["baseline_limits"] == {"tolerance": real["tolerance"], "slack_s": real["slack_s"]}
    assert noise["runs"] >= 20 and noise["source"], "measure enough green main runs and say which"
    at = noise["leave_one_out_at_baseline"]
    assert (at["tolerance"], at["slack_s"]) == (real["tolerance"], real["slack_s"])
    job = _ci_jobs()["slicer-timing-real"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    blocking = not job.get("continue-on-error") and "--warn-only" not in runs
    if at["failing_pairings"]:
        assert not blocking, (f"{at['failing_pairings']}/{at['pairings']} unchanged-code pairings fail at these "
                              "limits; the real leg must stay --warn-only and continue-on-error")


def test_timing_variance_leave_one_out(tmp_path):
    import json
    from tests.corpus import timing_variance

    def run_dir(name, wall):
        d = tmp_path / name
        d.mkdir()
        for i in (1, 2, 3):
            (d / f"run-{i}.json").write_text(json.dumps({"rows": [row(wall)]}))
        return d

    dirs = [run_dir("a", 4.0), run_dir("b", 4.2), run_dir("c", 9.0)]
    (tmp_path / "short").mkdir()  # fewer than 3 repeats: ignored
    runs = timing_variance.load_runs([*dirs, tmp_path / "short"])
    assert sorted(runs) == ["a", "b", "c"]
    # a<->b within 10% + 1 s both ways; c is far above either, and a/b recorded from c pass.
    assert timing_variance.leave_one_out(runs, 0.10, 1.0)[:2] == (2, 6)
    out = timing_variance.measure(runs, {"tolerance": 0.10, "slack_s": 1.0, "cases": {}}, "test")
    assert out["leave_one_out_at_baseline"]["failing_pairings"] == 2
    assert out["spread"]["single-part/direct wall_s"]["max"] == 9.0


# ----------------------------------------------------------------------------- #1142: drift printed on every raise

def test_summary_lists_cumulative_drift_for_every_raised_entry_not_only_the_warning_band():
    from tests.corpus.timing_gate import summary
    base = {"tolerance": 0.10, "slack_s": 0.5,
            "cases": {"tiny/direct": {"wall_s": 1.1, "original": {"wall_s": 1.0},
                                      "raised_from": {"wall_s": 1.0},
                                      "reason": "A long enough reason for this raise."}}}
    text = summary([small(1.1)], base, [])
    assert "Cumulative drift" in text and "tiny/direct" in text and "+10%" in text


def test_summary_without_any_raise_lists_no_cumulative_drift():
    from tests.corpus.timing_gate import summary
    base = {"tolerance": 0.10, "slack_s": 0.5,
            "cases": {"tiny/direct": {"wall_s": 1.0, "original": {"wall_s": 1.0}}}}
    assert "Cumulative drift" not in summary([small(1.0)], base, [])


def test_record_prints_cumulative_drift_for_a_raise_and_stdout_stays_pure_json(tmp_path, capsys):
    import json
    from tests.corpus.timing_gate import main
    old = {"tolerance": 0.10, "slack_s": 0.5,
           "cases": {"tiny/direct": {"wall_s": 1.0, "original": {"wall_s": 1.0}}}}
    base = tmp_path / "base.json"
    base.write_text(json.dumps(old))
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"rows": [small(1.1)]}))
    assert main([str(run), "--baseline", str(base), "--record"]) == 0
    out, err = capsys.readouterr()
    json.loads(out)  # CI redirects stdout to proposed-baseline.json; drift must not corrupt it
    assert "tiny/direct" in err and "wall_s" in err and "+10%" in err and "first recorded 1.00s" in err


def test_record_without_a_raise_prints_no_drift(tmp_path, capsys):
    import json
    from tests.corpus.timing_gate import main
    old = {"tolerance": 0.10, "slack_s": 0.5,
           "cases": {"tiny/direct": {"wall_s": 1.0, "original": {"wall_s": 1.0}}}}
    base = tmp_path / "base.json"
    base.write_text(json.dumps(old))
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"rows": [small(1.0)]}))
    assert main([str(run), "--baseline", str(base), "--record"]) == 0
    _, err = capsys.readouterr()
    assert err == ""
