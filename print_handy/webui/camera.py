"""Per-printer camera still for the Web UI (#708, camera snapshot proxy).

``GET /api/printers/<id>/camera/snapshot`` answers one JPEG from the
named roster printer's camera, so an external consumer (the Home
Assistant integration, a dashboard) never needs the camera URL or the
printer's credentials. The source is the one the rest of Print Handy
already uses for that printer, nothing new:

- ``[services] camera_source = none`` means camera images are off: 404;
- a printer whose adapter takes its own stills (Bambu LAN,
  ``adapter.camera_snapshot()``) uses that;
- otherwise the printer's ``camera_snapshot_url``. For the default
  printer, while the go2rtc fan-out is running it holds the one upstream
  camera pull (#311), so its still comes from go2rtc first with the
  direct URL as the single fallback, the watchdog's rule. go2rtc serves
  only the default printer's camera, so no other printer reads from it.

``snapshot()`` returns ``(http_code, body, content_type)``: JPEG bytes
on 200, the Web UI's ``{ok, reason}`` dict otherwise. A failure never
names the camera URL (it can carry credentials), only the error type.
"""
from __future__ import annotations

import urllib.request

from .. import go2rtc, printers, settings

MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT_S = 8
_JPEG_MAGIC = bytes((0xff, 0xd8))


class CameraError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def fetch_jpeg(url, timeout=_TIMEOUT_S):
    """One bounded JPEG from ``url`` (the listener's _fetch_snapshot rule)."""
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        image = response.read(MAX_BYTES + 1)
    if len(image) > MAX_BYTES or not image.startswith(_JPEG_MAGIC):
        raise ValueError("camera response is not a bounded JPEG snapshot")
    return image


def _still(adapter, view, is_default, fetch, fanout_url):
    if go2rtc.camera_source(view) == "none":
        raise CameraError(404, "camera images are off for this printer "
                               "(services.camera_source is none)")
    taker = getattr(adapter, "camera_snapshot", None)
    if callable(taker):
        try:
            return taker()
        except Exception as exc:  # the adapter's own message names no secret
            raise CameraError(502, f"camera snapshot failed: {exc}") from None
    direct = (view.get("services", {}).get("camera_snapshot_url") or "").strip()
    fanout = fanout_url(view) if is_default else None
    if fanout:
        try:
            return fetch(fanout)
        except Exception:
            if not direct:
                raise
    if not direct:
        raise CameraError(404, "no camera is configured for this printer "
                               "(camera_snapshot_url is empty)")
    return fetch(direct)


def snapshot(data, printer_id, *, adapter=None, fetch=None, fanout_url=None):
    """(code, body, content_type) for GET /api/printers/<id>/camera/snapshot."""
    fetch = fetch_jpeg if fetch is None else fetch
    fanout_url = go2rtc.fanout_frame_url if fanout_url is None else fanout_url
    try:
        try:
            entry = settings.by_id(printer_id, data)
        except settings.SettingsError:
            raise CameraError(404, f"no printer with id {printer_id!r} is configured") from None
        view = settings.view_for(printer_id, data)
        is_default = entry["id"] == settings.default_printer_id(data)
        try:
            adapter = adapter or printers.get(entry["model"], view)
            image = _still(adapter, view, is_default, fetch, fanout_url)
        except CameraError:
            raise
        except Exception as exc:  # unreachable camera: explicit, URL never echoed
            raise CameraError(502, f"camera unreachable ({type(exc).__name__})") from None
        if not isinstance(image, (bytes, bytearray)) or not bytes(image[:2]) == _JPEG_MAGIC:
            raise CameraError(502, "camera answered with something that is not a JPEG")
    except CameraError as refusal:
        return refusal.code, {"ok": False, "reason": refusal.reason}, "application/json"
    return 200, bytes(image), "image/jpeg"
