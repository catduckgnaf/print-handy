#!/bin/bash
# Required child lifecycle; tini reaps orphans and forwards process-group signals.
set -euo pipefail
if [ "$#" -gt 0 ]; then exec "$@"; fi
children=()
children_names=()
listener_loops=()
listener_run_dir=""
cleanup() {
    trap '' TERM INT
    local pid file
    for pid in "${children[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    for pid in "${listener_loops[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    if [ -n "$listener_run_dir" ]; then
        for file in "$listener_run_dir"/*.pid; do
            [ -f "$file" ] || continue
            kill -TERM "$(cat "$file")" 2>/dev/null || true
        done
    fi
    for _ in {1..100}; do
        alive=false
        for pid in "${children[@]}" "${listener_loops[@]}"; do kill -0 "$pid" 2>/dev/null && alive=true; done
        if [ -n "$listener_run_dir" ]; then
            for file in "$listener_run_dir"/*.pid; do
                [ -f "$file" ] || continue
                kill -0 "$(cat "$file")" 2>/dev/null && alive=true
            done
        fi
        if [ "$alive" = false ]; then break; fi
        sleep 0.1
    done
    for pid in "${children[@]}" "${listener_loops[@]}"; do kill -KILL "$pid" 2>/dev/null || true; done
    if [ -n "$listener_run_dir" ]; then
        for file in "$listener_run_dir"/*.pid; do
            [ -f "$file" ] || continue
            kill -KILL "$(cat "$file")" 2>/dev/null || true
        done
        rm -rf "$listener_run_dir"
    fi
    wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
export SPOOLMAN_HOST="${SPOOLMAN_HOST:-127.0.0.1}"
export SPOOLMAN_PORT="${SPOOLMAN_PORT:-8000}"
export SPOOLMAN_DIR_DATA="${SPOOLMAN_DIR_DATA:-/data/spoolman}"
export SPOOLMAN_DIR_CONFIG="${SPOOLMAN_DIR_CONFIG:-/data/spoolman/config}"
export SPOOLMAN_DIR_LOGS="${SPOOLMAN_DIR_LOGS:-/data/spoolman/logs}"
export SPOOLMAN_DIR_BACKUPS="${SPOOLMAN_DIR_BACKUPS:-/data/spoolman/backups}"
# Spoolman 0.27.1 added an outbound update check (SPOOLMAN_UPDATE_CHECK,
# upstream default true) that polls api.github.com. The bundled Spoolman is
# Print Handy's to update, on Print Handy's pins - never its own - and Print
# Handy is local-first, so the bundled instance never phones home. An
# operator who runs their own external Spoolman controls that instance's
# setting themselves; this default governs only the bundled launch.
export SPOOLMAN_UPDATE_CHECK="${SPOOLMAN_UPDATE_CHECK:-FALSE}"
export PRINT_HANDY__SERVICES__SPOOLMAN_URL="${PRINT_HANDY__SERVICES__SPOOLMAN_URL:-http://127.0.0.1:$SPOOLMAN_PORT}"
if [ "${PRINT_HANDY_BUNDLED_SPOOLMAN:-true}" = true ]; then
    mkdir -p "$SPOOLMAN_DIR_DATA" "$SPOOLMAN_DIR_CONFIG" "$SPOOLMAN_DIR_LOGS" "$SPOOLMAN_DIR_BACKUPS"
    # An empty SPOOLMAN_CORS_ORIGIN (Compose and Unraid always pass it) makes Spoolman log a warning on every start; unset it so CORS stays off.
    if [ -z "${SPOOLMAN_CORS_ORIGIN:-}" ]; then unset SPOOLMAN_CORS_ORIGIN; fi
    # Spoolman runs its migrations by spawning `alembic`, so its venv bin must lead PATH.
    (cd /opt/spoolman && PATH=/opt/spoolman/.venv/bin:$PATH exec .venv/bin/python -m uvicorn spoolman.main:app --host "$SPOOLMAN_HOST" --port "$SPOOLMAN_PORT") &
    children+=("$!"); children_names+=("spoolman")
    python3 - <<'PY'
import os, time, urllib.request
url = 'http://127.0.0.1:' + os.environ['SPOOLMAN_PORT'] + '/api/v1/health'
for attempt in range(90):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit('Bundled Spoolman failed readiness')
PY
else
    case "$PRINT_HANDY__SERVICES__SPOOLMAN_URL" in
        http://127.0.0.1:*|http://localhost:*) echo 'External Spoolman URL required when bundled API is disabled' >&2; exit 2;;
    esac
fi
# go2rtc (#246 stage 1) normalises the camera source on loopback for
# in-container timelapse capture and for live viewing in Discord (#311):
# it holds the one upstream camera pull and fans out to both. A
# WITH_GO2RTC=false build has no binary (so the whole block is skipped
# there); the published image includes it. It runs only when a feature
# needs it ([timelapse] capture_enabled or [live_stream] enabled). The
# gate resolves the effective configuration (print_handy.go2rtc), never
# an environment guess: 0 = start it, 3 = not started (no feature needs
# it, or no camera source is configured — the feature degrades off),
# anything else = the configuration could not be resolved; stop instead
# of guessing.
go2rtc_bin="${GO2RTC_BIN:-/usr/local/bin/go2rtc}"
if [ -x "$go2rtc_bin" ]; then
    go2rtc_gate=0
    python3 -m print_handy.go2rtc || go2rtc_gate=$?
    if [ "$go2rtc_gate" -eq 0 ]; then
        go2rtc_config="$(python3 -m print_handy.go2rtc --write-config)"
        "$go2rtc_bin" -config "$go2rtc_config" &
        children+=("$!"); children_names+=("go2rtc")
    elif [ "$go2rtc_gate" -eq 3 ]; then
        echo 'go2rtc not started: no feature needs it, or it has no camera source.'
    else
        echo "go2rtc gate failed (status $go2rtc_gate); configuration unresolved, stopping container." >&2
        exit "$go2rtc_gate"
    fi
fi
# Listener instances (#336, design §2.5): one per roster printer whose
# listener domain (print_handy.listeners, #299) has an implementation. The
# gate resolves the effective configuration per printer (model -> vendor
# connection -> domain), never the presence of a service URL, which
# settings forbid for most connections. Gate status per printer: 0 =
# start that printer's listener through the registry launcher, 3 = the
# domain has no listener (yet, or by definition), anything else = the
# configuration could not be resolved; stop instead of guessing. The
# roster itself failing to resolve stops the container the same way.
roster_status=0
roster_ids="$(python3 -m print_handy.listener_gate --roster)" || roster_status=$?
if [ "$roster_status" -ne 0 ]; then
    echo "Listener gate failed (status $roster_status); configuration unresolved, stopping container." >&2
    exit "$roster_status"
fi
listener_run_dir="$(mktemp -d)"
run_listener() {
    # One printer's listener, restarted alone when it exits, with backoff
    # and a restart budget (design §2.5): listener children are NOT
    # required children — a dead or looping listener degrades to "that
    # printer unhealthy" in the healthcheck, never to a container restart
    # loop (the #256 failure shape), and never touches the bot, Spoolman
    # or another printer's listener. The backoff/budget/window are
    # env-tunable for the container-shaped tests; defaults are production.
    local printer_id="$1"
    local backoff="${PRINT_HANDY_LISTENER_RESTART_BACKOFF_S:-5}"
    local budget="${PRINT_HANDY_LISTENER_RESTART_BUDGET:-5}"
    local window="${PRINT_HANDY_LISTENER_RESTART_WINDOW_S:-600}"
    local watch_interval="${PRINT_HANDY_LISTENER_WATCHDOG_INTERVAL_S:-15}"
    local watch_grace="${PRINT_HANDY_LISTENER_WATCHDOG_GRACE_S:-180}"
    local restarts=0 window_start=$SECONDS child="" status=0 watcher=""
    trap 'if [ -n "$watcher" ]; then kill "$watcher" 2>/dev/null || true; fi; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null || true; fi; exit 0' TERM INT
    while true; do
        python3 -m print_handy.listeners --printer "$printer_id" &
        child=$!
        echo "$child" > "$listener_run_dir/$printer_id.pid"
        status=0
        # Watchdog (#1020): a listener that is alive but hung (a spin or a
        # blocked event loop) never exits, so `wait` alone never restarts it
        # and its heartbeat just goes stale. A watcher subshell probes the
        # heartbeat every interval after a startup grace; when it is stale it
        # asks for a stack dump (faulthandler on SIGUSR1), then TERM, then KILL,
        # which ends the `wait` below and falls through to the normal restart
        # with its backoff and budget. The watcher dies with each child.
        # Only the probe's own FAIL verdict condemns a listener: a probe that
        # crashed or timed out says nothing about the heartbeat.
        (
            trap 'kill $(jobs -p) 2>/dev/null; exit 0' TERM
            sleep "$watch_grace" & wait $!
            probe_warned=""
            while kill -0 "$child" 2>/dev/null; do
                verdict="$(timeout 20 python3 -m print_handy.healthcheck --printer "$printer_id" 2>/dev/null || true)"
                case "$verdict" in
                    FAIL*)
                        echo "Listener for printer '$printer_id' has a stale heartbeat; dumping its stack and restarting it." >&2
                        kill -USR1 "$child" 2>/dev/null || true
                        sleep 2 & wait $!
                        kill -TERM "$child" 2>/dev/null || true
                        sleep 5 & wait $!
                        kill -KILL "$child" 2>/dev/null || true
                        exit 0
                        ;;
                    ok*) probe_warned="" ;;
                    *)
                        if [ -z "$probe_warned" ]; then
                            echo "Listener watchdog for printer '$printer_id': the health probe gave no verdict; leaving the listener running." >&2
                            probe_warned=1
                        fi
                        ;;
                esac
                sleep "$watch_interval" & wait $!
            done
        ) &
        watcher=$!
        wait "$child" || status=$?
        kill "$watcher" 2>/dev/null || true
        wait "$watcher" 2>/dev/null || true
        child=""
        rm -f "$listener_run_dir/$printer_id.pid"
        if [ $((SECONDS - window_start)) -ge "$window" ]; then
            window_start=$SECONDS
            restarts=0
        fi
        restarts=$((restarts + 1))
        # The restart state is persisted (#1020) so the diagnostics bundle
        # can report the counts and the bot can see a budget exhaustion;
        # shell variables alone are invisible to both. Best effort: a
        # failed record never changes the restart behavior.
        if [ "$restarts" -gt "$budget" ]; then
            python3 -m print_handy.supervisor_state --printer "$printer_id" --restarts "$restarts" --budget "$budget" --window "$window" --status "$status" --exhausted >/dev/null 2>&1 || true
            echo "Listener for printer '$printer_id' exited (status $status) and exhausted its restart budget ($budget restarts in ${window}s); leaving it down — the healthcheck reports this printer unhealthy." >&2
            return 0
        fi
        python3 -m print_handy.supervisor_state --printer "$printer_id" --restarts "$restarts" --budget "$budget" --window "$window" --status "$status" >/dev/null 2>&1 || true
        echo "Listener for printer '$printer_id' exited (status $status); restarting in ${backoff}s (restart $restarts/$budget)." >&2
        sleep "$backoff" & wait $! || true
        backoff=$((backoff * 2))
        if [ "$backoff" -gt 60 ]; then backoff=60; fi
    done
}
if [ -n "$roster_ids" ]; then
    while IFS= read -r printer_id; do
        [ -n "$printer_id" ] || continue
        listener_gate=0
        python3 -m print_handy.listener_gate --printer "$printer_id" || listener_gate=$?
        if [ "$listener_gate" -eq 0 ]; then
            run_listener "$printer_id" &
            listener_loops+=("$!")
        elif [ "$listener_gate" -eq 3 ]; then
            echo "Printer listener not started for printer '$printer_id': its domain has no listener."
        else
            echo "Listener gate failed for printer '$printer_id' (status $listener_gate); configuration unresolved, stopping container." >&2
            exit "$listener_gate"
        fi
    done <<< "$roster_ids"
fi
# The bot runs when the configured [bot] platform (discord, telegram or
# matrix) has its own token: the gate resolves the configuration, never
# DISCORD_BOT_TOKEN alone (which left Telegram/Matrix containers with no
# bot). Gate status: 0 = start, 3 = that platform's token is empty (start
# nothing), anything else = configuration unresolved; stop, never guess.
bot_gate=0
python3 -m print_handy.bot_gate || bot_gate=$?
if [ "$bot_gate" -eq 0 ]; then
    python3 -m print_handy.bot &
    children+=("$!"); children_names+=("bot")
elif [ "$bot_gate" -ne 3 ]; then
    echo "Bot gate failed (status $bot_gate); configuration unresolved, stopping container." >&2
    exit "$bot_gate"
fi
# The optional Web UI (#286) runs only when [webui] enabled is true and a
# port is chosen: the gate resolves the effective configuration, never an
# environment guess. Gate status: 0 = start the Web UI, 3 = disabled or no
# port (start nothing), anything else = the configuration could not be
# resolved; stop instead of guessing. The gate never bypasses the
# server's own start rules: enabling the Web UI on a non-loopback host
# without the WEBUI_ACCESS_TOKEN secret is reported by the gate as a
# blocked start (#440) — the Web UI is skipped with the fix in the log
# and the rest of the container carries on, instead of the child's
# refusal halting a healthy install. The server still refuses that
# bind outright when run standalone.
webui_gate=0
python3 -m print_handy.webui_gate || webui_gate=$?
if [ "$webui_gate" -eq 0 ]; then
    python3 -m print_handy.webui &
    children+=("$!"); children_names+=("webui")
elif [ "$webui_gate" -eq 3 ]; then
    echo 'Web UI not started: disabled, no port configured, or blocked (see the webui gate message above).'
else
    echo "Web UI gate failed (status $webui_gate); configuration unresolved, stopping container." >&2
    exit "$webui_gate"
fi
# The live-view Activity server (#311 stage 4) runs only when
# [live_stream] is enabled AND configured (voice channel, activity
# origin, a chosen loopback port, the Discord application ID and both
# secrets in the environment): the gate resolves the effective
# configuration (print_handy.live_gate), never an environment guess.
# Gate status: 0 = start the server (python3 -m print_handy.live_activity),
# 3 = disabled or incompletely configured (start nothing — a healthy,
# reported state, like the go2rtc no-source case), anything else = the
# configuration could not be resolved; stop instead of guessing. The
# server binds loopback only; Discord clients reach it through the
# named Cloudflare Tunnel and the Developer Portal URL mapping, and
# the tunnel is a separate, optional sidecar — its absence never stops
# this container.
live_gate=0
python3 -m print_handy.live_gate || live_gate=$?
if [ "$live_gate" -eq 0 ]; then
    python3 -m print_handy.live_activity &
    children+=("$!"); children_names+=("live view")
elif [ "$live_gate" -eq 3 ]; then
    echo 'Live view not started: disabled or not fully configured.'
else
    echo "Live gate failed (status $live_gate); configuration unresolved, stopping container." >&2
    exit "$live_gate"
fi
if [ "${#children[@]}" -eq 0 ]; then
    # No required children for this configuration (no bot, no bundled
    # Spoolman, no Web UI, no live view; listener instances run under
    # their own loops and are never required): stay up idling instead
    # of exiting into a restart loop.
    echo 'No supervised children for this configuration; idling.'
    sleep infinity &
    children+=("$!"); children_names+=("idle")
fi
status=0
wait -n "${children[@]}" || status=$?
# Name the child that exited (#390 stage 4): a bare status left the operator
# guessing which of spoolman/bot/webui/live-view/go2rtc died.
exited_name="unknown"
for i in "${!children[@]}"; do
    if ! kill -0 "${children[$i]}" 2>/dev/null; then exited_name="${children_names[$i]:-unknown}"; break; fi
done
echo "Required child exited: $exited_name (status $status); stopping container." >&2
if [ "$status" -eq 0 ]; then status=1; fi
exit "$status"
