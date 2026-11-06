"""PrusaLink camera source: the cameras the printer itself registers (#295).

Protocol facts are from Prusa's published PrusaLink OpenAPI specification
(the `prusa-link` repository's spec — the same source the LAN client's
facts come from) and the Prusa-Link-Web frontend built on that spec:

- ``GET /api/v1/cameras`` lists the cameras PrusaLink serves for the
  printer; each entry carries ``camera_id`` and a ``connected`` flag.
  A camera here is a camera attached to the printer's PrusaLink (a USB
  camera; the CORE One's built-in camera is served this way). Which
  models and firmware expose any is the printer's business — an empty
  list, a list with nothing connected, or firmware that answers 404
  for the endpoint all mean "no camera source", and this module never
  invents one. ``GET /api/v1/info`` reports the same fact as
  ``active_camera``.
- ``GET /api/v1/cameras/<id>/snap`` (``/api/v1/cameras/snap`` for the
  default camera) returns the latest still as **PNG** — 204 when no
  image is available yet, 304 when it has not changed since the last
  poll. There is no stream and no JPEG form: the still is the source,
  at whatever rate the camera's own trigger scheme produces it.

Captured frames live in the #246 pipeline as JPEG (the capture session
stores ``frame-%06d.jpg`` and the encoder assembles those), so the PNG
still is transcoded once, here, with Pillow — already a Print Handy
dependency — at its native resolution.

None of this is verified on a live Prusa printer: no Print Handy code
has yet read a real PrusaLink camera list or still, and the whole Prusa
vendor is offline-verified only (docs/PRUSA.md). Until an operator
confirms it on hardware, the docs describe this as
protocol-documented, not verified.
"""
from __future__ import annotations

import io

from .client import PrusaLinkError

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The same per-frame bound the capture pipeline enforces
# (timelapse_capture.MAX_FRAME_BYTES): a hostile or broken source
# cannot stream unbounded data into memory through the transcoder.
MAX_STILL_BYTES = 8 * 1024 * 1024


def default_camera_id(client) -> str:
    """The id of the first connected camera PrusaLink reports, or "".

    Raises PrusaLinkError when the printer cannot be read at all —
    including a 404 from firmware without the camera API, whose error
    text names the endpoint and the answer. Callers treat a raise as
    "no camera source this time", never as a print failure.
    """
    for camera in client.cameras():
        if camera.get("connected"):
            return str(camera.get("camera_id") or "")
    return ""


def snapshot_url(client, camera_id) -> str:
    """The snapshot endpoint URL, for the capture manifest and logs.

    Carries no credential: PrusaLink auth travels in the X-Api-Key /
    Digest headers, never in a URL, so this is safe to record.
    """
    if camera_id:
        return f"{client.base}/api/v1/cameras/{camera_id}/snap"
    return f"{client.base}/api/v1/cameras/snap"


def snapshot_jpeg(client, camera_id=None) -> bytes:
    """One camera still as JPEG bytes. Raises on any problem.

    No image yet (204), an unchanged image (304) and a non-PNG or
    oversized answer are all raises: the capture session counts a
    raise as one failed fetch and moves on, so a camera that never
    produces a still degrades to a frameless session, not a stuck one.
    """
    payload = client.camera_snapshot(camera_id)
    if payload is None:
        raise PrusaLinkError("PrusaLink camera has no snapshot image yet")
    if len(payload) > MAX_STILL_BYTES or not payload.startswith(PNG_MAGIC):
        raise PrusaLinkError("PrusaLink camera snapshot is not a bounded PNG still")
    from PIL import Image  # Pillow: a hard dependency (pyproject.toml)

    out = io.BytesIO()
    with Image.open(io.BytesIO(payload)) as image:
        image.convert("RGB").save(out, format="JPEG", quality=88)
    return out.getvalue()
