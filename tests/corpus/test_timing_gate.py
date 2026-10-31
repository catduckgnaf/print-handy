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
