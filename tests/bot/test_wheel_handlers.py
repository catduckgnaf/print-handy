"""A wheel built from the tree carries the chat handlers and the Router loads them (#1323, proposal 5 of #1160).

The handlers used to live outside the package (plugins/print-feedback), so a
wheel-only install had no chat commands (#1146). They are now the
print_handy.bot.handlers package: this builds the wheel from a copy of the
tree, unpacks it alone (beside the config, with no plugins folder) and
loads the Router from it in a fresh interpreter.
"""
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_FILES = ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "NOTICE",
               "plugins/print-feedback/__init__.py")
_BUILD = ("import sys\nfrom setuptools import build_meta as backend\n"
          "print(backend.build_wheel(sys.argv[1]))\n")
_LOAD = """
import json, sys
import print_handy
from print_handy.bot import handlers
from print_handy.bot.router import Router
router = Router()
print(json.dumps({"package": print_handy.__file__, "handlers": handlers.__file__,
                  "names": router.handler_names(),
                  "gated": sorted(h.__name__ for h in router.dispatch_hooks if getattr(h, "dispatch_gate", None))}))
"""


def test_a_wheel_built_from_the_tree_loads_the_handlers(tmp_path):
    tree, out, site = tmp_path / "tree", tmp_path / "dist", tmp_path / "site"
    for folder in (tree, out, site):
        folder.mkdir()
    for name in BUILD_FILES:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, tree / name)
    shutil.copytree(ROOT / "print_handy", tree / "print_handy",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    built = subprocess.run([sys.executable, "-c", _BUILD, str(out)], cwd=tree,
                           capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stdout[-3000:] + built.stderr[-3000:]
    wheel = out / built.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "print_handy/bot/handlers/__init__.py" in names
        assert "print_handy/bot/handlers/print_stop.py" in names
        assert not any(n.startswith("plugins/") for n in names)
        archive.extractall(site)
    shutil.copytree(ROOT / "config", site / "config")  # the settings the runtime reads beside the package
    assert not (site / "plugins").exists()
    run = subprocess.run([sys.executable, "-c", _LOAD], cwd=tmp_path, capture_output=True, text=True,
                         timeout=300, env={**_clean_env(), "PYTHONPATH": str(site)})
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-3000:]
    loaded = json.loads(run.stdout.strip().splitlines()[-1])
    assert Path(loaded["package"]).resolve().is_relative_to(site.resolve())
    assert Path(loaded["handlers"]).resolve().is_relative_to(site.resolve())
    from print_handy.bot import handlers
    assert loaded["names"] == [h.__name__ for h in handlers._DISPATCH_HOOKS]
    assert len(loaded["names"]) == len(handlers.HANDLERS) > 20
    assert "print_stop_dispatch" in loaded["gated"] and "model_link_dispatch" not in loaded["gated"]


def test_every_handler_declares_match_and_handle():
    from print_handy.bot import handlers
    for module in handlers.HANDLERS:
        assert callable(module.match) and callable(module.handle), module.__name__
        gate = getattr(module.handle, "dispatch_gate", None)
        assert gate is None or gate is module.match, module.__name__
    assert [m.handle for m in handlers.HANDLERS] == list(handlers._DISPATCH_HOOKS)


def test_the_old_plugin_file_registers_the_same_handlers_in_the_same_order():
    import importlib.util
    spec = importlib.util.spec_from_file_location("pf_wheel_parity", ROOT / "plugins/print-feedback/__init__.py")
    old = importlib.util.module_from_spec(spec)
    sys.modules["pf_wheel_parity"] = old
    try:
        spec.loader.exec_module(old)
    finally:
        sys.modules.pop("pf_wheel_parity", None)
    from print_handy.bot import handlers
    assert [h.__name__ for h in old._DISPATCH_HOOKS] == [h.__name__ for h in handlers._DISPATCH_HOOKS]
    old_gates = {h.__name__: getattr(h, "dispatch_gate", None) for h in old._DISPATCH_HOOKS}
    new_gates = {h.__name__: getattr(h, "dispatch_gate", None) for h in handlers._DISPATCH_HOOKS}
    assert {n: g.command for n, g in old_gates.items() if g} == {n: g.command for n, g in new_gates.items() if g}


def _clean_env():
    import os
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
