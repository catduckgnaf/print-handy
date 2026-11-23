"""print_handy.hermes became print_handy.runtime, and paths.hermes_home became paths.state_home (#1323, #1157).

Both old names keep working through 2.3.x and say so: the old import path
warns and resolves to the very same modules, and the old config key, in a
file or the environment, loads under the new name with a logged warning.
"""
import subprocess
import sys

import pytest
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


def test_the_old_helper_names_are_the_new_ones():
    assert _config.hermes_home is _config.state_home
    assert _config.pinned_hermes_home is _config.pinned_state_home
