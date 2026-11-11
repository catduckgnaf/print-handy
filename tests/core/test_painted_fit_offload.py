"""The painted-plate fit runs in the heavy worker, not a GIL-holding thread (#1153 item 8).

``painted_fit_for_job`` exports the painted 3MF's geometry at each ladder
scale: pure-Python work that, in ``owned_worker.in_thread``, held the GIL
for stretches long enough to stall the chat loop. It is now an offloaded
step like the audit and the color inventory. These tests pin that it is
routed to the worker, and that the worker's answer is the in-process one.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from print_handy.core import heavy_worker, model_job, owned_worker, prepared_intake
from print_handy.core.source_colors import canonical_prompts, inventory

ROOT = Path(__file__).resolve().parents[2]
PAINTED = ROOT / "tests" / "corpus" / "cases" / "prusa-mmu-project" / "model.3mf"


def _decisions(path):
    manifest = inventory(path)
    assert manifest["painted"], "the fixture must be a painted source"
    return {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
            "choices": {row["key"]: {"slot": n + 1} for n, row in enumerate(canonical_prompts(manifest))}}


def test_painted_fit_is_an_offloaded_step():
    entry = ("model_job", "painted_fit_for_job")
    assert entry in heavy_worker.ALLOWED
    assert entry in prepared_intake._OFFLOAD
    assert entry not in prepared_intake._SLICER_STEPS  # geometry only: the general lane, never the slicer's
    assert prepared_intake._STEP_LABELS["painted_fit_for_job"] == "the bed fit check"


def test_heavy_sends_the_painted_fit_to_the_worker_not_a_thread(monkeypatch):
    seen = {}

    async def fake_run(argv, payload, **kwargs):
        seen["request"], seen["lane"] = json.loads(payload), kwargs["lane"]
        return ("\n" + json.dumps({"ok": True, "path": False, "result": {"scale": 1.0}})).encode(), b""

    async def no_thread(*args, **kwargs):
        raise AssertionError("the painted fit must not run in a thread")

    monkeypatch.setattr(prepared_intake.owned_worker, "run", fake_run)
    monkeypatch.setattr(prepared_intake.owned_worker, "in_thread", no_thread)
    decisions = _decisions(PAINTED)
    result = asyncio.run(prepared_intake._heavy(model_job.painted_fit_for_job, PAINTED, decisions, None))
    assert result == {"scale": 1.0}
    assert seen["request"]["module"] == "model_job" and seen["request"]["func"] == "painted_fit_for_job"
    assert seen["request"]["args"] == [str(PAINTED), decisions, None]
    assert seen["lane"] is owned_worker.GENERAL


def test_the_worker_answers_exactly_what_the_in_process_fit_does():
    decisions = _decisions(PAINTED)
    expected = model_job.painted_fit_for_job(PAINTED, decisions, None)
    payload = json.dumps({"module": "model_job", "func": "painted_fit_for_job",
                          "args": [str(PAINTED), decisions, None], "kwargs": {}})
    done = subprocess.run([sys.executable, "-m", "print_handy.core.heavy_worker"], input=payload.encode(),
                          capture_output=True, cwd=ROOT, timeout=120, check=True)
    reply = json.loads(done.stdout.decode().rstrip().rsplit("\n", 1)[-1])
    assert reply["ok"] and not reply["path"]
    assert reply["result"] == expected
    assert expected["scale"] is not None


def test_one_painted_fit_inventories_the_source_once(monkeypatch):
    # #1153 item 8, manifest half: the fit's own read and the geometry export
    # it tries both asked for the color manifest, and each decoded every
    # painted triangle again. Inside the fit's inventory scope the source is
    # inventoried once (keyed by its content hash) and the answer is unchanged.
    from print_handy.core import source_colors
    decisions = _decisions(PAINTED)
    expected = model_job._painted_fit_for_job(PAINTED, decisions, None)
    calls = []
    real = source_colors._inventory

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(source_colors, "_inventory", counting)
    assert model_job.painted_fit_for_job(PAINTED, decisions, None) == expected
    assert len(calls) == 1
    calls.clear()
    model_job._painted_fit_for_job(PAINTED, decisions, None)
    assert len(calls) > 1, "without the scope the fit inventories the source more than once"
