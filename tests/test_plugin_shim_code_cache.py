"""Chat handlers load from the package, once, not from a plugin file (#1349, #1323).

The old print-feedback path compiled every handler into a fresh namespace on
each load. The handlers are ``print_handy.bot.handlers`` now: importing them
again is the same module.
"""
from __future__ import annotations

import importlib
from pathlib import Path


def test_handlers_import_from_the_package_once():
    import print_handy.bot.handlers as handlers
    from print_handy.bot.handlers.guided import guided_dispatch
    assert not (Path(__file__).resolve().parents[1] / "plugins" / "print-feedback").exists()
    again = importlib.import_module("print_handy.bot.handlers")
    guided_again = importlib.import_module("print_handy.bot.handlers.guided").guided_dispatch
    assert again is handlers
    assert guided_again is guided_dispatch
    assert any(hook is guided_dispatch for hook in handlers._DISPATCH_HOOKS)
