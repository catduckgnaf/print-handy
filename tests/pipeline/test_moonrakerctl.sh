#!/usr/bin/env bash
# Offline by default. The read-only commands need a real printer: PRINT_HANDY_LIVE_PRINTER=1 with
# MOONRAKER_URL (or services.moonraker_url in config/local.yaml) pointing at it.
set -euo pipefail

CTL="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/bin/moonrakerctl"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

assert_json() {
  local name="$1"
  shift
  "$CTL" "$@" >"$TMP_ROOT/$name.json"
  jq -e . "$TMP_ROOT/$name.json" >/dev/null
}

reads=0
if [[ "${PRINT_HANDY_LIVE_PRINTER:-}" == 1 ]]; then
  assert_json status status
  assert_json temps temps
  assert_json objects objects
  assert_json history history 2
  assert_json mesh mesh
  assert_json tmc tmc
  assert_json spool spool
  assert_json webcams webcams
  assert_json timelapse timelapse
  assert_json system-info system-info
  assert_json proc-stats proc-stats
  assert_json updates updates
  assert_json gcode-store gcode-store 5
  assert_json files files
  reads=14
fi

# Non-mutating validation of the high-risk command gates.
if "$CTL" reboot WRONG >/dev/null 2>&1; then
  echo "reboot confirmation gate failed" >&2
  exit 1
fi
if "$CTL" shutdown WRONG >/dev/null 2>&1; then
  echo "shutdown confirmation gate failed" >&2
  exit 1
fi
if "$CTL" update klipper WRONG >/dev/null 2>&1; then
  echo "update confirmation gate failed" >&2
  exit 1
fi

# Without a configured Moonraker URL nothing is sent anywhere.
if env -u MOONRAKER_URL PRINT_HANDY__SERVICES__MOONRAKER_URL= "$CTL" status >/dev/null 2>&1; then
  echo "unconfigured Moonraker URL was accepted" >&2
  exit 1
fi

# Mock curl so mutating command serialization can be tested without touching the printer.
mkdir -p "$TMP_ROOT/bin"
cat >"$TMP_ROOT/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
out='' data='' max_time=''
while (($#)); do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    --max-time) max_time="$2"; shift 2 ;;
    *) shift ;;
  esac
done
printf '%s' "$data" >"$MOCK_CAPTURE.data"
printf '%s' "$max_time" >"$MOCK_CAPTURE.timeout"
printf '{}' >"$out"
printf '200'
EOF
chmod +x "$TMP_ROOT/bin/curl"
MOONRAKER_URL=http://moonraker.test:7125 MOCK_CAPTURE="$TMP_ROOT/gcode" PATH="$TMP_ROOT/bin:$PATH" "$CTL" gcode $'G91\nG1 Z10 F600\nG90' >/dev/null
jq -e '.script == "G91\nG1 Z10 F600\nG90"' "$TMP_ROOT/gcode.data" >/dev/null || {
  echo "multiline G-code serialization failed" >&2
  exit 1
}
[[ "$(cat "$TMP_ROOT/gcode.timeout")" == 300 ]] || {
  echo "G-code timeout override failed" >&2
  exit 1
}

printf 'PASS: %s read-only commands, confirmation gates, and G-code transport regression tests\n' "$reads"
