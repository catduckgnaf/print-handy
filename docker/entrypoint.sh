#!/bin/bash
set -euo pipefail
# State volume (#914): the main state volume's neutral name is /data/state;
# /data/hermes is its pre-2.2.5 name, kept as a legacy fallback for at
# least two minor releases. The entrypoint runs before any service, so the
# backward-compatible migration lives here: when the legacy volume holds
# files and the new one holds none, the new path becomes a symlink to the
# legacy folder and every service keeps reading the existing data through
# /data/state. Nothing is moved or copied — except when /data/state is
# itself a mounted volume (the shipped two-bind-mount layout): a
# mountpoint cannot be removed or replaced by a symlink, so the legacy
# contents are copied into the mounted volume instead and the legacy
# volume is left untouched. An install that only maps the new path never
# sees this, and an explicit paths.hermes_home pointing at /data/hermes
# is honored unchanged (no symlink is needed then).
data_root="${HOME:-/data}"
state_home="$data_root/state"
legacy_home="$data_root/hermes"
_has_files() { [ -d "$1" ] && [ -n "$(find "$1" -type f -print -quit 2>/dev/null)" ]; }
_is_mountpoint() {
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
if [ -L "$state_home" ]; then
    echo "Print Handy: state volume is /data/state, a symlink to $(readlink "$state_home") — the legacy /data/hermes volume is in use (deprecated name; it stays accepted for at least two minor releases)." >&2
elif _has_files "$legacy_home" && { ! _has_files "$state_home" || [ -e "$state_home/.migration-incomplete" ]; }; then
    if _is_mountpoint "$state_home"; then
        # rm -rf on a mountpoint fails EBUSY and would stop the container
        # before any service starts, so the mounted volume is populated
        # from the legacy one instead of being replaced by a symlink.
        # Later starts see both volumes holding data and keep using
        # /data/state (the warning branch below). Ownership is not
        # preserved: the copy runs as the volume's runtime user, and a
        # filesystem that refuses chown must not stop the copy either.
        # The copy is marked in progress inside the state volume until
        # it completes: a start that finds the mark re-copies, so a copy
        # interrupted partway (full disk, killed container) never reads
        # as a finished migration on the next start.
        if [ -e "$state_home/.migration-incomplete" ]; then
            echo "Print Handy: an earlier copy of the legacy /data/hermes state volume into the mounted /data/state volume did not finish; copying it again to complete the migration." >&2
        fi
        touch "$state_home/.migration-incomplete"
        cp -a --no-preserve=ownership "$legacy_home"/. "$state_home"/
        rm -f "$state_home/.migration-incomplete"
        echo "Print Handy: /data/state is a mounted volume, so it cannot be replaced by a symlink to the legacy /data/hermes volume; the legacy volume's contents were copied into /data/state instead, and /data/hermes is left untouched (deprecated name; it stays accepted for at least two minor releases)." >&2
    else
        rm -rf "$state_home"
        ln -s "$legacy_home" "$state_home"
        echo "Print Handy: using the legacy /data/hermes state volume as /data/state (deprecated name; rename the host folder to 'state' when convenient — /data/hermes stays accepted for at least two minor releases)." >&2
    fi
elif _has_files "$legacy_home" && _has_files "$state_home"; then
    echo "Print Handy: WARNING both /data/state and the legacy /data/hermes state volumes hold data; using /data/state and ignoring /data/hermes." >&2
elif [ -e "$state_home/.migration-incomplete" ]; then
    # The mark is there but the legacy volume holds nothing to complete
    # the copy from (it was removed or unmapped between starts): say so
    # plainly instead of silently trusting a partial /data/state.
    echo "Print Handy: WARNING an earlier copy into /data/state did not finish, and the legacy /data/hermes volume now holds no files to complete it from; /data/state may be incomplete." >&2
elif [ -e "$state_home" ] && [ ! -e "$legacy_home" ] && [ ! -L "$legacy_home" ]; then
    # Only the new volume is mapped (Docker created it as an empty
    # mountpoint directory) and the legacy name does not exist at all:
    # leave it as a symlink to the new volume so tools that still use the
    # pre-2.2.5 path keep working (#938). A legacy path that exists as a
    # real folder or mount is never replaced.
    ln -s "$state_home" "$legacy_home"
    echo "Print Handy: /data/hermes is a symlink to /data/state so the pre-2.2.5 name keeps working (deprecated name; it stays accepted for at least two minor releases)." >&2
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
export PRINT_HANDY_ORCA_CMD="$selected"
# Thread pools follow a cgroup CPU quota (or PRINT_HANDY_THREADS) when one exists; with neither,
# nothing is exported and pools keep their defaults. A quota does not shrink the CPU count that
# BLAS and OpenMP see, so without this they size to every host CPU.
if limits="$(python3 /app/print_handy/core/resources.py --exports 2>/dev/null)"; then eval "$limits"; fi
# xvfb-run owns its display and cleanup; no fixed display or procps dependency.
if [[ ( -x "$selected" || -x /usr/bin/bambu-studio ) && -z "${DISPLAY:-}" ]]; then
    exec xvfb-run -a -s '-screen 0 1024x768x24' "$@"
fi
exec "$@"
