"""The listener health probe must load the way the container actually runs it.

The supervisor's watchdog runs ``python3 -m print_handy.healthcheck``. Run that
way the module is ``__main__`` and is imported a second time through the
printers package, which once died on a circular import: every probe failed and
a healthy listener was killed after each startup grace.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_python(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               XDG_CONFIG_HOME=str(tmp_path / 'config'), PRINT_HANDY_DATA_DIR=str(tmp_path / 'data'))
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=env, text=True,
                          capture_output=True, timeout=120)


def test_healthcheck_loads_when_run_as_a_module(tmp_path):
    done = run_python(tmp_path, '-c',
                      "import runpy; runpy.run_module('print_handy.healthcheck', run_name='probe')")
    assert done.returncode == 0, done.stderr
    assert 'partially initialized' not in done.stderr


def test_listeners_can_be_the_first_import(tmp_path):
    done = run_python(tmp_path, '-c',
                      'import print_handy.listeners as l; print(sorted(l.implemented_connections()))')
    assert done.returncode == 0, done.stderr
    assert 'moonraker' in done.stdout


def test_healthcheck_can_be_the_first_import(tmp_path):
    done = run_python(tmp_path, '-c', 'import print_handy.healthcheck as h; print(h.RUNTIME)')
    assert done.returncode == 0, done.stderr
    assert 'moonraker_event_listener' in done.stdout
