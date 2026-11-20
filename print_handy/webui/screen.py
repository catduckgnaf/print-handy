"""Per-printer printer screen view for the Web UI (#1303).

``GET /api/printers/<id>/screen/snapshot`` answers one JPEG of the
printer's own display — what the printer screen is showing — for a
printer whose firmware publishes it, so an operator can read a prompt
on the printer without walking to it. The page at
``GET /printers/<id>/screen`` shows that still, labeled as the printer
screen and refreshed while it is open.

The first source is ZMod's web screen service (audited against
ZMod 1.7.6, see docs/ZMOD.md): the printer serves its framebuffer
over HTTP and registers it with Moonraker as a webcam named
``screen``, next to the bed camera's ``video`` entry. The second is
HelixScreen 1.1's screen sharing: it registers a webcam named
``HelixScreen Screen`` (with the host name appended in parentheses
when it registers on another host's Moonraker), stamped with a
``helixscreen_screen`` marker in its ``extra_data`` — the marker is
HelixScreen's own recognizer, and it survives an operator renaming
the entry, so discovery accepts the marker or the name. Discovery
is those registrations:

- Moonraker's ``GET /server/webcams/list`` is read for the printer's
  configured Moonraker address; a ``screen`` entry marks the ZMod
  screen as published, a ``HelixScreen Screen`` entry or marker
  marks the HelixScreen one. When both are present the ZMod entry
  wins: it is the source this view was built on, and HelixScreen is
  the fallback when it is absent;
- the still is fetched from the one address the winning source
  registers for it, built on the printer's web port from the
  printer's configured host: ``/screen/snapshot`` for ZMod,
  ``/helixscreen/screen/snapshot.jpg`` for HelixScreen's
  Moonraker-proxied setup. A registration naming any other snapshot
  address is refused, never followed. HelixScreen's pairing-code
  setup registers an absolute address on the screen device's own
  port instead; that address is refused by name, because an address
  taken from a registration could steer the fetch anywhere.

Read-only while printer controls are off. With ``[webui]
controls_enabled`` on (#708), the dedicated page can also press the
screen (#1626): ``POST /printers/<id>/screen/touch`` sends exactly
one request to the service's click path (``/screen/click``), with
coordinates in the native 800 by 480 frame, through the same
discovery and address building as the still.

Since #1720 the tap also applies to a HelixScreen screen, through
HelixScreen's own screen-sharing protocol (the one its served page
speaks, prestonbrown/helixscreen#1825): the sharing server answers
``GET /helixscreen/screen/info`` with what this client may do
(``control``, ``paired``, the native frame ``w`` / ``h``), takes a
pairing exchange at ``POST /helixscreen/screen/pair`` (the six-digit
code the printer shows under Settings, Connection, Screen Sharing,
turned in once for a pairing token), and takes touch events at
``POST /helixscreen/screen/touch`` (JSON ``{"type": "down"|"up",
"x": ..., "y": ...}``, with the pairing token in the
``X-Helix-Pair`` header and ``X-Helix-Touch: 1`` on every post).
A tap is one down event and one up event at the same spot, in that
order. Print Handy pairs from its own screen page
(``POST /printers/<id>/screen/pair``, behind the same controls gate
and session as the tap): the code is exchanged and discarded, and
only the token is kept — in ``helixscreen_pairing.json`` in the
state folder, written atomically with mode 0600, the same
protection class as the remembered-session store. There is no
encrypted credential store in this codebase and the token is never
written to the settings file, in plain text or otherwise; it is a
revocable pairing credential (unpairing on the printer kills it),
and a stored token that the printer no longer honors is refused by
name so the operator can pair again. Before #1720 nothing here
paired at all, so an operator whose sharing required pairing had
to enter the code in HelixScreen's own page on every visit — its
page keeps the token in that browser's local storage, per origin,
so a different host name or address asked again.

These are the only other addresses this module ever requests, all
built from the printer's configured host exactly like the still,
and it still never embeds the service's own page and never treats
the screen as a camera: the bed camera keeps its own source, and
the screen is not one. The printer-side services answer the still
without authentication, so the still and the tap are served only
through these Web UI routes, behind the Web UI's own
authorization; a refusal never names the printer's address or
any URL.
"""
from __future__ import annotations

import html
import json
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from .. import settings
from . import camera as camera_module
from . import shell

log = logging.getLogger(__name__)

SCREEN_WEBCAM_NAME = "screen"
SCREEN_SNAPSHOT_PATH = "/screen/snapshot"
SCREEN_CLICK_PATH = "/screen/click"
#: The two screen sources discovery can resolve, in precedence order.
SOURCE_ZMOD = "zmod"
SOURCE_HELIXSCREEN = "helixscreen"
#: HelixScreen 1.1 screen sharing (prestonbrown/helixscreen,
#: release/1.1): the registered webcam name, the marker it stamps in
#: the entry's extra_data, and the snapshot address of its
#: Moonraker-proxied setup (the installer's nginx route).
HELIXSCREEN_SCREEN_NAME = "HelixScreen Screen"
HELIXSCREEN_MARKER = "helixscreen_screen"
HELIXSCREEN_SNAPSHOT_PATH = "/helixscreen/screen/snapshot.jpg"
#: HelixScreen screen sharing's own endpoints, under the same
#: Moonraker-proxied prefix as its snapshot (the protocol its served
#: page speaks): info names what this client may do, pair exchanges
#: the printer's six-digit code for a pairing token, and touch takes
#: one touch event per post.
HELIXSCREEN_INFO_PATH = "/helixscreen/screen/info"
HELIXSCREEN_PAIR_PATH = "/helixscreen/screen/pair"
HELIXSCREEN_TOUCH_PATH = "/helixscreen/screen/touch"
#: The pairing token store: one small file in the state folder,
#: {printer_id: token}, written atomically with mode 0600 (the
#: remembered-session store's protection class). The pairing code
#: itself is never stored — it is exchanged and discarded — and no
#: token is ever written to the settings file.
PAIRING_FILE = "helixscreen_pairing.json"
_MAX_PAIR_BYTES = 4096
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


def fetch_screen_jpeg(url, timeout=_TIMEOUT_S, *, headers=None):
    """One bounded JPEG from the screen service, redirects refused."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    request = urllib.request.Request(url, headers=headers or {})
    with _OPENER.open(request, timeout=timeout) as response:
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


def fetch_screen_info(url, token="", timeout=_TIMEOUT_S):
    """(status, dict) for one HelixScreen ``info`` read.

    The pairing token rides in its ``X-Helix-Pair`` header when one
    is stored, exactly as HelixScreen's own page sends it. A non-200
    answer is reported, not raised, so the caller can name it; a
    body that is not a bounded JSON object raises ValueError."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    headers = {"X-Helix-Pair": token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(_MAX_PAIR_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(_MAX_PAIR_BYTES + 1)
    if len(raw) > _MAX_PAIR_BYTES:
        raise ValueError("screen service answered with too much")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def post_screen_json(url, payload, token="", timeout=_TIMEOUT_S):
    """(status, dict) for one HelixScreen JSON post (pair or touch).

    Every post carries ``X-Helix-Touch: 1``, as HelixScreen's own
    page sends, and the pairing token header when one is stored.
    A non-200 answer is reported, not raised; transport errors
    propagate as themselves."""
    if not camera_module._web_url(url):
        raise ValueError("address is not http or https")
    headers = {"Content-Type": "application/json", "X-Helix-Touch": "1"}
    if token:
        headers["X-Helix-Pair"] = token
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read(_MAX_PAIR_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(_MAX_PAIR_BYTES + 1)
    if len(raw) > _MAX_PAIR_BYTES:
        raise ValueError("screen service answered with too much")
    try:
        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (ValueError, UnicodeDecodeError):
        body = {}
    return status, body if isinstance(body, dict) else {}


# ---------------------------------------------------------- pairing store

def pairing_path(data):
    return Path(settings.state_dir(data)) / PAIRING_FILE


def load_pairing_token(data, printer_id):
    """The stored HelixScreen pairing token for one printer, or "".

    A missing or unreadable store reads as no pairing, exactly like
    the remembered-session store; the token is never logged."""
    try:
        saved = json.loads(pairing_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(saved, dict):
        return ""
    token = saved.get(str(printer_id))
    return token if isinstance(token, str) else ""


def save_pairing_token(data, printer_id, token):
    """Persist one pairing token (0600, atomic). True when written.

    Only the token is stored, keyed by printer id; the pairing code
    that earned it is never written anywhere. A failure is logged
    and reported, never raised, and never names the token."""
    path = pairing_path(data)
    try:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        saved[str(printer_id)] = str(token)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".helixscreen_pairing.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(saved, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        log.warning("webui: could not save the HelixScreen pairing: %s", exc)
        return False
    return True


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


def _registered_helixscreen(webcams):
    """The HelixScreen screen entry, or None.

    Recognized the way HelixScreen itself recognizes its entry
    (``is_own_screen_webcam`` in its webcam_selection.cpp): the
    ``helixscreen_screen`` marker in ``extra_data`` (which survives
    an operator renaming the entry in Moonraker), or the registered
    name, plain or with the host suffix it adds when the entry lives
    on another host's Moonraker.
    """
    for entry in webcams:
        extra = entry.get("extra_data")
        if isinstance(extra, dict) and extra.get(HELIXSCREEN_MARKER):
            return entry
        name = str(entry.get("name") or "").strip()
        if name == HELIXSCREEN_SCREEN_NAME \
                or name.startswith(HELIXSCREEN_SCREEN_NAME + " ("):
            return entry
    return None


def _web_host(moonraker_url):
    """The printer's web host from its Moonraker address: the same
    derivation settings applies to the bed camera's snapshot address
    (scheme and API port dropped; the printer's web port serves both)."""
    base = moonraker_url.rstrip("/")
    return base.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[0]


def _discover_screen(view, *, discover):
    """(source, entry, moonraker_url) for the published screen, or a
    ScreenError.

    Discovery decides whether a screen is published and which source
    registered it; the ZMod ``screen`` entry wins when both sources
    are present (the source this view was built on), and the
    HelixScreen entry is the fallback.
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
    if entry is not None:
        return SOURCE_ZMOD, entry, moonraker_url
    entry = _registered_helixscreen(webcams)
    if entry is not None:
        return SOURCE_HELIXSCREEN, entry, moonraker_url
    raise ScreenError(404, "no printer screen is registered on this "
                           "printer (on a ZMod printer the screen is "
                           "published by ZMod 1.7.4 or newer with the "
                           "WEB_SCREEN parameter on; a HelixScreen screen "
                           "is published by its Screen Sharing setting; "
                           "screenless installs have no screen to show)")


def _screen_still_url(view, *, discover):
    """The one snapshot address the view will fetch, or a ScreenError."""
    url, _source = _still_url_and_source(view, discover=discover)
    return url


def _still_url_and_source(view, *, discover):
    """(snapshot address, source) for this printer, or a ScreenError.

    The address is built from the printer's own configured host,
    never taken from the registration, so a hostile or drifted
    registration cannot steer the fetch elsewhere; a registration
    naming any other snapshot address is refused, never followed.
    """
    source, entry, moonraker_url = _discover_screen(view, discover=discover)
    registered = str(entry.get("snapshot_url") or "").strip()
    if source == SOURCE_ZMOD:
        if registered != SCREEN_SNAPSHOT_PATH:
            raise ScreenError(502, "the registered printer screen snapshot "
                                   "address has an unexpected shape; nothing "
                                   "was fetched")
        return f"http://{_web_host(moonraker_url)}{SCREEN_SNAPSHOT_PATH}", source
    if registered != HELIXSCREEN_SNAPSHOT_PATH:
        if registered.startswith(("http://", "https://")):
            raise ScreenError(404, "the HelixScreen screen on this printer "
                                   "is registered at its own network "
                                   "address (its pairing-code setup); Print "
                                   "Handy does not follow a registered "
                                   "address, so it cannot show that screen. "
                                   "The Moonraker-proxied HelixScreen setup "
                                   "is the one it shows")
        raise ScreenError(502, "the registered HelixScreen screen snapshot "
                               "address has an unexpected shape; nothing "
                               "was fetched")
    return f"http://{_web_host(moonraker_url)}{HELIXSCREEN_SNAPSHOT_PATH}", source


def _screen_click_url(view, *, discover):
    """The one click address a ZMod tap will request, or a ScreenError.

    The tap is proxied to the ZMod service's click path, built and
    validated exactly like the snapshot address. (A HelixScreen tap
    goes to its touch path instead — see :func:`_helix_url`.)"""
    source, entry, moonraker_url = _discover_screen(view, discover=discover)
    if source == SOURCE_HELIXSCREEN:
        raise ScreenError(501, "this printer's screen is a HelixScreen "
                               "screen; its tap takes the HelixScreen "
                               "touch path, not the ZMod click path. "
                               "Nothing was sent")
    registered = str(entry.get("snapshot_url") or "").strip()
    if registered != SCREEN_SNAPSHOT_PATH:
        raise ScreenError(502, "the registered printer screen snapshot "
                               "address has an unexpected shape; nothing "
                               "was fetched")
    return f"http://{_web_host(moonraker_url)}{SCREEN_CLICK_PATH}"


def _helix_url(view, path, *, discover):
    """One HelixScreen sharing address (info / pair / touch), built
    from the printer's configured host exactly like its snapshot
    address, after the same registration-shape validation."""
    source, entry, moonraker_url = _discover_screen(view, discover=discover)
    if source != SOURCE_HELIXSCREEN:
        raise ScreenError(400, "this printer's screen is not a "
                               "HelixScreen screen; nothing was sent")
    registered = str(entry.get("snapshot_url") or "").strip()
    if registered != HELIXSCREEN_SNAPSHOT_PATH:
        if registered.startswith(("http://", "https://")):
            raise ScreenError(404, "the HelixScreen screen on this printer "
                                   "is registered at its own network "
                                   "address (its pairing-code setup); Print "
                                   "Handy does not follow a registered "
                                   "address, so it cannot press that screen. "
                                   "The Moonraker-proxied HelixScreen setup "
                                   "is the one it presses")
        raise ScreenError(502, "the registered HelixScreen screen snapshot "
                               "address has an unexpected shape; nothing "
                               "was sent")
    return f"http://{_web_host(moonraker_url)}{path}"


def _helix_info(view, token, *, discover, fetch_info):
    """The sharing server's info dict for this printer, or a ScreenError.

    Info is the server's own statement of what this client may do:
    ``control`` (Allow Remote Control on the printer), ``paired``
    (the stored token is honored) and the native frame size."""
    url = _helix_url(view, HELIXSCREEN_INFO_PATH, discover=discover)
    try:
        status, info = fetch_info(url, token)
    except Exception as exc:  # sharing server not answering
        raise ScreenError(502, f"printer screen unreachable "
                               f"({type(exc).__name__})") from None
    if status != 200:
        raise ScreenError(502, "the HelixScreen screen sharing did not "
                               "answer its info check; nothing was sent")
    return info if isinstance(info, dict) else {}


def snapshot(data, printer_id, *, discover=None, fetch=None, token_for=None):
    """(code, body, content_type) for GET /api/printers/<id>/screen/snapshot.

    A HelixScreen still is fetched with the stored pairing token
    header when one exists (a sharing setup that requires pairing
    for control may require it for the picture too); a ZMod still
    never carries one."""
    discover = _cached_discover if discover is None else discover
    default_fetch = fetch is None
    fetch = fetch_screen_jpeg if fetch is None else fetch
    token_for = load_pairing_token if token_for is None else token_for
    try:
        try:
            settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise ScreenError(404, f"no printer with id {printer_id!r} "
                                   f"is configured") from None
        view = settings.view_for(printer_id, data)
        url, source = _still_url_and_source(view, discover=discover)
        headers = None
        if source == SOURCE_HELIXSCREEN:
            token = token_for(data, printer_id)
            if token:
                headers = {"X-Helix-Pair": token}
        try:
            if default_fetch and headers:
                image = fetch(url, headers=headers)
            else:
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


def touch(data, printer_id, x, y, *, discover=None, send=None,
          helix_info=None, helix_post=None, token_for=None):
    """(code, body) for POST /printers/<id>/screen/touch (#1626, #1720).

    One tap on the printer's own screen. The ``[webui]
    controls_enabled`` gate is the consent: while it is off this
    answers 403 with the controls reason and sends nothing.
    Discovery and address building are the snapshot's own, so an
    unknown printer and a printer with no published screen refuse
    exactly as the snapshot refuses, and every address is built from
    the printer's configured host only.

    A ZMod tap is proxied to the service's click path. A HelixScreen
    tap is two touch events (down, then up, same spot) to the
    sharing server's touch path, and only when its info says remote
    control is allowed on the printer and the stored pairing token
    is honored: an unpaired tap answers 403 naming the pairing form
    on this page, and a view-only sharing setup answers 501 naming
    the Allow Remote Control setting — either way nothing is sent.
    Coordinates are validated against the frame the info answer
    names (the 800 by 480 frame when it names none).

    A service 200 never proves the tap landed (#1624), so the success
    body says the tap was sent and names the refreshed picture as the
    confirmation; it never claims delivery.
    """
    discover = _cached_discover if discover is None else discover
    send = send_click if send is None else send
    helix_info = fetch_screen_info if helix_info is None else helix_info
    helix_post = post_screen_json if helix_post is None else helix_post
    token_for = load_pairing_token if token_for is None else token_for
    try:
        from . import controls as controls_module
        if not controls_module.controls_enabled(data):
            raise ScreenError(403, controls_module.OFF_REASON)
        try:
            settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise ScreenError(404, f"no printer with id {printer_id!r} "
                                   f"is configured") from None
        view = settings.view_for(printer_id, data)
        # Coordinates are validated before discovery, as #1626 shipped:
        # a malformed or out-of-frame tap is a 400 that discovers and
        # sends nothing. A HelixScreen tap is then re-validated
        # against the (possibly smaller) frame its info answer names.
        tap_x = _coordinate(x, "x", SCREEN_FRAME_WIDTH)
        tap_y = _coordinate(y, "y", SCREEN_FRAME_HEIGHT)
        source, _entry, _moonraker_url = _discover_screen(
            view, discover=discover)
        if source == SOURCE_HELIXSCREEN:
            token = token_for(data, printer_id)
            info = _helix_info(view, token, discover=discover,
                               fetch_info=helix_info)
            if not info.get("control"):
                raise ScreenError(501, "this HelixScreen screen is shared "
                                       "view-only: turn on Allow Remote "
                                       "Control under Settings, Connection, "
                                       "Screen Sharing on the printer to press "
                                       "it from here. Nothing was sent")
            if not info.get("paired"):
                raise ScreenError(403, "this HelixScreen screen is not "
                                       "paired with Print Handy yet: enter "
                                       "the pairing code the printer shows "
                                       "under Settings, Connection, Screen "
                                       "Sharing in the form on the printer "
                                       "screen page. Nothing was sent")
            width = info.get("w") if isinstance(info.get("w"), int) \
                and not isinstance(info.get("w"), bool) else SCREEN_FRAME_WIDTH
            height = info.get("h") if isinstance(info.get("h"), int) \
                and not isinstance(info.get("h"), bool) else SCREEN_FRAME_HEIGHT
            tap_x = _coordinate(x, "x", max(0, width - 1))
            tap_y = _coordinate(y, "y", max(0, height - 1))
            url = _helix_url(view, HELIXSCREEN_TOUCH_PATH, discover=discover)
            for event in ("down", "up"):
                try:
                    status, _body = helix_post(
                        url, {"type": event, "x": tap_x, "y": tap_y}, token)
                except Exception as exc:  # sharing server not answering
                    raise ScreenError(502, f"printer screen unreachable "
                                           f"({type(exc).__name__})") from None
                if status in (401, 403):
                    raise ScreenError(403, "the HelixScreen screen no longer "
                                           "honors Print Handy's pairing: pair "
                                           "again from the printer screen "
                                           "page. Nothing more was sent")
                if status != 200:
                    raise ScreenError(502, "the HelixScreen screen answered "
                                           "a tap unexpectedly; nothing more "
                                           "was sent")
        else:
            url = _screen_click_url(view, discover=discover) \
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


def pair(data, printer_id, code, *, discover=None, helix_post=None,
         save_token=None):
    """(code, body) for POST /printers/<id>/screen/pair (#1720).

    Exchange the six-digit pairing code the printer shows for the
    pairing token HelixScreen's sharing server issues, and keep the
    token in the pairing store (see :func:`save_pairing_token`):
    the code itself is discarded the moment the exchange answers.
    Behind the same controls gate as the tap — pairing is what makes
    pressing possible, so it needs the same consent. A ZMod screen
    has nothing to pair and refuses by name; a wrong code is the
    server's own refusal, relayed without the code."""
    discover = _cached_discover if discover is None else discover
    helix_post = post_screen_json if helix_post is None else helix_post
    save_token = save_pairing_token if save_token is None else save_token
    try:
        from . import controls as controls_module
        if not controls_module.controls_enabled(data):
            raise ScreenError(403, controls_module.OFF_REASON)
        try:
            settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise ScreenError(404, f"no printer with id {printer_id!r} "
                                   f"is configured") from None
        text = str(code or "").strip()
        if len(text) != 6 or not text.isdigit():
            raise ScreenError(400, "the pairing code is the six digits the "
                                   "printer shows; nothing was sent")
        view = settings.view_for(printer_id, data)
        url = _helix_url(view, HELIXSCREEN_PAIR_PATH, discover=discover)
        try:
            status, body = helix_post(url, {"code": text}, "")
        except Exception as exc:  # sharing server not answering
            raise ScreenError(502, f"printer screen unreachable "
                                   f"({type(exc).__name__})") from None
        if status == 429:
            raise ScreenError(429, "too many pairing tries; wait a minute "
                                   "and try again")
        token = body.get("token") if isinstance(body, dict) else None
        if status != 200 or not isinstance(token, str) or not token:
            if status == 200:
                raise ScreenError(502, "the HelixScreen screen answered the "
                                       "pairing without a token; nothing "
                                       "was saved")
            raise ScreenError(401, "that pairing code did not match; "
                                   "nothing was saved")
        if not save_token(data, printer_id, token):
            raise ScreenError(502, "the pairing worked but Print Handy "
                                   "could not save it on this install, so "
                                   "it would be asked for again; nothing "
                                   "was kept")
    except ScreenError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}
    return 200, {"ok": True,
                 "detail": "Paired. Print Handy keeps the pairing on "
                           "this install, so the code is not asked "
                           "for again; taps on this screen are now "
                           "available while printer controls are on."}


def probe(data, printer_id, *, discover=None):
    """(available, reason) for the screen page: discovery only, no still
    is fetched, so opening the page never pulls a frame."""
    available, reason, _source = probe_source(data, printer_id,
                                              discover=discover)
    return available, reason


def probe_source(data, printer_id, *, discover=None):
    """(available, reason, source) for the screen page: discovery only,
    no still is fetched, so opening the page never pulls a frame.
    ``source`` is SOURCE_ZMOD or SOURCE_HELIXSCREEN for an available
    screen (the page labels it; taps are offered for a ZMod screen,
    and for a HelixScreen screen once it is paired), and an empty
    string when no screen is available."""
    discover = discover_webcams if discover is None else discover
    try:
        settings.by_id(printer_id, data)
    except settings.SettingsError:
        return False, f"no printer with id {printer_id!r} is configured", ""
    view = settings.view_for(printer_id, data)
    try:
        _url, source = _still_url_and_source(view, discover=discover)
    except ScreenError as refusal:
        return False, refusal.reason, ""
    return True, "", source


def render_page(printer_name, printer_id, *, ok, reason,
                theme=shell.DEFAULT_THEME, touch=False, form_token="",
                source=SOURCE_ZMOD, paired=False):
    """The dedicated screen page: a clearly labeled view of the screen.

    The still comes from this Web UI's own snapshot route, never from
    the printer's address. While ``touch`` is false (printer controls
    off) the page is read-only exactly as #1303 shipped it: the image
    is not a link and nothing on the page posts anywhere. While
    ``touch`` is true (the handler passes it only when
    ``[webui] controls_enabled`` is on), clicking the still posts one
    tap to this Web UI's own touch route with the page's form token;
    the image is still never a link to the printer and the service's
    own page is never embedded. A HelixScreen screen is tappable
    only once Print Handy is paired with it (``paired``, from the
    stored pairing token): while it is not paired the page carries
    the pairing form instead, posting the six-digit code to this Web
    UI's own pair route — the code is exchanged once and never
    stored, so it is not asked for again on later visits.
    """
    esc = html.escape
    name = esc(str(printer_name or printer_id))
    pair_block = ""
    if ok:
        src = "/api/printers/" + quote(str(printer_id), safe="") \
            + "/screen/snapshot"
        if touch and (source != SOURCE_HELIXSCREEN or paired):
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
        elif source == SOURCE_HELIXSCREEN:
            if touch:
                intro = (
                    "<p>This is the printer's own display, shown through "
                    "HelixScreen's screen sharing. It is not the print-bed "
                    "camera. Printer controls are on, but this screen is "
                    "not paired with Print Handy yet, so it cannot be "
                    "pressed from here: enter the pairing code the printer "
                    "shows under Settings, Connection, Screen Sharing once, "
                    "and Print Handy keeps the pairing on this install — "
                    "the code itself is not stored, and it is not asked "
                    "for again. Allow Remote Control must also be on in "
                    "that same Screen Sharing settings page.</p>")
                pair_url = "/printers/" + quote(str(printer_id), safe="") \
                    + "/screen/pair"
                pair_block = (
                    '<form id="printer-screen-pair">'
                    f'<input type="hidden" id="printer-screen-csrf" '
                    f'value="{esc(form_token)}">'
                    '<p><label for="f-pair-code">Pairing code</label> '
                    '<input type="text" id="f-pair-code" name="code" '
                    'inputmode="numeric" autocomplete="one-time-code" '
                    'maxlength="6" pattern="[0-9]{6}" required> '
                    '<button type="submit">Pair</button></p>'
                    '<p id="printer-screen-pair-note"></p></form>'
                    "<script>(function () {"
                    '  var form = document.getElementById("printer-screen-pair");'
                    '  var note = document.getElementById("printer-screen-pair-note");'
                    '  form.addEventListener("submit", function (e) {'
                    "    e.preventDefault();"
                    '    var csrf = document.getElementById("printer-screen-csrf");'
                    '    var code = document.getElementById("f-pair-code").value;'
                    "    note.textContent = 'Pairing...';"
                    f"    fetch({json.dumps(pair_url)}, {{method: 'POST',"
                    "      credentials: 'same-origin',"
                    "      headers: {'Content-Type': 'application/x-www-form-urlencoded'},"
                    "      body: '_csrf=' + encodeURIComponent(csrf ? csrf.value : '')"
                    "        + '&code=' + encodeURIComponent(code)})"
                    "    .then(function (r) { return r.json().catch(function () {"
                    "      return {ok: false, reason: 'HTTP ' + r.status}; }); })"
                    "    .then(function (j) {"
                    "      if (j.ok) { note.textContent = j.detail || 'Paired.';"
                    "        setTimeout(function () { location.reload(); }, 600); }"
                    "      else { note.textContent = 'Not paired: ' + (j.reason || 'refused'); }"
                    "    })"
                    "    .catch(function () { note.textContent ="
                    "      'Could not reach the Web UI server; nothing was sent.'; });"
                    "  });"
                    "})();</script>")
            else:
                intro = (
                    "<p>This is the printer's own display, shown read-only "
                    "through HelixScreen's screen sharing. It is not the "
                    "print-bed camera, and nothing on this page can press "
                    "the screen: to answer a prompt, use the printer itself "
                    "or HelixScreen's own screen sharing.</p>")
            img_style = "max-width:100%"
            touch_script = "  var keepNote = false;"
            csrf_input = ""
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
            + pair_block
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
