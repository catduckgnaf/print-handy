#!/usr/bin/env bash
# Hosted-only clean-directory Compose startup and bind persistence regression.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
image=${1:?usage: clean_compose_start.sh LOCAL_IMAGE}
work=$(mktemp -d)
project="ph-clean-start-${GITHUB_RUN_ID:-local}-$$"
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    (cd "$work" && docker compose -p "$project" logs) || true
  fi
  (cd "$work" && docker compose -p "$project" down --volumes --remove-orphans) >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT
mkdir -p "$work/fixture" "$work/data"
cp "$repo/docker-compose.yml" "$repo/tests/docker/compose.acceptance.yml" "$work/"
cp "$repo/tests/docker/fake_moonraker.py" "$repo/tests/docker/image_probe.py" "$work/fixture/"
sed -e "s#^PRINT_HANDY_IMAGE=.*#PRINT_HANDY_IMAGE=$image#" \
    -e 's#^DISCORD_BOT_TOKEN=.*#DISCORD_BOT_TOKEN=#' \
    "$repo/.env.example" > "$work/.env"
export ACCEPTANCE_FIXTURES="$work/fixture" PRINT_HANDY_IMAGE="$image"
# Intentionally do not create or chown any bind source. Docker creates them on first up.
compose=(docker compose -p "$project" -f docker-compose.yml -f compose.acceptance.yml)
(cd "$work" && "${compose[@]}" up -d --wait --wait-timeout 180)
(cd "$work" && "${compose[@]}" ps)
container=$(cd "$work" && "${compose[@]}" ps -q combined)
test -n "$container"
# A container that exited/restarted rather than becoming ready fails --wait above.
test "$(docker inspect -f '{{.State.Running}}' "$container")" = true
# Assert every source the deployment binds exists and is owned by container uid/gid.
for path in state hermes archive records cache spoolman; do
  test -d "$work/data/$path"
  owner=$(stat -c '%u:%g' "$work/data/$path")
  test "$owner" = 1000:1000
  printf '%s owner=%s\n' "$path" "$owner"
done
# Runtime writes to each mounted source must survive container recreation.
docker exec "$container" sh -ec 'for p in /data/state/state /data/state/logs /data/archive /data/records /data/cache /data/spoolman; do printf "persisted\n" > "$p/.clean-start-probe"; done'
(cd "$work" && "${compose[@]}" down)
(cd "$work" && "${compose[@]}" up -d --wait --wait-timeout 180)
container=$(cd "$work" && "${compose[@]}" ps -q combined)
docker exec "$container" sh -ec 'for p in /data/state/state /data/state/logs /data/archive /data/records /data/cache /data/spoolman; do test "$(cat "$p/.clean-start-probe")" = persisted; done'
printf '%s\n' 'clean-directory startup and writable bind persistence: PASS'
