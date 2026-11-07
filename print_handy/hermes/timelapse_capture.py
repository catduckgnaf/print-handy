"""In-container timelapse frame capture (#246, Stage 2).

Stage 1 bundled go2rtc and the supervisor plumbing; this stage captures
the frames. When a print starts, the Moonraker event listener opens a
capture session for that job; while the job prints, the session polls a
JPEG snapshot of the normalised camera stream at the configured trigger
and stores the frames under the cache discipline:

    <paths.cache>/timelapse/<job>/frames/frame-000001.jpg
    <paths.cache>/timelapse/<job>/capture.json     (the manifest)

Stage 3 (:mod:`print_handy.hermes.timelapse_encode`) encodes those
frames into the timelapse MP4 for the completion post; this module
never encodes and never posts anything.

Capture mechanics (the Stage 2 evaluation in #246): snapshot polling
through go2rtc won over ffmpeg stream extraction — it reuses the existing
snapshot concepts, needs no extra moving parts, and is GPU-independent
(encoding is Stage 3). The frame source is go2rtc's loopback snapshot
endpoint when the bundled binary is present (the deployment that enables
capture runs go2rtc under the supervisor); without the binary — a
non-container install — the resolved camera source's own snapshot URL is
polled directly, which is the same frame the camera watchdog checks.

The trigger is pluggable (the owner's preference on #246 is still an open
question): ``capture_trigger`` is ``interval`` (default; a frame every
``capture_interval_s`` seconds), ``layer`` (a frame on each Moonraker
layer change, moonraker-timelapse semantics) or ``both``.

Failure posture (the #235 lesson): capture is a side feature. Every
public entry point fails soft — a dead camera, a refused connection or a
full disk is logged and the print, its announcements and its completion
post are never affected. Per-job storage is bounded by
``capture_max_frames`` / ``capture_max_bytes`` (0 disables a cap), and
``prune()`` applies ``timelapse.local_retention_days`` to finished and
abandoned capture directories.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from .. import go2rtc
from ..live_stream import redact_url
from . import _config
from .print_records import _safe_slug
from .visual_watch import frame_dark

# go2rtc serves a JPEG snapshot of the generated config's one stream
# (live_stream.GO2RTC_STREAM_NAME) on its loopback API; see
# print_handy.go2rtc.render_config. The URL itself is defined once, in
# print_handy.go2rtc, so every stills consumer reads the same endpoint.
GO2RTC_FRAME_URL = go2rtc.FRAME_URL
# One frame is a camera JPEG; the bound mirrors the listener's snapshot
# check (_camera_snapshot_bytes) so a hostile or broken source cannot
# stream unbounded data into the cache.
MAX_FRAME_BYTES = 8 * 1024 * 1024
JPEG_MAGIC = bytes((0xFF, 0xD8))

DEFAULTS = {
    "capture_trigger": "interval",
    "capture_interval_s": 5.0,
    "capture_max_frames": 3600,
    "capture_max_bytes": 512 * 1024 * 1024,
}


def capture_settings(data=None) -> dict:
    """The [timelapse] capture knobs from a settings dict, with defaults.

    Reads with .get defaults (like print_handy.go2rtc) so plain dicts —
    older configs, test fixtures — resolve exactly like validated ones.
    """
    data = _config.cfg() if data is None else data
    timelapse = data.get("timelapse", {}) if isinstance(data, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        value = timelapse.get(key, default)
        out[key] = default if value is None else value
    out["capture_interval_s"] = float(out["capture_interval_s"])
    out["capture_max_frames"] = int(out["capture_max_frames"])
    out["capture_max_bytes"] = int(out["capture_max_bytes"])
    return out


def frame_url(data=None, environ=None) -> str | None:
    """The URL one frame is fetched from, or None when no source resolves.

    The go2rtc loopback snapshot when the bundled binary is present;
    otherwise the resolved source's own snapshot URL. A source with no
    snapshot URL (an RTSP override) still names the go2rtc endpoint —
    in the deployment that enables capture go2rtc is running; if it is
    not, the fetch simply fails soft.
    """
    data = _config.cfg() if data is None else data
    source = go2rtc.resolve_source(data)
    if source is None:
        return None
    if go2rtc.binary_present(environ) or not source.snapshot_url:
        return GO2RTC_FRAME_URL
    return source.snapshot_url


def dark_outcome_text(dark_frames) -> str:
    """The named camera-dark outcome line for a completion post (#476).

    The #383 principle — a missing timelapse always says why — applied
    to a capture whose every frame was dark: the delivery paths (the
    listener's timelapse postprocess and the printer monitors' terminal
    posts) append this line instead of staying silent or attaching an
    all-black video.
    """
    return ("Timelapse not attached: the camera image was completely dark — "
            f"all {dark_frames} captured frame(s) were skipped as dark, so "
            "there was nothing to encode. Check the camera or its light.")


def fetch_frame(url, timeout=8) -> bytes:
    """One bounded JPEG snapshot. Raises on any transport or format problem;
    callers (CaptureSession) treat every raise as a soft capture failure."""
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        image = response.read(MAX_FRAME_BYTES + 1)
    if len(image) > MAX_FRAME_BYTES or not image.startswith(JPEG_MAGIC):
        raise ValueError("camera response is not a bounded JPEG snapshot")
    return image


# An MJPEG stream's per-part headers and boundary lines ride in front of
# each frame; the scan window allows one maximum-size frame plus slack
# for them, so a source that never produces a frame cannot stream
# unbounded data into memory either.
MJPEG_SCAN_SLACK = 256 * 1024
JPEG_END = bytes((0xFF, 0xD9))


def fetch_mjpeg_frame(url, timeout=8) -> bytes:
    """The first complete JPEG frame from an MJPEG (multipart) stream.

    For camera sources that serve only a stream — the stock Flashforge
    ``cameraStreamUrl`` is one (``?action=stream``, no documented snapshot
    route). Reads until the first frame's end marker, bounded like
    fetch_frame; raises on any transport or format problem, and
    CaptureSession treats every raise as a soft capture failure.
    """
    request = urllib.request.Request(url)
    buffer = bytearray()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        while len(buffer) <= MAX_FRAME_BYTES + MJPEG_SCAN_SLACK:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            buffer += chunk
            start = buffer.find(JPEG_MAGIC)
            if start < 0:
                # No frame start yet: keep only a possible split marker.
                del buffer[:-1]
                continue
            end = buffer.find(JPEG_END, start + 2)
            if end >= 0:
                frame = bytes(buffer[start:end + 2])
                if len(frame) > MAX_FRAME_BYTES:
                    raise ValueError("MJPEG frame exceeds the bounded snapshot size")
                return frame
            if start:
                del buffer[:start]
    raise ValueError("MJPEG stream produced no complete JPEG frame")


class IntervalTrigger:
    """A frame every interval seconds. The first frame is due immediately."""

    name = "interval"

    def __init__(self, interval_s):
        self.interval_s = float(interval_s)
        self._last = None

    def due(self, now, layer_changed) -> bool:
        return self._last is None or now - self._last >= self.interval_s

    def note_captured(self, now):
        self._last = now


class LayerTrigger:
    """A frame on each layer change (and on the first layer seen)."""

    name = "layer"

    def __init__(self, interval_s=None):
        pass

    def due(self, now, layer_changed) -> bool:
        return bool(layer_changed)

    def note_captured(self, now):
        pass


class BothTrigger:
    """Either the interval floor or a layer change makes a frame due."""

    name = "both"

    def __init__(self, interval_s):
        self._interval = IntervalTrigger(interval_s)

    def due(self, now, layer_changed) -> bool:
        return bool(layer_changed) or self._interval.due(now, layer_changed)

    def note_captured(self, now):
        self._interval.note_captured(now)


_TRIGGERS = {"interval": IntervalTrigger, "layer": LayerTrigger, "both": BothTrigger}


def make_trigger(name, interval_s):
    """A trigger object for a capture_trigger setting value.

    Unknown names fall back to the interval trigger: settings validation
    refuses them at load, and a capture that falls back to the default
    trigger is better than one that silently never fires.
    """
    return _TRIGGERS.get(str(name or "interval").strip().lower(), IntervalTrigger)(interval_s)


def capture_root(data=None) -> Path:
    """Where capture sessions live: <paths.cache>/timelapse."""
    data = _config.cfg() if data is None else data
    return Path(data["paths"]["cache"]) / "timelapse"


def job_dir(root, job_key) -> Path:
    return Path(root) / _safe_slug(str(job_key or ""), "print")


#: Most reprint suffixes tried before the key is made unique by a timestamp.
#: The search is bounded so no input can keep begin() looping.
MAX_REPRINT_SUFFIXES = 50
_SUFFIX_ROOM = 12  # characters kept free for "-<n>" inside the 100 character slug


def _has_frames(root, key, filename) -> bool:
    frames = job_dir(root, key or filename) / "frames"
    try:
        return frames.is_dir() and any(frames.iterdir())
    except OSError:
        return False


def unused_job_key(root, job_key, filename="") -> str:
    """A job key whose capture directory holds no frames yet.

    A reprint of the same file inside the identity's resolution gets a fresh
    directory so two jobs' frames never mix. The directory name is a 100
    character slug (``_safe_slug``), so the suffix must fit INSIDE it: the
    base is cut to leave room first. Appending to the full key lost the
    suffix to the cut, handed back the same directory forever and hung the
    listener (#1020). The search is bounded and ends with a timestamp key.
    """
    base = job_key or filename
    if not _has_frames(root, base, filename):
        return job_key
    short = _safe_slug(str(base), "print")[:100 - _SUFFIX_ROOM].rstrip(".-")
    for suffix in range(2, MAX_REPRINT_SUFFIXES + 2):
        candidate = f"{short}-{suffix}"
        if not _has_frames(root, candidate, filename):
            return candidate
    return f"{short}-{int(time.time())}"


class CaptureSession:
    """Frames for one job. Every method fails soft: nothing here raises."""

    def __init__(self, root, job_key, filename="", *, trigger, max_frames=0,
                 max_bytes=0, url=None, fetch=None, log=None, now=None, printer_id=""):
        self.job_key = str(job_key or "")
        self.filename = str(filename or "")
        self.printer_id = str(printer_id or "")
        self.directory = job_dir(root, self.job_key or self.filename)
        self.frames_dir = self.directory / "frames"
        self.manifest_path = self.directory / "capture.json"
        self.trigger = trigger
        self.max_frames = int(max_frames or 0)
        self.max_bytes = int(max_bytes or 0)
        self.url = url
        self._fetch = fetch or fetch_frame
        self._log = log or (lambda message: None)
        self._now = now or time.time
        self.state = "new"
        self.capped = False
        self.failures = 0
        self.dark_frames = 0
        self._frames = 0
        self._bytes = 0
        self._last_layer = None
        self._started_at = None
        self._finished_at = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> bool:
        """Create the job directory and manifest. False when that failed."""
        if self.state != "new":
            return self.state == "capturing"
        try:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            self._started_at = self._now()
            self.state = "capturing"
            self._write_manifest()
        except Exception as exc:  # capture must never break the print flow
            self.state = "failed"
            self._say(f"timelapse capture could not start for {self.filename!r}: {exc!r}")
            return False
        self._say(f"timelapse capture started for {self.filename!r} "
                  f"(trigger={getattr(self.trigger, 'name', '?')}, dir={self.directory})")
        return True

    def stop(self, state="complete") -> dict | None:
        """End the session and stamp the manifest. Frames stay for the Stage 3 encode."""
        if self.state == "capturing":
            self.state = str(state or "complete")
            self._finished_at = self._now()
            try:
                self._write_manifest()
            except Exception as exc:
                self._say(f"timelapse capture manifest write failed for {self.filename!r}: {exc!r}")
            self._say(f"timelapse capture stopped for {self.filename!r}: "
                      f"{self._frames} frame(s) kept, {self.dark_frames} dark "
                      f"frame(s) skipped, state={self.state}")
        return self.manifest()

    @property
    def active(self) -> bool:
        return self.state == "capturing"

    @property
    def frame_count(self) -> int:
        return self._frames

    # -- capture ---------------------------------------------------------

    def due(self, layer=None, now=None) -> bool:
        """True when the trigger wants a frame now. Pure: no I/O, no fetch."""
        if not self.active or self.capped:
            return False
        now = self._now() if now is None else now
        return bool(self.trigger.due(now, self._layer_changed(layer)))

    def tick(self, layer=None, now=None) -> bool:
        """Capture one frame when due. True when a frame was stored."""
        now = self._now() if now is None else now
        changed = self._layer_changed(layer)
        if layer is not None:
            self._last_layer = layer
        if not self.active or self.capped or not self.trigger.due(now, changed):
            return False
        return self.capture_once(now=now)

    def capture_once(self, now=None) -> bool:
        """Fetch and store one frame, enforcing the caps. Never raises."""
        if not self.active:
            return False
        now = self._now() if now is None else now
        if self.max_frames and self._frames >= self.max_frames:
            return self._cap(f"frame cap ({self.max_frames})")
        try:
            image = self._fetch(self.url)
        except Exception as exc:  # a dead camera costs frames, never the print
            self.failures += 1
            # Count the attempt for interval purposes: a dead camera is
            # retried at the capture interval, not on every status event.
            self.trigger.note_captured(now)
            self._say(f"timelapse frame fetch failed for {self.filename!r} "
                      f"from {redact_url(self.url or '')}: {exc!r}")
            return False
        if not self.active:
            # stop() ran while the fetch was in flight (2.2.5 review, P2-133):
            # the manifest is already stamped, so a late frame would disagree
            # with its frame count and land in the directory the encoder reads.
            return False
        if frame_dark(image):
            # #476: a completely dark frame is a camera problem, not
            # footage — never stored, so a dark spell can never become
            # an all-black timelapse. Skipped frames are counted on the
            # session/manifest; a session that ends with only dark
            # frames is the named camera-dark outcome (timelapse_encode
            # .dark_capture), not a silent empty capture.
            self.dark_frames += 1
            self.trigger.note_captured(now)
            self._say(f"timelapse frame skipped for {self.filename!r}: camera frame "
                      f"completely dark ({self.dark_frames} dark frame(s) so far, "
                      f"{self._frames} kept)")
            try:
                self._write_manifest()
            except Exception:
                pass
            return False
        if self.max_bytes and self._bytes + len(image) > self.max_bytes:
            return self._cap(f"byte cap ({self.max_bytes})")
        try:
            self.frames_dir.mkdir(parents=True, exist_ok=True)
            target = self.frames_dir / f"frame-{self._frames + 1:06d}.jpg"
            partial = target.with_name(f".{target.name}.partial")
            partial.write_bytes(image)
            os.replace(partial, target)
        except Exception as exc:
            self.failures += 1
            self._say(f"timelapse frame write failed for {self.filename!r}: {exc!r}")
            return False
        self._frames += 1
        self._bytes += len(image)
        self.trigger.note_captured(now)
        try:
            self._write_manifest()
        except Exception as exc:  # the frame is stored; the manifest is bookkeeping
            self._say(f"timelapse capture manifest write failed for {self.filename!r}: {exc!r}")
        return True

    def _cap(self, why) -> bool:
        self.capped = True
        self._say(f"timelapse capture reached the {why} for {self.filename!r}; "
                  f"keeping the {self._frames} frame(s) already taken")
        try:
            self._write_manifest()
        except Exception:
            pass
        return False

    def _layer_changed(self, layer) -> bool:
        return layer is not None and self._last_layer != layer

    # -- manifest --------------------------------------------------------

    def manifest(self) -> dict:
        return {
            "job_key": self.job_key,
            "filename": self.filename,
            "state": self.state,
            "capped": self.capped,
            "trigger": getattr(self.trigger, "name", "interval"),
            "source": redact_url(self.url or ""),
            "printer_id": self.printer_id,
            "frames": self._frames,
            "bytes": self._bytes,
            "failures": self.failures,
            "dark_frames": self.dark_frames,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
        }

    def _write_manifest(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        partial = self.manifest_path.with_name(f".{self.manifest_path.name}.partial")
        partial.write_text(json.dumps(self.manifest(), sort_keys=True) + "\n", encoding="utf-8")
        os.replace(partial, self.manifest_path)

    def _say(self, message):
        try:
            self._log(message)
        except Exception:
            pass


def begin(job_key, filename="", *, data=None, fetch=None, log=None, environ=None,
          url=None) -> CaptureSession | None:
    """Open a capture session for a job, or None when capture cannot run.

    None — never an exception — when capture is disabled, when no camera
    source resolves, or when the session cannot be created: the caller
    (the listener) treats every one of those as "no capture for this job"
    and carries on exactly as before.

    ``url`` names an explicit frame source instead of the settings-derived
    one: a caller whose camera source is per-printer state rather than a
    setting (the stock Flashforge monitor reads the stream URL the printer
    advertises in /detail, #373) passes it together with a matching
    ``fetch``. The capture gate, the knobs, the storage discipline
    and the pruning are unchanged either way.

    The gate is go2rtc.capture_active — [timelapse] mode over
    capture_enabled (2.2.2) — and camera_source "none" refuses a session
    even for an explicit ``url``: no camera images means no capture.
    """
    data = _config.cfg() if data is None else data
    try:
        if not go2rtc.capture_active(data):
            return None
        if go2rtc.camera_source(data) == "none":
            return None
        url = url or frame_url(data, environ)
        if not url:
            return None
        knobs = capture_settings(data)
        root = capture_root(data)
        # A repeated job key (the same file reprinted inside the identity's
        # resolution) gets a fresh directory: two jobs' frames never mix.
        key = unused_job_key(root, str(job_key or ""), filename)
        session = CaptureSession(
            root, key, filename,
            trigger=make_trigger(knobs["capture_trigger"], knobs["capture_interval_s"]),
            max_frames=knobs["capture_max_frames"], max_bytes=knobs["capture_max_bytes"],
            url=url, fetch=fetch, log=log, printer_id=printer_identity(data))
        if not session.start():
            return None
        # A new print starting is the natural sweep point: drop finished
        # and abandoned captures past the local retention window.
        try:
            removed = prune(root, data)
            if removed and log is not None:
                log(f"timelapse capture pruned {len(removed)} expired capture(s): "
                    f"{', '.join(removed)}")
        except Exception:
            pass
        return session
    except Exception as exc:  # the listener's print flow must never see this
        if log is not None:
            try:
                log(f"timelapse capture unavailable for {filename!r}: {exc!r}")
            except Exception:
                pass
        return None


def printer_identity(data=None) -> str:
    """The roster id of the printer this process captures for, or "".

    The capture root is shared by every printer (#787, DATA-5), so each
    manifest records whose capture it is: the listener process's own
    printer, else the default printer for the single-printer layout.
    """
    try:
        from print_handy import instance, settings

        return instance.printer_id() or settings.default_printer_id(
            _config.cfg() if data is None else data)
    except Exception:
        return ""


def read_manifest(directory) -> dict | None:
    try:
        data = json.loads((Path(directory) / "capture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prune(root=None, data=None, now=None) -> list[str]:
    """Drop capture directories past timelapse.local_retention_days.

    A directory is past retention when its manifest's finish time (or,
    for a capture abandoned mid-print — listener gone, container
    restarted — its manifest's last update) is older than the retention
    window. A capture still marked "capturing" with a fresh manifest is
    a live print and is never touched. Returns the removed job slugs.
    """
    import shutil

    data = _config.cfg() if data is None else data
    days = float(data.get("timelapse", {}).get("local_retention_days", 1))
    root = capture_root(data) if root is None else Path(root)
    if days <= 0 or not root.is_dir():
        return []
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    removed = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        manifest = read_manifest(directory)
        try:
            stamp = directory.stat().st_mtime
        except OSError:
            continue
        if manifest:
            stamp = manifest.get("finished_at") or stamp
            if manifest.get("state") == "capturing" and stamp >= cutoff:
                continue  # a live capture, however old the print is
        if stamp < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                removed.append(directory.name)
    return removed
