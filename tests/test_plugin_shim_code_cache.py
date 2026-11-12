"""Loading the old plugin path again must not recompile the handler modules (#1349).

plugins/print-feedback/__init__.py executes every handler module's code in
its own namespace. Parsing, rewriting and compiling that code cost about
0.12 s per load, and the timing corpus loads the plugin once per case, so
every timed intake carried it. The compiled bodies are reused now; each
load still gets its own namespace.
"""
from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "print-feedback" / "__init__.py"
HANDLERS = str(Path("print_handy") / "bot" / "handlers")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_second_load_compiles_no_handler_module(monkeypatch):
    _load("shim_cache_first")
    compiled = []
    real_compile = builtins.compile

    def counting_compile(source, filename, *args, **kwargs):
        if HANDLERS in str(filename):
            compiled.append(filename)
        return real_compile(source, filename, *args, **kwargs)

    monkeypatch.setattr(builtins, "compile", counting_compile)
    second = _load("shim_cache_second")
    assert compiled == []
    assert callable(second.guided_dispatch)


def test_each_load_keeps_its_own_namespace():
    first = _load("shim_cache_ns_a")
    second = _load("shim_cache_ns_b")
    assert first.guided_dispatch is not second.guided_dispatch
    first.logger = "patched"
    assert second.logger != "patched"
