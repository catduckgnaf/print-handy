# Web UI

[Documentation home](README.md) · [Settings reference](SETTINGS.md) · [YAML configuration](CONFIGURATION-YAML.md) · [Docker](DOCKER.md) · [Guided setup and debugging](GUIDED-SETUP-DEBUGGING.md)

The Web UI is a local page for two jobs:

- **Status:** whether Print Handy, its bot, printer listener and Spoolman are up, plus the doctor checks.
- **Configuration:** a picker-based editor for the catalogued settings in the same YAML settings file Print Handy already loads.

It is **on by default**. While `[webui] enabled` is false, or no port has been chosen, the standalone server does not start and nothing listens. In the combined Docker container, the supervisor likewise starts no Web UI child in that state. On first boot the Web UI serves openly and offers the setup walkthrough — there is no token on the local network, and a login exists only once you set one. See [First boot and logins](#first-boot-and-logins).

The YAML file remains the only source of truth. The Web UI does not keep a second copy of the configuration: its status comes from the effective settings, and a confirmed configuration save writes only the settings-file layer — the file named by `PRINT_HANDY_CONFIG`, or `config/local.yaml` when no file is named. See [YAML configuration](CONFIGURATION-YAML.md) for the full precedence order.

## Pages and routes

| Route | What it returns |
|---|---|
| `GET /` | The status page |
| `GET /config` | The picker configuration page |
| `GET /api/status` | The same status snapshot as JSON |
| `GET /api/printers/<id>/status` | One roster printer's live status as JSON: its identity plus the adapter's normalized status (`state`, `filename`, `progress`, temperatures, and any target/ETA/layer fields that printer's adapter reports). An unknown id is a 404; a printer that cannot be reached answers with `state: "unavailable"` instead of an error |
| `GET /healthz` | The standalone bot heartbeat verdict: `200` when it passes, `503` when it does not |
| `GET /readyz` | Per-component readiness as JSON: `spoolman`, `slicer` and one `printers.<id>` row each, plus `ready`. `200` when no component is known to be down, `503` when one is. Same access token as `/api/status` |
| `GET /designs` | The read-only designs catalog: AI design sessions on disk, newest first |
| `GET /designs/<id>` | One design session: its final `.scad` source and file downloads |
| `GET /designs/<id>/<scad\|stl\|preview>` | That session's `.scad`, final `.stl`, or preview PNG download |
| `GET /layers` | Read-only list of sliced plates already in the archive |
| `GET /layers/<job>/<n>?layer=N` | Plate number `n` of that job (0 is the first), colored by extrusion role |
| `GET /walkthrough` | The first-run walkthrough, while it is pending for this install |
| `POST /walkthrough` | One walkthrough step's action (continue, skip, back, finish) |
| `GET /data` | The Data tab: print-record statistics. Only while `timelapse.wrapped_enabled` is on; otherwise a named 403 refusal |
| `GET /api/data` | The same statistics as JSON, with an optional `?year=` filter |
| `GET /wrapped` | Print Handy Wrapped: the year-in-review page, with a `?year=` picker. Same gating as `/data` |
| `GET /api/wrapped` | The Wrapped payload as JSON (headline, highlights, commentary script) |
| `POST /wrapped/video` | The Wrapped page's video form: compiles the year's video locally, on demand. Needs `timelapse.wrapped_ai_video` on as well |
| `GET /wrapped/video-file?year=<year>` | The compiled Wrapped video file: serves only `wrapped-<year>.mp4` from the wrapped outputs, behind the same login and the `wrapped_enabled` gate, so the Wrapped page can embed it |
| `POST /wrapped/share` | The Wrapped page's share form: exports the year's static share bundle locally, on demand. Same gating as `/wrapped` |
| `GET /theme?set=dark\|light&back=<path>` | The header theme toggle: remembers the choice in the `ph-theme` cookie and redirects back |

Every page renders through one shared shell: the same header (Print Handy brand, navigation over the pages above, and a Dark/Light toggle), the same stylesheet, and the same layout on the status, configuration, spools, designs, layers, login and walkthrough pages. **Dark is the default theme.** The toggle stores the choice in a browser cookie (`ph-theme`, one year, this browser only) — it is a presentation preference, not a setting: no settings-file key or environment variable mirrors it, and it works with JavaScript disabled. The status page opens with an overall card — **All good** or **Attention needed**, with the doctor checks' warning and failure counts — above the same status tables as before. While the default printer is printing or paused, the card also shows the job's file name, progress, layer count and estimated time left, read live from the printer for the page only (the `/api/status` JSON does not gain the read, and a printer that cannot be reached simply leaves the card as it was).

### Chat page

`/chat` is the bot in the browser: the same BotCore that answers on Discord, Telegram and Matrix, hosted in the Web UI process behind the Web UI's sign-in. It does everything the chat bot does — commands, model links, design (`/print-design`), status, spools — including starting a print, and always through the bot's own questions: slicing choices, material picks and the final print confirmation appear on the page as buttons, and nothing prints until they are answered, exactly as in chat. The page refreshes itself every few seconds while replies land, and messages are rate-limited per minute. `[webui] chat_llm_enabled` is the web chat twin of `[discord] chat_llm_enabled`: set it `false` to run web chat as a pure bot — commands and flows still work, but no `[llm]` or `[agent]` lane call is made for web chat text. File uploads for a print go through the upload page below; chat attachments are not accepted. `[webui] chat_enabled` (default `true`) is the page's own switch: set it `false` and `/chat` shows that web chat is turned off — no chat service starts, and no message or button press is processed. An install with no login set up is not restricted here: web chat can start prints and answer confirmations whether or not a password has been set, gated only by the Web UI's own access to the page.

### Upload page and upload links

`/upload` takes a model file (`.stl` or `.3mf`; an STL over the 32 MiB intake limit is refused) and puts it in the inbox (`<state>/webui-inbox`). An uploaded file is not a print: it becomes printable only when it is picked up in chat, where it goes through the normal confirm-before-print flow like a file dropped in chat. The page also lists the inbox contents.

The page can mint an **upload link**, `/u/<token>`: a no-login address that opens an upload form and nothing else — no navigation, no other page honors it — for handing to someone who should send a model file to this inbox. Only a digest of the token is kept, the full link is shown once when it is created, and a link can be revoked on the upload page. Files sent through a link land in the same inbox under the same rules.

### Model sources page

`/sources` lists every place a model link can be dropped from: the model sites Print Handy accepts (with the support level and whether a needed login is saved), plus a direct link to a `.stl`/`.3mf` file on any host, Dropbox share links included. The page reads the same registry (`print_handy/core/model_sources.py`) that link intake and the chat `/print-source` report use, so it cannot drift from what the bot actually accepts. It is read-only: connecting a site still happens by dropping a link in chat and following the walkthrough there. The Designs page links here from its top.

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

The status page also lists the newest archived slice-failure folders under "Recent model failures", and `/api/status` carries them as `slice_failures` (folder name, local path, modified time). The folders hold a failed model's raw slicer logs; the page shows where they are and does not serve them.

With a multi-printer roster configured, the status page also lists the roster read-only — each printer's id, name, model, connection, channel and endpoint — and `/api/status` carries the same rows under `printers`.

For external consumers (the Home Assistant integration, dashboards), `GET /api/printers/<id>/status` serves one printer's live status: the same identity fields as a roster row plus a `status` object holding the printer adapter's normalized status, read live through that printer's own connection — `state`, `filename`, `progress`, `hotend_c`, `bed_c`, and, where the adapter provides them, `hotend_target_c`, `bed_target_c`, `remaining_min`, `layer` and `total_layer`, plus the always-present exclude-object keys `current_object`, `objects` and `excluded_objects` (ha-print-handy #1; empty for families that do not report objects). It sits behind the same access-token auth as `/api/status` and is read-only — it sends nothing to the printer. An unknown id is a 404. A configured printer that is offline or unreachable still answers 200 with `status.state` set to `"unavailable"` and a `detail`, so a consumer polling every printer does not lose the whole poll to one dead machine; a printer on the `offline` connection reads the same way. **Stage 2e decision: the roster is not editable here.** The field catalog addresses global fields only in this version, so per-printer changes stay where the roster is declared: the config file, the CLI setup wizard, or the guided setup's review step. A per-printer addressing scheme for the catalog is a future stage's decision, deliberately not pre-empted by this one.

The Web UI doctor rows reuse the local doctor checks — settings, printer model, Orca templates, core modules, hardware record, Orca CLI, configured Moonraker and Spoolman services, bot mode, and stray state files. Two command-line doctor checks are deliberately not run by the page: Discord login and the shared-token comparison. Both need the bot token, and the login check can make a live network call; run `python3 -m print_handy.doctor` when you want those checks.

`/healthz` is narrower than the status page. It evaluates the bot heartbeat only: a `503` can mean the bot heartbeat is missing or stale while the Web UI itself is still serving. The Docker healthcheck therefore treats either a `200` or a `503` answer from `/healthz` as proof that the Web UI is serving, while the separate bot row owns the heartbeat verdict. An unreachable Web UI, an unresolved configuration, or any other HTTP status makes the Web UI healthcheck row fail.

`/readyz` (also the `components` block of `/api/status`) splits readiness so one dependency cannot mask another. `spoolman` is the same probe as the Spoolman row. `slicer` checks that the configured primary slicer executable (`[slicer] flash_studio_path`, else the `[orca]` or `[bambu]` command for `[printer] slicer`, with `PRINT_HANDY_ORCA_CMD` overriding the `[orca]` command exactly as it does for slicing) resolves on `PATH`; it is not run, so `python3 -m print_handy.doctor` remains the full slicer check. Each `printers.<id>` row comes from that printer's own listener heartbeat: `true` when the listener reports the printer connected, `false` when the listener is running but the printer is not, and `null` (unknown) when there is no listener or it is down. Nothing is sent to any printer. Unknown and not configured rows do not fail `ready`. `/readyz` builds only these rows, without the doctor checks, so polling it never launches the slicer. `/healthz` and the Docker healthcheck stay liveness only, so an offline printer never makes the container unhealthy or triggers a restart.

## Data tab and Print Handy Wrapped

**The Data tab and Print Handy Wrapped are a named, opt-in feature: your print history, shown back to you.** Print Handy already writes one JSON record per print (title, printer, times, outcome, filament, spool, slicer settings, pre-print risk flag, stop reason). This feature aggregates those records — read-only, all printers, every supported family, because the records are family-neutral JSON — and presents them in the Web UI:

- **The Data tab** (`/data`, JSON at `/api/data`) answers the everyday questions: total prints and success rate, hours printing (average and longest print included), filament used in grams and kilograms (estimated next to actual where both were recorded), breakdowns by material, color, vendor, spool, printer, printer profile and nozzle, failures with their stop reasons and risk-flagged prints (prints that started with a pre-print risk flag from the slicer findings and support checks — not camera detections), activity by month, and the top prints by duration and by filament. It also shows repeat prints (a print counts as the same print only when both its title and its G-code file name match exactly), the time of prints by hour and weekday in the configured display timezone, and failed, stopped and pasta as three separate numbers — pasta counts prints the camera watchdog flagged as spaghetti in its recorded events (see "Watchdog event recording" below; before that recording existed the tab said so instead of inventing a count), and stopped counts canceled prints without claiming to know who stopped them. The tab states plainly that its hour and filament totals are of prints with that data recorded, with the count behind each total, because records from some printer families never captured a weight or a duration. Grams recovered from a recorded filament length (for prints that recorded millimeters but no grams) are shown as their own labeled DERIVED line and are never mixed into the headline filament total. A Graphs section renders the same aggregate numbers as server-rendered SVG bar charts: prints per month, filament per material, the outcome split (with risk-flagged as its own bar, and a pasta bar once event recording exists), top colors, top materials, and the hour-of-day distribution. A record that never captured a color, nozzle or weight lands in an explicit "unknown" bucket for that breakdown — nothing is guessed or filled in. A malformed record file is skipped and named, never a crash. Prints still running count as in progress, not as successes or failures.
- **More of the record, joined** (Data tab and Wrapped): multi-color prints get a real per-color filament split — from the per-tool usage recorded in the print's slicer settings at slice time, or, for older archived jobs, recovered from the archived G-code header — shown only where that data exists. Intake receipts and JOB.json files join to prints on the print number, which adds each print's per-slot filament selection, a count of support waivers (receipts that waived a support check and went on to print), and "prints you almost made": intakes that were sliced but never started, listed by title (receipts carry no creation date, so that one list is not split by year). A maintenance block shows the year's sessions, problems and cold pulls from the maintenance log. A **Spoolman lifetime (all-time)** block shows per-spool lifetime usage (initial minus remaining weight, archived spools included) — it is an all-time block, never presented as a per-year number, and when Spoolman does not answer the block says so instead of showing zeros. Every join is read-only and fails soft: a missing or corrupt receipt, JOB.json, sidecar or Spoolman answer is skipped and named, and changes nothing else on the page.
- **Wrapped** (`/wrapped`, JSON at `/api/wrapped`) is the once-a-year recap, in the Spotify Wrapped spirit: the year's headline numbers, your top color, material, printer and vendor, your busiest month, longest print and biggest print by filament, the risk-flag and failure reel, your most-reprinted print, and a short commentary script that makes fun of all of it. With watchdog events recorded, the pasta/failure section names the year's true pasta count and its worst-spaghetti print (the highest recorded streak), and the Spaghetti Magnet personality can be earned from real events — risk flags never count as pasta. The worst failure (the failed print that burned the most time) is enriched from its stop-capture sidecar where one exists. Timelapse clips are joined to prints by the `print-<n>` in their delivery file names across every place clips live (the timelapse archive first), so the page can name the longest print's own clip; in the video, slots are reserved within the usual cap for clips of failed or pasta prints, and a card right before such a clip labels it ("This one turned into pasta" / "This one failed") — the video is a presentation of the year, never a best-hits reel. The Wrapped page is one continuous scrolling page — hero, the year's numbers, colors and materials, time patterns with charts, repeats, your printing personality, the pasta/failure section, and the share card last; the video is a section of the page, not the lead format. Wrapped also names your one printing personality for the year — a deterministic, data-gated archetype (for example Night Owl, Reprint Machine, The Perfectionist, or the neutral fallback The Regular) with the number that earned it — and shows it on a static share card with the year's prints, hours, kilograms, success rate and top color. Every commentary line is driven by the year's real numbers — a roast line appears only when the number behind it is true — and the script is always generated locally and deterministically, with no AI model. Years, months and the late-night count are bucketed in the configured display timezone (`discord.timezone`), so a New Year's Eve evening print counts in the year it felt like; the recorded start instants themselves stay in UTC.

**Watchdog event recording is always-on and is not part of this opt-in.** The camera watchdog appends its alert verdicts (spaghetti, detached, air, uncertain — the verdicts that already raise an operator alert) to `<records vault>/Watchdog/events.jsonl` as they happen, whether or not Wrapped is enabled: there is no setting for it and no toggle, because events from before a toggle is flipped could never be recovered. Healthy verdicts are not recorded. Recording is best-effort and append-only — a failed write is logged and never affects the watch or a print. One consequence to read the numbers with: pasta counts cover events from when this recording began (the payload and pages name that starting point), so the first partial year is a partial count, labeled as such.

Both pages are **off by default**, under the timelapse settings:

- The Data tab and Wrapped are on by default, each under its own switch: `timelapse.data_enabled` (default `true`) turns on the Data tab and its JSON endpoint, and `timelapse.wrapped_enabled` (default `true`) turns on the Wrapped page and its JSON endpoints. While a switch is unset, `data_enabled` follows `wrapped_enabled`; setting it pins the Data tab independently. While a switch is off, its link is not in the navigation and its routes answer a named disabled refusal. Installs that set `wrapped_enabled: false` before the split keep both off — an explicit value is never overridden by the new default.
- `timelapse.wrapped_ai_video` (default `false`) turns on the video step on the Wrapped page. It needs `wrapped_enabled` on. With it on, the page offers **Make my Wrapped video**: an on-demand, local compile of generated stat cards, the commentary as on-screen text, and the year's timelapse clips where they can be found on disk (cards only, named in the result, when none are found). The commentary is the local deterministic script, never rewritten by an AI model (the setting keeps its original name); any missing piece (no ffmpeg, no timelapses) is named in the result. The finished video lands in a `wrapped` folder under the records vault as `wrapped-<year>.mp4`, next to a `wrapped-<year>-script.json` sidecar, and the page names where it landed. Nothing is generated automatically and the video itself is never posted anywhere — sharing it is your choice. Nothing about Wrapped leaves this machine. Narration is separate and opt-in: see the Wrapped video presentation section below.

### Sharing a Wrapped

The Wrapped page's **Share this Wrapped** panel exports the year as a self-contained static bundle, written locally to a folder under the `wrapped` folder in the records vault (`wrapped/exports/<folder>/`). The bundle is the scrolling Wrapped page itself — `index.html`, standalone with its styling inline and no external assets, in the same section order as the Web UI page — plus the summary share card as PNG images (`share-card.png` and `share-card-vertical.png`), a `manifest.json` describing what was exported and when, and, when that year's `wrapped-<year>.mp4` already exists in the `wrapped` folder, the video file beside the page, embedded in it with a video element. An export happens only when you press the button; Print Handy uploads nothing, holds no tokens for any host, and runs no deploys. The bundle contains only that year's Wrapped payload data — no print records dump, no settings, no tokens.

Hosting is your own free static-host account. For **GitHub Pages**: create a repository (or reuse one), copy the bundle's files into it so `index.html` sits at the repository root (for a link-only export, keep the bundle's slug folder as a folder and share that folder's address), commit, and turn on Pages for that branch in the repository settings. For **Cloudflare Pages**: create a Pages project and upload the bundle folder (or connect the GitHub repository); the same `index.html` works unchanged at the project root. There is no build step either way. Taking a share down is deleting those files at the host — Print Handy cannot retract a hosted copy.

Visibility is chosen per export, and the words mean exactly this:

- **Public** — the plain bundle in a `wrapped-<year>` folder. It is public and indexable: anyone can open it and search engines may list it.
- **Link-only** — the bundle under an unguessable folder name, with a noindex tag and a `robots.txt` that asks crawlers to stay out. This is obscurity, not privacy: anyone with the link can open the page, and there is no access control. Do not treat a link-only share as private.
- **Private** — not available yet. A private export will encrypt the payload so the host sees only ciphertext and the page asks for a passphrase to decrypt in the browser (the StatiCrypt pattern); true access control on a static host is not possible, and that encrypted form is the strongest a static host can offer. It is not shipped yet because it needs an established AES-GCM encryption library that Print Handy does not currently depend on, and the Share panel says so by name instead of offering a weaker substitute. When private exports ship, the video will be excluded from them: a plaintext video file never rides beside an encrypted page.

Every export is recorded in a local list, `exports.json` in the `wrapped` folder: the folder name, year, visibility, the destination note you typed (for example "my GitHub Pages repo"), the export time, and whether a video was included. The Share panel shows that list — what you have shared and where — and a corrupt registry file is named on the panel and never blocks a new export. Regenerating creates a new export (and a new unguessable folder for link-only); old copies stay wherever you hosted them until you delete them there.

### Wrapped video presentation, music and narration

The compiled video is a slide presentation of the same data, embedded in the Wrapped page (and in share bundles) — the scrolling page stays the primary format. Its arc is 11–14 slides at 5–7 seconds each (about 60–90 seconds total): a hook, the volume numbers (prints, hours, filament), success, the longest and most-reprinted prints, top color and material, the busiest month, one or two commentary slides, one full-bleed timelapse moment, your printing personality, one failures slide placed mid-late and never last, and the finale share card, which is held longest. A slide whose data does not exist is omitted, never invented. Each slide carries at most 20 words in three type levels (eyebrow, hero number, one short explanation), composed in the central vertical-safe area of the 16:9 frame so a 9:16 cut can be derived later.

Slide backgrounds are your own images: frames grabbed from the year's joined timelapse clips. A usable frame renders full-bleed, blurred, under a black overlay of at least 62% plus a stronger bottom scrim, so text stays legible (never below about 54% effective black). The fallback rule is simple: a frame that fails extraction, is missing or empty, or is near-uniform (its extracted JPEG is under 4096 bytes — a near-uniform frame compresses to almost nothing) renders as a solid background with the photo as an inset, and a year with no usable images at all uses the solid style throughout, with the video result noting `backgrounds: no operator images found`. No stock or invented imagery is ever used.

Music is never a commercial track and no audio files are bundled. The default bed is synthesized locally at compile time (ffmpeg lavfi chord pads, an upbeat major feel). Set `timelapse.wrapped_music_file` to an audio file you supply and that file is the bed instead; a missing file is named in the result and the synthesized bed is used. Over the failures slide the bed ducks down, a slow minor pad and a 2–4 second descending sad sting play — the comedic beat — and then the upbeat bed returns for the finale. The video form's **Export silent** choice produces a video with no music and no narration at all, for adding music in the app you post to.

Narration is governed by `timelapse.wrapped_tts_engine` (default `none`: no narration). `piper` speaks each slide's line locally with the Piper binary at `paths.piper` and the voice model at `paths.piper_model`, timed to the slide and mixed over the ducked bed; a missing binary or model is named in the video result (`narration: piper binary not found at <path>`) and the video is still produced without narration. There is no cloud or AI-model narration engine: narration never leaves this machine.


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

This loopback example needs no access token because only the same machine can reach the listener. To bind a LAN address instead, set `host` to the machine's LAN address or `0.0.0.0`; the Web UI then serves openly on the LAN until you set a login. To require a token from the first start, put one in the environment the server process inherits:

```bash
export WEBUI_ACCESS_TOKEN='<choose-a-long-random-token>'
python3 -m print_handy.webui
```

Never put the token value in YAML. If Print Handy runs under systemd or another service manager, the token belongs in that service's protected environment, not in the settings file; restart the Web UI process after changing its environment.

## Enable it with Docker Compose

The container path is covered in [Docker](DOCKER.md#web-ui-optional-286). The container serves the Web UI on `0.0.0.0:8088` by default and `docker-compose.yml` publishes that port by default, so there is nothing to enable: `docker compose up` and open the Web UI. Optionally set an access token in `.env` if you want a login from the very first visit (otherwise the Web UI is open and offers the walkthrough; you can set a password there instead):

```dotenv
WEBUI_ACCESS_TOKEN=<choose-a-long-random-token>
```

On startup, the supervisor's Web UI gate reads the resolved configuration. It starts `python3 -m print_handy.webui` only when the Web UI is enabled and a port is set. If the configuration cannot be resolved, the gate stops the container rather than guessing. A bind with no access token and no saved login starts open and unclaimed, and the gate logs `no login set yet`.

The Web UI can also be enabled in the selected Docker YAML or TOML file (`webui` in `config/docker.yaml`, or `[webui]` in `config/docker.toml`). Environment values override the file, so an environment setting left at the Compose default can be the reason a file change appears to have no effect; the configuration page marks those fields as environment-overridden.

## Enable it on Unraid

In the in-repo Unraid template:

1. The template sets **Web UI** (`PRINT_HANDY__WEBUI__ENABLED`) to `true` for you; set it to `false` only to turn the Web UI off entirely.
2. Leave **Web UI bind host** at the template default `0.0.0.0` when the page must be reachable through the port mapping.
3. Keep **Web UI port setting** (`PRINT_HANDY__WEBUI__PORT`) and the **Web UI port** mapping matched; the template uses `8088` for both.
4. Optional: set the masked **Web UI access token** (`WEBUI_ACCESS_TOKEN`) to a long random value. Leave it empty to run without a login, or to set a password later in the walkthrough.
5. Apply/restart the container, then open the **WebUI** link.

With a token set, the link lands on the login page, where the token signs the browser in. Without one, the Web UI opens right away and offers the setup walkthrough; if you set a password in its Login step, the login page asks for that password from then on. The port mapping exists even while the Web UI is disabled; then, nothing answers on it.

The published Community Applications template is separate from the in-repo template and follows the normal release-time sync; do not treat this guide as proof that a published template has already picked up a source change.

## First boot and logins

1. Start Print Handy (in Docker, `docker compose up` is the whole step).
2. Open the Web UI's address. It serves right away and offers the [first-run walkthrough](#first-run-walkthrough) — no password choice stands in front of setup.
3. Use the status page to review service health; use Configuration to edit supported settings.

The UI does not start prints. Printer control endpoints are a separate opt-in described below.

There is **no token on the local network by default**. An install with no access token and no saved login is *unclaimed*: it serves openly and is fully usable by anyone who can reach its port, printing and arming controls included, and the status page says plainly that no login is set until you either set one or record that you mean to leave it open. A login comes into existence only by a deliberate act, through one of three routes:

- **The Docker variable:** set `WEBUI_ACCESS_TOKEN` in `.env` (or the masked Unraid template variable). That token is then required from every client.
- **The Web UI walkthrough:** the walkthrough's **Login** step sets a username and password, or records the "leave it open" choice that dismisses the no-login note. A password saved there is stored only as a salted PBKDF2-SHA256 hash in `webui_access.json` in the Print Handy state folder (in Docker, under `/data`, so it survives updates). The browser that set it is signed in. Afterwards the login page asks for it, and API clients such as the Home Assistant integration send it as the `X-WebUI-Token` header, exactly like an access token. A password saved before usernames existed keeps working with any name.
- **Chat:** the chat setup walkthrough (Discord, Telegram or Matrix) can set the same password or record the same open choice, through the same stored file — useful when you are already talking to the bot.

Claiming is first-come on a network you trust: whoever reaches an unclaimed install first — in the browser or from chat — can set its password. That is the accepted model for a home network; if your network is not that kind of trusted, set `WEBUI_ACCESS_TOKEN` before first start so the install is claimed by configuration, not by whoever arrives first. To change the choice later, use the walkthrough's **Login** step, or delete `webui_access.json` from the state folder and restart (the install is unclaimed again). A saved username and password is what the login page asks people for; an access token in the environment is still accepted alongside it, so header clients keep working and the token signs you in after a forgotten password. A saved "open" choice does not override a token.

## First-run walkthrough

Every install is offered the first-run walkthrough at `/walkthrough` from its first boot: six steps — **Printer**, **Chat**, **Camera**, **Login**, **Providers**, **Review** — covering the settings a first print needs. While it is unfinished, the first browser to open the status page is sent to the walkthrough once; after that the status page shows a **Finish setting up Print Handy** banner until it is done; every other page stays reachable, and nothing is trapped. Progress is kept in `webui_walkthrough.json` in the state folder, so leaving and coming back resumes at the step you left.

- **Printer:** the printer's name, its model, the connection address the model uses (a Moonraker or OctoPrint URL, a Bambu LAN host and serial, a PrusaLink host, or a stock Flashforge / Elegoo SDCP host), the ZMod delivery format on Flashforge printers, and the Spoolman URL for filament tracking. The name is edited where it lives: as `[printer] name` on an install without a printer roster (the configuration page has the same field), or in place in the default printer's roster entry — renaming changes the display name only, never the printer's id. A name set by an environment variable or in your own settings file is shown disabled with the reason, exactly like any other field.
- **Chat:** the bot platform, and the chosen platform's channel and allowed users.
- **Camera:** the camera source and snapshot address, and the timelapse essentials.
- **Login:** who may open the Web UI. Choose a new username and password (typed twice), or tick the box to remove the login on a network you trust. Leaving the fields empty keeps the current choice. To skip logins entirely, set `webui.require_auth = false` instead. Saving a password while `require_auth` is false turns it back on (in the settings file, and at once in the running server), so the opt-out never silently outlives a password; when an environment variable forces it off, the step refuses and nothing is saved.
- **Providers:** nothing to fill in. The step lists the model sites and which are connected; a site's token lives in a file or an environment variable, and the bot asks for it in chat the first time a link needs it. Printables needs no account.
- **Review:** the values now in effect for the settings above, then **Finish setup**.

Each step saves through the same validated write as the configuration page when you continue (fields an environment variable or your own settings file overrides are shown disabled, exactly as on `/config`), and each step can be skipped; **Skip setup** on any step ends the walkthrough without finishing it. Like every Web UI save, changes take effect after a restart.

Every install is offered the walkthrough once, however access was resolved (an access token in the environment, a loopback bind, `require_auth = false`, a saved login, or no credential at all). The redirect happens only once; afterwards the banner is the only reminder, and **Skip setup** stops it for good. To run it again, use **Restart setup** at the bottom of `/config`; nothing is reset, and every step starts from the current settings.

## Printers and spools pages

`GET /printers` lists the printer roster with each printer's name, model and connection, and has an **Add a printer** form that appends a new entry to the roster. When more than one printer is configured, each spools page shows a printer switcher. With exactly one printer, `GET /spools` goes straight to that printer's spools page instead of showing a one-item list.

## Access token and browser access

The token's value is a secret held only in the environment variable named by `webui.access_token_env`. The default name is `WEBUI_ACCESS_TOKEN`. Who may use the Web UI is decided in this order, first match wins:

1. **`webui.require_auth = false`:** open (see below); a token is ignored.
2. **A saved password** (set in the walkthrough's Login step or from chat): that password is required. The access token, when one is also set, still works alongside it as a co-credential — header clients keep working, and the token is the way back in after a forgotten password.
3. **The access token is set:** it is required on every bind, from every client.
4. **A saved "leave it open" choice:** open — and, unlike the token, this choice never overrides a token: rule 3 wins when both exist.
5. **Nothing set:** open and *unclaimed*, on every bind. The Web UI serves, offers the walkthrough, and shows a "no login is set" note until a login is set or the open choice is recorded.

Only a *missing* `webui_access.json` counts as nothing set. A file that exists but cannot be used (unreadable by the user Print Handy runs as, as after a container user change or a restore with other ownership; not valid JSON; or missing the fields a saved password needs) never reopens the Web UI. Every request except `/healthz` gets `503` with a short plain message naming the file and the likely cause, an access token does not get past it either, the server logs the reason once, and the Docker gate prints a warning at start. Fix the file's owner or mode, or delete it to clear the login. Either applies on the next request, without a restart.
- **`webui.require_auth = false`** (`PRINT_HANDY__WEBUI__REQUIRE_AUTH=false`): the explicit opt-out for a network you trust. The token is neither required nor checked on any bind host, even if it is set, and there is no login page. Anyone who can reach the port gets the whole Web UI, including printer controls and configuration writes (the per-form `_csrf` check still applies). The Docker gate logs `authentication OFF` at every start. The setting is also a field on the Web UI Settings page. Whenever the Web UI is open, for any reason, the status page carries a banner that says which: no login set yet, `require_auth = false` (adding that a saved login or token is being ignored, when one exists), or the walkthrough's leave-it-open choice.
- Requests that need the access token send it as the `X-WebUI-Token` header. For example:

  ```bash
  curl -H "X-WebUI-Token: $WEBUI_ACCESS_TOKEN" http://127.0.0.1:8080/api/status
  ```

- A missing or wrong header on an API request returns `401` JSON with `missing or wrong X-WebUI-Token`. Wrong tokens count toward the same per-address failed-attempt limit as the login form: after 5 wrong tokens in 5 minutes the address gets `429` with a `Retry-After` header for 60 seconds, and even the correct token is refused during that time. A request with no header is not a guess and is not counted. The counter is in memory and resets on restart. A browser navigation (a GET for a page with `Accept: text/html`) redirects to `/login` instead.

**Browser sign-in:** a normal browser cannot add a custom header to an address-bar request, so browsers sign in at `/login` instead. Open the Web UI address (the Unraid **WebUI** link lands on `/`, which redirects to the login page while signed out), enter the access token, and the server sets a `webui_session` cookie (`HttpOnly`, `SameSite=Strict`) that authorizes page views (`GET` requests) for 12 hours. A plain sign-in lives only in the server's memory, so restarting the server signs it out. Tick **Keep me signed in on this browser** on the login form to stay signed in for 30 days instead, including across server restarts: the cookie gains a matching 30-day `Max-Age`, and the session is stored in `webui_sessions.json` in the Print Handy state folder (in Docker, under `/data`) with mode `0600` — only the SHA-256 hash of the session id is written, never the raw id. `POST /logout` ends your own session immediately, in both stores. Changing the Web UI login (in the walkthrough's Login step or from chat) revokes every remembered session and ends every live one, so browsers signed in under the old credential must sign in again with the new one (the browser that made the change in the walkthrough gets a fresh session). Login-step saves run one at a time, so two browsers claiming an unclaimed install at the same moment cannot both succeed: the second is refused and asked to reload; the store is likewise discarded at start when the credential it was minted under no longer matches, so rotating the access token in the environment and restarting ends remembered sessions too. If the state folder cannot be written, a remembered sign-in falls back to a plain 12-hour session rather than failing. With no credential configured there is nothing to sign in to and `/login` simply redirects to the status page. Scripts and API clients keep using the `X-WebUI-Token` header exactly as above — the cookie is a browser convenience, not a second token. There is one saved login at most (no multi-user accounts) and no HTTPS listener. Removing the token from a LAN-reachable bind is a real choice, not a trick to make a page load: without a token or a saved password, the Web UI is open to everyone who can reach it, and it says so on the status page.

Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`: no Web UI page may be framed by another site (the Discord Activity is a separate server and is unaffected). Each connection has a 30-second socket timeout and at most 64 requests are handled at once, so a stalled client cannot tie the server up. An internal error answers a generic `500` and the details go to the Web UI log, never into the response. `camera_snapshot_url` is fetched only over `http` or `https` (redirects included); any other scheme answers `400` and nothing is fetched.

The session cookie and the `ph-theme` cookie omit the `Secure` attribute unless `[webui] cookie_secure` is true. The default is false so a plain-HTTP browser on the LAN still stores the cookie. Set it true only when every browser that should keep the session reaches the Web UI over HTTPS. The flag does not turn on TLS and does not change who may log in.

Form posts (configuration, the walkthrough, spools, printers, uploads, chat, debug mode) also carry a `_csrf` hidden field, and what it takes depends on whether the install is claimed:

- **Open** (unclaimed, `require_auth = false`, or the leave-it-open choice): the server generates one form token when it starts, keeps it only in memory, and embeds it in every page. A post needs that token, so another site open in the same browser cannot silently submit changes.
- **Claimed** (a password or access token is required): a post needs the `X-WebUI-Token` header, or a signed-in session together with the `_csrf` value bound to that session (an HMAC of the session id under an in-memory secret). A token copied out of a page is worthless without that browser's session, a session cookie alone never authorizes a post, and changing a setting such as `require_auth` therefore always needs someone signed in.

Changing or removing the login in the walkthrough's Login step on a claimed install also asks for the **current password** (or the access token); a wrong entry counts toward the login backoff. HTML pages are sent with `Cache-Control: no-store`, so a browser or proxy cache never keeps a page that embeds a token. Restarting the server, or changing the login, replaces the form secrets, so an old page must be reloaded.

## Where the Web UI may be reached

The Web UI has no HTTPS listener of its own. Put it where the network already limits who can open the port.

| Boundary | Supported | Cookie setting |
|---|---|---|
| Loopback (`127.0.0.1` or `localhost`) | Yes. The default bind. Only the same machine can connect. | Leave `cookie_secure` false. |
| Trusted LAN, plain HTTP | Yes. Bind a LAN address or `0.0.0.0` and publish the container port only on that network. | Leave `cookie_secure` false. A `Secure` cookie is not stored by a browser that loaded the page over HTTP, so the login would not stick. |
| VPN that places you on that LAN | Yes. Treat it as the LAN above. The VPN is what admits you; Print Handy does not see it. | Same as the LAN. |
| HTTPS reverse proxy on that LAN or VPN | Yes, if the proxy is the only public name and it forwards to the Web UI over the private network. Terminate TLS at the proxy. Leave the Web UI on plain HTTP behind it. | Set `cookie_secure` true (`PRINT_HANDY__WEBUI__COOKIE_SECURE=true`). Browsers then send the session and theme cookies only on HTTPS requests to the proxy. |
| Router port forward, or the Web UI published directly on the public internet | Possible, and it is your choice to make — but only behind a login you set deliberately first: an access token (`WEBUI_ACCESS_TOKEN`) or a saved password, set before the port is forwarded. An unclaimed or left-open Web UI on the public internet is open to the whole internet; the "no login is set" note is the warning, and plain HTTP also sends that login unencrypted, so the HTTPS reverse proxy row above is the better shape for outside access. | Set `cookie_secure` true when an HTTPS proxy fronts it; on plain public HTTP there is no cookie setting that makes the bind safe. |

`cookie_secure` is a field on the configuration page, like the other Web UI settings. Restart after changing it. Authentication (`require_auth`, the access token, or a saved login) is separate and stays as configured.

## Use the configuration page

Open `/config`, or follow the **configuration page** link from the status page. The groups are side sub-tabs: one group shows at a time (pick from the tab row, or link straight to one with `/config?section=<group>`), and the form is built from the Web UI field catalog and is grouped as:

- Printers
- Platform and chat
- Spools
- AI and models
- Camera, timelapse and images
- Wrapped
- Slicing and delivery
- Home Assistant
- Notifications
- Logging
- Web UI

The **Telegram** and **Matrix** field sets only draw when that platform is configured — it is the selected chat platform, or one of its identity fields (chat ID, homeserver, room) is already set; until then the Platform and chat tab shows a **Set up Telegram** / **Set up Matrix** row instead, and following it draws the fields for a first-time setup. Their values are still part of every save either way. Saving still works on the whole form: the groups you are not looking at ride along as hidden values, so previewing and saving from any tab can never clear another group's settings. Pickers appear wherever the setting has a defined choice set — for example the printer registry, bot mode and platform, slicer and delivery methods offered by the configured printer's vendor, timezone, log level and format, and the timelapse and camera type pickers. Options are listed A to Z by the text shown (a printer's name, not its key). Toggles are checkboxes. Numbers use bounded numeric inputs; genuinely free values such as IDs, hosts and URLs remain text inputs and are checked by the same settings validators Print Handy uses at startup.

The two type pickers are explicit settings, not page-only choices, and both are wired to the paths they name. **Timelapse mode** is `rendered` (the printer renders it with moonraker-timelapse; the in-container capture below is preferred when it produced a video), `container` (Print Handy captures and encodes it in the container; the printer-rendered path is not used), or `off` (no timelapses — capture and the rendered path both stop); an empty file value derives `rendered` when a Moonraker URL is configured, otherwise `container` when in-container capture is on, otherwise `off`. The **In-container capture** toggle beside it is the capture switch itself: in `rendered` mode it decides whether capture runs, in `container` mode capture runs regardless, and `off` stops it whatever the toggle says. **Camera source** is `auto` (follow the configured printer vendor's rule), `moonraker` (the Moonraker webcam snapshot only), `bambu` (the printer's own Bambu camera), `go2rtc` (the go2rtc fan-out only), or `none` (no camera images anywhere — no watchdog stills, no timelapse capture, no fan-out); the watchdog stills, the timelapse capture source and the Bambu camera paths all honor the pin and name it when they refuse. **RTSP stream URL** (`timelapse.capture_source_url`) is the address go2rtc ingests when you have an `rtsp://` camera; leave it empty to keep using the snapshot. The printer's address field sits in the **Printers** group and is labeled per connection: **Moonraker URL**, **OctoPrint URL** (including an OctoPi on another computer), or the LAN host field for Bambu Lab, PrusaLink, stock Flashforge and Elegoo SDCP — only the field for the selected model's connection is shown. For a Bambu Lab model the **Printer serial** field shows with the host — the LAN connection needs the pair, and settings validation refuses one without the other. The ZMod delivery format field is shown only for a Flashforge ZMod model.

The live camera feed values in the **Camera, timelapse and images** group work as one switch. While the feed height, frame rate, bitrate cap and encoder all hold their defaults (720, 15, 2500, auto), the live view is the camera stream relayed as it arrives. Change any one and go2rtc publishes a second, transcoded stream for the live view with all four values applied — ffmpeg re-encodes the loopback restream, so the camera still gets one upstream pull, and timelapse capture and watchdog stills keep the camera's own stream. The encoder preference is resolved against a probe of the machine's ffmpeg (hardware first, CPU last), the same candidate order as the timelapse assembly encode.

The **Home Assistant** group carries the MQTT event fields: the **MQTT events** switch with its broker host, port, topic root and username. The **Notifications** group carries the **ntfy events** switch with its server URL and topic. The **Spools** group carries the **Spoolman URL**. Neither channel's credential is a field — the MQTT password is `MQTT_PASSWORD` and the ntfy access token is `NTFY_ACCESS_TOKEN`, both in the environment only. The **AI and models** group carries the chat, design and agent endpoint URLs and model names, the vision endpoint and model, and the AI edit gate (allowed users and the per-hour request limit); their API keys likewise stay in the environment. The [ai] tier tables are named in the group as file-only — they are edited in the settings file, not here. **Plate spacing** (Slicing and delivery) is the auto-layout spacing preset.

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

One validation rule deserves naming: the settings loader refuses certain keys outright — a credential-shaped key written into the file (credentials belong in the environment, never in settings), any extra key in the closed `[notify]` table, and the retired `[printer] family` key. If your settings file already carries such a key from a hand edit or an older release, a Web UI save is no longer blocked by it: the save validates and lands as usual, the offending key is left in the file exactly as it was, and its name is reported on the save-result page — one line per key, saying the next restart will fail until it is removed — and in the Web UI process's unknown-settings note, so you can remove it by hand. The rewrite and its timestamped backup also keep the settings file's existing permissions, so a tolerated key in a file readable only by its owner stays that way. The key is still in the file, so the settings will still refuse to load at the next startup until you remove it: a fresh startup or `doctor` process that reloads the file hits the loader's refusal, not a warning. A save that tries to add such a key, or change the value of one already there, is still refused outright — the tolerance covers only what was already in the file before the save.

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
| `webui: not started: [webui] port is 0 (unset)` | Port `0` means no port has been chosen. Set `webui.port` (the container uses `8088`), then restart. |
| `webui gate: ... no login set yet` | Nothing is wrong: the Web UI is serving openly and unclaimed. Set a password in the walkthrough's Login step, record the leave-it-open choice there, or set `WEBUI_ACCESS_TOKEN` — see [First boot and logins](#first-boot-and-logins). |
| Every page answers `503` naming `webui_access.json` | The saved login file exists but cannot be read or parsed, so the Web UI refuses to reopen without it. Make the file readable by the user Print Handy runs as (in Docker, check its ownership after changing the container user or restoring a backup), or delete it to clear the login. |
| Forgot the Web UI password | Delete `webui_access.json` from the Print Handy state folder (Docker: under `/data/state/state`) and restart; the install is unclaimed again, or sign in with the access token if one is set (it works alongside the password). |
| `401` with `missing or wrong X-WebUI-Token` | In a browser, open `/login` and enter the access token (a plain session lasts 12 hours and ends when the server restarts; tick **Keep me signed in on this browser** for a 30-day session that survives restarts — changing the Web UI login revokes remembered sessions, so sign in again afterwards with the new credential). For scripts, send the exact token value in the `X-WebUI-Token` header. See [Access token and browser access](#access-token-and-browser-access). |
| `403` with `missing or wrong form token` on a configuration post | Load `/config` from the running server and submit its own form, so the current in-memory `_csrf` value is included. Restarting the server creates a new form token, so an old page must be reloaded. |
| The Docker container stops after enabling the Web UI | Read the container log for the gate or server refusal. An unresolved settings file still stops the container; a bind with no `WEBUI_ACCESS_TOKEN` does not (it starts open and unclaimed). Fix the named setting and restart. |
| The Web UI healthcheck row says `enabled but no port chosen; not started` | This is an `ok` row, not a serving Web UI. Choose a port and restart if you intended to run it. |
| The Web UI healthcheck row says `disabled` | This is an `ok` skip. Nothing is listening, as configured. |
| The status page says **attention needed**, but `/healthz` is `200` | They answer different questions: `/healthz` is only the bot heartbeat, while the status page also includes the listener, Spoolman and doctor rows. Read the failing row on `/` or `/api/status`. |
| `/healthz` is `503` | The Web UI answered, but the bot heartbeat is missing, stale, invalid, disconnected, or otherwise fails the bot-health evaluation. Check the bot row and the standalone bot process. |
| A field cannot be edited | Its badge says `environment override`, or the value is not in the catalog. Change the environment variable and restart, or edit the YAML directly for an uncatalogued setting. |
| A save is refused and the file did not change | Read the error beside the field or at the top of the form. Invalid combinations are rejected before the atomic write; the file remains byte-identical. Correct the value and preview again. |
| Saving worked, but behaviour did not change | Restart the Print Handy process or container that consumes the setting. A running process keeps the settings it loaded at start. |
| A legacy TOML save is refused | The Web UI writes YAML only. Select a YAML settings file with `PRINT_HANDY_CONFIG`, or keep editing the TOML file manually. |

## Printer controls

`POST /api/printers/<id>/pause`, `/resume`, `/cancel`, `/temperature` and `/target` send one command to one roster printer through the same paths chat uses: Moonraker's print pause/resume/cancel and `M104`/`M140` for Moonraker printers, the adapter's own `control()` for every other family. A family with no existing temperature path answers 501 ("not supported"); nothing is sent.

Guard:

- **Off by default.** Only `[webui] controls_enabled = true` turns them on. While off, every control route answers **403** with the reason and sends nothing. The switch is deliberately not a Web UI field.
- **Auth.** The `X-WebUI-Token` header only, like `GET /api/printers/<id>/status`; a browser login cookie does not authorize a control. Bodies must be `application/json`, so a cross-site HTML form cannot post here.
- **cancel** also needs the body `{"confirm": true}` (400 otherwise).
- **temperature** takes `{"heater": "nozzle" | "bed", "target": <°C>}`; `0` turns the heater off. The target is refused (400), never clamped, above the printer's limit: for the nozzle the lower of the fitted nozzle type's safe maximum and the model's `max_hotend_c`; for the bed the model's `max_bed_c`, or the hottest configured material bed temperature when the model has no such fact.
- **target** takes `{"hotend_c": <°C> | null, "bed_c": <°C> | null}`, the body the Home Assistant integration (ha-print-handy) sends: both heaters in one call, `null` leaving that heater alone, at least one a number. Same limits as `temperature`; every value is checked before anything is sent, so one bad value sends nothing. If the second heater fails after the first was sent, the 502 reason names what was already sent.
- Every control re-reads the printer first. An unknown id is a **404** in the status route's shape, an unreachable printer an explicit **502**, and a printer in the wrong state (resume while printing, say) a **409**; none of them send anything. If the printer is reached but refuses the command (Moonraker answers an invalid transition with HTTP 400, for instance when someone paused from the printer screen a moment earlier), Print Handy reads the printer again: if it already shows the result (paused after pause, printing after resume, no longer printing or paused after cancel) the answer is **200** and says so; otherwise it is a **409** naming the state it now reports. A refused command is never resent, and a printer error (HTTP 5xx) stays a **502**.

### Power, send-G-code and exclude-object (ha-print-handy #1)

Three more control routes sit under the same guard — same off-by-default `controls_enabled` switch, same `X-WebUI-Token` header-only auth, same JSON-only bodies. They exist so the Home Assistant integration can offer them; two of them deliberately reverse #708 presumptions at catduck's direction: emergency stop no longer stays chat-side only, and raw G-code is no longer out of scope for the API.

- **`POST /api/printers/<id>/emergency_stop`**, **`/firmware_restart`** and **`/server_restart`** each need the body `{"confirm": true}` (400 without it). They call Moonraker's `/printer/emergency_stop`, `/printer/firmware_restart` and `/server/restart` — the same endpoints the chat panic ladder uses. Emergency stop has **no state gate and no status read**: a hung or unreachable controller is when it is needed, so it sends at once from any state. If the emergency-stop call itself fails, the firmware's `M112` is sent through `/printer/gcode/script` instead, and the answer says which one was sent (`"sent"` in the JSON); when both fail it is a **502** naming both failures. Firmware restart still reads the printer first. Server restart's pre-read is best-effort, because Moonraker rejects the status query while Klippy is disconnected and server restart is the recovery control for exactly that failure — the restart request itself decides whether Moonraker is reachable. No other printer family exposes a verified stop or restart capability in its adapter or client code, so every other family answers **501** ("not supported") and nothing is sent; no behavior is invented for them.
- **`POST /api/printers/<id>/gcode`** takes `{"script": "<G-code text>"}` and sends raw G-code. A missing, non-string, empty, or over-2048-character script is a **400** and sends nothing. Temperature commands inside the script are held to the same fitted-nozzle/model ceiling as the `/temperature` route: a target outside that range is a **400** naming the command, the heater and the limit, and nothing at all is sent — never a trimmed script. The scan reads each line the way the firmware does (uppercased letter/value pairs with spaces or tabs allowed between letter and value, `*` not ending the line, every value of a repeated parameter checked), so `M104 S 300`, `M 104 S300` and `M104 *S400` are caught. It covers `M104`/`M109` (S, R, B) for the nozzle, `M140`/`M190` for the bed, `M141`/`M191` for the chamber, Marlin's `M303`, and Klipper's `SET_HEATER_TEMPERATURE` and `PID_CALIBRATE` for any named heater: `extruder*` and `heater_bed` against their limits, a chamber against the model's chamber maximum, and any other heater (a `heater_generic`, say) against a conservative 60 °C ceiling. On any other extended command (a macro such as `PRINT_START`), a numeric parameter whose name marks a temperature (`EXTRUDER`, `HOTEND`, `NOZZLE`, `TEMP`, `BED`, `CHAMBER` and names built from them, like `BED_TEMP`) is held to that heater's ceiling. This is a screen before sending, not the safety limit itself: the firmware's own `max_temp` remains the real ceiling, and a macro can still heat in ways its call does not show. The send is also state-gated: a printer reporting a state that cannot accept commands (unknown, error, shutdown, disconnected, offline, unavailable) answers **409** and receives nothing, while scripts sent mid-print remain allowed. Unlike cancel and the power routes, the G-code route needs no `{"confirm": true}` — the temperature routes perform the same heating action without one, so the ceiling, not a confirmation, is the guard. Moonraker printers receive the script through `/printer/gcode/script`; OctoPrint printers receive one command per non-empty line through `/api/printer/command`, the endpoint OctoPrint's client already uses for its temperature commands. Every other family answers **501**.
- **`POST /api/printers/<id>/exclude`** takes `{"object": "<name>", "confirm": true}` and excludes one named object from the running print. It is Moonraker only — exclude-object is a Klipper feature, so other families answer **501**. The decision follows the same plan chat builds (`core.exclude_object.exclude_plan`) over the live status: excluding the **last remaining object** answers **409** and sends nothing (cancel the whole print instead), an unknown or already-excluded name answers **400**, and a missing `confirm` answers **400** naming the requirement. A confirmed exclude sends `EXCLUDE_OBJECT NAME=<name>` to the printer.

So dashboards can offer exclude-object, `GET /api/printers/<id>/status` now also carries the object data for Moonraker printers: `current_object` (the object printing now, or `null`), `objects` (the defined object names) and `excluded_objects` (the names already excluded). Like the #708 telemetry keys, all three are always present — `null` / empty for families that do not report objects.

```bash
curl -X POST -H "X-WebUI-Token: $WEBUI_ACCESS_TOKEN" -H "Content-Type: application/json" \
     -d '{"confirm": true}' http://127.0.0.1:8765/api/printers/garage-printer/cancel
```

Still open from #708: Docker defaults, Bearer auth, extended telemetry, and the camera proxy.

## Spool management

`GET /api/printers/<id>/spools` is the loaded view as JSON: the printer's slots with their assigned spool (id, name, material, color, remaining weight, and percent left where the spool's starting weight is recorded — never estimated), plus the unassigned spools on the shelf. Assignments are read from the Spoolman `ph_slot` extra field, the reconciled state, with the local slot cache as the fallback; duplicate or archived claims are named in the response's `conflicts` list instead of silently picking a winner. An unknown id is a 404 — every spool operation names its printer, and none falls back to a default printer.

`POST /api/printers/<id>/spools/<action>` makes the changes, through the same operations Discord uses — Spoolman owns spool records, Print Handy owns slot assignment, and no surface keeps a second state store:

| Action | Body | What it does |
| --- | --- | --- |
| `assign` | `{"slot": 1, "spool_id": 7}` | Binds the spool to the slot through `spool_sync.bind_slot`; a replaced spool goes back on the shelf. |
| `swap` | `{"slot_a": 1, "slot_b": 2}` | Swaps the two slots' spools through `spool_sync.swap_slots`; one side may be empty (a move). |
| `unassign` | `{"slot": 1}` | Empties the slot through `spool_sync.unbind_slot`: the spool's `ph_slot` claim is cleared, its location becomes the shelf, and the local slot record is removed. |
| `add` | `{"brand": ..., "material": ..., "color": ..., "grams": 1000}` | Creates one spool on the shelf through `spool_sync.create_spool` (the #512 creation path, without the chat walkthrough). |
| `edit` | `{"spool_id": 7, "remaining_weight": 321}` | Writes the spool's Spoolman record: remaining weight and `archived` on the spool itself; `name`, `material`, `color`/`color_hex` and `brand` on its filament record — the record Spoolman's own UI edits, shared by every spool of that filament type. Only the fields present are written. |
| `sync` | `{}` | Runs `spool_sync.reconcile` for the printer: Spoolman's assignments rebuild the local slot state and the screens converge. |
| `purchase/propose` | `{"url": "https://..."}` | The #801 hook: proposes spools from a shop/order link through `purchase_intake.propose_from_link`. Creates nothing. |
| `purchase/create` | `{"lines": [...]}` | Creates the proposed lines on the shelf through `purchase_intake.create_purchase`, after re-validating them. This is the confirm step; call it only with lines the operator has seen. |

Guard: the POSTs follow the printer controls' posture — the `X-WebUI-Token` header only (a login session never authorizes one), JSON bodies only, and `[webui] controls_enabled` must be true: while it is off every spool POST answers 403 and changes nothing. Refusals are named in the response: an unknown spool or slot, a reconcile conflict, or an archived spool is a 409 with the reason; a malformed body is a 400. Assignment changes during a print behave exactly as they do from Discord — the write is an inventory change whose screen push defers until the print ends; no new mid-print rule is invented here.

The purchase-link actions additionally need an AI route (`PRINT_VISION_URL` or `services.vision_chat_url`), #801's gate: with none configured, `purchase/propose` refuses naming why, and the Spools page shows that reason instead of the form.

`/spools` in the browser lists the configured printers; `/spools/<id>` is that printer's Spools page — the same loaded view and shelf, with assign/swap/unassign/add forms using pickers (no raw ids typed by hand), the Spoolman record edit form, a sync button, and the paste-a-link purchase action with its propose-then-confirm steps. The page's forms post under the same form-token rule as the configuration page.

Browser pages never answer with a raw JSON dump: when Spoolman is down the Spools page renders a themed error page at the same 502 status (the JSON shape is kept for `/api/printers/<id>/spools` only), and an unknown printer id gets a themed 404 with a link back to the printer picker. Operator-facing spool wording is plain — exception details go to the server log, not the page. A successful spool form post redirects back to the page with the result as its notice (Post/Redirect/Get), so reloading never repeats the action; refusals and the purchase proposal keep their direct render. The same themed refusal covers the other browser forms (`/config`, `/walkthrough`, `/debug`, `/spools`): a form left open across a Print Handy restart is told the page is out of date and to reload, and a `/debug` validation error re-renders the status page with the error on it. Every page carries the shell's one small script: on submit it asks for confirmation where a form or button says so (Unassign, Swap, debug Turn off), and disables the form's buttons — labeling the clicked one with its pending text — so a slow action (Read the purchase, Sync, Create these spools) cannot be sent twice. With JavaScript disabled the pages behave exactly as before.
