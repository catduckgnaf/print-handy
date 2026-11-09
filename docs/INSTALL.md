# Install Print Handy (native, no Docker)

Print Handy runs as its own bot — there is one runtime. This page is the native install
(Python + systemd user services). The other install is the Docker image:
see [docs/DOCKER.md](DOCKER.md). The bot's behavior and settings are covered by
[docs/STANDALONE.md](STANDALONE.md).

> **The Hermes install is removed.** Print Handy used to offer a second install that
> hosted the bot inside a Hermes Agent profile (`scripts/install-hermes.sh`,
> `bot.mode: hermes`). That install method and mode are gone: a config that still says
> `mode: hermes` is refused at load with a message naming the removal. To move an old
> Hermes-hosted install over: keep your config and state where they are
> (`paths.hermes_home` is just the state folder's path — it can keep pointing at the old
> profile directory), set `bot.mode` to `standalone`, and install the standalone
> services below. Stop the old gateway's Discord connection first — one bot token, one
> live connection.

## 1. Clone

```sh
git clone https://github.com/<you>/print-handy.git ~/print-handy
cd ~/print-handy
python3 -m pip install -e ".[test]"
```

Needs Python 3.12+ and OrcaSlicer as a Flatpak.
Stock Flashforge IFS occupancy is read from `/detail` and is not verified on a live printer.

## 2. Settings: YAML preferred, TOML supported during migration

Defaults live in `config/home.yaml`. Put machine-specific overrides in the gitignored `config/local.yaml`; existing `config/home.toml` and `config/local.toml` files remain supported. YAML takes precedence when both formats are present. See [the configuration guide](CONFIGURATION-YAML.md) for precedence, validation and migration details.

- `services.moonraker_url` and `services.spoolman_url`
- `paths.hermes_home` (the state folder), `archive`, `records_vault` and `cache`
- `printer.model` and supported printer-specific overrides

The guided setup (`python3 -m print_handy.bot setup`) writes these with you; see
[docs/GUIDED-SETUP-DEBUGGING.md](GUIDED-SETUP-DEBUGGING.md).

## 3. Install

The setup wizard asks the questions and installs the services:

```sh
python3 -m print_handy.setup_wizard
```

Or install the systemd user services directly:

```sh
scripts/install-standalone.sh
systemctl --user enable --now print-handy-bot.service print-handy-listener.service
```

## 4. Check

```sh
python3 -m print_handy.doctor
```

## 5. Update

```sh
scripts/home-update.sh          # fast-forward, run checks, roll back on failure
```

Code is symlinked, so the pull is the update. Template changes do not touch files you already have.
To restart the listener after an update, put `systemctl --user restart print-handy-listener.service`
in `config/after-update.sh`.
