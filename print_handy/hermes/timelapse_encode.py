"""Encode captured timelapse frames and deliver them (#246, Stage 3).

Stage 2 (:mod:`print_handy.hermes.timelapse_capture`) stores JPEG frames
per job and never encodes. This module is the other half: it assembles a
finished capture's frames into one H.264 MP4 that fits the platform
upload cap, so the listener's completion post can carry the
in-container timelapse. The fallback order for a completion post's
video is: this captured video when capture produced one, then the
printer-rendered moonraker-timelapse download, then no video — the
printer-rendered path is untouched.

Encoder selection is shared, as the issue requires:

- *Which* encoders are candidates, in which order, comes from
  :mod:`print_handy.live_stream`'s codec-aware plan (#311): for H.264,
  VAAPI → NVENC → QSV → CPU (``libx264``), with the
  ``[timelapse] capture_encoder`` preference moving its vendor first.
  Availability is a probe of the real ffmpeg (``ffmpeg -encoders``)
  plus device presence (``/dev/dri`` for VAAPI/QSV, ``/dev/nvidia*``
  for NVENC).
- *How* one encoder is spelled on the ffmpeg command line comes from
  :mod:`print_handy.hermes.vertical_short`'s encoder seam — the same
  seam the Discord sizing shrink (#314) and the plan combine use, so
  there is exactly one place that knows each vendor's argv.

The sizing strategy mirrors the shrink's: a budget-derived bitrate
(90% of the cap over the video's duration), then a second attempt at
70%. A hardware encoder that fails — or produces an oversize file on
both attempts — falls through to the next candidate; CPU is always the
last candidate, and a total failure returns None. Nothing here raises
into the listener: a failed encode degrades the completion post to the
printer-rendered video or to text, never to a failed announcement (the
#235 posture). GPU paths are software-verified only: no VAAPI/NVENC/QSV
encode in this module has been run on real hardware (docs/DOCKER.md
says so too).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from .. import live_stream
from . import _config, timelapse_capture, vertical_short

# Captured frames play back at the same rate the plan combine normalises
# to, so a captured clip and a printer-rendered clip combine cleanly.
PLAYBACK_FPS = 30
# The encoder names a probe can report; anything else in `ffmpeg
# -encoders` output is not an H.264 candidate for this pipeline.
_H264_ENCODERS = ("h264_vaapi", "h264_nvenc", "h264_qsv", "libx264")


def probe_ffmpeg(ffmpeg, *, run=None, dri_present=None, nvidia_present=None) -> live_stream.EncoderProbe:
    """Probe the real ffmpeg for H.264 encoders and GPU device presence.

    Every input is injectable for tests. A probe that cannot run yields
    an empty encoder set — candidate selection then still offers CPU,
    and the encode itself reports the real failure if ffmpeg is broken.
    """
    run = run or subprocess.run
    encoders = set()
    try:
        out = run([str(ffmpeg), "-hide_banner", "-encoders"],
                  capture_output=True, text=True, timeout=60)
        text = out.stdout or ""
        encoders = {name for name in _H264_ENCODERS if name in text}
    except Exception:
        encoders = set()
    if dri_present is None:
        dri_present = Path("/dev/dri").is_dir()
    if nvidia_present is None:
        nvidia_present = any(Path("/dev").glob("nvidia[0-9]*"))
    return live_stream.EncoderProbe(encoders=frozenset(encoders),
                                    dri_present=bool(dri_present),
                                    nvidia_present=bool(nvidia_present))


def candidates(preference="auto", probe=None, *, ffmpeg=None, run=None) -> tuple:
    """Ordered encoder candidates for one encode, CPU guaranteed last.

    `preference` is a ``[timelapse] capture_encoder`` value (``auto`` |
    ``vaapi`` | ``nvenc`` | ``qsv`` | ``cpu``); an unknown value behaves
    as ``auto``. The plan comes from live_stream; the CPU candidate is
    appended when the probe did not report libx264, because the shipped
    image always carries it and a stale probe must not remove the
    fallback that can still save the timelapse.
    """
    if probe is None:
        probe = probe_ffmpeg(ffmpeg or _ffmpeg(), run=run)
    preference = str(preference or "auto").strip().lower()
    if preference not in ("auto", "vaapi", "nvenc", "qsv", "cpu"):
        preference = "auto"
    ordered = list(live_stream.encoder_candidates(probe, preference=preference,
                                                  target_codec="h264"))
    if not any(c.vendor == "cpu" for c in ordered):
        ordered.append(live_stream.EncoderCandidate(encoder="libx264", vendor="cpu", codec="h264"))
    return tuple(ordered)


def capture_preference(data=None) -> str:
    """The [timelapse] capture_encoder preference, 'auto' when unset/invalid."""
    data = _config.cfg() if data is None else data
    timelapse = data.get("timelapse", {}) if isinstance(data, dict) else {}
    value = str(timelapse.get("capture_encoder") or "auto").strip().lower()
    return value if value in ("auto", "vaapi", "nvenc", "qsv", "cpu") else "auto"


def unique_partial(target) -> Path:
    """A fresh, private temp output path beside target (same filesystem for os.replace).

    A fixed ``.<name>.partial.mp4`` let two overlapping renders of one job
    open the same inode: the first finished and os.replace()d it into
    place while the second kept writing (and finally rewrote the moov
    for +faststart) into the now-final file, leaving a video with no
    streams (Print #64). Each writer gets its own file instead, so a
    losing writer can only ever corrupt its own temp.
    """
    target = Path(target)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial.mp4", dir=target.parent)
    os.close(fd)
    os.chmod(name, 0o644)
    return Path(name)


def validate_video(path, *, ffmpeg=None, run=None, min_frames=1) -> bool:
    """True only when path fully decodes to at least min_frames video frames.

    Probes for a video stream, then decodes the whole file to a null sink
    with ffmpeg's error level on; any decode error, an empty stream list
    or a short frame count is invalid. Never raises.
    """
    run = run or subprocess.run
    ffmpeg = str(ffmpeg or _ffmpeg())
    ffprobe = str(Path(ffmpeg).with_name("ffprobe")) if "/" in ffmpeg else "ffprobe"
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        probed = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                      "-show_entries", "stream=codec_name,width,height,nb_read_frames",
                      "-of", "json", str(path)],
                     capture_output=True, text=True, timeout=300, check=True)
        streams = json.loads(probed.stdout or "{}").get("streams") or []
        if not streams or int(streams[0].get("width") or 0) <= 0:
            return False
        if int(streams[0].get("nb_read_frames") or 0) < min_frames:
            return False
        decoded = run([ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                      capture_output=True, text=True, timeout=600)
        return decoded.returncode == 0 and not (decoded.stderr or "").strip()
    except Exception:
        return False


_RENDER_LOCKS: dict = {}
_RENDER_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def render_lock(target):
    """Serialise renders of one output file across threads AND processes.

    Waiters block, then find the winner's finished video and reuse it.
    """
    key = str(Path(target).resolve())
    with _RENDER_LOCKS_GUARD:
        thread_lock = _RENDER_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        handle = None
        try:
            try:
                handle = open(Path(target).with_name(f".{Path(target).name}.lock"), "a")
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                handle = None  # in-process lock still holds; never fail the render over a lock file
            yield
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()


def _ffmpeg():
    name = _config.ffmpeg_bin()
    import shutil
    return name if "/" in name else (shutil.which(name) or name)


def _encode_frames_once(frames_dir, partial, kbps, vendor, *, fps, ffmpeg, run):
    """One ffmpeg assembly of a frames directory at kbps with one vendor's codec."""
    run([str(ffmpeg), "-y", "-v", "error", *vertical_short.encoder_input_args(vendor),
         "-framerate", str(fps), "-i", str(Path(frames_dir) / "frame-%06d.jpg"),
         "-an", "-vf", vertical_short.encoder_filter(vendor, "scale='min(1280,iw)':-2"),
         *vertical_short.encoder_args(vendor),
         "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
         "-movflags", "+faststart", str(partial)],
        capture_output=True, timeout=1800, check=True)


def assemble_frames(frames_dir, target, *, max_bytes, preference="auto", fps=PLAYBACK_FPS,
                    probe=None, ffmpeg=None, run=None, log=None, validate=None) -> Path | None:
    """Assemble frames into target under max_bytes, or None on total failure.

    Candidates run in selection order; each gets the two-attempt
    bitrate strategy (budget, then 70%). The first candidate whose
    output fits wins. Never raises: every failure is logged (when a log
    callable is given) and the next candidate — ultimately CPU — is
    tried, exactly the issue's "a missing GPU must never fail a
    timelapse" rule.
    """
    log = log or (lambda message: None)
    run = run or subprocess.run
    ffmpeg = ffmpeg or _ffmpeg()
    frames_dir, target = Path(frames_dir), Path(target)
    frames = sorted(frames_dir.glob("frame-*.jpg")) if frames_dir.is_dir() else []
    if not frames:
        return None
    duration = max(1.0, len(frames) / float(fps or PLAYBACK_FPS))
    kbps = max(200, int(max_bytes * 8 * 0.90 / duration / 1000))
    if validate is None:
        # Injected fake runners write placeholder bytes; only the real
        # ffmpeg path can be held to a real decode.
        validate = (lambda path: validate_video(path, ffmpeg=ffmpeg, min_frames=len(frames))) \
            if run is subprocess.run else (lambda path: True)
    partial = unique_partial(target)
    try:
        return _assemble(frames_dir, target, partial, frames, kbps, max_bytes, preference, fps,
                         probe, ffmpeg, run, log, validate)
    finally:
        partial.unlink(missing_ok=True)


def _assemble(frames_dir, target, partial, frames, kbps, max_bytes, preference, fps,
              probe, ffmpeg, run, log, validate):
    for candidate in candidates(preference, probe, ffmpeg=ffmpeg, run=run):
        for attempt_kbps in (kbps, int(kbps * 0.7)):
            try:
                _encode_frames_once(frames_dir, partial, attempt_kbps, candidate.vendor,
                                    fps=fps, ffmpeg=ffmpeg, run=run)
            except Exception as exc:
                log(f"timelapse encode with {candidate.encoder} failed: {exc!r}")
                break  # this encoder is done; fall through to the next candidate
            if partial.is_file() and 0 < partial.stat().st_size <= max_bytes:
                if not validate(partial):
                    log(f"timelapse encode with {candidate.encoder} produced an undecodable "
                        f"video; discarding it")
                    break
                os.replace(partial, target)
                log(f"timelapse assembled {len(frames)} frame(s) -> {target} "
                    f"({target.stat().st_size} bytes, {candidate.encoder} at {attempt_kbps}k)")
                return target
            partial.unlink(missing_ok=True)
        else:
            continue
    log(f"timelapse assembly failed for {frames_dir}: no encoder produced a video "
        f"under {max_bytes} bytes")
    return None


def find_capture(job_key, *, data=None, root=None) -> Path | None:
    """The finished capture directory for a job, or None.

    Capture sessions are keyed by the listener's notification identity
    while the completion postprocess knows the announcer's job key (the
    print's file path), so the directory is found by evidence, not by
    assuming the keys match: first the manifest in the directory the
    job key slugs to (its recorded job_key must equal the key), then a
    scan for the newest finished manifest whose recorded filename names
    the same file. Live ("capturing") sessions and empty captures never
    match — their job is still printing or produced nothing to encode.
    """
    data = _config.cfg() if data is None else data
    root = timelapse_capture.capture_root(data) if root is None else Path(root)
    if not root.is_dir():
        return None
    key = str(job_key or "")

    def usable(directory) -> dict | None:
        manifest = timelapse_capture.read_manifest(directory)
        if not manifest or manifest.get("state") in (None, "new", "capturing"):
            return None
        if not int(manifest.get("frames") or 0):
            return None
        frames_dir = Path(directory) / "frames"
        if not frames_dir.is_dir() or not any(frames_dir.glob("frame-*.jpg")):
            return None
        return manifest

    direct = timelapse_capture.job_dir(root, key)
    manifest = usable(direct)
    if manifest and manifest.get("job_key") == key:
        return direct
    wanted = Path(key).name
    best, best_stamp = None, None
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory == direct:
            continue
        manifest = usable(directory)
        if not manifest:
            continue
        if Path(str(manifest.get("filename") or "")).name != wanted:
            continue
        stamp = manifest.get("finished_at") or 0
        if best is None or stamp >= (best_stamp or 0):
            best, best_stamp = directory, stamp
    return best


def dark_capture(job_key, *, data=None, root=None) -> dict | None:
    """The manifest of a finished capture that kept NO frames because every
    frame was completely dark (#476), or None.

    The named camera-dark outcome beside find_capture: matches a job the
    same way (direct directory, then the newest finished manifest naming
    the same file), but a dark capture is exactly what find_capture must
    never hand to the encoder — zero kept frames and at least one frame
    skipped as dark. The delivery path uses the returned manifest (its
    dark_frames count) to name the outcome in the completion post.
    """
    data = _config.cfg() if data is None else data
    root = timelapse_capture.capture_root(data) if root is None else Path(root)
    if not root.is_dir():
        return None
    key = str(job_key or "")

    def dark_manifest(directory) -> dict | None:
        manifest = timelapse_capture.read_manifest(directory)
        if not manifest or manifest.get("state") in (None, "new", "capturing"):
            return None
        if int(manifest.get("frames") or 0):
            return None
        if not int(manifest.get("dark_frames") or 0):
            return None
        return manifest

    direct = timelapse_capture.job_dir(root, key)
    manifest = dark_manifest(direct)
    if manifest and manifest.get("job_key") == key:
        return manifest
    wanted = Path(key).name
    best, best_stamp = None, None
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory == direct:
            continue
        manifest = dark_manifest(directory)
        if not manifest:
            continue
        if Path(str(manifest.get("filename") or "")).name != wanted:
            continue
        stamp = manifest.get("finished_at") or 0
        if best is None or stamp >= (best_stamp or 0):
            best, best_stamp = manifest, stamp
    return best


def captured_video(job_key, *, data=None, max_bytes, log=None, probe=None,
                   ffmpeg=None, run=None) -> Path | None:
    """The assembled in-container timelapse for a job, or None. Never raises.

    None — the caller's cue to use the printer-rendered path — when
    capture was off, no finished capture matches the job, it holds no
    frames, or every encoder failed. An already-assembled video that
    still fits is reused, so a retried postprocess does not re-encode.

    The gate is go2rtc.capture_active ([timelapse] mode over
    capture_enabled, 2.2.2), the same gate sessions open under, so a
    mode change cannot strand or resurrect a capture.
    """
    log = log or (lambda message: None)
    try:
        data = _config.cfg() if data is None else data
        from .. import go2rtc
        if not go2rtc.capture_active(data):
            return None
        directory = find_capture(job_key, data=data)
        if directory is None:
            return None
        target = directory / "timelapse.mp4"
        real = run is None
        count = len(list((directory / "frames").glob("frame-*.jpg")))

        def reusable() -> bool:
            if not (target.is_file() and 0 < target.stat().st_size <= max_bytes):
                return False
            # A cached video is only reused when it still fully decodes:
            # an earlier colliding render may have left a stream-less file.
            return validate_video(target, ffmpeg=ffmpeg, min_frames=count) if real else True

        if reusable():
            return target
        with render_lock(target):
            if reusable():  # a concurrent render finished while we waited
                return target
            if target.exists():
                log(f"cached timelapse {target} failed validation; re-rendering")
                target.unlink(missing_ok=True)
            return assemble_frames(directory / "frames", target, max_bytes=max_bytes,
                                   preference=capture_preference(data), probe=probe,
                                   ffmpeg=ffmpeg, run=run, log=log)
    except Exception as exc:  # the completion post must never see this fail
        log(f"captured timelapse unavailable for {job_key!r}: {exc!r}")
        return None
