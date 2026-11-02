#!/usr/bin/env bash
# One-shot rename migration for an install made before the Print Buddy -> Print Handy rename.
# Anything still carrying the older Print Bitch names is chained straight to Print Handy too.
# Safe to run again; every step prints what it does.
#
#   scripts/migrate-names.sh [--dry-run] [--hermes-home DIR] [--archive DIR]
#                            [--systemd-dir DIR] [--config FILE]
#
# 1. Renames files and folders whose name contains print_buddy (or print_bitch) to print_handy
#    in the profile state dir and in the archive dir. A target that already exists is skipped.
# 2. Replaces PRINT_BUDDY_ / PRINT_BITCH_ with PRINT_HANDY_ in the selected config/local.yaml or
#    config/local.toml, the profile .env and config.yaml, and the listener systemd unit and
#    drop-ins. The leftover short-prefix PB_ORCA_CMD becomes PRINT_HANDY_ORCA_CMD in the same
#    pass. Each edited file is first copied to <file>.pre-rename.
# 3. Rewrites [printer] family = "flashforge_zmod" (or generic_klipper, sovol_klipper) in
#    config/local.yaml or config/local.toml to model = "flashforge.ad5x" (klipper.generic,
#    sovol.sv06). The file is first copied to <file>.pre-model.
# 4. Prints the steps that have to be done by hand on the printer.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
DRY=0
# Not read from the environment: the Hermes gateway exports HERMES_HOME=~/.hermes for every profile.
HERMES_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --hermes-home) shift; HERMES_HOME=${1:?--hermes-home needs a directory} ;;
        --archive) shift; ARCHIVE=${1:?--archive needs a directory} ;;
        --systemd-dir) shift; SYSTEMD_USER_DIR=${1:?--systemd-dir needs a directory} ;;
        --config) shift; LOCAL_CONFIG=${1:?--config needs a file} ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

setting() {  # setting SECTION.KEY
    (cd "$REPO" && python3 -m print_handy.settings get "$1" 2>/dev/null) || true
}
HERMES_HOME=${HERMES_HOME:-$(setting paths.hermes_home)}
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes/profiles/3dprinting}
ARCHIVE=${ARCHIVE:-$(setting paths.archive)}
SYSTEMD_USER_DIR=${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}
LOCAL_CONFIG=${LOCAL_CONFIG:-${PRINT_HANDY_CONFIG:-$REPO/config/local.yaml}}

say() { printf '%s\n' "$*"; }
act() { if [ "$DRY" = 1 ]; then say "would: $*"; else say "$*"; fi; }

say "profile:  $HERMES_HOME"
say "archive:  ${ARCHIVE:-(not set)}"
say "config:   $LOCAL_CONFIG"
say "systemd:  $SYSTEMD_USER_DIR"
[ "$DRY" = 1 ] && say "(dry run: nothing will be changed)"

# 1. Rename state and archive entries, deepest first so a folder is renamed after its contents.
rename_tree() {  # rename_tree DIR FROM TO
    local root="$1" from="$2" to="$3" path dir base new found=0
    if [ -z "$root" ] || [ ! -d "$root" ]; then say "skipped $root (no such directory)"; return 0; fi
    while IFS= read -r -d '' path; do
        found=1
        dir=$(dirname "$path"); base=$(basename "$path"); new="$dir/${base//$from/$to}"
        if [ -e "$new" ] || [ -L "$new" ]; then say "skipped $path (target $new exists)"; continue; fi
        act "rename $path -> $new"
        [ "$DRY" = 1 ] || mv "$path" "$new"
    done < <(find "$root" -mindepth 1 -depth -name "*${from}*" -print0)  # never the root itself
    [ "$found" = 1 ] || say "ok      $root (nothing to rename)"
}
rename_tree "$HERMES_HOME/state" print_bitch print_handy
rename_tree "$HERMES_HOME/state" print_buddy print_handy
rename_tree "$ARCHIVE" print_bitch print_handy
rename_tree "$ARCHIVE" print_buddy print_handy

# 2. Environment variable names inside config files and units.
edit_file() {  # edit_file FILE
    local f="$1"
    if [ ! -f "$f" ]; then say "skipped $f (not found)"; return 0; fi
    local old_names=0 orca_names=0
    grep -qE 'PRINT_(BUDDY|BITCH)_' "$f" && old_names=1
    grep -qE '\bPB_ORCA_CMD\b' "$f" && orca_names=1
    if [ "$old_names" = 0 ] && [ "$orca_names" = 0 ]; then say "ok      $f (no old names)"; return 0; fi
    if [ -e "$f.pre-rename" ]; then
        # The existing backup already preserves the pre-rename original, so
        # it is never overwritten. PRINT_BUDDY_/PRINT_BITCH_ leftovers stay
        # a by-hand check, as before — but PB_ORCA_CMD still migrates
        # (P2-194): a file an earlier rename pass already migrated can
        # carry it, and the current code no longer reads the stale name.
        if [ "$old_names" = 1 ]; then
            say "skipped $f: PRINT_BUDDY_/PRINT_BITCH_ pass (backup $f.pre-rename already exists; check it by hand)"
        fi
        if [ "$orca_names" = 1 ]; then
            act "edit   $f: PB_ORCA_CMD -> PRINT_HANDY_ORCA_CMD ($(grep -cE '\bPB_ORCA_CMD\b' "$f") lines)"
            [ "$DRY" = 1 ] || sed -i 's/\bPB_ORCA_CMD\b/PRINT_HANDY_ORCA_CMD/g' "$f"
        fi
        return 0
    fi
    act "backup $f -> $f.pre-rename"
    [ "$DRY" = 1 ] || cp -p "$f" "$f.pre-rename"
    if [ "$old_names" = 1 ]; then
        act "edit   $f: PRINT_BUDDY_/PRINT_BITCH_ -> PRINT_HANDY_ ($(grep -cE 'PRINT_(BUDDY|BITCH)_' "$f") lines)"
        [ "$DRY" = 1 ] || sed -i 's/PRINT_BUDDY_/PRINT_HANDY_/g; s/PRINT_BITCH_/PRINT_HANDY_/g' "$f"
    fi
    if [ "$orca_names" = 1 ]; then
        act "edit   $f: PB_ORCA_CMD -> PRINT_HANDY_ORCA_CMD ($(grep -cE '\bPB_ORCA_CMD\b' "$f") lines)"
        [ "$DRY" = 1 ] || sed -i 's/\bPB_ORCA_CMD\b/PRINT_HANDY_ORCA_CMD/g' "$f"
    fi
}
edit_file "$LOCAL_CONFIG"
edit_file "$HERMES_HOME/.env"
edit_file "$HERMES_HOME/config.yaml"
for f in "$SYSTEMD_USER_DIR"/hermes-3dprinting-moonraker-listener.service \
         "$SYSTEMD_USER_DIR"/hermes-3dprinting-moonraker-listener.service.d/*.conf; do
    case "$f" in *'*'*) continue ;; esac
    edit_file "$f"
done

# 3. [printer] family became model.
FAMILY_RE='^[[:space:]]*family[[:space:]]*=[[:space:]]*"(flashforge_zmod|generic_klipper|sovol_klipper)"'
if [ -f "$LOCAL_CONFIG" ] && grep -Eq "$FAMILY_RE" "$LOCAL_CONFIG"; then
    if [ -e "$LOCAL_CONFIG.pre-model" ]; then
        say "skipped $LOCAL_CONFIG (backup $LOCAL_CONFIG.pre-model already exists; set [printer] model by hand)"
    else
        act "backup $LOCAL_CONFIG -> $LOCAL_CONFIG.pre-model"
        [ "$DRY" = 1 ] || cp -p "$LOCAL_CONFIG" "$LOCAL_CONFIG.pre-model"
        act "edit   $LOCAL_CONFIG: [printer] family -> model"
        [ "$DRY" = 1 ] || sed -E -i \
            -e 's/^([[:space:]]*)family[[:space:]]*=[[:space:]]*"flashforge_zmod"/\1model = "flashforge.ad5x"/' \
            -e 's/^([[:space:]]*)family[[:space:]]*=[[:space:]]*"generic_klipper"/\1model = "klipper.generic"/' \
            -e 's/^([[:space:]]*)family[[:space:]]*=[[:space:]]*"sovol_klipper"/\1model = "sovol.sv06"/' "$LOCAL_CONFIG"
    fi
else
    say "ok      $LOCAL_CONFIG (no [printer] family)"
fi

# 4. Printer side.
cat <<'TXT'

Printer steps (cannot be done from here; do them while the printer is idle). Details in
docs/RENAME-MIGRATION.md.
  1. Printer console: DISABLE_PLUGIN name=print-buddy
  2. In mod_data/user.moonraker.conf replace [update_manager print-buddy] with
     [update_manager print-handy], path .../mod_data/plugins/print-handy and origin
     https://github.com/catduckgnaf/print-handy.git. Restart Moonraker so it
     clones the plugin, then delete mod_data/plugins/print-buddy.
  3. Printer console: ENABLE_PLUGIN name=print-handy
  4. In mod_data/user.cfg rename [gcode_macro _PRINT_BUDDY_VARS] to [gcode_macro _PRINT_HANDY_VARS],
     and any PRINT_BUDDY macro calls to PRINT_HANDY.
  5. FIRMWARE_RESTART, then run PRINT_HANDY SLOT=1 to check.
Then on this machine, restart the listener:
  systemctl --user daemon-reload && systemctl --user restart hermes-3dprinting-moonraker-listener.service
TXT
