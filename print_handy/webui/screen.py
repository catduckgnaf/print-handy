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

Read-only while printer controls are off. With ``[webui]
controls_enabled`` on (#708), the dedicated page can also press the
screen (#1626): ``POST /printers/<id>/screen/touch`` sends exactly
one request to the service's click path (``/screen/click``), with
coordinates in the native 800 by 480 frame, through the same
discovery and address building as the still. That is the only other
address this module ever requests, it never embeds the service's
own page (that page wires clicks to touch, outside every Web UI
gate), and it never treats the screen as a camera: the bed camera
keeps its own source, and the screen is not one. The printer-side
service answers without authentication, so the still and the tap
are served only through these Web UI routes, behind the Web UI's
own authorization; a refusal never names the printer's address or
any URL.
"""
from __future__ import annotations

import html
import json
import threading
import time
import urllib.request
from urllib.parse import quote

from .. import settings
from . import camera as camera_module
from . import shell

SCREEN_WEBCAM_NAME = "screen"
SCREEN_SNAPSHOT_PATH = "/screen/snapshot"
SCREEN_CLICK_PATH = "/screen/click"
#: The frame every screen coordinate lives in: the snapshot JPEG's
#: own size, and the space the service's click path takes (#1624).
SCREEN_FRAME_WIDTH = 800
SCREEN_FRAME_HEIGHT = 480
_MAX_JSON_BYTES = 256 * 1024
_MAX_CLICK_BYTES = 4096
_TIMEOUT_S = 8
#: How long one successful webcam-list answer is reused for the same
#: Moonraker address, so a refreshing page does not re-run discovery
#: for every frame.
_DISCOVERY_TTL_S = 30.0
_discovery_cache = {}
_discovery_lock = threading.Lock()


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Screen fetches follow no redirect at all: the view only ever
    requests the two addresses it builds itself, so a printer answering
    with a redirect (to another host, an internal address, anything) is
    refused instead of followed (#1303 review)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("the printer answered with a redirect; not followed")


_OPENER = urllib.request.build_opener(_NoRedirects)


class ScreenError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _fetch_json(url, timeout=_TIMEOUT_S):
    """One bounded JSON document from ``url`` (Moonraker's webcam list)."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    with _OPENER.open(urllib.request.Request(url), timeout=timeout) as response:
        raw = response.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("response is too large")
    return json.loads(raw.decode("utf-8"))


def fetch_screen_jpeg(url, timeout=_TIMEOUT_S):
    """One bounded JPEG from the screen service, redirects refused."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    with _OPENER.open(urllib.request.Request(url), timeout=timeout) as response:
        image = response.read(camera_module.MAX_BYTES + 1)
    if len(image) > camera_module.MAX_BYTES             or not image.startswith(camera_module._JPEG_MAGIC):
        raise ValueError("screen response is not a bounded JPEG")
    return image


def send_click(url, timeout=_TIMEOUT_S):
    """One tap request to the screen service, redirects refused.

    Returns the service's answer body. The service answers 200 with
    ``OK`` even when the tap itself failed (#1624), so the body is
    evidence the request was answered, never that a tap landed;
    transport errors propagate as themselves."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    with _OPENER.open(urllib.request.Request(url), timeout=timeout) as response:
        status = response.status
        body = response.read(_MAX_CLICK_BYTES + 1)
    if status != 200 or len(body) > _MAX_CLICK_BYTES:
        raise ValueError("screen service answered unexpectedly")
    return body


def _cached_discover(moonraker_url, *, now=time.monotonic):
    """discover_webcams with a short per-address cache of good answers.

    Only a readable answer is cached; a failure is never cached, so a
    printer that comes back is seen on the next request."""
    key = moonraker_url.rstrip("/")
    with _discovery_lock:
        hit = _discovery_cache.get(key)
        if hit is not None and now() - hit[0] < _DISCOVERY_TTL_S:
            return hit[1]
    webcams = discover_webcams(moonraker_url)
    with _discovery_lock:
        _discovery_cache[key] = (now(), webcams)
    return webcams


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
    """The one snapshot address the view will fetch, or a ScreenError."""
    return _screen_service_url(view, discover=discover,
                                path=SCREEN_SNAPSHOT_PATH)


def _screen_service_url(view, *, discover, path):
    """One screen service address (``path``) for this printer, or a
    ScreenError.

    Discovery decides whether a screen is published and which shape it
    registered; the address itself is built from the printer's own
    configured host, never taken from the registration, so a hostile
    or drifted registration cannot steer the request elsewhere.
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
    return f"http://{_web_host(moonraker_url)}{path}"


def snapshot(data, printer_id, *, discover=None, fetch=None):
    """(code, body, content_type) for GET /api/printers/<id>/screen/snapshot."""
    discover = _cached_discover if discover is None else discover
    fetch = fetch_screen_jpeg if fetch is None else fetch
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


def _coordinate(value, name, limit):
    """One tap coordinate as an int inside the frame, or a ScreenError.

    Accepts a real int or the form's decimal text for one; bools,
    floats and anything else are malformed (400), and a whole number
    outside ``0..limit`` is refused by name (400). Either way nothing
    is sent."""
    number = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        if digits.isdigit():
            number = int(text)
    if number is None:
        raise ScreenError(400, f"the tap needs a whole-number {name} "
                               f"coordinate; nothing was sent")
    if not 0 <= number <= limit:
        raise ScreenError(400, f"the tap {name} coordinate {number} is "
                               f"outside the printer screen frame "
                               f"(0 to {limit}); nothing was sent")
    return number


def touch(data, printer_id, x, y, *, discover=None, send=None):
    """(code, body) for POST /printers/<id>/screen/touch (#1626).

    One tap on the printer's own screen, proxied to the screen
    service's click path. The ``[webui] controls_enabled`` gate is
    the consent: while it is off this answers 403 with the controls
    reason and sends nothing. Discovery and address building are the
    snapshot's own, so an unknown printer and a printer with no
    published screen refuse exactly as the snapshot refuses, and the
    click address is built from the printer's configured host only.
    A service 200 never proves the tap landed (#1624), so the success
    body says the tap was sent and names the refreshed picture as the
    confirmation; it never claims delivery.
    """
    discover = _cached_discover if discover is None else discover
    send = send_click if send is None else send
    try:
        from . import controls as controls_module
        if not controls_module.controls_enabled(data):
            raise ScreenError(403, controls_module.OFF_REASON)
        try:
            settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise ScreenError(404, f"no printer with id {printer_id!r} "
                                   f"is configured") from None
        tap_x = _coordinate(x, "x", SCREEN_FRAME_WIDTH)
        tap_y = _coordinate(y, "y", SCREEN_FRAME_HEIGHT)
        view = settings.view_for(printer_id, data)
        url = _screen_service_url(view, discover=discover,
                                   path=SCREEN_CLICK_PATH) \
            + f"?x={tap_x}&y={tap_y}"
        try:
            answer = send(url)
        except Exception as exc:  # screen service not answering
            raise ScreenError(502, f"printer screen unreachable "
                                   f"({type(exc).__name__})") from None
        if answer is not None and bytes(answer).strip() != b"OK":
            raise ScreenError(502, "the printer screen answered with "
                                   "something unexpected")
    except ScreenError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True,
                 "detail": "Tap sent. The refreshed picture shows "
                           "what it did."}


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
                theme=shell.DEFAULT_THEME, touch=False, form_token=""):
    """The dedicated screen page: a clearly labeled view of the screen.

    The still comes from this Web UI's own snapshot route, never from
    the printer's address. While ``touch`` is false (printer controls
    off) the page is read-only exactly as #1303 shipped it: the image
    is not a link and nothing on the page posts anywhere. While
    ``touch`` is true (the handler passes it only when
    ``[webui] controls_enabled`` is on), clicking the still posts one
    tap to this Web UI's own touch route with the page's form token;
    the image is still never a link to the printer and the service's
    own page is never embedded.
    """
    esc = html.escape
    name = esc(str(printer_name or printer_id))
    if ok:
        src = "/api/printers/" + quote(str(printer_id), safe="") \
            + "/screen/snapshot"
        if touch:
            intro = (
                "<p>This is the printer's own display. It is not the "
                "print-bed camera. Printer controls are on, so you can "
                "press the screen from here: click or tap the picture "
                "and one tap is sent to the printer at that spot. Taps "
                "are all this page sends: no drags and no long presses. "
                "The printer cannot confirm a tap, so the refreshed "
                "picture is how you see what it did.</p>")
            img_style = "max-width:100%;cursor:pointer"
            touch_url = "/printers/" + quote(str(printer_id), safe="") \
                + "/screen/touch"
            # The frame the tap coordinates live in is
            # SCREEN_FRAME_WIDTH by SCREEN_FRAME_HEIGHT (800 by 480).
            touch_script = (
                f'  var touchUrl = {json.dumps(touch_url)};'
                "  var csrf = document.getElementById('printer-screen-csrf');"
                "  var keepNote = false;"
                "  img.addEventListener('click', function (e) {"
                "    var rect = img.getBoundingClientRect();"
                "    if (!rect.width || !rect.height) { return; }"
                "    var x = Math.round((e.clientX - rect.left) / rect.width * 800);"
                "    var y = Math.round((e.clientY - rect.top) / rect.height * 480);"
                "    x = Math.max(0, Math.min(800, x));"
                "    y = Math.max(0, Math.min(480, y));"
                "    note.textContent = 'Sending the tap...';"
                "    fetch(touchUrl, {method: 'POST', credentials: 'same-origin',"
                "      headers: {'Content-Type': 'application/x-www-form-urlencoded'},"
                "      body: '_csrf=' + encodeURIComponent(csrf ? csrf.value : '')"
                "        + '&x=' + x + '&y=' + y})"
                "    .then(function (r) { return r.json().catch(function () {"
                "      return {ok: false, reason: 'HTTP ' + r.status}; }); })"
                "    .then(function (j) {"
                "      keepNote = true;"
                "      note.textContent = j.ok ? (j.detail || 'Tap sent.')"
                "        : ('Not sent: ' + (j.reason || 'refused'));"
                "      if (j.ok) { img.src = base + '?t=' + Date.now(); }"
                "    })"
                "    .catch(function () { note.textContent ="
                "      'Could not reach the Web UI server; this page sent nothing.'; });"
                "  });")
            csrf_input = (
                f'<input type="hidden" id="printer-screen-csrf" '
                f'value="{esc(form_token)}">')
        else:
            intro = (
                "<p>This is the printer's own display, shown read-only. It is "
                "not the print-bed camera, and nothing on this page can press "
                "the screen: to answer a prompt, use the printer itself.</p>")
            img_style = "max-width:100%"
            touch_script = "  var keepNote = false;"
            csrf_input = ""
        body = (
            f"<h2>Printer screen — {name}</h2>"
            + intro
            + f'<p><img id="printer-screen" src="{esc(src)}" '
            f'alt="The printer\'s screen" style="{img_style}"></p>'
            "<p>The picture refreshes while this page is open.</p>"
            '<p id="printer-screen-note"></p>'
            + csrf_input
            + "<script>"
            "(function () {"
            "  var img = document.getElementById('printer-screen');"
            "  var note = document.getElementById('printer-screen-note');"
            "  var base = img.src.split('?')[0];"
            "  var failures = 0;"
            "  function next() {"
            "    var wait = Math.min(2000 * Math.pow(2, failures), 60000);"
            "    setTimeout(tick, wait);"
            "  }"
            "  function tick() {"
            "    if (document.hidden) { next(); return; }"
            "    img.src = base + '?t=' + Date.now();"
            "  }"
            "  img.onload = function () { failures = 0;"
            "    if (keepNote) { keepNote = false; } else { note.textContent = ''; }"
            "    next(); };"
            "  img.onerror = function () {"
            "    failures += 1;"
            "    note.textContent = 'The printer screen did not answer; retrying less often.';"
            "    next();"
            "  };"
            + touch_script
            + "})();"
            "</script>")
    else:
        body = (
            f"<h2>Printer screen — {name}</h2>"
            f'<p class="error">{esc(reason)}</p>'
            "<p>The printer screen view shows the printer's own display "
            "where the firmware publishes one. The print-bed camera is "
            "unaffected either way.</p>")
    return shell.page("Printer screen", body, path="/printers", theme=theme)
