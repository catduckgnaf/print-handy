"""The state volume is /data/state (#914), and its pre-2.2.5 name is gone
(#1817): no module reports a legacy volume, the doctor names the configured
folder, and the container healthcheck has no state-volume row. The
entrypoint's refusal of an install still using the old folder is covered in
tests/test_entrypoint_layout_matrix.py.
"""
from pathlib import Path

from print_handy import doctor, settings

ROOT = settings.ROOT


def test_the_legacy_state_volume_module_is_gone():
    assert not (ROOT / "print_handy" / "state_volume.py").exists()


def test_doctor_reports_the_configured_state_folder_plainly(tmp_path):
    home = tmp_path / "state"
    assert doctor.check_state_volume({"paths": {"state_home": str(home)}}) == str(home)


def test_healthcheck_no_longer_reports_a_state_volume():
    script = (ROOT / "docker" / "healthcheck.py").read_text()
    assert "state_volume" not in script
    assert "state volume" not in script


def test_docker_defaults_point_at_the_state_volume():
    for suffix in ("yaml", "toml"):
        text = (ROOT / "config" / f"docker.{suffix}").read_text()
        assert "/data/hermes" not in text
        cfg = settings.load(local=ROOT / "config" / f"docker.{suffix}", user=False, environ={})
        assert cfg["paths"]["state_home"] == Path("/data/state")
        assert cfg["logging"]["file"] == "/data/state/logs/print-handy.log"
