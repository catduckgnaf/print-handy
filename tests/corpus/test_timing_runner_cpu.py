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
