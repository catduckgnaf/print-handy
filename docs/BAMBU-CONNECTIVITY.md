# Bambu Lab live connectivity, research for #187, decision input for #127

[Documentation home](README.md) · [Bambu Lab printers, offline](BAMBU-LAB.md) · [Printer profiles](PRINTER-PROFILES.md)

**Status: the research below was the decision input for #127/#187; the staged LAN path it recommended is now implemented.** The `bambulab` vendor is `connection="bambulab_lan"`: read-only MQTT telemetry, FTPS upload and gated print start are implemented and documented in [Bambu Lab printers](BAMBU-LAB.md). This document remains the evidence record those stages were built from, read its stage-by-stage claims as the 2026-10-25 research position, not as a description of today's code. No printer was contacted and no credentials were used to produce the original research; live printer tests remain separately scoped and approved, exactly as #127 and #187 state.

How to read the evidence labels, following [BAMBU-LAB](BAMBU-LAB.md):

- **Verified**: read directly in the named source at the pinned commit or official page listed in [Sources](#sources), on 2026-10-25.
- **Not verified**: third-party only, conflicting between sources, or inferred from verified facts. Each one is a candidate for the live-test list in [Unknowns](#unknowns-that-need-a-live-test), not a basis for a claim of support.

## Recommendation in one page

Build **LAN-only, in stages, A1 first**, and only after a maintainer decision on this document:

1. **Read-only telemetry first.** Local MQTT over TLS on port 8883, user `bblp`, password = the printer's LAN access code, subscribing to `device/<serial>/report` and requesting a full snapshot with `pushall` on every connect. Monitoring is the one thing Bambu's 2025 authorization change explicitly leaves open without authorization, so this stage works in plain LAN Mode and does not need Developer Mode.
2. **FTPS upload second.** Implicit-TLS FTP on port 990, same `bblp` / access-code pair, uploading the already-validated `.gcode.3mf` and verifying the remote size before anything calls it delivered. Python's standard library (`ftplib.FTP_TLS`) covers this; no new dependency.
3. **Print start last, and only in Developer Mode.** An MQTT `project_file` command naming a file already on the printer, sent only through the existing per-job bed-clear approval, readiness checks and duplicate-start protection. On current firmware, control requires **LAN Mode + Developer Mode** (see [The 2025 authorization change](#the-2025-authorization-change-lan-mode-and-developer-mode)); Developer Mode disconnects the printer from Bambu Cloud and Bambu Handy, which is a user-visible cost that must be stated wherever live support is offered.
4. **No cloud path.** Cloud MQTT/HTTP needs a Bambu account token obtained through an interactive login (with email/SMS code or TFA), the token lasts about 90 days with no refresh flow in any inspected client, and the undocumented endpoints change without notice; the Home Assistant integration's most recent commit at inspection time is a repair for exactly such a change. Cloud buys remote access, not openness, and control through it is restricted by the same authorization system.

Staging matters for safety: in Print Handy's profile system, adding the `printer` delivery method is what offers the "Slice and print now" button. That method must not be added until stage 3 actually works; stages 1–2 keep today's delivery methods unchanged. See [Mapping to Print Handy](#mapping-to-print-handy).

## Connection modes

| Mode | Transport | Credentials | What it gives | Verified |
|---|---|---|---|---|
| Local MQTT | MQTT/TLS to the printer IP, port 8883 | User `bblp`, password = LAN access code; serial number (upper case) used in topics, not in auth | Telemetry push, and, subject to firmware mode, commands including print start | Yes: pybambu `bambu_client.py` at the pinned commit |
| Local FTPS | Implicit-TLS FTP to the printer IP, port 990, passive ports 50000–50100 | Same `bblp` / access code | Upload/download/list files on the printer's storage | Yes: official ports page; pybambu and bambulabs_api clients |
| Local camera | A1/P1: proprietary JPEG stills over TCP 6000. X1: RTSPS on port 322 | Same access code (6000 protocol embeds it in an auth packet; RTSP as the password for `bblp`) | Chamber images / live view | Yes: pybambu, OpenBambuAPI. Print Handy scope: A1/P1 stills implemented in #294 (`print_handy/printers/bambulab/camera.py`); X1 stills read from #246's go2rtc fan-out of the RTSPS stream (`bblp` / access code, port 322, path `/streaming/live/1`) rather than an in-repo RTSP stack (#295) |
| Cloud MQTT | MQTT/TLS to `us.mqtt.bambulab.com` (or `cn.mqtt.bambulab.com` for China), port 8883 | User `u_<user id>`, password = account access token | Telemetry and tasks for cloud-bound printers, from anywhere | Yes: pybambu `bambu_cloud.py`, OpenBambuAPI |
| Cloud HTTP API | HTTPS to `api.bambulab.com` | `Authorization: Bearer <access token>` | Login, device list (which itself returns each printer's LAN access code), slicer settings, task history | Yes: pybambu, OpenBambuAPI |
| Bambu Connect | Desktop app; third-party slicers hand it files via a `bambu-connect://import-file` URL scheme | Bambu's own authorization flow | The vendor-sanctioned path for third-party slicers to send prints under the authorization system | Yes: Bambu blog and Bambu Connect wiki. It is an application handoff, not a protocol Print Handy can implement against |

Two naming traps, both verified: the Python library people call "pybambu" is **not** the dormant standalone repository (last commit 2024-01-27, README says it is out of date); the maintained code is vendored inside the Home Assistant integration at `custom_components/bambu_lab/pybambu/`. And `bambulan`'s maintained upstream is `gonzalop/bambulan`; the `srausser/bambulan` fork named in early #187 leads is dormant, cite the upstream.

## Official ports

From Bambu Lab's own [printer network ports page](https://wiki.bambulab.com/en/general/printer-network-ports) (**verified** there; the protocol labels in the third column are from the clients in [Sources](#sources), because the wiki does not label them):

| Use | Port | Notes |
|---|---|---|
| LAN MQTT | 8883/TCP | Cloud MQTT is also 8883/TCP, to Bambu's brokers |
| LAN FTP | 990/TCP plus passive 50000–50100/TCP | Implicit-TLS FTPS; an explicit-TLS client hangs |
| LAN video | 322/TCP and 6000/TCP | 322 = X1 RTSPS; 6000 = A1/P1 JPEG stills |
| Discovery | SSDP 1990/2021 | See [Discovery](#discovery-reconnect-and-state), in practice clients mostly use manual or cloud discovery |
| NTP | 123 | Cloud/LAN |
| Cloud only | HTTP API 80/443/8080, device binding 3000, remote-video ranges | Irrelevant to a LAN-only integration; in LAN Mode the printer does not talk to the cloud at all, per the same page |

Port **12345**, sometimes cited in community posts as a Bambu port, was **not found** on the official ports page, the LAN-mode page, or in OpenBambuAPI at its pinned commit. Do not document it as a Bambu port on current evidence.

## The 2025 authorization change: LAN Mode and Developer Mode

This is the fact the whole design turns on. Sources: Bambu's [firmware authorization announcement](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/) (2025-01-16), its [third-party integration update](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/) (2025-01-20), and the [LAN Mode wiki page](https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode); all **verified**.

- **LAN Mode** moves the printer off the cloud: same-LAN operation via Bambu Studio and the access code. The wiki lists what it disables: starting prints remotely from outside the LAN, Bambu Handy, and Print History. Enabling it: A1/A1 mini, Settings, page 3 → LAN Only Mode; P1P/P1S, Settings → WLAN → LAN Only Mode (the access code is shown there). An X1 additionally needs an SD card inserted to start a LAN print from Studio; the wiki does **not** say that for A1/P1S (**not verified** either way for our models).
- **Authorization Control (2025)** then restricts *control* even on the LAN: initiating a print, motion/temperature/fan/AMS/calibration control, binding, remote video and firmware upgrades require authorization. Announced first for X1 firmware 01.08.03.00+, with P/A series "planned" at announcement time. Two things are explicitly **not** restricted: MQTT status push / monitoring (the blog names Home Assistant as the example), and starting a print from the SD card itself.
- **Developer Mode** is the escape hatch added a week later: an optional toggle *inside* LAN Mode (X1/P1/A1/A1 mini) that leaves the MQTT channel, live stream and FTP open to third-party software. It must be switched on at the printer, Bambu provides no support for it, and the user takes responsibility for network security. The maintained clients agree on the consequence: with Developer Mode on, unsigned LAN commands are accepted; without it, they are rejected.
- **Detection is in-band.** pybambu (**verified** in its `const.py`/`models.py`) reads the push field `print.fun` as a bitfield; bit `0x20000000` (`MQTT_SIGNATURE_REQUIRED`) set means Developer Mode is off, and the integration then *refuses to send writes at all* ("All control actions are blocked") rather than attempting signed commands. Its firmware floors for that behavior are A1 ≥ 01.05.00.00, P1 ≥ 01.08.02.00, X1 ≥ 01.08.50.32. Those floors are **verified as what the client code encodes**; Bambu's own pages inspected here do not state A1/P1 floors, and third-party pages conflict (one puts an X-series floor at 01.08.05.00), so the floors for a specific printer are a live-test item.
- That the FTPS upload specifically fails without Developer Mode is an **inference** from "Developer Mode leaves FTP open", not a direct official quote. It is consistent across clients, but it belongs on the live-test list too.

Practical consequence for #127: *read* support can be promised for LAN Mode alone; *write* support (upload, start, pause, cancel) can only be promised for **LAN Mode + Developer Mode + access code**, on the A1 first and the P1S second, and the loss of Handy/cloud while so configured is part of the offer, not a footnote.

## MQTT protocol

All from pybambu at the pinned commit and OpenBambuAPI (**verified** unless noted).

- **Topics.** Subscribe `device/<serial>/report`; publish `device/<serial>/request`. Serial is upper-cased. Message bodies are JSON with a single top-level category key, `{"pushing": …}`, `{"print": …}`, `{"info": …}`, `{"system": …}`, plus a `sequence_id`.
- **On connect**, clients subscribe, then publish `{"info": {"command": "get_version", …}}` and `{"pushing": {"command": "pushall", …}}`, in that order.
- **Pushall is mandatory on A1/P1.** Those families report *deltas*, only changed values, so state is incomplete until a `pushall` snapshot (`print.command == "push_status"`, with `msg == 0` marking the complete snapshot) arrives. X1 sends full state each time. OpenBambuAPI cautions against requesting pushall more often than every 5 minutes on P1P because it lags the printer; on-connect and on-watchdog (below) are the established uses, not polling.
- **Telemetry (`print` push).** `gcode_state` (`IDLE`, `RUNNING`, `PAUSE`, `FINISH`, `FAILED`, `PREPARE`, …), `mc_percent`, `mc_remaining_time` (minutes), `layer_num` / `total_layer_num`, `subtask_name`, `gcode_file`, nozzle and bed current/target temperatures, chamber temperature, fan speeds (raw 0–15), `spd_lvl` / `spd_mag`, current stage `stg_cur`, `print_error` and `hms[]` errors, AMS block and external-spool `vt_tray`, `wifi_signal`, and the `fun` bitfield above. This maps cleanly onto Print Handy's normalized status shape (`state`, `filename`, `progress`, `hotend_c`, `bed_c`) with room to spare, progress is a direct percentage, unlike Moonraker's file-position estimate.
- **Per-model differences that matter.** A1: AMS **Lite** (detected from reported hardware in `get_version`, not from the model name; an A1 can now also carry a regular AMS, so detection must follow the report). P1S: regular AMS, up to 4 units × 4 trays, `tray_now = ams_id*4 + tray_id` (254 = external spool). A1/P1 have no chamber-temperature sensor feature and no door sensor in the client feature matrix (X1 has both); their payloads can still carry a meaningless chamber value (a fixture shows `5`), so Print Handy must gate fields on the feature matrix, not on presence. Model codes in reports: A1 = `N2S`, A1 mini = `N1`, P1S = `C12`, P1P = `C11`.
- **Sequence IDs disagree between sources, flagged, not resolved.** OpenBambuAPI documents `sequence_id` as a string incremented per command and echoed in the report, with a case-insensitive `result`. pybambu in practice sends `"0"` for almost everything. For telemetry this does not matter; for confirming that *our* print-start was accepted, it does, see the unknowns.

## Commands and print start

Published to `device/<serial>/request` (**verified** in pybambu `commands.py` / `coordinator.py`):

- Pause / resume / stop: `{"print": {"command": "pause" | "resume" | "stop", …}}`, gated client-side on state (pause only while running, resume only while paused).
- Speed: `{"print": {"command": "print_speed", "param": "1".."4"}}`.
- Temperatures and fans are **not** dedicated commands: they are sent as raw G-code via `{"print": {"command": "gcode_line", "param": "M104 S…\n"}}` and similar. That is a sharp edge for Print Handy: a `gcode_line` channel is arbitrary code execution on the printer, and any design must allowlist what may be sent rather than exposing the channel generally.
- AMS filament change/settings, skip-objects and similar exist; none are in Print Handy's staged scope. The chamber light is the one exception, wired since the #516 follow-up through `system/ledctrl` (`led_node: chamber_light`, `led_mode` on/off) purely as the auto light-off's target, on at print start, off after a sustained empty-plate verdict, with availability read from the printer's own `lights_report` telemetry, so a model that reports no chamber light is never switched.
- There is **no standalone calibration command** in the inspected library; calibration appears only as boolean flags on print start and as reported stages.

**Print start is `project_file`, and it does not upload anything.** The command names a file *already on the printer*:

```json
{"print": {"sequence_id": 0, "command": "project_file",
  "param": "Metadata/plate_1.gcode",
  "url": "file:///sdcard/<path of the uploaded .gcode.3mf>",
  "bed_type": "auto", "timelapse": false,
  "bed_leveling": false, "flow_cali": false, "vibration_cali": false,
  "layer_inspect": false, "use_ams": false, "ams_mapping": [0],
  "subtask_name": "<file name>", "profile_id": "0", "project_id": "0",
  "subtask_id": "0", "task_id": "0"}}
```

Notes, all verified in the client: the library's template defaults the calibration flags to true but its service call sends **false** unless the caller opts in, Print Handy should likewise send false explicitly rather than inherit either default silently, and record the choice per job. For A1, P1S and X1 the URL form is `file:///sdcard/<path>` (newer models use `ftp:///`); `param` is the plate G-code path *inside* the 3MF. The Home Assistant flow that uses this first checks/uploads the file over FTPS (its `ensure_cache_file` endpoint), then calls the service, upload and start are separate steps there too, which matches Print Handy's separation of delivery from print authorization.

## FTPS upload

**Verified** in pybambu and bambulabs_api:

- Implicit TLS on 990; log in as `bblp` with the access code; `prot_p()` so the data channel is encrypted too; the data connection must **reuse the control connection's TLS session** (both clients carry a custom `FTP_TLS` subclass for exactly this; a stock client fails here).
- Upload creates missing parent directories, streams the file, then **verifies `SIZE` equals the local size** and raises on mismatch. That readback check is the minimum bar for Print Handy to call an upload delivered.
- Layout: LAN prints live at the storage root (`/<name>.gcode.3mf`); cloud prints under `/cache/`. No `/model` path appears anywhere in the inspected clients, treat any claim about a `/model` directory as **not verified**. Whether Print Handy's own packaged `.gcode.3mf` (Bambu Studio export layout; see [BAMBU-LAB](BAMBU-LAB.md)) is accepted by an A1 from the root, from `/cache`, or at all, is untested on hardware and is the single most important live-test item.
- Download (timelapse, the running job's file) uses the same connection; not in the staged scope.

## Discovery, reconnect and state

- **Discovery is mostly not discovery.** The HA integration declares an SSDP trigger (`urn:bambulab-com:device:3dprinter:1`) but parses nothing from it; a setup is either bootstrapped from the cloud device list, or entered manually as serial + IP + access code. Print Handy should plan on manual entry (its existing setup-wizard pattern), optionally reading the printer's IP back from the telemetry `net.info` field once connected.
- **Reconnect** (pybambu, verified): Paho with `reconnect_delay_set(min 1 s, max 30 s)`, keepalive 30 s, retry forever with short sleeps on connection errors; a CONNACK refusal (rc 5, access denied) stops the loop instead, because retrying a wrong access code will not fix itself, and after a factory reset the access code rotates, which the integration detects by comparing against the cloud-returned code. Print Handy has no cloud to compare against, so its equivalent must be a clear "access code rejected, re-read it from the printer" error, not a silent retry loop.
- **Watchdog:** 60 s without any payload marks the printer offline and re-sends a `pushing` start request (not a reconnect).
- **Contention is real on A1 (third-party reports, not verified on our hardware):** an A1 sustains only one or two MQTT clients; Bambu Studio or Handy connecting can evict another client. Design for eviction and reconnect, never for a stable session, and say so in user-facing text.

## Why not cloud

Verified from pybambu's cloud code and OpenBambuAPI, summarised: login is interactive (password, or email/SMS code, or a TFA flow that goes through a CSRF-token dance on `bambulab.com`); the returned token's documented lifetime is `expiresIn: 7776000` s (≈90 days) and **no inspected client implements refresh**, expiry means a fresh interactive login, which is hostile to an unattended Discord bot. The endpoints are undocumented and change; the HA integration's HEAD commit at inspection is a fix for a changed CSRF endpoint. All traffic transits Bambu/AWS infrastructure, an outage or account action cuts off the printer, and authorization control restricts cloud control anyway. One discrepancy worth recording: OpenBambuAPI says the cloud MQTT password is the full access token; some older community docs pair the `u_<id>` username with the *device* access code instead. Unresolved, and moot if Print Handy stays LAN-only, which this document recommends.

## Safety and security

- **TLS validation is the weak point of every inspected client, and Print Handy must decide, not inherit.** Printer certificates are issued by a Bambu Lab CA, with the printer serial as CN. OpenBambuAPI ships that CA and explicitly warns not to disable validation, noting a Python 3.13+ `VERIFY_X509_STRICT` pitfall. pybambu verifies the chain against Bambu's bundled CA but sets `check_hostname = False` (IP mismatch), caps TLS at 1.2 (one printer family never answers a TLS 1.3 ClientHello), and offers a user-facing "disable SSL verify (unsafe)" option that drops to `CERT_NONE`. bambulabs_api and bambulan simply use `CERT_NONE` / `InsecureSkipVerify`. Print Handy's staged design should verify against the Bambu CA with hostname checking replaced by an explicit serial/CN match, and should not ship a `CERT_NONE` mode.
- **One secret opens everything.** The LAN access code is simultaneously the MQTT password, the FTPS password, the camera credential, and, in cloud mode, a value the cloud API hands out. It must be handled with Print Handy's existing secret patterns: the config holds only the *name* of an environment variable (the `[discord] token_env` / `[llm] api_key_env` pattern), the value lives in the profile `.env`, and logs redact it. Concrete trap found in the current codebase: the log redactor auto-redacts env values whose names end `_TOKEN`/`_KEY`/`_SECRET`/`_PASSWORD`, a name like `BAMBU_ACCESS_CODE` ends in `_CODE` and would **not** be redacted, and `access_code` is not among its redacted key patterns. Name the variable accordingly (for example `BAMBU_LAN_PASSWORD`) or extend the redactor and its tests in the same change.
- **Commands are unsigned JSON.** Inside Developer Mode, security rests entirely on TLS plus the access code; that is the user's explicit trade when they enable the mode, and Print Handy's docs must present it that way. Outside Developer Mode, Print Handy should mirror pybambu: read the `fun` signature bit and refuse writes, never attempt the X.509 signing path that OpenBambuAPI documents (no inspected MIT client implements it).
- **Never send foreign start code.** The existing guard already refuses Klipper/ZMod code for Bambu printers; a live path inherits that unchanged, and the `gcode_line` channel (above) must not become a way around it.

## Clients and libraries compared

Pinned commits inspected 2026-10-25. "Reuse" means patterns, schemas, fixtures or documentation; no code is copied by this document, and any future copying needs the licence noted and attribution in the change that does it.

| Project | Licence | Pinned commit | State | Coverage | Reuse value |
|---|---|---|---|---|---|
| [greghesp/ha-bambulab](https://github.com/greghesp/ha-bambulab) (vendored pybambu) | MIT | `0e027ff135a6d9265cb756d3e246747954c76722` (v2.2.26) | Actively maintained | LAN + cloud MQTT, FTPS, camera (6000 + RTSP), cloud login/MFA | **Highest.** Protocol behavior, per-model feature matrix, and the best test fixtures found: `mock_mqtt_client.py` plus `mock_data/MOCK-*.json` recorded pushall/get_version payloads (serials already redacted). No dedicated P1S fixture; the P1P fixture is the same family |
| [Doridian/OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) | GFDL-1.3 (documentation) | `cc383a2c96576a9f53391c879acfb8dca5c534e4` | Maintained | Protocol documentation for MQTT, FTPS, video, cloud HTTP, X.509 signing | Protocol truth to **paraphrase and cite**, not copy, GFDL is copyleft for text. Ships a reverse-engineered cloud OpenAPI spec |
| [BambuTools/bambulabs_api](https://github.com/BambuTools/bambulabs_api) | MIT | `c34af6d7b0e1ee24c4a54003913e3570d8635931` (PyPI 2.6.6) | Maintained, releases lag commits | LAN only: MQTT, FTPS, camera 6000 (no RTSP, no cloud) | State enums and AMS models; thin smoke tests only, and its MQTT disables TLS verification |
| [gonzalop/bambulan](https://github.com/gonzalop/bambulan) | MIT | `20840cd8e45b1c9029912270795e67c966dfbffc` | Maintained | LAN only (Go): MQTT, FTPS, camera | `printer_capabilities.json` per-model capability map; typed status models |
| Others surveyed | MIT / Apache-2.0 | See [Sources](#sources) | Mixed | Rust and Go LAN clients, a farm manager | Confirmed the same protocol facts independently; no additional reuse proposed |

Not reusable: **Bambu Studio's networking is closed source**, Bambu's own AGPL-compliance post calls it an "independent closed-source networking plugin", so the open slicer source does not document the printer protocol. The standalone `greghesp/pybambu` repository and the `srausser/bambulan` fork are dormant; do not cite or depend on them. One surveyed cloud-API repository (`coelacant1/Bambu-Lab-Cloud-API`) is stale and its licence is ambiguous between listings; it was not relied on.

All of these protocols are reverse-engineered community work, Bambu states the Developer Mode protocols are "not officially supported". That is a maintenance fact to plan around (firmware can change behavior; the `fun`-bit firmware floors above moved once already), not a reason to avoid the LAN path the vendor itself leaves open.

## Mapping to Print Handy

From the current codebase (paths as of this document's base commit). The OctoPrint integration is the structural precedent throughout: a pure-transport `client.py` beside the adapter, an injectable transport for offline tests, readiness re-checked inside the client before any start, and no retries of ambiguous mutations.

**Stage (a); this research.** Documentation only: this file, an index entry, and a pointer from [BAMBU-LAB](BAMBU-LAB.md). No code, settings, or vendor-profile change.

**Stage (b), read-only telemetry.** New connection value in `print_handy/printers/base.py` `CONNECTIONS` (an unregistered value fails vendor validation at import, so this is step one); the Bambu vendor's `connection` changes but its **delivery stays `("discord", "bambu_handoff")`**; a new `print_handy/printers/bambulab/client.py` (MQTT/TLS, subscribe + pushall, no publish of commands); the adapter gains a real `normalize_status` and client-backed readiness while its start methods still refuse; `print_handy/settings.py` gains Bambu host/serial keys plus the access-code env-name key and a replacement for the offline-URL guard at the current `settings.py` offline check; `print_handy/core/printer_state.py` gains an explicit Bambu branch before its Moonraker fall-through (mirroring its OctoPrint branch); the setup wizard gains a Bambu branch (today an unknown connection value gets no prompt at all). Dependencies: MQTT needs **`paho-mqtt`**, either a hard `requirements.txt` entry (weigh the light-install and Docker contracts) or a lazy import with a clear error, following the codebase's guarded-import precedents; that choice belongs in the stage-(b) change, not here. Tests rewrite the A1/P1S "live calls are refused" expectations *for telemetry only* and use recorded payloads in the pybambu fixture style (MIT, attribution) or payloads recorded from Print Handy's own future live test, never a real socket in CI. The golden vendor-profile test asserting `connection == "offline"` is updated in this stage.

**Stage (c), FTPS upload.** `client.py` gains `upload()` on stdlib `ftplib.FTP_TLS` (session-reuse subclass, filename sanitising, size readback, no retry of an ambiguous transfer, the OctoPrint client's rules). Delivered as an explicit post-slice action or a `bambu_handoff` variant, a delivery-design choice for that change; either way `core/delivery.py`'s handoff note ("no upload") and label change there, and `"printer"` delivery is still **not** added, so `print-now` still refuses.

**Stage (d), print start.** Only now: vendor delivery gains `"printer"` (which automatically offers the bed-clear button through `prepared_intake`, no gating edit needed, and exactly why the sequencing matters); `core/model_job.py` gains a `_print_bambu()` copied from `_print_octoprint()`'s structure (re-verify, readiness twice, consume the approval inside the launch lease, mark the launch uncertain on any error rather than resubmitting, confirm `printing` + filename from telemetry before recording the start); pause/cancel go through MQTT in `print_control`/`print_stop` or are explicitly unsupported, never through the Moonraker paths, and the Moonraker event listener stays excluded (a Bambu listener, if ever wanted, is a separate process and a separate decision).

## HMS error translation (#306)

Status and readiness output decode Bambu's HMS faults and `print_error` values into text instead of reporting a count only. The code → description mapping is Bambu's own data, shipped in Bambu Studio's `resources/hms/hms_en_<prefix>.json` files (`device_hms`, keyed by the 16-hex attr+code; `device_error`, keyed by the 8-hex value; each entry carries `ecode`/`intro`). Print Handy's table (`print_handy/printers/bambulab/hms_data.json`: 5,289 HMS codes and 1,082 print-error codes, plus per-model overrides) is produced by Print Handy's own generator, `scripts/bambu-hms.py`, from those files at the pinned Bambu Studio commit recorded in the table's provenance block (`56a57d00a591360cf41bf21383915ee5bf357be5`), with each text shortened to its first sentence. Lookup discipline, from the #298 Stage 0 audit: the two key spaces are never mixed; a per-model text wins over the common one; a code Bambu lists with empty text is listed-but-not-a-fault, while an unlisted code is reported as unknown with its raw code and still blocks. Print-start gating is unchanged; an active HMS fault still blocks, exactly as before. About two dozen `print_error` codes that Bambu Studio does not ship are not covered yet; their texts exist in the ha-bambulab table, whose own source is unverified, so they are deliberately absent rather than guessed.

## Unknowns that need a live test

Each needs a real A1 (then P1S), a separately approved test plan, and the printer in the stated mode. None is answered by any source inspected:

1. Does an A1 accept and print Print Handy's own `.gcode.3mf` package when it is uploaded to the storage root (or `/cache`) and started with `project_file`? This gates everything in stages (c)–(d).
2. Developer Mode firmware floors and the `fun` signature bit on the actual test printer's firmware, for A1 and P1S; and whether FTPS upload works in LAN Mode *without* Developer Mode.
3. TLS: does the test printer's certificate chain to the Bambu CA in OpenBambuAPI/pybambu, with CN = serial, under Python's strict verification, and does the TLS-1.2 cap matter for A1/P1S?
4. MQTT contention on the A1: what happens to Print Handy's session when Bambu Studio or Handy connects, and does the pushall-on-reconnect behavior recover full state cleanly?
5. `sequence_id` semantics on the wire: is `"0"` accepted for `project_file`, or must IDs increment and be matched to the report echo to confirm acceptance?
6. AMS reporting on the specific A1: AMS Lite vs a regular AMS, `tray_now` encoding, and humidity/temperature availability. (#307 now decodes the AMS block from the recorded payload shapes, unit kind from the get_version module list, presence from `tray_exist_bits`, humidity from `humidity_raw` only, so what remains unknown is whether a real A1's report matches those shapes, not how Print Handy would read it.)
7. `project_file` calibration flags on A1/P1S: what `bed_leveling` / `flow_cali` / `vibration_cali` true actually triggers, and confirmation that false skips them.
8. Whether `pushall` cadence limits observed on P1P apply to the P1S at the watchdog/reconnect rates the design uses.

## Sources

Official (accessed 2026-10-25):

- Printer network ports, <https://wiki.bambulab.com/en/general/printer-network-ports>
- How to enable LAN Mode, <https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode>
- Firmware update introducing the new authorization control system, <https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/> (2025-01-16)
- Updates and third-party integration with Bambu Connect (Developer Mode), <https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/> (2025-01-20)
- Bambu Connect, <https://wiki.bambulab.com/en/software/bambu-connect>
- AGPL compliance of Bambu Studio (networking plugin is closed source), <https://blog.bambulab.com/agpl-compliance-of-bambu-studio/>

Code and documentation inspected at pinned commits:

- `greghesp/ha-bambulab` @ `0e027ff135a6d9265cb756d3e246747954c76722` (MIT), vendored `pybambu` (`bambu_client.py`, `bambu_cloud.py`, `commands.py`, `const.py`, `models.py`), its `docs/` pages, `mock_mqtt_client.py` and `mock_data/`.
- `Doridian/OpenBambuAPI` @ `cc383a2c96576a9f53391c879acfb8dca5c534e4` (GFDL-1.3), `mqtt.md`, `ftp.md`, `video.md`, `cloud-http.md`, `tls.md`.
- `BambuTools/bambulabs_api` @ `c34af6d7b0e1ee24c4a54003913e3570d8635931` (MIT; PyPI `bambulabs_api` 2.6.6).
- `gonzalop/bambulan` @ `20840cd8e45b1c9029912270795e67c966dfbffc` (MIT), including `printer_capabilities.json`.
- Also surveyed at pinned commits, confirming the same protocol facts: `markhaehnel/bambulab` @ `aa003e767dcd6b86465964dbe670f073860aea4b` (Apache-2.0), `torbenconto/bambulabs_api` @ `4c1ed852df65c00b1f88ae2ebc0ad938f4c0c7bd` (MIT), `voska/bambu` @ `fb87bfaee870d4d9b05262fc386a644b7c395c5c` (MIT), `TFyre/bambu-farm` @ `3cf56fc19876` (Apache-2.0; SHA truncated in survey notes).
- `bambulab/BambuStudio` @ `79df7ae309be398ed30c9f397c13c9911c2edc57` (AGPL-3.0), for the networking-plugin boundary only.
- `bambulab/BambuStudio` @ `56a57d00a591360cf41bf21383915ee5bf357be5`, `resources/hms/hms_en_<prefix>.json` HMS/print-error texts, the primary source for Print Handy's own generated HMS table (#306; see [HMS error translation](#hms-error-translation-306)).
- `maziggy/bambuddy` @ `98ecaf5020e3a20a5de49859b8ad5a754d5ad85c` (AGPL-3.0), inspected for the #298 Stage 0 audit as a **protocol-facts resource only**: Bambu protocol behavior, payload shapes and per-model quirks were adopted as facts and implemented as original Print Handy code. **No Bambuddy code, catalog text or assets are copied or used**, in particular its generated HMS catalog is not a source for Print Handy's table; both derive independently from Bambu Studio's own HMS files (see [HMS error translation](#hms-error-translation-306)).
- Print Handy itself at the base commit of the change that adds this document: `print_handy/printers/base.py`, `print_handy/printers/bambulab/`, `print_handy/printers/octoprint/`, `print_handy/settings.py`, `print_handy/logs.py`, `print_handy/core/printer_state.py`, `print_handy/core/model_job.py`, `print_handy/core/delivery.py`, `print_handy/core/prepared_intake.py`.
