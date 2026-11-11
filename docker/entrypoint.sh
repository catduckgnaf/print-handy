#!/bin/bash
set -euo pipefail
# State volume (#914): the main state volume's neutral name is /data/state;
# /data/hermes is its pre-2.2.5 name, kept as a legacy fallback for at
# least two minor releases. The entrypoint runs before any service, so the
# backward-compatible migration lives here.
#
# The rules (#1144):
#   * Nothing is ever overwritten. The legacy volume is only a source for
#     files /data/state does not have; an existing file in /data/state wins
#     whatever its timestamp, so a stale legacy copy can never replace newer
#     data, however many times the copy runs.
#   * The copy is one-time. When it completes, /data/state/.migrated-from-legacy
#     records that, and later starts leave the legacy volume alone (with a
#     warning while it still holds files). That record, not "state looks
#     empty", is what stops a leftover /data/hermes from coming back.
#   * The copy is resumable. /data/state/.migration-incomplete exists while it
#     runs. Each file is written under a temporary name and renamed into
#     place, so a file with its final name is always complete; a start that
#     finds the mark finishes the job, skipping what is already there, and
#     re-copying over a mark is safe.
#   * When only the legacy volume holds data and /data/state is an empty plain
#     folder, /data/state becomes a symlink to it and nothing is copied.
# Rollback limits: see docs/DOCKER.md "State volume rename". A release before
# 2.2.5 reads only /data/hermes, which this migration leaves as it was, so
# anything written there during a rollback is never carried back into
# /data/state.
#
# Compose no longer mounts ./data/hermes. Its print-handy-migrate service runs
# this script with PRINT_HANDY_STATE_MIGRATE_ONLY=1 and the host folder (when
# it exists) mounted read-only at PRINT_HANDY_LEGACY_STATE, so an install that
# still has the folder keeps migrating and a new install never creates it.
# An explicit paths.hermes_home pointing at /data/hermes is honored unchanged
# (no symlink is needed then).
data_root="${HOME:-/data}"
state_home="$data_root/state"
legacy_home="${PRINT_HANDY_LEGACY_STATE:-$data_root/hermes}"
mark_file="$state_home/.migration-incomplete"
done_file="$state_home/.migrated-from-legacy"
# A folder "has files" when it holds anything that is not a directory: a
# regular file, a symlink (dangling or not), a socket. Empty directories are
# structure, not data, so a tree of only empty directories reads as empty.
# The two migration marks do not count. -H follows a symlink given as the
# folder itself but never a symlink inside it.
_has_files() {
    [ -d "$1" ] || return 1
    [ -n "$(find -H "$1" -mindepth 1 \( -path "$1/.migration-incomplete" -o -path "$1/.migrated-from-legacy" \) -prune -o ! -type d -print -quit 2>/dev/null)" ]
}
# The legacy volume is a migration source when it holds files and is not just
# another name for /data/state (the compat symlink this script creates).
_legacy_is_source() {
    [ -d "$legacy_home" ] || return 1
    [ "$(readlink -f "$legacy_home")" != "$(readlink -f "$state_home")" ] || return 1
    _has_files "$legacy_home"
}
_is_mountpoint() {
    # Test hook: PRINT_HANDY_ASSUME_STATE_MOUNT=1 makes /data/state read as a
    # mountpoint, because a test process cannot create a real one.
    [ "${PRINT_HANDY_ASSUME_STATE_MOUNT:-}" = 1 ] && [ "$1" = "$state_home" ] && return 0
    # Two signals, either is enough: the folder is listed as a
    # mountpoint in /proc/self/mounts (catches every mount, bind mounts
    # included), or its device differs from its parent folder's device
    # (a mounted filesystem of its own; stat is GNU coreutils, which the
    # Debian image ships). Unreadable inputs read as "not a mountpoint"
    # so the plain-folder path below still applies.
    [ -d "$1" ] || return 1
    local dev parent_dev mp _
    if [ -r /proc/self/mounts ]; then
        while read -r _ mp _; do
            case "$mp" in *\\0*) mp="$(printf '%b' "$mp")" ;; esac
            [ "$mp" = "$1" ] && return 0
        done < /proc/self/mounts
    fi
    dev="$(stat -c %d "$1" 2>/dev/null)" || return 1
    parent_dev="$(stat -c %d "$1/.." 2>/dev/null)" || return 1
    [ -n "$dev" ] && [ -n "$parent_dev" ] && [ "$dev" != "$parent_dev" ]
}
# Copy into $2 every entry of $1 that $2 does not have. Directories are
# created, existing entries are skipped untouched, and every other entry is
# written inside $3 first and renamed into place, so an interruption leaves no
# half-written file under a real name. $3 is a private directory made by
# mktemp for this run: no temporary path is ever derived from a file name, so
# a legitimate file can never collide with one and be deleted (#1215 review).
# Ownership is not preserved: the copy runs as the volume's runtime user, and
# a filesystem that refuses chown must not stop it.
_copy_missing() {
    local src="$1" dst="$2" scratch="$3" path rel target tmp
    tmp="$scratch/entry"
    while IFS= read -r -d '' path; do
        rel="${path#"$src"/}"
        target="$dst/$rel"
        case "$rel" in .migration-incomplete|.migrated-from-legacy) continue ;; esac
        if [ -d "$path" ] && [ ! -L "$path" ]; then
            mkdir -p "$target"
        elif [ ! -e "$target" ] && [ ! -L "$target" ]; then
            rm -rf -- "$tmp"
            cp -a --no-preserve=ownership "$path" "$tmp"
            mv -n "$tmp" "$target"
        fi
    done < <(find -H "$src" -mindepth 1 -print0)
    rm -rf -- "$tmp"
}
# The scratch directory an interrupted copy recorded in the mark file, when
# the mark names one this script made; empty otherwise. Resume removes only
# that exact directory, never anything matched by name.
_recorded_scratch() {
    local recorded
    recorded="$(head -n 1 "$mark_file" 2>/dev/null)" || return 0
    case "$recorded" in
        "$state_home"/.ph-migrating.*)
            if [ -d "$recorded" ] && [ ! -L "$recorded" ]; then printf '%s' "$recorded"; fi ;;
    esac
    return 0
}
if [ -L "$state_home" ]; then
    echo "Print Handy: state volume is /data/state, a symlink to $(readlink "$state_home") — the legacy /data/hermes volume is in use (deprecated name; it stays accepted for at least two minor releases)." >&2
elif [ -e "$done_file" ]; then
    # Migrated once already. A leftover mark means the copy finished and the
    # start ended before the mark was removed.
    rm -f "$mark_file"
    if _legacy_is_source; then
        echo "Print Handy: the legacy /data/hermes volume was already copied into /data/state; using /data/state and ignoring /data/hermes, which may be stale and is never copied again (it can be deleted once /data/state is verified)." >&2
    fi
elif _legacy_is_source && { ! _has_files "$state_home" || [ -e "$mark_file" ]; }; then
    if _is_mountpoint "$state_home" || _has_files "$state_home"; then
        # rm -rf on a mountpoint fails EBUSY and would stop the container
        # before any service starts, and a folder that already holds files
        # (a partial copy) must not be deleted: both are populated from the
        # legacy volume instead of being replaced by a symlink. Entries that
        # already exist are kept, so nothing newer is overwritten.
        if [ -e "$mark_file" ]; then
            echo "Print Handy: an earlier copy of the legacy /data/hermes state volume into /data/state did not finish; completing it (files already in /data/state are kept)." >&2
            stale_scratch="$(_recorded_scratch)"
            if [ -n "$stale_scratch" ]; then rm -rf -- "$stale_scratch"; fi
        fi
        scratch="$(mktemp -d "$state_home/.ph-migrating.XXXXXX")"
        printf '%s\n' "$scratch" > "$mark_file"
        _copy_missing "$legacy_home" "$state_home" "$scratch"
        rm -rf -- "$scratch"
        date -u +%Y-%m-%dT%H:%M:%SZ > "$done_file"
        rm -f "$mark_file"
        echo "Print Handy: /data/state is a mounted volume, so it cannot be replaced by a symlink to the legacy /data/hermes volume; the legacy volume's files that /data/state lacked were copied into /data/state instead (nothing was overwritten), and /data/hermes is left untouched (deprecated name; it stays accepted for at least two minor releases)." >&2
    else
        rm -rf "$state_home"
        ln -s "$legacy_home" "$state_home"
        echo "Print Handy: using the legacy /data/hermes state volume as /data/state (deprecated name; rename the host folder to 'state' when convenient — /data/hermes stays accepted for at least two minor releases)." >&2
    fi
elif _legacy_is_source && _has_files "$state_home"; then
    echo "Print Handy: WARNING both /data/state and the legacy /data/hermes state volumes hold data; using /data/state and ignoring /data/hermes." >&2
elif [ -e "$mark_file" ]; then
    # The mark is there but the legacy volume holds nothing to complete
    # the copy from (it was removed or unmapped between starts): say so
    # plainly instead of silently trusting a partial /data/state.
    echo "Print Handy: WARNING an earlier copy into /data/state did not finish, and the legacy /data/hermes volume now holds no files to complete it from; /data/state may be incomplete." >&2
elif [ "$legacy_home" = "$data_root/hermes" ] && [ -e "$state_home" ] && [ ! -e "$legacy_home" ] && [ ! -L "$legacy_home" ]; then
    # Only the new volume is mapped (Docker created it as an empty
    # mountpoint directory) and the legacy name does not exist at all (the
    # image does not create it): leave it as a symlink to the new volume so
    # tools that still use the pre-2.2.5 path keep working (#938). A legacy
    # path that exists as a real folder or mount is never replaced.
    ln -s "$state_home" "$legacy_home"
    echo "Print Handy: /data/hermes is a symlink to /data/state so the pre-2.2.5 name keeps working (deprecated name; it stays accepted for at least two minor releases)." >&2
fi
# Compose's print-handy-migrate service stops here: it exists only to run the
# migration above as the runtime user, before the application starts.
if [ -n "${PRINT_HANDY_STATE_MIGRATE_ONLY:-}" ]; then
    exit 0
fi
mkdir -p "$state_home/state" "$state_home/logs" "$data_root/archive" "$data_root/records" "$data_root/cache"
# Container timezone: TZ names an IANA zone (image default America/New_York).
# Consumers read TZ directly, so there is nothing to relink here — the
# entrypoint runs unprivileged and cannot rewrite /etc/localtime, which the
# image already points at the default zone. An unknown name falls back to the
# default with a log line instead of silently keeping whatever it resolved to.
tz="${TZ:-America/New_York}"
if [[ "$tz" == *[!A-Za-z0-9._+/-]* || "$tz" == */../* || "$tz" == ../* || ! -f "/usr/share/zoneinfo/$tz" ]]; then
    echo "TZ '$tz' is not a known IANA timezone; using America/New_York instead" >&2
    tz="America/New_York"
fi
export TZ="$tz"
channel="${PRINT_HANDY_ORCA_CHANNEL:-stable}"
# Print Handy ships ONE bundled OrcaSlicer (the stable build); there is no
# second channel to select. 'preview' was the second channel's name (and
# 'alpha' its name before the #600 rename); the preview channel was removed,
# so both old values still start on the single build, with a startup notice
# (the #600 alias precedent: a removed channel must not break an existing
# install's startup). Installs should drop the setting or set it to 'stable'.
if [ "$channel" = preview ] || [ "$channel" = alpha ]; then
    echo "PRINT_HANDY_ORCA_CHANNEL=$channel: the preview channel was removed - Print Handy now ships one bundled OrcaSlicer, so this install starts on the single (stable) build. The setting can be dropped, or set to PRINT_HANDY_ORCA_CHANNEL=stable." >&2
    channel=stable
fi
export PRINT_HANDY_ORCA_CHANNEL="$channel"
case "$channel" in
    stable)
        selected=/usr/local/bin/orca-slicer-stable
        ;;
    *)
        echo "PRINT_HANDY_ORCA_CHANNEL must be 'stable'; got '$channel' (the preview channel was removed - 'preview' and 'alpha' start on the single stable build)" >&2
        exit 2
        ;;
esac
: "${PRINT_HANDY_ORCA_CMD:=/usr/local/bin/orca-slicer-stable}"
export PRINT_HANDY_ORCA_CMD
# Thread pools follow a cgroup CPU quota (or PRINT_HANDY_THREADS) when one exists; with neither,
# nothing is exported and pools keep their defaults. A quota does not shrink the CPU count that
# BLAS and OpenMP see, so without this they size to every host CPU.
if limits="$(python3 /app/print_handy/core/resources.py --exports 2>/dev/null)"; then eval "$limits"; fi
# xvfb-run owns its display and cleanup; no fixed display or procps dependency.
if [[ ( -x "$selected" || -x /usr/bin/bambu-studio ) && -z "${DISPLAY:-}" ]]; then
    exec xvfb-run -a -s '-screen 0 1024x768x24' "$@"
fi
exec "$@"
