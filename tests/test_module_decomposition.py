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
    "print_handy.intake.worker": "print_handy.core.prepared_intake",
    "print_handy.intake.questions": "print_handy.core.prepared_intake",
    "print_handy.intake.support_flow": "print_handy.core.prepared_intake",
    "print_handy.intake.fit_flow": "print_handy.core.prepared_intake",
}
# Packages and modules the decomposition adds: none may sit in an import cycle.
NEW_MODULES = ("print_handy.gcode", "print_handy.jobs", "print_handy.launch", "print_handy.intake",
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
