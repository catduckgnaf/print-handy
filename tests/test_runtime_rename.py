"""print_handy.hermes became print_handy.runtime, and paths.hermes_home became paths.state_home (#1323, #1157).

The old import and helper names have been removed in 2.4.0. The separate
paths.hermes_home configuration alias remains covered here until the settings
cleanup PR is merged.
"""
import logging
import subprocess
import sys
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


def test_the_old_state_key_in_a_config_file_loads_as_state_home(tmp_path, caplog):
    local = tmp_path / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp_path / 'old-home'}\n", encoding="utf-8")
    settings._warned_aliases.discard(("paths", "hermes_home"))
    with caplog.at_level(logging.WARNING, logger="print_handy.settings"):
        data = settings.load(local=local, environ={})
    assert data["paths"]["state_home"] == tmp_path / "old-home"
    assert "hermes_home" not in data["paths"]
    assert "[paths] hermes_home is deprecated" in caplog.text and "state_home" in caplog.text


def test_the_old_environment_name_loads_and_the_new_one_wins():
    data = settings.load(local=False, environ={"PRINT_HANDY__PATHS__HERMES_HOME": str(ROOT / "old")})
    assert data["paths"]["state_home"] == ROOT / "old"
    both = {"PRINT_HANDY__PATHS__HERMES_HOME": str(ROOT / "old"), "PRINT_HANDY__PATHS__STATE_HOME": str(ROOT / "new")}
    assert settings.load(local=False, environ=both)["paths"]["state_home"] == ROOT / "new"


def test_an_install_setting_only_the_old_environment_name_gets_the_same_state_path(tmp_path):
    folder = str(tmp_path / "state")
    old = settings.load(local=False, environ={"PRINT_HANDY__PATHS__HERMES_HOME": folder})
    new = settings.load(local=False, environ={"PRINT_HANDY__PATHS__STATE_HOME": folder})
    assert old["paths"]["state_home"] == new["paths"]["state_home"] == Path(folder)
    assert settings.state_dir(old) == settings.state_dir(new)
    assert "PRINT_HANDY__PATHS__HERMES_HOME" in (ROOT / "docs" / "SETTINGS.md").read_text(encoding="utf-8")


def test_removed_state_helpers_are_no_longer_exported():
    assert not hasattr(_config, "hermes_home")
    assert not hasattr(_config, "pinned_hermes_home")
    assert callable(_config.state_home)
    assert callable(_config.pinned_state_home)
