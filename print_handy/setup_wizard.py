"""Interactive setup for a native Print Handy install (Raspberry Pi or Debian/Ubuntu, no Docker).

    python3 -m print_handy.setup_wizard [--answers FILE] [--dry-run] [--yes]

Asks brand then model (or Other / generic), connection, nozzle, filament, slicer, Spoolman, camera and
timelapse, missing printer software, Discord and whether you run Hermes Agent. Shows a summary, then writes
config/local.yaml (existing keys always win; you see a diff first) and the secrets to the data folder's .env
(mode 600). Secrets are never printed.

Menus come from the printer registry, so a new printer model shows up here without editing this file.
Every question can be answered ahead of time with --answers (YAML or KEY=value) for an unattended install.

More than one printer (#337): after the first printer the wizard asks "Add another printer?"
(answers keys add_printer_2, add_printer_3, ...). Saying yes asks the first printer's id and own
channel (printer1_id, printer1_chat_id) and then, per added printer, the same brand/model and
connection questions with p2_/p3_-prefixed answer keys (p2_brand, p2_model, p2_lan_host, ...),
its id (p2_id) and its own channel (p2_chat_id; empty means the main print channel). Two or more
printers write a [[printers]] roster instead of the legacy single-printer sections. And an
existing config file is only ever rewritten when it parses and re-emits faithfully — a file the
wizard cannot round-trip is left byte-for-byte unchanged with instructions (see merge_config).
"""
from __future__ import annotations

import argparse
import difflib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/catduckgnaf/print-handy"
FLATPAK = {"orca": "com.orcaslicer.OrcaSlicer", "bambu_studio": "com.bambulab.BambuStudio"}
SLICER_LABEL = {"orca": "OrcaSlicer", "bambu_studio": "Bambu Studio"}
MATERIALS = ("PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC")
OTHER = "other"
# Bot permissions the code uses (docs/STANDALONE.md "Discord setup"). Values are Discord permission bits.
DISCORD_PERMISSIONS = {
    "View Channels": 1 << 10,
    "Send Messages": 1 << 11,
    "Embed Links": 1 << 14,
    "Attach Files": 1 << 15,
    "Read Message History": 1 << 16,
    "Send Messages in Threads": 1 << 38,
}
KIAUH = ["bash", "-c", "git clone https://github.com/dw-0/kiauh.git ~/kiauh && ~/kiauh/kiauh.sh"]
OCTOPRINT = ["bash", "-c", "python3 -m venv ~/OctoPrint/venv && ~/OctoPrint/venv/bin/pip install OctoPrint"]
SPOOLMAN = ["bash", "-c", "mkdir -p ~/Spoolman && cd ~/Spoolman && "
            "curl -fsSL -o spoolman.zip https://github.com/Donkie/Spoolman/releases/latest/download/spoolman.zip && "
            "unzip -oq spoolman.zip && rm spoolman.zip && bash scripts/install.sh"]


def discord_permissions():
    return dict(DISCORD_PERMISSIONS)


def discord_invite_url(client_id):
    perms = sum(DISCORD_PERMISSIONS.values())
    return (f"https://discord.com/oauth2/authorize?client_id={client_id or '<APPLICATION_ID>'}"
            f"&scope=bot+applications.commands&permissions={perms}")


DISCORD_STEPS = """\
Discord bot setup (full guide: docs/DISCORD-BOT-SETUP.md)
  1. https://discord.com/developers/applications > New Application > Bot > Reset Token. Copy the token.
  2. Bot > Privileged Gateway Intents: turn on MESSAGE CONTENT. Nothing else is needed.
  3. Copy the Application ID from General Information; the invite link is printed next.
  4. Discord User Settings > Advanced > Developer Mode on. Right-click the print channel > Copy Channel ID,
     right-click yourself > Copy User ID."""


def parse_answers(path):
    """Read an answers file: a flat YAML mapping (key: value) or KEY=value lines."""
    if not path:
        return {}
    values = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sep = "=" if "=" in line and (":" not in line or line.index("=") < line.index(":")) else ":"
        if sep not in line:
            continue
        key, value = line.split(sep, 1)
        values[key.strip().lower()] = value.strip().strip("\"'")
    return values


#: Environment prefix for preset wizard answers (#590): a question's
#: preset is read from PRINT_HANDY_WIZARD_<KEY>. This is a clean break
#: from the old PB_<KEY> spelling (the #446/#504 rename pattern, no
#: alias): the variables are setup-time answer presets only — nothing
#: in a running install reads them — so an unmigrated preset simply
#: means the wizard asks the question again instead of pre-filling it.
#: See docs/RENAME-MIGRATION.md.
ENV_PREFIX = "PRINT_HANDY_WIZARD_"


class Wizard:
    """Question helper: answers file, then PRINT_HANDY_WIZARD_<KEY> environment, then the terminal."""

    def __init__(self, answers=None, input_stream=None, output=None, yes=False, environ=None):
        self.answers = dict(answers or {})
        self.input = input_stream if input_stream is not None else io.StringIO("")
        self.output = output or sys.stdout
        self.yes = yes
        self.environ = os.environ if environ is None else environ
        self.choices = {}

    def say(self, text=""):
        print(text, file=self.output, flush=True)

    def _preset(self, key):
        if key in self.answers:
            return str(self.answers[key])
        return self.environ.get(ENV_PREFIX + key.upper())

    def _read(self):
        line = self.input.readline()
        return line.rstrip("\r\n").strip()

    def ask(self, key, prompt, default="", validate=None):
        preset = self._preset(key)
        while True:
            if preset is not None:
                value = preset
                self.say(f"{prompt}: {value}")
            elif self.yes:
                value = default
                self.say(f"{prompt}: {value}")
            else:
                print(f"{prompt}" + (f" [{default}]" if default else "") + ": ", end="", file=self.output, flush=True)
                value = self._read() or default
            error = validate(value) if validate else None
            if not error:
                self.choices[key] = value
                return value
            if preset is not None or self.yes:
                raise SystemExit(f"setup: {key}: {error}")
            self.say(f"  {error}")

    def confirm(self, key, prompt, default=True):
        hint = "Y/n" if default else "y/N"
        value = self.ask(key, f"{prompt} [{hint}]", "yes" if default else "no")
        return value.strip().lower() in ("y", "yes", "true", "1")

    def secret(self, key, prompt):
        preset = self._preset(key)
        if preset is not None:
            value = preset
        elif self.yes:
            value = ""
        else:
            print(f"{prompt} (hidden, Enter to skip): ", end="", file=self.output, flush=True)
            if hasattr(self.input, "fileno") and _isatty(self.input):
                import getpass
                value = getpass.getpass("", stream=self.output)
            else:
                value = self._read()
        self.say(f"{prompt}: {'(set)' if value else '(not set)'}")
        return value.strip()

    def menu(self, key, prompt, options, default, labels=None):
        """options are internal values; the user picks by number or by value."""
        labels = labels or options
        preset = self._preset(key)
        self.say(prompt)
        for i, label in enumerate(labels, 1):
            mark = "  (default)" if options[i - 1] == default else ""
            self.say(f"  {i}. {label}{mark}")
        while True:
            if preset is not None:
                raw = preset
            elif self.yes:
                raw = default
            else:
                print(f"Number [{options.index(default) + 1}]: ", end="", file=self.output, flush=True)
                raw = self._read() or default
            value = _pick(raw, options)
            if value is not None:
                self.choices[key] = value
                if preset is not None or self.yes:
                    self.say(f"  -> {labels[options.index(value)]}")
                return value
            if preset is not None or self.yes:
                raise SystemExit(f"setup: {key}: {raw!r} is not one of {', '.join(options)}")
            self.say(f"  Pick 1 to {len(options)}.")


def _isatty(stream):
    try:
        return os.isatty(stream.fileno())
    except (OSError, ValueError, io.UnsupportedOperation):
        return False


def _pick(raw, options):
    raw = str(raw).strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1]
    lowered = {o.lower(): o for o in options}
    return lowered.get(raw.lower())


def _url(value):
    if value and not re.match(r"^https?://[^\s/]+", value):
        return "must start with http:// or https://"
    return None


def _host(value):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value or ""):
        return "must be a plain IP address or hostname, without http:// or a port"
    return None


def _serial(value):
    if not re.fullmatch(r"[A-Za-z0-9]+", value or ""):
        return "must be the printer's alphanumeric serial number"
    return None


def _ids(value):
    if value and not all(p.isdigit() for p in re.split(r"[,\s]+", value.strip()) if p):
        return "Discord IDs are numbers (Developer Mode > Copy ID); separate several with commas"
    return None


def _positive(value):
    try:
        if float(value) > 0:
            return None
    except ValueError:
        pass
    return "must be a positive number"


def profile_name_error(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value or ""):
        return "lowercase letters, digits, - or _, starting with a letter or digit"
    return None


def registry():
    from . import printers
    return printers.VENDORS, printers.MODELS


def choose_printer(w, prefix=""):
    """Brand, then model (or Other / generic). Returns (model key, printer overrides, display name).

    ``prefix`` namespaces the answer keys ("" for the first printer, "p2_" for the
    second, and so on) so an answers file can describe every printer in the
    add-a-printer loop without the answers colliding."""
    vendors, models = registry()
    brands = list(vendors) + [OTHER]
    brand = w.menu(prefix + "brand", "Printer brand:", brands, brands[0],
                   [vendors[k].label for k in vendors] + ["Other / generic"])
    generic = brand == OTHER
    if not generic:
        keys = [k for k in models if k.startswith(brand + ".")]
        options = keys + [OTHER]
        labels = [getattr(models[k], "label", "") or models[k].key.split(".", 1)[1].upper() for k in keys]
        labels.append(f"Other / generic {vendors[brand].label}")
        picked = w.menu(prefix + "model", f"{vendors[brand].label} model:", options, keys[0] if keys else OTHER, labels)
        generic = picked == OTHER
        if not generic:
            model = models[picked]
            label = labels[keys.index(picked)]
            vendor_label = vendors[brand].label
            return picked, {}, label if label.lower().startswith(vendor_label.lower()) else f"{vendor_label} {label}"
    # Other / generic: pick the firmware family, then describe the machine.
    families = [("klipper", "klipper.generic"), ("octoprint", "octoprint.marlin"), ("bambulab", "bambulab.a1")]
    families = [(f, m) for f, m in families if m in models]
    family = w.menu(prefix + "firmware", "Printer firmware:", [f for f, _ in families], families[0][0],
                    ["Klipper (Moonraker)", "Marlin with OctoPrint", "Bambu Lab (offline: slice and check only)"][:len(families)])
    key = dict(families)[family]
    name = w.ask(prefix + "printer_name", "Printer name (for messages)",
                 vendors[brand].label + " printer" if brand != OTHER else "My printer")
    defaults = models[key].defaults()["printer"]
    x = float(w.ask(prefix + "bed_x", "Bed size X (mm)", f"{defaults['bed_mm'][0]:g}", _positive))
    y = float(w.ask(prefix + "bed_y", "Bed size Y (mm)", f"{defaults['bed_mm'][1]:g}", _positive))
    z = float(w.ask(prefix + "bed_z", "Max print height (mm)", f"{defaults['max_print_height_mm']:g}", _positive))
    return key, {"bed_mm": [x, y], "max_print_height_mm": z, "name": name}, name


def _slug_id(name):
    """The roster id a display name suggests: the settings loader's own slug rule."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").lower()).strip("-_")
    return slug or "printer"


def _printer_id_error(value, taken):
    """Validate a roster id against the settings rule (settings.PRINTER_ID) plus uniqueness."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value or ""):
        return "printer ids are slugs: lowercase letters, digits, - or _, starting with a letter or digit"
    if value in taken:
        return f"printer id {value!r} is already used by another printer in this setup"
    return None


def _ids_or_empty(value):
    if not str(value or "").strip():
        return None
    return _ids(value)


def _ask_printer(w, prefix="", roster=False):
    """One printer's questions: identity, connection, nozzle (and, for the first
    printer, filament and slicer). Returns a record dict; the global questions
    (Spoolman, Discord, Hermes) live in build_plan. ``roster`` is set for printers
    after the first in the add-a-printer loop: only what a [[printers]] entry can
    express is asked (model, connection, nozzle type) — nozzle size, slicer and
    main filament are model defaults there, not roster keys."""
    vendors, models = registry()
    key, printer, display = choose_printer(w, prefix)
    model = models[key]
    vendor = model.vendor
    printer = {"model": key, **printer}
    services, notes, actions = {}, [], []

    # Thermal facts (issue #720): guided setup surfaces only the facts that
    # are unknown for this model, the way it asks about filament and
    # nozzles for known choices. The questions are shown as a note and kept
    # on the record; no answer is recorded as a setting and nothing is
    # inferred from "enclosed".
    thermal_questions = []
    try:
        from .knowledge import thermal_entry_for_model, thermal_setup_questions
        thermal_entry = thermal_entry_for_model(key)
        if thermal_entry is not None:
            thermal_questions = thermal_setup_questions(thermal_entry)
    except Exception:
        thermal_questions = []
    if thermal_questions:
        notes.append(f"Thermal facts still unknown for {display} (not assumed): "
                     + "; ".join(q["question"] for q in thermal_questions))

    # Connection
    connection = vendor.connection
    bambu_lan, bambu_code = {}, ""
    stock_lan, stock_serial, stock_code = {}, "", ""
    prusa_link, prusa_key = {}, ""
    elegoo_sdcp_host = {}
    if connection == "offline":
        notes.append(f"{vendor.label}: no live printer connection. Print Handy slices, checks G-code and "
                     "packages the file; you start the print yourself.")
    if connection == "bambulab_lan":
        notes.append(f"{vendor.label}: Print Handy slices, checks G-code and packages the file; you start "
                     "the print yourself. Read-only LAN status is optional and never starts anything.")
        if w.confirm(prefix + "bambu_lan", "Enable read-only LAN status for this printer (MQTT telemetry)?", default=False):
            bambu_lan = {"lan_host": w.ask(prefix + "lan_host", "Printer LAN IP address or hostname", "", _host),
                         "lan_serial": w.ask(prefix + "lan_serial", "Printer serial number (on the printer / Settings > WLAN)", "", _serial)}
            bambu_code = w.secret(prefix + "bambu_lan_password", "Printer LAN access code (Settings > WLAN; stored as a secret)")
            notes.append("LAN status needs the printer in LAN Mode (Bambu Handy then cannot reach it) and "
                         "the access code above; Print Handy only reads status in this stage.")
    if connection == "flashforge_stock":
        notes.append(f"{vendor.label}: Print Handy uploads one .gcode and starts it through the printer's own "
                     "LAN HTTP API. There is no Moonraker, no ZMod, and no multicolor. Leave the address blank "
                     "to stay offline until the printer is on the network.")
        # Empty is valid: the printer stays offline until [flashforge_stock] host is set.
        # A typed value still has to be a plain address, the same rule as the Bambu LAN host.
        host = w.ask(
            prefix + "stock_host",
            "Printer LAN IP address or hostname (port 8898; Enter to leave offline)",
            "",
            lambda value: _host(value) if str(value or "").strip() else None,
        )
        if str(host or "").strip():
            stock_lan = {"host": host.strip()}
            stock_serial = w.ask(prefix + "stock_serial", "Printer serial number (on the printer; stored as a secret)", "", _serial)
            stock_code = w.secret(prefix + "stock_check_code", "Printer check code (stored as a secret)")
    if connection == "prusalink":
        notes.append(f"{vendor.label}: Print Handy slices and checks the file; with the printer's "
                     "PrusaLink address set it can also read status, upload the verified file and "
                     "start it after the usual confirmation. Leave the address blank to stay offline.")
        host = w.ask(
            prefix + "prusa_host",
            "Printer LAN IP address or hostname (PrusaLink; Enter to leave offline)",
            "",
            lambda value: _host(value) if str(value or "").strip() else None,
        )
        if str(host or "").strip():
            prusa_link = {"link_host": host.strip()}
            prusa_key = w.secret(prefix + "prusa_link_api_key", "PrusaLink API key (printer display, Settings > Network > PrusaLink; stored as a secret)")
    if connection == "elegoo_sdcp":
        notes.append(f"{vendor.label}: Print Handy slices and checks the file; with the printer's "
                     "LAN address set it can also read status, upload the verified file and start "
                     "it after the usual confirmation. SDCP has no password or key — the printer "
                     "must be on a network you trust. Leave the address blank to stay offline.")
        host = w.ask(
            prefix + "elegoo_sdcp_host",
            "Printer LAN IP address or hostname (SDCP; Enter to leave offline)",
            "",
            lambda value: _host(value) if str(value or "").strip() else None,
        )
        if str(host or "").strip():
            elegoo_sdcp_host = {"host": host.strip()}
    has_software = True
    if connection == "moonraker":
        has_software = w.confirm(prefix + "has_printer_software", "Is Klipper with Moonraker already running on the printer?")
        services["moonraker_url"] = w.ask(prefix + "moonraker_url", "Moonraker URL", "http://printer.local:7125", _url)
        if not has_software:
            actions.append(("Install Klipper, Moonraker and Mainsail with KIAUH (official, menu-driven)", KIAUH))
    elif connection == "octoprint":
        has_software = w.confirm(prefix + "has_printer_software", "Is OctoPrint already running?")
        services["octoprint_url"] = w.ask(prefix + "octoprint_url", "OctoPrint URL", "http://octopi.local", _url)
        if not has_software:
            actions.append(("Install OctoPrint into ~/OctoPrint/venv (official pip install)", OCTOPRINT))
    octoprint_key = w.secret(prefix + "octoprint_api_key", "OctoPrint API key") if connection == "octoprint" else ""

    # Nozzle and filament
    material, slicer, flatpaks = "", "", []
    if not roster:
        sizes = [f"{s:g}" for s in model.supported_nozzle_sizes]
        size = w.menu(prefix + "nozzle_diameter_mm", "Nozzle size (mm):", sizes, "0.4" if "0.4" in sizes else sizes[0])
        printer["nozzle_diameter_mm"] = float(size)
    types = list(model.supported_nozzle_types)
    printer["nozzle_type"] = w.menu(prefix + "nozzle_type", "Nozzle material:", types, types[0],
                                     [t.replace("_", " ") for t in types])
    if not roster:
        material = w.menu(prefix + "material", "Main filament:", list(MATERIALS), "PLA")

        # Slicer: Bambu Lab uses Bambu Studio, everything else OrcaSlicer.
        slicers = list(vendor.slicers)
        slicer = slicers[0]
        if len(slicers) > 1:
            slicer = w.menu(prefix + "slicer", "Slicer:", slicers, slicers[0], [SLICER_LABEL[s] for s in slicers])
        printer["slicer"] = slicer
        install_slicer = w.confirm(prefix + "install_slicer", f"Install {SLICER_LABEL[slicer]} from Flathub if missing?")
        flatpaks = [FLATPAK[slicer]] if install_slicer else []

    return {
        "key": key, "printer": printer, "display": display, "model": model, "vendor": vendor,
        "connection": connection, "services": services, "notes": notes, "actions": actions,
        "bambu_lan": bambu_lan, "bambu_code": bambu_code, "stock_lan": stock_lan,
        "stock_serial": stock_serial, "stock_code": stock_code, "prusa_link": prusa_link,
        "prusa_key": prusa_key, "elegoo_sdcp_host": elegoo_sdcp_host,
        "octoprint_key": octoprint_key, "has_software": has_software,
        "material": material, "slicer": slicer, "flatpaks": flatpaks,
        "thermal_questions": thermal_questions,
        "id": "", "chat_id": "",
    }


def _ask_camera(w, rec, prefix=""):
    """The camera/timelapse questions for one printer record (mutates its services/notes)."""
    services, notes = rec["services"], rec["notes"]
    if w.confirm(prefix + "camera", "Do you have a printer camera?", default=False):
        default_cam = ""
        if services.get("moonraker_url"):
            host = services["moonraker_url"].split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]
            default_cam = f"http://{host}/webcam/?action=snapshot"
        cam = w.ask(prefix + "camera_snapshot_url", "Camera snapshot URL", default_cam, _url)
        if cam:
            services["camera_snapshot_url"] = cam
        if rec["connection"] == "moonraker" and w.confirm(prefix + "timelapse", "Post timelapse videos after prints?", default=False):
            notes.append("Timelapse needs moonraker-timelapse on the printer (KIAUH > Install > Timelapse). "
                         "Print Handy posts the finished video; archiving is optional (timelapse.* settings).")


def _claim_secret(secrets, notes, name, value, rec):
    """Store a secret under a name shared by every printer (the stock Flashforge and
    OctoPrint secrets have one global env name in this version). The first value
    wins — update_env would keep it anyway — and a differing later value is called
    out in the notes instead of being silently dropped."""
    if not value:
        return
    if name in secrets and secrets[name] != value:
        notes.append(f"{rec['display']}: its {name} value differs from an earlier printer's; the .env "
                     "keeps the first value, so set this printer's secret by hand if they must differ.")
        return
    secrets[name] = value


def build_plan(w, *, hermes_found=False, existing_profiles=()):
    """Ask every question. Returns a plan dict; nothing is written or run here."""
    first = _ask_printer(w)
    key, printer, display = first["key"], first["printer"], first["display"]
    model, vendor = first["model"], first["vendor"]
    services, notes, actions = first["services"], first["notes"], first["actions"]
    bambu_lan, bambu_code = first["bambu_lan"], first["bambu_code"]
    stock_lan, stock_serial, stock_code = first["stock_lan"], first["stock_serial"], first["stock_code"]
    prusa_link, prusa_key = first["prusa_link"], first["prusa_key"]
    elegoo_sdcp_host = first["elegoo_sdcp_host"]
    octoprint_key = first["octoprint_key"]
    material, slicer = first["material"], first["slicer"]
    flatpaks = first["flatpaks"]

    # Spoolman: one inventory for the whole household, so a global question.
    spool = w.menu("spoolman", "Spoolman (filament inventory):", ["skip", "existing", "install"], "skip",
                   ["Skip", "I already run Spoolman", "Install Spoolman on this machine (official installer)"])
    if spool == "existing":
        services["spoolman_url"] = w.ask("spoolman_url", "Spoolman URL", "http://spoolman.local:7912", _url)
    elif spool == "install":
        services["spoolman_url"] = "http://localhost:7912"
        actions.append(("Install Spoolman into ~/Spoolman (official release and scripts/install.sh)", SPOOLMAN))

    # Camera and timelapse for the first printer.
    _ask_camera(w, first)

    # Discord
    w.say()
    if not w.confirm("have_bot", "Do you already have a Discord bot token?", default=False):
        w.say(DISCORD_STEPS)
    client_id = w.ask("discord_app_id", "Discord Application ID (for the invite link, optional)", "", _ids)
    w.say(f"Invite link: {discord_invite_url(client_id)}")
    chat_id = w.ask("discord_chat_id", "Print channel ID", "", _ids)
    users = w.ask("discord_user_ids", "Discord user IDs allowed to print (comma separated; "
                                      "blank = everyone in the print channel)", "", _ids)
    bot_name = w.ask("bot_name", "Bot name", "Print Handy")
    token = w.secret("discord_bot_token", "Discord bot token")

    # Hermes or standalone
    w.say()
    w.say("Print Handy runs either as its own Discord bot (standalone) or as a Hermes Agent profile.")
    hermes = w.confirm("hermes", "Do you use Hermes Agent?" + (" (found on this machine)" if hermes_found else ""),
                       default=False)
    profile = ""
    if hermes:
        profile = w.ask("profile", "Hermes profile name", "3dprinting", profile_name_error)
    # Add-a-printer loop (#337): a second printer turns the plan into a [[printers]]
    # roster. The first printer's id and channel are only needed then, so they are
    # asked when the loop is first entered.
    records = [first]
    taken_ids = set()
    fallback_owner = display  # the main channel is the first printer's unless it claims its own
    number = 1
    while w.confirm(f"add_printer_{number + 1}", "Add another printer?", default=False):
        number += 1
        prefix = f"p{number}_"
        if number == 2:
            first["id"] = w.ask("printer1_id", "Give the first printer an id (its state folder and channel binding use it)",
                                _slug_id(display), lambda v: _printer_id_error(v, taken_ids))
            taken_ids.add(first["id"])
            first["chat_id"] = w.ask("printer1_chat_id",
                                     "First printer's own channel ID (Enter = the main print channel above)", "", _ids_or_empty)
            fallback_owner = display if not first["chat_id"] else ""
        rec = _ask_printer(w, prefix, roster=True)
        rec["id"] = w.ask(prefix + "id", f"Printer {number} id (its state folder and channel binding use it)",
                          _slug_id(rec["display"]), lambda v: _printer_id_error(v, taken_ids))
        taken_ids.add(rec["id"])

        def chat_error(value, owner=fallback_owner):
            # Only one printer may ride the main channel: two entries claiming it
            # are refused at load, so the wizard never writes that config.
            error = _ids_or_empty(value)
            if error:
                return error
            if not value.strip() and owner:
                return (f"the main print channel already belongs to {owner} — "
                        "give this printer its own channel ID")
            return None

        rec["chat_id"] = w.ask(prefix + "chat_id",
                               f"Printer {number}'s own channel ID (Enter = the main print channel)", "", chat_error)
        if not rec["chat_id"]:
            fallback_owner = rec["display"]
        _ask_camera(w, rec, prefix)
        records.append(rec)
        notes.extend(rec["notes"])
        actions.extend(rec["actions"])

    timezone = detect_timezone()
    discord = {"chat_id": chat_id, "allowed_user_ids": users, "bot_name": bot_name, "timezone": timezone}
    data_home = f"~/.hermes/profiles/{profile}" if hermes else "~/.local/share/print-handy"
    roster_rows = []
    if len(records) == 1:
        config = {
            "printer": printer,
            "services": services,
            "discord": {k: v for k, v in discord.items() if v},
            "bot": {"mode": "hermes" if hermes else "standalone"},
            "paths": {"hermes_home": data_home},
        }
        if bambu_lan:
            config["bambu"] = bambu_lan
        if stock_lan:
            config["flashforge_stock"] = stock_lan
        if prusa_link:
            config["prusa"] = prusa_link
        if elegoo_sdcp_host:
            config["elegoo_sdcp"] = elegoo_sdcp_host
        secrets = {"DISCORD_BOT_TOKEN": token, "OCTOPRINT_API_KEY": octoprint_key,
                   "BAMBU_LAN_PASSWORD": bambu_code, "AD5X_SERIAL": stock_serial,
                   "AD5X_CHECK_CODE": stock_code, "PRUSA_LINK_API_KEY": prusa_key}
    else:
        entries, secrets = [], {"DISCORD_BOT_TOKEN": token}
        for rec in records:
            entry = {"id": rec["id"], "name": rec["display"], "model": rec["key"]}
            if rec["chat_id"]:
                entry["chat_id"] = rec["chat_id"]
            for svc_key in ("moonraker_url", "octoprint_url", "camera_snapshot_url"):
                if rec["services"].get(svc_key):
                    entry[svc_key] = rec["services"][svc_key]
            if rec["printer"].get("nozzle_type"):
                entry["nozzle_type"] = rec["printer"]["nozzle_type"]
            if rec["bambu_lan"]:
                env_name = "BAMBU_LAN_PASSWORD_" + rec["id"].upper().replace("-", "_")
                entry.update(rec["bambu_lan"])
                entry["lan_access_code_env"] = env_name
                if rec["bambu_code"]:
                    secrets[env_name] = rec["bambu_code"]
            if rec["stock_lan"]:
                entry.update(rec["stock_lan"])
                _claim_secret(secrets, notes, "AD5X_SERIAL", rec["stock_serial"], rec)
                _claim_secret(secrets, notes, "AD5X_CHECK_CODE", rec["stock_code"], rec)
            if rec["prusa_link"]:
                env_name = "PRUSA_LINK_API_KEY_" + rec["id"].upper().replace("-", "_")
                entry.update(rec["prusa_link"])
                entry["api_key_env"] = env_name
                if rec["prusa_key"]:
                    secrets[env_name] = rec["prusa_key"]
            if rec["elegoo_sdcp_host"]:
                entry.update(rec["elegoo_sdcp_host"])
            _claim_secret(secrets, notes, "OCTOPRINT_API_KEY", rec["octoprint_key"], rec)
            entries.append(entry)
            roster_rows.append({"id": rec["id"], "display": rec["display"], "model": rec["key"],
                                "chat_id": rec["chat_id"]})
        config = {
            "printers": entries,
            "services": {k: v for k, v in services.items() if k == "spoolman_url"},
            "discord": {k: v for k, v in discord.items() if v},
            "bot": {"mode": "hermes" if hermes else "standalone"},
            "paths": {"hermes_home": data_home},
        }
        notes.append("Multi-printer: the roster lives in the config file (it cannot come from the "
                     "environment); any entry value can be overridden later with the indexed spelling "
                     "PRINT_HANDY__PRINTERS__<ID>__<KEY>. Each printer's channel is its own — "
                     "approvals in a channel can only start that channel's printer.")
        notes.append("Multi-printer: nozzle size, slicer and main filament are each model's defaults "
                     "in a roster config; they are not per-printer keys in this version.")
    return {
        "config": config, "display": display, "material": material, "slicer": slicer, "flatpaks": flatpaks,
        "actions": actions, "notes": notes, "hermes": hermes, "profile": profile,
        "profile_exists": profile in existing_profiles, "data_home": data_home, "timezone": timezone,
        "secrets": secrets,
        "hermes_env": {"DISCORD_ALLOWED_USERS": users.replace(" ", ""), "DISCORD_HOME_CHANNEL": chat_id},
        "spoolman": spool,
        "printers": roster_rows,
    }


def detect_timezone():
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return Path("/etc/timezone").read_text().strip() or "UTC"
    except OSError:
        return "UTC"


def build_hermes_commands(profile, display, repo, python, *, channel_id="", bot_name="Print Handy",
                          timezone="UTC", display_name="3D Printing", profile_exists=False):
    """Commands that set up a Hermes profile for Print Handy. Never clones another profile."""
    commands = []
    if not profile_exists:
        commands.append(["hermes", "profile", "create", profile, "--description",
                         f"Print Handy 3D printer specialist: slicing, printing, and printer status for {display}"])
    env = {"DISCORD_CHANNEL_ID": channel_id, "BOT_NAME": bot_name, "TIMEZONE": timezone,
           "DISPLAY_NAME": display_name}
    commands.append(["env", *(f"{k}={v}" for k, v in env.items() if v), f"{repo}/scripts/install-hermes.sh",
                     "--hermes-home", str(Path.home() / ".hermes" / "profiles" / profile), "--python", str(python)])
    return commands


def show(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def hermes_profiles(env=None, which=shutil.which):
    """Profile names from `hermes profile list` (the name in parentheses, else the first column)."""
    path = (env or os.environ).get("PATH")
    exe = which("hermes", path=path)
    if not exe:
        return set()
    try:
        out = subprocess.run([exe, "profile", "list"], capture_output=True, text=True, timeout=30, env=env).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    names = set(re.findall(r"\(([a-z0-9][a-z0-9_-]*)\)", out))
    return names


def summary(plan):
    c = plan["config"]
    lines = ["Summary"]
    if plan.get("printers"):
        for row in plan["printers"]:
            lines.append(f"  Printer:     {row['display']} ({row['model']}), id {row['id']}, "
                         f"channel {row['chat_id'] or 'the main print channel'}")
        lines.append("  Slicer:      each model's default (multi-printer configs carry no per-printer slicer)")
    else:
        p = c["printer"]
        lines += [
            f"  Printer:     {plan['display']} ({p['model']}), nozzle {p['nozzle_diameter_mm']:g} mm "
            f"{p['nozzle_type'].replace('_', ' ')}, main filament {plan['material']}",
            f"  Slicer:      {SLICER_LABEL[plan['slicer']]}" + (" (install from Flathub)" if plan["flatpaks"] else ""),
        ]
    lines += [
        f"  Services:    " + (", ".join(f"{k}={v}" for k, v in c["services"].items()) or "none"),
        f"  Discord:     channel {c['discord'].get('chat_id') or '(not set)'}, "
        f"users {c['discord'].get('allowed_user_ids') or '(everyone in the channel)'}, "
        f"token {'set' if plan['secrets']['DISCORD_BOT_TOKEN'] else 'not set'}",
        f"  Runs as:     " + (f"Hermes profile '{plan['profile']}'" + (" (exists, reused)" if plan["profile_exists"] else "")
                             if plan["hermes"] else "standalone bot (systemd user services)"),
        f"  Data folder: {plan['data_home']} (state, logs, .env with secrets)",
    ]
    for label, cmd in plan["actions"]:
        lines.append(f"  Install:     {label}")
    lines += [f"  Note:        {n}" for n in plan["notes"]]
    return "\n".join(lines)


def _dump(data):
    import yaml
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=None)


def _merge_missing(existing, proposed):
    out = dict(existing)
    for key, value in proposed.items():
        if key not in out:
            out[key] = value
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge_missing(out[key], value)
    return out


def _load_for_rewrite(text):
    """Parse an existing config file for a wizard rewrite, or return None when the file
    cannot be re-emitted faithfully: it does not parse, it repeats a key, its top level
    is not a mapping, or a parse -> dump -> parse round trip changes the data. A None
    result means the round-trip rule applies: the file is left byte-for-byte unchanged
    (#337 — setup write-back is the one place configs are written, so a config the
    wizard cannot round-trip is never partially rewritten)."""
    import yaml

    class _StrictLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            keys = [k.value for k, _ in node.value if isinstance(k, yaml.ScalarNode)]
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate key in mapping")
            return super().construct_mapping(node, deep=deep)

    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except (yaml.YAMLError, ValueError):
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        return None
    try:
        if yaml.safe_load(_dump(data)) != data:
            return None
    except yaml.YAMLError:
        return None
    return data


def merge_config(target, new, w, dry_run=False):
    """Write config/local.yaml. Existing keys always win; an existing file changes only after a yes."""
    target = Path(target)
    header = "# Written by the Print Handy setup wizard. Edit freely; re-running the wizard keeps your values.\n"
    if not target.exists():
        text = header + _dump(new)
        if dry_run:
            w.say(f"would write {target}:\n{text}")
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, text, 0o644)
        w.say(f"wrote {target}")
        return True
    old_text = target.read_text(encoding="utf-8")
    existing = _load_for_rewrite(old_text)
    if existing is None:
        w.say(f"{target} was left unchanged: it cannot be rewritten safely (it does not parse, "
              "repeats a key, or would change meaning when re-emitted). Fix it by hand "
              "(docs/CONFIGURATION-YAML.md), or move it aside and re-run setup to start fresh.")
        return False
    merged = _merge_missing(existing, new)
    new_text = _dump(merged)
    if existing == merged:
        w.say(f"{target} already has every setting; left unchanged.")
        return True
    w.say("".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                       str(target), "with new settings")))
    if dry_run:
        return True
    if not w.confirm("merge_config", f"Add the missing settings to {target.name}? (comments are not kept)", default=False):
        w.say(f"{target} left unchanged.")
        return False
    _atomic_write(target, header + new_text, 0o644)
    return True


def _atomic_write(path, text, mode):
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_env(folder, values):
    """Add KEY=value lines that are missing or empty. Existing values are kept. Mode 600."""
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    env = folder / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else ["# Print Handy secrets"]
    present = {l.split("=", 1)[0]: i for i, l in enumerate(lines) if "=" in l and not l.startswith("#")}
    changed = []
    for key, value in values.items():
        if not value:
            continue
        if key in present:
            if lines[present[key]].split("=", 1)[1].strip():
                continue
            lines[present[key]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
        changed.append(key)
    if changed or not env.exists():
        _atomic_write(env, "\n".join(lines) + "\n", 0o600)
    else:
        env.chmod(0o600)
    return changed


def apply(plan, w, *, repo=REPO, python=None, dry_run=False, runner=subprocess.run, local=None):
    """Write config and secrets, then run the installs the user agreed to."""
    python = python or sys.executable
    local = Path(local) if local else repo / "config" / "local.yaml"
    if not merge_config(local, plan["config"], w, dry_run):
        w.say("Continuing with your existing config/local.yaml.")

    def run(label, cmd, ask=True, key=None):
        w.say(f"\n{label}:\n  $ {show(cmd)}")
        if dry_run:
            return True
        if ask and not w.confirm(key or "run", "Run this now?"):
            w.say("  Skipped. You can run it later.")
            return False
        result = runner(cmd)
        if getattr(result, "returncode", 0) != 0:
            w.say(f"  Failed with exit code {result.returncode}. Fix the cause and re-run the wizard.")
            return False
        return True

    for app in plan["flatpaks"]:
        run(f"Install {app} from Flathub (skipped automatically if already installed)",
            ["bash", "-c", f"flatpak info {app} >/dev/null 2>&1 || flatpak install --user -y --noninteractive flathub {app}"],
            ask=False)
    for label, cmd in plan["actions"]:
        run(label, cmd, key="run_" + label.split()[1].lower())

    secrets = {k: v for k, v in plan["secrets"].items() if v}
    if plan["hermes"]:
        name = plan["profile"]
        cmds = build_hermes_commands(name, plan["display"], repo, python,
                                     channel_id=plan["config"]["discord"].get("chat_id", ""),
                                     bot_name=plan["config"]["discord"].get("bot_name", "Print Handy"),
                                     timezone=plan["timezone"], profile_exists=plan["profile_exists"])
        if plan["profile_exists"]:
            w.say(f"\nHermes profile '{name}' already exists; it is reused, not recreated.")
        w.say("The standalone bot services are not installed in Hermes mode: two bots on one token collide.")
        ok = True
        for cmd in cmds:
            ok = ok and run("Hermes setup", cmd, key="run_hermes")
        home = Path.home() / ".hermes" / "profiles" / name
        if ok and not dry_run:
            changed = update_env(home, {**secrets, **{k: v for k, v in plan["hermes_env"].items() if v}})
            w.say(f"Saved {', '.join(changed) or 'nothing new'} to {home}/.env (values not shown).")
        w.say(f"\nNext:\n  1. hermes -p {name} setup        # pick the model and API key for this profile"
              f"\n  2. hermes gateway restart        # or: hermes -p {name} gateway install --if-missing --start-now"
              f"\n  3. In Discord, @mention the bot in the print channel: \"status\"")
    else:
        run("Install the standalone bot services (enables nothing yet)",
            [str(repo / "scripts" / "install-standalone.sh"), "--python", str(python)], ask=False)
        if not dry_run:
            changed = update_env(plan["data_home"], secrets)
            w.say(f"Saved {', '.join(changed) or 'nothing new'} to {plan['data_home']}/.env (values not shown).")
        user = os.environ.get("USER") or "$USER"
        run("Keep the bot running after you log out", ["loginctl", "enable-linger", user], key="run_linger")
        run("Start the bot now", ["systemctl", "--user", "enable", "--now",
                                  "print-handy-bot.service", "print-handy-listener.service"], key="run_start")
        w.say(f"\nCheck it: {python} -m print_handy.bot check")
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print Handy setup wizard")
    ap.add_argument("--answers", help="answers file (YAML or KEY=value); unanswered questions are asked")
    ap.add_argument("--dry-run", action="store_true", help="ask, show what would happen, change nothing")
    ap.add_argument("--yes", action="store_true", help="take defaults for unanswered questions and confirmations")
    ap.add_argument("--python", help="Python the services run with (default: this one)")
    args = ap.parse_args(argv)
    stream = sys.stdin
    if not _isatty(sys.stdin):
        try:
            stream = open("/dev/tty", encoding="utf-8")
        except OSError:
            stream = io.StringIO("")
    w = Wizard(parse_answers(args.answers), stream, sys.stdout, args.yes)
    w.say("Print Handy setup. Press Enter to take the default in [brackets].\n")
    found = bool(shutil.which("hermes")) or (Path.home() / ".hermes" / "hermes-agent").is_dir()
    plan = build_plan(w, hermes_found=found, existing_profiles=hermes_profiles() if found else ())
    w.say("\n" + summary(plan))
    if not args.dry_run and not w.confirm("apply", "\nApply these settings?"):
        w.say("Nothing was changed.")
        return 1
    apply(plan, w, python=args.python, dry_run=args.dry_run)
    w.say("\nDone." if not args.dry_run else "\nDry run: nothing was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
