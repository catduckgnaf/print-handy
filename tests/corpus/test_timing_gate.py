"""The Discord timing gate fails on slowdowns, missing runs and unexplained baseline raises."""
from tests.corpus.timing_gate import check, record

BASE = {"tolerance": 0.25, "slack_s": 1.0,
        "cases": {"single-part/hermes": {"wall_s": 4.0, "first_prompt_s": 2.0}}}


def row(wall, prompt=2.0, result="PASS"):
    return {"case": "single-part", "transport": "hermes", "result": result,
            "wall_s": wall, "first_prompt_s": prompt}


def test_within_tolerance_passes():
    assert check([row(5.9)], BASE) == []  # limit 4 * 1.25 + 1 = 6.0


def test_slower_than_baseline_fails():
    problems = check([row(6.1)], BASE)
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
    proposed = record([row(8.0)], BASE)
    entry = proposed["cases"]["single-part/hermes"]
    assert entry["raised_from"] == {"wall_s": 4.0, "first_prompt_s": 2.0}
    assert any("without a reason" in p for p in check([row(8.0)], proposed))
    entry["reason"] = "full mesh validation added, accepted"
    assert check([row(8.0)], proposed) == []


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
            "cases": {"single-part/hermes": {"wall_s": 4.0, "first_prompt_s": 2.0, "fit_check_s": 1.0}}}
    assert check([fit_row(2.2)], base) == []  # 1 * 1.25 + 1 = 2.25
    assert any("fit_check_s" in p for p in check([fit_row(2.3)], base))


def test_fit_check_is_measured_from_the_real_intake_flow(tmp_path):
    from tests.corpus import harness, time_corpus
    case = next(harness.load_case(f) for f in harness.case_dirs() if f.name == "must-shrink")
    row_ = time_corpus.run_one(case, "hermes", False, tmp_path)
    assert row_["result"] == "PASS", row_
    assert row_["fit_check_s"] is not None and 0 <= row_["fit_check_s"] <= row_["wall_s"]


def test_missing_fit_measurement_fails_for_required_case_only():
    req = dict(BASE, fit_check_required=["single-part/hermes"])
    assert any("not measured" in p for p in check([row(4.0)], req))
    assert check([fit_row(0.5)], req) == []
    assert check([row(4.0)], BASE) == []  # not required: no measurement is fine
    assert record([row(4.0)], req)["fit_check_required"] == ["single-part/hermes"]
    from tests.corpus.timing_gate import median_rows
    merged = median_rows([[fit_row(0.5)], [row(4.0)], [fit_row(0.6)]])
    assert any("not measured" in p for p in check(merged, req))


# ----------------------------------------------------------------------------- #734: +10%, reporting leg, release step

def test_default_limits_are_the_issue_ten_percent_with_absolute_slack():
    from tests.corpus.timing_gate import DEFAULT_SLACK_S, DEFAULT_TOLERANCE, limits
    assert (DEFAULT_TOLERANCE, DEFAULT_SLACK_S) == (0.10, 0.5)
    bare = {"cases": {"single-part/hermes": {"wall_s": 10.0}}}
    assert limits(bare) == (0.10, 0.5)
    assert check([row(11.5)], bare) == []  # 10 * 1.10 + 0.5 = 11.5
    assert any("wall_s 11.60s > limit 11.50s" in p for p in check([row(11.6)], bare))


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
    assert "raised_from" not in proposed["cases"]["single-part/hermes"]


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
    assert main([*runs, "--baseline", str(base)]) == 1
    summary = tmp_path / "summary.md"
    assert main([*runs, "--baseline", str(base), "--warn-only", "--summary", str(summary), "--title", "T"]) == 0
    out = capsys.readouterr().out
    assert "::warning title=Timing (reporting only)::single-part/hermes: wall_s 9.10s" in out
    text = summary.read_text()
    assert text.startswith("### T") and "| single-part/hermes | PASS | wall_s | 9.10 | 4.00 | 6.00 | +12" in text and "% OVER |" in text


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
    real = json.loads((Path(__file__).parent / "timing_baseline_real_slicer.json").read_text())
    assert {k.split("/")[0] for k in real["cases"]} <= names
    from tests.corpus import harness
    assert names <= {f.name for f in harness.case_dirs()}
