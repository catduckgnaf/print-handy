#!/bin/sh
# Update the home checkout, then prove it works. Roll back if it does not.
#
#   scripts/home-update.sh            fast-forward to origin/main
#   scripts/home-update.sh v2.0.2     fast-forward to a tag or branch
#
# Never touches config/local.yaml or config/local.toml. If config/after-update.sh exists (gitignored), it runs after a
# good update, for example to restart the Discord listener. A good update also removes Print Handy
# state files stranded outside the profile state folder (python3 -m print_handy.state_files).
set -eu
cd "$(dirname "$0")/.."
target="${1:-origin/main}"
# An explicit $PYTHON wins, then python3.
if [ -n "${PYTHON:-}" ]; then
    python=$PYTHON
else
    python=python3
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Tracked files have local edits. Move machine settings into config/local.yaml or config/local.toml, then retry."
    exit 1
fi

previous=$(git rev-parse HEAD)
git fetch --quiet --tags origin
case "$target" in origin/*|v*|*.*) ;; *) git rev-parse --verify --quiet "origin/$target" >/dev/null && target="origin/$target" ;; esac
if ! git merge --ff-only --quiet "$target"; then
    echo "Cannot fast-forward to $target. Nothing changed."
    exit 1
fi
if [ "$(git rev-parse HEAD)" = "$previous" ]; then
    echo "Already at $target."
    exit 0
fi

echo "Updated $(git rev-parse --short "$previous") -> $(git rev-parse --short HEAD). Checking..."
update_ok=1
hook_failed=0
if PRINT_HANDY_UPDATE_CHECK=1 "$python" -m pytest -q -p no:cacheprovider tests && "$python" -m print_handy.doctor; then
    # The post-update hook is part of the proof (P2-4): if it fails — a listener
    # restart, say — the update rolls back like a failed check, so a retry
    # re-runs the hook instead of stranding the install at the new HEAD behind
    # the "Already at" exit, with the hook never retried.
    if [ -x config/after-update.sh ] && ! config/after-update.sh; then
        echo "The post-update hook (config/after-update.sh) failed."
        update_ok=0
        hook_failed=1
    fi
else
    update_ok=0
fi
if [ "$update_ok" = 1 ]; then
    "$python" -m print_handy.state_files --remove || echo "Could not check for stray state files."
    echo "Update good."
else
    git reset --quiet --hard "$previous"
    echo "Checks failed. Rolled back to $(git rev-parse --short HEAD)."
    # The failed hook may have restarted services onto the new code; run it again on the restored
    # tree so they follow the rollback (2.2.5 review). Best effort: a second failure is only a warning.
    if [ "$hook_failed" = 1 ] && [ -x config/after-update.sh ] && ! config/after-update.sh; then
        echo "WARNING: config/after-update.sh also failed on the restored code. Services may still be running the new code; restart them by hand."
    fi
    exit 1
fi
