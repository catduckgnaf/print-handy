# Web UI

[Documentation home](README.md) · [Settings reference](SETTINGS.md) · [YAML configuration](CONFIGURATION-YAML.md) · [Docker](DOCKER.md) · [Guided setup and debugging](GUIDED-SETUP-DEBUGGING.md)

The Web UI is an optional local page for two jobs:

- **Status:** whether Print Handy, its bot, printer listener and Spoolman are up, plus the doctor checks.
- **Configuration:** a picker-based editor for the catalogued settings in the same YAML settings file Print Handy already loads.

It is **off by default**. While `[webui] enabled` is false, or no port has been chosen, the standalone server does not start and nothing listens. In the combined Docker container, the supervisor likewise starts no Web UI child in that state.

The YAML file remains the only source of truth. The Web UI does not keep a second copy of the configuration: its status comes from the effective settings, and a confirmed configuration save writes only the settings-file layer — the file named by `PRINT_HANDY_CONFIG`, or `config/local.yaml` when no file is named. See [YAML configuration](CONFIGURATION-YAML.md) for the full precedence order.

## Pages and routes

| Route | What it returns |
|---|---|
| `GET /` | The status page |
| `GET /config` | The picker configuration page |
| `GET /api/status` | The same status snapshot as JSON |
| `GET /api/printers/<id>/status` | One roster printer's live status as JSON: its identity plus the adapter's normalized status (`state`, `filename`, `progress`, temperatures, and any target/ETA/layer fields that printer's adapter reports). An unknown id is a 404; a printer that cannot be reached answers with `state: "unavailable"` instead of an error |
| `GET /healthz` | The standalone bot heartbeat verdict: `200` when it passes, `503` when it does not |
| `GET /designs` | The read-only designs catalog (#388): AI design sessions on disk, newest first |
| `GET /designs/<id>` | One design session: its final `.scad` source and file downloads |
| `GET /designs/<id>/<scad\|stl\|preview>` | That session's `.scad`, final `.stl`, or preview PNG download |
| `GET /layers` | Read-only list of sliced plates already in the archive |
| `GET /layers/<job>/<n>?layer=N` | Plate number `n` of that job (0 is the first), colored by extrusion role |

### Designs page

The designs page is a read-only catalog of the AI design sessions kept on the machine (the same 14-day window the design engine keeps under the cache directory). Each row names the session — derived from the instruction it was started with — says whether it was created from a description or is an edit (and of what), its state (in progress, ready, handed off, cancelled), its turn count, its created/updated times, and its preview when one exists. A session's own page shows the final `.scad` source and downloads for the source, the final STL and the preview PNG; files are served only from inside that session's own directory. The catalog lists what is on disk whether or not `[design]` is currently enabled, and it has no write actions: nothing there deletes, slices or prints. See [AI design with OpenSCAD](DESIGN.md#browsing-designs-in-the-web-ui).

### Sliced layers

`/layers` lists plates whose G-code is already in the archive (the `JOB.json` next to each plate names the file). Opening one draws that plate one layer at a time. Extrusion is colored by role: perimeter, infill, support, and anything else. Travel is drawn faint. Previous and next links stay inside the file's layer range, and the drawing can be panned. A job sliced into several plates (a multicolor split, for example) lists every plate. A file that is not G-code, or that has no extrusion, shows an error and no drawing. A file over 8 MB, or one with more than 200,000 moves, shows "too large to draw here" and no drawing. The page has no print, pause, or exclude control. It does not start a job and it does not change the confirm card in chat. With the Web UI disabled, the route is not served.

Paths stay inside the archive: the address is a job folder plus the plate's position in that job's own `JOB.json` (`gcode` first, then each derived plate), because every plate file has the same name. Older `/layers/<job>/plate_1.gcode` links no longer open. A path outside that plate is not opened.

### Status page

The status page reports:

- an overall **ok** or **attention needed** result;
- the Print Handy version and host;
- the configured printer name, model and connection;
- the bot platform and mode, its heartbeat reason, and whether the heartbeat says it is connected;
- whether the printer listener is required, and its health result when it is;
- whether Spoolman is bundled or external, its configured URL, and the result of its `/api/v1/health` probe;
- the other configured service URLs; and
- the doctor checks as rows, each with an `ok`, `skip`, `fail`, or `warn` status and its detail.

With a multi-printer roster configured, the status page also lists the roster read-only — each printer's id, name, model, connection, channel and endpoint — and `/api/status` carries the same rows under `printers`.

For external consumers (the Home Assistant integration, dashboards), `GET /api/printers/<id>/status` serves one printer's live status (#664): the same identity fields as a roster row plus a `status` object holding the printer adapter's normalized status, read live through that printer's own connection — `state`, `filename`, `progress`, `hotend_c`, `bed_c`, and, where the adapter provides them, `hotend_target_c`, `bed_target_c`, `remaining_min`, `layer` and `total_layer`. It sits behind the same access-token auth as `/api/status` and is read-only — it sends nothing to the printer. An unknown id is a 404. A configured printer that is offline or unreachable still answers 200 with `status.state` set to `"unavailable"` and a `detail`, so a consumer polling every printer does not lose the whole poll to one dead machine; a printer on the `offline` connection reads the same way. **Stage 2e decision (#337): the roster is not editable here.** The field catalog addresses global fields only in this version, so per-printer changes stay where the roster is declared: the config file, the CLI setup wizard, or the guided setup's review step. A per-printer addressing scheme for the catalog is a future stage's decision, deliberately not pre-empted by this one.

The Web UI doctor rows reuse the local doctor checks — settings, printer model, Orca templates, core modules, hardware record, Orca CLI, configured Moonraker and Spoolman services, bot mode, and stray state files. Two command-line doctor checks are deliberately not run by the page: Discord login and the shared-token comparison. Both need the bot token, and the login check can make a live network call; run `python3 -m print_handy.doctor` when you want those checks.

`/healthz` is narrower than the status page. It evaluates the bot heartbeat only: a `503` can mean the bot heartbeat is missing or stale while the Web UI itself is still serving. The Docker healthcheck therefore treats either a `200` or a `503` answer from `/healthz` as proof that the Web UI is serving, while the separate bot row owns the heartbeat verdict. An unreachable Web UI, an unresolved configuration, or any other HTTP status makes the Web UI healthcheck row fail.

## Enable it on a native or manual install

Add a `[webui]` section to the installation's settings file. For a native install, that is `config/local.yaml`:

```yaml
webui:
  enabled: true
  host: 127.0.0.1
  port: 8080
  access_token_env: WEBUI_ACCESS_TOKEN
```

`8080` is only an example; any free port from `1` to `65535` works. The code default is `port: 0`, which means no port has been chosen yet. Do not edit the checked-in `config/home.yaml` for a local choice if a local settings file is available.

From the repository root, start the server in the foreground:

```bash
python3 -m print_handy.webui
```

Then open `http://127.0.0.1:8080/`. The command keeps running until Ctrl-C. It is a separate process from the bot and listener; starting the Web UI does not start either of them.

This loopback example needs no access token because only the same machine can reach the listener. To bind a LAN address instead, set `host` to the machine's LAN address or `0.0.0.0`, and put a token in the environment the server process inherits:

```bash
export WEBUI_ACCESS_TOKEN='<choose-a-long-random-token>'
python3 -m print_handy.webui
```

Never put the token value in YAML. If Print Handy runs under systemd or another service manager, the token belongs in that service's protected environment, not in the settings file; restart the Web UI process after changing its environment.

## Enable it with Docker Compose

The container path is covered in [Docker](DOCKER.md#web-ui-optional-286). In `.env`, set:

```dotenv
PRINT_HANDY__WEBUI__ENABLED=true
PRINT_HANDY__WEBUI__HOST=0.0.0.0
PRINT_HANDY__WEBUI__PORT=8088
WEBUI_ACCESS_TOKEN=<choose-a-long-random-token>
```

Then uncomment the Web UI port example in `docker-compose.yml`:

```yaml
ports:
  - "8088:${PRINT_HANDY__WEBUI__PORT:-8088}"
```

Recreate the container:

```bash
docker compose up -d
```

On startup, the supervisor's Web UI gate reads the resolved configuration. It starts `python3 -m print_handy.webui` only when the Web UI is enabled and a port is set. If the configuration cannot be resolved, the gate stops the container rather than guessing. If the Web UI is enabled on a non-loopback host without its access-token secret, the gate reports the blocked start with the fix in the log and the container carries on without the Web UI (#440) — the standalone server still refuses that bind outright, because a LAN-reachable Web UI must never run unauthenticated.

The Web UI can also be enabled in the selected Docker YAML or TOML file (`webui` in `config/docker.yaml`, or `[webui]` in `config/docker.toml`). Environment values override the file, so an environment setting left at the Compose default can be the reason a file change appears to have no effect; the configuration page marks those fields as environment-overridden.

## Enable it on Unraid

In the in-repo Unraid template:

1. The template sets **Web UI** (`PRINT_HANDY__WEBUI__ENABLED`) to `true` for you; set it to `false` only to turn the Web UI off entirely.
2. Leave **Web UI bind host** at the template default `0.0.0.0` when the page must be reachable through the port mapping.
3. Keep **Web UI port setting** (`PRINT_HANDY__WEBUI__PORT`) and the **Web UI port** mapping matched; the template uses `8088` for both.
4. Set the masked **Web UI access token** (`WEBUI_ACCESS_TOKEN`) to a long random value. This step is required: with no token, the Web UI does not start (the container keeps running and its log explains the fix).
5. Apply/restart the container.

The template's **WebUI** link points at the mapped port; the browser is redirected to the login page, where the token from step 4 signs it in. The port mapping exists even while the Web UI is disabled or blocked; then, nothing answers on it.

The published Community Applications template is separate from the in-repo template and follows the normal release-time sync; do not treat this guide as proof that a published template has already picked up a source change.

## Access token and browser access

The token's value is a secret held only in the environment variable named by `webui.access_token_env`. The default name is `WEBUI_ACCESS_TOKEN`.

- **Loopback bind** (`127.0.0.1`, another loopback address, `::1`, or `localhost`): the token is optional. If the named environment variable is set, the token is still required for requests.
- **Any other bind host:** the token is required. If it is empty, the server refuses to start.
- **`webui.require_auth = false`** (`PRINT_HANDY__WEBUI__REQUIRE_AUTH=false`): the explicit opt-out for a network you trust. The token is neither required nor checked on any bind host, even if it is set, and there is no login page. Anyone who can reach the port gets the whole Web UI, including printer controls and configuration writes (the per-form `_csrf` check still applies). The Docker gate logs `authentication OFF` at every start. The setting is deliberately not a Web UI field: turning auth off is a file or environment decision.
- Requests that need the access token send it as the `X-WebUI-Token` header. For example:

  ```bash
  curl -H "X-WebUI-Token: $WEBUI_ACCESS_TOKEN" http://127.0.0.1:8080/api/status
  ```

- A missing or wrong header on an API request returns `401` JSON with `missing or wrong X-WebUI-Token`. A browser navigation (a GET for a page with `Accept: text/html`) redirects to `/login` instead (#440).

**Browser sign-in:** a normal browser cannot add a custom header to an address-bar request, so browsers sign in at `/login` instead. Open the Web UI address (the Unraid **WebUI** link lands on `/`, which redirects to the login page while signed out), enter the access token, and the server sets a `webui_session` cookie (`HttpOnly`, `SameSite=Strict`) that authorizes page views (`GET` requests) for 12 hours. Sessions live only in the server's memory, so restarting the server signs everyone out; `POST /logout` ends your own session immediately. With no token configured there is nothing to sign in to and `/login` simply redirects to the status page. Scripts and API clients keep using the `X-WebUI-Token` header exactly as above — the cookie is a browser convenience, not a second token. There are no user accounts and no HTTPS listener, so do not remove the token from a LAN-reachable bind just to make a page load.

Configuration form posts have a second, narrower credential: the server generates a form token when it starts, keeps it only in memory, and embeds it as the `_csrf` hidden field. A `POST` is accepted with either the `X-WebUI-Token` header or that form token. The form token is not the access token and does not authorize `GET` requests, and a session cookie alone never authorizes a post either — a signed-in browser still submits the form's own `_csrf` value. On loopback with no access token configured, the form token is still required for posts, so another page in the operator's browser cannot silently submit configuration changes.

Keep the Web UI on a trusted LAN or reach it over a VPN. Never forward its port from a router or expose it directly to the public internet.

## Use the configuration page

Open `/config`, or follow the **configuration page** link from the status page. The form is built from the Web UI field catalog and is grouped as:

- Printer
- Platform and chat
- Services
- Model endpoints
- Timelapse
- Images and camera watch
- Live camera (Discord)
- Slicing and delivery
- Logging
- Web UI

Pickers appear wherever the setting has a defined choice set — for example the printer registry, bot mode and platform, slicer and delivery methods offered by the configured printer's vendor, timezone, log level and format, and the timelapse and camera type pickers. Options are listed A to Z by the text shown (a printer's name, not its key). Toggles are checkboxes. Numbers use bounded numeric inputs; genuinely free values such as IDs, hosts and URLs remain text inputs and are checked by the same settings validators Print Handy uses at startup.

The two type pickers are explicit settings, not page-only choices, and both are wired to the paths they name. **Timelapse mode** is `rendered` (the printer renders it with moonraker-timelapse; the in-container capture below is preferred when it produced a video), `container` (Print Handy captures and encodes it in the container; the printer-rendered path is not used), or `off` (no timelapses — capture and the rendered path both stop); an empty file value derives `rendered` when a Moonraker URL is configured, otherwise `container` when in-container capture is on, otherwise `off`. The **In-container capture** toggle beside it is the capture switch itself: in `rendered` mode it decides whether capture runs, in `container` mode capture runs regardless, and `off` stops it whatever the toggle says. **Camera source** is `auto` (follow the configured printer vendor's rule), `moonraker` (the Moonraker webcam snapshot only), `bambu` (the printer's own Bambu camera), `go2rtc` (the go2rtc fan-out only), or `none` (no camera images anywhere — no watchdog stills, no timelapse capture, no fan-out); the watchdog stills, the timelapse capture source and the Bambu camera paths all honor the pin and name it when they refuse. **RTSP stream URL** (`timelapse.capture_source_url`) is the address go2rtc ingests when you have an `rtsp://` camera; leave it empty to keep using the snapshot. **Printer URL** is the one address field for the selected model: Moonraker, OctoPrint (including an OctoPi on another computer), or the LAN hostname for Bambu, Prusa, stock Flashforge, and Elegoo SDCP. For a Bambu Lab model the **Printer serial** field shows with the URL — the LAN connection needs the pair, and settings validation refuses one without the other. The ZMod delivery format field is shown only for a Flashforge ZMod model.

The **Live camera (Discord)** group's feed values work as one switch. While the feed height, frame rate, bitrate cap and encoder all hold their defaults (720, 15, 2500, auto), the live view is the camera stream relayed as it arrives. Change any one and go2rtc publishes a second, transcoded stream for the live view with all four values applied — ffmpeg re-encodes the loopback restream, so the camera still gets one upstream pull, and timelapse capture and watchdog stills keep the camera's own stream. The encoder preference is resolved against a probe of the machine's ffmpeg (hardware first, CPU last), the same candidate order as the timelapse assembly encode.

The Services group also carries the event-notification fields (#622): the **MQTT events** switch with its broker host, port, topic root and username, and the **ntfy events** switch with its server URL and topic. Neither channel's credential is a field — the MQTT password is `MQTT_PASSWORD` and the ntfy access token is `NTFY_ACCESS_TOKEN`, both in the environment only. The **Model endpoints** group carries the chat, design and agent endpoint URLs and model names; their API keys likewise stay in the environment. **Plate spacing** (Slicing and delivery) is the auto-layout spacing preset.

Every field shows its effective value and a provenance badge:

| Badge | Meaning |
|---|---|
| `model default` | Supplied by the selected printer model's defaults |
| `home config` | Supplied by `config/home.yaml` or its legacy TOML fallback |
| `settings file` | Supplied by the selected settings file |
| `environment override` | Supplied by an environment variable |
| `default` | Supplied by settings validation when no layer names the key |

### Save flow

1. Change one or more fields and choose **Preview changes**. This sends `POST /config/preview`; it writes nothing.
2. Conversion errors — for example a non-number in a numeric field — return the form with the error beside that field.
3. If the values convert, the change set is validated through the real settings loader on a throwaway copy of the settings file. A refusal returns the form with the loader's message beside the field that fails on its own, or as a general error when the failure belongs to the combination. The real file stays byte-identical and no backup is made.
4. A valid preview shows the exact setting, current value and new value for every change. If nothing differs, the form says so instead of showing an empty preview.
5. Choose **Save changes** only from that preview. It sends the confirmation form to `POST /config`; a direct save without the preview confirmation is refused.
6. The write is validated again, then landed atomically. When an existing settings file is replaced, the previous file is kept beside it as `<settings-file>.bak-YYYYMMDD-HHMMSS` (with a numeric suffix if that name is already used).
7. The result page lists the exact `section.key` pairs that changed, or says that the file already held the submitted values.

The preview form carries the parsed state forward as hidden inputs, and the confirmation re-parses exactly the fields the preview named. That preserves details such as an unchecked toggle or an emptied multi-select instead of treating their absence as “not part of this save.”

### Environment overrides stay locked

A field whose effective value comes from the environment renders disabled and is labelled as overridden. It is skipped when the form is parsed — even a hand-crafted submission naming it changes nothing. To change that setting, change or unset the `PRINT_HANDY__SECTION__KEY` variable (or its supported legacy name), then restart Print Handy. The Web UI never shadow-writes an env-overridden value into the file merely to make the file look complete.

### Restart required

Every configuration change needs a restart of the process or container that uses it. Settings are read once at process start; the result page says **Restart required** and never claims a change was applied live. This includes changes to the Web UI's own `enabled`, `host` and `port`: the already-running server does not rebind, stop, or pick up a new access-token environment until it is restarted.

### Comments and file shape

A save rewrites the settings file from its parsed mappings. Comments in a hand-edited file are **not guaranteed to survive**, and list or table values outside the catalog are preserved as parsed data rather than as their original text. The page states this before saving. Keep a separate copy when the comments in a hand-maintained file matter.

If the selected settings file is a legacy TOML file, the Web UI refuses to rewrite it as YAML. Point `PRINT_HANDY_CONFIG` at a YAML file, or edit the TOML file by hand.

## What the Web UI cannot do

- It cannot control a printer: no print, pause, resume, cancel, movement, heating, macro, or job-management actions.
- It cannot display, accept, or write secret values. Bot tokens, API keys, passwords, access codes and the Web UI token itself stay in the environment. Settings that merely name an environment variable, such as `webui.access_token_env`, are not picker fields.
- It cannot edit the environment, the home configuration, or any layer other than the selected settings file.
- It cannot edit settings outside the catalog. File-only tables and lists such as `material_temps`, `nozzle_temp_offsets` and the `[[printers]]` roster remain YAML/TOML edits (or the setup wizard / guided setup for the roster); `delivery.methods` is in the catalog because it has a vendor-defined picker.
- It does not replace Discord, Telegram, Matrix, or the CLI setup wizard. Those surfaces and the Web UI work over the same settings; see [guided setup and debugging](GUIDED-SETUP-DEBUGGING.md) and [YAML configuration](CONFIGURATION-YAML.md).
- The timelapse archive `ssh_key` field is only a filesystem path. It never contains or uploads private-key contents.

## Troubleshooting

| Symptom | Check |
|---|---|
| `webui: not started: [webui] enabled is false` | Set `webui.enabled` to `true` in the effective settings file, or `PRINT_HANDY__WEBUI__ENABLED=true`; then restart the process/container. |
| `webui: not started: [webui] port is 0 (unset)` | Port `0` means no port has been chosen. Set `webui.port` (the deployment examples use `8080`), then restart. |
| `webui: refusing to bind ... a non-loopback bind requires an access token` | Set the secret named by `webui.access_token_env` (default `WEBUI_ACCESS_TOKEN`) in the server/container environment, or bind `127.0.0.1`. In Docker, the gate instead logs the blocked start (next row) and the container carries on without the Web UI. |
| `webui gate: enabled on ... but $WEBUI_ACCESS_TOKEN is empty ... it was not started` | The container is healthy; only the Web UI is off. Set `WEBUI_ACCESS_TOKEN` (Unraid: the **Web UI access token** field) to a long random value and restart, or bind `127.0.0.1` to keep the Web UI inside the container. |
| `401` with `missing or wrong X-WebUI-Token` | In a browser, open `/login` and enter the access token (sessions last 12 hours and end when the server restarts). For scripts, send the exact token value in the `X-WebUI-Token` header. See [Access token and browser access](#access-token-and-browser-access). |
| `403` with `missing or wrong form token` on a configuration post | Load `/config` from the running server and submit its own form, so the current in-memory `_csrf` value is included. Restarting the server creates a new form token, so an old page must be reloaded. |
| The Docker container stops after enabling the Web UI | Read the container log for the gate or server refusal. An unresolved settings file still stops the container; a non-loopback bind with no `WEBUI_ACCESS_TOKEN` no longer does (the gate logs the blocked start and the container carries on without the Web UI — #440). Fix the named setting and restart. |
| The Web UI healthcheck row says `enabled but no port chosen; not started` | This is an `ok` row, not a serving Web UI. Choose a port and restart if you intended to run it. |
| The Web UI healthcheck row says `disabled` | This is an `ok` skip. Nothing is listening, as configured. |
| The status page says **attention needed**, but `/healthz` is `200` | They answer different questions: `/healthz` is only the bot heartbeat, while the status page also includes the listener, Spoolman and doctor rows. Read the failing row on `/` or `/api/status`. |
| `/healthz` is `503` | The Web UI answered, but the bot heartbeat is missing, stale, invalid, disconnected, or otherwise fails the bot-health evaluation. Check the bot row and the standalone bot process. |
| A field cannot be edited | Its badge says `environment override`, or the value is not in the catalog. Change the environment variable and restart, or edit the YAML directly for an uncatalogued setting. |
| A save is refused and the file did not change | Read the error beside the field or at the top of the form. Invalid combinations are rejected before the atomic write; the file remains byte-identical. Correct the value and preview again. |
| Saving worked, but behaviour did not change | Restart the Print Handy process or container that consumes the setting. A running process keeps the settings it loaded at start. |
| A legacy TOML save is refused | The Web UI writes YAML only. Select a YAML settings file with `PRINT_HANDY_CONFIG`, or keep editing the TOML file manually. |

## Printer controls (#708, control endpoints phase)

`POST /api/printers/<id>/pause`, `/resume`, `/cancel`, `/temperature` and `/target` send one command to one roster printer through the same paths chat uses: Moonraker's print pause/resume/cancel and `M104`/`M140` for Moonraker printers, the adapter's own `control()` for every other family. A family with no existing temperature path answers 501 ("not supported"); nothing is sent.

Guard:

- **Off by default.** Only `[webui] controls_enabled = true` turns them on. While off, every control route answers **403** with the reason and sends nothing. The switch is deliberately not a Web UI field.
- **Auth.** The `X-WebUI-Token` header only, like `GET /api/printers/<id>/status`; a browser login cookie does not authorize a control. Bodies must be `application/json`, so a cross-site HTML form cannot post here.
- **cancel** also needs the body `{"confirm": true}` (400 otherwise).
- **temperature** takes `{"heater": "nozzle" | "bed", "target": <°C>}`; `0` turns the heater off. The target is refused (400), never clamped, above the printer's limit: for the nozzle the lower of the fitted nozzle type's safe maximum and the model's `max_hotend_c`; for the bed the model's `max_bed_c`, or the hottest configured material bed temperature when the model has no such fact.
- **target** takes `{"hotend_c": <°C> | null, "bed_c": <°C> | null}`, the body the Home Assistant integration (ha-print-handy) sends: both heaters in one call, `null` leaving that heater alone, at least one a number. Same limits as `temperature`; every value is checked before anything is sent, so one bad value sends nothing. If the second heater fails after the first was sent, the 502 reason names what was already sent.
- Every control re-reads the printer first. An unknown id is a **404** in the status route's shape, an unreachable printer an explicit **502**, and a printer in the wrong state (resume while printing, say) a **409**; none of them send anything.

```bash
curl -X POST -H "X-WebUI-Token: $WEBUI_ACCESS_TOKEN" -H "Content-Type: application/json" \
     -d '{"confirm": true}' http://127.0.0.1:8765/api/printers/flashforge-ad5x/cancel
```

Still open from #708: Docker defaults, Bearer auth, extended telemetry, and the camera proxy.
