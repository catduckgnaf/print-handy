# Install on a Hermes box

> **New installs default to the standalone bot**: `bot.mode` defaults to `standalone`, and the
> shipped config templates say so. Start with [docs/STANDALONE.md](STANDALONE.md) and
> `scripts/install-standalone.sh` unless you specifically want Print Handy hosted inside a Hermes
> profile. This page remains the install path for that explicit choice — `bot.mode: hermes` is still
> supported — and the migration notes at the bottom are for installs moving off it.

## Why Hermes mode needs a dedicated profile (any name)

Hermes mode is two things sharing one Discord connection: the bot, and the hosting profile's
AI agent. The bot claims what it claims — commands, model links, files, button flows — and
everything it claims is covered by the bot's own rules (the print channel is the access
boundary, with the user lists as opt-in refinement).

What the bot does *not* claim reaches the agent, by design. A plain-English request, an
unmatched message, a photo with no caption: those fall through to the hosting profile's
agent, and the agent is what carries them out. That fall-through is a feature of the mode —
it is how natural-language requests get executed — and it means the hosting profile is the
agent's fence. The profile carries the print-only doctrine (`SOUL.md` / `AGENTS.md`), the
bounded toolsets, the scoping to the one print channel and the approvals posture, and the
plugin's own tool guards supervise the agent's calls under the same identity.

So the profile must be **dedicated**: a profile that exists to host the printer bot. Hosting
the bot inside your general profile is not supported — print-channel messages would land in
front of your general assistant with its full tools and no print doctrine, and none of the
bounds above would apply.

The name is up to you. `3dprinting` is only the shipped default: create the profile under
any name, point `paths.hermes_home` at its directory (`~/.hermes/profiles/<name>`), and
install with `scripts/install-hermes.sh --hermes-home ~/.hermes/profiles/<name>`. The plugin
derives which profile it is running in from the install itself — there is no profile-name
setting. `python3 -m print_handy.bot check` shows the derived name on its hosting profile
row and fails that row when it cannot be derived.

The profile is *not* needed for the bot's channel/user access rules (the bot's own, above),
for where state lives (`paths.hermes_home` is just a path — standalone installs keep their
state in the same place with no profile at all), or for session resets (the profile
template's daily reset is the host's own behaviour). Standalone mode needs no profile for
the reason given in [docs/STANDALONE.md](STANDALONE.md): there is no agent behind the bot,
so an unclaimed message simply ends.

## 1. Clone

```sh
git clone https://github.com/<you>/print-handy.git ~/print-handy
cd ~/print-handy
python3 -m pip install -e ".[test]"
```

Needs Python 3.12+, Hermes (`~/.hermes/hermes-agent`), and OrcaSlicer as a Flatpak.
Stock Flashforge IFS occupancy is read from `/detail` and is not verified on a live printer.

## 2. Settings: YAML preferred, TOML supported during migration

Defaults live in `config/home.yaml`. Put machine-specific overrides in the gitignored `config/local.yaml`; existing `config/home.toml` and `config/local.toml` files remain supported. YAML takes precedence when both formats are present. See [the configuration guide](CONFIGURATION-YAML.md) for precedence, validation and migration details.

- `services.moonraker_url` and `services.spoolman_url`
- `paths.hermes_home`, `archive`, `records_vault`, `cache` and `hermes_bin`
- `printer.model` and supported printer-specific overrides

## 3. Overlay: `config/overlay/` (gitignored)

Your private profile files. Each is copied over the profile at the same relative path:
`SOUL.md`, `MEMORY.md`, `config.yaml`, `.env`, and `systemd/<unit>.service.d/*.conf`.
Without an overlay you get the generic templates in `hermes/profile/`.
Fill template values from the environment on first install:
`LLM_PROVIDER LLM_MODEL LLM_BASE_URL TIMEZONE DISCORD_CHANNEL_ID DISPLAY_NAME`.

## 4. Install

```sh
scripts/install-hermes.sh --dry-run   # show what would happen
scripts/install-hermes.sh
systemctl --user enable --now hermes-3dprinting-moonraker-listener.service
```

Options: `--hermes-home DIR`, `--python PATH`, env `SYSTEMD_USER_DIR`.
It symlinks the skills, `print-feedback` plugin and docs into the profile, renders templates only
where the target is missing, applies the overlay, and installs the systemd user units.
It also writes a `print_handy.pth` into the Hermes venv's site-packages so `python -m print_handy...`
works from any directory, and copies old `~/.print_bitch_*.json` (to the `print_handy_*` names) and `~/.moonraker_discord_announcements.json`
state files into `<hermes home>/state/` when the new copy is missing. An old private `scripts/` folder in the
profile is moved aside. Anything replaced is moved to `<hermes home>/.install-backups/<time>/<path>` (units go under
`<systemd dir>/.install-backups/`), never next to the original, so Hermes cannot load a backup as a duplicate
plugin or skill. Rerunning is safe.

The listener unit is named `hermes-3dprinting-moonraker-listener.service`, the same as the private
setup used, so installing it replaces the old unit instead of running a second listener.

## 5. Check

```sh
python3 -m print_handy.doctor
```

The doctor also checks the Hermes profile itself. If `platforms.discord.enabled` is `false` in
`<hermes home>/config.yaml`, the gateway never connects Discord — environment credentials do not
override an explicit platform disable — so attachment intake is silently absent while everything
else looks healthy. The doctor (and `python3 -m print_handy.bot check`) report that as a named
failure with the fix: set it to `true` and restart the gateway. Print Handy's template renders it
`true` and the installer never rewrites an existing profile config, so a `false` there always
comes from outside the installer: a private overlay `config.yaml`, a hand edit, or Hermes-side
tooling.

## 6. Update

```sh
scripts/home-update.sh          # fast-forward, run checks, roll back on failure
```

Code is symlinked, so the pull is the update. Template changes do not touch files you already have.
To restart the listener after an update, put `systemctl --user restart hermes-3dprinting-moonraker-listener.service`
in `config/after-update.sh`.

## Optional external plugins

Not shipped here. Install them yourself if you want them: `image_gen`.

## Migration notes: the default mode is now standalone

The `bot.mode` default flipped from `hermes` to `standalone`. Whether an update changes your
install depends on where your mode is stated:

- If your own `config/local.yaml` (or `config/local.toml`) states `mode: hermes`, nothing
  changes: the local file is merged over the shipped template, so an explicit local mode keeps
  resolving to Hermes exactly as before.
- If it does not — the common case for installs that followed this page and keep only
  machine-specific overrides in the local file — the mode comes from the shipped home template
  (`config/home.yaml`), and an update replaces that line with `mode: standalone`. The install
  would come up standalone after the update, and the updater's standalone doctor checks may
  fail. Neither the installer nor the updater carries the old mode over for you.

**Staying on Hermes: pin the mode before updating.** Add the mode to your local overrides
first — in `config/local.yaml`:

```yaml
bot:
  mode: hermes
```

or in `config/local.toml`:

```toml
[bot]
mode = "hermes"
```

Then update as usual (section 6). With the mode pinned in the local file, no template change
can move the install again.

If you move an install to standalone deliberately, do these in order
(the full checklist is [docs/STANDALONE.md](STANDALONE.md#moving-from-hermes)):

1. **One token, one connection:** stop the Hermes gateway's Discord platform (or remove the print
   channel from it) *before* starting the standalone bot. Two live connections on one bot token
   answer twice and race the button flows; the doctor's shared-token check
   (`python3 -m print_handy.bot check`) detects it, but stopping the gateway first is the control.
2. **Keep the agent (optional):** set `[agent] chat_url` (plus `api_key_env`) pointing at Hermes'
   API to keep stray-chat agent answers. Without it, stray chat gets the bounded `[llm]` call if one
   is configured, else the short reply. Set `[discord] chat_llm_enabled` explicitly if you had an
   opinion about chat answers.
3. **Design model moves:** if you used the profile's `auxiliary.print_design` pin, set `[design]
   model` (or `[llm]`) to the same model — the HTTP values are the normal path now.
4. **Session resets become Hermes' own:** the bot's post-print session reset/archive is retired in
   every mode; the profile's `session_reset: daily, at_hour 4` (already in the shipped template) is
   the remaining behaviour.
