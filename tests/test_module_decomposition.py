"""The #1323 module decomposition after the 2.4.0 re-export shim is gone.

Moved code lives in its new module. ``print_handy.core.model_job`` no longer
re-exports those names or forwards patches onto them. The new modules still
must not form an import cycle or import the modules they were cut from.
"""
import ast
import importlib
from pathlib import Path
from unittest import mock

import pytest

from print_handy.core import model_job

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "print_handy"

# Each new home and the old module that used to re-export it.
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
NEW_MODULES = ("print_handy.gcode", "print_handy.jobs", "print_handy.launch", "print_handy.intake", "print_handy.bot.handlers")


def _moved_names(old, home):
    """Names the old module still imports from ``home`` because it calls them."""
    tree = ast.parse(Path(importlib.import_module(old).__file__).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            resolved = ".".join(old.split(".")[:-node.level] + [node.module])
            if resolved == home:
                names += [a.name for a in node.names]
    return names


def test_the_relocated_forwarding_shim_is_gone():
    with pytest.raises(ImportError):
        importlib.import_module("print_handy._relocated")


@pytest.mark.parametrize("home", sorted(HOMES))
def test_a_name_the_old_module_still_imports_is_the_same_object(home):
    """An empty import list is the shim being gone. A leftover import must not be a copy."""
    old = importlib.import_module(HOMES[home])
    new = importlib.import_module(home)
    for name in _moved_names(HOMES[home], home):
        assert getattr(old, name) is getattr(new, name), name


def test_bounds_helpers_are_not_reexported_from_model_job():
    from print_handy.gcode import bounds
    assert callable(bounds.gcode_motion_bounds)
    for name in ("BOUNDS_CACHE", "gcode_motion_bounds", "file_sha256", "verify_gcode", "zmod_md5_valid"):
        assert not hasattr(model_job, name), name


def test_patching_model_job_does_not_reach_the_moved_bounds_module():
    from print_handy.gcode import bounds
    original = bounds.BOUNDS_CACHE
    with mock.patch.object(bounds, "BOUNDS_CACHE", object()):
        assert not hasattr(model_job, "BOUNDS_CACHE")
        assert bounds.BOUNDS_CACHE is not original
    assert bounds.BOUNDS_CACHE is original


def test_patching_prepared_intake_does_not_forward_to_intake_questions():
    from print_handy.core import prepared_intake
    from print_handy.intake import questions
    original = questions.ask
    with mock.patch.object(prepared_intake, "ask", mock.Mock()):
        assert questions.ask is original
    assert questions.ask is original


def test_moved_loggers_are_their_own():
    from print_handy.gcode import bounds
    from print_handy.jobs import store
    assert bounds.logger is not store.logger
    assert bounds.logger.name == "print_handy.gcode.bounds"


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


def test_slice_job_lives_on_jobs_slice_not_model_job():
    from print_handy.jobs import slice as jobs_slice
    assert callable(jobs_slice.slice_job)
    assert not hasattr(model_job, "slice_job")


def test_the_model_job_module_still_runs_as_a_command():
    """``python3 -m print_handy.core.model_job`` stays the documented command. It is not jobs.cli.main."""
    import subprocess
    import sys
    from print_handy.jobs import cli
    assert not hasattr(model_job, "main")
    assert callable(cli.main)
    out = subprocess.run([sys.executable, "-m", "print_handy.core.model_job", "--help"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "print-now" in out.stdout and "fetch" in out.stdout


def test_maker_inventory_is_not_forwarded_from_model_job():
    from print_handy.jobs import sources
    assert callable(sources.maker_inventory)
    assert not hasattr(model_job, "maker_inventory")


def test_prepared_intake_no_longer_lists_model_job_offload_names():
    from print_handy.core import prepared_intake
    assert not hasattr(prepared_intake, "_OFFLOAD")


def test_the_handler_package_does_not_reexport_routed_printer_id():
    from print_handy.bot import handlers
    from print_handy.bot.handlers import common
    assert not hasattr(handlers, "_routed_printer_id")
    assert hasattr(common, "_routed_printer_id")
