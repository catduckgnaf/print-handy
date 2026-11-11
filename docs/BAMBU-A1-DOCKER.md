# From the image to a first A1 print (Docker + Bambu Lab A1)

[Documentation home](README.md) · [Docker](DOCKER.md) · [Bambu Lab printers](BAMBU-LAB.md)

Every step from a published image to a print running on a Bambu Lab A1 over the LAN, with nothing else on the path (no Hermes, no Moonraker, no Spoolman record, no Bambu Studio). Every step below was run on 26 Oct 2026 with the 2.1.3 image, an A1 in Developer Mode and OrcaSlicer 2.4.2 from the image. The commands and keys match the code on `main` at the time of writing. Read [Bambu Lab printers](BAMBU-LAB.md) first for what the Bambu path is and what it guards; this page only tells you what to type, in order.

Some steps work around behavior that was being fixed when this walkthrough was run. They are marked below as limitations or workarounds. Check the release notes for your version: a workaround may no longer be needed.

| Step | Limitation or workaround |
|---|---|
| 1 | Container restart-loops for a Bambu model: the stock supervisor starts the Moonraker listener, which exits when `services.moonraker_url` is empty |
| 1, 4 | `/data/.config` (MakerWorld token, local settings) is not a declared volume |
| 3 | The hardware record must be written by hand; editing it invalidates prepared jobs |
| 5 | Single-extruder slices fail the post-slice check |
| 5 | A package over the Discord attachment limit is not delivered |
| 6 | `delivery.methods` can only come from a mounted YAML that replaces `config/docker.yaml` |
| 6 | The Discord Start button's idle check queries Moonraker only |
| 6 | A started print can show as "submitted, awaiting confirmation" |
| 6 | `/print-stop` could not stop a Bambu print (fixed since PR #1163) |
| 6 | The handoff package has no preview thumbnails |
| 7 | The summary wording says "AD5X" |

## Before you start

- A host with Docker and Compose, on the same LAN as the printer.
- The A1 on the same network, reachable from the container on TCP ports 8883 (MQTT over TLS) and 990 (implicit FTPS). Both are what `print_handy/printers/bambulab/client.py` uses; nothing else is opened.
- A Discord bot token, the print channel ID and your user ID; see [Discord bot setup](DISCORD-BOT-SETUP.md).
- A MakerWorld account if you want MakerWorld links. Printables links need no account.
- The published image: it bundles OrcaSlicer, which the A1 workflow needs in order to slice (see [Docker](DOCKER.md#supported-platforms)).

Paths below assume the checked-out `docker-compose.yml`, a private `.env` beside it and `PRINT_HANDY_DATA=./data`. Everything under the data directory must be writable by UID/GID 1000:1000, the container user.

## 1. Compose: pin the image, add the sixth mount, replace the command

**Why:** the stock service definition assumes a Moonraker printer and the bundled Spoolman; on a Bambu model the Moonraker listener exits at once (`services.moonraker_url is not configured`), the supervisor treats a child exit as fatal and Docker restarts the container forever.

1. Copy `.env.example` to `.env`. Set `PRINT_HANDY_IMAGE` to the digest of the image you tested, not a tag; the digest is the first `RepoDigests` entry after a pull:

   ```bash
   docker pull ghcr.io/catduckgnaf/print-handy:2.1.3
   docker image inspect --format '{{index .RepoDigests 0}}' ghcr.io/catduckgnaf/print-handy:2.1.3
   ```

   Put the printed `ghcr.io/catduckgnaf/print-handy@sha256:...` value in `PRINT_HANDY_IMAGE`.

2. Create the data directories the five declared volumes map to, plus one more, `dot-config`, for `/data/.config`:

   ```bash
   mkdir -p data/state data/archive data/records data/cache data/spoolman data/dot-config
   sudo chown -R 1000:1000 data
   ```

   `HOME` inside the image is `/data` (`Dockerfile`), so the MakerWorld token is read from `/data/.config/bambu/token` and the bot keeps a local settings file under `/data/.config` as well. That directory is not one of the five declared volumes, so without a mount it is lost on every recreate. Mount the whole of `/data/.config`, not only `bambu/`: a mount point Docker creates itself is owned by root.

3. Write `docker-compose.override.yml` next to `docker-compose.yml`. Compose merges it over the checked-in file, so `docker-compose.yml` stays untouched:

   ```yaml
   services:
     combined:
       # Run only the Discord bot. The Moonraker listener and the
       # bundled Spoolman are not started, so the stock healthcheck would fail.
       command: ["python3", "-m", "print_handy.bot"]
       healthcheck:
         test: ["CMD", "python3", "-m", "print_handy.bot", "health"]
         interval: 30s
         timeout: 10s
         start_period: 90s
         retries: 3
       volumes:
         - ${PRINT_HANDY_DATA:-./data}/dot-config:/data/.config
         - ./print-handy.yaml:/data/config/print-handy.yaml:ro
   ```

   `command` works because the supervisor runs whatever arguments it receives instead of its children (`docker/supervise.sh`). The entrypoint still selects the bundled OrcaSlicer and starts Xvfb for headless slicing. `python3 -m print_handy.bot health` is the bot's own heartbeat check (`print_handy/bot/health.py`). The second mount is the settings file from step 6; create it before the first `up`.

4. In `.env`, set the printer and the slicer:

   ```bash
   PRINT_HANDY__PRINTER__MODEL=bambulab.a1
   PRINT_HANDY__PRINTER__SLICER=orca
   PRINT_HANDY_CONFIG=/data/config/print-handy.yaml
   ```

   Bambu Lab printers default to Bambu Studio as the slicer, but the published image is built with `WITH_BAMBU=false` and has no `/usr/bin/bambu-studio`, so slicing would fail closed. `orca` selects the bundled OrcaSlicer v2.5.0 (`PRINT_HANDY_ORCA_CHANNEL=stable`, the default; the one bundled build). `PRINT_HANDY_CONFIG` is read by Compose for variable substitution and names the file from step 6. Leave `PRINT_HANDY__SERVICES__MOONRAKER_URL` empty: settings refuse to load for a Bambu model when it is set.

With `.env` complete (steps 4, 5 and 6 add the rest), start the container with `docker compose up -d` and watch `docker compose logs -f combined`. Nothing contacts the printer yet.

## 2. Generate the A1 templates inside the container

**Why:** the A1 slicer profiles are OrcaSlicer's and Bambu Lab's (AGPL-3.0) and are not shipped; Print Handy builds its templates from an installed OrcaSlicer and refuses to slice until it has them (see [Set up the A1 profiles](BAMBU-LAB.md#set-up-the-a1-profiles)).

The image extracts the pinned OrcaSlicer v2.5.0 AppImage to `/opt/orca-stable` (`docker/orca-prebuilt.sh`), so its resources folder is `/opt/orca-stable/resources`. It holds the vendor profiles as a JSON tree (`profiles/BBL.json`, `profiles/BBL/`) and as the packed preset cache (`profiles/BBL.opc`); the generator reads either. Run the generator in the running container:

```bash
docker compose exec combined python3 scripts/bambu-templates.py --printer a1 --resources /opt/orca-stable/resources
docker compose exec combined python3 scripts/bambu-templates.py --printer a1 --resources /opt/orca-stable/resources --check
```

The first command reports `wrote Bambu Lab A1 Orca templates from OrcaSlicer 2.5.0 (BBL vendor profiles 02.08.00.16) to /data/state/state/orca-profiles/bambulab_a1`; `--check` writes nothing and should end with `matches; generated folder is current`. The templates live in the state volume, so they survive a recreate. Nothing is downloaded. Print Handy ships one bundled OrcaSlicer: `PRINT_HANDY_ORCA_CHANNEL=preview` (or `alpha`, its old name) starts on the same single stable build with a startup notice, because the preview channel was removed.

## 3. Write the hardware record by hand

**Why:** slicing, the color question and the readiness check read the installed nozzle and the loaded spool from `<hermes_home>/state/printer_hardware.json` (`print_handy/core/printer_state.py`). The `set --spool` command that normally fills it reads a Spoolman record and then writes the Moonraker active spool, neither of which exists in this setup, so the file is written by hand.

Create `data/state/state/printer_hardware.json` on the host (the container sees it as `/data/state/state/printer_hardware.json`). This is a complete record for one black PETG spool with the stock 0.4 mm stainless steel nozzle:

```json
{
  "printer": "Bambu Lab A1",
  "moonraker_url": "",
  "bed_mm": [256, 256],
  "max_print_height_mm": 256,
  "nozzle": {"diameter_mm": 0.4, "type": "stainless_steel", "installed": "2026-10-26"},
  "spool": {
    "spoolman_id": 1,
    "name": "Black PETG",
    "material": "PETG",
    "diameter_mm": 1.75,
    "density": 1.27,
    "color_code": "#000000"
  },
  "temps_c": {"nozzle": 240, "bed": 85},
  "filament_slots": {
    "1": {
      "spoolman_id": 1,
      "name": "Black PETG",
      "color": "Black",
      "material": "PETG",
      "hex": "#000000",
      "physical_color_verified": true
    }
  }
}
```

Make the file owned by 1000:1000. What each part does, and what you see when it is missing:

| Key | Used for | If missing or wrong |
|---|---|---|
| `filament_slots` with one slot | The color/material labels offered on the first link and the slot the slice is bound to | `requires one to four distinct configured slots` (the link is refused) |
| `bed_mm`, `max_print_height_mm` | Placement on the bed and the fit check | `Slicing failed: 'bed_mm'` |
| `filament_slots.1.spoolman_id`, `material`, `physical_color_verified: true` | The guard that the physical slot matches what is sliced | `IFS slot color/material mapping is unverified` |
| `hex` | The filament color written into the slice; must be six hex digits | `filament color must be an explicit six-digit RGB value` |
| `spool.material`, `temps_c` | Material family and the temperatures the G-code guard checks; 240/85 °C are the documented PETG defaults | An unrecognised material fails closed |
| `nozzle` | The A1 accepts only 0.4 mm, stainless or hardened steel | A readiness problem naming the supported sizes |

`spoolman_id: 1` is a label here, not a lookup: with no Spoolman the check logs `Spoolman check skipped` as a warning and continues on the recorded state. Two things to know:

- **Settings and state are read once per process.** Restart the bot after editing the file: `docker compose restart combined`.
- **Editing the record invalidates jobs prepared before the edit.** A prepared job is bound to a recipe hash of the state it was prepared with (`slice_recipe.binding_sha256`), so a link you posted before the edit will refuse to slice. Post the link again.

## 4. MakerWorld token

**Why:** MakerWorld model lookups and downloads are sent with an `Authorization: Bearer` header read from `~/.config/bambu/token` (`maker_headers` in `print_handy/core/model_job.py`). Printables needs none.

Sign in at makerworld.com in a browser, open the developer tools and copy the value of the site cookie named `token`. On the host, store it as the only line of the file the container sees as `/data/.config/bambu/token`:

```bash
mkdir -p data/dot-config/bambu
(umask 077 && cat > data/dot-config/bambu/token)   # paste the cookie value, press Enter, then Ctrl-D
chmod 700 data/dot-config/bambu && chmod 600 data/dot-config/bambu/token
sudo chown -R 1000:1000 data/dot-config
```

No restart is needed; the file is read on each MakerWorld request. A wrong or expired token does not fail early: the model lookup still answers and the summary is posted, and only the download is refused. When downloads start failing, sign in again and replace the file.

## 5. Discord: first link, first slice

**Why:** the standalone bot serves one channel and only the listed users; everything else is ignored, so an empty list refuses everyone.

1. In `.env`, set the three values from [Discord bot setup](DISCORD-BOT-SETUP.md) and restart:

   ```bash
   DISCORD_BOT_TOKEN=...
   PRINT_HANDY__DISCORD__CHAT_ID=123456789012345678
   PRINT_HANDY__DISCORD__ALLOWED_USER_IDS=234567890123456789
   ```

   `docker compose exec combined python3 -m print_handy.bot check` validates mode, token presence, channel, users and handlers offline before you connect.

2. Post a MakerWorld or Printables model link in the channel. The bot replies with the model summary, then the questions:
   - **Colors.** A multi-part or painted model first asks `Choose colors for these parts or one color for everything?`; answer **One color for everything**. The A1 path is single colour (AMS Lite is not driven), so per-part colors would be refused at slicing. Then, for every model, `Choose the color for <model>` offers the slot labels from step 3; with one slot that is one button, `Black PETG`.
   - **What to do with the slice.** The choices are `Slice and print now (bed is clear)` (only after step 6), `Slice only`, `Scale the model` and `Cancel print request`. For the first run pick **Slice only**: it slices and verifies, and never touches the printer. `Scale the model` asks bigger or smaller, then 1%, 5% or 10%, and repeats this question at the new size; a size that does not fit the bed is refused, and the bot offers the biggest size that does fit instead.
   - **Delivery.** After a `Slice only`, the delivery menu offers `Send the files here` (the G-code as a Discord attachment) and `Package for Bambu (no upload)`, which builds the `.gcode.3mf` in Bambu Studio's export layout. Each runs at most once per slice.

3. Known limits on this step today:
   - **Single-extruder slices fail the post-slice check** in this version. The slice itself completes; the failure is in the check that follows.
   - **Attachment size.** `discord.upload_max_bytes` is 9 500 000 bytes (`PRINT_HANDY__DISCORD__UPLOAD_MAX_BYTES`). A 22.9 MB package cannot be posted, so fetch it from the host: the handoff writes it to a `bambu/` folder beside the plate's G-code in the job folder under the data directory.

## 6. LAN printing opt-in

**Why:** a Start button exists only when three separate things hold (see [Printing over LAN](BAMBU-LAB.md#printing-over-lan-opt-in)): `printer` is in `delivery.methods`, LAN status is configured, and the printer is in Developer Mode. None is a default, so nobody gets a print button by accident.

1. **`delivery.methods` in a mounted YAML.** It is a list, and lists cannot come from the environment. A file selected with `PRINT_HANDY_CONFIG` replaces the automatic file; it does not layer on top of `config/docker.yaml` ([YAML configuration](CONFIGURATION-YAML.md)). So copy the image's defaults and add the one section (there is no layered override yet):

   ```bash
   docker compose run --rm --no-deps --entrypoint cat combined /app/config/docker.yaml > print-handy.yaml
   ```

   Then append to `print-handy.yaml`:

   ```yaml
   delivery:
     methods: [printer, discord, bambu_handoff]
   ```

   The order does not matter; settings keep the vendor's order. Any method the Bambu Lab vendor profile does not offer stops startup and names the setting. The file is mounted read-only at `/data/config/print-handy.yaml` by the override from step 1, and `PRINT_HANDY_CONFIG` in `.env` points at it. `printer.model` in the copy still says `flashforge.ad5x`; the environment value from step 1 wins.

2. **LAN status in `.env`.** Host and serial are settings; the access code is a secret, never a setting:

   ```bash
   PRINT_HANDY__BAMBU__LAN_HOST=192.168.1.50
   PRINT_HANDY__BAMBU__LAN_SERIAL=00M00A000000000
   BAMBU_LAN_PASSWORD=12345678
   ```

   The serial is on the printer under Settings > WLAN, with the access code. The code changes after a factory reset.

3. **On the A1:** turn on LAN Only Mode, then Developer Mode (both under Settings). Developer Mode is what lets the firmware accept unsigned MQTT commands; without it Print Handy reads the signature bit from the status snapshot and refuses before sending anything. What it switches off: Bambu Handy, cloud access, and Bambu Studio's own connection to the printer, which then shows "failed to connect to printer". Expect that; it is not a Print Handy error.

4. **Restart** (`docker compose up -d` after the YAML and `.env` changes), post the link again, and the first question now starts with **Slice and print now (bed is clear)**. Tapping it is the single-use bed-clear approval for that exact slice. The path from there is the one [Bambu Lab printers](BAMBU-LAB.md#printing-over-lan-opt-in) describes: readiness over MQTT, offline G-code verification, package build, lease, readiness again, FTPS upload with the remote size read back, then the `project_file` start command, confirmed from a fresh status snapshot.

What to expect from the pending fixes on this step:

- **The Discord button's idle check** (`live_idle_check` in `print_handy/hermes/print_next.py`) queries Moonraker, so on a Bambu printer it is not a live check of the A1 in this version. Look at the printer yourself before you confirm the bed is clear.
- **"submitted, awaiting confirmation".** A start that the printer accepted can still be reported this way when the confirming snapshot does not arrive in time. The print is running; the record is uncertain. With LAN telemetry configured, the Bambu listener settles it on its own: the printer reporting that file running or preparing settles it as started, and the printer sitting idle with both heater targets at 0 for a minute without ever running it releases it as not started. If the printer shows that file already finished, cancelled or failed, the listener cannot tell whether it ran, so it leaves the record uncertain for you. Without the listener, or if it has not settled, check the printer; once you see it printing, settle it, and only then:

  ```bash
  docker compose exec combined python3 -m print_handy.core.model_job reconcile-launch --started
  ```

  Never resubmit. The lease refuses a second start on the printer while the first is uncertain.
- **`/print-stop` on a Bambu print.** In the 2.1.3 image this walkthrough was run with, the command did not reach the LAN client. Since PR #1163 it sends one LAN stop, reads the nozzle and bed targets back, and says so. The printer then shows the cancelled job as an error until you acknowledge it on its screen; that is the printer, not a failed stop.
- **No preview thumbnails** in the handoff package: the printer screen shows the file without a picture.

## 7. What is normal in the messages

- `spool weight unknown` in the spool projection line: there is no Spoolman record to take the remaining weight from.
- `Estimated total: unknown` on the start message: the start message on this path carries no time estimate (`discord_print_summary` prints `unknown` when it is given none).
- Two to three minutes of silence after you confirm: the package is being uploaded over FTPS and the start confirmed. Nothing is posted until that completes.
- "AD5X" in the summary wording: the estimate text names the AD5X regardless of the configured printer. The slice is for the A1.

## 8. What worked

On 26 Oct 2026, with the 2.1.3 image and the steps above, a link posted in Discord was sliced with the bundled OrcaSlicer 2.4.2 using the generated A1 templates, packaged, uploaded over FTPS to the A1, started with `project_file`, and the printer reported printing. That is the first run of the LAN print path on real hardware; [Bambu Lab printers](BAMBU-LAB.md) still describes the path as untested where it was written before this run, and its evidence table has not been updated for it.
