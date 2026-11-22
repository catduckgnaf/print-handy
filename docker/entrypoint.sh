#!/bin/bash
set -euo pipefail
# The main state volume is /data/state. Its pre-2.2.5 name, /data/hermes, is
# no longer read or migrated (#1817): an install whose state still sits only
# in the old folder refuses to start (below) instead of starting empty.
data_root="${HOME:-/data}"
state_home="$data_root/state"
legacy_home="$data_root/hermes"
# A folder "has files" when it holds anything that is not a directory: a
# regular file, a symlink (dangling or not), a socket. Empty directories are
# structure, not data, so a tree of only empty directories reads as empty.
# A leftover write probe (below) does not count. -H follows a symlink given
# as the folder itself but never a symlink inside it.
_has_files() {
    [ -d "$1" ] || return 1
    [ -n "$(find -H "$1" -mindepth 1 -path "$1/.ph-write-check.*" -prune -o ! -type d -print -quit 2>/dev/null)" ]
}
# Writable data folders (#1144). The runtime user (uid 1000 in the image) must
# be able to write every data folder the container uses; a folder it cannot
# write would otherwise fail later with a bare shell error under set -e, or
# leave a half-working app, and the restart policy would loop on it. Compose's
# print-handy-init service fixes ownership in the common case; this check
# catches what it cannot (a share that refuses chown, a read-only bind, a
# plain docker run or the Unraid template without the init service, rootless
# id mapping) and prints ONE message naming the first such folder and the
# command that fixes it, then stops. It runs before anything is created, so a
# refused start changes nothing. A folder that does not exist yet is skipped:
# its parent is checked first, and mkdir creates it.
# The probe is a temporary file made by mktemp and removed at once; it is the
# only reliable test on network shares whose mode bits do not tell the truth.
_can_write() {
    local probe
    probe="$(mktemp "$1/.ph-write-check.XXXXXX" 2>/dev/null)" || return 1
    rm -f -- "$probe"
}
_unwritable_exit() {
    local path="$1" uid gid host
    uid="$(id -u)"; gid="$(id -g)"
    case "$path" in
        "$data_root") host="the folder mapped to /data (Unraid: the Appdata Storage path). Compose maps only the folders inside /data, so there it means the container is not running as uid 1000, the user the image is built for" ;;
        "$data_root"/spoolman*) host="the folder mapped to /data/spoolman (Compose: \${PRINT_HANDY_SPOOLMAN_DATA:-./data/spoolman}; Unraid: spoolman inside the Appdata Storage path)" ;;
        *) host="the folder mapped to ${path/#"$data_root"//data} (Compose: inside \${PRINT_HANDY_DATA:-./data}; Unraid: inside the Appdata Storage path)" ;;
    esac
    {
        printf 'Print Handy: cannot start: %s is not writable by the container user (uid %s, gid %s).\n' "${path/#"$data_root"//data}" "$uid" "$gid"
        printf '  On the host this is %s.\n' "$host"
        printf '  Fix it on the host, then start the container again:\n'
        printf '    Docker Compose:  sudo chown -R %s:%s ./data   (your PRINT_HANDY_DATA folder, and PRINT_HANDY_SPOOLMAN_DATA if set)\n' "$uid" "$gid"
        printf '    Unraid:          chown -R %s:%s /mnt/user/appdata/print-handy   (your Appdata Storage path)\n' "$uid" "$gid"
        printf '    Rootless Podman: podman unshare chown -R %s:%s <your data folder>\n' "$uid" "$gid"
        printf '  With Compose, the print-handy-init service sets the owner of each top-level data folder on every up. If this still\n'
        printf '  appears, a folder inside is owned by another user (the chown above fixes that), the folder is on an NFS or SMB share\n'
        printf '  that ignores ownership (set the owner, or the uid/gid mount option, on the server or the mount), or it is mapped read-only (:ro).\n'
        printf '  Nothing was changed; the container stops here instead of running without its data.\n'
    } >&2
    exit 73
}
_check_data_folders() {
    local folder
    local folders=("$data_root" "$state_home" "$state_home/state"
                   "$data_root/archive" "$data_root/records" "$data_root/cache")
    if [ "${PRINT_HANDY_BUNDLED_SPOOLMAN:-true}" = true ]; then
        folders+=("$data_root/spoolman" "$data_root/spoolman/config"
                  "$data_root/spoolman/logs" "$data_root/spoolman/backups")
    fi
    for folder in "${folders[@]}"; do
        [ -d "$folder" ] || continue
        _can_write "$folder" || _unwritable_exit "$folder"
    done
}
_check_data_folders
# Old state folder (#1817): a pre-2.2.5 install kept its state in
# /data/hermes. That folder is no longer read, so when it holds files and
# /data/state holds none (or /data/state is an old compat symlink to it),
# starting would show an empty install. Stop with one line instead; nothing is
# changed. A /data/hermes that is itself a symlink (the old compat link to
# /data/state) is not data and is ignored.
if [ ! -L "$legacy_home" ] && _has_files "$legacy_home" \
        && { [ "$(readlink -f "$state_home")" = "$(readlink -f "$legacy_home")" ] \
             || ! _has_files "$state_home"; }; then
    echo "Print Handy: cannot start: your state is still in the old folder /data/hermes and /data/state is empty; stop the container, delete the empty host folder state (or the state symlink), rename the host folder hermes to state (Compose: ./data/hermes; Unraid: hermes in the Appdata Storage path), then start again." >&2
    exit 78
fi
mkdir -p "$state_home/state" "$data_root/archive" "$data_root/records" "$data_root/cache"
# The log folder is best effort (#1416): file logging falls back to stderr, so
# a log folder that cannot be created must never stop the container starting.
mkdir -p "$state_home/logs" 2>/dev/null \
    || echo "Print Handy: cannot create $state_home/logs; file logging falls back to stderr" >&2
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
# Print Handy ships ONE bundled OrcaSlicer, and 'stable' is the only channel.
# The removed preview channel's names ('preview', 'alpha') are refused like
# any other value (#1817).
channel="${PRINT_HANDY_ORCA_CHANNEL:-stable}"
export PRINT_HANDY_ORCA_CHANNEL="$channel"
case "$channel" in
    stable)
        selected=/usr/local/bin/orca-slicer-stable
        ;;
    *)
        echo "PRINT_HANDY_ORCA_CHANNEL must be 'stable'; got '$channel' (Print Handy ships one bundled OrcaSlicer; drop the setting or set it to stable)" >&2
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
