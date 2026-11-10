"""Live camera source resolution and encoder planning (issue #311, stages 1-2).

Stage 1 — camera source core. This module resolves one normalised
:class:`CameraSource` from the payloads Print Handy already knows how to
obtain, without performing any network I/O itself: callers fetch a payload
through their existing client/transport and pass the parsed result in, so
every resolver here is pure, stdlib-only and offline-testable, following
the precedent of :mod:`print_handy.macros`.

- **Moonraker** (which also covers Fluidd and Mainsail households — both
  are frontends over the same Moonraker webcam registry, so there is no
  separate Fluidd integration): a webcams-API payload of the shape
  ``{"webcams": [...]}`` whose entries carry ``stream_url`` /
  ``snapshot_url`` / ``service`` / ``name``. Relative URLs are resolved
  against the Moonraker base URL; the configured (preferred) name wins,
  otherwise the first usable webcam.
- **Fallback derivation**: when there is no usable webcams payload, a
  stream URL is derived from the existing snapshot-URL convention
  (``?action=snapshot`` becomes ``?action=stream``). A snapshot URL with
  no ``action`` parameter does not imply a stream, so the source stays
  snapshot-kind.
- **OctoPrint**: a settings payload carrying the classic webcam fields
  (``streamUrl`` / ``snapshotUrl``, usually under a ``webcam`` key).
  Relative URLs resolve against the OctoPrint base. A multi-webcam style
  payload (a ``webcams`` list under the payload or under ``webcam``) is
  handled the same way when present; otherwise the legacy single-webcam
  shape is the supported one.
- **Snapshot-only**: a source with only a snapshot URL is a valid
  :class:`CameraSource` of kind ``snapshot`` (a later stage can turn it
  into an image sequence; it is labelled honestly, not as a stream).

Credentials: camera URLs may carry userinfo or secret query values in the
wild. The resolved :class:`CameraSource` keeps the real URLs as fields
(the encoder needs them), but its ``repr`` — and every error message this
module raises — passes URLs through :func:`redact_url`, so a password or
secret query value never appears in a repr, a log-facing string or an
error.

Stage 2 — codec-aware encoder plan. Discord voice video targets H.264 or
VP8, so encoder selection is codec-aware:

- H.264 default order: VAAPI, NVENC, QSV, CPU (``libx264``).
- VP8 default order: VAAPI (``vp8_vaapi``), CPU (``libvpx``). NVENC has
  no VP8 encoder and QSV is not a VP8 target here, so neither ever
  appears for VP8.
- An explicit preference (``vaapi`` / ``qsv`` / ``nvenc`` / ``cpu``)
  moves that vendor first when it is given *and* available; otherwise
  the default order stands.

Availability comes from an injected probe (:class:`EncoderProbe`: the set
of encoder names ffmpeg reports, whether ``/dev/dri`` is present, and
whether an NVIDIA device is present). VAAPI and QSV candidates require
``dri_present``; NVENC requires ``nvidia_present``; every candidate also
requires its encoder name to be in the probed set. No probing, process
execution or hardware access happens in this module.

:func:`build_ffmpeg_args` is a pure argv builder for one chosen
candidate. Its output decision is deliberate and narrow: the argv ends
with exactly the caller-supplied ``output`` argument and the builder
adds no muxer, RTP or Discord-specific flags. A later transport stage
supplies that output target (for example a pipe target it owns) and any
muxer flags its transport needs; this module does not invent a Discord
transport.

Stage 3 — the viewing session. The Stage 0 audit concluded native bot
video is a no-go (Discord's bot API is audio-only; the custom-sender
lineage is selfbot-only), and the chosen fallback is the Discord
Embedded App (Activity): :class:`LiveStreamSession` is the small state
machine the ``/print-live`` bot command drives — ``idle`` / ``live``,
who started it, when, and a viewer-count hook the transport reports
through so a later stage can end a session whose voice channel has
been empty for the configured timeout. The ``start_problem`` /
``pointer_text`` / ``session_text`` helpers word the command's replies
from a ``[live_stream]`` settings section. Everything here is still
pure: no connection is opened, no channel is joined, no camera is
touched.

Stage 4 — deployment wiring. The session gains a shared on-disk record
(:func:`session_file_path`, :func:`read_session_file`,
:func:`write_session_file`) so the processes that each see one side of
the feature — the bot (``/print-live``), the printer listener (print
starts and ends) and the Activity server (authenticated page loads) —
share one session instead of three private ones. The two lifecycle
helpers the stage promised live here too: :func:`auto_start` (the
auto-start-on-print trigger) and :func:`expire_empty` (the
leave-when-empty enforcement). The go2rtc stream the Activity page
plays is named by :data:`GO2RTC_STREAM_NAME`, the one name
``print_handy.go2rtc`` renders and ``print_handy.live_activity``
requests, so the two can never drift apart.
"""
from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # non-POSIX platform: no cross-process lock available
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "CAMERA_KINDS",
    "ENCODER_PREFERENCES",
    "GO2RTC_LIVE_STREAM_NAME",
    "GO2RTC_STREAM_NAME",
    "TARGET_CODECS",
    "VAAPI_DEVICE",
    "CameraSource",
    "EncoderCandidate",
    "EncoderProbe",
    "LiveStreamError",
    "LiveStreamSession",
    "SESSION_STATES",
    "auto_start",
    "build_ffmpeg_args",
    "build_ffmpeg_command",
    "camera_source_from_moonraker",
    "camera_source_from_octoprint",
    "derive_from_snapshot_url",
    "encoder_candidates",
    "expire_empty",
    "feed_stream_name",
    "plan_encoders",
    "pointer_text",
    "read_session_file",
    "redact_url",
    "resolve_moonraker_camera_source",
    "resolve_octoprint_camera_source",
    "session_file_lock",
    "session_file_path",
    "session_text",
    "start_problem",
    "transact_session_file",
    "write_session_file",
]

CAMERA_KINDS = ("mjpeg", "rtsp", "webrtc", "snapshot")
TARGET_CODECS = ("h264", "vp8")
ENCODER_PREFERENCES = ("auto", "vaapi", "qsv", "nvenc", "cpu")
VAAPI_DEVICE = "/dev/dri/renderD128"
# The one go2rtc stream name for the printer camera (#311 stage 4):
# print_handy.go2rtc renders its generated config under this name and
# the Activity page (print_handy.live_activity) requests it, so the
# page can never ask for a stream the container does not publish.
GO2RTC_STREAM_NAME = "camera"
# The transcoded live-view stream (#311, wired in 2.2.2): when a
# [live_stream] transcode value is set away from its default, go2rtc's
# generated config publishes this second stream — an ffmpeg transcode
# of the raw GO2RTC_STREAM_NAME restream, so the camera still gets
# exactly one upstream pull and timelapse capture keeps the camera's
# own stream — and the Activity page requests this name instead.
GO2RTC_LIVE_STREAM_NAME = "camera-live"
# The [live_stream] transcode values and their defaults. Changing any
# one engages the transcode with all four current values applied. At
# the defaults an RTSP or WebRTC source is relayed as it arrives, but
# an MJPEG or snapshot source is transcoded anyway: the Activity page
# speaks only MSE H.264, which those sources are not.
LIVE_TRANSCODE_DEFAULTS = {"max_height": 720, "fps": 15,
                           "max_bitrate_kbps": 2500, "encoder": "auto"}

_SENSITIVE_QUERY_KEYS = frozenset({
    "token", "api_key", "apikey", "api-key", "access_token", "access-token",
    "password", "passwd", "secret", "key", "auth", "authorization",
    "signature", "sig", "credential", "credentials",
})


class LiveStreamError(RuntimeError):
    """A camera source could not be resolved, or an encoder plan is invalid.

    Messages never contain URL userinfo, passwords or secret query values;
    any URL included in a message has passed through :func:`redact_url`.
    """


# --------------------------------------------------------------------- redaction

def redact_url(url) -> str:
    """Return `url` with userinfo and secret query values replaced by ``***``.

    Safe to call on any value: non-strings and unparseable values return a
    fixed placeholder rather than the raw input, so a malformed
    credential-bearing string cannot leak through an error message.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return "***"
    if not parts.netloc and not parts.query:
        # A wrapper scheme such as go2rtc's ffmpeg: carries a nested URL in
        # its path (ffmpeg:rtsp://user:pass@host/...), which has no authority
        # of its own at this level: redact the nested URL too, or the wrapper
        # passes the camera credentials through verbatim.
        if "://" in parts.path:
            nested = redact_url(parts.path)
            return f"{parts.scheme}:{nested}" if parts.scheme else nested
        # A relative path carries no userinfo; still scrub a query if present.
        return url.strip()
    host = parts.hostname or ""
    if parts.username or parts.password:
        netloc = f"***@{host}"
    else:
        netloc = host
    try:
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
    except ValueError:
        netloc = f"{netloc}:***"
    query = ""
    if parts.query:
        pairs = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.casefold()
            if lowered in _SENSITIVE_QUERY_KEYS or any(
                    word in lowered for word in ("token", "secret", "password", "passwd")):
                pairs.append((key, "***"))
            else:
                pairs.append((key, value))
        query = urlencode(pairs)
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold()
    return lowered in _SENSITIVE_QUERY_KEYS or any(
        word in lowered for word in ("token", "secret", "password", "passwd"))


# --------------------------------------------------------------------- helpers

def _split_url(url, *, context="camera URL"):
    """`urlsplit` that never lets a parse failure carry credentials out.

    urlsplit's ValueError message embeds the whole authority — userinfo
    included — so a malformed credential-bearing URL would otherwise leak
    its password through the exception text, and through the traceback
    chain if the ValueError rode along as context. Raise a sanitized
    LiveStreamError instead, with the chain suppressed.
    """
    try:
        return urlsplit(url)
    except ValueError:
        pass  # fall through: raise only once the except block has exited,
        # so the ValueError is neither the cause nor the context of the
        # LiveStreamError and no traceback of it can disclose the authority.
    raise LiveStreamError(f"{context}: unparseable URL {redact_url(url)!r}")


def _clean(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _resolve_url(base_url, raw_url, *, context: str):
    """Resolve `raw_url` against `base_url`; absolute URLs pass through."""
    url = _clean(raw_url)
    if url is None:
        return None
    parts = _split_url(url, context=context)
    if parts.scheme:
        if parts.scheme not in ("http", "https", "rtsp", "rtsps", "webrtc"):
            raise LiveStreamError(
                f"{context}: unsupported camera URL scheme in {redact_url(url)!r}")
        return url
    base = _clean(base_url)
    if not base:
        raise LiveStreamError(
            f"{context}: relative camera URL {redact_url(url)!r} has no base URL to resolve against")
    if _split_url(base, context=context).scheme not in ("http", "https"):
        raise LiveStreamError(f"{context}: base URL {redact_url(base)!r} is not an http(s) URL")
    return urljoin(base.rstrip("/") + "/", url)


def _infer_kind(stream_url, snapshot_url, service=None) -> str:
    if stream_url:
        service_text = (service or "").casefold()
        stream_text = stream_url.casefold()
        if _split_url(stream_url).scheme in ("rtsp", "rtsps"):
            return "rtsp"
        if "webrtc" in service_text or "webrtc" in stream_text:
            return "webrtc"
        return "mjpeg"
    if snapshot_url:
        return "snapshot"
    raise LiveStreamError("camera source has neither a stream URL nor a snapshot URL")


def _make_source(*, stream_url, snapshot_url, name="", service=None) -> "CameraSource":
    """Build a source, deriving the stream from the snapshot when needed."""
    if stream_url:
        kind = _infer_kind(stream_url, snapshot_url, service)
        return CameraSource(stream_url=stream_url, snapshot_url=snapshot_url,
                            kind=kind, name=name or "", service=service)
    if snapshot_url:
        derived = derive_from_snapshot_url(snapshot_url, name=name or "", service=service)
        return derived
    raise LiveStreamError("camera source has neither a stream URL nor a snapshot URL")


# --------------------------------------------------------------------- CameraSource

@dataclass(frozen=True)
class CameraSource:
    """One normalised camera source.

    `stream_url` is ``None`` for a snapshot-only source (`kind` is then
    ``"snapshot"``). The fields hold the real URLs; ``repr`` redacts them,
    so logging a source never logs a credential.
    """

    stream_url: str | None
    snapshot_url: str | None
    kind: str
    name: str = ""
    service: str | None = None

    def __post_init__(self):
        if self.kind not in CAMERA_KINDS:
            raise LiveStreamError(f"unknown camera source kind {self.kind!r}")
        if self.kind == "snapshot" and self.stream_url is not None:
            raise LiveStreamError("a snapshot-kind camera source must not carry a stream URL")
        if self.kind != "snapshot" and not self.stream_url:
            raise LiveStreamError(f"a {self.kind} camera source needs a stream URL")
        if not self.stream_url and not self.snapshot_url:
            raise LiveStreamError("a camera source needs a stream URL or a snapshot URL")

    def __repr__(self):
        return (f"CameraSource(name={self.name!r}, kind={self.kind!r}, "
                f"stream_url={redact_url(self.stream_url)!r}, "
                f"snapshot_url={redact_url(self.snapshot_url)!r}, "
                f"service={self.service!r})")

    @property
    def redacted_stream_url(self) -> str:
        return redact_url(self.stream_url)

    @property
    def redacted_snapshot_url(self) -> str:
        return redact_url(self.snapshot_url)


def derive_from_snapshot_url(snapshot_url, *, name="", service=None) -> CameraSource:
    """Resolve a source from a snapshot URL alone (the fallback derivation).

    ``?action=snapshot`` becomes ``?action=stream`` (kind ``mjpeg``). A
    snapshot URL with no ``action`` parameter yields a snapshot-only
    source (kind ``snapshot``, no stream URL).
    """
    snapshot = _clean(snapshot_url)
    if not snapshot:
        raise LiveStreamError("no snapshot URL was provided to derive a camera source from")
    parts = _split_url(snapshot, context="snapshot URL")
    if parts.scheme not in ("http", "https", "rtsp", "rtsps"):
        raise LiveStreamError(
            f"snapshot URL {redact_url(snapshot)!r} is not an http(s) or rtsp URL")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    action_values = [value for key, value in pairs if key == "action"]
    if "snapshot" in action_values:
        new_pairs = [(key, "stream" if (key == "action" and value == "snapshot") else value)
                     for key, value in pairs]
        stream_url = urlunsplit((parts.scheme, parts.netloc, parts.path,
                                 urlencode(new_pairs), ""))
        return CameraSource(stream_url=stream_url, snapshot_url=snapshot,
                            kind=_infer_kind(stream_url, snapshot, service),
                            name=name or "", service=service)
    return CameraSource(stream_url=None, snapshot_url=snapshot, kind="snapshot",
                        name=name or "", service=service)


# --------------------------------------------------------------------- Moonraker

def _webcam_entries(payload):
    """The webcam entry dicts from a Moonraker-shaped payload, or ``None``.

    Accepts ``{"webcams": [...]}``, ``{"result": {"webcams": [...]}}`` and
    a bare list, so recorded responses replay unchanged. Returns ``None``
    when the payload carries no webcams list at all.
    """
    data = payload
    if isinstance(data, dict) and "result" in data and isinstance(data["result"], dict):
        data = data["result"]
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("webcams"), list):
        entries = data["webcams"]
    else:
        return None
    return [entry for entry in entries if isinstance(entry, dict)]


def _pick_entry(entries, preferred_name):
    usable = []
    for entry in entries:
        if entry.get("enabled") is False:
            continue
        stream = _clean(entry.get("stream_url") or entry.get("streamUrl"))
        snapshot = _clean(entry.get("snapshot_url") or entry.get("snapshotUrl"))
        if stream or snapshot:
            usable.append(entry)
    if not usable:
        return None
    if preferred_name:
        wanted = str(preferred_name).casefold()
        for entry in usable:
            entry_name = entry.get("name")
            if isinstance(entry_name, str) and entry_name.casefold() == wanted:
                return entry
    return usable[0]


def camera_source_from_moonraker(payload, *, base_url=None, preferred_name=None,
                                 fallback_snapshot_url=None) -> CameraSource:
    """Resolve one camera source from a Moonraker webcams-API payload.

    Fluidd and Mainsail households are covered by this same path: both are
    frontends over Moonraker's webcam registry, whose entries this payload
    lists. When the payload is missing, empty or holds no usable webcam,
    `fallback_snapshot_url` (the existing snapshot-URL convention) is used
    via :func:`derive_from_snapshot_url`; without a fallback that case
    raises :class:`LiveStreamError`.
    """
    entries = _webcam_entries(payload)
    if entries:
        entry = _pick_entry(entries, preferred_name)
        if entry is not None:
            stream = _resolve_url(base_url, entry.get("stream_url") or entry.get("streamUrl"),
                                   context="Moonraker webcam")
            snapshot = _resolve_url(base_url, entry.get("snapshot_url") or entry.get("snapshotUrl"),
                                     context="Moonraker webcam")
            name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            service = entry.get("service") if isinstance(entry.get("service"), str) else None
            return _make_source(stream_url=stream, snapshot_url=snapshot,
                                name=name, service=service)
    if fallback_snapshot_url:
        return derive_from_snapshot_url(fallback_snapshot_url)
    raise LiveStreamError(
        "Moonraker's webcams payload had no usable webcam and no fallback snapshot URL was provided")


def resolve_moonraker_camera_source(*args, **kwargs) -> CameraSource:
    """Alias for :func:`camera_source_from_moonraker`."""
    return camera_source_from_moonraker(*args, **kwargs)


# --------------------------------------------------------------------- OctoPrint

def _octoprint_single_and_entries(payload):
    """Return ``(single_dict_or_None, entries_or_None)`` for an OctoPrint payload."""
    if not isinstance(payload, dict):
        return None, None
    if isinstance(payload.get("webcams"), list):
        return None, [e for e in payload["webcams"] if isinstance(e, dict)]
    webcam = payload.get("webcam")
    if isinstance(webcam, dict):
        if isinstance(webcam.get("webcams"), list):
            return None, [e for e in webcam["webcams"] if isinstance(e, dict)]
        return webcam, None
    if any(key in payload for key in ("streamUrl", "snapshotUrl", "stream_url", "snapshot_url")):
        return payload, None
    return None, None


def camera_source_from_octoprint(payload, *, base_url=None, preferred_name=None,
                                 fallback_snapshot_url=None) -> CameraSource:
    """Resolve one camera source from an OctoPrint settings payload.

    Supports the classic single-webcam shape (``webcam`` with
    ``streamUrl`` / ``snapshotUrl``, or those fields at the top level)
    and, when present, a multi-webcam style ``webcams`` list (at the top
    level or under ``webcam``) whose entries use the same field names.
    Relative URLs resolve against the OctoPrint base. When only a
    snapshot URL is present, the classic derivation applies
    (``?action=snapshot`` -> ``?action=stream``); a snapshot URL with no
    ``action`` parameter yields a snapshot-only source.
    """
    single, entries = _octoprint_single_and_entries(payload)
    entry = None
    if entries:
        # OctoPrint entries use the same name/enabled conventions; reuse the picker.
        normalised = []
        for item in entries:
            copy = dict(item)
            if "enabled" not in copy and "webcamEnabled" in copy:
                copy["enabled"] = copy["webcamEnabled"]
            normalised.append(copy)
        entry = _pick_entry(normalised, preferred_name)
    elif single is not None:
        if single.get("webcamEnabled") is False or single.get("enabled") is False:
            entry = None
        else:
            entry = single
    if entry is not None:
        stream = _resolve_url(base_url, entry.get("streamUrl") or entry.get("stream_url"),
                               context="OctoPrint webcam")
        snapshot = _resolve_url(base_url, entry.get("snapshotUrl") or entry.get("snapshot_url"),
                                 context="OctoPrint webcam")
        if stream or snapshot:
            name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            return _make_source(stream_url=stream, snapshot_url=snapshot, name=name)
    if fallback_snapshot_url:
        return derive_from_snapshot_url(fallback_snapshot_url)
    raise LiveStreamError(
        "OctoPrint's settings payload had no usable webcam and no fallback snapshot URL was provided")


def resolve_octoprint_camera_source(*args, **kwargs) -> CameraSource:
    """Alias for :func:`camera_source_from_octoprint`."""
    return camera_source_from_octoprint(*args, **kwargs)


# --------------------------------------------------------------------- encoder plan

@dataclass(frozen=True)
class EncoderProbe:
    """An injected ffmpeg/hardware probe result (no probing happens here)."""

    encoders: frozenset = frozenset()
    dri_present: bool = False
    nvidia_present: bool = False

    def __post_init__(self):
        object.__setattr__(self, "encoders", frozenset(self.encoders or ()))


@dataclass(frozen=True)
class EncoderCandidate:
    """One encoder choice: ffmpeg encoder name, vendor and target codec."""

    encoder: str
    vendor: str  # "vaapi" | "nvenc" | "qsv" | "cpu"
    codec: str   # "h264" | "vp8"
    hardware: bool = False

    @property
    def name(self) -> str:
        return self.encoder


# Default order per codec: (vendor, ffmpeg encoder name, device requirement).
# "dri" = requires dri_present, "nvidia" = requires nvidia_present,
# None = requires only the encoder name in the probed set.
_DEFAULT_ORDER = {
    "h264": (
        ("vaapi", "h264_vaapi", "dri"),
        ("nvenc", "h264_nvenc", "nvidia"),
        ("qsv", "h264_qsv", "dri"),
        ("cpu", "libx264", None),
    ),
    "vp8": (
        # NVENC has no VP8 encoder and QSV is not a VP8 target here: neither
        # is ever a VP8 candidate, whatever the probe or preference says.
        ("vaapi", "vp8_vaapi", "dri"),
        ("cpu", "libvpx", None),
    ),
}


def _coerce_probe(probe, dri_present, nvidia_present):
    """Normalise the accepted probe shapes into (encoders, dri, nvidia)."""
    if isinstance(probe, EncoderProbe):
        encoders = set(probe.encoders)
        dri = probe.dri_present if dri_present is None else dri_present
        nvidia = probe.nvidia_present if nvidia_present is None else nvidia_present
        return encoders, bool(dri), bool(nvidia)
    if isinstance(probe, dict):
        encoders = set(probe.get("encoders") or probe.get("available_encoders") or ())
        dri = probe.get("dri_present", False) if dri_present is None else dri_present
        nvidia = probe.get("nvidia_present", False) if nvidia_present is None else nvidia_present
        return encoders, bool(dri), bool(nvidia)
    if isinstance(probe, (set, frozenset, list, tuple)):
        encoders = set(probe)
        dri = ("h264_vaapi" in encoders or "vp8_vaapi" in encoders or "h264_qsv" in encoders) \
            if dri_present is None else dri_present
        nvidia = ("h264_nvenc" in encoders) if nvidia_present is None else nvidia_present
        return encoders, bool(dri), bool(nvidia)
    raise LiveStreamError("encoder probe must be an EncoderProbe, a mapping, or a set of encoder names")


def encoder_candidates(probe, *, dri_present=None, nvidia_present=None,
                       preference="auto", target_codec="h264", codec=None) -> tuple:
    """Ordered encoder candidates for `target_codec` from an injected probe.

    `probe` is an :class:`EncoderProbe`, a mapping with ``encoders`` /
    ``dri_present`` / ``nvidia_present``, or a bare set of ffmpeg encoder
    names (device presence then defaults from the hardware encoders in
    the set, unless `dri_present` / `nvidia_present` are passed). A given
    `preference` moves its vendor first when that candidate is available;
    an unavailable or codec-inapplicable preference (for example ``nvenc``
    for VP8) leaves the default order unchanged.
    """
    effective_codec = codec or target_codec
    if effective_codec not in TARGET_CODECS:
        raise LiveStreamError(f"unsupported target codec {effective_codec!r}; expected one of {TARGET_CODECS}")
    if preference not in ENCODER_PREFERENCES:
        raise LiveStreamError(
            f"unsupported encoder preference {preference!r}; expected one of {ENCODER_PREFERENCES}")
    encoders, dri, nvidia = _coerce_probe(probe, dri_present, nvidia_present)
    candidates = []
    for vendor, encoder_name, requirement in _DEFAULT_ORDER[effective_codec]:
        if encoder_name not in encoders:
            continue
        if requirement == "dri" and not dri:
            continue
        if requirement == "nvidia" and not nvidia:
            continue
        candidates.append(EncoderCandidate(encoder=encoder_name, vendor=vendor,
                                           codec=effective_codec, hardware=(vendor != "cpu")))
    if preference != "auto":
        for index, candidate in enumerate(candidates):
            if candidate.vendor == preference:
                if index:
                    candidates.insert(0, candidates.pop(index))
                break
    return tuple(candidates)


def plan_encoders(*args, **kwargs) -> tuple:
    """Alias for :func:`encoder_candidates`."""
    return encoder_candidates(*args, **kwargs)


_ENCODER_INFO = {
    "h264_vaapi": ("vaapi", "h264"),
    "vp8_vaapi": ("vaapi", "vp8"),
    "h264_nvenc": ("nvenc", "h264"),
    "h264_qsv": ("qsv", "h264"),
    "libx264": ("cpu", "h264"),
    "libvpx": ("cpu", "vp8"),
    "libvpx-vp8": ("cpu", "vp8"),
}


def _coerce_candidate(candidate, target_codec):
    if isinstance(candidate, EncoderCandidate):
        if target_codec is not None and target_codec != candidate.codec:
            raise LiveStreamError(
                f"encoder {candidate.encoder!r} is for codec {candidate.codec!r}, not {target_codec!r}")
        return candidate
    if isinstance(candidate, str) and candidate in _ENCODER_INFO:
        vendor, codec_name = _ENCODER_INFO[candidate]
        if target_codec is not None and target_codec != codec_name:
            raise LiveStreamError(
                f"encoder {candidate!r} is for codec {codec_name!r}, not {target_codec!r}")
        encoder_name = "libvpx" if candidate == "libvpx-vp8" else candidate
        return EncoderCandidate(encoder=encoder_name, vendor=vendor, codec=codec_name,
                                hardware=(vendor != "cpu"))
    raise LiveStreamError(f"unknown encoder candidate {candidate!r}")


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LiveStreamError(f"{name} must be a positive integer, got {value!r}")
    return value


def build_ffmpeg_args(candidate, *, input_url, target_codec=None, max_height=720,
                      fps=15, bitrate_kbps=2500, output="-", executable="ffmpeg") -> list:
    """Pure ffmpeg argv for one chosen encoder candidate.

    Scales to `max_height` keeping aspect (width ``-2``, so it stays even),
    caps at `fps` and `bitrate_kbps`, and encodes with the candidate's
    encoder. VAAPI candidates initialise ``-vaapi_device
    /dev/dri/renderD128`` before the input and use the
    ``format=nv12,hwupload`` tail on the scale filter (the analogous form
    for ``vp8_vaapi``); CPU H.264 uses ``libx264 -preset ultrafast`` and
    CPU VP8 uses ``libvpx -deadline realtime``.

    The argv starts with `executable`: the caller passes the configured
    ``paths.ffmpeg`` so the binary launched is the one that was selected
    and probed (P2-262) — a bare ``"ffmpeg"`` could resolve to a
    different binary, or to none when ffmpeg exists only at the
    configured path. The default keeps the bare name for callers with
    no configured path.

    Output decision: the argv ends with exactly the caller-supplied
    `output` argument and adds no muxer, RTP or transport flags — the
    caller (a later transport stage) owns that target and its format.
    The input URL is placed in the argv verbatim because ffmpeg needs it;
    callers must not log the argv without redacting that element.
    """
    chosen = _coerce_candidate(candidate, target_codec)
    if target_codec is not None and target_codec not in TARGET_CODECS:
        raise LiveStreamError(f"unsupported target codec {target_codec!r}; expected one of {TARGET_CODECS}")
    if not isinstance(input_url, str) or not input_url.strip():
        raise LiveStreamError("an input URL is required to build ffmpeg arguments")
    if not isinstance(output, str) or not output.strip():
        raise LiveStreamError("an output target is required to build ffmpeg arguments")
    if not isinstance(executable, str) or not executable.strip():
        raise LiveStreamError("an ffmpeg executable is required to build ffmpeg arguments")
    max_height = _positive_int(max_height, "max_height")
    fps = _positive_int(fps, "fps")
    bitrate_kbps = _positive_int(bitrate_kbps, "bitrate_kbps")

    argv = [executable, "-hide_banner", "-nostdin"]
    if chosen.vendor == "vaapi":
        argv += ["-vaapi_device", VAAPI_DEVICE]
    argv += ["-i", input_url, "-an"]
    if chosen.vendor == "vaapi":
        video_filter = f"scale=-2:{max_height},format=nv12,hwupload"
    else:
        video_filter = f"scale=-2:{max_height}"
    argv += ["-vf", video_filter, "-r", str(fps),
             "-b:v", f"{bitrate_kbps}k", "-c:v", chosen.encoder]
    if chosen.encoder == "libx264":
        argv += ["-preset", "ultrafast"]
    elif chosen.encoder == "libvpx":
        argv += ["-deadline", "realtime"]
    argv.append(output)
    return argv


def build_ffmpeg_command(*args, **kwargs) -> list:
    """Alias for :func:`build_ffmpeg_args`."""
    return build_ffmpeg_args(*args, **kwargs)


# --------------------------------------------------------------------- stage 3: the viewing session

SESSION_STATES = ("idle", "live")


class LiveStreamSession:
    """The one live-viewing session's state (issue #311, stage 3).

    ``idle`` means nobody is being shown the camera; ``live`` means a
    start was accepted and the Activity pointer posted. The session
    records who started it and when (wall-clock seconds, supplied by the
    caller as ``now=`` so the machine stays pure and offline-testable),
    plus the viewer count the transport reports through
    :meth:`note_viewers`: a later stage ends the session once the voice
    channel has held no viewers for the configured timeout
    (:meth:`empty_expired`). A session that starts with no viewers is
    already "empty" from its start, so a start nobody joins can expire
    the same way. Nothing here opens a connection, joins a channel or
    touches a camera.
    """

    def __init__(self):
        self.state = "idle"
        self.started_by = ""
        self.started_at = None
        self.viewers = 0
        self.empty_since = None

    @property
    def is_live(self):
        return self.state == "live"

    def start(self, user_id, *, now):
        """idle -> live, started by `user_id` at wall-clock `now`."""
        if self.state == "live":
            raise LiveStreamError("the live view is already running")
        self.state = "live"
        self.started_by = str(user_id)
        self.started_at = float(now)
        self.viewers = 0
        self.empty_since = float(now)
        return self

    def stop(self, *, now=None):
        """live -> idle, clearing the start record. `now` is accepted for symmetry."""
        if self.state != "live":
            raise LiveStreamError("the live view is not running")
        self.state = "idle"
        self.started_by = ""
        self.started_at = None
        self.viewers = 0
        self.empty_since = None
        return self

    def note_viewers(self, count, *, now):
        """The transport's viewer-count hook: how many humans are in the channel."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LiveStreamError(f"viewer count must be a whole number from 0 up, got {count!r}")
        self.viewers = count
        if count > 0:
            self.empty_since = None
        elif self.empty_since is None:
            self.empty_since = float(now)
        return self

    def empty_expired(self, timeout_s, *, now):
        """True when live and viewer-less for at least `timeout_s` seconds."""
        return (self.state == "live" and self.empty_since is not None
                and float(now) - self.empty_since >= float(timeout_s))


def _section_value(section, key, default):
    value = (section or {}).get(key, default)
    return default if value is None else value


def transcode_engaged(section, *, source_kind=None) -> bool:
    """True when the live view must be the transcoded stream.

    The four values (max_height, fps, max_bitrate_kbps, encoder) act as
    one switch: changing any one makes print_handy.go2rtc publish the
    transcoded GO2RTC_LIVE_STREAM_NAME stream with all four applied.
    At the defaults the live view is go2rtc's relay of the camera
    stream as it arrives — but only for a source the Activity's MSE
    player can play (RTSP or WebRTC). An MJPEG or snapshot source is
    not H.264, and the page speaks only MSE fMP4, so those kinds
    engage the same transcode at the default values. ``source_kind``
    None keeps the values-only decision for callers that have no
    resolved source (the pointer text, the page default).
    """
    if any(_section_value(section, key, default) != default
           for key, default in LIVE_TRANSCODE_DEFAULTS.items()):
        return True
    return source_kind in ("mjpeg", "snapshot")


def feed_stream_name(section, *, source_kind=None) -> str:
    """The go2rtc stream the live Activity page plays for this section."""
    return (GO2RTC_LIVE_STREAM_NAME
            if transcode_engaged(section, source_kind=source_kind)
            else GO2RTC_STREAM_NAME)


def start_problem(section):
    """Why /print-live start cannot proceed, or None when it can.

    `section` is the ``[live_stream]`` settings mapping; missing keys read
    as the validated defaults, so a hand-built section in a test behaves
    like an older config.
    """
    if not _section_value(section, "enabled", False):
        return ("Live camera viewing is off. Set `[live_stream] enabled = true` "
                "(or PRINT_HANDY__LIVE_STREAM__ENABLED) to allow it; nothing was started.")
    if not str(_section_value(section, "voice_channel_id", "") or "").strip():
        return ("Live camera viewing is on, but no voice channel is configured. "
                "Set `[live_stream] voice_channel_id` to the ID of the private voice "
                "channel the Activity is launched in; nothing was started.")
    return None


def _channel_mention(section):
    channel = str(_section_value(section, "voice_channel_id", "") or "").strip()
    return f"<#{channel}>" if channel else "the configured voice channel"


def _user_mention(user_id):
    user_id = str(user_id or "")
    return f"<@{user_id}>" if user_id.isdigit() else (user_id or "someone")


def _duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min" if not rest else f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _plan_line(section):
    """What the live feed actually is, from the section's transcode values.

    At the defaults an RTSP or WebRTC source is relayed as it arrives.
    An MJPEG or snapshot source is not a format the Activity's MSE
    player can open, so that source is transcoded to H.264 at the
    default values instead. With any transcode value changed, go2rtc
    publishes the transcoded stream the Activity plays
    (print_handy.go2rtc's generated config), so the plan sentence
    names real, applied values — it is no longer a description of a
    plan nothing runs (the 2.2.1 audit found the old wording printed
    the values while the relay ignored them).
    """
    if transcode_engaged(section):
        return (f"Feed plan: {_section_value(section, 'encoder', 'auto')} encoder, "
                f"{_section_value(section, 'max_height', 720)}p at "
                f"{_section_value(section, 'fps', 15)} fps, "
                f"{_section_value(section, 'max_bitrate_kbps', 2500)} kbit/s cap — "
                "the live view is transcoded to this plan; timelapse capture "
                "keeps the camera's own stream.")
    return ("Feed: the camera stream is relayed to the live view as it arrives "
            "when it is already a stream the Activity can play (RTSP or WebRTC); "
            "an MJPEG or snapshot camera is transcoded to H.264 at the default "
            "feed values. Change the live feed height, frame rate, bitrate cap "
            "or encoder and the live view is transcoded to those values instead.")


def pointer_text(session, section):
    """The /print-live start reply: the Activity pointer for the configured channel."""
    lines = [f"Live camera view is on. Join {_channel_mention(section)} and launch the "
             "**Print Handy** activity in that channel to watch the printer camera live."]
    origin = str(_section_value(section, "activity_origin", "") or "").strip()
    if origin:
        lines.append(f"Activity page: {origin}")
    else:
        lines.append("The Activity page has no public origin configured yet "
                     "(`[live_stream] activity_origin`); set it to the tunnel origin "
                     "from the live-view setup in docs/DISCORD-BOT-SETUP.md.")
    lines.append(_plan_line(section))
    lines.append("`/print-live stop` ends the view. Nothing is recorded.")
    return "\n".join(lines)


def session_text(session, section, *, now):
    """The /print-live status reply (also the already-live answer to a second start)."""
    problem = start_problem(section)
    if problem and not session.is_live:
        # Off or unconfigured: the problem text is the honest status, but
        # worded as a status, not a refusal of a start nobody asked for.
        if not _section_value(section, "enabled", False):
            return ("Live camera viewing is off (`[live_stream] enabled = false`). "
                    "Nothing is streaming.")
        return ("Live camera viewing is on, but no voice channel is configured "
                "(`[live_stream] voice_channel_id` is empty). Nothing is streaming.")
    if not session.is_live:
        return (f"Live camera viewing is ready but not running. `/print-live start` "
                f"points viewers at {_channel_mention(section)}. {_plan_line(section)}")
    elapsed = _duration(float(now) - float(session.started_at or now))
    viewers = (f"{session.viewers} watching" if session.viewers
               else "nobody in the channel yet")
    return (f"Live camera view is on in {_channel_mention(section)}, started by "
            f"{_user_mention(session.started_by)} {elapsed} ago, {viewers}. "
            f"{_plan_line(section)} `/print-live stop` ends it.")


# --------------------------------------------------------------------- stage 4: lifecycle + shared record

#: Who the shared record credits when a print start opens the session.
AUTO_STARTED_BY = "auto-start on print"


def auto_start(session, section, *, now):
    """The auto-start-on-print trigger (#311 stage 4).

    When ``[live_stream] auto_start_on_print`` is on and the feature is
    otherwise configured (:func:`start_problem` has nothing to say),
    start `session` and return the pointer text to post; in every other
    case — the switch off, the feature unconfigured, a session already
    live — change nothing and return None. The caller owns posting the
    text; this helper never raises for a configuration problem, because
    a live-view trigger must never disturb a print announcement.
    """
    if not _section_value(section, "auto_start_on_print", False):
        return None
    if session.is_live or start_problem(section) is not None:
        return None
    session.start(AUTO_STARTED_BY, now=now)
    return pointer_text(session, section)


def expire_empty(session, section, *, now):
    """The leave-when-empty enforcement (#311 stage 4).

    End `session` when it has been live with no viewers for at least
    ``[live_stream] empty_timeout_s``; return True when this call ended
    it. Viewer presence reaches the session through
    :meth:`LiveStreamSession.note_viewers` — the Activity server notes
    a viewer on every authenticated page load — so a session nobody has
    opened, or whose viewers have all left, ends itself here. Callers
    run this wherever the session is next touched (a ``/print-live``
    command, a print transition, an Activity page load); there is no
    background timer, and the wording in the operator guide says so.
    """
    timeout = _section_value(section, "empty_timeout_s", 300)
    if session.empty_expired(timeout, now=now):
        session.stop(now=now)
        return True
    return False


def session_file_path(data=None):
    """Where the shared live-session record lives: ``<state dir>/live_session.json``.

    The state dir follows the same rule as every other Print Handy
    state file (``print_handy.settings.state_dir``); the import is
    local so this module stays importable without the settings loader.
    """
    from . import settings

    return settings.state_dir(data) / "live_session.json"


def write_session_file(session, path):
    """Persist `session` to `path` atomically (tmp file + replace).

    The record holds only session bookkeeping — state, starter,
    timestamps, viewer count — never a URL, a credential or a token.
    """
    path = Path(path)
    payload = {
        "state": session.state,
        "started_by": session.started_by,
        "started_at": session.started_at,
        "viewers": session.viewers,
        "empty_since": session.empty_since,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n")
    os.replace(tmp, path)
    return path


def read_session_file(path):
    """The session recorded at `path`, or a fresh idle session.

    A missing, unreadable or malformed record is an idle session, never
    an error: the record is a cache of bookkeeping shared between
    processes, and a live view must degrade to "not running" rather
    than fail a command or an announcement over a torn write.
    """
    session = LiveStreamSession()
    try:
        payload = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return session
    if not isinstance(payload, dict) or payload.get("state") not in SESSION_STATES:
        return session
    if payload["state"] == "live":
        session.state = "live"
        session.started_by = str(payload.get("started_by") or "")
        try:
            session.started_at = float(payload["started_at"])
            session.empty_since = (None if payload.get("empty_since") is None
                                   else float(payload["empty_since"]))
            viewers = int(payload.get("viewers") or 0)
        except (KeyError, TypeError, ValueError):
            return LiveStreamSession()
        session.viewers = viewers if viewers > 0 else 0
    return session


@contextlib.contextmanager
def session_file_lock(path):
    """Serialize session read-modify-write transactions across processes (P2-143).

    The record at `path` is shared by three processes — the bot
    (``/print-live``), the printer listener (auto-start / print-end
    hooks) and the Activity server (viewer presence) — and every writer
    runs a read-modify-write transaction over it. Atomic replacement
    alone cannot protect that transaction: a presence tick that read
    the live record just before a stop landed could write its stale
    snapshot back over the stop, resurrecting a session its owner had
    ended. This lock — an ``flock`` on the sibling ``<path>.lock``
    file — is the one guard every writer takes, so transactions run
    one at a time and each reads the record the previous writer left.

    ``flock`` is released by the kernel when a holder dies, so a
    crashed writer can never wedge the record. Where the lock file
    cannot be opened (a state directory too broken to hold the record
    either) or ``fcntl`` is unavailable, the transaction proceeds
    unguarded rather than breaking the caller: session bookkeeping
    stays fail-soft, exactly like the reads and writes it protects.
    """
    if fcntl is None:
        yield
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def transact_session_file(path, update):
    """Run one serialized read-modify-write transaction over the record at `path`.

    Under :func:`session_file_lock`: read the current session, hand it
    to `update(session)`, and persist it only when `update` returns
    True. Returns the session as read (and possibly mutated), so a
    caller can report the state it acted on. Every session writer goes
    through here (or holds the lock itself) — see P2-143.
    """
    with session_file_lock(path):
        session = read_session_file(path)
        if update(session):
            write_session_file(session, path)
        return session
