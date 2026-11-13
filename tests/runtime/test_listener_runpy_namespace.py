"""The Moonraker listener's seam namespace must be its own globals, however it is run.

The launcher (``print_handy.listeners._run_domain_module``) runs the
listener with ``runpy.run_module``. runpy executes the code in a fresh
namespace and does not put it in ``sys.modules`` under its run name, so a
namespace taken from ``sys.modules[__name__]`` was the launcher's module
in the 2.3.0 container: every seam lookup through ``listener_common``
failed with "module '__main__' has no attribute 'log'" (start message
edits, the camera watch notice, spool nags, the receive loop itself).
"""
from __future__ import annotations

import runpy
import sys

import pytest

from print_handy.runtime import listener_common
from print_handy.runtime import moonraker_event_listener as listener

MODULE = "print_handy.runtime.moonraker_event_listener"
PROBE = "_ph_listener_runpy_probe"


@pytest.fixture
def runpy_globals():
    assert PROBE not in sys.modules
    namespace = runpy.run_module(MODULE, run_name=PROBE)
    assert PROBE not in sys.modules  # exactly the launcher's situation
    return namespace


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_seams_resolve_through_the_runpy_namespace(runpy_globals):
    seams = runpy_globals["_SELF"]
    for name in ("log", "_profile_env_value", "_telegram_standalone", "_matrix_standalone"):
        assert getattr(seams, name) is runpy_globals[name]


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_listener_common_calls_work_from_a_runpy_run(runpy_globals, monkeypatch):
    monkeypatch.setitem(runpy_globals, "_platform_name", lambda: "telegram")
    assert listener_common._buttonless_standalone(ns=runpy_globals["_SELF"]) == "telegram"


def test_imported_module_seams_follow_a_monkeypatch(monkeypatch):
    monkeypatch.setattr(listener, "_platform_name", lambda: "matrix")
    assert listener_common._buttonless_standalone(ns=listener._SELF) == "matrix"


def test_a_missing_seam_is_an_attribute_error():
    with pytest.raises(AttributeError):
        listener._SELF.no_such_seam
