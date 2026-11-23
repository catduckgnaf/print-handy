"""print_handy.hermes became print_handy.runtime, and paths.hermes_home became paths.state_home (#1323, #1157).

The old import and helper names have been removed in 2.4.0. The separate
paths.hermes_home configuration alias remains covered here until the settings
cleanup PR is merged.
"""
import subprocess
import sys

import pytest
from pathlib import Path

from print_handy import settings
from print_handy.runtime import _config

ROOT = Path(__file__).resolve().parents[1]


def test_the_removed_package_name_no_longer_imports():
    run = subprocess.run([sys.executable, "-c", "import print_handy.hermes"],
                         cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode != 0
    assert "No module named 'print_handy.hermes'" in run.stderr


def test_runtime_modules_remain_available_under_the_current_package():
    from print_handy.runtime import panic, print_stop
    assert panic.__name__ == "print_handy.runtime.panic"
    assert print_stop.__name__ == "print_handy.runtime.print_stop"


def test_the_old_state_key_is_refused_with_the_new_name(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp_path / 'old-home'}\n", encoding="utf-8")
    with pytest.raises(settings.SettingsError, match=r"hermes_home .*state_home"):
        settings.load(local=local, environ={})


def test_old_state_environment_name_is_refused_even_with_new_name(tmp_path):
    old = str(tmp_path / "old")
    new = str(tmp_path / "new")
    with pytest.raises(settings.SettingsError, match="PRINT_HANDY__PATHS__STATE_HOME"):
        settings.load(local=False, environ={"PRINT_HANDY__PATHS__HERMES_HOME": old})
    with pytest.raises(settings.SettingsError, match="PRINT_HANDY__PATHS__STATE_HOME"):
        settings.load(local=False, environ={"PRINT_HANDY__PATHS__HERMES_HOME": old,
                                            "PRINT_HANDY__PATHS__STATE_HOME": new})
    assert settings.load(local=False, environ={"PRINT_HANDY__PATHS__STATE_HOME": new})["paths"]["state_home"] == Path(new)


def test_install_must_use_the_new_state_path(tmp_path):
    folder = str(tmp_path / "state")
    with pytest.raises(settings.SettingsError, match="hermes_home"):
        settings.load(local=False, environ={"PRINT_HANDY__PATHS__HERMES_HOME": folder})
    data = settings.load(local=False, environ={"PRINT_HANDY__PATHS__STATE_HOME": folder})
    assert data["paths"]["state_home"] == Path(folder)
    assert settings.state_dir(data)
    assert "PRINT_HANDY__PATHS__HERMES_HOME" in (ROOT / "docs" / "SETTINGS.md").read_text(encoding="utf-8")


def test_removed_state_helpers_are_no_longer_exported():
    assert not hasattr(_config, "hermes_home")
    assert not hasattr(_config, "pinned_hermes_home")
    assert callable(_config.state_home)
    assert callable(_config.pinned_state_home)
