"""The Web UI Printers page (#875): the roster with per-printer actions,
and Add a printer.

Adding goes through :func:`print_handy.roster_write.add_printer`, the same
writer guided onboarding uses: the candidate file is validated by the real
settings loader, the previous file is backed up, the write is atomic, and
an existing printer is never dropped. Secrets (a Bambu access code, a
PrusaLink API key) are never typed here; the entry names the environment
variable that carries them, as onboarding does.
"""

from __future__ import annotations

from urllib.parse import quote

from .. import printers
from ..bot.guided import _bambu_env_name, _prusa_env_name
from . import shell
from .pages import CSRF_FIELD

_ADDRESS = {
    # connection kind -> (entry key, label, placeholder)
    "moonraker": ("moonraker_url", "Moonraker URL", "http://192.168.1.50:7125"),
    "octoprint": ("octoprint_url", "OctoPrint URL", "http://192.168.1.50"),
    "bambulab_lan": ("lan_host", "Printer IP address", "192.168.1.50"),
    "prusalink": ("link_host", "Printer IP address", "192.168.1.50"),
    "elegoo_sdcp": ("host", "Printer IP address", "192.168.1.50"),
    "flashforge_stock": ("host", "Printer IP address", "192.168.1.50"),
}


_esc = shell._esc


def build_entry(form, roster):
    """The roster entry a submitted Add form describes, or ``(None, error)``."""
    def one(name):
        return ((form.get(name) or [""])[0]).strip()

    name, model_key, address = one("name"), one("model"), one("address")
    if not name:
        return None, "Give the printer a name."
    if model_key not in printers.MODELS:
        return None, "Pick the printer model from the list."
    from ..bot.guided import _derive_printer_id
    pid = _derive_printer_id(name, {entry["id"] for entry in roster})
    entry = {"id": pid, "model": model_key, "name": name}
    conn = printers.MODELS[model_key].vendor.connection
    if address and conn in _ADDRESS:
        entry[_ADDRESS[conn][0]] = address
        if conn == "bambulab_lan":
            serial = one("serial")
            if not serial:
                return None, "A Bambu printer on the LAN needs its serial number too."
            entry["lan_serial"] = serial
            entry["lan_access_code_env"] = _bambu_env_name(pid)
        elif conn == "prusalink":
            entry["api_key_env"] = _prusa_env_name(pid)
    return entry, None


def add(form, roster, *, environ=None):
    """Append the submitted printer. Returns ``(ok, message)``."""
    from .. import roster_write

    entry, error = build_entry(form, roster)
    if entry is None:
        return False, error or "Nothing to add."
    try:
        result = roster_write.add_printer(entry, environ=environ)
    except roster_write.RosterWriteError as exc:
        return False, str(exc)
    secret = ""
    for key in ("lan_access_code_env", "api_key_env"):
        if entry.get(key):
            secret = (f" Put its secret in the environment variable {entry[key]} "
                      "before restarting.")
    return True, (f"{entry['name']} ({entry['id']}) was added to {result['path'].name}; "
                  f"the previous file is kept as {result['backup'].name}.{secret} "
                  "Restart Print Handy to bring it live.")


def render(roster, *, form_token, notice=None, error=None, theme=shell.DEFAULT_THEME):
    esc = _esc
    parts = []
    if notice:
        parts.append(f'<p class="notice">{esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{esc(error)}</p>')
    cards = []
    for entry in roster:
        pid = quote(str(entry["id"]), safe="")
        default = ' <span class="badge ok">Default printer</span>' if entry.get("default") else ""
        model = printers.MODELS.get(entry.get("model") or "")
        # The printer screen view (#1303) discovers its screen through
        # Moonraker, so only Moonraker printers carry the link; the page
        # itself explains when no screen is published.
        screen_link = (f' <a class="button" href="/printers/{pid}/screen">'
                       'Printer screen</a>'
                       if model is not None
                       and model.vendor.connection == "moonraker" else "")
        cards.append(
            '<div class="card printer-card">'
            f'<h3>{esc(entry.get("name") or entry["id"])}{default}</h3>'
            f'<p class="mono">{esc(entry["id"])} · {esc(entry.get("model") or "")}</p>'
            f'<p><a class="button" href="/spools/{pid}">Open spools</a> '
            f'<a class="button" href="/layers">Layers</a>{screen_link}</p></div>')
    parts.append("<h2>Printers</h2>" + ("".join(cards) or "<p>No printers configured.</p>"))
    options = "".join(
        f'<option value="{esc(key)}">{esc(model.label)}</option>'
        for key, model in sorted(printers.MODELS.items(), key=lambda kv: kv[1].label))
    parts.append(
        '<h2 id="add">Add a printer</h2>'
        '<p>Adds a printer to the settings file (a backup of the current file is kept). '
        'Existing printers are not changed. Secrets are never typed here.</p>'
        '<form method="post" action="/printers">'
        f'<input type="hidden" name="{CSRF_FIELD}" value="{esc(form_token)}">'
        '<div class="field"><label for="p-name">Name</label>'
        '<input id="p-name" name="name" required placeholder="e.g. Garage printer"></div>'
        '<div class="field"><label for="p-model">Model</label>'
        f'<select id="p-model" name="model" required><option value="">Pick a model</option>{options}'
        '</select></div>'
        '<div class="field"><label for="p-address">Address (optional)</label>'
        '<input id="p-address" name="address" placeholder="IP address or URL">'
        '<p class="help">Moonraker or OctoPrint printers take a URL; the others take an IP address.'
        ' Leave it empty to add the printer without a connection.</p></div>'
        '<div class="field"><label for="p-serial">Serial number (Bambu only)</label>'
        '<input id="p-serial" name="serial"></div>'
        '<button type="submit" class="accent">Add printer</button></form>')
    return shell.page("printers", "".join(parts), path="/printers", theme=theme,
                      heading="Printers")
