"""The timing artifacts name the runner CPU (#1332).

The #1160 bisect of the Discord-flow timing rise after #1165 downloaded
the discord-timing artifact of every main CI run from #1165 to #1188 and
beyond. Summed over the 108 flows common to all of them, the same code
lands in speed bands about 0.9x, 1.2x and 1.55x of the pre-#1165 run:
4957c67a, which already holds #1172, #1174, #1177 and #1181, ran at
0.98x, while e2a8c9ae, before it, ran at 1.56x, and 3766178d, a day
later, at 0.85x. No change in the window added time; #1188 re-recorded
on a slow runner. The artifact carried no CPU name, so nobody could tell.
"""
import json

from tests.corpus import time_corpus
from tests.corpus.timing_gate import main, runner_cpus


def test_cpu_model_reads_the_linux_model_name(tmp_path):
    info = tmp_path / "cpuinfo"
    info.write_text("processor\t: 0\nvendor_id\t: AuthenticAMD\nmodel name\t: AMD EPYC 7763 64-Core Processor\n")
    assert time_corpus.cpu_model(str(info)) == "AMD EPYC 7763 64-Core Processor"


def test_cpu_model_never_raises_without_cpuinfo(tmp_path):
    assert isinstance(time_corpus.cpu_model(str(tmp_path / "missing")), str)


def _run(tmp_path, name, cpu):
    path = tmp_path / name
    path.write_text(json.dumps({"cpu_model": cpu, "rows": [
        {"case": "single-part", "transport": "direct", "result": "PASS", "wall_s": 4.0}]}))
    return str(path)


def test_the_gate_summary_and_a_record_name_the_runner_cpu(tmp_path, capsys):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"tolerance": 0.25, "slack_s": 1.0,
                                "cases": {"single-part/direct": {"wall_s": 4.0}}}))
    runs = [_run(tmp_path, f"run-{i}.json", "AMD EPYC 7763") for i in range(3)]
    summary = tmp_path / "summary.md"
    assert main([*runs, "--baseline", str(base), "--summary", str(summary)]) == 0
    assert "Runner CPU: AMD EPYC 7763." in summary.read_text()
    capsys.readouterr()
    assert main([*runs, "--baseline", str(base), "--record", "--recorded-from", "CI run 1 (abc)"]) == 0
    proposed = json.loads(capsys.readouterr().out)
    assert proposed["recorded_from"] == "CI run 1 (abc), runner CPU AMD EPYC 7763"


def test_runner_cpus_lists_each_model_once_and_skips_unrecorded():
    assert runner_cpus([{"cpu_model": "A"}, {"cpu_model": "B"}, {"cpu_model": "A"}, {}]) == ["A", "B"]


# ---------------------------------------------- per-CPU baselines (catduck, 2026-11-15)

def _base(tmp_path, **extra):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"tolerance": 0.1, "slack_s": 0.5, "slack_floor_s": 0.1, "slack_rel": 0.0,
                                "cases": {"single-part/direct": {"wall_s": 4.0}}, **extra}))
    return base


def test_a_run_on_a_cpu_with_its_own_profile_is_gated_against_that_profile(tmp_path):
    # 6.0 s fails the 4.0 s default, but this CPU's own recording is 5.8 s, so it passes.
    base = _base(tmp_path, by_cpu={"AMD EPYC 9V45": {"recorded_from": "CI run 2",
                                                     "cases": {"single-part/direct": {"wall_s": 5.8}}}})
    runs = [_run_wall(tmp_path, f"r{i}.json", "AMD EPYC 9V45", 6.0) for i in range(3)]
    assert main([*runs, "--baseline", str(base)]) == 0


def test_a_cpu_with_no_profile_falls_back_to_the_default(tmp_path):
    base = _base(tmp_path, by_cpu={"AMD EPYC 9V45": {"recorded_from": "CI run 2",
                                                     "cases": {"single-part/direct": {"wall_s": 5.8}}}})
    runs = [_run_wall(tmp_path, f"r{i}.json", "INTEL XEON 8573C", 6.0) for i in range(3)]
    assert main([*runs, "--baseline", str(base)]) == 1


def test_the_profile_still_fails_a_real_slowdown_on_its_own_cpu(tmp_path):
    base = _base(tmp_path, by_cpu={"AMD EPYC 9V45": {"recorded_from": "CI run 2",
                                                     "cases": {"single-part/direct": {"wall_s": 5.8}}}})
    runs = [_run_wall(tmp_path, f"r{i}.json", "AMD EPYC 9V45", 8.0) for i in range(3)]
    assert main([*runs, "--baseline", str(base)]) == 1


def test_per_cpu_record_adds_a_profile_and_keeps_the_default_and_other_cpus(tmp_path, capsys):
    base = _base(tmp_path, recorded_from="CI run 1",
                 by_cpu={"OTHER": {"recorded_from": "CI run 0", "cases": {"single-part/direct": {"wall_s": 3.0}}}})
    runs = [_run_wall(tmp_path, f"r{i}.json", "AMD EPYC 9V45", 6.0) for i in range(3)]
    assert main([*runs, "--baseline", str(base), "--record", "--per-cpu", "--recorded-from", "CI run 3 (abc)"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cases"] == {"single-part/direct": {"wall_s": 4.0}}  # default untouched
    assert out["recorded_from"] == "CI run 1"
    assert out["by_cpu"]["OTHER"]["cases"]["single-part/direct"]["wall_s"] == 3.0
    prof = out["by_cpu"]["AMD EPYC 9V45"]
    assert prof["recorded_from"] == "CI run 3 (abc), runner CPU AMD EPYC 9V45"
    entry = prof["cases"]["single-part/direct"]
    # A CPU's first profile is a fresh recording: not a "raise" over another CPU's numbers.
    assert entry["wall_s"] == 6.0 and "raised_from" not in entry


def test_a_second_record_on_the_same_cpu_is_a_raise_that_needs_a_reason(tmp_path, capsys):
    base = _base(tmp_path, by_cpu={"AMD EPYC 9V45": {"recorded_from": "CI run 2",
                                                     "cases": {"single-part/direct": {"wall_s": 5.8}}}})
    runs = [_run_wall(tmp_path, f"r{i}.json", "AMD EPYC 9V45", 6.0) for i in range(3)]
    assert main([*runs, "--baseline", str(base), "--record", "--per-cpu"]) == 0
    entry = json.loads(capsys.readouterr().out)["by_cpu"]["AMD EPYC 9V45"]["cases"]["single-part/direct"]
    assert entry["raised_from"] == {"wall_s": 5.8} and entry["reason"] == ""


def test_per_cpu_record_refuses_mixed_cpus(tmp_path):
    base = _base(tmp_path)
    runs = [_run_wall(tmp_path, "a.json", "A", 4.0), _run_wall(tmp_path, "b.json", "B", 4.0)]
    try:
        main([*runs, "--baseline", str(base), "--record", "--per-cpu"])
    except SystemExit as exc:
        assert "exactly one runner CPU" in str(exc)
    else:
        raise AssertionError("mixed CPUs must be refused")


def _run_wall(tmp_path, name, cpu, wall):
    path = tmp_path / name
    path.write_text(json.dumps({"cpu_model": cpu, "rows": [
        {"case": "single-part", "transport": "direct", "result": "PASS", "wall_s": wall}]}))
    return str(path)
