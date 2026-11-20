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


def render(roster, *, form_token, notice=None, error=None, theme=shell.DEFAULT_THEME,
           screen_for=None):
    """The Printers page: one card per printer, settings on the left
    and the printer screen itself on the right (#1720).

    The screen moved back under Printers here: for a Moonraker
    printer that publishes a screen (``screen_for`` answers, through
    the screen module's cached discovery), the right column carries
    the live still from the screen snapshot route, labeled as the
    printer's own display and linked to the full screen view; a
    printer that publishes none gets the explanation in its place.
    Without ``screen_for`` (a direct render in a test) every
    Moonraker printer's column carries the still — discovery decides
    on the real page, through the handler."""
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
        # Moonraker, so only Moonraker printers carry the screen; the
        # full view itself explains when no screen is published.
        moonraker = model is not None and model.vendor.connection == "moonraker"
        # The screen is already embedded on this card; avoid a redundant
        # action competing with settings and spool links on narrow screens.
        screen_link = ""
        settings_col = (
            '<div class="printer-settings-col">'
            f'<h3>{esc(entry.get("name") or entry["id"])}{default}</h3>'
            f'<p class="mono">{esc(entry["id"])} · {esc(entry.get("model") or "")}</p>'
            f'<p><a class="button" href="/printer-settings?printer={pid}">Printer settings</a> '
            f'<a class="button" href="/spools/{pid}">Open spools</a>'
            f'{screen_link}</p></div>')
        if not moonraker:
            cards.append(f'<div class="card printer-card">{settings_col}</div>')
            continue
        published = True if screen_for is None else bool(screen_for(entry["id"]))
        if published:
            screen_col = (
                '<div class="printer-screen-col"><h3>Printer screen</h3>'
                f'<img data-printer-screen src="/api/printers/{pid}/screen/snapshot" '
                f'alt="Printer screen of {esc(entry.get("name") or entry["id"])}">'
                '<p class="muted">This is the printer\'s own display, not '
                'the bed camera. '
                f'<a href="/printers/{pid}/screen">Open the full printer '
                'screen view</a>.</p></div>')
        else:
            screen_col = (
                '<div class="printer-screen-col"><h3>Printer screen</h3>'
                '<p class="muted">No printer screen is published on this '
                'printer. '
                f'<a href="/printers/{pid}/screen">The screen view</a> '
                'says why.</p></div>')
        cards.append(
            '<div class="card printer-card"><div class="printer-layout">'
            f'{settings_col}{screen_col}</div></div>')
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
    if any('data-printer-screen' in card for card in cards):
        # Refresh each embedded screen still in place (the screen
        # page's backoff pattern), so the Printers page shows the
        # live display, not the frame from when it was opened.
        parts.append(
            "<script>"
            "document.querySelectorAll('img[data-printer-screen]').forEach(function(img){"
            "var base=img.src.split('?')[0];var failures=0;"
            "function next(){setTimeout(tick,Math.min(2000*Math.pow(2,failures),60000));}"
            "function tick(){if(document.hidden){next();return;}img.src=base+'?t='+Date.now();}"
            "img.onload=function(){failures=0;next();};"
            "img.onerror=function(){failures+=1;next();};"
            "});"
            "</script>")
    return shell.page("printers", "".join(parts), path="/printers", theme=theme,
                      heading="Printers")
