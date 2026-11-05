#!/bin/bash
set -euo pipefail
mkdir -p "${HOME:-/data}/hermes/state" "${HOME:-/data}/hermes/logs" "${HOME:-/data}/archive" "${HOME:-/data}/records" "${HOME:-/data}/cache"
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
