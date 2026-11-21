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
           screen_for=None, screen_touch=False):
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
        if published and screen_touch:
            screen_col = (
                '<div class="printer-screen-col"><h3>Printer screen</h3>'
                f'<input type="hidden" id="printer-preview-csrf-{pid}" value="{_esc(form_token)}">'
                f'<div style="position:relative"><img data-printer-screen '
                f'data-touch-url="/printers/{pid}/screen/touch" '
                f'data-csrf-id="printer-preview-csrf-{pid}" '
                'role="button" tabindex="0" aria-keyshortcuts="Enter Space" '
                f'src="/api/printers/{pid}/screen/snapshot" '
                f'alt="Interactive printer screen of {esc(entry.get("name") or entry["id"])}" '
                'style="width:100%;display:block;touch-action:manipulation;cursor:pointer">'
                '<span class="printer-tap-marker" aria-hidden="true" style="display:none;'
                'position:absolute;width:22px;height:22px;border:3px solid #d22;border-radius:50%;'
                'box-sizing:border-box;transform:translate(-50%,-50%);pointer-events:none;'
                'box-shadow:0 0 0 5px #fff8"></span></div>'
                f'<p class="muted" id="printer-preview-note-{pid}" role="status" '
                'aria-live="polite">Tap to send one screen press. Drags and long presses are not sent.</p>'
                '<p class="muted">This is the printer\'s own display, not '
                'the bed camera. '
                f'<a href="/printers/{pid}/screen">Open the full printer '
                'screen view</a>.</p></div>')
        elif published:
            screen_col = (
                '<div class="printer-screen-col"><h3>Printer screen</h3>'
                f'<img data-printer-screen src="/api/printers/{pid}/screen/snapshot" '
                f'alt="Printer screen of {esc(entry.get("name") or entry["id"])}" '
                f'aria-describedby="printer-preview-note-{pid}">'
                f'<p class="muted" id="printer-preview-note-{pid}" role="status" '
                f'aria-live="polite">Preview only. Tap or click to learn how to open the full screen; '
                'this preview does not send touches to the printer.</p>'
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
            "var touchUrl=img.getAttribute('data-touch-url');"
            "var note=img.closest('.printer-screen-col').querySelector('[role=status]');"
            "function sendTap(clientX,clientY){"
            "var rect=img.getBoundingClientRect();if(!rect.width||!rect.height){return;}"
            "var x=Math.max(0,Math.min(799,Math.round((clientX-rect.left)/rect.width*800)));"
            "var y=Math.max(0,Math.min(479,Math.round((clientY-rect.top)/rect.height*480)));"
            "var marker=img.parentElement.querySelector('.printer-tap-marker');"
            "if(marker){marker.style.left=(x/800*100)+'%';marker.style.top=(y/480*100)+'%';marker.style.display='block';}"
            "if(!touchUrl){if(note){note.textContent=\"Preview only. No printer action was sent. Use the Open the full printer screen view link to interact.\";}return;}"
            "var csrf=document.getElementById(img.getAttribute('data-csrf-id'));if(note){note.textContent='Sending tap...';}"
            "fetch(touchUrl,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'_csrf='+encodeURIComponent(csrf?csrf.value:'')+'&x='+x+'&y='+y})"
            ".then(function(r){return r.json();}).then(function(j){if(note){note.textContent=j.ok?(j.detail||'Tap sent.'):'Tap refused: '+(j.reason||'not sent');}if(j.ok){img.src=base+'?t='+Date.now();}})"
            ".catch(function(){if(note){note.textContent='Could not reach the Web UI; tap was not confirmed.';}});"
            "}"
            # Watch the whole document so a second finger outside the frame,
            # or a pointer that leaves and returns, cannot become a tap.
            "var gesture=null;"
            "document.addEventListener('pointerdown',function(e){if(gesture&&e.pointerId!==gesture.id){gesture=null;}},true);"
            "img.addEventListener('pointerdown',function(e){if(e.button!==0||!e.isPrimary){return;}"
            "gesture={id:e.pointerId,x:e.clientX,y:e.clientY,moved:false,started:Date.now()};});"
            "document.addEventListener('pointermove',function(e){if(gesture&&e.pointerId===gesture.id&&"
            "Math.hypot(e.clientX-gesture.x,e.clientY-gesture.y)>10){gesture.moved=true;}},true);"
            "img.addEventListener('pointercancel',function(e){if(gesture&&e.pointerId===gesture.id){gesture=null;}});"
            "img.addEventListener('pointerup',function(e){if(!gesture||e.pointerId!==gesture.id){return;}var g=gesture;gesture=null;"
            "if(g.moved||Date.now()-g.started>500||Math.hypot(e.clientX-g.x,e.clientY-g.y)>10){return;}sendTap(e.clientX,e.clientY);});"
            "img.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();var r=img.getBoundingClientRect();sendTap(r.left+r.width/2,r.top+r.height/2);}});"
            "function next(){setTimeout(tick,Math.min(2000*Math.pow(2,failures),60000));}"
            "function tick(){if(document.hidden){next();return;}img.src=base+'?t='+Date.now();}"
            "img.onload=function(){failures=0;next();};"
            "img.onerror=function(){failures+=1;next();};"
            "});"
            "</script>")
    return shell.page("printers", "".join(parts), path="/printers", theme=theme,
                      heading="Printers")
