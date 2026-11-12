"""P1-81: the launcher's instance identity must reach canonical importers.

The supervisor runs ``python3 -m print_handy.listeners --printer <id>``
(docker/supervise.sh). A module run with ``-m`` executes as ``__main__``,
a *different* module object from the ``print_handy.listeners`` that the
healthcheck and the domain listeners import — so an identity stored in
the launcher's own globals never reaches them, and every listener
resolved its printer as the roster's first. The identity therefore lives
in a canonical shared module (print_handy.instance); this test executes
the launcher the way production does, through runpy as ``__main__``,
substituting only the domain runner.
"""
import runpy
import sys
from unittest import mock

import pytest

from print_handy import healthcheck, listeners, logs, settings

ROSTER_YAML = (
    "discord:\n  chat_id: \"111\"\n"
    "printers:\n"
    "  - id: moon-two\n    model: klipper.generic\n    chat_id: \"chat-two\"\n"
    "  - id: bambu-two\n    model: bambulab.a1\n")


def _roster(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(ROSTER_YAML)
    return settings.load(home=settings.HOME, local=local, environ={})


def test_main_module_launch_installs_identity_for_canonical_importers(tmp_path):
    roster = _roster(tmp_path)
    real_run_module = runpy.run_module
    ran = []

    def fake_run_module(name, run_name=None, **kwargs):
        if name == "print_handy.listeners":
            return real_run_module(name, run_name=run_name, **kwargs)
        ran.append((name, run_name))
        return {}

    canonical = sys.modules["print_handy.listeners"]
    seen = {}
    try:
        with mock.patch.object(settings, "current", return_value=roster), \
                mock.patch.object(sys, "argv",
                                  ["print_handy.listeners", "--printer", "moon-two"]), \
                mock.patch.object(runpy, "run_module", side_effect=fake_run_module):
            with pytest.raises(SystemExit) as excinfo:
                runpy.run_module("print_handy.listeners", run_name="__main__")
        seen["exit"] = excinfo.value.code
        seen["identity"] = listeners.instance_printer_id()
        seen["healthcheck"] = healthcheck.resolve_printer_id(roster)
    finally:
        sys.modules["print_handy.listeners"] = canonical
        settings.set_override(None)
        listeners._set_instance_printer_id(None)
        logs.clear_context()
    assert seen["exit"] == 0
    assert ran == [("print_handy.runtime.moonraker_event_listener", "__main__")]
    # The point of the finding: importers of the canonical module (the
    # healthcheck, the domain listeners) see the launched identity.
    assert seen["identity"] == "moon-two"
    assert seen["healthcheck"] == "moon-two"
