#!/usr/bin/env bash
# Install the Print Handy Hermes profile by symlink. Safe to run again.
#
#   scripts/install-hermes.sh [--dry-run] [--hermes-home DIR] [--python PATH]
#
# Env: PYTHON, SYSTEMD_USER_DIR (default ~/.config/systemd/user),
#      PRINT_HANDY_OVERLAY (default config/overlay), NO_SYSTEMCTL=1 to skip daemon-reload.
#      Template values: LLM_PROVIDER LLM_MODEL LLM_BASE_URL TIMEZONE DISCORD_CHANNEL_ID DISPLAY_NAME BOT_NAME
#      (BOT_NAME defaults to [discord] bot_name in config/home.yaml).
#
# Code folders are symlinked to this checkout. Templates are rendered only when the target is
# missing, so your edited AGENTS.md, SOUL.md and config.yaml are never overwritten. Files under
# config/overlay/ are then copied over the profile. Anything replaced is moved to
# <hermes home>/.install-backups/<time>/<relative path> (units: <systemd dir>/.install-backups/<time>/...),
# never next to the original, so Hermes never discovers a backup as a second plugin or skill.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
DRY=0
# Not read from the environment: the Hermes gateway exports HERMES_HOME=~/.hermes for every profile.
# Use --hermes-home, PRINT_HANDY__PATHS__HERMES_HOME or [paths] hermes_home.
HERMES_HOME=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --hermes-home) shift; HERMES_HOME=${1:?--hermes-home needs a directory} ;;
        --python) shift; PYTHON=${1:?--python needs a path} ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "${HERMES_HOME:-}" ]; then
    HERMES_HOME=$(cd "$REPO" && python3 -m print_handy.settings get paths.hermes_home 2>/dev/null) || HERMES_HOME=""
    [ -n "$HERMES_HOME" ] || HERMES_HOME="$HOME/.hermes/profiles/3dprinting"
fi
if [ -z "${BOT_NAME:-}" ]; then
    BOT_NAME=$(cd "$REPO" && python3 -m print_handy.settings get discord.bot_name 2>/dev/null) || BOT_NAME=""
    [ -n "$BOT_NAME" ] || BOT_NAME="Print Handy"
fi
if [ -z "${PYTHON:-}" ]; then
    if [ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]; then
        PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
    else
        PYTHON=$(command -v python3)
    fi
fi
SYSTEMD_USER_DIR=${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}
OVERLAY=${PRINT_HANDY_OVERLAY:-$REPO/config/overlay}
STAMP=$(date +%Y%m%d-%H%M%S)
LAST_CHANGED=0

say() { printf '%s\n' "$*"; }
act() { if [ "$DRY" = 1 ]; then say "would: $*"; else say "$*"; fi; }

backup_path() {  # print a free backup name under <root>/.install-backups/<stamp>/<relative path>
    local p="$1" root rel b n=1
    case "$p" in
        "$HERMES_HOME"/*) root=$HERMES_HOME ;;
        "$SYSTEMD_USER_DIR"/*) root=$SYSTEMD_USER_DIR ;;
        *) root=$(dirname "$p") ;;
    esac
    rel=${p#"$root"/}
    [ "$root" != "$(dirname "$p")" ] || rel=$(basename "$p")
    b="$root/.install-backups/$STAMP/$rel"
    while [ -e "$b" ] || [ -L "$b" ]; do b="$root/.install-backups/$STAMP/$rel.$n"; n=$((n + 1)); done
    printf '%s' "$b"
}

backup() {  # move a path aside
    local b; b=$(backup_path "$1")
    act "backup $1 -> $b"
    [ "$DRY" = 1 ] || { mkdir -p "$(dirname "$b")" && mv "$1" "$b"; }
}

ensure_dir() {
    [ -d "$1" ] && return 0
    act "mkdir $1"
    [ "$DRY" = 1 ] || mkdir -p "$1"
}

link() {  # link SRC DST: DST becomes a symlink to SRC
    local src="$1" dst="$2"
    if [ -L "$dst" ]; then
        if [ "$(readlink "$dst")" = "$src" ]; then say "ok      $dst -> $src"; return 0; fi
        act "relink $dst -> $src"
        [ "$DRY" = 1 ] || ln -sfn "$src" "$dst"
        return 0
    fi
    ensure_dir "$(dirname "$dst")"
    if [ -e "$dst" ]; then backup "$dst"; fi
    act "link   $dst -> $src"
    [ "$DRY" = 1 ] || ln -s "$src" "$dst"
}

render() {  # render SRC DST: substitute ${NAME} and {{NAME}} placeholders
    LLM_PROVIDER=${LLM_PROVIDER:-} LLM_MODEL=${LLM_MODEL:-} LLM_BASE_URL=${LLM_BASE_URL:-} \
    DISCORD_CHANNEL_ID=${DISCORD_CHANNEL_ID:-} \
    TIMEZONE=${TIMEZONE:-${TZ:-UTC}} DISPLAY_NAME=${DISPLAY_NAME:-3D Printing} BOT_NAME="$BOT_NAME" \
    PROFILE_DIR="$HERMES_HOME" HERMES_HOME="$HERMES_HOME" REPO="$REPO" PYTHON="$PYTHON" \
    python3 - "$1" <<'PY'
import json, os, re, sys
text = open(sys.argv[1]).read()
yaml = sys.argv[1].endswith((".yaml", ".yml", ".yaml.template", ".yml.template"))
def fill(match):
    name = match.group(1) or match.group(2)
    value = os.environ.get(name)
    if not value:
        return match.group(0)
    # Free text in YAML sits inside double quotes; JSON string escaping is valid YAML escaping.
    return json.dumps(value)[1:-1] if yaml and name == "BOT_NAME" else value
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\{\{(\w+)\}\}", fill, text))
PY
}

write_rendered() {  # write_rendered SRC DST MODE: MODE=keep leaves an existing DST alone, MODE=sync replaces it
    local src="$1" dst="$2" mode="$3" tmp
    tmp=$(mktemp)
    render "$src" > "$tmp"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        if [ "$mode" = keep ]; then say "skipped $dst (exists, left as is)"; rm -f "$tmp"; return 0; fi
        if [ ! -L "$dst" ] && cmp -s "$tmp" "$dst"; then say "ok      $dst"; rm -f "$tmp"; return 0; fi
        backup "$dst"
    fi
    ensure_dir "$(dirname "$dst")"
    act "render $dst"
    LAST_CHANGED=1
    [ "$DRY" = 1 ] || cp "$tmp" "$dst"
    unfilled=$(grep -oE '\$\{[A-Z_]+\}|\{\{[A-Z_]+\}\}' "$tmp" 2>/dev/null | sort -u | tr '\n' ' ' || true)
    [ -z "$unfilled" ] || say "note    $dst has unfilled placeholders: $unfilled(set them in the environment or in config/overlay)"
    rm -f "$tmp"
}

copy_if_missing() {  # copy_if_missing SRC DST
    if [ -e "$2" ] || [ -L "$2" ]; then say "skipped $2 (exists, left as is)"; return 0; fi
    ensure_dir "$(dirname "$2")"
    act "copy   $2"
    [ "$DRY" = 1 ] || cp "$1" "$2"
}

copy_over() {  # copy_over SRC DST: overlay copy, replaces a different file after backing it up
    local src="$1" dst="$2"
    if [ -f "$dst" ] && [ ! -L "$dst" ] && cmp -s "$src" "$dst"; then say "ok      $dst (overlay)"; return 0; fi
    ensure_dir "$(dirname "$dst")"
    if [ -e "$dst" ] || [ -L "$dst" ]; then backup "$dst"; fi
    act "overlay $dst"
    LAST_CHANGED=1
    [ "$DRY" = 1 ] || cp "$src" "$dst"
}

say "repo:        $REPO"
say "hermes home: $HERMES_HOME"
say "python:      $PYTHON"
[ "$DRY" = 1 ] && say "(dry run: nothing will be changed)"

# 1. Profile directory and code symlinks.
ensure_dir "$HERMES_HOME"
link "$REPO/hermes/skills/klipper-3d-printing" "$HERMES_HOME/skills/smart-home/klipper-3d-printing"
link "$REPO/hermes/skills/ad5x-multicolor-slicing" "$HERMES_HOME/skills/smart-home/ad5x-multicolor-slicing"
link "$REPO/plugins/print-feedback" "$HERMES_HOME/plugins/print-feedback"
link "$REPO/hermes/profile/docs" "$HERMES_HOME/docs"

# 1b. Make `python -m print_handy` importable from any cwd for the Hermes interpreter only.
purelib=$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null) || purelib=""
if [ -z "$purelib" ] || [ ! -d "$purelib" ]; then
    say "warning: cannot find site-packages for $PYTHON; skipped print_handy.pth (run the agent from $REPO instead)"
elif [ -f "$purelib/print_handy.pth" ] && [ "$(cat "$purelib/print_handy.pth")" = "$REPO" ]; then
    say "ok      $purelib/print_handy.pth"
else
    act "write  $purelib/print_handy.pth -> $REPO"
    [ "$DRY" = 1 ] || printf '%s\n' "$REPO" > "$purelib/print_handy.pth" || say "warning: could not write print_handy.pth"
fi

# 1c. Old private layout: stale scripts dir and state files kept in $HOME.
if [ -d "$HERMES_HOME/scripts" ] && [ ! -L "$HERMES_HOME/scripts" ] \
        && [ -e "$HERMES_HOME/scripts/moonraker_event_listener.py" ]; then
    say "note    old private scripts/ dir found in the profile; moving it out of the way"
    backup "$HERMES_HOME/scripts"
fi
for pair in .print_bitch_context.json:print_handy_context.json \
            .print_bitch_start_claims.json:print_handy_start_claims.json \
            .moonraker_discord_announcements.json:moonraker_discord_announcements.json; do
    old="$HOME/${pair%%:*}"; new="$HERMES_HOME/state/${pair##*:}"
    [ -f "$old" ] || continue
    if [ -e "$new" ]; then say "skipped $new (exists, legacy $old left as is)"; continue; fi
    ensure_dir "$HERMES_HOME/state"
    act "copy   $old -> $new"
    [ "$DRY" = 1 ] || cp "$old" "$new"
done

# 2. Templates: rendered or copied only when the target is missing.
P="$REPO/hermes/profile"
write_rendered "$P/config.yaml.template" "$HERMES_HOME/config.yaml" keep
write_rendered "$P/profile.yaml.template" "$HERMES_HOME/profile.yaml" keep
write_rendered "$P/SOUL.md" "$HERMES_HOME/SOUL.md" keep
write_rendered "$P/locales/en.yaml" "$HERMES_HOME/locales/en.yaml" keep
for f in AGENTS.md MEMORY.md status_phrases.yaml cron/jobs.json; do
    copy_if_missing "$P/$f" "$HERMES_HOME/$f"
done

# 3. Local overlay (gitignored): files replace the profile files at the same relative path.
overlay_units=""
if [ -d "$OVERLAY" ]; then
    while IFS= read -r f; do
        rel=${f#"$OVERLAY"/}
        case "$rel" in
            README.md|.gitignore) continue ;;
            systemd/*) overlay_units="$overlay_units $rel"; continue ;;
        esac
        copy_over "$f" "$HERMES_HOME/$rel"
    done < <(find "$OVERLAY" -type f | sort)
else
    say "no overlay folder at $OVERLAY"
fi

# 4. systemd user units. Managed files are re-rendered when they differ.
LAST_CHANGED=0
for unit in "$REPO"/hermes/systemd/*.service.template; do
    [ -e "$unit" ] || continue
    name=$(basename "$unit" .template)
    dst="$SYSTEMD_USER_DIR/$name"
    write_rendered "$unit" "$dst" sync
    for dropin in "$REPO/hermes/systemd/$name.d/"*.conf.template; do
        [ -e "$dropin" ] || continue
        write_rendered "$dropin" "$SYSTEMD_USER_DIR/$name.d/$(basename "$dropin" .template)" sync
    done
done
# Overlay drop-ins: config/overlay/systemd/<unit>.service.d/*.conf, for personal Environment= lines.
for rel in $overlay_units; do
    copy_over "$OVERLAY/$rel" "$SYSTEMD_USER_DIR/${rel#systemd/}"
done

if [ "$LAST_CHANGED" = 1 ] && [ -z "${NO_SYSTEMCTL:-}" ] && command -v systemctl >/dev/null 2>&1; then
    act "systemctl --user daemon-reload"
    [ "$DRY" = 1 ] || systemctl --user daemon-reload || say "warning: daemon-reload failed"
fi

# 5. Remove Print Handy state files stranded outside the profile state folder (only when the
# profile holds the real copy, or the stray is empty).
if [ "$DRY" = 1 ]; then strays_flag="--dry-run --remove"; else strays_flag="--remove"; fi
# shellcheck disable=SC2086
PRINT_HANDY__PATHS__HERMES_HOME="$HERMES_HOME" PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m print_handy.state_files $strays_flag || say "warning: could not check for stray state files"

# 6. Provision the default printer's hardware record when the profile has none (P1-7):
# every pipeline command reads state/printer_hardware.json, and a fresh profile cannot
# create it by hand — `printer_state set` loads before it saves. Never overwrites an
# existing record; the operator records the real hardware afterwards with printer_state set.
if [ "$DRY" = 1 ]; then
    act "provision $HERMES_HOME/state/printer_hardware.json if missing"
else
    PRINT_HANDY__PATHS__HERMES_HOME="$HERMES_HOME" PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" -m print_handy.core.printer_state provision || say "warning: could not provision the printer hardware record"
fi

say "done. Enable the listener with: systemctl --user enable --now hermes-3dprinting-moonraker-listener.service"
