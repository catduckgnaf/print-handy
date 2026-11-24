"""The shipped Docker settings contract is YAML-only as of 2.4.0 (#1849)."""
from pathlib import Path

import pytest
from print_handy import settings

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_docker_config_loads_from_yaml_only():
    shipped = ROOT / "config" / "docker.yaml"
    assert shipped.is_file()
    assert not (ROOT / "config" / "docker.toml").exists()
    assert settings.SHIPPED_DOCKER_CONFIGS == {shipped.resolve()}
    data = settings.load(local=shipped, environ={}, user=False)
    assert data["printer"]["model"]


def test_an_explicit_docker_toml_path_is_refused(tmp_path):
    stale = tmp_path / "docker.toml"
    stale.write_text("[printer]\nmodel = 'flashforge.ad5x'\n")
    with pytest.raises(settings.SettingsError, match="TOML settings are no longer supported"):
        settings.load(local=stale, environ={}, user=False)
