"""Issue #914: the state volume's neutral name (/data/state), the legacy
/data/hermes fallback, and the entrypoint's backward-compatible migration.

The migration is filesystem-shaped and runs in docker/entrypoint.sh before
any service starts, so these tests run the real entrypoint with HOME pointed
at a scratch data root (the same harness as the TZ and Orca-channel tests in
tests/test_docker_release_contract.py): <HOME>/state is /data/state and
<HOME>/hermes is /data/hermes.
"""
import os
import subprocess
from pathlib import Path

from print_handy import doctor, settings, state_volume

ROOT = settings.ROOT
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def run_entrypoint(data, command):
    env = {"PATH": os.environ["PATH"], "HOME": str(data), "TZ": "Europe/London"}
    return subprocess.run(["bash", str(ENTRYPOINT), *command], cwd=data, env=env,
                          text=True, capture_output=True, check=True)


def seed(folder, name="marker.json"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("{}")


class TestDescribeStateVolume:
    def test_new_path_only_is_reported_plainly(self, tmp_path):
        home = tmp_path / "state"
        seed(home / "state")
        assert state_volume.describe_state_volume(home) == str(home)

    def test_symlink_to_legacy_reports_the_legacy_volume_in_use(self, tmp_path):
        legacy = tmp_path / "hermes"
        seed(legacy / "state")
        home = tmp_path / "state"
        home.symlink_to(legacy, target_is_directory=True)
        text = state_volume.describe_state_volume(home)
        assert str(legacy) in text
        assert "legacy" in text and "deprecated" in text

    def test_explicit_legacy_path_reports_the_deprecated_name(self, tmp_path):
        home = tmp_path / "hermes"
        seed(home / "state")
        text = state_volume.describe_state_volume(home)
        assert "legacy /data/hermes" in text
        assert "/data/state" in text

    def test_both_present_reports_the_new_path_wins(self, tmp_path):
        seed(tmp_path / "state" / "state")
        seed(tmp_path / "hermes" / "state")
        text = state_volume.describe_state_volume(tmp_path / "state")
        assert "both" in text and "wins" in text

    def test_unrelated_home_is_reported_plainly(self, tmp_path):
        home = tmp_path / "profiles" / "3dprinting"
        seed(home / "state")
        assert state_volume.describe_state_volume(home) == str(home)


class TestDoctorRow:
    def test_doctor_reports_the_configured_state_volume(self, tmp_path):
        home = tmp_path / "hermes"
        seed(home / "state")
        text = doctor.check_state_volume({"paths": {"hermes_home": str(home)}})
        assert text.startswith(str(home))
        assert "legacy" in text


class TestEntrypointMigration:
    def test_old_only_links_the_new_path_to_the_legacy_volume(self, tmp_path):
        seed(tmp_path / "hermes" / "state", "printer_hardware.json")
        result = run_entrypoint(tmp_path, ["sh", "-c", "cat \"$HOME/state/state/printer_hardware.json\""])
        new = tmp_path / "state"
        assert new.is_symlink()
        assert new.resolve() == (tmp_path / "hermes").resolve()
        assert result.stdout.strip() == "{}"
        assert "deprecated" in result.stderr
        assert "/data/hermes" in result.stderr

    def test_new_only_keeps_the_real_folder_and_stays_quiet(self, tmp_path):
        seed(tmp_path / "state" / "state")
        run_entrypoint(tmp_path, ["true"])
        new = tmp_path / "state"
        assert new.is_dir() and not new.is_symlink()
        result = run_entrypoint(tmp_path, ["true"])
        assert "deprecated" not in result.stderr
        assert "WARNING" not in result.stderr

    def test_both_present_new_path_wins_with_a_warning(self, tmp_path):
        seed(tmp_path / "state" / "state", "new.json")
        seed(tmp_path / "hermes" / "state", "old.json")
        result = run_entrypoint(tmp_path, ["sh", "-c", "ls \"$HOME/state/state\""])
        new = tmp_path / "state"
        assert new.is_dir() and not new.is_symlink()
        assert result.stdout.split() == ["new.json"]
        assert "WARNING" in result.stderr and "both" in result.stderr

    def test_old_folder_with_no_files_does_not_trigger_the_migration(self, tmp_path):
        # A pre-2.2.5 template recreated against an empty legacy folder:
        # directories alone are the seeded skeleton, not data.
        (tmp_path / "hermes" / "state").mkdir(parents=True)
        run_entrypoint(tmp_path, ["true"])
        new = tmp_path / "state"
        assert new.is_dir() and not new.is_symlink()
        assert (new / "state").is_dir() and (new / "logs").is_dir()

    def test_fresh_install_creates_the_new_volume_layout(self, tmp_path):
        run_entrypoint(tmp_path, ["true"])
        assert (tmp_path / "state" / "state").is_dir()
        assert (tmp_path / "state" / "logs").is_dir()
        assert not (tmp_path / "state").is_symlink()

    def test_second_start_keeps_the_migration_and_repeats_the_notice(self, tmp_path):
        seed(tmp_path / "hermes" / "state")
        run_entrypoint(tmp_path, ["true"])
        result = run_entrypoint(tmp_path, ["true"])
        assert (tmp_path / "state").is_symlink()
        assert "deprecated" in result.stderr


class TestReportingWiring:
    def test_healthcheck_reports_the_state_volume(self):
        script = (ROOT / "docker" / "healthcheck.py").read_text()
        assert "state volume" in script
        assert "describe_state_volume" in script

    def test_docker_defaults_point_at_the_new_volume(self):
        for suffix in ("yaml", "toml"):
            cfg = settings.load(local=ROOT / "config" / f"docker.{suffix}", user=False, environ={})
            assert cfg["paths"]["hermes_home"] == Path("/data/state")
            assert cfg["logging"]["file"] == "/data/state/logs/print-handy.log"
