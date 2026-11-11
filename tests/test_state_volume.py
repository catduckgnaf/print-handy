"""Issue #914: the state volume's neutral name (/data/state), the legacy
/data/hermes fallback, and the entrypoint's backward-compatible migration.

The migration is filesystem-shaped and runs in docker/entrypoint.sh before
any service starts, so these tests run the real entrypoint with HOME pointed
at a scratch data root (the same harness as the TZ and Orca-channel tests in
tests/test_docker_release_contract.py): <HOME>/state is /data/state and
<HOME>/hermes is /data/hermes.
"""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from print_handy import doctor, settings, state_volume

ROOT = settings.ROOT
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"

# #971: this suite exercises the shipped image's entrypoint and its /data
# state volumes, so it is container-marked: it runs inside the image and in
# the CI offline-tests job (both set PRINT_HANDY_IN_CONTAINER=1) and skips
# on a plain local checkout instead of failing on the environment.
pytestmark = pytest.mark.container


def run_entrypoint(data, command):
    env = {"PATH": os.environ["PATH"], "HOME": str(data), "TZ": "Europe/London"}
    return subprocess.run(["bash", str(ENTRYPOINT), *command], cwd=data, env=env,
                          text=True, capture_output=True, check=True)


def run_entrypoint_with_path(data, command, path_prefix):
    env = {"PATH": str(path_prefix) + os.pathsep + os.environ["PATH"],
           "HOME": str(data), "TZ": "Europe/London"}
    return subprocess.run(["bash", str(ENTRYPOINT), *command], cwd=data, env=env,
                          text=True, capture_output=True, check=True)


def stat_shim_reporting_a_mountpoint(folder):
    """A stat(1) shim that reports the scratch state folder as living on a
    foreign device, so the entrypoint's mountpoint detector sees a mounted
    /data/state; every other path delegates to the real stat. The harness
    cannot bind-mount portably, so the detector's input is stubbed while the
    real entrypoint runs unmodified end to end (the test below with a real
    bind mount covers the unstubbed detector where namespaces allow it)."""
    real_stat = shutil.which("stat")
    folder.mkdir(parents=True, exist_ok=True)
    shim = folder / "stat"
    shim.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        f'case "$last" in */state) echo 424242 ;; *) exec {real_stat} "$@" ;; esac\n')
    shim.chmod(0o755)
    return folder


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

    def test_state_only_empty_mountpoint_links_the_legacy_name_to_it(self, tmp_path):
        # #938 item 4: the host maps only /data/state; Docker created it as
        # an empty mountpoint directory and there is no /data/hermes at all.
        # The legacy name still has to resolve, as a symlink to the new path.
        (tmp_path / "state").mkdir()
        result = run_entrypoint(tmp_path, ["true"])
        legacy = tmp_path / "hermes"
        assert legacy.is_symlink()
        assert legacy.resolve() == (tmp_path / "state").resolve()
        assert (tmp_path / "state").is_dir() and not (tmp_path / "state").is_symlink()
        assert (legacy / "state").is_dir()
        assert "/data/hermes" in result.stderr

    def test_neither_volume_present_creates_no_legacy_symlink(self, tmp_path):
        run_entrypoint(tmp_path, ["true"])
        assert not (tmp_path / "hermes").is_symlink()
        assert not (tmp_path / "hermes").exists()

    def test_fresh_install_creates_the_new_volume_layout(self, tmp_path):
        run_entrypoint(tmp_path, ["true"])
        assert (tmp_path / "state" / "state").is_dir()
        assert (tmp_path / "state" / "logs").is_dir()
        assert not (tmp_path / "state").is_symlink()

    def test_legacy_data_and_a_plain_empty_state_dir_still_links(self, tmp_path):
        # The state path exists (Docker or a template created it) but is a
        # plain folder on the same filesystem: it is replaced by the
        # symlink, exactly as before the mountpoint fix.
        seed(tmp_path / "hermes" / "state", "printer_hardware.json")
        (tmp_path / "state").mkdir()
        run_entrypoint(tmp_path, ["true"])
        new = tmp_path / "state"
        assert new.is_symlink()
        assert new.resolve() == (tmp_path / "hermes").resolve()

    def test_legacy_data_and_a_mounted_state_volume_is_copied_not_linked(self, tmp_path):
        # The shipped two-bind-mount layout mounts /data/state itself, so
        # the migration branch cannot rm it (EBUSY) or replace it with a
        # symlink: the legacy contents are copied into the mounted volume
        # and the legacy volume is left untouched.
        seed(tmp_path / "hermes" / "state", "printer_hardware.json")
        (tmp_path / "state").mkdir()
        shim_dir = stat_shim_reporting_a_mountpoint(tmp_path / "shim-bin")
        result = run_entrypoint_with_path(tmp_path, ["true"], shim_dir)
        new = tmp_path / "state"
        assert new.is_dir() and not new.is_symlink()
        assert (new / "state" / "printer_hardware.json").read_text() == "{}"
        legacy = tmp_path / "hermes"
        assert legacy.is_dir() and not legacy.is_symlink()
        assert (legacy / "state" / "printer_hardware.json").read_text() == "{}"
        assert "mounted" in result.stderr and "copied" in result.stderr
        # The next start finds the one-time record (#1144): /data/state keeps
        # winning, the stale legacy volume is ignored and never copied again,
        # and nothing is linked.
        result = run_entrypoint_with_path(tmp_path, ["true"], shim_dir)
        assert (tmp_path / "state").is_dir() and not (tmp_path / "state").is_symlink()
        assert "already copied" in result.stderr

    def test_an_interrupted_mountpoint_copy_is_completed_on_the_next_start(self, tmp_path):
        # A copy that dies partway (full disk, killed container) must not
        # read as a finished migration on the next start: the partial
        # state would otherwise win over the intact legacy volume via the
        # both-hold-data branch. The entrypoint marks the copy in
        # progress inside the state volume and re-copies while the mark
        # is there.
        seed(tmp_path / "hermes" / "state", "printer_hardware.json")
        (tmp_path / "state").mkdir()
        mount_shim = stat_shim_reporting_a_mountpoint(tmp_path / "mount-shim")
        dying_cp = tmp_path / "dying-cp"
        dying_cp.mkdir()
        script = dying_cp / "cp"
        script.write_text(
            "#!/bin/sh\n"
            "for last; do :; done\n"
            "printf '{\"truncated' > \"$last\"\n"
            "exit 1\n")
        script.chmod(0o755)
        both = tmp_path / "both-shims"
        both.mkdir()
        for shim in (mount_shim / "stat", script):
            (both / shim.name).symlink_to(shim)
        with pytest.raises(subprocess.CalledProcessError):
            run_entrypoint_with_path(tmp_path, ["true"], both)
        state = tmp_path / "state"
        # The half-written file sits under a temporary name (#1144): the real
        # name never holds partial data, and the mark is still there.
        assert not (state / "state" / "printer_hardware.json").exists()
        assert (state / "state" / "printer_hardware.json.ph-migrating").read_text() == '{"truncated'
        assert (state / ".migration-incomplete").exists()
        result = run_entrypoint_with_path(tmp_path, ["true"], mount_shim)
        assert state.is_dir() and not state.is_symlink()
        assert (state / "state" / "printer_hardware.json").read_text() == "{}"
        assert not (state / ".migration-incomplete").exists()
        assert not (state / "state" / "printer_hardware.json.ph-migrating").exists()
        assert "did not finish" in result.stderr

    def test_legacy_data_and_a_real_bind_mounted_state_volume_is_copied(self, tmp_path):
        # The unstubbed detector against a real bind mount (a folder bound
        # onto itself, like the Compose layout's /data/state bind mount),
        # inside a user+mount namespace; skipped where namespaces are not
        # available. This is the condition the 2.2.5 lifecycle gate caught:
        # pre-fix the entrypoint dies on rm -rf of the mountpoint.
        unshare = shutil.which("unshare")
        if unshare is None:
            pytest.skip("unshare(1) is not available")
        if subprocess.run([unshare, "-rm", "true"], capture_output=True).returncode != 0:
            pytest.skip("user and mount namespaces are not available")
        seed(tmp_path / "hermes" / "state", "printer_hardware.json")
        state = tmp_path / "state"
        state.mkdir()
        inner = (
            f"mount --bind {shlex.quote(str(state))} {shlex.quote(str(state))} && "
            f"HOME={shlex.quote(str(tmp_path))} TZ=Europe/London "
            f"bash {shlex.quote(str(ENTRYPOINT))} true")
        result = subprocess.run([unshare, "-rm", "bash", "-c", inner],
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert state.is_dir() and not state.is_symlink()
        assert (state / "state" / "printer_hardware.json").read_text() == "{}"
        legacy = tmp_path / "hermes"
        assert legacy.is_dir() and not legacy.is_symlink()
        assert (legacy / "state" / "printer_hardware.json").read_text() == "{}"
        assert "mounted" in result.stderr and "copied" in result.stderr

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
