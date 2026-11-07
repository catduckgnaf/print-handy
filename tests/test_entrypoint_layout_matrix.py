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


# Acceptance-layout cell, deliberately gated and not run by any CI workflow:
# accounted for by name in tests/README.md ("Fixture-vs-live rules" and the
# live-proof roster). It runs only in the prepared acceptance layout.
@pytest.mark.container
@pytest.mark.skipif(os.environ.get("PRINT_HANDY_LAYOUT_TEST_MOUNT") != "1",
                    reason="needs the acceptance layout: HOME/state prepared "
                           "as a real mountpoint with a populated legacy volume")
def test_mounted_state_volume_copies_instead_of_symlinking(tmp_path):
    """The two-bind layout: /data/state is a mountpoint, so it cannot be
    replaced by a symlink — the entrypoint must COPY the legacy contents
    in, leave the legacy volume untouched, and clear the mark."""
    seed(tmp_path / "hermes", "old.txt")
    result = run_entrypoint(tmp_path)
    assert result.returncode == 0, result.stderr
    state = tmp_path / "state"
    assert not state.is_symlink()
    assert (state / "old.txt").read_text() == "state\n"
    assert not (state / ".migration-incomplete").exists()
    assert "cannot be replaced by a symlink" in result.stderr
