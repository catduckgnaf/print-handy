# Install Print Handy (native, no Docker)

No Docker? Python plus systemd gets you the same bot. Most people should use [Docker](DOCKER.md) instead. Coming from an old Hermes-hosted install? That mode is gone; the move is at the [bottom of this page](#coming-from-the-old-hermes-install).

This page is the native install: Python and systemd user services. The bot's behavior and settings are covered by the [standalone bot guide](STANDALONE.md).

## 1. Clone and install

You need Python 3.12 or newer, git, and OrcaSlicer as a Flatpak (step 2). Install into a virtual environment: a plain `pip install` into the system Python is refused on current Debian, Ubuntu and Fedora.

```sh
git clone https://github.com/catduckgnaf/print-handy.git ~/print-handy
cd ~/print-handy
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

Use `.venv/bin/python` wherever this page says `python3` from here on, or activate the environment with `. .venv/bin/activate`. The direct service installer in step 4 uses the system `python3` by default, so pass the virtual environment's interpreter explicitly when running it without activation.

## 2. OrcaSlicer

Print Handy's default slicer command is `flatpak run com.orcaslicer.OrcaSlicer`. Install the 2.5.0 build that the Docker image ships. [`scripts/install-orca-flatpak.sh`](../scripts/install-orca-flatpak.sh) does exactly this for CI: it installs the pinned GNOME runtime, checks the bundle's SHA-256 and installs it. It reads its pins from the environment, which the top of [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) sets (`ORCA_BUNDLE_VERSION`, `ORCA_BUNDLE_SHA256`, `ORCA_RUNTIME_REF`, `ORCA_RUNTIME_COMMIT`, `ORCA_FLATPAK_APP_ID`), so export those values first and run it from the repository root:

```sh
sh scripts/install-orca-flatpak.sh
```

Or install by hand from the OrcaSlicer 2.5.0 release page: download the `x86_64` Flatpak bundle and run `flatpak install --user --bundle <the downloaded file>`. Either way, `flatpak run com.orcaslicer.OrcaSlicer --help` should print usage.

## 3. Settings: YAML preferred, TOML supported

Defaults live in `config/home.yaml`. Put machine-specific overrides in the gitignored `config/local.yaml`; an existing `config/local.toml` remains supported. YAML takes precedence when both formats are present. See [the configuration guide](CONFIGURATION-YAML.md) for precedence, validation and migration details.

- `services.moonraker_url` and `services.spoolman_url`
- `paths.hermes_home` (the state folder; the name is historical), `archive`, `records_vault` and `cache`
- `printer.model` and supported printer-specific overrides

The guided setup (`python3 -m print_handy.bot setup`) writes these with you; see [guided setup and debugging](GUIDED-SETUP-DEBUGGING.md).

## 4. Install the services

The setup wizard asks the questions and installs the services:

```sh
python3 -m print_handy.setup_wizard
```

Or install the systemd user services directly:

```sh
scripts/install-standalone.sh --python "$PWD/.venv/bin/python"
systemctl --user enable --now print-handy-bot.service print-handy-listener.service
```

Put the bot token in the data folder's `.env`; see the [Discord](DISCORD-BOT-SETUP.md), [Telegram](TELEGRAM-BOT-SETUP.md) or [Matrix](MATRIX-BOT-SETUP.md) guide. The listener service runs `print_handy.hermes.moonraker_event_listener` (a module name left over from the project's history), and only applies to Moonraker printers.

## 5. Check

```sh
python3 -m print_handy.doctor
```

## 6. Update

```sh
scripts/home-update.sh          # fast-forward, run checks, roll back on failure
```

Code is symlinked, so the pull is the update. Template changes do not touch files you already have. To restart the listener after an update, put `systemctl --user restart print-handy-listener.service` in `config/after-update.sh`.

`home-update.sh` runs the full test suite and resets the checkout to the previous commit if it fails, so run it on a checkout you keep no local changes in.

## Coming from the old Hermes install

The Hermes install is removed. Print Handy used to offer a second install that hosted the bot inside a Hermes Agent profile (`scripts/install-hermes.sh`, `bot.mode: hermes`). That install method and mode are gone, and a config that still says `mode: hermes` is refused at load with a message naming the removal.

To move an old Hermes-hosted install over:

1. Stop the old gateway's Discord connection first. One bot token, one live connection.
2. Keep your config and state where they are. `paths.hermes_home` is just the state folder's path, and it can keep pointing at the old profile directory.
3. Set `bot.mode` to `standalone`.
4. Install the services as in step 4 above.
