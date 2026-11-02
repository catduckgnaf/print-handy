# Docker

## Combined runtime for 2.1.2

The 2.1.2 candidate replaces separate bot, listener and Spoolman services with ONE runtime container. Published 2.1.1 remains unchanged. Pin the immutable digest from a successful publication run; candidate source is not a published image.

Tini owns orphan reaping and process-group signals. The supervisor waits for the bundled API before starting the Moonraker listener and Discord bot. Any required child exit ends the container nonzero, including an unexpected clean exit. SIGTERM stops children with a bounded grace period. Docker restart reacts to process exit, not healthcheck failures alone.

The container starts the chat bot for the configured `[bot] platform` (Discord, Telegram or Matrix) when that platform's own token variable is set (`DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN` or `MATRIX_ACCESS_TOKEN` by default); an empty token disables only the bot, for staging. Never run duplicate consumers/listeners against the same state. HEALTHCHECK verifies listener heartbeat, enabled bot readiness and Spoolman `/api/v1/health`. An offline printer does not make a reconnecting listener unhealthy. Dummy credentials do not prove Discord login.

## Which processes run

Which children the supervisor starts depends on each configured printer's **listener domain** (#256, #299): the listener registry (`print_handy/listeners.py`) maps the printer model's connection to a domain, decided from the resolved configuration (`python3 -m print_handy.listener_gate`, per printer since #336), never from whether a service URL is filled in. The table reads per roster printer — a multi-printer roster gets one listener instance per printer whose domain has one:

| Listener domain (connection) | Printer listener | Discord bot | Bundled Spoolman |
|---|---|---|---|
| `moonraker` (Flashforge AD5X, Klipper, Sovol, Elegoo Neptune) | runs | with a token | unless disabled |
| `bambulab_lan` (Bambu Lab) | runs — the Bambu listener; it idles without network contact when LAN is not configured | with a token | unless disabled |
| anything else (`prusalink`, `elegoo_sdcp`, `octoprint`, `flashforge_stock`, `offline`) | **not started** — the domain has no listener yet, or none by definition | with a token | unless disabled |

The `moonraker` and `bambulab_lan` domains have listener implementations today; the full domain list and the normalised status/event contract every domain listener reports through are in [Listener domains](LISTENERS.md). Listener instances are not required children (#336): the supervisor restarts a dead listener alone, with backoff and a restart budget, and a listener that exhausts its budget stays down as "that printer unhealthy" — the bot and bundled Spoolman remain the required children whose exit ends the container. The healthcheck mirrors the same registry decision, one row per printer in a multi-printer roster: a listener that was not started reports "not required for this printer connection" instead of failing, while a printer whose domain has a listener with a stale or missing heartbeat still fails. If the configuration cannot be resolved at all, the supervisor stops with the settings error rather than guessing which children to run. A configuration with no required children at all (no bot token, external Spoolman) idles instead of exiting.

## Included Spoolman

Spoolman 0.27.0 is built from exact upstream revision `bed3f14ee77a3a571d31b14215d54b0b88a7519c` with its committed uv.lock, isolated from Print Handy dependencies. No third-party source is patched. No Node, npm or web UI assets are built or copied. A local static placeholder satisfies upstream's static-directory check. Print Handy spool commands and slicing lookups are the interface; `/api/v1` remains available.

Default bind is `127.0.0.1:8000` inside the container. Print Handy defaults to `http://127.0.0.1:8000`. Compose publishes no port by default, and with no port mapped Spoolman stays loopback-only and unexposed.

**Optional LAN access (#443, opt-in only):** Moonraker running on printer hardware (for example a ZMod AD5X at its own LAN address) and Home Assistant need network access to the bundled API. Both deployment surfaces now carry the opt-in:

- **Unraid template:** the advanced **Spoolman port** mapping (host 7912 to container 8000) plus the advanced **Spoolman bind host** variable. Keep `SPOOLMAN_HOST=127.0.0.1` (the default) and Spoolman stays loopback-only even with the mapping present; set `SPOOLMAN_HOST=0.0.0.0` to actually serve the LAN.
- **Compose:** uncomment the `#   - "7912:8000"` line under the combined service and set `SPOOLMAN_HOST=0.0.0.0` in `.env`.

Then point Moonraker's `[spoolman]` section at `server: http://<unraid-ip>:7912` (see [ZMod printers](ZMOD.md#bundled-spoolman-from-printer-moonraker-443)); a healthy bundled Spoolman answers `GET http://<unraid-ip>:7912/api/v1/info` with HTTP 200. The API is unauthenticated: keep it on the LAN only and never forward its port from a router. Spoolman's web UI is not bundled (#214) — Print Handy remains the interface.

Persist `/data/spoolman` separately, including database, config, logs and backups. Upstream Alembic migrations remain intact for existing 0.26.1/0.27.0 databases. Runtime migration acceptance is required before deployment; retaining migration files alone is not evidence of a successful migration. Preserve source data and reconcile inventory at final cutover. No automatic downgrade is provided.

For an existing external instance set `PRINT_HANDY__SERVICES__SPOOLMAN_URL=http://spoolman.example:8000`. Its data is not migrated or reset; the local database remains separate. Optionally set `PRINT_HANDY_BUNDLED_SPOOLMAN=false` to skip the local API, requiring a nonlocal URL. Healthcheck then checks the external API. Changing a URL never moves or deletes a database.

## Print records

Every print — completed, failed or cancelled — gets one self-contained JSON
record in `/data/records/Prints/` (`paths.records_vault` + `Prints`), named by
its permanent print number: `print-0057.json`. The folder lives on the `/data`
volume, so records survive container recreation and upgrades, and any past
print can be looked up with `jq` or `grep` alone — no index, database or
external app required:

```json
{
  "print_number": 57,
  "title": "Print in Place Bag Clip",
  "status": "completed",
  "success": true,
  "started_at": "2026-10-26T20:00:00Z",
  "completed_at": "2026-10-26T21:30:32Z",
  "printer": "Flashforge AD5X",
  "gcode_file": "bag-clip.gcode",
  "source_platform": "Printables",
  "filament_material": "PLA",
  "filament_color": "Black",
  "spoolman_spool_id": 12,
  "filament_used_g": 12.5,
  "print_duration_seconds": 5432,
  "record_id": "1a2b3c4d",
  "cleanup_status": "pending"
}
```

Fields the system does not know are omitted, never invented (`print_number`
is present as `null` for prints that never went through a Print Handy
launch, which instead get a dated filename). A record starts as
`"status": "printing"` when the print starts and is finalized in place when
the print ends. Example lookup: `jq -r 'select(.status != "completed") |
"\(.print_number) \(.title) \(.status)"' /data/records/Prints/*.json`.

## More than one printer

One container serves the whole roster (#257 Stage 2). Declare every printer in the mounted config file with a `[[printers]]` list — the list itself is file-only, because list values cannot come from environment variables:

```toml
[[printers]]
id = "garage-a1"
model = "bambulab.a1"
chat_id = "<a1-channel-id>"
lan_host = "<a1-lan-ip>"
lan_serial = "<a1-serial>"

[[printers]]
id = "workshop-ad5x"
model = "flashforge.ad5x"
moonraker_url = "http://<ad5x-lan-ip>:7125"
```

The first entry is the default printer and is named wherever output talks about "the printer". Each printer keeps its own state folder and job queue under `state/printers/<id>/` inside the one data directory, gets its own listener child under the supervisor (a dead listener never takes the bot or a sibling listener down), and is bound to its own chat channel — an approval in a channel can only start that channel's printer. The healthcheck reports every listener, and the Web UI status page lists the roster read-only. Spoolman stays one shared inventory for all printers. The full roster reference is [SETTINGS.md — Printers list](SETTINGS.md#printers-list).

On Unraid the template has no per-printer variables (the roster is file-only), so mount a config file: copy `config/docker.yaml`, replace its `[printer]` section with your `[[printers]]` list, mount the file and point `PRINT_HANDY_CONFIG` at it. Any single value inside a declared entry can still be overridden from the environment with the indexed spelling `PRINT_HANDY__PRINTERS__<ID>__<KEY>` — the entry's `id` uppercased in the `<ID>` position, the key in the `<KEY>` position.

Run a second *instance* instead only when you want hard process isolation between printers; that remains an alternative, not the only answer — see [More than one printer](MULTI-INSTANCE.md).

## Camera watch (visual_watch)

While a print runs, the listener checks the printer camera at layer 2, layer 5 and then periodically. A vision model classifies each frame; spaghetti, a detached part, an empty bed, a stalled print, an uncertain frame or a failed check posts the camera photo to Discord with **Pause**, **It's fine** and **Stop asking for this print** buttons. **Stop asking for this print** mutes camera posts for that print only — the next print asks again, even if it has the same filename. The watchdog is alert-only and never controls the printer: pausing, resuming or cancelling is always an operator tap (the paused post offers Resume and Cancel). A camera classification is not a guarantee of safe unattended printing.

A completely dark frame is treated as a camera problem, not a print verdict: it is never sent to the vision model and never posts as a print alert. When the watchdog reads its stills from the go2rtc fan-out and the fan-out frame is completely dark, it falls back to the direct camera snapshot once. If the frame is still dark, the check is logged and, at most once per print, a plain camera notice (with only the **Stop asking for this print** button) says the camera image is completely dark and monitoring continues. Timelapse capture applies the same rule: dark frames are skipped and counted rather than recorded, and a print whose every captured frame was dark ends with no video and a completion post that names the dark camera as the reason.

It needs a Moonraker URL, a camera snapshot (`PRINT_HANDY__SERVICES__CAMERA_SNAPSHOT_URL`; derived from the Moonraker host when empty) and a way to judge frames: a vision endpoint (`PRINT_HANDY__SERVICES__VISION_CHAT_URL` plus the `CLIPROXY_API_KEY` secret in `.env`; an empty vision URL disables roasts), a `PRINT_VISION_URL` custom endpoint, or the built-in local failure detector (`PRINT_HANDY__VISION__LOCAL_DETECTOR_ENABLED=true`, #455 — an ONNX detector that runs in this container, no endpoint or key; its pinned model, about 202 MB, downloads once into `/data/hermes/models`, SHA-256 verified). With none of the three, camera checks stay off.

The same endpoint powers the **roast**: the one-line joke added to the print-started post only (one per print, when printing begins; never on the completion post) on every platform, Telegram included. It is **enabled by default whenever an LLM/vision endpoint is available** — `PRINT_HANDY__VISION__ROAST_ENABLED` defaults to `true` in Compose and the Unraid template (`vision.roast_enabled` in YAML). Set it to `false` to turn roasts off; camera checks keep working.

An optional **auto light-off** (#516) builds on the same verdicts: set `PRINT_HANDY__VISION__AUTO_LIGHT_OFF_MINUTES` above zero (30 is the suggested value; `0`, the default, is off) and the printer's light turns on when a print starts and off again after the camera has seen an empty plate for that many minutes after a print ends — any non-empty or uncertain check restarts the wait, so a part left on the plate keeps the light on. It covers Moonraker printers (the light object named with `PRINT_HANDY__VISION__LIGHT_OBJECT`, for example `led chamber_led`, or auto-discovered when the printer has exactly one `led` / `neopixel` / `output_pin` object), Bambu Lab chamber lights, and Elegoo SDCP chamber lights (turn-on only until an SDCP camera source is verified); other families have no light to switch and `bot check` says so. On Moonraker the switch is thrown in Moonraker, so Home Assistant's Moonraker integration sees the same light state with no extra wiring. This is the one opt-in exception to the watchdog's alert-only rule, and it covers the light only — never motion, heaters or print control. Full behaviour is in [SETTINGS.md](SETTINGS.md#auto-light-off).

Two settings tune it, settable in the mounted YAML (`visual_watch` in `config/docker.yaml`), in TOML (`[visual_watch]` in `config/docker.toml`), or as Compose/`.env` variables — Compose and the Unraid template both carry them:

| YAML key | Environment variable | Default | What it does |
|---|---|---|---|
| `visual_watch.enabled` | `PRINT_HANDY__VISUAL_WATCH__ENABLED` | `true` | Camera monitoring switch |
| `visual_watch.sensitivity` | `PRINT_HANDY__VISUAL_WATCH__SENSITIVITY` | `3` | How readily the watchdog alerts, 1 (quietest) to 5 (most alerts) |

Sensitivity also sets the periodic check interval (30, 20, 15, 10 or 7.5 minutes for levels 1 to 5) and how many unchanged layers count as a stall; level 3 is the original behavior. The full per-level table is in the [project reference](PROJECT-REFERENCE.md#camera-stall-sensitivity), and every setting is listed in [SETTINGS.md](SETTINGS.md). Restart the container after a change; settings are read once at start.

## Web UI (optional, #286)

Print Handy has an optional Web UI: a simple status page plus a picker configuration page that reads and writes the same YAML settings file as everything else. It is off by default in the code settings, but **on in the container** since #708: `config/docker.yaml`, Compose and the Unraid template all serve it on `0.0.0.0:8088`. Set `PRINT_HANDY__WEBUI__ENABLED=false` to turn it off; then the supervisor starts no extra child and nothing listens. The supervisor decides from the resolved configuration (`python3 -m print_handy.webui_gate`, mirroring the listener gate), and the container healthcheck reports a Web UI row that is skipped while disabled.

Enable it in the mounted YAML (`webui` in `config/docker.yaml`), in TOML (`[webui]` in `config/docker.toml`), or with Compose/`.env` variables — Compose and the Unraid template both carry them:

| YAML key | Environment variable | Default | What it does |
|---|---|---|---|
| `webui.enabled` | `PRINT_HANDY__WEBUI__ENABLED` | `false` (container: `true`) | Web UI switch; `true` plus a port starts the supervised Web UI child |
| `webui.host` | `PRINT_HANDY__WEBUI__HOST` | `127.0.0.1` (container: `0.0.0.0`) | Host the Web UI binds to inside the container |
| `webui.port` | `PRINT_HANDY__WEBUI__PORT` | `0` (container: `8088`) | Port the Web UI binds to; `0` means no port chosen, so nothing starts |
| `webui.access_token_env` | `PRINT_HANDY__WEBUI__ACCESS_TOKEN_ENV` | `WEBUI_ACCESS_TOKEN` | Name of the environment variable holding the access token; the token itself is never a setting |
| `webui.require_auth` | `PRINT_HANDY__WEBUI__REQUIRE_AUTH` | `true` | `false` turns the access token off entirely, even on a LAN bind: anyone who can reach the port can use the Web UI, printer controls included. Only for a network you trust; not editable from the Web UI |
| `webui.controls_enabled` | `PRINT_HANDY__WEBUI__CONTROLS_ENABLED` | `false` | `true` turns on the Web UI printer control routes (`POST /api/printers/<id>/pause`, `resume`, `cancel`, `temperature`, `target`, #708). While `false` they answer 403 and send nothing. Not editable from the Web UI: arming controls is a file or environment decision |

To reach the Web UI from another machine, publish the port (Compose ships the `8088` mapping commented out; the Unraid template maps it). Protection then comes from one of three places: the `WEBUI_ACCESS_TOKEN` secret in `.env` (API requests send it as the `X-WebUI-Token` header; a browser gets the login page), or, with no token, the **first-run setup page** the first visit shows, where you set a password (stored hashed under `/data`) or tick that it should stay open on your LAN. Until that choice is made the Web UI serves nothing but the setup page, and the gate logs `first-run setup is waiting`. Existing installs with a token or `require_auth = false` never see the setup page. See [First-run setup](WEBUI.md#first-run-setup). Keep the Web UI on the LAN only: never forward its port from a router or expose it publicly. Settings changes saved in the Web UI take effect on the next container start; settings are read once at start. The full guide — pages, save flow, limits and troubleshooting — is [Web UI](WEBUI.md).

## Live camera in Discord (optional, #311)

Print Handy can show the printer camera live inside a Discord voice channel. The bot itself cannot send video — Discord's bot API is audio-only — so the live view is an **Activity**: a small web page served by this container that runs inside the voice channel of the *same* Discord application as the bot (no second bot, no second token). The full operator walkthrough (Developer Portal, tunnel, allowlist, privacy) is [Discord bot setup](DISCORD-BOT-SETUP.md#7-live-camera-activity-optional-311); this section is the container side. **Honest status: the Stage 0 live Discord spike has not run yet, so this path is built and contract-tested offline but not yet exercised live against Discord — treat it as unverified until that sitting happens.**

It is **off by default** — with it disabled the supervisor starts no extra child and nothing new listens. Two pieces run when it is enabled *and* configured:

- The **Activity server** (`python3 -m print_handy.live_activity`), a supervised child gated exactly like the Web UI (`python3 -m print_handy.live_gate`, mirroring the supervisor decision in the healthcheck's live-view row). It binds **loopback only** on `[live_stream] port`, serves the self-contained page behind server-side OAuth + allowlist auth, and ends sessions that stay empty past `empty_timeout_s`. Enabled but incompletely configured (no voice channel, no activity origin, no port, or a missing secret below) starts nothing and is a healthy, reported state — the gate lists what is missing.
- **go2rtc** now starts when *either* feature needs the camera fan-out — timelapse capture or live viewing — still as the single upstream camera pull both share. While go2rtc is running, the camera watchdog reads its stills from that same fan-out (go2rtc's loopback frame endpoint), so it never opens a second direct camera connection; when neither feature is on, the watchdog uses the direct snapshot URL as before, and a failed fan-out fetch falls back to it once so a broken stream never degrades the watchdog. Enabling live viewing in a local `WITH_GO2RTC=false` build fails the healthcheck with that reason, because the binary is not there.

Discord clients never connect to this container directly. They reach the Activity server through a **named Cloudflare Tunnel** (stable hostname) and the Developer Portal **URL mapping** for that hostname; `activity_origin` is that hostname's https origin. The tunnel connector is a **sidecar, not part of this image**:

- **Compose:** the `cloudflared` service under the `live-stream` profile (`docker compose --profile live-stream up -d`). It shares the combined container's network namespace, so its ingress target is the loopback Activity port (`http://127.0.0.1:<port>` — point the tunnel's public hostname at it in the Cloudflare dashboard). Set `CLOUDFLARED_IMAGE` to a tested connector image and `TUNNEL_TOKEN` in `.env` — without a token the sidecar exits immediately (cloudflared refuses to run), which affects only the live view.
- **Unraid:** run a separate `cloudflared` container the same way, sharing Print Handy's network namespace (Extra Parameters `--network container:<print-handy container name>`) so its `127.0.0.1` is Print Handy's; without it, loopback is the connector's own and the tunnel reaches nothing. The in-repo template carries the token as a masked field beside the other live fields for convenience, and Print Handy itself never reads it.

If the tunnel is down, only the live view is unreachable — the bot, listener and Spoolman are unaffected, and nothing about a print changes.

| YAML key / variable | Environment variable | Default | What it does |
|---|---|---|---|
| `live_stream.enabled` | `PRINT_HANDY__LIVE_STREAM__ENABLED` | `false` | Live view switch; `true` plus the rows below starts the Activity server and the go2rtc fan-out |
| `live_stream.voice_channel_id` | `PRINT_HANDY__LIVE_STREAM__VOICE_CHANNEL_ID` | empty | The one private voice channel the Activity is launched in |
| `live_stream.activity_origin` | `PRINT_HANDY__LIVE_STREAM__ACTIVITY_ORIGIN` | empty | The tunnel hostname's https origin, mapped in the Developer Portal |
| `live_stream.port` | `PRINT_HANDY__LIVE_STREAM__PORT` | `0` | Loopback port the Activity server binds; `0` means no port chosen, so nothing starts (the Unraid template pre-fills 8091 as the fixed tunnel target) |
| — | `DISCORD_CLIENT_ID` | empty | The Discord application ID (a plain identifier, not a secret) |
| — | `DISCORD_CLIENT_SECRET` | empty | Secret: the application's client secret for the server-side OAuth exchange |
| — | `LIVE_STREAM_SESSION_SECRET` | empty | Secret: signs the Activity session markers |
| — | `TUNNEL_TOKEN` | empty | Secret: the named tunnel's token, read only by the cloudflared sidecar |

GPU passthrough for the live feed follows the same opt-in rules as timelapse encoding (next section): nothing is granted by default, and the feed plan falls back to CPU encoding. **Not verified on real hardware**, and not yet verified live in Discord at all (see the honest-status note above).

## Timelapse capture (go2rtc, #246)

All three stages of #246 have landed: Stage 1 is the container plumbing below, Stage 2 captures frames during a print, and Stage 3 encodes and delivers the video with the completion post. Everything here is off by default; what Stage 1 adds is the machinery both later stages use:

### Two timelapse paths, and which settings belong to which

Print Handy has two separate timelapse paths. They share no settings, and neither requires the other:

- **In-container capture (this section)** is **off by default and opt-in**: set `PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED=true` (the Unraid template variable) or `timelapse.capture_enabled: true` in the config file. When both are set, **the environment variable wins** — the settings load order is defaults, home file, local file, then environment, later winning key by key. Captured frames and the assembled video are **local files** under the container cache (`<cache>/timelapse/<job>/`, pruned under `timelapse.local_retention_days`), and the video rides the completion post. No SSH archive is involved at any point.
- **Printer-rendered** (the moonraker-timelapse plugin on the printer): Print Handy waits a bounded time (`timelapse.render_wait_s`), downloads the rendered MP4 through Moonraker and attaches it to the completion post. The `timelapse.archive_host` / `timelapse.archive_dir` settings are an **optional extra on this path only**: when set, the rendered video is also copied to that host over SSH as a durable archive. They are **not** required for the video to be attached, and in-container capture never reads them.

Capture needs a camera source, and what counts as one depends on the printer family (#295):

- **Moonraker (Klipper) printers:** the Moonraker webcam — the source derives from `services.camera_snapshot_url` / the Moonraker webcam configuration, or set `timelapse.capture_source_url` explicitly.
- **Bambu Lab:** the A1/P1 stills camera, or the X1 family's RTSPS stream used as the go2rtc source (see [BAMBU-LAB.md](BAMBU-LAB.md)).
- **Prusa:** a camera registered with PrusaLink — a USB camera, or the CORE One's built-in camera. No registered camera, no capture.
- **Stock Flashforge (AD5X):** the printer has no built-in camera; an accessory camera the printer advertises is required, and there is no go2rtc leg (see [FLASHFORGE-STOCK.md](FLASHFORGE-STOCK.md)).
- **Elegoo SDCP:** no camera endpoint is verified for SDCP today, so capture produces no video there yet (see [ELEGOO.md](ELEGOO.md)).

With capture on but no usable source, nothing is recorded and the completion post simply carries no video. The listener log names the actual state of each path — capture disabled or produced no video, the printer-rendered lookup skipped because the archive is off, an archive failure, or a render that never arrived within the wait — instead of reporting every missing video as a render timeout.

- The image bundles **go2rtc** (upstream AlexxIT/go2rtc v1.9.15, one static binary per architecture, downloaded at build time and verified by SHA256 in `docker/go2rtc-prebuilt.sh`, like the OrcaSlicer prebuilts) and the **VAAPI driver stack** (`intel-media-va-driver`, `mesa-va-drivers`, `libva-drm2`, `vainfo`). Both are under the `WITH_GO2RTC` build toggle, which the published image sets; a local `WITH_GO2RTC=false` build omits both.
- The image bundles **ffmpeg** (the Debian package, in the runtime stage's unconditional package list, so every image variant carries it): it is the timelapse workhorse — sizing printer-rendered videos to the chat platform's limit, assembling captured frames, and building the vertical short. Debian's ffmpeg provides `libx264` (the `[timelapse] encoder` CPU path) and the `h264_vaapi` encoder for the VAAPI stack above; the NVENC/QSV encoders are in the package too and activate only when the host runtime provides the device (see the encoder table below). The image acceptance probe verifies that `ffmpeg` resolves and runs for the runtime user and that both encoders are listed (#373).
- With `timelapse.capture_enabled` set (`PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED=true`, or `timelapse.capture_enabled` in the config file), the supervisor starts go2rtc as a supervised child and the healthcheck covers it. The gate is resolved from settings (`python3 -m print_handy.go2rtc`), never guessed: capture disabled — or enabled with no camera source configured — starts nothing and is a healthy state, reported in the healthcheck like the disabled-bot case. Enabling capture in an image built without go2rtc fails the healthcheck with that reason, because the binary is not there. Since #311 the same gate also starts go2rtc for live viewing (`live_stream.enabled`) with the same generated config and the same one-upstream rule — the two features share the single camera pull.
- The supervisor generates go2rtc's config from settings — no hand-edited YAML for the standard case. The source is `[timelapse] capture_source_url` when set (an MJPEG/RTSP URL or a go2rtc `ffmpeg:` source), otherwise it derives from `services.camera_snapshot_url` / the Moonraker webcam exactly like the camera watchdog's source. The generated file lives at `<cache>/go2rtc/go2rtc.yaml`, is written `0600`, and holds the raw camera URL, which may carry credentials; Print Handy's own logs only ever show it redacted.
- go2rtc does stream normalisation only: it restreams the camera on **loopback**. Its API (1984) and RTSP (8554) bind `127.0.0.1` inside the container, are never published by Compose or the Unraid template, and its WebUI is not exposed.

### Frame capture (stage 2)

With `timelapse.capture_enabled` on, the Moonraker listener also captures frames for each print (Stage 2 of #246) and delivers the assembled video with the completion post (Stage 3, below). With capture off nothing in this section happens at all.

- **Lifecycle:** a capture session opens when a print starts (the same listener lifecycle and job identity as the completion notifications) and closes on completion, failure or cancel. It follows the print through pauses; a print already running when the listener reconnects starts capturing from the reconnect.
- **Mechanics:** snapshot polling. The listener polls a JPEG snapshot of the go2rtc-normalised stream (`http://127.0.0.1:1984/api/frame.jpeg?src=camera`) — outside the container image, where there is no bundled go2rtc, it polls the camera source's own snapshot URL directly. Capture is GPU-independent; no ffmpeg runs in this stage.
- **Trigger:** `timelapse.capture_trigger` chooses what makes a frame due — `interval` (default: every `timelapse.capture_interval_s` seconds, 5.0 by default), `layer` (each Moonraker layer change, the moonraker-timelapse semantics), or `both` (an interval floor plus layer events). The owner's trigger preference on #246 is still an open question, so it stays a setting rather than a hard-coded choice; the interval default is the printer-agnostic one.
- **Storage:** frames land under the existing cache discipline at `<cache>/timelapse/<job>/frames/frame-000001.jpg`, with a `capture.json` manifest (job, trigger, frame/byte counts, final state) beside them — per-plate jobs keep separate directories, so the Stage 3 encode and the plan combine keep working per plate. Per-job caps `timelapse.capture_max_frames` (3600) and `timelapse.capture_max_bytes` (512 MiB) bound the storage (0 disables a cap); capture stops at a cap and keeps the frames already taken. Finished and abandoned capture directories are pruned under `timelapse.local_retention_days`, like the other local timelapse copies.
- **Failure posture:** capture is a side feature and fails soft, like the Spoolman warning-only rule (#235). A dead camera, a stopped go2rtc or a full disk costs frames — logged, with the source URL redacted — and never delays, blocks or fails a print or its completion post.
- **Encoder preference:** `timelapse.capture_encoder` (`auto` | `vaapi` | `nvenc` | `qsv` | `cpu`) chooses the Stage 3 assembly encoder; `auto` tries VAAPI, then NVENC, then QSV, then CPU.

### Encode and delivery (stage 3)

When a print finishes, the captured frames are assembled into the timelapse the completion post carries:

- **Assembly:** frames play back at 30 fps into one H.264 MP4 (scaled to at most 1280 wide, `+faststart`, no audio — the same shape Discord clients already play). The encoder is picked by probing the real ffmpeg and the devices present: `timelapse.capture_encoder` is the preference (`auto` = VAAPI → NVENC → QSV → CPU), and a hardware encoder that fails — or whose output misses the size cap on both bitrate attempts (budget-derived, then 70%, the sizing shrink's strategy) — falls through to the next candidate. CPU `libx264` is always the last candidate, so a missing GPU can never fail a timelapse.
- **One selection, three call sites:** the same encoder selection and the same encoder seam drive the frame assembly, the Discord sizing shrink (`timelapse.encoder`, #314) and the multi-plate plan combine re-encode. The final video still has to fit the platform upload cap (`discord.upload_max_bytes`, 9,500,000 bytes by default on Discord) — the cap is unchanged.
- **Fallback order:** the completion post carries the in-container captured video when capture produced one; otherwise the printer-rendered moonraker-timelapse download, exactly as before (that path is not removed); otherwise no video. Any capture or encode failure degrades to the next step, and a total failure posts the completion text alone — a failed attachment is never reported as delivered, and the completion acknowledgement is unaffected.
- **Per plate:** in a multi-plate plan each plate's clip is its own captured video when one exists (its printer-rendered clip otherwise), and the clips combine into the one posted video as before.
- **Verification:** assembly, sizing and combine are covered by offline tests, including a real-ffmpeg assembly from generated frames (ffprobe-valid H.264 under the cap). The VAAPI/NVENC/QSV paths are pinned by command-construction tests and are **not verified on real hardware** — no GPU was available when this landed; the CPU path is the one exercised end to end.

### GPU passthrough and supported combinations

Hardware encoding is opportunistic and never required; without a GPU everything falls back to CPU encoding, and the container starts and runs with no GPU present:

| Combination | What you grant | Notes |
|---|---|---|
| Intel iGPU / AMD GPU (VAAPI) | `/dev/dri` passthrough (below) | The baseline. The image carries the VAAPI drivers; `vainfo` in the image reports what the granted device supports. |
| NVIDIA (NVENC) | The host NVIDIA driver plus the NVIDIA container toolkit passthrough | No CUDA toolkit or driver is baked into the image; Debian's ffmpeg already carries the `h264_nvenc` encoder, which activates only when the host runtime provides the device and driver. |
| Intel Quick Sync (QSV) | `/dev/dri` passthrough | The `h264_qsv` encoder is in Debian's ffmpeg; it activates only when the host device/driver supports it. |
| Anything else | Nothing | CPU (`libx264`) encoding, as today. |

**Not verified on real hardware:** no GPU path above has been run on physical hardware yet. The plumbing is covered by offline contract tests (pins, gates, loopback binds, image contents), and Stage 3's VAAPI/NVENC/QSV encoding is pinned by command-construction tests with the CPU fallback proven — but no hardware encode has been executed on a real GPU, and this section will say so until one has.

**Security note:** granting `/dev/dri` widens the container's access to the host — a device is not a network port, and a compromised container with GPU device access has more host surface than one without. That is why passthrough is opt-in and off by default in both deployment files, exactly like a host capability the operator chooses to grant.

- Compose: uncomment the `devices:` / `group_add:` block in `docker-compose.yml` and set `group_add` to the host's render group GID (`getent group render`).
- Unraid: add `--device=/dev/dri` to the template's Extra Parameters and make sure the container user can use the host render group; the shipped template passes no device.

## First-run configuration

1. Copy `.env.example` to a private `.env`. Set `PRINT_HANDY_IMAGE` to a tested digest. Configure Moonraker with `PRINT_HANDY__SERVICES__MOONRAKER_URL`; do not use a real printer for offline validation.
2. Set `PRINT_HANDY_DATA` to a persistent directory with `hermes`, `archive`, `records`, `cache` and `spoolman` subdirectories writable by UID/GID 1000:1000; `PRINT_HANDY_SPOOLMAN_DATA` can point the Spoolman data elsewhere, such as an existing Spoolman data folder. They map separately to `/data/hermes`, `/data/archive`, `/data/records`, `/data/cache`, `/data/spoolman`.
3. Leave the token empty for staging. At separately authorized live cutover configure token and chat ID (allowed user IDs are optional; empty lets everyone in the channel use the bot). Stop previous consumers before enabling the replacement. Live startup never authorizes heating, movement or printing.
4. Use `PRINT_HANDY__SECTION__KEY` scalar overrides from [SETTINGS.md](SETTINGS.md), or mount YAML/TOML and set `PRINT_HANDY_CONFIG`. The image sets `PRINT_HANDY_CONFIG=/app/config/docker.yaml`; a mounted file you point it at **replaces** `docker.yaml` rather than layering on it, so copy `config/docker.yaml`, edit the copy and mount that. Lists stay file-only except `delivery.methods`: `PRINT_HANDY__DELIVERY__METHODS=discord,bambu_handoff,printer` (comma- or space-separated, values from the vendor profile) enables the Bambu LAN print opt-in without a mounted file (#270). Never bake secrets into images or mount host source over `/app`.
5. One Compose service, `combined`, sets no CPU, memory or process limits, so slicing can use what the host has; it does set 256 MiB of shared memory. Capabilities are dropped and logs rotate. If you add `cpus`, `mem_limit` or `pids_limit` yourself, Print Handy sizes its thread pools (OrcaSlicer, numpy, onnxruntime, Python executors) to a CPU quota when one exists, and `PRINT_HANDY_THREADS` sets the count explicitly. A `pids_limit` below 4096 can starve OrcaSlicer: at the limit new threads fail, and Print Handy reports that the container is at its process limit when a slice cannot start or times out, rather than a bare failure or timeout. `python3 -m print_handy.doctor` shows the process count against any limit.
6. MakerWorld, Thingiverse, MyMiniFactory, Cults3D, Creality Cloud, Thangs and Nexprint links only: provide the token file and keep it on the host; see [MakerWorld account token](#makerworld-account-token). Printables links and uploads need no token.
7. Decide on timelapses. In-container capture is **off by default**: leaving `PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED` unset keeps the printer-rendered behaviour, and setting it to `true` records timelapses inside the container — which needs a camera source for your printer family (see [Timelapse capture](#timelapse-capture-go2rtc-246), including which settings belong to which of the two paths). The SSH archive settings (`timelapse.archive_host` / `archive_dir`) are a separate optional extra for printer-rendered videos, never a requirement for capture or delivery.

### MakerWorld account token

The image sets `HOME=/data`, so Print Handy reads the MakerWorld token from `/data/.config/bambu/token`. How to get and renew the token is in the [project reference](PROJECT-REFERENCE.md#makerworld-account-token). `/data/.config` is not one of the persistent paths above, so a token written inside a running container is lost when the container is recreated. Keep it on the host and bind-mount it with a local Compose override, for example a `docker-compose.override.yml` next to `docker-compose.yml`:

```yaml
services:
  combined:
    volumes:
      - ${PRINT_HANDY_DATA:-./data}/dot-config:/data/.config
```

On the host, put only the token in `dot-config/bambu/token` under `PRINT_HANDY_DATA`. Make `dot-config` and everything in it owned by UID/GID 1000:1000, with mode 0700 on `bambu` and 0600 on `token`, then recreate the container. Thingiverse links use the same mounted directory: the App Token (see the [project reference](PROJECT-REFERENCE.md#thingiverse-app-token)) goes in `dot-config/thingiverse/token`, with mode 0700 on `thingiverse` and 0600 on `token`, so it survives container recreation exactly the way the MakerWorld token does. MyMiniFactory links use it too: the API key (see the [project reference](PROJECT-REFERENCE.md#myminifactory-api-key)) goes in `dot-config/myminifactory/token`, and the download sign-in in `dot-config/myminifactory/oauth_token`, with mode 0700 on `myminifactory` and 0600 on both files. Cults3D links (metadata only — Cults' API never exposes a listing's files) use it too: `nickname:key` from your Cults account (see the [project reference](PROJECT-REFERENCE.md#cults3d-api-key)) goes in `dot-config/cults3d/token`, with mode 0700 on `cults3d` and 0600 on `token`. The login-token sites (Creality Cloud, Thangs, Nexprint, and MyMiniFactory's sign-in; see the [project reference](PROJECT-REFERENCE.md#connecting-a-login-token-site-from-chat)) use it the same way: `dot-config/<site>/token` with mode 0700 on the folder and 0600 on `token`, where `<site>` is `crealitycloud`, `thangs` or `nexprint`. When one of them needs a token, the bot posts the walkthrough in the chat: paste the token there, or save the file and press **Saved**. Mount the whole `/data/.config` directory, not just `bambu`: the image does not create `/data/.config`, and a mount point Docker has to create itself is owned by root, which leaves the rest of `~/.config` unwritable for the container user. Anything else the container keeps under `~/.config` persists in that host directory too. On Unraid, the Community Applications template has an optional **Account tokens** Path mapping from `/mnt/user/appdata/print-handy/dot-config` to container path `/data/.config`; fill it in (or, on a container created from an older template or from `docker/unraid/print-handy.xml` in this repository, add that Path mapping by hand) and make the host folder owned by 1000:1000 before the container starts.

### Upgrading a Flashforge AD5X install

As of 2.2.2 the shipped Docker default model is `klipper.generic` (it was `flashforge.ad5x`). If you run an AD5X and never set the model, add `PRINT_HANDY__PRINTER__MODEL=flashforge.ad5x` to your `.env` or container environment before upgrading. Without it the container runs as Generic Klipper, and a leftover `PRINT_HANDY__PRINTER__FILAMENT_SLOTS=4` (or the older `IFS_SLOTS=4`) stops startup with `filament_slots must be 1`. When the model comes only from the default and the saved hardware record shows several slots, `doctor` and the listener log print a warning that names this variable.

## Unraid DockerMan

Use `docker/unraid/print-handy.xml`, replacing the prior three templates. One entry runs all required processes; leave PostArgs empty. The template specifies UID/GID 1000:1000 and five persistent paths under `/mnt/user/appdata/print-handy`. Set Repository to the tested immutable digest before deployment. It publishes no port; add an approved API mapping only as described under Included Spoolman. Image HEALTHCHECK covers the combined runtime.

The runtime selector `PRINT_HANDY_ORCA_CHANNEL` (Compose, `.env.example` and the template) defaults to `stable` and accepts `preview`; the pre-#600 name `alpha` is still accepted as an alias for `preview` with a startup notice, and any other value stops container startup.

The container timezone `TZ` (Compose, `.env.example` and the template) defaults to `America/New_York` and takes any IANA name, for example `Europe/London`. It sets the container's local time: log timestamps, scheduled and timed features, and the bundled Spoolman's timestamps. The image ships the timezone database and points `/etc/localtime` at the default zone; a `TZ` set at runtime overrides it for every process, and an unknown name is logged at startup while the default is used instead. Print Handy's chat display times keep following `discord.timezone`, a separate setting.

The template and the Compose environment also carry the feature-enabling variables (#252) — the ones that turn a feature on, as opposed to tuning settings, which stay in a mounted YAML pointed to by `PRINT_HANDY_CONFIG`:

- **Camera watch:** `PRINT_HANDY__SERVICES__VISION_CHAT_URL`, `PRINT_HANDY__SERVICES__CAMERA_SNAPSHOT_URL`, `PRINT_HANDY__VISION__MODEL` and the `CLIPROXY_API_KEY` secret; see [Camera watch](#camera-watch-visual_watch).
- **Stock Flashforge LAN:** `PRINT_HANDY__FLASHFORGE_STOCK__HOST` with the `AD5X_SERIAL` and `AD5X_CHECK_CODE` secrets.
- **Bambu Lab LAN status:** `PRINT_HANDY__BAMBU__LAN_HOST`, `PRINT_HANDY__BAMBU__LAN_SERIAL` and the `BAMBU_LAN_PASSWORD` secret. The template's printer model list includes `bambulab.a1` and `bambulab.p1s` (offline only).
- **Key-protected Moonraker:** the `MOONRAKER_API_KEY` secret.
- **Discord and LLM:** `PRINT_HANDY__DISCORD__APPROVAL_USER_ID`, `PRINT_HANDY__DISCORD__TIMEZONE`, `PRINT_HANDY__LLM__CHAT_URL`, `PRINT_HANDY__LLM__MODEL` and the `LLM_API_KEY` secret.

In Compose, the settings are environment entries with their documented defaults and the secrets reach the container through the `.env` file, like `DISCORD_BOT_TOKEN`. In the template, secrets are masked fields. Leaving every one of them at its default or empty changes nothing about a running install.

This source template does not prove a Community Applications listing or an exercised Unraid installation. The host owner verifies deployment and final network mapping.

## Supported platforms (#192, #353)

One image is published, on two platforms. It is a multi-arch manifest, so `docker pull` selects the right architecture automatically:

| Build toggles | Tags | Contents |
|---|---|---|
| `WITH_ORCA=true`, `WITH_GO2RTC=true`, `WITH_BAMBU=false` | `ghcr.io/catduckgnaf/print-handy:VERSION` and `:latest` | Discord bot, Moonraker listener, bundled Spoolman API, OpenSCAD, ffmpeg, both pinned OrcaSlicer channels (stable and preview), go2rtc and the VAAPI stack (#246) |

Platforms are `linux/amd64` and `linux/arm64`. There is no `linux/arm/v7` image: upstream OrcaSlicer ships no 32-bit ARM Linux build, and 32-bit hosts are out of scope.

```bash
docker pull ghcr.io/catduckgnaf/print-handy:VERSION
```

Replace `VERSION` with a published release version. The manifest scheme starts with the first release published after this support landed; the earlier single-arch amd64 tags (2.1.1, 2.1.2 and 2.1.3 era) are unchanged and are never republished or overwritten. No image is published by a source change alone — publication is the separate maintainer dispatch described under Validation and publication, and no multi-arch image is claimed published or validated on hardware until that dispatch's evidence exists.

Up to 2.1.4 a second, slicer-free `-light` variant was also published (`:VERSION-light` and `:latest-light`). It is removed (#353): those tags remain exactly as published, as historical artifacts, and no new `-light` images are built. Anyone still pulling a `-light` tag should move to the full image, which runs on both architectures.

There is also a floating **nightly** channel for trying current code between releases:

```bash
docker pull ghcr.io/catduckgnaf/print-handy:nightly
```

Nightly is built from the `nightly` branch, which is a pointer, not a development branch: it only ever names a commit on `main` whose CI run completed successfully (see the nightly workflow under Validation and publication). Nightly images carry the same offline acceptance as release images, but they are development builds — features may be unfinished and settings may change without a release note. Pin a release `VERSION` for anything you rely on. (Nightly also had a `:nightly-light` tag up to this change; it is no longer published.)

### What happens when a slicer is needed in a no-Orca build

The published image always bundles OrcaSlicer. A locally built `WITH_ORCA=false` image (a supported build option, just not a published one) behaves exactly as it always has: slicing fails closed. The configured slicer executable (`/usr/local/bin/orca-slicer-stable` or `-preview`, selected by `PRINT_HANDY_ORCA_CHANNEL`) is absent, so a slicing attempt raises `FileNotFoundError`, produces no G-code and sends nothing to a printer, while everything that does not slice still runs. The offline acceptance harness proves this behaviour for a no-Orca image (its slice probe requires the refusal and checks that no `.gcode` file was written).

### How the ARM images are built

Each architecture is built on its matching native runner — `ubuntu-24.04` for amd64 and `ubuntu-24.04-arm` for arm64. No QEMU emulation is used for the slicer-containing image. OrcaSlicer is never source-built on either architecture: `docker/orca-prebuilt.sh` downloads the pinned official upstream AppImage for the target architecture (upstream spells arm64 as `aarch64` in the asset name) and verifies its per-architecture SHA256 before extraction. The build info recorded in the image (`/orca-<channel>-build-info`) carries the real target architecture.

## Build dimensions and slicers

All OS stages use Debian 13 slim. `APP_CHANNEL=stable|development` labels provenance, not a source switch. `WITH_ORCA=true` includes Orca; default false omits it. OrcaSlicer is never built from source: `WITH_ORCA=true` installs the pinned official prebuilt release on both channels — since #356 both pin the 2.5 line (v2.5.0-alpha first, now v2.5.0-beta), the shipped slicer (the first release whose BBL profiles cover the H2C and A2L, and whose packed preset caches Print Handy reads directly); the preview channel diverges again when upstream publishes a newer prerelease. `PRINT_HANDY_ORCA_CHANNEL=stable|preview` selects wrappers (`alpha`, the channel's pre-#600 name, is still accepted as an alias for `preview`); absent executables and invalid selections fail closed. `/usr/local/bin/orca-slicer` remains the stable path. Existing entrypoint preserves Xvfb for headless slicing.

The official prebuilt archives are downloaded and extracted only inside approved hosted image builds, never installed on a Hermes host. Each channel's version and source revision (per channel, shared by both architectures) and each architecture's asset name and SHA256 are pinned by `docker/orca-prebuilt.sh`, and the SHA256 is verified before extraction; any architecture other than amd64 or arm64 fails closed in that script. `ORCA_BUILD_JOBS=auto` bounds CPU/memory usage for the optional Bambu Studio source build only; explicit values accept 1 through 64. See [SETTINGS.md](SETTINGS.md#build-arguments).

`WITH_BAMBU=false` is the publication default; enabling pinned source-built Bambu Studio needs its own runtime acceptance. OpenSCAD (`openscad`) is included for CAD splitting and AI design (`/print-design`, #388).

## Validation and publication

The manual `.github/workflows/docker-image-validation.yml` validates ONE architecture per dispatch: choose `architecture` (`amd64` or `arm64`, built on the matching native runner). It builds the published image without pushing, runs the headless slicer smoke, and runs `tests/docker/image_acceptance.sh` against that leg's image.

The scheduled `.github/workflows/nightly.yml` publishes the nightly channel. Its promote job fast-forwards the `nightly` branch to the newest `main` commit with a successful CI run — it refuses to move the branch to anything that is not a descendant of its current commit, and it never forces the ref — then the build and manifest jobs repeat the release publisher's matrix, acceptance and push-by-digest flow for that exact commit and publish the one floating tag `:nightly`. A scheduled run whose promotion moved nothing builds nothing. A maintainer can also dispatch it manually (`promote` and `publish` both default to true; `publish=false` is the no-push candidate mode). This workflow never creates or moves release tags or `latest`.

Every `docker-publish` dispatch starts with the **release surfaces gate** (the `surfaces-gate` job, running `scripts/check_release_surfaces.py`). The gate checks that the README and the `print_handy` package version match the version being published, that the README's links resolve, and that the GitHub Pages site (`catduckgnaf/print-handy-docs`) has no drift against `docs/` at the release ref — drift as defined by the docs repo's own Docs sync check (`scripts/check-sync.sh` there), including removed pages. In candidate mode (`publish=false`) stale surfaces are only reported as warnings. With `publish=true` the gate fails the run before any image is built or pushed until the surfaces are current. The remedy is the **Docs auto-sync** workflow in `print-handy-docs` (`gh workflow run docs-auto-sync.yml --repo catduckgnaf/print-handy-docs -f ref=<version>`): it opens a sync PR there; merge it, then re-dispatch the publish. Publishing the GitHub release also triggers that sync automatically via `repository_dispatch` when the `DOCS_SYNC_TOKEN` secret is configured on this repo (a token with contents + pull-requests write on `print-handy-docs`); without it, the post-release surfaces checklist issue records the skip and the manual link. The gate applies to version releases only: nightly is a separate workflow (`.github/workflows/nightly.yml`) and is never gated.

The manual `.github/workflows/docker-publish.yml` covers the whole matrix in one dispatch. Its build job runs both legs (amd64 and arm64) on native runners; each leg builds its platform image, runs the headless slicer smoke and the offline runtime acceptance for that arch. Only with `publish=true`, and only after that leg's acceptance succeeded, does a leg log in and push — by digest only, never by tag. A final manifest job (publication dispatches only, after every leg succeeded) combines the pushed digests with `docker buildx imagetools create` into the two tags `:VERSION` and `:latest`, and writes the manifest digests to the step summary and the uploaded evidence. With `publish=false` (candidate mode) every leg still builds and must pass acceptance, and nothing is pushed and no manifest is created. Candidate inputs for the next release are `version=<next MAJOR.MINOR.PATCH>`, `publish=false`. Preserve every existing tag (2.1.1, 2.1.2, 2.1.3 era tags included); never overwrite or republish an existing tag.

Each build leg has a 350 minute timeout. Buildx uses a `docker-container` builder: the publishing legs push by digest, which the Docker driver does not implement — nightly run 40330775729 proved it the hard way, with every leg passing acceptance and then failing at the push step with exactly that error (the no-push validation workflow still uses the Docker driver, which is fine because it never pushes). Runtime fixtures must remain isolated, use dummy credentials and clean up owned containers. Evidence is retained 30 days. No live Discord, printer, Home Assistant or household Spoolman endpoints are permitted. Native tests prove lifecycle logic, not image startup, migration or slicing. Report actual workflow evidence and immutable identities separately.

Upgrade: stop prior consumers, preserve source state and immutable digest, reconcile at final cutover and recreate with the same five paths. Never use `compose down -v`. Rollback requires a compatible schema and matching old image; no automatic database downgrade is supplied.

## Attribution

Standalone foundations originated in zaheria985/print-handy commit fba5727184dd8f0070e59c8f1fc088751142a003 and merged through #132. Docker contribution #139 commit 3bb0984250c20eb336d11ba1da06633950b45182 (MIT) informed context filtering and listener heartbeat. Original notices remain. Spoolman is MIT; slicers retain upstream licenses. [DOCKER-117-PROGRESS.md](DOCKER-117-PROGRESS.md) contains historical evidence, not acceptance of this candidate.
