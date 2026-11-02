"""Contracts for the corpus runners and the harness's fit-probe timing.

P2-281: the mock capacity probe must keep the production function's name,
or the name-based fit-check timing never records it.
P2-177: a pinned intake refusal must verify the refusal reason and that
nothing was sliced or started — any FAILED receipt is not enough.
P1-58: the external runner must exit nonzero when any model fails.
P2-178: the external runner must honor a case folder's case.toml
expectations (the complex cases extracted into the shared corpus root)
instead of expecting STARTED for every model.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.corpus import harness, run_external_corpus  # noqa: E402


# --------------------------------------------------------------------- P2-281

def test_mock_capacity_probe_keeps_the_production_name():
    probe = harness.model_capacity(lambda *a, **k: 0, lambda p: [], lambda p: 1)
    assert probe.__name__ == "orca_plate_capacity"
    assert probe.__name__ in harness.FIT_CHECK_STEPS


def test_already_fitting_case_records_the_capacity_probe(tmp_path):
    """P2-281 behavioral pin, on an already-fitting case (no shrink search):
    the mock capacity probe's fit-check events are recorded, so the timing
    job's fit_check_s is a number instead of None. Also the regression pin
    for the harness's offline verify stub, which must accept the adapter /
    state / printer_id arguments verify_gcode gained (bonus)."""
    from tests.corpus import time_corpus
    case = harness.load_case(Path(__file__).parent / "cases" / "core-one-color")
    run = harness.run_case(case, tmp_path / "run")
    harness.check_expectations(case, run)
    fit_names = [event[1] for event in run.events if event[0] == "fit_check_start"]
    assert "orca_plate_capacity" in fit_names
    assert time_corpus._phases(run)["fit_check_s"] is not None


# --------------------------------------------------------------------- P2-177

def refused_run(error="STL exceeds 32 MiB limit", **overrides):
    run = S(adapter=S(unanswered=[], errors=[], questions=[], sent=[]),
            receipt={"status": "FAILED", "error": error},
            slice_starts=[], starts=[])
    for key, value in overrides.items():
        setattr(run, key, value)
    return run


def refusal_case(**expect):
    return S(name="refused", expect={"status": "FAILED", **expect})


def test_check_refusal_accepts_the_pinned_reason():
    harness.check_refusal(refusal_case(error_contains="STL exceeds 32 MiB limit"), refused_run())


def test_check_refusal_rejects_a_different_failure():
    with pytest.raises(AssertionError, match="refusal reason"):
        harness.check_refusal(refusal_case(error_contains="STL exceeds 32 MiB limit"),
                              refused_run(error="something else broke"))


def test_check_refusal_rejects_a_slice_or_a_start():
    with pytest.raises(AssertionError, match="never reach slicing"):
        harness.check_refusal(refusal_case(), refused_run(slice_starts=[("slice_start", "x")]))
    with pytest.raises(AssertionError, match="never start"):
        harness.check_refusal(refusal_case(), refused_run(starts=["job"]))


def test_check_refusal_keeps_the_adapter_error_checks():
    with pytest.raises(AssertionError, match="adapter exploded"):
        harness.check_refusal(refusal_case(),
                              refused_run(adapter=S(unanswered=[], errors=["adapter exploded"],
                                                    questions=[], sent=[])))


def test_pinned_refusal_cases_name_their_reason():
    from tests.corpus import fetch_complex_corpus
    for slug, needle in (("fnaf-mask", "STL exceeds 32 MiB limit"),
                         ("gyroid-surface", "STL exceeds 32 MiB limit"),
                         ("chinese-dragon", "model XML exceeds 400 MiB limit")):
        assert fetch_complex_corpus.CASE_EXPECT[slug]["error_contains"] == needle


# --------------------------------------------------------------------- P1-58 + P2-178

def write_model(root, name="case-a", case_toml=None):
    folder = root / name
    folder.mkdir(parents=True)
    model = folder / "model.stl"
    model.write_bytes(b"solid x\nendsolid x\n")
    if case_toml is not None:
        (folder / "case.toml").write_text(case_toml)
    return model


def started_run():
    return S(adapter=S(unanswered=[], errors=[], questions=[], sent=[]),
             receipt={"status": "STARTED"},
             slice_starts=[("slice_start", "job")], events=[("slice", ["orca"])],
             starts=["job"], job={}, gcodes=[], state={})


def run_main(tmp_path, monkeypatch, run_case):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setenv("PRINT_HANDY_PRIVATE_CORPUS", str(root))
    monkeypatch.setenv("CORPUS_SHARD_INDEX", "0")
    monkeypatch.setenv("CORPUS_SHARD_COUNT", "1")
    monkeypatch.setenv("CORPUS_WORK", str(tmp_path / "work"))
    results_path = tmp_path / "results.json"
    monkeypatch.setenv("CORPUS_RESULTS", str(results_path))
    monkeypatch.setattr(harness, "run_case", run_case)
    return root, results_path


def test_external_runner_exits_nonzero_when_a_model_fails(tmp_path, monkeypatch):
    root, results_path = run_main(tmp_path, monkeypatch,
                                  lambda case, work: (_ for _ in ()).throw(RuntimeError("boom")))
    write_model(root)
    with pytest.raises(SystemExit) as excinfo:
        run_external_corpus.main()
    assert excinfo.value.code
    rows = json.loads(results_path.read_text())
    assert rows[0]["result"] == "FAIL"  # results are still written for the artifacts


def test_external_runner_passes_a_started_case(tmp_path, monkeypatch):
    root, results_path = run_main(tmp_path, monkeypatch, lambda case, work: started_run())
    write_model(root)
    assert run_external_corpus.main() == 0
    assert json.loads(results_path.read_text())[0]["result"] == "PASS"


def test_external_runner_honors_a_case_toml_refusal(tmp_path, monkeypatch):
    root, results_path = run_main(tmp_path, monkeypatch, lambda case, work: refused_run())
    write_model(root, case_toml='[expect]\nstatus = "FAILED"\nerror_contains = "STL exceeds 32 MiB limit"\n')
    assert run_external_corpus.main() == 0
    assert json.loads(results_path.read_text())[0]["result"] == "PASS"


def test_external_runner_fails_a_case_toml_refusal_with_the_wrong_reason(tmp_path, monkeypatch):
    root, results_path = run_main(
        tmp_path, monkeypatch, lambda case, work: refused_run(error="unrelated intake crash"))
    write_model(root, case_toml='[expect]\nstatus = "FAILED"\nerror_contains = "STL exceeds 32 MiB limit"\n')
    with pytest.raises(SystemExit):
        run_external_corpus.main()
    assert json.loads(results_path.read_text())[0]["result"] == "FAIL"
