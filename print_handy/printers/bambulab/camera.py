"""Bambu Lab chamber camera: one bounded JPEG snapshot over the LAN (#294).

A1/P1-family printers serve camera stills as a raw JPEG frame stream on TCP
port 6000: the client connects, sends one 80-byte authentication packet (the
user name ``bblp`` and the printer's LAN access code in fixed 32-byte fields),
and the printer then streams frames, each preceded by a 16-byte little-endian
header whose first word is the frame size. The packet and framing are the
protocol behaviour of the maintained clients cited in
docs/BAMBU-CONNECTIVITY.md (pybambu and bambulabs_api, both MIT, at the pinned
commits recorded there); this implementation is written from those protocol
facts, not copied, and — like every Bambu LAN path in Print Handy — it has
**not been run against a real printer yet**.

X1-family printers serve RTSPS on port 322 instead (``bblp`` / access code,
stream path ``/streaming/live/1`` — the same maintained-client protocol
facts). Print Handy deliberately grows no RTSP stack for a still image: X1
stills come through the go2rtc fan-out from #246 (the decision recorded on
#294). go2rtc ingests the printer's RTSPS stream as its one configured
camera source and serves a JPEG still of it on its loopback API;
:func:`fanout_snapshot` reads that still, and only when the fan-out is
actually running with this printer's stream as its source — otherwise it
raises a plain error naming exactly what to enable, never a fake success.
Models whose camera protocol is not documented in the research (P2S, H2
family) report ``None`` — unverified is stated, never guessed.

The snapshot contract mirrors the Moonraker path's
``_camera_snapshot_bytes()``: at most 8 MiB, and the bytes must be a JPEG
(``FF D8`` … ``FF D9``). The transport is injectable (``transport_factory``)
so tests run fully offline with fakes, following the MQTT/FTPS factories in
client.py. Error messages never include the access code or any payload bytes:
the code is a secret, and a payload could be anything the network sent back.
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

import socket
import struct
import urllib.request
from urllib.parse import quote, urlsplit

from .client import BambuLanError

CAMERA_PORT = 6000
RTSPS_PORT = 322
RTSPS_PATH = "/streaming/live/1"
RTSPS_USER = "bblp"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_FRAME_HEADER = struct.Struct("<IIII")
_AUTH_PACKET = struct.Struct("<IIQ32s32s")
_MAX_FRAMES = 5  # a stream may open with a partial or non-JPEG frame; never read forever
_JPEG_SOI = bytes((0xFF, 0xD8))
_JPEG_EOI = bytes((0xFF, 0xD9))

# Model key -> camera protocol, from docs/BAMBU-CONNECTIVITY.md's camera row:
# A1/P1 = JPEG stills on TCP 6000; X1 = RTSPS on 322. Anything not listed here
# is not verified and reports None.
_CAMERA_KINDS = {
    "bambulab.a1": "tcp6000",
    "bambulab.a1mini": "tcp6000",
    "bambulab.p1p": "tcp6000",
    "bambulab.p1s": "tcp6000",
    "bambulab.x1": "rtsps",
    "bambulab.x1c": "rtsps",
    "bambulab.x1e": "rtsps",
}


class BambuCameraError(BambuLanError):
    """A camera failure, with no credential or payload detail attached."""


def camera_kind(model_key) -> str | None:
    """The verified camera protocol for a Bambu model key: "tcp6000", "rtsps", or None."""
    return _CAMERA_KINDS.get(str(model_key or ""))


def auth_packet(access_code: str) -> bytes:
    """The 80-byte port-6000 authentication packet: magic words, then bblp / access code."""
    return _AUTH_PACKET.pack(0x40, 0x3000, 0, b"bblp", str(access_code).encode("utf-8"))


def validate_snapshot_bytes(image) -> bytes:
    """Enforce the shared snapshot contract (bounded, JPEG) and return the frame.

    Mirrors ``_camera_snapshot_bytes()`` on the Moonraker path: at most
    ``MAX_SNAPSHOT_BYTES`` and JPEG magic at both ends. Trailing bytes after
    the end-of-image marker are dropped — the frame stream pads some frames.
    """
    if not isinstance(image, (bytes, bytearray)) or not image:
        raise BambuCameraError("camera response is not a bounded JPEG snapshot")
    if len(image) > MAX_SNAPSHOT_BYTES:
        raise BambuCameraError("camera response is not a bounded JPEG snapshot")
    if not bytes(image[:2]) == _JPEG_SOI:
        raise BambuCameraError("camera response is not a bounded JPEG snapshot")
    end = bytes(image).rfind(_JPEG_EOI)
    if end < 0:
        raise BambuCameraError("camera response is not a bounded JPEG snapshot")
    return bytes(image[: end + 2])


class _SocketTransport:
    """The real TCP transport: a connected socket with exact-length reads."""

    def __init__(self, host, port, timeout_s):
        self._sock = socket.create_connection((host, port), timeout=timeout_s)
        self._sock.settimeout(timeout_s)

    def sendall(self, data):
        self._sock.sendall(data)

    def recv_exact(self, count):
        chunks = []
        remaining = count
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError("the camera closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            logger.debug("camera socket close failed", exc_info=True)


def _socket_transport_factory(host, port, timeout_s):
    return _SocketTransport(host, port, timeout_s)


class BambuCameraClient:
    """One-shot camera client: connect, authenticate, take the first usable frame, disconnect."""

    def __init__(self, host, access_code, *, transport_factory=None, timeout_s=10.0):
        if not host or "://" in host or "/" in host or ":" in host or any(c.isspace() for c in host):
            raise BambuCameraError("Bambu Lab camera host must be a plain IP address or hostname, without scheme or port")
        if not access_code:
            raise BambuCameraError("Bambu Lab LAN access code is not set; put it in the environment variable named "
                                   "by [bambu] lan_access_code_env (default BAMBU_LAN_PASSWORD), never in config files")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
            raise BambuCameraError("Bambu Lab camera timeout must be a number of seconds up to 120")
        self.host = host
        self._access_code = access_code
        self.timeout_s = float(timeout_s)
        self._transport_factory = transport_factory or _socket_transport_factory

    def snapshot(self) -> bytes:
        """Return one bounded JPEG snapshot. Raises BambuCameraError on any failure.

        An authentication refusal looks like a camera that closes the
        connection or never sends a usable frame — the protocol has no
        explicit refusal message — so the error says exactly that, and never
        includes the access code or anything the printer sent.
        """
        try:
            transport = self._transport_factory(self.host, CAMERA_PORT, self.timeout_s)
        except Exception as exc:
            raise BambuCameraError(f"Bambu Lab camera at {self.host} could not be reached "
                                   f"({type(exc).__name__}); check the printer is on, in LAN Mode, and on this network") from exc
        try:
            transport.sendall(auth_packet(self._access_code))
            for _ in range(_MAX_FRAMES):
                header = transport.recv_exact(_FRAME_HEADER.size)
                size = _FRAME_HEADER.unpack(header)[0]
                if size <= 0 or size > MAX_SNAPSHOT_BYTES:
                    raise BambuCameraError("the camera sent a frame larger than the snapshot limit; nothing was kept")
                payload = transport.recv_exact(size)
                try:
                    return validate_snapshot_bytes(payload)
                except BambuCameraError:
                    continue  # a partial or non-JPEG frame: the stream's next frame may be whole
            raise BambuCameraError("the camera did not send a usable image; if this persists, the LAN access code "
                                   "may be wrong (re-read it on the printer, Settings > WLAN) or the camera may be busy")
        except BambuCameraError:
            raise
        except Exception as exc:
            raise BambuCameraError(f"the camera at {self.host} did not answer with an image ({type(exc).__name__}); "
                                   "if this persists, the LAN access code may be wrong (re-read it on the printer, "
                                   "Settings > WLAN) or the camera may be unavailable") from exc
        finally:
            try:
                transport.close()
            except Exception:
                logger.debug("camera transport close failed", exc_info=True)


# -- X1 family: stills through the go2rtc fan-out (#246, #294) ------------

def rtsp_url(host, access_code) -> str:
    """This printer's RTSPS stream URL: bblp / access code, port 322.

    The URL carries the access code, so callers must never log it
    unredacted; the errors below name the stream without it.
    """
    return (f"rtsps://{RTSPS_USER}:{quote(str(access_code), safe='')}@{host}:"
            f"{RTSPS_PORT}{RTSPS_PATH}")


def _default_fanout_url():
    """go2rtc's loopback stills URL when the fan-out should be running, else None."""
    from ... import go2rtc
    return go2rtc.fanout_frame_url()


def _default_resolved_source():
    """The URL go2rtc is configured to ingest, or None when none resolves."""
    from ... import go2rtc
    source = go2rtc.resolve_source()
    return go2rtc.source_url(source) if source is not None else None


def _source_is_this_printer(source_url, host) -> bool:
    """True when a go2rtc source URL is this printer's X1 RTSPS stream.

    The password is deliberately not compared: the operator's configured
    source may encode it differently, and a wrong password fails loudly at
    fetch time. Host, port, path and user are the identity, and they are
    compared exactly so a fan-out serving a different camera can never be
    mistaken for this printer's.
    """
    try:
        parts = urlsplit(source_url or "")
    except ValueError:
        return False
    if parts.scheme not in ("rtsp", "rtsps"):
        return False
    if (parts.hostname or "").lower() != str(host).lower():
        return False
    port = parts.port or (RTSPS_PORT if parts.scheme == "rtsps" else 554)
    if port != RTSPS_PORT:
        return False
    if (parts.path or "").rstrip("/") != RTSPS_PATH:
        return False
    return parts.username in (None, RTSPS_USER)


def fanout_frame_url(host, access_code, *, fanout=None, resolved=None) -> str:
    """The go2rtc stills URL for this printer's X1 stream.

    Raises BambuCameraError naming the limitation — never a fake success —
    when the fan-out is not running for this printer (neither timelapse
    capture nor live viewing enabled, or the bundled binary absent), or
    when it runs with a different camera source. The ``fanout`` and
    ``resolved`` seams exist for offline tests; production reads go2rtc.
    """
    fanout = _default_fanout_url if fanout is None else fanout
    resolved = _default_resolved_source if resolved is None else resolved
    url = fanout()
    if not url:
        raise BambuCameraError(
            "X1 camera stills come from the printer's RTSPS stream through go2rtc, "
            "and go2rtc is not running for this printer: enable [timelapse] "
            "capture_enabled or [live_stream] enabled and set [timelapse] "
            "capture_source_url to this printer's RTSPS stream "
            "(bblp / the LAN access code, port 322, path /streaming/live/1); "
            "no snapshot was taken")
    if not _source_is_this_printer(resolved(), host):
        raise BambuCameraError(
            "X1 camera stills come from the printer's RTSPS stream through go2rtc, "
            "but go2rtc's configured camera source is not this printer's stream; "
            "set [timelapse] capture_source_url to this printer's RTSPS stream "
            "(bblp / the LAN access code, port 322, path /streaming/live/1); "
            "no snapshot was taken")
    return url


def fetch_url_jpeg(url, timeout_s=10.0) -> bytes:
    """One bounded JPEG from an HTTP snapshot URL (the go2rtc frame endpoint).

    Same contract as a port-6000 snapshot: at most 8 MiB, JPEG-validated,
    and the error never carries the URL's credentials.
    """
    try:
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            image = response.read(MAX_SNAPSHOT_BYTES + 1)
    except Exception as exc:
        raise BambuCameraError(
            f"the go2rtc camera still could not be fetched ({type(exc).__name__}); "
            "if this persists, check go2rtc is running and its source is this "
            "printer's RTSPS stream") from exc
    try:
        return validate_snapshot_bytes(image)
    except BambuCameraError as exc:
        raise BambuCameraError(
            "the go2rtc camera still did not answer with an image; if this "
            "persists, check go2rtc is running and its source is this "
            "printer's RTSPS stream") from exc


def fanout_snapshot(host, access_code, *, fanout=None, resolved=None, fetch=None) -> bytes:
    """One X1 still through the go2rtc fan-out, or a named limitation.

    The adapter's rtsps branch calls this; the monitor's timelapse source
    for X1 models calls :func:`fanout_frame_url` directly and reuses
    :func:`fetch_url_jpeg` as its frame fetch.
    """
    fetch = fetch_url_jpeg if fetch is None else fetch
    url = fanout_frame_url(host, access_code, fanout=fanout, resolved=resolved)
    return fetch(url)
