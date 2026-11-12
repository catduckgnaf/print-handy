"""print_handy.hermes became print_handy.runtime, and paths.hermes_home became paths.state_home (#1323, #1157).

Both old names keep working through 2.3.x and say so: the old import path
warns and resolves to the very same modules, and the old config key, in a
file or the environment, loads under the new name with a logged warning.
"""
import logging
import subprocess
import sys
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.runtime import _config

ROOT = Path(__file__).resolve().parents[1]


def test_the_old_package_name_warns_and_is_the_same_modules():
    code = ("import warnings, sys\n"
            "warnings.simplefilter('always')\n"
            "with warnings.catch_warnings(record=True) as seen:\n"
            "    import print_handy.hermes.print_stop as old\n"
            "import print_handy.runtime.print_stop as new\n"
            "from print_handy.hermes import print_control\n"
            "import print_handy.runtime.print_control as control\n"
            "assert old is new and print_control is control\n"
            "assert new.__spec__.name == 'print_handy.runtime.print_stop'\n"
            "assert any(issubclass(w.category, DeprecationWarning) and 'print_handy.runtime' in str(w.message)"
            " for w in seen), [str(w.message) for w in seen]\n"
            "print('ok')\n")
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and run.stdout.strip().endswith("ok"), run.stdout + run.stderr


def test_patching_through_the_old_package_name_patches_the_real_module():
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import print_handy.hermes.panic  # noqa: F401
    from print_handy.runtime import panic
    with mock.patch("print_handy.hermes.panic.main") as fake:
        assert panic.main is fake


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


def test_the_old_helper_names_are_the_new_ones():
    assert _config.hermes_home is _config.state_home
    assert _config.pinned_hermes_home is _config.pinned_state_home
