"""Per-printer printer screen view for the Web UI (#1303).

``GET /api/printers/<id>/screen/snapshot`` answers one JPEG of the
printer's own display — what the printer screen is showing — for a
printer whose firmware publishes it, so an operator can read a prompt
on the printer without walking to it. The page at
``GET /printers/<id>/screen`` shows that still, labeled as the printer
screen and refreshed while it is open.

The source is ZMod's web screen service (audited against ZMod 1.7.6,
see docs/ZMOD.md): the printer serves its framebuffer over HTTP and
registers it with Moonraker as a webcam named ``screen``, next to the
bed camera's ``video`` entry. Discovery is that registration:

- Moonraker's ``GET /server/webcams/list`` is read for the printer's
  configured Moonraker address; the entry named ``screen`` — and only
  that entry — marks the screen as published;
- the still is fetched from the address ZMod 1.7.6 registers for it,
  ``/screen/snapshot`` on the printer's web port, the same derivation
  the bed camera's snapshot uses one path over. A registration naming
  any other snapshot address is refused, never followed.

Read-only by construction. The view only ever requests those two
addresses. It never requests the screen service's touch path, never
embeds the service's own page (that page wires clicks to touch), and
never treats the screen as a camera: the bed camera keeps its own
source, and the screen is not one. The printer-side service answers
without authentication, so the still is served only through this Web
UI route, behind the Web UI's own authorization; a refusal never
names the printer's address or any URL.
"""
from __future__ import annotations

import html
import json
import urllib.request
from urllib.parse import quote

from .. import settings
from . import camera as camera_module
from . import shell

SCREEN_WEBCAM_NAME = "screen"
SCREEN_SNAPSHOT_PATH = "/screen/snapshot"
_MAX_JSON_BYTES = 256 * 1024
_TIMEOUT_S = 8


class ScreenError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _fetch_json(url, timeout=_TIMEOUT_S):
    """One bounded JSON document from ``url`` (Moonraker's webcam list)."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    with camera_module._OPENER.open(urllib.request.Request(url),
                                    timeout=timeout) as response:
        raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("response is too large")
    return json.loads(raw.decode("utf-8"))


def discover_webcams(moonraker_url, *, fetch_json=None):
    """Moonraker's registered webcams for one printer, as a list of dicts.

    Raises ValueError when the answer is not a readable webcam list;
    transport errors propagate as themselves. A readable answer with
    no webcam list in it is an empty list, not an error.
    """
    fetch_json = _fetch_json if fetch_json is None else fetch_json
    payload = fetch_json(moonraker_url.rstrip("/") + "/server/webcams/list")
    if not isinstance(payload, dict) or "result" not in payload:
        raise ValueError("Moonraker answered with an unreadable webcam list")
    result = payload["result"]
    if isinstance(result, list):
        webcams = result
    elif isinstance(result, dict):
        webcams = result.get("webcams")
        if webcams is None:
            return []
    else:
        raise ValueError("Moonraker answered with an unreadable webcam list")
    if not isinstance(webcams, list):
        raise ValueError("Moonraker answered with an unreadable webcam list")
    return [entry for entry in webcams if isinstance(entry, dict)]


def _registered_screen(webcams):
    for entry in webcams:
        if str(entry.get("name") or "").strip() == SCREEN_WEBCAM_NAME:
            return entry
    return None


def _web_host(moonraker_url):
    """The printer's web host from its Moonraker address: the same
    derivation settings applies to the bed camera's snapshot address
    (scheme and API port dropped; the printer's web port serves both)."""
    base = moonraker_url.rstrip("/")
    return base.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]


def _screen_still_url(view, *, discover):
    """The one snapshot address the view will fetch, or a ScreenError.

    Discovery decides whether a screen is published and which shape it
    registered; the address itself is built from the printer's own
    configured host, never taken from the registration, so a hostile
    or drifted registration cannot steer the fetch elsewhere.
    """
    moonraker_url = (view.get("services", {}).get("moonraker_url") or "").strip()
    if not moonraker_url:
        raise ScreenError(404, "the printer screen view needs a Moonraker "
                               "connection; this printer has none configured")
    try:
        webcams = discover(moonraker_url)
    except Exception as exc:  # unreachable or unreadable Moonraker
        raise ScreenError(502, f"the printer did not answer the screen "
                               f"check ({type(exc).__name__})") from None
    entry = _registered_screen(webcams)
    if entry is None:
        raise ScreenError(404, "no printer screen is registered on this "
                               "printer (on a ZMod printer the screen is "
                               "published by ZMod 1.7.4 or newer with the "
                               "WEB_SCREEN parameter on; screenless installs "
                               "have no screen to show)")
    registered = str(entry.get("snapshot_url") or "").strip()
    if registered != SCREEN_SNAPSHOT_PATH:
        raise ScreenError(502, "the registered printer screen snapshot "
                               "address has an unexpected shape; nothing "
                               "was fetched")
    return f"http://{_web_host(moonraker_url)}{SCREEN_SNAPSHOT_PATH}"


def snapshot(data, printer_id, *, discover=None, fetch=None):
    """(code, body, content_type) for GET /api/printers/<id>/screen/snapshot."""
    discover = discover_webcams if discover is None else discover
    fetch = camera_module.fetch_jpeg if fetch is None else fetch
    try:
        try:
            settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise ScreenError(404, f"no printer with id {printer_id!r} "
                                   f"is configured") from None
        view = settings.view_for(printer_id, data)
        url = _screen_still_url(view, discover=discover)
        try:
            image = fetch(url)
        except Exception as exc:  # screen service not answering
            raise ScreenError(502, f"printer screen unreachable "
                                   f"({type(exc).__name__})") from None
        if not isinstance(image, (bytes, bytearray)) \
                or not bytes(image[:2]) == camera_module._JPEG_MAGIC:
            raise ScreenError(502, "the printer screen answered with "
                                   "something that is not a JPEG")
    except ScreenError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}, \
            "application/json"
    return 200, bytes(image), "image/jpeg"


def probe(data, printer_id, *, discover=None):
    """(available, reason) for the screen page: discovery only, no still
    is fetched, so opening the page never pulls a frame."""
    discover = discover_webcams if discover is None else discover
    try:
        settings.by_id(printer_id, data)
    except settings.SettingsError:
        return False, f"no printer with id {printer_id!r} is configured"
    view = settings.view_for(printer_id, data)
    try:
        _screen_still_url(view, discover=discover)
    except ScreenError as refusal:
        return False, refusal.reason
    return True, ""


def render_page(printer_name, printer_id, *, ok, reason,
                theme=shell.DEFAULT_THEME):
    """The dedicated screen page: a clearly labeled, read-only view.

    The still comes from this Web UI's own snapshot route, never from
    the printer's address, and the page carries no control that could
    send anything to the printer: the image is not a link and nothing
    on the page posts anywhere.
    """
    esc = html.escape
    name = esc(str(printer_name or printer_id))
    if ok:
        src = "/api/printers/" + quote(str(printer_id), safe="") \
            + "/screen/snapshot"
        body = (
            f"<h2>Printer screen — {name}</h2>"
            "<p>This is the printer's own display, shown read-only. It is "
            "not the print-bed camera, and nothing on this page can press "
            "the screen: to answer a prompt, use the printer itself.</p>"
            f'<p><img id="printer-screen" src="{esc(src)}" '
            'alt="The printer\'s screen" style="max-width:100%"></p>'
            "<p>The picture refreshes while this page is open.</p>"
            "<script>"
            "setInterval(function () {"
            "  var img = document.getElementById('printer-screen');"
            "  var base = img.src.split('?')[0];"
            "  img.src = base + '?t=' + Date.now();"
            "}, 2000);"
            "</script>")
    else:
        body = (
            f"<h2>Printer screen — {name}</h2>"
            f'<p class="error">{esc(reason)}</p>'
            "<p>The printer screen view shows the printer's own display "
            "where the firmware publishes one. The print-bed camera is "
            "unaffected either way.</p>")
    return shell.page("Printer screen", body, path="/printers", theme=theme)
