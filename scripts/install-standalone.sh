#!/usr/bin/env bash
# Install Print Handy as a standalone Discord bot: no Hermes, no LLM. Safe to run again.
#
#   scripts/install-standalone.sh [--dry-run] [--python PATH]
#
# Writes two systemd user units, print-handy-bot.service and print-handy-listener.service, and the
# data folder [paths] hermes_home (state, logs, the .env holding DISCORD_BOT_TOKEN). It enables nothing:
# the last lines say what to run. Env: PYTHON, SYSTEMD_USER_DIR (default ~/.config/systemd/user),
# NO_SYSTEMCTL=1 to skip daemon-reload. Use one bot token for one mode: stop the Hermes gateway
# for this bot before starting the standalone one.
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --python) shift; PYTHON=${1:?--python needs a path} ;;
        -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

PYTHON=${PYTHON:-$(command -v python3)}
SYSTEMD_USER_DIR=${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}
DATA_HOME=$(cd "$REPO" && "$PYTHON" -m print_handy.settings get paths.hermes_home)
STAMP=$(date +%Y%m%d-%H%M%S)

say() { printf '%s\n' "$*"; }
act() { if [ "$DRY" = 1 ]; then say "would: $*"; else say "$*"; fi; }

for dir in "$DATA_HOME" "$DATA_HOME/state" "$DATA_HOME/logs" "$SYSTEMD_USER_DIR"; do
    [ -d "$dir" ] && continue
    act "mkdir $dir"
    [ "$DRY" = 1 ] || mkdir -p "$dir"
done
if [ ! -e "$DATA_HOME/.env" ]; then
    act "create $DATA_HOME/.env (mode 600): add DISCORD_BOT_TOKEN=..."
    [ "$DRY" = 1 ] || (umask 077; printf '# Print Handy standalone secrets\nDISCORD_BOT_TOKEN=\n' > "$DATA_HOME/.env")
fi

# Provision the default printer's hardware record when the install has none (P1-7):
# every pipeline command reads state/printer_hardware.json, and a fresh install cannot
# create it by hand — `printer_state set` loads before it saves. Never overwrites an
# existing record; record the real hardware afterwards with printer_state set.
if [ "$DRY" = 1 ]; then
    act "provision $DATA_HOME/state/printer_hardware.json if missing"
elif ! PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m print_handy.core.printer_state provision; then
    say "warning: could not provision the printer hardware record"
fi

for name in print-handy-bot print-handy-listener; do
    target="$SYSTEMD_USER_DIR/$name.service"
    rendered=$(sed -e "s|{{REPO}}|$REPO|g" -e "s|{{PYTHON}}|$PYTHON|g" -e "s|{{DATA_HOME}}|$DATA_HOME|g" \
        "$REPO/systemd/$name.service.template")
    if [ -f "$target" ] && [ "$(cat "$target")" = "$rendered" ]; then
        say "unchanged $target"
        continue
    fi
    if [ -e "$target" ]; then
        backup="$SYSTEMD_USER_DIR/.install-backups/$STAMP/$name.service"
        act "backup $target -> $backup"
        [ "$DRY" = 1 ] || { mkdir -p "$(dirname "$backup")" && mv "$target" "$backup"; }
    fi
    act "write $target"
    [ "$DRY" = 1 ] || printf '%s\n' "$rendered" > "$target"
done

if [ "${NO_SYSTEMCTL:-0}" != 1 ] && [ "$DRY" = 0 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || say "systemctl --user daemon-reload failed; run it yourself"
fi
for unit in hermes-gateway.service hermes-3dprinting-moonraker-listener.service; do
    if [ -e "$SYSTEMD_USER_DIR/$unit" ]; then
        say "NOTE: $unit is installed. If it uses the same bot token, stop and disable it first."
    fi
done
say "Next: set DISCORD_BOT_TOKEN in $DATA_HOME/.env, set bot.mode: standalone and discord.chat_id"
say "in config/local.yaml (discord.allowed_user_ids is optional; empty lets everyone in the channel in), then:"
say "  $PYTHON -m print_handy.bot check"
say "  systemctl --user enable --now print-handy-bot.service print-handy-listener.service"
