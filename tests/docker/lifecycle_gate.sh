#!/bin/bash
# Release-candidate lifecycle gate: fresh install, upgrade, restore and downgrade, offline.
#
#   tests/docker/lifecycle_gate.sh PREVIOUS_IMAGE CANDIDATE_IMAGE [OUT_DIR]
#
# PREVIOUS_IMAGE is the release users run today; CANDIDATE_IMAGE is the image about to ship. Both
# must already be local (a tag or immutable ID); this script never builds, pulls or pushes. It is
# the scripted form of steps B (backup) and R (restore rehearsal) of the local rollout plan, run
# against throwaway data trees only:
#
#   fresh     candidate on an empty data tree: healthy, launch DB and Spoolman created.
#   upgrade   previous on a new tree, seeded with a launch record, a spool and file markers;
#             stopped; backed up; candidate on the same tree: healthy and every seed intact.
#   restore   the pre-upgrade backup restored into a new tree, candidate on it: healthy, seeds
#             intact (migration is idempotent on restored data).
#   downgrade the pre-upgrade backup restored into a new tree, previous image on it: healthy,
#             seeds intact. This is the rollback path; no automatic database downgrade exists.
#
# Phases that run the previous image (prev, downgrade) run with the previous release's own
# example env, generated from that release's tag in this repo: a shipped release refuses
# settings keys introduced after it, so feeding it the candidate's env fails prev:healthy on a
# configuration no real install of that release could ever have run with. The in-place
# downgrade probe is the one deliberate exception — it keeps the candidate env, simulating a
# user who downgrades in place with their current, candidate-era config.
#
# The backup (tests/docker/lifecycle_backup.py) is taken with every container stopped: each
# SQLite file is copied with the SQLite backup API, stale -wal/-shm files are dropped, PRAGMA
# integrity_check must answer ok, and a SHA-256 manifest is written. Every phase runs as its own Compose project on an internal
# network with no route out, with dummy secrets and a fake Moonraker. No Discord, printer, Home
# Assistant, household Spoolman or production data tree is touched, and no host port is used.
# Results: OUT_DIR/results.txt plus logs.
set -euo pipefail

previous=${1:?usage: lifecycle_gate.sh PREVIOUS_IMAGE CANDIDATE_IMAGE [OUT_DIR]}
candidate=${2:?usage: lifecycle_gate.sh PREVIOUS_IMAGE CANDIDATE_IMAGE [OUT_DIR]}
repo=$(cd "$(dirname "$0")/../.." && pwd)
out=${3:-$(mktemp -d)}
mkdir -p "$out"
out=$(cd "$out" && pwd)  # absolute: phases and cleanup run from $work
run_id="$(date +%s)-$$"
work=$(mktemp -d)
failures=0
projects=()  # one "NAME<TAB>IMAGE<TAB>TREE" entry per started phase, for cleanup
files=(-f docker-compose.yml -f compose.acceptance.yml)

log() { echo "== $*"; }
record() {  # record NAME ok|FAIL|info DETAIL; info never fails the gate
    printf '%-30s %-4s %s\n' "$1" "$2" "$3" | tee -a "$out/results.txt"
    case "$2" in ok|info) ;; *) failures=$((failures + 1)) ;; esac
}
cleanup() {
    local entry name image tree
    # Through phase(), so Compose gets the same required variables it was started with.
    for entry in "${projects[@]}"; do
        IFS=$'\t' read -r name image tree <<< "$entry"
        phase "$name" "$image" "$tree" logs --no-color > "$out/logs-ph-life-$name-$run_id.txt" 2>&1 || true
        phase "$name" "$image" "$tree" down --remove-orphans > /dev/null 2>&1 ||
            echo "warning: compose down failed for ph-life-$name-$run_id" >&2
    done
    if ! rm -rf "$work" 2> /dev/null; then
        sudo rm -rf "$work" || echo "warning: could not remove $work" >&2
    fi
}
trap cleanup EXIT

cp "$repo/docker-compose.yml" "$repo/tests/docker/compose.acceptance.yml" "$work/"
mkdir -p "$work/fixture" && cp "$repo/tests/docker/fake_moonraker.py" "$repo/tests/docker/lifecycle_backup.py" "$work/fixture/"
sed -e "s#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=#" \
    -e "s#^PRINT_HANDY__DISCORD__CHAT_ID=.*#PRINT_HANDY__DISCORD__CHAT_ID=100000000000000001#" \
    -e "s#^PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=.*#PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=200000000000000002#" \
    "$repo/.env.example" > "$work/.env"
cp "$work/.env" "$work/.env.candidate"  # pristine: phase() swaps $work/.env per phase

# The previous release's own example env for the phases that run it (prev, downgrade):
# derived from the previous image's version label -> that tag's .env.example in this repo,
# through the same substitutions as the candidate env above. Needs the tag in the checkout
# (the workflows fetch full history). If the version does not resolve to a repo tag, fall
# back to the candidate env and say so — never fail the gate over this.
prev_version=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$previous" 2> /dev/null || true)
if [[ "$prev_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && git -C "$repo" show "$prev_version:.env.example" > "$work/.env.prev.raw" 2> /dev/null; then
    sed -e "s#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=#" \
        -e "s#^PRINT_HANDY__DISCORD__CHAT_ID=.*#PRINT_HANDY__DISCORD__CHAT_ID=100000000000000001#" \
        -e "s#^PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=.*#PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=200000000000000002#" \
        "$work/.env.prev.raw" > "$work/.env.prev"
    rm -f "$work/.env.prev.raw"
    record identity:previous-env info "prev and downgrade phases use the $prev_version example env (git tag $prev_version:.env.example)"
else
    cp "$work/.env" "$work/.env.prev"
    record identity:previous-env info "prev and downgrade phases use the candidate example env (previous version '${prev_version:-unknown}' did not resolve to a repo tag)"
fi
export ACCEPTANCE_FIXTURES="$work/fixture"

# phase NAME IMAGE TREE: the Compose wrapper for one phase. Shell variables beat .env in Compose.
# The env file is selected per phase: phases running the previous image (prev, downgrade) get
# the previous release's own example env (.env.prev) — it refuses candidate-era keys — while
# every other phase gets the candidate env (.env). The in-place downgrade probe (inplace) is
# the deliberate exception: it keeps the candidate env, simulating a downgrade in place with a
# current config. --env-file feeds Compose interpolation; the selected file is also copied
# onto $work/.env because the services' env_file: [.env] resolves in the project directory —
# the two channels must never disagree.
phase() { local name=$1 image=$2 tree=$3; shift 3
    local env_file="$work/.env" env_src="$work/.env.candidate"
    case "$name" in
        prev|downgrade) env_file="$work/.env.prev"; env_src="$work/.env.prev" ;;
    esac
    cp "$env_src" "$work/.env"
    (cd "$work" && PRINT_HANDY_IMAGE="$image" PRINT_HANDY_DATA="$tree" PRINT_HANDY_SPOOLMAN_DATA="$tree/spoolman" \
        docker compose --env-file "$env_file" -p "ph-life-$name-$run_id" "${files[@]}" "$@"); }
new_tree() {  # new_tree DIR: an empty data tree owned by the image's uid 1000
    # Both state-volume names (#914): the previous image seeds under
    # hermes/, this one under state/ — the upgrade phase exercises the
    # entrypoint migration between them.
    mkdir -p "$1"/{state,hermes,archive,records,cache,spoolman}
    chown -R 1000:1000 "$1" 2> /dev/null || sudo chown -R 1000:1000 "$1"
}
# as_handy IMAGE TREE CMD...: a one-shot, no-network helper container on a stopped tree.
as_handy() { local image=$1 tree=$2; shift 2
    docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges:true \
        --user 1000:1000 --entrypoint python3 -v "$tree:/tree" -v "$work/backup:/backup" \
        -v "$work/fixture:/fixture:ro" "$image" "$@"; }
start() {  # start NAME IMAGE TREE: up and healthy, or a recorded failure
    projects+=("$1"$'\t'"$2"$'\t'"$3")
    if phase "$1" "$2" "$3" up -d --wait --wait-timeout 240 fake-moonraker combined > "$out/up-$1.txt" 2>&1; then
        record "$1:healthy" ok "$(docker image inspect --format '{{.Id}}' "$2" | cut -c1-19)"
        return 0
    fi
    record "$1:healthy" FAIL "see up-$1.txt"
    return 1
}
stop() { phase "$1" "$2" "$3" stop -t 30 > /dev/null 2>&1 || true; }

# The seeds are written through each image's own public code paths, never by raw SQL from here.
seed() {  # seed NAME IMAGE TREE
    phase "$1" "$2" "$3" exec -T combined sh -c 'echo lifecycle > /data/archive/lifecycle-marker && echo lifecycle > /data/records/lifecycle-marker' &&
    phase "$1" "$2" "$3" exec -T combined python3 -c "
from print_handy.core import launch_state
with launch_state.transaction() as con:
    launch_state.record_consumed(con, '/data/archive/lifecycle/JOB.json', 'f' * 64, 'lifecycle-seed', '2026-01-01T00:00:00Z')
print(launch_state.db_path())" &&
    phase "$1" "$2" "$3" exec -T combined python3 -c "
import json, urllib.request
def post(path, body):
    req = urllib.request.Request('http://127.0.0.1:8000/api/v1/' + path, json.dumps(body).encode(), {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=10))
f = post('filament', {'name': 'Lifecycle PLA', 'material': 'PLA', 'density': 1.24, 'diameter': 1.75})
post('spool', {'filament_id': f['id'], 'location': 'Lifecycle Slot'})"
}
verify() {  # verify NAME IMAGE TREE: every seed is still there, read back through the image
    local detail
    if detail=$(phase "$1" "$2" "$3" exec -T combined python3 -c "
import json, sys, urllib.request
from print_handy.core import launch_state
for path in ('/data/archive/lifecycle-marker', '/data/records/lifecycle-marker'):
    assert open(path).read().strip() == 'lifecycle', path
with launch_state.transaction() as con:
    rows = launch_state.consumed(con, '/data/archive/lifecycle/JOB.json', 'f' * 64)
assert [r['kind'] for r in rows] == ['lifecycle-seed'], rows
spools = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/spool', timeout=10))
assert any(s['location'] == 'Lifecycle Slot' and s['filament']['name'] == 'Lifecycle PLA' for s in spools), spools
print('markers, launch record and spool intact')" 2>&1); then
        record "$1:data-intact" ok "$detail"
    else
        echo "$detail" > "$out/verify-$1.txt"
        record "$1:data-intact" FAIL "$(tail -n 1 <<< "$detail" | cut -c1-160)"
    fi
}

log "identities"
for role in previous candidate; do
    image=${!role}
    docker image inspect "$image" > "$out/image-$role.json"
    record "identity:$role" info "$image $(docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}} channel={{index .Config.Labels "org.print-handy.app-channel"}} digests={{.RepoDigests}}' "$image")"
done

log "fresh install: candidate on an empty tree"
fresh="$work/fresh"; new_tree "$fresh"
if start fresh "$candidate" "$fresh"; then
    if detail=$(phase fresh "$candidate" "$fresh" exec -T combined python3 -c "
import urllib.request
from print_handy.core import launch_state
with launch_state.transaction():
    pass
urllib.request.urlopen('http://127.0.0.1:8000/api/v1/spool', timeout=10)
print('launch DB', launch_state.db_path(), 'and Spoolman API ready')" 2>&1); then
        record fresh:schema ok "$detail"
    else
        echo "$detail" > "$out/fresh-schema.txt"; record fresh:schema FAIL "$(tail -n 1 <<< "$detail" | cut -c1-160)"
    fi
fi
stop fresh "$candidate" "$fresh"

log "upgrade: previous seeded, stopped, backed up, then candidate on the same tree"
tree="$work/upgrade"; new_tree "$tree"
start prev "$previous" "$tree" || { echo "previous image never became healthy" >&2; exit 1; }
if seed prev "$previous" "$tree" > "$out/seed.txt" 2>&1; then record upgrade:seeded ok "launch record, spool and markers via $previous"
else record upgrade:seeded FAIL "see seed.txt"; fi
verify prev "$previous" "$tree"
stop prev "$previous" "$tree"

log "backup: SQLite backup API, integrity_check, SHA-256 manifest (containers stopped)"
mkdir -p "$work/backup"
chown 1000:1000 "$work/backup" 2> /dev/null || sudo chown 1000:1000 "$work/backup"
if detail=$(as_handy "$candidate" "$tree" /fixture/lifecycle_backup.py backup /tree /backup 2>&1); then
    record backup:verified ok "$detail"
    cp "$work/backup/SHA256SUMS" "$out/backup-SHA256SUMS.txt" 2> /dev/null || true
else
    echo "$detail" > "$out/backup.txt"; record backup:verified FAIL "$(tail -n 1 <<< "$detail" | cut -c1-160)"
fi

start upgrade "$candidate" "$tree" && verify upgrade "$candidate" "$tree"
stop upgrade "$candidate" "$tree"
# Informational: the previous image on a tree the candidate has migrated. Rollback never relies
# on this (it restores the backup below); the line shows whether an in-place downgrade happens to work.
projects+=("inplace"$'\t'"$previous"$'\t'"$tree")
if phase inplace "$previous" "$tree" up -d --wait --wait-timeout 240 fake-moonraker combined > "$out/up-inplace.txt" 2>&1; then
    record downgrade:in-place info "previous image healthy on the candidate-migrated tree"
else
    record downgrade:in-place info "previous image not healthy on the migrated tree; rollback must restore the backup"
fi
stop inplace "$previous" "$tree"

restore_into() {  # restore_into DIR: the pre-upgrade backup into a new tree, manifest checked
    as_handy "$candidate" "$1" /fixture/lifecycle_backup.py restore /backup /tree
}
for step in restore downgrade; do
    log "$step: pre-upgrade backup restored into a new tree"
    dir="$work/$step"; new_tree "$dir"
    if detail=$(restore_into "$dir" 2>&1); then record "$step:restored" ok "$detail"
    else echo "$detail" > "$out/$step-restore.txt"; record "$step:restored" FAIL "$(tail -n 1 <<< "$detail" | cut -c1-160)"; continue; fi
    image=$candidate; [ "$step" = downgrade ] && image=$previous
    start "$step" "$image" "$dir" && verify "$step" "$image" "$dir"
    stop "$step" "$image" "$dir"
done

echo "results: $out/results.txt"
if [ "$failures" -gt 0 ]; then
    echo "FAILED: $failures check(s)" | tee -a "$out/results.txt"
    exit 1
fi
echo "PASSED: $previous -> $candidate" | tee -a "$out/results.txt"
