#!/bin/bash
set -euo pipefail
# State volume (#914): the main state volume's neutral name is /data/state;
# /data/hermes is its pre-2.2.5 name, kept as a legacy fallback for at
# least two minor releases. The entrypoint runs before any service, so the
# backward-compatible migration lives here: when the legacy volume holds
# files and the new one holds none, the new path becomes a symlink to the
# legacy folder and every service keeps reading the existing data through
# /data/state. Nothing is moved or copied; an install that only maps the
# new path never sees this, and an explicit paths.hermes_home pointing at
# /data/hermes is honored unchanged (no symlink is needed then).
data_root="${HOME:-/data}"
state_home="$data_root/state"
legacy_home="$data_root/hermes"
_has_files() { [ -d "$1" ] && [ -n "$(find "$1" -type f -print -quit 2>/dev/null)" ]; }
if [ -L "$state_home" ]; then
    echo "Print Handy: state volume is /data/state, a symlink to $(readlink "$state_home") — the legacy /data/hermes volume is in use (deprecated name; it stays accepted for at least two minor releases)." >&2
elif _has_files "$legacy_home" && ! _has_files "$state_home"; then
    rm -rf "$state_home"
    ln -s "$legacy_home" "$state_home"
    echo "Print Handy: using the legacy /data/hermes state volume as /data/state (deprecated name; rename the host folder to 'state' when convenient — /data/hermes stays accepted for at least two minor releases)." >&2
elif _has_files "$legacy_home" && _has_files "$state_home"; then
    echo "Print Handy: WARNING both /data/state and the legacy /data/hermes state volumes hold data; using /data/state and ignoring /data/hermes." >&2
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
# 'alpha' was the preview channel's name until #600 renamed it to describe the
# role instead of the upstream stage. It stays an accepted alias for at least
# one release, with a startup notice; installs should switch to 'preview'.
if [ "$channel" = alpha ]; then
    echo "PRINT_HANDY_ORCA_CHANNEL=alpha is the old name for the 'preview' channel; it still works in this release but will be removed in a later one - set PRINT_HANDY_ORCA_CHANNEL=preview instead." >&2
    channel=preview
fi
export PRINT_HANDY_ORCA_CHANNEL="$channel"
case "$channel" in
    stable)
        selected=/usr/local/bin/orca-slicer-stable
        ;;
    preview)
        selected=/usr/local/bin/orca-slicer-preview
        ;;
    *)
        echo "PRINT_HANDY_ORCA_CHANNEL must be 'stable' or 'preview'; got '$channel'" >&2
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
