"""Entrypoint x layout matrix (testing-hardening pass, E7; #1817).

The main state volume is /data/state. Its pre-2.2.5 name, /data/hermes, is
no longer read or migrated: ``docker/entrypoint.sh`` refuses to start an
install whose state still sits only in the old folder, so it never comes up
empty, and otherwise leaves the old folder alone. The entrypoint keys its
data root off ``$HOME``, so every cell runs the REAL shipped script offline
against a synthetic home: the script runs to its final ``exec "$@"`` with
``true`` as the command.

One cell needs /data/state to be a genuine (empty) mountpoint; it is marked
``container`` and gated on ``PRINT_HANDY_LAYOUT_TEST_MOUNT=1``, which the
"State volume (real bind mount)" CI job sets.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def run_entrypoint(home: Path, **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TZ"] = "America/New_York"
    env.pop("PRINT_HANDY_ORCA_CHANNEL", None)
    env.pop("PRINT_HANDY_ORCA_CMD", None)
    # The layout cells decide state from the synthetic home, not from an
    # operator's ambient paths.state_home.
    env.pop("PRINT_HANDY__PATHS__STATE_HOME", None)
    env.pop("PRINT_HANDY__PATHS__HERMES_HOME", None)
    env.pop("PRINT_HANDY_CONFIG", None)
    env.pop("PRINT_HANDY_USER_CONFIG", None)
    env.update(extra)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "true"],
        env=env, capture_output=True, text=True, timeout=60)


def seed(folder: Path, name="record.txt"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("state\n", encoding="utf-8")


def snapshot(home: Path) -> list:
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def test_compose_init_leaves_legacy_refusal_to_config_aware_entrypoint():
    # The init image cannot resolve the application's configured paths.state_home;
    # refusing on an empty /data/state alone would reject a populated custom folder.
    compose = (ROOT / "docker-compose.yml").read_text()
    init = compose.split("  print-handy-init:", 1)[1].split("  cloudflared:", 1)[0]
    assert "/host-data/hermes" not in init
    assert "for path in /data/state" in init
    assert "_has_files \"$legacy_home\"" in ENTRYPOINT.read_text()


# --- #1817: the old hermes folder is refused, never read or migrated.
OLD_FOLDER = "Print Handy: cannot start: your state is still in the old folder /data/hermes"


def assert_refused(result):
    assert result.returncode == 78, result.stderr
    assert result.stderr.count(OLD_FOLDER) == 1, result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr
    assert "rename the host folder hermes to state" in result.stderr


@pytest.mark.parametrize("channel", ["stable", "preview", "alpha"])
def test_existing_orca_channel_aliases_start_with_bundled_stable(tmp_path, channel):
    result = run_entrypoint(tmp_path, PRINT_HANDY_ORCA_CHANNEL=channel)
    assert result.returncode == 0, result.stderr


def test_unknown_orca_channel_is_still_rejected(tmp_path):
    result = run_entrypoint(tmp_path, PRINT_HANDY_ORCA_CHANNEL="nonexistent")
    assert result.returncode == 2
    assert "PRINT_HANDY_ORCA_CHANNEL must be 'stable'" in result.stderr


def test_state_only_layout_starts_and_creates_no_legacy_link(tmp_path):
    seed(tmp_path / "state")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "record.txt").read_text() == "state\n"
    assert not (tmp_path / "hermes").exists() and not (tmp_path / "hermes").is_symlink()
    assert "hermes" not in result.stderr


def test_fresh_layout_creates_the_state_tree_and_no_links(tmp_path):
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "state").is_dir()
    assert (tmp_path / "state" / "logs").is_dir()
    assert not (tmp_path / "state").is_symlink()
    assert not (tmp_path / "hermes").exists() and not (tmp_path / "hermes").is_symlink()


@pytest.mark.parametrize("state_shape", ["absent", "empty", "empty-tree"])
def test_legacy_only_layout_refuses_with_one_line_and_changes_nothing(tmp_path, state_shape):
    seed(tmp_path / "hermes" / "state", "printer_hardware.json")
    if state_shape == "empty":
        (tmp_path / "state").mkdir()
    elif state_shape == "empty-tree":
        # The skeleton Docker or an earlier start made: directories only.
        (tmp_path / "state" / "state").mkdir(parents=True)
        (tmp_path / "state" / "logs").mkdir()
    before = snapshot(tmp_path)
    assert_refused(run_entrypoint(tmp_path))
    assert snapshot(tmp_path) == before
    assert (tmp_path / "hermes" / "state" / "printer_hardware.json").read_text() == "state\n"


def test_configured_state_home_with_files_is_not_an_empty_install(tmp_path):
    """A configured paths.state_home that already has files counts as state.

    The refusal stays when that directory is the same path as /data/state,
    is empty, or is unset. A different directory that already holds files
    must not exit 78, and the old folder is left untouched. With the
    variable unset, a populated old folder and an empty /data/state still
    exit 78.
    """
    configured = tmp_path / "configured-state"
    seed(configured, "kept.txt")
    seed(tmp_path / "hermes", "old.txt")
    hermes_before = snapshot(tmp_path / "hermes")
    configured_before = snapshot(configured)
    started = run_entrypoint(
        tmp_path, PRINT_HANDY__PATHS__STATE_HOME=str(configured))
    assert started.returncode != 78, started.stderr
    assert started.returncode == 0, started.stderr
    assert "is empty" not in started.stderr
    assert OLD_FOLDER not in started.stderr
    assert snapshot(tmp_path / "hermes") == hermes_before
    assert snapshot(configured) == configured_before
    assert (tmp_path / "hermes" / "old.txt").read_text() == "state\n"
    assert (configured / "kept.txt").read_text() == "state\n"
    assert not (tmp_path / "state" / "old.txt").exists()
    assert not (tmp_path / "hermes").is_symlink()

    unset = tmp_path / "unset-home"
    seed(unset / "hermes", "old.txt")
    before = snapshot(unset)
    assert_refused(run_entrypoint(unset))
    assert snapshot(unset) == before
    assert (unset / "hermes" / "old.txt").read_text() == "state\n"


def test_an_old_state_symlink_to_the_legacy_folder_is_refused(tmp_path):
    # An earlier release replaced an empty /data/state with a symlink to the
    # legacy folder; that link no longer makes the old folder count as state.
    seed(tmp_path / "hermes", "old.txt")
    os.symlink(tmp_path / "hermes", tmp_path / "state")
    result = run_entrypoint(tmp_path)
    assert_refused(result)
    assert (tmp_path / "state").is_symlink()


def test_both_folders_populated_uses_state_and_leaves_hermes_alone(tmp_path):
    seed(tmp_path / "state", "new.txt")
    seed(tmp_path / "hermes", "old.txt")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state").is_symlink()
    assert (tmp_path / "state" / "new.txt").exists()
    assert not (tmp_path / "state" / "old.txt").exists()
    assert (tmp_path / "hermes" / "old.txt").read_text() == "state\n"
    assert "hermes" not in result.stderr


def test_a_legacy_folder_of_only_empty_directories_is_not_data(tmp_path):
    (tmp_path / "hermes" / "state").mkdir(parents=True)
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "state").is_dir()
    assert not (tmp_path / "state").is_symlink()


def test_a_legacy_compat_link_to_state_is_not_data(tmp_path):
    # Earlier releases left /data/hermes as a symlink to /data/state.
    (tmp_path / "state").mkdir()
    os.symlink(tmp_path / "state", tmp_path / "hermes")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "state").is_dir()


def test_the_removed_migration_variables_change_nothing(tmp_path):
    seed(tmp_path / "legacy", "old.txt")
    result = run_entrypoint(tmp_path, PRINT_HANDY_STATE_MIGRATE_ONLY="1",
                            PRINT_HANDY_LEGACY_STATE=str(tmp_path / "legacy"))
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "state").is_dir()
    assert not (tmp_path / "state" / "old.txt").exists()
    text = ENTRYPOINT.read_text()
    for gone in ("PRINT_HANDY_STATE_MIGRATE_ONLY", "PRINT_HANDY_LEGACY_STATE",
                 ".migrated-from-legacy", ".migration-incomplete",
                 "PRINT_HANDY_ASSUME_STATE_MOUNT"):
        assert gone not in text


def test_the_image_does_not_precreate_the_legacy_folder():
    dockerfile = (ROOT / "Dockerfile").read_text()
    run_line = next(line for line in dockerfile.splitlines() if "mkdir -p /data/state" in line)
    assert "/data/hermes" not in run_line
    assert "/data/hermes" not in dockerfile


def test_a_leftover_write_probe_is_not_state_data(tmp_path):
    # A start killed between the probe's mktemp and rm must not make an empty
    # /data/state look populated and start without the old folder's data.
    seed(tmp_path / "hermes", "old.txt")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".ph-write-check.AbC123").write_text("")
    assert_refused(run_entrypoint(tmp_path))


# Real-mountpoint cell. Gated by PRINT_HANDY_LAYOUT_TEST_MOUNT=1 and run by
# the "State volume (real bind mount)" job in ci.yml (a runner VM where
# unprivileged user+mount namespaces work; that job probes them first and
# fails instead of skipping). Inside `unshare -rm` it bind-mounts HOME/state
# onto itself, the same shape as the Compose and Unraid bind mounts.
@pytest.mark.container
@pytest.mark.skipif(os.environ.get("PRINT_HANDY_LAYOUT_TEST_MOUNT") != "1",
                    reason="runs in the State volume (real bind mount) CI job, "
                           "which sets PRINT_HANDY_LAYOUT_TEST_MOUNT=1")
def test_an_empty_mounted_state_volume_with_legacy_data_refuses(tmp_path):
    import shlex
    import shutil
    unshare = shutil.which("unshare")
    if unshare is None or subprocess.run([unshare, "-rm", "true"],
                                         capture_output=True).returncode != 0:
        pytest.fail("user and mount namespaces are required for this cell")
    seed(tmp_path / "hermes", "old.txt")
    state = tmp_path / "state"
    state.mkdir()
    inner = (f"mount --bind {shlex.quote(str(state))} {shlex.quote(str(state))} && "
             f"HOME={shlex.quote(str(tmp_path))} TZ=America/New_York "
             f"bash {shlex.quote(str(ENTRYPOINT))} true")
    result = subprocess.run([unshare, "-rm", "bash", "-c", inner],
                            capture_output=True, text=True, timeout=60)
    assert_refused(result)
    assert list(state.iterdir()) == []
    assert (tmp_path / "hermes" / "old.txt").read_text() == "state\n"

# --- #1144: an unwritable data folder stops the start with one message that
# names the folder and the command that fixes it, before anything else
# runs. chmod 0555 is the stand-in for every real cause (a share
# that refuses chown, a read-only bind, a folder owned by another uid); root
# ignores modes, so these cells need an unprivileged test user.
non_root = pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                              reason="root ignores folder modes")
DATA_FOLDERS = ["state", "state/state", "state/logs", "archive", "records",
                "cache", "spoolman", "spoolman/config", "spoolman/logs",
                "spoolman/backups"]
CANNOT_START = "Print Handy: cannot start:"


def full_layout(home: Path):
    for rel in DATA_FOLDERS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    seed(home / "state", "record.txt")


def assert_one_clear_message(result, container_path: str):
    assert result.returncode != 0
    assert result.stderr.count(CANNOT_START) == 1, result.stderr
    uid, gid = os.getuid(), os.getgid()
    assert (f"{CANNOT_START} {container_path} is not writable by the container "
            f"user (uid {uid}, gid {gid}).") in result.stderr
    assert f"sudo chown -R {uid}:{gid} ./data" in result.stderr
    assert f"chown -R {uid}:{gid} /mnt/user/appdata/print-handy" in result.stderr
    assert "print-handy-init" in result.stderr
    # It stopped before anything else ran.
    assert OLD_FOLDER not in result.stderr


# #1416: the log folder is the one optional data folder. File logging is best
# effort and falls back to stderr, so an unwritable logs folder must not stop
# the start; every other folder keeps the #1144 stop-with-one-message rule.
OPTIONAL_FOLDERS = ["state/logs"]
REQUIRED_FOLDERS = [rel for rel in DATA_FOLDERS if rel not in OPTIONAL_FOLDERS]


@non_root
@pytest.mark.parametrize("rel", OPTIONAL_FOLDERS)
def test_an_unwritable_log_folder_still_starts_and_changes_nothing(tmp_path, rel):
    full_layout(tmp_path)
    before = snapshot(tmp_path)
    locked = tmp_path / rel
    locked.chmod(0o555)
    try:
        result = run_entrypoint(tmp_path)
    finally:
        locked.chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert CANNOT_START not in result.stderr
    assert (tmp_path / "state" / "record.txt").read_text() == "state\n"
    assert set(before) <= set(snapshot(tmp_path))


@non_root
@pytest.mark.parametrize("rel", ["", *REQUIRED_FOLDERS])
def test_an_unwritable_data_folder_stops_with_one_message_and_changes_nothing(tmp_path, rel):
    full_layout(tmp_path)
    before = snapshot(tmp_path)
    locked = tmp_path / rel
    locked.chmod(0o555)
    try:
        result = run_entrypoint(tmp_path)
    finally:
        locked.chmod(0o755)
    assert_one_clear_message(result, "/data/" + rel if rel else "/data")
    assert snapshot(tmp_path) == before
    assert (tmp_path / "state" / "record.txt").read_text() == "state\n"


@non_root
def test_a_new_install_whose_data_root_is_unwritable_stops_before_creating_anything(tmp_path):
    # The Unraid shape with a root-owned Appdata folder: nothing exists yet.
    tmp_path.chmod(0o555)
    try:
        result = run_entrypoint(tmp_path)
    finally:
        tmp_path.chmod(0o755)
    assert_one_clear_message(result, "/data")
    assert snapshot(tmp_path) == []


@non_root
def test_an_external_spoolman_install_does_not_check_the_spoolman_folder(tmp_path):
    full_layout(tmp_path)
    (tmp_path / "spoolman").chmod(0o555)
    try:
        env = dict(os.environ, HOME=str(tmp_path), TZ="America/New_York",
                   PRINT_HANDY_BUNDLED_SPOOLMAN="false")
        env.pop("PRINT_HANDY_ORCA_CHANNEL", None)
        env.pop("PRINT_HANDY_ORCA_CMD", None)
        result = subprocess.run(["bash", str(ENTRYPOINT), "true"], env=env,
                                capture_output=True, text=True, timeout=60)
    finally:
        (tmp_path / "spoolman").chmod(0o755)
    assert result.returncode == 0, result.stderr
    assert CANNOT_START not in result.stderr


def test_a_healthy_install_starts_with_no_message_and_leaves_no_probe(tmp_path):
    full_layout(tmp_path)
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert CANNOT_START not in result.stderr
    assert not list(tmp_path.rglob(".ph-write-check.*"))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    result = run_entrypoint(fresh)                      # nothing exists yet
    assert result.returncode == 0, result.stderr
    assert CANNOT_START not in result.stderr


# The Compose print-handy-init service (root, sh -ec): a folder it cannot
# create or chown fails it with one message naming that folder, and it stops
# there instead of carrying on to the next folder. The command runs as the
# shipped YAML has it, with /data/ pointed at a sandbox and chown shimmed (a
# test user cannot chown to 1000:1000, and the shim stands in for a share
# that refuses it).
INIT_FAILED = "Print Handy init: cannot create"


def run_init(tmp_path: Path, chown_exit: int) -> subprocess.CompletedProcess:
    import yaml
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["print-handy-init"]
    assert service["entrypoint"] == ["/bin/sh", "-ec"]
    # Container paths only (not the host-relative ./data/ in the messages);
    # /host-data is the read-only mount of the whole data folder (#1817).
    script = re.sub(r"(?<![.\w])/data/", f"{tmp_path.as_posix()}/data/",
                    service["command"][0].replace("$$", "$"))
    script = script.replace("/host-data", f"{tmp_path.as_posix()}/data")
    shim = tmp_path / "bin"
    shim.mkdir()
    refusal = "" if chown_exit == 0 else (
        "echo \"chown: changing ownership of '$2': Operation not permitted\" >&2\n")
    (shim / "chown").write_text(f"#!/bin/sh\n{refusal}exit {chown_exit}\n")
    (shim / "chown").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")
    # cwd is the temp folder: a path the shell mangles lands there, never in the repo.
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True,
                          text=True, timeout=60, cwd=tmp_path)


def test_init_service_creates_every_folder_when_chown_works(tmp_path):
    result = run_init(tmp_path, 0)
    assert result.returncode == 0, result.stderr
    assert INIT_FAILED not in result.stderr
    for name in ("state", "archive", "records", "cache", "spoolman"):
        assert (tmp_path / "data" / name).is_dir()


def test_init_service_names_the_folder_whose_chown_is_refused(tmp_path):
    result = run_init(tmp_path, 1)
    assert result.returncode != 0
    assert result.stderr.count(INIT_FAILED) == 1, result.stderr
    folder = f"{tmp_path.as_posix()}/data/state"
    assert f"{INIT_FAILED} {folder} or make it owned by 1000:1000" in result.stderr
    assert "sudo chown -R 1000:1000 ./data" in result.stderr
    assert not (tmp_path / "data" / "archive").exists()    # stopped at the first


@non_root
def test_init_service_stops_at_a_folder_it_cannot_create(tmp_path):
    # Before #1144 a failed mkdir inside `mkdir && chown` did not trip -e, so
    # the loop carried on to the next folder.
    data = tmp_path / "data"
    data.mkdir()
    (data / "state").mkdir()
    data.chmod(0o555)
    try:
        result = run_init(tmp_path, 0)
    finally:
        data.chmod(0o755)
    assert result.returncode != 0
    assert result.stderr.count(INIT_FAILED) == 1, result.stderr
    assert f"{INIT_FAILED} {data}/archive or make it owned" in result.stderr


# #1817: Compose's main container mounts no hermes folder, so the init service
# is where an install whose state is still only in ./data/hermes is refused.
INIT_OLD_FOLDER = "Print Handy init: cannot start: your state is still in the old folder ./data/hermes"


def test_init_service_refuses_state_left_only_in_the_old_hermes_folder(tmp_path):
    seed(tmp_path / "data" / "hermes" / "state", "printer_hardware.json")
    (tmp_path / "data" / "state" / "state").mkdir(parents=True)   # empty skeleton
    result = run_init(tmp_path, 0)
    assert result.returncode != 0
    assert result.stderr.count(INIT_OLD_FOLDER) == 1, result.stderr
    assert "rename ./data/hermes to ./data/state" in result.stderr
    assert not (tmp_path / "data" / "archive").exists()             # stopped first
    assert (tmp_path / "data" / "hermes" / "state" / "printer_hardware.json").is_file()


def test_init_service_starts_when_state_holds_data_beside_an_old_hermes_folder(tmp_path):
    seed(tmp_path / "data" / "hermes", "old.txt")
    seed(tmp_path / "data" / "state", "new.txt")
    result = run_init(tmp_path, 0)
    assert result.returncode == 0, result.stderr
    assert INIT_OLD_FOLDER not in result.stderr
    assert (tmp_path / "data" / "archive").is_dir()


def test_init_service_ignores_an_empty_old_hermes_folder(tmp_path):
    (tmp_path / "data" / "hermes" / "state").mkdir(parents=True)
    result = run_init(tmp_path, 0)
    assert result.returncode == 0, result.stderr
    assert INIT_OLD_FOLDER not in result.stderr
