# Home Assistant OS app

Print Handy also runs as an app (formerly called an add-on) on Home Assistant OS and Supervised installs. It is the same published image the [Docker](DOCKER.md) and Unraid installs use, wrapped so Home Assistant can install, start, back up and update it. The app lives in its own repository, [print-handy-haos-addon](https://github.com/catduckgnaf/print-handy-haos-addon).

## Install

1. In Home Assistant, open **Settings > Apps > App store**, then the menu in the top right, then **Repositories**.
2. Add `https://github.com/catduckgnaf/print-handy-haos-addon`.
3. Install **Print Handy** and start it.
4. Select **Open Web UI** (port 8088). **Claim it in the walkthrough's Login step**: set a password, or record that you mean to leave it open. Until one of those is done, anyone who can reach port 8088 can use it, printer controls included. To have it claimed before the first start, set the app's **Web UI access token** option first.

The app needs 64-bit hardware (`amd64` or `aarch64`), like the image itself. A Raspberry Pi 4 or 5 on the 64-bit Home Assistant OS works; slicing large models is faster on an x86 machine.

## Settings and secrets

The settings file the Web UI edits is `print-handy.yaml` in the app's config folder (`addon_configs/<id>_print_handy/` in the File editor, Studio Code Server or Samba). The app creates it on the first start from the image's Docker defaults and never overwrites it. Settings are read once at start, so restart the app after editing the file by hand.

Secrets and overrides that a Docker install keeps in `.env` go in the app's **Extra environment variables** option, one name and value per entry: `DISCORD_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, `BAMBU_LAN_PASSWORD`, `AD5X_CHECK_CODE`, `MOONRAKER_API_KEY`, or any `PRINT_HANDY__SECTION__KEY` override. The timezone comes from Home Assistant.

## Data, backups and updates

Everything Print Handy keeps (state, print archive, records, the bundled Spoolman database and the account tokens under `.config`) lives in the app's data folder and is part of Home Assistant backups. The download cache is left out of backups.

Each app version runs the Print Handy release of the same number. A new Print Handy release reaches the app automatically: publishing the release starts the app's own build, and Home Assistant offers the update under **Settings > Apps** once the app image is published, usually within minutes. The app also checks for new releases on a schedule, as a fallback.

## Network

The app uses Home Assistant's normal app network, not the host network. Port 8088 is the Web UI and API. The bundled Spoolman API (container port 8000) is off unless you give it a host port under **Network** and turn on **Serve the bundled Spoolman on the network**, which Moonraker needs to reach it; it has no login, so keep it on your LAN. Printers are reached at the addresses in your settings. Discovery that relies on LAN broadcasts (such as finding Elegoo printers) does not cross the app network, so enter those printers' IP addresses yourself.

## Printers in Home Assistant

The [Print Handy integration](https://github.com/catduckgnaf/ha-print-handy) puts every printer on the roster in Home Assistant as a device, with live status, controls and spool sensors. Install it from HACS and use `http://<your Home Assistant IP>:8088` as the URL.
