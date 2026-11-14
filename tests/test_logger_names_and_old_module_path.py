"""Logger names that survive the runpy launcher, and the old ``-m`` path.

The listeners run as ``__main__`` (listeners.py uses runpy), so a logger
named after ``__name__`` was ``__main__``: ``[logging] levels`` overrides and
debug mode (which raises the ``print_handy`` tree) never reached it. The chat
handlers logged as ``print_handy_print_feedback``, outside that tree too.
Service files written before 2.3.0 start ``python -m print_handy.hermes.X``,
which crashed in runpy until the alias finder could hand over the real code.
"""
import ast
import logging
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = sorted((ROOT / "print_handy" / "bot" / "handlers").glob("*.py"))


def _module_logger_names(path):
    """The names a module passes to ``logging.getLogger`` for its module-level ``logger``.

    A ``__name__`` argument resolves to the module's own dotted path
    (#1404: the chat handlers log per module, ``print_handy.bot.handlers.<x>``,
    which sits under the tree exactly like a literal name). The listeners
    keep literal names on purpose: run as ``__main__`` their ``__name__``
    would resolve to ``__main__``, outside the tree.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "logger" for t in node.targets)
                and isinstance(node.value, ast.Call) and getattr(node.value.func, "attr", None) == "getLogger"):
            arg = node.value.args[0] if node.value.args else None
            if isinstance(arg, ast.Name) and arg.id == "__name__":
                names.append(".".join(path.relative_to(ROOT).with_suffix("").parts))
            else:
                names.append(arg.value if isinstance(arg, ast.Constant) else ast.unparse(arg) if arg else "")
    return names


@pytest.mark.parametrize("module", ["moonraker_event_listener", "family_event_listener"])
def test_a_listener_run_as_main_keeps_its_package_logger_name(module):
    path = ROOT / "print_handy" / "runtime" / f"{module}.py"
    assert _module_logger_names(path) == [f"print_handy.runtime.{module}"]


def test_chat_handler_loggers_sit_under_the_print_handy_tree():
    named = {path.name: _module_logger_names(path) for path in HANDLERS}
    outside = {name: loggers for name, loggers in named.items()
               if any(not logger.startswith("print_handy.") for logger in loggers)}
    assert not outside, outside
    assert any(named.values())


def test_debug_mode_reaches_the_handler_and_listener_loggers():
    from print_handy.bot.handlers import common
    root = logging.getLogger("print_handy")
    before = root.level
    try:
        root.setLevel(logging.DEBUG)
        for name in (common.logger.name, "print_handy.runtime.moonraker_event_listener",
                     "print_handy.runtime.family_event_listener"):
            assert logging.getLogger(name).getEffectiveLevel() == logging.DEBUG, name
    finally:
        root.setLevel(before)


def test_python_dash_m_through_the_old_package_name_runs_the_real_module():
    run = subprocess.run([sys.executable, "-W", "always", "-m", "print_handy.hermes.panic", "--help"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "No-LLM printer panic controls" in run.stdout
    assert "print_handy.runtime.panic" in run.stderr
