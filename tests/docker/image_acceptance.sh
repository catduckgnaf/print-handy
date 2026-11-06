#!/bin/bash
# Offline runtime acceptance for ONE already-built Print Handy image. Never builds or pulls it.
#
#   tests/docker/image_acceptance.sh IMAGE [included] [OUT_DIR]
#
# IMAGE is a local tag or immutable ID. Print Handy runs as ONE supervised container: Discord
# bot, Moonraker listener and the bundled Spoolman API (#214). The mode argument is kept for the
# workflow: `included`, or the legacy alias `bundled`; there is no Spoolman-free mode.
# Everything runs under a unique Compose project on an internal network with no route out, with
# dummy secrets, and only this run's containers and temp files are removed afterwards. No
# Discord, printer, Home Assistant or household Spoolman is contacted, and no host port is used.
#
# Checks: image identity; packaged payload and handlers without a source mount; Orca executable
# and shared-library closure, or its absence; the ffmpeg runtime dependency and its timelapse
# encoders; the OpenSCAD runtime dependency (#388); real slicing plus G-code guards, or fail-closed
# refusal; egress isolation; the same on a read-only root; the Unraid template's run flags;
# combined readiness (listener plus Spoolman, bot idle); Spoolman API on loopback only with no
# web UI; stale and cross-container heartbeat refusal; graceful SIGTERM; data and Spoolman
# persistence across container recreation; a same-image tag switch (recreation coverage, NOT a
# cross-version upgrade); and a dummy-token bot that never reports healthy.
# Results: OUT_DIR/results.txt plus logs.
set -euo pipefail

image=${1:?usage: image_acceptance.sh IMAGE [included] [OUT_DIR]}
mode=${2:-included}
case "$mode" in included|bundled) ;; *) echo "mode must be included (Spoolman is included in the stack; there is no external mode)" >&2; exit 2 ;; esac
if [ "$mode" = bundled ]; then mode=included; fi
repo=$(cd "$(dirname "$0")/../.." && pwd)
out=${3:-$(mktemp -d)}
mkdir -p "$out"
run_id="$(date +%s)-$$"
project="ph-accept-${mode}-${run_id}"
work=$(mktemp -d)
data="$work/data"
failures=0

log() { echo "== $*"; }
record() {  # record NAME ok|FAIL DETAIL
    printf '%-34s %-4s %s\n' "$1" "$2" "$3" | tee -a "$out/results.txt"
    if [ "$2" != ok ]; then failures=$((failures + 1)); fi
}
collect() {
    compose ps -a > "$out/compose-ps.txt" 2>&1 || echo "compose ps failed" >> "$out/compose-ps.txt"
    compose logs --no-color > "$out/compose-logs.txt" 2>&1 || echo "compose logs failed" >> "$out/compose-logs.txt"
}
cleanup() {
    collect
    if ! compose down --remove-orphans > "$out/down.txt" 2>&1; then
        echo "warning: compose down failed for $project; see $out/down.txt" >&2
    fi
    docker image rm "ph-accept-tagswitch:$run_id" > /dev/null 2>&1 || true  # only this run's tag
    if ! rm -rf "$work" 2> /dev/null; then
        sudo rm -rf "$work" || echo "warning: could not remove $work" >&2
    fi
}
trap cleanup EXIT

# The deployment files a user downloads, plus the acceptance overlays, in a fresh folder.
cp "$repo/docker-compose.yml" "$repo/tests/docker/compose.acceptance.yml" "$work/"
mkdir -p "$work/fixture" && cp "$repo/tests/docker/fake_moonraker.py" "$repo/tests/docker/image_probe.py" "$work/fixture/"
files=(-f docker-compose.yml -f compose.acceptance.yml)
compose() { (cd "$work" && docker compose -p "$project" "${files[@]}" "$@"); }

# .env from the shipped example: dummy values only, never real credentials. The token starts
# empty so the bot idles; the last check sets the dummy token to prove the bot fails closed.
dummy_token="offline-dummy-not-a-discord-token"
sed -e "s#^PRINT_HANDY_IMAGE=.*#PRINT_HANDY_IMAGE=$image#" \
    -e "s#^PRINT_HANDY_DATA=.*#PRINT_HANDY_DATA=$data#" \
    -e "s#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=#" \
    -e "s#^PRINT_HANDY__DISCORD__CHAT_ID=.*#PRINT_HANDY__DISCORD__CHAT_ID=100000000000000001#" \
    -e "s#^PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=.*#PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=200000000000000002#" \
    "$repo/.env.example" > "$work/.env"
export ACCEPTANCE_FIXTURES="$work/fixture" PRINT_HANDY_IMAGE="$image"
mkdir -p "$data"/{state,hermes,archive,records,cache,spoolman}
if ! chown -R 1000:1000 "$data" 2> /dev/null; then
    sudo chown -R 1000:1000 "$data"
fi

log "image identity"
docker image inspect "$image" > "$out/image-inspect.json"
image_id=$(docker image inspect --format '{{.Id}}' "$image")
record identity ok "$image_id $(docker image inspect --format '{{.Os}}/{{.Architecture}} app-channel={{index .Config.Labels "org.print-handy.app-channel"}}' "$image")"

probe_args=(--rm --network none --cap-drop ALL --security-opt no-new-privileges:true
    -e "DISCORD_BOT_TOKEN=$dummy_token" -e PRINT_HANDY__BOT__MODE=standalone
    -e PRINT_HANDY__DISCORD__CHAT_ID=100000000000000001 -e PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=200000000000000002
    -v "$data/state:/data/state" -v "$data/hermes:/data/hermes" -v "$data/archive:/data/archive" -v "$data/records:/data/records"
    -v "$data/cache:/data/cache" -v "$work/fixture:/probe:ro")
probe() {  # probe LABEL [docker run options...] -- CHECK
    local label=$1; shift
    local opts=()
    while [ "$1" != -- ]; do opts+=("$1"); shift; done
    shift
    local name="$project-probe-$label-$1"
    if output=$(timeout 1200 docker run --name "$name" "${probe_args[@]}" "${opts[@]}" "$image" \
            python3 /probe/image_probe.py "$1" 2>&1); then
        record "$label:$1" ok "$(grep '^PROBE ' <<< "$output" | cut -c7-400)"
    else
        echo "$output" > "$out/probe-$label-$1.txt"
        record "$label:$1" FAIL "$(grep '^PROBE ' <<< "$output" | cut -c7-400 || tail -n 3 <<< "$output")"
    fi
}

for check in payload closure slice egress ffmpeg openscad; do
    probe default -- "$check"
    probe readonly --read-only --tmpfs /tmp:size=64m -- "$check"
done
if output=$(timeout 300 docker run --name "$project-probe-botcheck" "${probe_args[@]}" "$image" \
        python3 -m print_handy.bot check 2>&1); then
    record bot:offline-check ok "$(grep -c '^ok' <<< "$output") ok lines; $(grep 'handlers' <<< "$output" | cut -c1-80)"
else
    echo "$output" > "$out/bot-check.txt"
    record bot:offline-check FAIL "$(grep FAIL <<< "$output" | head -n 3 | tr '\n' ' ')"
fi

log "Unraid template: its ExtraParams as docker run sees them"
template="$repo/docker/unraid/print-handy.xml"
mapfile -t extra < <(python3 -c "
import shlex, sys, xml.etree.ElementTree as ET
params = shlex.split(ET.parse(sys.argv[1]).getroot().findtext('ExtraParams'))
i = params.index('--restart'); del params[i:i + 2]   # --rm below replaces DockerMan's restart policy
print('\n'.join(params))" "$template")
# Every supervised module must import under exactly these runtime flags.
if output=$(timeout 300 docker run --name "$project-unraid" "${probe_args[@]}" "${extra[@]}" "$image" \
        python3 -c 'import importlib; [importlib.import_module(m) for m in ("print_handy.bot", "print_handy.hermes.moonraker_event_listener")]; print("imported bot and listener")' 2>&1); then
    record unraid:params ok "docker run accepted ${#extra[@]} template arguments; $(tail -n 1 <<< "$output")"
else
    echo "$output" > "$out/unraid.txt"
    record unraid:params FAIL "$(tail -n 2 <<< "$output" | tr '\n' ' ')"
fi

health() { docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}} running={{.State.Running}} exit={{.State.ExitCode}}' "$1" 2>/dev/null || echo gone; }

dump_health() {  # dump_health TAG: capture the combined container's own health-probe evidence
    local cid
    cid=$(compose ps -q combined 2>/dev/null || true)
    if [ -n "$cid" ]; then
        docker inspect --format '{{json .State.Health}}' "$cid" > "$out/health-$1.json" 2>&1 || true
    fi
    compose exec -T combined python3 /usr/local/bin/print-handy-healthcheck > "$out/healthcheck-$1.txt" 2>&1 || true
}

log "combined container: listener and bundled Spoolman healthy, bot idle (empty token)"
if compose up -d --wait --wait-timeout 240 fake-moonraker combined > "$out/up-1.txt" 2>&1; then
    record combined:healthy ok "compose up --wait passed; healthcheck covers listener and Spoolman"
else
    record combined:healthy FAIL "see up-1.txt"
    dump_health 1
fi
connected=false
for _ in $(seq 30); do
    # Since the Stage 2b listener rework (#334) the heartbeat is per-printer:
    # <state>/printers/<id>/listener_health.json (print_handy/healthcheck.py).
    if compose exec -T combined python3 -c "
import glob, json, sys
connected = False
for path in glob.glob('/data/state/state/printers/*/listener_health.json'):
    try:
        connected = connected or bool(json.load(open(path)).get('moonraker_connected'))
    except Exception:
        pass
sys.exit(0 if connected else 1)" > /dev/null 2>&1; then
        connected=true
        break
    fi
    sleep 2
done
if [ "$connected" = true ]; then record listener:moonraker ok "heartbeat reports moonraker_connected"; else record listener:moonraker FAIL "never connected to fake-moonraker"; fi

log "bundled Spoolman: API only, loopback only"
spool=""
if spool=$(compose exec -T combined python3 -c "
import json, urllib.request
def post(path, body):
    req = urllib.request.Request('http://127.0.0.1:8000/api/v1/' + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=10))
f = post('filament', {'name': 'Acceptance PLA', 'material': 'PLA', 'density': 1.24, 'diameter': 1.75})
print(post('spool', {'filament_id': f['id'], 'location': 'IFS Slot 1'})['id'])" 2> "$out/spoolman-create.txt"); then
    record spoolman:create ok "spool $spool"
else
    spool=""
    record spoolman:create FAIL "see spoolman-create.txt"
fi
if compose exec -T fake-moonraker python3 -c "import urllib.request; urllib.request.urlopen('http://combined:8000/api/v1/health', timeout=3)" > /dev/null 2>&1; then
    record spoolman:loopback-only FAIL "another container reached the Spoolman API"
else
    record spoolman:loopback-only ok "unreachable from another container on the same network"
fi
if compose exec -T combined test ! -e /opt/spoolman/client_v2/build/assets; then
    record spoolman:no-web-ui ok "no built Spoolman web UI assets in the image"
else
    record spoolman:no-web-ui FAIL "/opt/spoolman/client_v2/build/assets exists"
fi

log "heartbeat isolation"
if compose run --rm --no-deps -T combined python3 -m print_handy.healthcheck > "$out/foreign-health.txt" 2>&1; then
    record heartbeat:other-container FAIL "a second container accepted the listener's heartbeat"
else
    record heartbeat:other-container ok "$(grep -m 1 FAIL "$out/foreign-health.txt")"
fi

log "persistence marker, then graceful SIGTERM"
compose exec -T combined sh -c 'echo accepted > /data/archive/acceptance-marker && echo accepted > /data/records/acceptance-marker'
combined_before=$(compose ps -q combined)
start=$(date +%s)
compose stop -t 30 combined > /dev/null 2>&1
elapsed=$(( $(date +%s) - start ))
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$combined_before")
if [ "$exit_code" != 137 ] && [ "$elapsed" -lt 30 ]; then
    record signal:combined-sigterm ok "exit $exit_code after ${elapsed}s (137 would mean SIGKILL)"
else
    record signal:combined-sigterm FAIL "exit $exit_code after ${elapsed}s"
fi
# Age-based staleness is covered natively (tests/test_listener_health.py); this proves a stopped
# container's last heartbeat cannot make any other container healthy.
if compose run --rm --no-deps -T combined python3 -m print_handy.healthcheck > "$out/stopped-health.txt" 2>&1; then
    record heartbeat:after-stop FAIL "heartbeat of a stopped container accepted"
else
    record heartbeat:after-stop ok "$(grep -m 1 FAIL "$out/stopped-health.txt")"
fi

log "recreate the container (down without -v, up again)"
compose down > "$out/down-1.txt" 2>&1
compose up -d --wait --wait-timeout 240 fake-moonraker combined > "$out/up-2.txt" 2>&1 || { record recreate:combined FAIL "see up-2.txt"; dump_health 2; }
combined_after=$(compose ps -q combined)
if [ -n "$combined_after" ] && [ "$combined_after" != "$combined_before" ] &&
    compose exec -T combined sh -c 'grep -qx accepted /data/archive/acceptance-marker && grep -qx accepted /data/records/acceptance-marker'; then
    record recreate:data-persisted ok "new container ${combined_after:0:12}; markers intact"
else
    record recreate:data-persisted FAIL "container ${combined_after:-none}"
fi
if [ -n "$spool" ]; then
    if compose exec -T combined python3 -c "
import json, sys, urllib.request
s = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/spool/$spool', timeout=10))
sys.exit(0 if s['filament']['name'] == 'Acceptance PLA' and s['location'] == 'IFS Slot 1' else 1)"; then
        record spoolman:persisted ok "spool $spool survived recreation"
    else
        record spoolman:persisted FAIL "spool $spool missing after recreation"
    fi
fi

log "same-image tag switch (recreation coverage only, not a version upgrade)"
docker tag "$image_id" "ph-accept-tagswitch:$run_id"
sed -i "s#^PRINT_HANDY_IMAGE=.*#PRINT_HANDY_IMAGE=ph-accept-tagswitch:$run_id#" "$work/.env"
export PRINT_HANDY_IMAGE="ph-accept-tagswitch:$run_id"
if compose up -d --wait --wait-timeout 240 fake-moonraker combined > "$out/up-3.txt" 2>&1 &&
    compose exec -T combined grep -qx accepted /data/archive/acceptance-marker; then
    record tagswitch:same-image ok "ph-accept-tagswitch:$run_id -> $image_id; data reused"
else
    record tagswitch:same-image FAIL "see up-3.txt"
    dump_health 3
fi

log "bot: dummy token, no route out; the container must never report healthy"
sed -i "s#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=$dummy_token#" "$work/.env"
compose up -d --force-recreate combined > "$out/up-bot.txt" 2>&1 || echo "up returned non-zero" >> "$out/up-bot.txt"
combined_bot=$(compose ps -aq combined)
bot_healthy=false
for _ in $(seq 20); do
    state=$(health "$combined_bot")
    case "$state" in healthy*) bot_healthy=true; break ;; *running=false*) break ;; esac
    sleep 3
done
state=$(health "$combined_bot")
compose logs --no-color combined > "$out/bot.log" 2>&1 || echo "logs unavailable" >> "$out/bot.log"
bot_reason=$(grep -E 'Cannot connect to host|LoginFailure|Error:|Required child exited' "$out/bot.log" | tail -n 1 | sed 's/^[^|]*| *//' | cut -c1-160 || true)
if [ "$bot_healthy" = false ]; then record bot:fails-closed ok "$state; never healthy without Discord; ${bot_reason:-see bot.log}"; else record bot:fails-closed FAIL "reported healthy with a dummy token"; fi

echo "results: $out/results.txt"
if [ "$failures" -gt 0 ]; then
    echo "FAILED: $failures check(s)" | tee -a "$out/results.txt"
    exit 1
fi
echo "PASSED: image $image_id ($mode)" | tee -a "$out/results.txt"
