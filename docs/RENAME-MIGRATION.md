# Rename migration: Print Buddy to Print Handy

The project is now called **Print Handy**. Everything named after the old name has been renamed with it — the Python package (`print_handy`), the environment variables (`PRINT_HANDY__...` / `PRINT_HANDY_...`), the state files (`print_handy_*`), the container image (`ghcr.io/catduckgnaf/print-handy`), the systemd units, the Unraid template, and the ZMod plugin and macros (`print-handy`, `PRINT_HANDY`). This is a clean break: there are **no aliases or fallbacks** for the Print Buddy names, and Print Handy does not read them at all, so an existing install has to be migrated once. (The only old names still recognised are the much older `print_bitch_*` state files from the first rename: `scripts/install-hermes.sh` copies them across and `python -m print_handy.state_files` reports them.) The known installs are few and coordinated directly; this guide plus `scripts/migrate-names.sh` is the whole procedure.

## This machine (Hermes or standalone install)

```sh
scripts/migrate-names.sh --dry-run   # show every action
scripts/migrate-names.sh
```

It renames `print_buddy*` files in the profile `state/` dir and the archive dir to `print_handy*` (anything still carrying the older `print_bitch*` names is chained straight to `print_handy*`), and replaces `PRINT_BUDDY_` / `PRINT_BITCH_` env var names with `PRINT_HANDY_` in `config/local.toml`, the profile `.env` and `config.yaml`, and the listener systemd unit and drop-ins (each edited file is kept as `<file>.pre-rename`). It is safe to run again. Renamed state files: `print_buddy_context.json`, `print_buddy_launches.sqlite3`, `print_buddy_start_claims.json` and `print_buddy_start_messages.json` become their `print_handy_*` equivalents. Renamed env vars: every `PRINT_BUDDY__...` and single-underscore `PRINT_BUDDY_...` name (including `PRINT_BUDDY_CONFIG` and `PRINT_BUDDY_IMAGE`) becomes the `PRINT_HANDY` spelling; the old names are simply not read any more. One leftover short-prefix variable was renamed as fallout of the same rename (#504): `PB_ORCA_CMD` (the entrypoint's selected OrcaSlicer command, also settable by hand to point at a native OrcaSlicer binary) is now `PRINT_HANDY_ORCA_CMD`; the migration script rewrites it in the same files, and the old name is not read.

Two steps the script cannot do for you:

- **Hermes installs:** the Hermes venv holds `print_buddy.pth`. Delete it and re-run `scripts/install-hermes.sh`, which writes `print_handy.pth` (same content — the repo path — under the new name).
- **Standalone installs:** the systemd units are now `print-handy-bot.service` and `print-handy-listener.service`. Stop and disable the old `print-buddy-bot.service` / `print-buddy-listener.service`, remove the old unit files, re-run `scripts/install-standalone.sh`, then `systemctl --user daemon-reload` and enable the new units.

Then restart the listener:

```sh
systemctl --user daemon-reload
systemctl --user restart hermes-3dprinting-moonraker-listener.service
```

The chat-facing bot name defaults to `Print Handy` now. To keep a different name, set `[discord] bot_name` in your local config (that setting was never renamed).

## Later leftovers renamed (#590)

Two `PB_` spellings survived the main rename and were cleaned up later, again as a clean break with no aliases:

- **Reference codes:** diagnostics markers in chat posts, log lines and doctor rows were `PB_<SUBSYSTEM>_<CATEGORY>` (for example `PB_INTAKE_UNKNOWN`) and are now `PH_...`. Nothing to migrate — old codes appear only in old posts, logs and support notes, and [docs/DIAGNOSTICS.md](DIAGNOSTICS.md#reference-codes) carries an old → new alias table so they stay searchable.
- **Setup wizard answer presets:** the setup wizard (`python3 -m print_handy.setup_wizard`) could pre-fill its answers from environment variables named `PB_<KEY>` (for example `PB_DISCORD_CHAT_ID`). It now reads `PRINT_HANDY_WIZARD_<KEY>` (for example `PRINT_HANDY_WIZARD_DISCORD_CHAT_ID`), and the old names are not read. **What an existing install needs to do:** nothing for a running install — these variables are only read while the wizard runs, never at runtime. If you keep wizard presets in your shell environment or a wrapper script, rename them `PB_<KEY>` → `PRINT_HANDY_WIZARD_<KEY>` before your next wizard run; an unrenamed preset is simply not picked up and the wizard asks the question instead. `scripts/migrate-names.sh` does not rewrite these: they live in your shell environment, not in the config files that script edits.

## Docker and Unraid

- **Image:** pull `ghcr.io/catduckgnaf/print-handy`. The old `ghcr.io/catduckgnaf/print-buddy` package stays frozen at the last Print Buddy release — there is no pointer release on it.
- **Compose:** rename `PRINT_BUDDY_IMAGE` to `PRINT_HANDY_IMAGE` and every `PRINT_BUDDY__...` entry in the environment to its `PRINT_HANDY__...` spelling. The `/data/...` container paths are unchanged.
- **Unraid:** the renamed template (`print-handy.xml`) is a **new Community Applications app** — CA does not migrate an existing app when its name, file and image change. Install the new template, create (or copy) your appdata at `/mnt/user/appdata/print-handy` (copying the old `print-buddy` appdata contents across keeps your state; rename any `print_buddy_*` state files inside to `print_handy_*`), move the configuration values across under the new env var names, start the new container and check it passes its healthcheck, then remove the old app.

## OrcaSlicer channel rename: `alpha` becomes `preview` (#600)

The bundled OrcaSlicer channels are named for their role, not for the upstream release stage, because the stage changes (alpha, beta, release candidate) while the role does not. The second channel is now called `preview`:

- If you set `PRINT_HANDY_ORCA_CHANNEL=alpha`, change it to `PRINT_HANDY_ORCA_CHANNEL=preview` when convenient. Nothing breaks today: `alpha` is still accepted as an alias for `preview` and the container prints a startup notice saying so, but the alias will be removed in a later release.
- The on-disk names follow the channel: the wrapper is `/usr/local/bin/orca-slicer-preview`, the install tree is `/opt/orca-preview`, and the build record is `/orca-preview-build-info` (its `channel=` field reads `preview`; the exact pinned OrcaSlicer build is unchanged).
- The CI jobs were renamed to match: "Slicer test (stable)", "Slicer test (preview)" and "Slicer test (bambu)". This only affects contributors; branch protection on `main` requires "Slicer test (stable)" in place of the old `real-slicer` check.
- **Superseded by the single-Orca change:** Print Handy now ships one bundled OrcaSlicer and the preview channel itself was removed. `PRINT_HANDY_ORCA_CHANNEL=preview` and `=alpha` both start on the single stable build with a startup notice saying the preview channel was removed and the setting can be dropped; the only on-disk names are the stable ones (`/usr/local/bin/orca-slicer-stable`, `/opt/orca-stable`, `/orca-stable-build-info`), and the "Slicer test (preview)" CI job is gone.

## Printer (AD5X, ZMod)

Do this while the printer is idle. These steps run on the printer, so the script only prints them.

1. Disable the old plugin: `DISABLE_PLUGIN name=print-buddy` (ZMod runs its `uninstall.sh`).
2. In `mod_data/user.moonraker.conf`, replace the `[update_manager print-buddy]` section with:
   ```ini
   [update_manager print-handy]
   type: git_repo
   channel: dev
   path: /root/printer_data/config/mod_data/plugins/print-handy
   origin: https://github.com/catduckgnaf/print-handy.git
   is_system_service: False
   primary_branch: main
   managed_services: klipper
   ```
   Restart Moonraker so it clones the repo into `plugins/print-handy`, then delete the old `plugins/print-buddy` folder.
3. Enable the new one: `ENABLE_PLUGIN name=print-handy`.
4. In `mod_data/user.cfg`, rename `[gcode_macro _PRINT_BUDDY_VARS]` to `[gcode_macro _PRINT_HANDY_VARS]`. The values stay as they are. Update any gcode or slicer start macros that call `PRINT_BUDDY` to call `PRINT_HANDY`.
5. Run `FIRMWARE_RESTART`, then `PRINT_HANDY SLOT=1` to check the macro answers.

ZMod's external plugin list links this repo; ask for the entry to become `print-handy` so fresh installs get the new name. Existing printers keep running the old plugin folder until they are migrated — a repo rename alone does not migrate them.

## Chat identities

The slash-command group is now `/print-handy` (registered from code; existing installs see it change on the next command sync). The Discord application name/avatar, the Telegram bot display name and the Matrix bot display name are changed in their respective portals/accounts — they are not in any repo. A Matrix MXID cannot be renamed: if the bot's MXID embeds the old name it stays as it is (a new account plus re-invites is the only way to change it, and is not required).

---

## History: the previous rename (Print Bitch to Print Buddy)

The guide below is the previous rename's migration document, kept verbatim as history. Its steps describe the `print_bitch` -> `print_buddy` move; `scripts/migrate-names.sh` now chains those names straight to `print_handy`.

## Rename migration: Print Bitch to Print Buddy

Everything named after the old project name now uses Print Buddy. There are no aliases or fallbacks, so an existing install has to be migrated once.

### This machine

```sh
scripts/migrate-names.sh --dry-run   # show every action
scripts/migrate-names.sh
```

It renames `print_bitch*` files in the profile `state/` dir and the archive dir, and replaces `PRINT_BITCH_` env var names with `PRINT_BUDDY_` in `config/local.toml`, the profile `.env` and `config.yaml`, and the listener systemd unit and drop-ins (each edited file is kept as `<file>.pre-rename`). It is safe to run again. Then restart the listener:

```sh
systemctl --user daemon-reload
systemctl --user restart hermes-3dprinting-moonraker-listener.service
```

Renamed env vars: `PRINT_BITCH_CONTEXT_PATH`, `_START_CLAIMS`, `_START_MESSAGES`, `_ARCHIVE`, `_BOUNDS_CACHE`, `_NOTIFICATION_STATE` and `_CLEANUP_DRY_RUN` all become `PRINT_BUDDY_...`.

To keep the old bot name in Discord and in the roast persona, set `[discord] bot_name = "Print Bitch"` in `config/local.toml`, then re-render `SOUL.md` and `locales/en.yaml` (the installer only renders them when missing).

### Printer (AD5X)

Do this while the printer is idle. These steps run on the printer, so the script cannot do them.

1. Disable the old plugin: `DISABLE_PLUGIN name=print-bitch` (ZMod runs its `uninstall.sh`).
2. In `mod_data/user.moonraker.conf`, replace the `[update_manager print-bitch]` section with:
   ```ini
   [update_manager print-buddy]
   type: git_repo
   channel: dev
   path: /root/printer_data/config/mod_data/plugins/print-buddy
   origin: https://github.com/catduckgnaf/print-buddy.git
   is_system_service: False
   primary_branch: main
   managed_services: klipper
   ```
   Restart Moonraker so it clones the repo into `plugins/print-buddy`, then delete the old `plugins/print-bitch` folder.
3. Enable the new one: `ENABLE_PLUGIN name=print-buddy`.
4. In `mod_data/user.cfg`, rename `[gcode_macro _PRINT_BITCH_VARS]` to `[gcode_macro _PRINT_BUDDY_VARS]`. The values stay as they are.
5. Run `FIRMWARE_RESTART`, then `PRINT_BUDDY SLOT=1` to check the macro answers.

ZMod's external plugin list links this repo; if it registered the plugin as `print-bitch`, ask for the entry to become `print-buddy` so fresh installs get the new name.

The macro is now `PRINT_BUDDY`; update any gcode or slicer start macros that call `PRINT_BITCH`.

### Printer model setting

`[printer] family` is gone. `scripts/migrate-names.sh` rewrites it in `config/local.toml` (backup `local.toml.pre-model`). To do it by hand, replace it with `model = "flashforge.ad5x"` (or `sovol.sv06`, `klipper.generic`), or the import stops and names the key. Printer defaults now come from the model file under `print_buddy/printers/`. JOB.json files record `printer_model` instead of `printer_family`; a job without it is an AD5X job.
