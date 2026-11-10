"""go2rtc supervision gate and generated configuration (#246, Stage 1).

Stage 1 is container plumbing only: the full image bundles a pinned go2rtc
binary (docker/go2rtc-prebuilt.sh), and docker/supervise.sh runs it as a
supervised child exactly when a feature needs the camera fan-out.
go2rtc's only job is stream normalisation — it ingests the configured
camera source and restreams it on loopback; nothing in this module
captures frames or encodes video. One exception: the generated config
publishes a transcoded live-view stream (an ffmpeg exec source over
the loopback restream) when a [live_stream] transcode value is set
away from its default, and also at the defaults when the camera
source is MJPEG or a snapshot. The Activity page plays only MSE
H.264, which those sources are not, so the default relay would hand
the player a stream it cannot open. Capture still reads the raw
stream.

Two features share the one upstream camera pull (the #311 contention
rule: go2rtc holds the single connection to the camera and fans out):

- in-container timelapse capture (#246), when ``[timelapse]
  capture_enabled`` is set;
- live camera viewing in Discord (#311), when ``[live_stream] enabled``
  is set — the Activity page plays the restreamed feed, so live viewing
  must not require timelapse capture to be on.

This module is the shell-facing gate, mirroring print_handy.listener_gate:

    python3 -m print_handy.go2rtc              # gate: exit 0 / 3 / 2
    python3 -m print_handy.go2rtc --write-config

Exit codes: 0 = a feature needs go2rtc and a camera source resolved,
start go2rtc; 3 = not started (no feature needs it, or one does but no
camera source is configured — the feature degrades off, the container
is unaffected); 2 = the configuration could not be resolved (settings
error; the supervisor stops rather than guessing, like the listener
gate).

The generated configuration binds go2rtc's API (1984) and RTSP (8554) to
loopback only, and renders the one stream under
:data:`print_handy.live_stream.GO2RTC_STREAM_NAME` — the name the
timelapse capture polls and the #311 Activity page requests. It contains
the camera source URL, which may carry credentials, so it is written
with 0600 permissions, lives under the cache path with the other runtime
files, and is never logged unredacted: every message this module prints
passes URLs through print_handy.live_stream.redact_url.
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import urllib.request
from pathlib import Path

from . import settings
from .live_stream import (GO2RTC_LIVE_STREAM_NAME, GO2RTC_STREAM_NAME,
                          CameraSource, LiveStreamError, build_ffmpeg_args,
                          derive_from_snapshot_url, redact_url,
                          transcode_engaged)

API_URL = "http://127.0.0.1:1984/api"
#: Where go2rtc serves a JPEG still of the one published stream, on its
#: loopback API. Every stills consumer (timelapse capture, the camera
#: watchdog) reads from this one URL while go2rtc is running, so no
#: consumer ever opens a second direct connection to the camera.
FRAME_URL = f"http://127.0.0.1:1984/api/frame.jpeg?src={GO2RTC_STREAM_NAME}"
DEFAULT_BINARY = "/usr/local/bin/go2rtc"


def binary_path(environ=None) -> str:
    """Where the bundled go2rtc binary lives (GO2RTC_BIN overrides, for tests)."""
    return (environ or os.environ).get("GO2RTC_BIN", DEFAULT_BINARY)


def binary_present(environ=None) -> bool:
    """True when this image bundles go2rtc (a WITH_GO2RTC=false build does not)."""
    return os.access(binary_path(environ), os.X_OK)


def capture_enabled(data=None) -> bool:
    """True when [timelapse] capture_enabled is set in the resolved settings."""
    data = settings.current() if data is None else data
    return bool(data.get("timelapse", {}).get("capture_enabled", False))


def timelapse_mode(data=None) -> str:
    """The resolved [timelapse] mode: rendered, container or off.

    Settings validation derives an empty mode ("rendered" when a
    Moonraker URL is configured, else "container" when in-container
    capture is enabled, else "off"); the same derivation is mirrored
    here so a raw settings dict reads the same as a loaded one.
    """
    data = settings.current() if data is None else data
    timelapse = data.get("timelapse", {})
    mode = str(timelapse.get("mode") or "").strip().lower()
    if mode in ("rendered", "container", "off"):
        return mode
    if data.get("services", {}).get("moonraker_url"):
        return "rendered"
    return "container" if timelapse.get("capture_enabled") else "off"


def capture_active(data=None) -> bool:
    """The in-container capture gate: [timelapse] mode over capture_enabled.

    This is the gate the capture paths consult (2.2.2 wired mode into
    them; before, only the raw switch was read and mode=off stopped
    nothing). "off" vetoes capture even when capture_enabled is set;
    an explicit "container" opens capture on its own; "rendered" leaves
    the gate to capture_enabled exactly as before — the derivation makes
    "rendered" the mode of every Moonraker install, so it must not veto
    a capture the operator explicitly enabled there.
    """
    mode = timelapse_mode(data)
    if mode == "off":
        return False
    if mode == "container":
        return True
    return capture_enabled(data)


def rendered_active(data=None) -> bool:
    """True when the printer-rendered timelapse path may attach a clip.

    "off" disables timelapses outright and an explicit "container"
    chooses the in-container capture instead, so neither attaches the
    printer-rendered clip; "rendered" (including the derived default)
    leaves the rendered path as it has always run.
    """
    return timelapse_mode(data) not in ("off", "container")


def camera_source(data=None) -> str:
    """The [services] camera_source pin: auto, moonraker, bambu, go2rtc or none."""
    data = settings.current() if data is None else data
    value = str(data.get("services", {}).get("camera_source") or "auto").strip().lower()
    return value if value in ("auto", "moonraker", "bambu", "go2rtc", "none") else "auto"


def live_enabled(data=None) -> bool:
    """True when [live_stream] enabled is set in the resolved settings (#311)."""
    data = settings.current() if data is None else data
    return bool(data.get("live_stream", {}).get("enabled", False))


def needed_by(data=None) -> tuple:
    """The features that need the go2rtc fan-out, in report order.

    Timelapse capture (#246) and live viewing (#311) share the one
    upstream camera pull, so either one needing go2rtc starts the same
    single child with the same generated config.
    """
    data = settings.current() if data is None else data
    needed = []
    if capture_active(data):
        needed.append("timelapse capture")
    if live_enabled(data):
        needed.append("live viewing")
    return tuple(needed)


def fanout_frame_url(data=None, environ=None) -> str | None:
    """The go2rtc stills URL when go2rtc should be running, else None.

    go2rtc should be running exactly when a feature needs the fan-out
    (:func:`needed_by`), the bundled binary is present, and a camera
    source resolves — the same conditions the supervisor's gate starts
    it under. Consumers that need a still (the camera watchdog) use this
    URL in that case, so the camera keeps exactly one upstream pull;
    when it returns None go2rtc is not expected to be running and the
    consumer's own direct snapshot URL is the sole pull, as before.
    Any resolution failure reads as None: a stills consumer must never
    be broken by the fan-out's configuration.
    """
    try:
        data = settings.current() if data is None else data
        if not needed_by(data) or not binary_present(environ):
            return None
        if resolve_source(data) is None:
            return None
    except Exception:
        return None
    return FRAME_URL


def resolve_source(data=None) -> CameraSource | None:
    """The camera source go2rtc should ingest, or None when none is configured.

    An explicit [timelapse] capture_source_url wins; otherwise the source
    derives from services.camera_snapshot_url (which settings validation
    already derives from the Moonraker host for the standard Moonraker
    webcam case) via print_handy.live_stream's snapshot derivation.
    A go2rtc ``ffmpeg:`` source is passed through verbatim as the stream
    URL — go2rtc, not this module, interprets it.

    The [services] camera_source pin narrows the choice (2.2.2; before,
    nothing read the pin and "auto" behavior applied whatever it said):
    "none" resolves no source at all; "moonraker" uses only the
    snapshot-URL derivation, ignoring a stream override — the Moonraker
    webcam is the source, not an operator-named stream; "go2rtc" and
    "bambu" accept a stream override, and "go2rtc" additionally refuses
    a snapshot-only derivation (a stills URL is not the fan-out stream
    the pin asks for); "bambu" resolves no shared fan-out source without
    a stream override — the Bambu adapter runs its own camera paths, and
    its RTSPS stream reaches the fan-out through capture_source_url.
    """
    data = settings.current() if data is None else data
    pin = camera_source(data)
    if pin == "none":
        return None
    override = (data.get("timelapse", {}).get("capture_source_url") or "").strip()
    stream_override = override if override.startswith(("ffmpeg:", "rtsp://", "rtsps://")) else ""
    if stream_override and pin in ("auto", "go2rtc", "bambu"):
        kind = "mjpeg" if stream_override.startswith("ffmpeg:") else "rtsp"
        return CameraSource(stream_url=stream_override, snapshot_url=None,
                            kind=kind, name=GO2RTC_STREAM_NAME)
    if pin == "bambu":
        return None
    url = (override if not stream_override else "") or \
        (data.get("services", {}).get("camera_snapshot_url") or "").strip()
    if not url:
        return None
    source = derive_from_snapshot_url(url, name=GO2RTC_STREAM_NAME)
    if pin == "go2rtc" and source is not None and source.kind == "snapshot":
        return None
    return source


def source_url(source: CameraSource) -> str:
    """The single URL go2rtc ingests: the stream, or the snapshot for a
    snapshot-only source (go2rtc serves stills/MJPEG from it)."""
    return source.stream_url or source.snapshot_url or ""


#: Where go2rtc restreams the raw camera stream on loopback. The
#: transcoded live stream's ffmpeg reads this restream, so the camera
#: still gets exactly one upstream pull (the #311 contention rule) and
#: the transcode only runs while someone is watching.
RESTREAM_URL = f"rtsp://127.0.0.1:8554/{GO2RTC_STREAM_NAME}"


def transcode_plan(data=None, *, probe=None, run=None) -> dict | None:
    """The live-view transcode plan, or None when the feed is relayed as-is.

    Engaged when live viewing is enabled and either a [live_stream]
    transcode value is set away from its default, or the resolved
    camera source is one the Activity's MSE player cannot play
    (MJPEG or snapshot). The encoder is the first H.264 candidate for
    the operator's [live_stream] encoder preference from a probe of the
    real ffmpeg — the timelapse encode's prober and candidate order,
    which guarantee a CPU candidate last, so a plan always exists when
    ffmpeg does. ``probe`` and ``run`` are injectable for tests.
    """
    data = settings.current() if data is None else data
    section = data.get("live_stream", {})
    if not live_enabled(data):
        return None
    source = resolve_source(data)
    kind = None if source is None else source.kind
    if not transcode_engaged(section, source_kind=kind):
        return None
    from .hermes import timelapse_encode
    # The configured executable is carried in the plan (P2-262): it is
    # the binary the probe below inspects, so it must also be the one
    # the generated exec command launches. The live feed is H.264: the
    # Activity page offers only avc1 MSE codecs, so a VP8-first
    # candidate order would hand the player a stream it cannot open.
    ffmpeg = data.get("paths", {}).get("ffmpeg") or "ffmpeg"
    if probe is None:
        probe = timelapse_encode.probe_ffmpeg(ffmpeg, run=run)
    candidate = timelapse_encode.candidates(
        str(section.get("encoder") or "auto"), probe)[0]
    return {"candidate": candidate,
            "ffmpeg": ffmpeg,
            "max_height": int(section.get("max_height") or 720),
            "fps": int(section.get("fps") or 15),
            "bitrate_kbps": int(section.get("max_bitrate_kbps") or 2500)}


#: Stands in for go2rtc's {output} placeholder while the exec command is
#: shell-quoted: braces are not in shlex's safe set, so a literal
#: {output} would come out quoted and the address go2rtc substitutes
#: would reach ffmpeg wrapped in quote characters.
_OUTPUT_SENTINEL = "__GO2RTC_OUTPUT__"


def _transcode_source(plan: dict) -> str:
    """The go2rtc exec source that produces the transcoded live stream.

    The argv comes from print_handy.live_stream.build_ffmpeg_args — its
    first production caller (2.2.2; the 2.2.1 audit found none, so the
    four [live_stream] values shaped nothing). The builder's contract
    leaves muxer/transport flags to the caller: go2rtc's exec source
    publishes to its local RTSP server, whose address replaces
    ``{output}`` in the command at launch, so the caller appends the
    RTSP output flags before that placeholder.
    """
    argv = build_ffmpeg_args(plan["candidate"], input_url=RESTREAM_URL,
                             max_height=plan["max_height"], fps=plan["fps"],
                             bitrate_kbps=plan["bitrate_kbps"], output=_OUTPUT_SENTINEL,
                             executable=plan.get("ffmpeg") or "ffmpeg")
    argv = argv[:-1] + ["-rtsp_transport", "tcp", "-f", "rtsp", _OUTPUT_SENTINEL]
    return "exec:" + " ".join(shlex.quote(arg) for arg in argv).replace(
        _OUTPUT_SENTINEL, "{output}")


def render_config(source: CameraSource, *, transcode: dict | None = None) -> str:
    """go2rtc YAML for one camera source; API and RTSP bind loopback only.

    The URL is emitted as a JSON string scalar, which is valid YAML and
    needs no hand-rolled escaping. The file holds the raw URL (go2rtc
    needs it), so callers must treat the file as a secret.

    With a transcode plan, a second stream is published under
    :data:`print_handy.live_stream.GO2RTC_LIVE_STREAM_NAME`: an exec
    source running ffmpeg over the raw stream's loopback restream. The
    raw stream keeps its name and full quality — timelapse capture and
    the watchdog stills read it — and only the Activity's live view
    plays the transcoded one.
    """
    lines = [
        "# Generated by Print Handy (print_handy.go2rtc) from settings; do not edit.",
        "# Holds the camera source URL, which may carry credentials: keep private.",
        "api:",
        '  listen: "127.0.0.1:1984"',
        "rtsp:",
        '  listen: "127.0.0.1:8554"',
        "log:",
        "  level: info",
        "streams:",
        f"  {GO2RTC_STREAM_NAME}: {json.dumps(source_url(source))}",
    ]
    if transcode is not None:
        lines.append(f"  {GO2RTC_LIVE_STREAM_NAME}: "
                     f"{json.dumps(_transcode_source(transcode))}")
    lines.append("")
    return "\n".join(lines)


def config_path(data=None, environ=None) -> Path:
    """Where the generated config is written (GO2RTC_CONFIG overrides, for tests)."""
    override = (environ or os.environ).get("GO2RTC_CONFIG")
    if override:
        return Path(override)
    data = settings.current() if data is None else data
    return Path(data["paths"]["cache"]) / "go2rtc" / "go2rtc.yaml"


def write_config(path=None, data=None) -> Path:
    """Generate the go2rtc config from settings and write it 0600."""
    data = settings.current() if data is None else data
    source = resolve_source(data)
    if source is None:
        raise LiveStreamError("no camera source is configured for go2rtc")
    path = Path(path) if path is not None else config_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(render_config(source, transcode=transcode_plan(data)))
    os.chmod(path, 0o600)
    return path


def gate(data=None) -> int:
    """The supervisor/healthcheck decision: 0 start, 3 not started, 2 unresolved."""
    try:
        data = settings.current() if data is None else data
        warning = settings.unknown_settings_warning()
        if warning:
            print(f"go2rtc gate: WARNING {warning}", file=sys.stderr)
        needed = needed_by(data)
        if not needed:
            print("go2rtc gate: timelapse capture and live viewing are both "
                  "disabled; not starting go2rtc")
            return 3
        source = resolve_source(data)
    except settings.SettingsError as exc:
        print(f"go2rtc gate: configuration unresolved: {exc}", file=sys.stderr)
        return 2
    except LiveStreamError as exc:
        print(f"go2rtc gate: camera source unresolved: {exc}; not starting go2rtc")
        return 3
    if source is None:
        print(f"go2rtc gate: {' and '.join(needed)} enabled but no camera source "
              "is configured; not starting go2rtc")
        return 3
    print(f"go2rtc gate: {' and '.join(needed)} enabled, "
          f"source {redact_url(source_url(source))}")
    return 0


def check(url=API_URL, timeout=3) -> tuple[bool, str]:
    """Health probe for the docker healthcheck: go2rtc's API on loopback."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200, f"HTTP {response.status}"
    except Exception as exc:  # the healthcheck reports, never raises
        return False, str(exc)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--write-config"]:
        # stdout carries ONLY the written path: docker/supervise.sh captures it.
        try:
            print(write_config())
        except (settings.SettingsError, LiveStreamError) as exc:
            print(f"go2rtc: could not write config: {exc}", file=sys.stderr)
            return 2
        return 0
    if argv:
        print("usage: python3 -m print_handy.go2rtc [--write-config]", file=sys.stderr)
        return 2
    return gate()


if __name__ == "__main__":
    sys.exit(main())
