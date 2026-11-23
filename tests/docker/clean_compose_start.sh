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
  docker compose -p "$project-legacy" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if ! rm -rf "$work" 2> /dev/null; then
    sudo rm -rf "$work" || echo "warning: could not remove $work" >&2
  fi
  exit "$status"
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
for path in state archive records cache spoolman; do
  test -d "$work/data/$path"
  owner=$(stat -c '%u:%g' "$work/data/$path")
  test "$owner" = 1000:1000
  printf '%s owner=%s\n' "$path" "$owner"
done
# A new install must not get the legacy folder (#1144): nothing mounts it.
test ! -e "$work/data/hermes"
# Runtime writes to each mounted source must survive container recreation.
docker exec "$container" sh -ec 'for p in /data/state/state /data/state/logs /data/archive /data/records /data/cache /data/spoolman; do printf "persisted\n" > "$p/.clean-start-probe"; done'
(cd "$work" && "${compose[@]}" down)
(cd "$work" && "${compose[@]}" up -d --wait --wait-timeout 180)
container=$(cd "$work" && "${compose[@]}" ps -q combined)
docker exec "$container" sh -ec 'for p in /data/state/state /data/state/logs /data/archive /data/records /data/cache /data/spoolman; do test "$(cat "$p/.clean-start-probe")" = persisted; done'
test ! -e "$work/data/hermes"
printf '%s\n' 'clean-directory startup and writable bind persistence: PASS'

# Existing install whose state is still only in the pre-2.2.5 hermes folder
# (#1817): that folder is no longer read, so the main container must refuse the
# start and change nothing; the documented rename then starts it on its data.
legacy="$work/legacy-install"
mkdir -p "$legacy/data/hermes/state"
cp "$work/docker-compose.yml" "$work/compose.acceptance.yml" "$work/.env" "$legacy/"
printf 'from legacy\n' > "$legacy/data/hermes/state/legacy-probe"
# Legacy data is owned by the container user, as the earlier release left it.
sudo chown -R 1000:1000 "$legacy/data/hermes"
legacy_compose=(docker compose -p "$project-legacy" -f docker-compose.yml -f compose.acceptance.yml)
if (cd "$legacy" && "${legacy_compose[@]}" up -d --wait --wait-timeout 180); then
  echo "an install whose state is only in data/hermes started; it must be refused" >&2
  exit 1
fi
(cd "$legacy" && "${legacy_compose[@]}" logs combined) \
  | grep -F "your state is still in the old folder /data/hermes"
test -z "$(sudo find "$legacy/data/state" -mindepth 1 ! -type d -print -quit)"
test "$(cat "$legacy/data/hermes/state/legacy-probe")" = "from legacy"
(cd "$legacy" && "${legacy_compose[@]}" down)
sudo rm -rf "$legacy/data/state"
sudo mv "$legacy/data/hermes" "$legacy/data/state"
(cd "$legacy" && "${legacy_compose[@]}" up -d --wait --wait-timeout 180)
test "$(sudo cat "$legacy/data/state/state/legacy-probe")" = "from legacy"
test ! -e "$legacy/data/hermes"
(cd "$legacy" && "${legacy_compose[@]}" down --volumes --remove-orphans)
printf '%s\n' 'old hermes folder refused, and the rename starts on its data: PASS'
