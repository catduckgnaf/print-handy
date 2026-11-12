"""The #1323 module decomposition: old import paths, patch forwarding, no new import cycles.

Code moved out of ``print_handy.core.model_job`` keeps its old import path for
one release. The old module re-exports every moved name and forwards
assignments (``mock.patch``, ``monkeypatch.setattr``) to the new home, so a
caller or test that patches the old path still changes what the moved code
sees. The modules the decomposition adds sit in no import cycle.
"""
import ast
import importlib
from pathlib import Path
from unittest import mock

import pytest

from print_handy import _relocated
from print_handy.core import model_job

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "print_handy"

# Each new home and the old module that still answers for its names.
HOMES = {
    "print_handy.gcode.bounds": "print_handy.core.model_job",
    "print_handy.gcode.verify": "print_handy.core.model_job",
    "print_handy.gcode.artifact": "print_handy.core.model_job",
    "print_handy.jobs.common": "print_handy.core.model_job",
    "print_handy.jobs.store": "print_handy.core.model_job",
    "print_handy.jobs.filament": "print_handy.core.model_job",
    "print_handy.launch.gates": "print_handy.core.model_job",
    "print_handy.launch.print_now": "print_handy.core.model_job",
    "print_handy.jobs.slice": "print_handy.core.model_job",
    "print_handy.jobs.sources": "print_handy.core.model_job",
    "print_handy.jobs.audit": "print_handy.core.model_job",
    "print_handy.jobs.prompts": "print_handy.core.model_job",
    "print_handy.jobs.cli": "print_handy.core.model_job",
    "print_handy.intake.worker": "print_handy.core.prepared_intake",
    "print_handy.intake.questions": "print_handy.core.prepared_intake",
    "print_handy.intake.support_flow": "print_handy.core.prepared_intake",
    "print_handy.intake.fit_flow": "print_handy.core.prepared_intake",
}
# Packages and modules the decomposition adds: none may sit in an import cycle.
NEW_MODULES = ("print_handy.gcode", "print_handy.jobs", "print_handy.launch", "print_handy.intake", "print_handy.bot.handlers",
               "print_handy._relocated")


def _moved_names(old, home):
    """Names the old module imports from ``home`` in its re-export block."""
    tree = ast.parse(Path(importlib.import_module(old).__file__).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            resolved = ".".join(old.split(".")[:-node.level] + [node.module])
            if resolved == home:
                names += [a.name for a in node.names]
    return names


@pytest.mark.parametrize("home", sorted(HOMES))
def test_every_moved_name_is_the_same_object_on_the_old_path(home):
    old = importlib.import_module(HOMES[home])
    new = importlib.import_module(home)
    names = _moved_names(HOMES[home], home)
    assert names, f"{HOMES[home]} re-exports nothing from {home}"
    for name in names:
        assert getattr(old, name) is getattr(new, name), name


def test_from_import_on_the_old_path_still_works():
    from print_handy.core.model_job import (  # noqa: F401
        BOUNDS_CACHE, file_sha256, gcode_motion_bounds, request_json, verify_gcode, zmod_md5_valid)


def test_patching_the_old_path_reaches_the_moved_code(tmp_path):
    from print_handy.gcode import bounds, verify
    from print_handy.jobs import common
    with mock.patch.object(model_job, "BOUNDS_CACHE", tmp_path):
        assert bounds.BOUNDS_CACHE == tmp_path
    assert bounds.BOUNDS_CACHE is model_job.BOUNDS_CACHE != tmp_path
    fake = mock.Mock(return_value={"result": {}})
    with mock.patch.object(model_job, "_mr", fake):
        assert verify._mr is fake and common._mr is fake
    assert verify._mr is common._mr is model_job._mr is not fake


def test_monkeypatch_on_the_old_path_reaches_the_moved_code(monkeypatch):
    from print_handy.jobs import common
    sentinel = object()
    monkeypatch.setattr(model_job, "request_json", sentinel)
    assert common.request_json is sentinel
    monkeypatch.undo()
    assert common.request_json is model_job.request_json is not sentinel


def test_live_state_reads_and_writes_the_new_home():
    from print_handy.gcode import bounds
    assert "_native_bounds_broken" not in vars(model_job)
    with mock.patch.object(model_job, "_native_bounds_broken", True):
        assert bounds._native_bounds_broken is True and model_job._native_bounds_broken is True
    assert bounds._native_bounds_broken is model_job._native_bounds_broken
    model_job._reset_native_bounds_cache()
    assert model_job._native_bounds_broken is False


def test_an_unrelated_name_with_another_value_is_not_forwarded():
    import types
    old, home = types.ModuleType("old_x"), types.ModuleType("home_x")
    old.shared = home.shared = object()
    old.mine, home.mine = 1, 2
    import sys
    sys.modules["old_x"] = old
    try:
        _relocated.keep_old_path("old_x", home)
        old.shared = "patched"
        old.mine = 3
        assert home.shared == "patched" and home.mine == 2
    finally:
        del sys.modules["old_x"]


def test_patching_a_start_gate_on_the_old_path_steers_print_now(monkeypatch):
    """The gate seams tests and callers patch on model_job still reach the moved start path."""
    from print_handy.launch import gates, print_now
    refuse = mock.Mock(side_effect=RuntimeError("bed clear refused"))
    monkeypatch.setattr(model_job, "_claim_bed_clear", refuse)
    assert gates._claim_bed_clear is refuse and print_now._claim_bed_clear is refuse
    monkeypatch.setattr(model_job, "verify_gcode", mock.Mock())
    from print_handy.gcode import verify
    assert verify.verify_gcode is model_job.verify_gcode


def test_patching_prepared_intake_reaches_the_moved_intake_flows(monkeypatch):
    from print_handy.core import prepared_intake
    from print_handy.intake import fit_flow, questions, worker
    fake_ask = mock.AsyncMock()
    monkeypatch.setattr(prepared_intake, "ask", fake_ask)
    assert questions.ask is fake_ask
    monkeypatch.setattr(prepared_intake, "settings", mock.Mock())
    assert fit_flow.settings is prepared_intake.settings and worker.settings is prepared_intake.settings
    assert prepared_intake.supports_decision.__module__ == "print_handy.intake.support_flow"
    assert worker.logger is prepared_intake.logger


def test_moved_code_keeps_the_model_job_logger():
    from print_handy.gcode import bounds
    from print_handy.jobs import store
    from print_handy.launch import print_now
    assert bounds.logger is store.logger is print_now.logger is model_job.logger


# --------------------------------------------------------------- import cycles

def _module_name(path):
    rel = path.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_level_imports(path, name, known):
    """Modules ``name`` imports at import time (top level, not inside functions)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    out = set()

    def add(target):
        while target and target not in known:
            target = target.rsplit(".", 1)[0] if "." in target else ""
        if target and target != name:
            out.add(target)

    def visit(nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package.split(".")
                    base = base[:len(base) - node.level + 1]
                    stem = ".".join(base + ([node.module] if node.module else []))
                else:
                    stem = node.module or ""
                add(stem)
                for alias in node.names:
                    if stem + "." + alias.name in known:
                        add(stem + "." + alias.name)
            elif isinstance(node, (ast.If, ast.Try)):
                visit(node.body)
                visit(getattr(node, "orelse", []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)
                visit(getattr(node, "finalbody", []))
    visit(tree.body)
    return out


def _import_graph():
    files = {_module_name(p): p for p in PACKAGE.rglob("*.py")}
    known = set(files)
    return {name: _module_level_imports(path, name, known) for name, path in files.items()}


def _cycles(graph):
    """Strongly connected components with more than one module (Tarjan)."""
    index, low, stack, on, out = {}, {}, [], set(), []
    counter = [0]

    def strong(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))

    import sys
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 10000))
    try:
        for v in graph:
            if v not in index:
                strong(v)
    finally:
        sys.setrecursionlimit(limit)
    return out


def test_no_new_module_sits_in_an_import_cycle():
    graph = _import_graph()
    for new in NEW_MODULES:
        assert any(name == new or name.startswith(new + ".") for name in graph), new
    bad = [comp for comp in _cycles(graph)
           if any(m == new or m.startswith(new + ".") for m in comp for new in NEW_MODULES)]
    assert not bad, f"import cycles through the decomposed modules: {bad}"


def test_new_modules_never_import_the_modules_they_were_cut_from():
    graph = _import_graph()
    old = {"print_handy.core.model_job", "print_handy.core.prepared_intake"}
    for name, deps in graph.items():
        if any(name == new or name.startswith(new + ".") for new in NEW_MODULES):
            assert not deps & old, f"{name} imports {sorted(deps & old)} at import time"


def test_the_cycle_finder_finds_a_cycle():
    assert _cycles({"a": {"b"}, "b": {"a"}, "c": {"a"}}) == [["a", "b"]]


def test_moved_slice_steps_still_run_in_the_heavy_worker(monkeypatch, tmp_path):
    """slice_job moved to jobs/slice.py; the intake still offloads it as ('model_job', 'slice_job')."""
    import asyncio
    import json
    from print_handy.core import heavy_worker, owned_worker, prepared_intake
    from print_handy.jobs import slice as jobs_slice
    assert model_job.slice_job is jobs_slice.slice_job
    seen = {}

    async def fake_run(argv, payload, **kwargs):
        seen["request"], seen["lane"], seen["record"] = json.loads(payload), kwargs["lane"], kwargs["record"]
        return ("\n" + json.dumps({"ok": True, "path": False, "result": "done"})).encode(), b""

    async def no_thread(*args, **kwargs):
        raise AssertionError("slicing must not run in a thread")

    monkeypatch.setattr(prepared_intake.owned_worker, "run", fake_run)
    monkeypatch.setattr(prepared_intake.owned_worker, "in_thread", no_thread)
    job = tmp_path / "JOB.json"
    assert asyncio.run(prepared_intake._heavy(model_job.slice_job, job)) == "done"
    assert (seen["request"]["module"], seen["request"]["func"]) == ("model_job", "slice_job")
    assert ("model_job", "slice_job") in heavy_worker.ALLOWED
    assert seen["lane"] is owned_worker.SLICER
    assert seen["record"] == tmp_path / owned_worker.RECORD


def test_the_old_module_still_runs_the_cli():
    """``python3 -m print_handy.core.model_job`` is the documented command line (#1323 moved it to jobs/cli.py)."""
    import subprocess
    import sys
    from print_handy.jobs import cli
    assert model_job.main is cli.main
    out = subprocess.run([sys.executable, "-m", "print_handy.core.model_job", "--help"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "print-now" in out.stdout and "fetch" in out.stdout


def test_site_dispatch_by_name_finds_a_function_patched_on_the_old_path(monkeypatch):
    """inventory_for_url looks its site function up by name; patching model_job still steers it."""
    from print_handy.jobs import sources
    fake = mock.Mock(return_value="inventory")
    monkeypatch.setattr(model_job, "maker_inventory", fake)
    assert sources.maker_inventory is fake
    assert model_job.inventory_for_url("https://makerworld.com/en/models/1") == "inventory"
    fake.assert_called_once_with("https://makerworld.com/en/models/1")


def test_every_moved_offload_step_keeps_its_offload_name():
    """Each ('model_job', step) the intake offloads resolves to a function whose home maps back to model_job."""
    from print_handy.core import heavy_worker, prepared_intake
    from print_handy.intake import worker
    for module, name in sorted(prepared_intake._OFFLOAD):
        if module != "model_job":
            continue
        func = getattr(model_job, name)
        short = worker._OFFLOAD_HOMES.get(func.__module__, func.__module__.rsplit(".", 1)[-1])
        assert short == "model_job", f"{name} moved to {func.__module__} without an _OFFLOAD_HOMES entry"
        assert (module, name) in heavy_worker.ALLOWED


def test_patching_the_handler_package_reaches_the_handler_that_uses_the_name(monkeypatch):
    from print_handy.bot import handlers
    from print_handy.bot.handlers import common, print_stop
    fake = mock.Mock()
    monkeypatch.setattr(handlers, "_routed_printer_id", fake)
    assert common._routed_printer_id is fake and print_stop._routed_printer_id is fake
    assert "_LIVE_SESSION" not in vars(handlers)
    assert handlers._LIVE_SESSION is common._LIVE_SESSION
