"""Entrypoint x layout matrix (testing-hardening pass, E7).

``docker/entrypoint.sh`` owns the #914 state-volume migration: the
neutral name is /data/state, /data/hermes is the pre-2.2.5 legacy name,
and which of the two a given install actually maps is a LAYOUT
dimension the release-contract tests did not exercise as a matrix.
The entrypoint keys its data root off ``$HOME``, so every cell runs
the REAL shipped script offline against a synthetic home: the script
runs to its final ``exec "$@"`` with ``true`` as the command.

Cells that need /data/state to be a genuine mountpoint (the copy
branch) cannot be faked from a test process; the one such cell is
marked ``container`` per repo convention and additionally gated on the
acceptance layout being prepared — it is named in tests/README.md as
an acceptance-environment cell, not silently skipped here.
"""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"


def run_entrypoint(home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TZ"] = "America/New_York"
    env.pop("PRINT_HANDY_ORCA_CHANNEL", None)
    env.pop("PRINT_HANDY_ORCA_CMD", None)
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "true"],
        env=env, capture_output=True, text=True, timeout=60)


def seed(folder: Path, name="record.txt"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("state\n", encoding="utf-8")


def test_state_only_layout_keeps_state_and_links_the_legacy_name(tmp_path):
    seed(tmp_path / "state")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "record.txt").read_text() == "state\n"
    legacy = tmp_path / "hermes"
    assert legacy.is_symlink() and os.readlink(legacy) == str(tmp_path / "state")
    assert "pre-2.2.5 name keeps working" in result.stderr


def test_legacy_only_layout_adopts_the_legacy_volume_by_symlink(tmp_path):
    seed(tmp_path / "hermes")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    state = tmp_path / "state"
    assert state.is_symlink() and os.readlink(state) == str(tmp_path / "hermes")
    assert (state / "record.txt").read_text() == "state\n"
    assert "using the legacy /data/hermes state volume as /data/state" in result.stderr


def test_both_volumes_populated_uses_state_and_warns(tmp_path):
    seed(tmp_path / "state", "new.txt")
    seed(tmp_path / "hermes", "old.txt")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state").is_symlink()
    assert (tmp_path / "state" / "new.txt").exists()
    assert "both /data/state and the legacy /data/hermes state volumes hold data" in result.stderr


def test_fresh_layout_creates_the_state_tree_and_no_links(tmp_path):
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "state").is_dir()
    assert (tmp_path / "state" / "logs").is_dir()
    assert not (tmp_path / "hermes").exists()


def test_incomplete_copy_mark_with_legacy_data_restarts_the_migration(tmp_path):
    seed(tmp_path / "hermes", "old.txt")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".migration-incomplete").write_text("", encoding="utf-8")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    # The plain-folder path replaces the partial volume with the symlink.
    assert state.is_symlink() and os.readlink(state) == str(tmp_path / "hermes")


def test_already_symlinked_state_is_left_untouched(tmp_path):
    seed(tmp_path / "hermes", "old.txt")
    os.symlink(tmp_path / "hermes", tmp_path / "state")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state").is_symlink()
    assert "a symlink to" in result.stderr and "legacy /data/hermes volume is in use" in result.stderr


# Real-mountpoint cell. Gated by PRINT_HANDY_LAYOUT_TEST_MOUNT=1 and run by
# the "State volume (real bind mount)" job in ci.yml (a runner VM where
# unprivileged user+mount namespaces work; that job probes them first and
# fails instead of skipping). The test makes the mountpoint itself: inside
# `unshare -rm` it bind-mounts HOME/state onto itself, the same shape as the
# Compose bind mount, so no unprivileged-hostile preparation is needed.
@pytest.mark.container
@pytest.mark.skipif(os.environ.get("PRINT_HANDY_LAYOUT_TEST_MOUNT") != "1",
                    reason="runs in the State volume (real bind mount) CI job, "
                           "which sets PRINT_HANDY_LAYOUT_TEST_MOUNT=1")
def test_mounted_state_volume_copies_instead_of_symlinking(tmp_path):
    """The two-bind layout: /data/state is a mountpoint, so it cannot be
    replaced by a symlink; the entrypoint must COPY the legacy contents
    in, leave the legacy volume untouched, and clear the mark."""
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
    assert result.returncode == 0, result.stderr
    assert not state.is_symlink()
    assert (state / "old.txt").read_text() == "state\n"
    assert (tmp_path / "hermes" / "old.txt").read_text() == "state\n"
    assert not (state / ".migration-incomplete").exists()
    assert (state / ".migrated-from-legacy").is_file()
    assert "cannot be replaced by a symlink" in result.stderr


# --- #1144: the copy is one-time, never overwrites, resumes, and counts
# symlinks as data. PRINT_HANDY_ASSUME_STATE_MOUNT=1 makes an empty
# /data/state read as the mountpoint it is in Compose (a test process cannot
# create a real mount), which selects the copy branch.

def run_mounted(home: Path, **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"HOME": str(home), "TZ": "America/New_York",
                "PRINT_HANDY_ASSUME_STATE_MOUNT": "1", **extra})
    env.pop("PRINT_HANDY_ORCA_CHANNEL", None)
    env.pop("PRINT_HANDY_ORCA_CMD", None)
    return subprocess.run(["bash", str(ENTRYPOINT), "true"], env=env,
                          capture_output=True, text=True, timeout=60)


def symlink(target: str, link: Path):
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["bash", "-c", 'ln -s "$1" "$2"', "_", target, str(link)], check=True)


def tree(root: Path) -> dict:
    """Relative path -> (kind, content-or-target, mtime_ns) for every entry."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path), None)
        elif path.is_dir():
            out[rel] = ("dir", None, None)
        else:
            out[rel] = ("file", path.read_text(), path.stat().st_mtime_ns)
    return out


def test_copy_runs_once_and_the_second_run_is_a_no_op(tmp_path):
    seed(tmp_path / "hermes", "old.txt")
    (tmp_path / "hermes" / "sub").mkdir()
    (tmp_path / "hermes" / "sub" / "deep.txt").write_text("deep\n")
    (tmp_path / "state").mkdir()
    first = run_mounted(tmp_path)
    assert first.returncode == 0, first.stderr
    state = tmp_path / "state"
    assert (state / "old.txt").read_text() == "state\n"
    assert (state / "sub" / "deep.txt").read_text() == "deep\n"
    assert (state / ".migrated-from-legacy").is_file()
    assert not (state / ".migration-incomplete").exists()
    assert "were copied into /data/state" in first.stderr
    legacy_before, state_before = tree(tmp_path / "hermes"), tree(state)
    second = run_mounted(tmp_path)
    assert second.returncode == 0, second.stderr
    assert "were copied into /data/state" not in second.stderr
    assert "already copied into /data/state" in second.stderr
    assert tree(state) == state_before
    assert tree(tmp_path / "hermes") == legacy_before


def test_stale_legacy_never_overwrites_newer_state_after_the_migration(tmp_path):
    seed(tmp_path / "hermes", "shared.txt")
    (tmp_path / "state").mkdir()
    assert run_mounted(tmp_path).returncode == 0
    state = tmp_path / "state"
    # The app keeps running on /data/state; the legacy folder stays stale.
    (state / "shared.txt").write_text("newer\n")
    (state / "fresh.txt").write_text("fresh\n")
    (tmp_path / "hermes" / "shared.txt").write_text("stale\n")
    (tmp_path / "hermes" / "gone.txt").write_text("deleted since\n")
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "shared.txt").read_text() == "newer\n"
    assert (state / "fresh.txt").read_text() == "fresh\n"
    # Not even a file the operator deleted from /data/state comes back.
    assert not (state / "gone.txt").exists()


def test_stale_legacy_does_not_overwrite_state_that_predates_the_record(tmp_path):
    # An install migrated by a release that wrote no record: both folders
    # hold data, so nothing is copied.
    seed(tmp_path / "state", "shared.txt")
    (tmp_path / "state" / "shared.txt").write_text("newer\n")
    seed(tmp_path / "hermes", "shared.txt")
    (tmp_path / "hermes" / "shared.txt").write_text("stale\n")
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "state" / "shared.txt").read_text() == "newer\n"
    assert not (tmp_path / "state" / ".migrated-from-legacy").exists()
    assert "both /data/state and the legacy /data/hermes state volumes hold data" in result.stderr


def test_resume_over_a_mark_keeps_newer_files_and_fills_the_gaps(tmp_path):
    seed(tmp_path / "hermes", "a.txt")
    (tmp_path / "hermes" / "a.txt").write_text("legacy a\n")
    (tmp_path / "hermes" / "b.txt").write_text("legacy b\n")
    (tmp_path / "hermes" / "c.txt").write_text("legacy c\n")
    state = tmp_path / "state"
    state.mkdir()
    scratch = state / ".ph-migrating.Ab12Cd"             # killed mid-copy:
    scratch.mkdir()                                      # the run's scratch,
    (scratch / "entry").write_text("leg")                # named by the mark
    (state / ".migration-incomplete").write_text(f"{scratch}\n")
    (state / "a.txt").write_text("newer a\n")           # done, then edited
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "did not finish; completing it" in result.stderr
    assert (state / "a.txt").read_text() == "newer a\n"
    assert (state / "b.txt").read_text() == "legacy b\n"
    assert (state / "c.txt").read_text() == "legacy c\n"
    assert not scratch.exists()
    assert not list(state.glob(".ph-migrating.*"))
    assert not (state / ".migration-incomplete").exists()
    assert (state / ".migrated-from-legacy").is_file()


def test_files_named_like_the_old_temp_suffix_are_data_and_survive(tmp_path):
    # #1215 review: the copy used "<name>.ph-migrating" as its temporary
    # path and `rm -rf`-ed it, and a resume deleted every "*.ph-migrating"
    # under /data/state. Both destroyed legitimate files with that name.
    legacy = tmp_path / "hermes"
    seed(legacy, "foo")
    (legacy / "foo.ph-migrating").write_text("legacy sibling\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "keep.ph-migrating").write_text("operator file\n")
    (state / ".migration-incomplete").write_text("")   # a resume, old-style empty mark
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "foo").read_text() == "state\n"
    assert (state / "foo.ph-migrating").read_text() == "legacy sibling\n"
    assert (state / "keep.ph-migrating").read_text() == "operator file\n"
    assert not list(state.glob(".ph-migrating.*"))


def test_a_mark_naming_any_other_path_deletes_nothing(tmp_path):
    # Resume removes only a scratch directory this script made under
    # /data/state; a mark that names anything else is ignored.
    seed(tmp_path / "hermes", "a.txt")
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "x").write_text("keep\n")
    state = tmp_path / "state"
    state.mkdir()
    lookalike = state / "notes.ph-migrating.d"
    lookalike.mkdir()
    for named in (outside, lookalike):
        (state / ".migration-incomplete").write_text(f"{named}\n")
        result = run_mounted(tmp_path)
        assert result.returncode == 0, result.stderr
        (state / ".migrated-from-legacy").unlink()
    assert (outside / "x").read_text() == "keep\n"
    assert lookalike.is_dir()


def test_recopying_over_a_mark_twice_is_safe(tmp_path):
    seed(tmp_path / "hermes", "a.txt")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".migration-incomplete").write_text("")
    assert run_mounted(tmp_path).returncode == 0
    before = tree(state)
    (state / ".migration-incomplete").write_text("")  # a stale mark again
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (state / ".migration-incomplete").exists()
    assert tree(state) == before


def test_a_legacy_folder_of_only_symlinks_is_data_and_is_copied_as_links(tmp_path):
    symlink("elsewhere", tmp_path / "hermes" / "link")
    (tmp_path / "state").mkdir()
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert os.readlink(tmp_path / "state" / "link") == "elsewhere"
    assert (tmp_path / "state" / ".migrated-from-legacy").is_file()


def test_a_state_folder_of_only_symlinks_is_data_and_is_not_replaced(tmp_path):
    symlink("elsewhere", tmp_path / "state" / "link")
    seed(tmp_path / "hermes", "old.txt")
    result = run_entrypoint(tmp_path)  # plain folder: would be swapped for a link if "empty"
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state").is_symlink()
    assert os.readlink(tmp_path / "state" / "link") == "elsewhere"
    assert not (tmp_path / "state" / "old.txt").exists()
    assert "both /data/state and the legacy /data/hermes state volumes hold data" in result.stderr


def test_a_legacy_folder_of_only_empty_directories_is_not_a_source(tmp_path):
    (tmp_path / "hermes" / "state" / "nested").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state" / ".migrated-from-legacy").exists()
    assert not (tmp_path / "state" / "nested").exists()
    assert "copied into /data/state" not in result.stderr


def test_a_state_folder_of_only_empty_directories_still_migrates(tmp_path):
    (tmp_path / "state" / "state").mkdir(parents=True)
    (tmp_path / "state" / "logs").mkdir()
    seed(tmp_path / "hermes", "old.txt")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    state = tmp_path / "state"
    assert state.is_symlink() and os.readlink(state) == str(tmp_path / "hermes")


def test_the_compat_link_to_state_is_not_a_migration_source_on_restart(tmp_path):
    seed(tmp_path / "state")
    assert run_entrypoint(tmp_path).returncode == 0  # creates hermes -> state
    assert (tmp_path / "hermes").is_symlink()
    before = tree(tmp_path / "state")
    again = run_entrypoint(tmp_path)  # a restart in the same container
    assert again.returncode == 0, again.stderr
    assert "hold data" not in again.stderr and "copied" not in again.stderr
    assert not (tmp_path / "state").is_symlink()
    assert tree(tmp_path / "state") == before


def test_migrate_only_mode_copies_from_a_foreign_path_and_stops(tmp_path):
    legacy = tmp_path / "ro" / "hermes"
    seed(legacy, "old.txt")
    home = tmp_path / "data"
    (home / "state").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home), PRINT_HANDY_ASSUME_STATE_MOUNT="1",
               PRINT_HANDY_STATE_MIGRATE_ONLY="1", PRINT_HANDY_LEGACY_STATE=str(legacy))
    result = subprocess.run(["bash", str(ENTRYPOINT), "false"], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr   # exited before exec "$@"
    assert (home / "state" / "old.txt").read_text() == "state\n"
    assert not (home / "archive").exists()           # no application setup
    assert not (home / "hermes").exists()            # no compat link either
    again = subprocess.run(["bash", str(ENTRYPOINT), "false"], env=env,
                           capture_output=True, text=True, timeout=60)
    assert again.returncode == 0
    assert "were copied" not in again.stderr


def test_migrate_only_mode_with_no_legacy_folder_is_a_quiet_no_op(tmp_path):
    home = tmp_path / "data"
    (home / "state").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home), PRINT_HANDY_STATE_MIGRATE_ONLY="1",
               PRINT_HANDY_LEGACY_STATE=str(tmp_path / "ro" / "hermes"))
    result = subprocess.run(["bash", str(ENTRYPOINT), "false"], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stderr == ""
    assert not (home / "state" / ".migrated-from-legacy").exists()
    assert not (tmp_path / "ro").exists()


def test_the_image_does_not_precreate_the_legacy_folder():
    dockerfile = (ROOT / "Dockerfile").read_text()
    run_line = next(line for line in dockerfile.splitlines()
                    if line.startswith("RUN mkdir -p /data/state/state"))
    assert "/data/hermes" not in run_line


def test_a_file_where_legacy_has_a_folder_is_kept_and_the_rest_still_migrates(tmp_path):
    # #1215 review: /data/state/foo a file, legacy foo/bar a file inside a
    # folder. `mkdir -p` failed under set -e and aborted the whole migration.
    legacy = tmp_path / "hermes"
    (legacy / "foo").mkdir(parents=True)
    (legacy / "foo" / "bar").write_text("legacy bar\n")
    (legacy / "foo" / "deeper").mkdir()
    (legacy / "foo" / "deeper" / "x").write_text("x\n")
    (legacy / "other.txt").write_text("other\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "foo").write_text("operator file\n")
    (state / ".migration-incomplete").write_text("")
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "foo").read_text() == "operator file\n"
    assert (state / "other.txt").read_text() == "other\n"
    assert "/data/state/foo already exists and is not a folder" in result.stderr
    assert result.stderr.count("is not a folder") == 1
    assert (state / ".migrated-from-legacy").is_file()
    assert not (state / ".migration-incomplete").exists()


def test_nothing_is_written_through_a_symlink_in_state(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    legacy = tmp_path / "hermes"
    (legacy / "linked").mkdir(parents=True)
    (legacy / "linked" / "x.txt").write_text("x\n")
    (legacy / "plain.txt").write_text("plain\n")
    state = tmp_path / "state"
    state.mkdir()
    symlink(str(outside), state / "linked")
    (state / ".migration-incomplete").write_text("")
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(outside.iterdir()) == []
    assert (state / "linked").is_symlink()
    assert (state / "plain.txt").read_text() == "plain\n"


def test_resume_completes_a_file_an_old_style_copy_left_truncated(tmp_path):
    # A pre-#1144 `cp -a` wrote straight onto the final names, so a copy
    # killed mid-file left a truncated file under its real name. A strict
    # prefix of the legacy file is completed; any other difference is kept.
    legacy = tmp_path / "hermes"
    legacy.mkdir()
    (legacy / "db.json").write_text('{"jobs": [1, 2, 3]}\n')
    (legacy / "edited.txt").write_text("legacy text\n")
    (legacy / "longer.txt").write_text("ab\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "db.json").write_text('{"jobs": [1,')
    (state / "edited.txt").write_text("legacy\n")        # not a prefix: kept
    (state / "longer.txt").write_text("ab\nmore\n")      # longer: kept
    (state / ".migration-incomplete").write_text("")
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "db.json").read_text() == '{"jobs": [1, 2, 3]}\n'
    assert (state / "edited.txt").read_text() == "legacy\n"
    assert (state / "longer.txt").read_text() == "ab\nmore\n"
    assert "completed /data/state/db.json" in result.stderr


def test_a_first_copy_never_rewrites_an_existing_prefix_file(tmp_path):
    # Without a mark (a first copy) an existing file always wins as is.
    legacy = tmp_path / "hermes"
    legacy.mkdir()
    (legacy / "log.txt").write_text("line 1\nline 2\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "log.txt").write_text("line 1\n")
    (state / "sub").mkdir()                            # state reads as empty
    result = run_mounted(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (state / "log.txt").read_text() == "line 1\n"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root reads a mode-000 folder, so the failure cannot be staged")
def test_an_unreadable_legacy_folder_fails_the_start_and_is_not_recorded(tmp_path):
    legacy = tmp_path / "hermes"
    seed(legacy, "ok.txt")
    locked = legacy / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("s\n")
    state = tmp_path / "state"
    state.mkdir()
    locked.chmod(0)
    try:
        result = run_mounted(tmp_path)
    finally:
        locked.chmod(0o755)
    assert result.returncode != 0
    assert (state / ".migration-incomplete").exists()
    assert not (state / ".migrated-from-legacy").exists()
    again = run_mounted(tmp_path)                       # fixed: the next start resumes
    assert again.returncode == 0, again.stderr
    assert (state / "locked" / "secret.txt").read_text() == "s\n"
    assert (state / ".migrated-from-legacy").is_file()


def test_a_migrated_install_without_the_legacy_folder_still_gets_the_compat_link(tmp_path):
    # New Compose: print-handy-migrate copied ./data/hermes, and the main
    # container mounts no /data/hermes. The pre-2.2.5 name (and an explicit
    # paths.hermes_home=/data/hermes) must still resolve to /data/state.
    state = tmp_path / "state"
    seed(state)
    (state / ".migrated-from-legacy").write_text("2026-11-10T00:00:00Z\n")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    legacy = tmp_path / "hermes"
    assert legacy.is_symlink() and os.readlink(legacy) == str(state)
    assert (state / "record.txt").read_text() == "state\n"



# --- #1144: an unwritable data folder stops the start with one message that
# names the folder and the command that fixes it, before the migration or
# anything else runs. chmod 0555 is the stand-in for every real cause (a share
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


def snapshot(home: Path) -> list:
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def assert_one_clear_message(result, container_path: str):
    assert result.returncode != 0
    assert result.stderr.count(CANNOT_START) == 1, result.stderr
    uid, gid = os.getuid(), os.getgid()
    assert (f"{CANNOT_START} {container_path} is not writable by the container "
            f"user (uid {uid}, gid {gid}).") in result.stderr
    assert f"sudo chown -R {uid}:{gid} ./data" in result.stderr
    assert f"chown -R {uid}:{gid} /mnt/user/appdata/print-handy" in result.stderr
    assert "print-handy-init" in result.stderr
    # It stopped before the migration and the application: nothing else ran.
    assert "pre-2.2.5 name keeps working" not in result.stderr


@non_root
@pytest.mark.parametrize("rel", ["", *DATA_FOLDERS])
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
def test_migrate_only_mode_also_stops_on_an_unwritable_state_folder(tmp_path):
    # Compose's print-handy-migrate service runs the same check first.
    seed(tmp_path / "legacy", "old.txt")
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    try:
        result = run_mounted(tmp_path, PRINT_HANDY_STATE_MIGRATE_ONLY="1",
                             PRINT_HANDY_LEGACY_STATE=str(tmp_path / "legacy"))
    finally:
        state.chmod(0o755)
    assert_one_clear_message(result, "/data/state")
    assert list(state.iterdir()) == []


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


def test_a_leftover_write_probe_is_not_data_for_the_migration(tmp_path):
    # A start killed between the probe's mktemp and rm must not turn an empty
    # /data/state into "both volumes hold data" and strand the legacy volume.
    seed(tmp_path / "hermes", "old.txt")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".ph-write-check.AbC123").write_text("")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    assert state.is_symlink() and os.readlink(state) == str(tmp_path / "hermes")


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
    script = service["command"][0].replace("$$", "$").replace("/data/", f"{tmp_path}/data/")
    shim = tmp_path / "bin"
    shim.mkdir()
    refusal = "" if chown_exit == 0 else (
        "echo \"chown: changing ownership of '$2': Operation not permitted\" >&2\n")
    (shim / "chown").write_text(f"#!/bin/sh\n{refusal}exit {chown_exit}\n")
    (shim / "chown").chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}")
    return subprocess.run(["sh", "-ec", script], env=env, capture_output=True,
                          text=True, timeout=60)


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
    folder = f"{tmp_path}/data/state"
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
