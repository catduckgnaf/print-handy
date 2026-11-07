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
#235 posture). Every move off the GPU is logged with a named reason
(#455): :func:`selection_note` when the plan does not start on the GPU
(device missing, render node not accessible, encoder not built), and
:func:`ffmpeg_failure`'s exit code plus last stderr line when a GPU
encode fails. GPU paths are software-verified only: no VAAPI/NVENC/QSV
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
# GPU vendor -> its H.264 encoder, in the default candidate order (#455).
GPU_ENCODERS = (("vaapi", "h264_vaapi"), ("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"))
DRI_DIR = Path("/dev/dri")


def dri_status(dri_dir=None, *, access=None) -> tuple[bool, str]:
    """(usable, note) for the Intel/AMD render device (#455).

    Usable only when a ``renderD*`` node exists AND this process can read
    and write it: a /dev/dri mapped without the device's group (the usual
    Unraid/Compose mistake) is named as such instead of being tried and
    failing inside ffmpeg. Never raises.
    """
    dri_dir = Path(dri_dir) if dri_dir is not None else DRI_DIR
    access = access or os.access
    try:
        if not dri_dir.is_dir():
            return False, f"no {dri_dir} in the container (the GPU is not passed through)"
        nodes = sorted(dri_dir.glob("renderD*"))
        if not nodes:
            return False, f"{dri_dir} has no renderD* render node"
        for node in nodes:
            if access(str(node), os.R_OK | os.W_OK):
                return True, f"{node} is usable"
        uid = os.getuid() if hasattr(os, "getuid") else "?"
        return False, (f"{nodes[0]} is not readable and writable by uid {uid} "
                       f"(add the device's group: group_add / --group-add)")
    except Exception as exc:  # noqa: BLE001 - a status probe must never fail an encode
        return False, f"{dri_dir} could not be checked: {exc}"


def nvidia_status(dev_dir=None) -> tuple[bool, str]:
    """(present, note) for an NVIDIA device granted by the NVIDIA container runtime. Never raises."""
    dev_dir = Path(dev_dir) if dev_dir is not None else Path("/dev")
    try:
        nodes = sorted(dev_dir.glob("nvidia[0-9]*"))
    except Exception:  # noqa: BLE001
        nodes = []
    if nodes:
        return True, f"{nodes[0]} is present"
    return False, ("no /dev/nvidia* device (the NVIDIA runtime is not granted: "
                   "--runtime=nvidia with NVIDIA_VISIBLE_DEVICES)")


def gpu_notes(probe, *, dri_note=None, nvidia_note=None) -> tuple:
    """One named reason per GPU encoder that is not a candidate under ``probe``.

    ``dri_note`` / ``nvidia_note`` override the device explanations (tests);
    otherwise they come from :func:`dri_status` / :func:`nvidia_status`,
    read only when the probe says the device is missing.
    """
    encoders, dri, nvidia = live_stream._coerce_probe(probe, None, None)
    notes = []
    for vendor, encoder in GPU_ENCODERS:
        if encoder not in encoders:
            notes.append(f"{vendor}: {encoder} is not in this ffmpeg build")
        elif vendor in ("vaapi", "qsv") and not dri:
            if dri_note is None:
                dri_note = dri_status()[1]
            notes.append(f"{vendor}: {dri_note}")
        elif vendor == "nvenc" and not nvidia:
            if nvidia_note is None:
                nvidia_note = nvidia_status()[1]
            notes.append(f"{vendor}: {nvidia_note}")
    return tuple(notes)


def ffmpeg_failure(exc) -> str:
    """A short named reason for a failed ffmpeg run: exit code plus the last stderr line.

    ``{exc!r}`` of a CalledProcessError carries the argv but not what
    ffmpeg said, so a missing driver (``Cannot load libnvidia-encode.so.1``,
    ``Failed to initialise VAAPI connection``) was invisible in the log.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        lines = [line.strip() for line in str(stderr).splitlines() if line.strip()]
        detail = f": {lines[-1][:300]}" if lines else ""
        return f"ffmpeg exited {exc.returncode}{detail}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"ffmpeg timed out after {exc.timeout}s"
    if isinstance(exc, FileNotFoundError):
        return f"ffmpeg not found ({exc.filename or exc})"
    return repr(exc)


def probe_ffmpeg(ffmpeg, *, run=None, dri_present=None, nvidia_present=None) -> live_stream.EncoderProbe:
    """Probe the real ffmpeg for H.264 encoders and GPU device presence.

    Every input is injectable for tests. A probe that cannot run yields
    an empty encoder set — candidate selection then still offers CPU,
    and the encode itself reports the real failure if ffmpeg is broken.
    The Intel/AMD device counts only when a render node is accessible
    (:func:`dri_status`), not merely when /dev/dri exists.
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
        dri_present = dri_status()[0]
    if nvidia_present is None:
        nvidia_present = nvidia_status()[0]
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


# -- bounded playback length (#906) ---------------------------------------------------------------
# Frames per second is chosen from the frame count and a target length, clamped to
# [min_fps, max_fps]. When the clamp still leaves the video longer than the target the frames are
# decimated (evenly spaced); when it leaves it shorter than min_length_s they are held (each shown
# for several output frames). PLAYBACK_FPS stays the fallback when no valid settings are given.
PLAYBACK_DEFAULTS = {"target_length_s": 20.0, "min_fps": 10.0, "max_fps": 60.0,
                     "min_length_s": 5.0, "hold_last_frame_s": 1.0}


def playback_settings(data=None):
    """The [timelapse] playback knobs as numbers, or None when they are unusable (fallback)."""
    try:
        data = _config.cfg() if data is None else data
        timelapse = data.get("timelapse", {}) if isinstance(data, dict) else {}
        out = {}
        for key, default in PLAYBACK_DEFAULTS.items():
            value = timelapse.get(key, default)
            out[key] = float(default if value is None else value)
        if out["target_length_s"] <= 0 or out["min_fps"] <= 0 or out["max_fps"] < out["min_fps"] \
                or out["min_length_s"] < 0 or out["hold_last_frame_s"] < 0:
            return None
        return out
    except Exception:
        return None


def plan_playback(frame_count, settings=None):
    """(fps, indices) for frame_count captured frames; indices pick frames in output order.

    Duplicated indices are holds; a shorter list than frame_count is a decimation. With no valid
    settings the plan is the legacy one: every frame once at PLAYBACK_FPS.
    """
    n = int(frame_count)
    if n <= 0:
        return PLAYBACK_FPS, []
    if not settings:
        return PLAYBACK_FPS, list(range(n))
    target = float(settings["target_length_s"])
    fps = min(max(n / target, settings["min_fps"]), settings["max_fps"])
    fps_int = max(1, int(round(fps)))
    out_frames = n
    if n / fps_int > target:  # still too long at the clamp: decimate
        out_frames = max(1, int(target * fps_int))
    elif n / fps_int < settings["min_length_s"]:  # too short at the clamp: hold
        out_frames = max(n, int(round(settings["min_length_s"] * fps_int)))
    if out_frames == n:
        indices = list(range(n))
    elif out_frames < n:  # decimate across both endpoints so the last captured frame is kept
        indices = [0] if out_frames == 1 else [i * (n - 1) // (out_frames - 1) for i in range(out_frames)]
        indices[-1] = n - 1
    else:  # hold: spread the extra output frames evenly
        indices = [min(n - 1, i * n // out_frames) for i in range(out_frames)]
    indices += [indices[-1]] * int(round(settings["hold_last_frame_s"] * fps_int))
    return fps_int, indices


def expected_playback_frames(count, data=None) -> int:
    """Frames the assembled video holds for ``count`` captured frames (at least 1)."""
    return max(1, len(plan_playback(count, playback_settings(data))[1]))


@contextlib.contextmanager
def playback_frames(frames_dir, settings=None):
    """Yield (directory, fps, count) of the frames to encode, per plan_playback.

    An identity plan yields the capture directory itself (no copy); otherwise a temporary
    directory of symlinks named frame-%06d.jpg in playback order.
    """
    frames = sorted(Path(frames_dir).glob("frame-*.jpg"))
    fps, indices = plan_playback(len(frames), settings)
    if indices == list(range(len(frames))):
        yield Path(frames_dir), fps, len(frames)
        return
    with tempfile.TemporaryDirectory(prefix="playback-", dir=str(Path(frames_dir).parent)) as tmp:
        for number, index in enumerate(indices, start=1):
            os.symlink(frames[index].resolve(), Path(tmp) / f"frame-{number:06d}.jpg")
        yield Path(tmp), fps, len(indices)


def _record_for(job_key, filename=None) -> dict:
    """The print record naming this job: finished by job key first, then by gcode file."""
    from . import print_records
    record = print_records.latest_completed_record(job_key) if job_key else {}
    if not record and (filename or job_key):
        record = print_records.latest_record_for_file(filename or job_key)
    return record or {}


def delivery_name(job_key, fallback, filename=None) -> str:
    """The user-facing name: ``<YYYYMMDD-HHMM>-<model>-print-<n>`` from the print record (#906).

    Start time first (display timezone), then the model name, then the record number. Never
    carries the gcode hash, is filesystem safe and length bounded. Falls back to ``fallback``
    (the capture directory name) when no completed record names the job. Never raises.
    """
    try:
        from datetime import datetime
        from . import print_records
        record = _record_for(job_key, filename)
        number = record.get("print_number")
        title = str(record.get("title") or "").strip()
        started = str(record.get("started_at") or record.get("completed_at") or "")
        if not (title or isinstance(number, int)):
            return str(fallback)
        parts = []
        if started:
            when = datetime.fromisoformat(started.replace("Z", "+00:00"))
            parts.append(when.astimezone(_config.tz()).strftime("%Y%m%d-%H%M"))
        if title:
            parts.append(_safe_part(title)[:60])
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            parts.append(f"print-{number}")
        name = "-".join(p for p in parts if p)
        return name[:100].strip("-.") or str(fallback)
    except Exception:
        return str(fallback)


def _safe_part(text) -> str:
    return "-".join("".join(c if c.isalnum() or c in "-_" else "-" for c in str(text)).split("-")).strip("-")


def archive_name(job_key, fallback, filename=None) -> str:
    """Kept for callers of the #906 first cut: the same record-based delivery name."""
    return delivery_name(job_key, fallback, filename)


def named_copy(video, name):
    """Hard link (or copy) video as ``<name>.mp4`` beside it so the chat attachment carries it.

    Returns the named path, or the original video when naming fails. Never raises.
    """
    try:
        video = Path(video)
        safe = _safe_part(name) or "timelapse"
        dest = video.with_name(safe if safe.lower().endswith(".mp4") else safe + ".mp4")
        if dest == video:
            return video
        if dest.exists():
            dest.unlink()
        try:
            os.link(video, dest)
        except OSError:
            import shutil
            shutil.copyfile(video, dest)
        return dest
    except Exception:
        return Path(video)


def caption_with_number(text, job_key, filename=None) -> str:
    """Add the print number to a post caption when the record has one and the text lacks it."""
    try:
        if "Print #" in text:
            return text
        record = _record_for(job_key, filename)
        number = record.get("print_number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            return f"{text}\nPrint #{number}"
    except Exception:
        pass
    return text


def assemble_frames(frames_dir, target, *, max_bytes, preference="auto", fps=PLAYBACK_FPS,
                    probe=None, ffmpeg=None, run=None, log=None, validate=None,
                    playback=None) -> Path | None:
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
    if playback:  # #906: bounded playback length; PLAYBACK_FPS stays the no-settings fallback
        with playback_frames(frames_dir, playback) as (planned_dir, planned_fps, _count):
            return assemble_frames(planned_dir, target, max_bytes=max_bytes, preference=preference,
                                   fps=planned_fps, probe=probe, ffmpeg=ffmpeg, run=run, log=log,
                                   validate=validate)
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


def selection_note(preference, ordered, probe, *, dri_note=None, nvidia_note=None) -> str:
    """The named note for an encode that will not start on the GPU, or "" (#455).

    - an explicit GPU preference that is unavailable names why;
    - ``auto`` with no GPU candidate at all names each vendor's reason;
    - ``cpu`` (the operator's choice) and a GPU-led plan need no note.
    """
    preference = str(preference or "auto").strip().lower()
    if preference == "cpu" or not ordered:
        return ""
    gpu_led = ordered[0].vendor != "cpu"
    if preference in dict(GPU_ENCODERS) and ordered[0].vendor != preference:
        reasons = [n for n in gpu_notes(probe, dri_note=dri_note, nvidia_note=nvidia_note)
                   if n.startswith(f"{preference}:")]
        why = reasons[0].split(": ", 1)[1] if reasons else "not available"
        return (f"timelapse encoder preference {preference} unavailable ({why}); "
                f"using {ordered[0].encoder}")
    if not gpu_led:
        notes = gpu_notes(probe, dri_note=dri_note, nvidia_note=nvidia_note)
        return (f"timelapse encode: no GPU encoder available ({'; '.join(notes) or 'none probed'}); "
                f"using CPU (libx264)")
    return ""


def _assemble(frames_dir, target, partial, frames, kbps, max_bytes, preference, fps,
              probe, ffmpeg, run, log, validate):
    if probe is None:
        probe = probe_ffmpeg(ffmpeg, run=run)
    ordered = candidates(preference, probe, ffmpeg=ffmpeg, run=run)
    note = selection_note(preference, ordered, probe)
    if note:
        log(note)
    gpu_failures = []
    for candidate in ordered:
        for attempt_kbps in (kbps, int(kbps * 0.7)):
            try:
                _encode_frames_once(frames_dir, partial, attempt_kbps, candidate.vendor,
                                    fps=fps, ffmpeg=ffmpeg, run=run)
            except Exception as exc:
                reason = ffmpeg_failure(exc)
                log(f"timelapse encode with {candidate.encoder} failed: {reason}")
                if candidate.vendor != "cpu":
                    gpu_failures.append(f"{candidate.encoder}: {reason}")
                break  # this encoder is done; fall through to the next candidate
            if partial.is_file() and 0 < partial.stat().st_size <= max_bytes:
                if not validate(partial):
                    log(f"timelapse encode with {candidate.encoder} produced an undecodable "
                        f"video; discarding it")
                    if candidate.vendor != "cpu":
                        gpu_failures.append(f"{candidate.encoder}: undecodable output")
                    break
                os.replace(partial, target)
                if candidate.vendor == "cpu" and gpu_failures:
                    log("timelapse encode fell back to CPU (libx264) after GPU failure: "
                        + "; ".join(gpu_failures))
                log(f"timelapse assembled {len(frames)} frame(s) -> {target} "
                    f"({target.stat().st_size} bytes, {candidate.encoder} at {attempt_kbps}k)")
                return target
            partial.unlink(missing_ok=True)
        else:
            if candidate.vendor != "cpu":
                gpu_failures.append(f"{candidate.encoder}: output over the {max_bytes}-byte cap")
            continue
    if gpu_failures:
        log("timelapse encode falling back to CPU (libx264, constant quality) after: "
            + "; ".join(gpu_failures))
    fitted = _assemble_quality_fallback(frames_dir, target, partial, frames, max_bytes, fps,
                                        ffmpeg, run, log, validate)
    if fitted is not None:
        return fitted
    log(f"timelapse assembly failed for {frames_dir}: no encoder produced a video "
        f"under {max_bytes} bytes")
    return None


# Constant quality always lands near the content's real size, unlike a forced bitrate that some
# encoders overshoot on a short, mostly static capture. Tried in order, then at lower resolution.
_FALLBACK_CRF = (23, 28, 32)
_FALLBACK_WIDTHS = (1280, 960, 640)


def _assemble_quality_fallback(frames_dir, target, partial, frames, max_bytes, fps,
                               ffmpeg, run, log, validate):
    """Last resort: software libx264 at constant quality until the video fits max_bytes."""
    for width in _FALLBACK_WIDTHS:
        for crf in _FALLBACK_CRF:
            try:
                run([str(ffmpeg), "-y", "-v", "error", "-framerate", str(fps),
                     "-i", str(Path(frames_dir) / "frame-%06d.jpg"), "-an",
                     "-vf", f"scale='min({width},iw)':-2", "-c:v", "libx264", "-preset", "medium",
                     "-crf", str(crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)],
                    capture_output=True, timeout=1800, check=True)
            except Exception as exc:
                log(f"timelapse libx264 fallback (crf {crf}, width {width}) failed: {exc!r}")
                return None  # software x264 itself is unusable; lower quality will not help
            if partial.is_file() and 0 < partial.stat().st_size <= max_bytes:
                if not validate(partial):
                    log(f"timelapse libx264 fallback (crf {crf}, width {width}) produced an "
                        "undecodable video; discarding it")
                    partial.unlink(missing_ok=True)
                    continue
                os.replace(partial, target)
                log(f"timelapse assembled {len(frames)} frame(s) -> {target} "
                    f"({target.stat().st_size} bytes, libx264 crf {crf}, width {width})")
                return target
            partial.unlink(missing_ok=True)
    return None


def archive_local_copy(video, name, *, directory=None, log=None):
    """Keep a finished timelapse in the mounted local archive, whatever the chat upload does.

    A no-op unless the archive directory exists (the container mounts it at /data/timelapses), so a
    stock install without the mount behaves as before. Never raises: archiving must not fail a post.
    """
    log = log or (lambda message: None)
    try:
        root = Path(directory or os.environ.get("PRINT_TIMELAPSE_LOCAL_ARCHIVE_DIR") or "/data/timelapses")
        if not root.is_dir():
            return None
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in str(name)).strip("-.") or "timelapse"
        dest = root / (safe if safe.lower().endswith(".mp4") else safe + ".mp4")
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{int(Path(video).stat().st_mtime)}{dest.suffix}")
        tmp = dest.with_name(f".{dest.name}.partial")
        import shutil
        shutil.copyfile(video, tmp)
        os.replace(tmp, dest)
        log(f"timelapse archived locally -> {dest}")
        return dest
    except Exception as exc:
        log(f"local timelapse archive failed: {exc!r}")
        return None


def _expected_source(data) -> str:
    """The camera source this settings view would capture from, redacted
    the way manifests record it — or "" when no source resolves."""
    try:
        url = timelapse_capture.frame_url(data)
    except Exception:
        return ""
    return live_stream.redact_url(url or "") if url else ""


def _scan_match(root, key, data):
    """The one finished capture a filename lookup may use for a job.

    Returns (directory, manifest) or None. The scan matches by evidence,
    strongest first, because a basename alone does not identify a print:

    - Filename tier: a manifest whose recorded filename equals the job
      key's full filename outranks one matching only by basename — two
      folders can hold files with the same basename.
    - Printer tier: the capture root is shared (paths.cache is shared by
      every printer's settings view). A candidate whose manifest records
      a different printer id is another printer's capture and never
      matches (#787, DATA-5) — that is positive evidence, so it is a
      gate; a manifest written before printer ids were recorded carries
      none and stays. Then, when this view resolves a camera source and
      a candidate recorded that same source, only those candidates stay.
      When no candidate matches the source (it changed, or none was
      recorded), all candidates stay: that filter is a preference.
    - Newest only: within what remains, the newest finished capture is
      the current print's. Callers reject it when it is not usable for
      them rather than reaching back to an earlier print's capture —
      a previous print's footage must never attach to this print.
    """
    wanted = Path(key).name
    tiers = ([], [])
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        manifest = timelapse_capture.read_manifest(directory)
        if not manifest or manifest.get("state") in (None, "new", "capturing"):
            continue
        recorded = str(manifest.get("filename") or "")
        if recorded == key:
            tiers[0].append((directory, manifest))
        elif Path(recorded).name == wanted:
            tiers[1].append((directory, manifest))
    printer_id = timelapse_capture.printer_identity(data)
    if printer_id:
        tiers = tuple([c for c in tier if str(c[1].get("printer_id") or printer_id) == printer_id]
                      for tier in tiers)
    candidates = tiers[0] or tiers[1]
    if not candidates:
        return None
    expected = _expected_source(data)
    if expected:
        same_source = [c for c in candidates if str(c[1].get("source") or "") == expected]
        if same_source:
            candidates = same_source
    best, best_stamp = None, None
    for directory, manifest in candidates:
        stamp = manifest.get("finished_at") or 0
        if best is None or stamp >= (best_stamp or 0):
            best, best_stamp = (directory, manifest), stamp
    return best


def find_capture(job_key, *, data=None, root=None) -> Path | None:
    """The finished capture directory for a job, or None.

    Capture sessions are keyed by the listener's notification identity
    while the completion postprocess knows the announcer's job key (the
    print's file path), so the directory is found by evidence, not by
    assuming the keys match: first the manifest in the directory the
    job key slugs to (its recorded job_key must equal the key), then
    the _scan_match evidence scan. Live ("capturing") sessions and
    empty captures never match — their job is still printing or
    produced nothing to encode.
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
    match = _scan_match(root, key, data)
    if match is None:
        return None
    directory, _manifest = match
    return directory if usable(directory) else None


def dark_capture(job_key, *, data=None, root=None) -> dict | None:
    """The manifest of a finished capture that kept NO frames because every
    frame was completely dark (#476), or None.

    The named camera-dark outcome beside find_capture: matches a job the
    same way (direct directory, then the _scan_match evidence scan), but
    a dark capture is exactly what find_capture must never hand to the
    encoder — zero kept frames and at least one frame skipped as dark.
    The delivery path uses the returned manifest (its dark_frames count)
    to name the outcome in the completion post.
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
    match = _scan_match(root, key, data)
    if match is None:
        return None
    _directory, manifest = match
    return manifest if dark_manifest(_directory) else None


def _deliver(video, job_key, directory, log, filename=None):
    """Archive the video under its record based name and return the named attachment path."""
    name = delivery_name(job_key, directory.name, filename)
    archive_local_copy(video, name, log=log)
    return named_copy(video, name)


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
        # Playback may decimate or hold, so the video holds the planned frame count, not the capture's.
        expected = expected_playback_frames(count, data)

        def reusable() -> bool:
            if not (target.is_file() and 0 < target.stat().st_size <= max_bytes):
                return False
            # A cached video is only reused when it still fully decodes:
            # an earlier colliding render may have left a stream-less file.
            return validate_video(target, ffmpeg=ffmpeg, min_frames=expected) if real else True

        if reusable():
            return _deliver(target, job_key, directory, log)
        with render_lock(target):
            if reusable():  # a concurrent render finished while we waited
                return _deliver(target, job_key, directory, log)
            if target.exists():
                log(f"cached timelapse {target} failed validation; re-rendering")
                target.unlink(missing_ok=True)
            made = assemble_frames(directory / "frames", target, max_bytes=max_bytes,
                                   preference=capture_preference(data), probe=probe,
                                   ffmpeg=ffmpeg, run=run, log=log,
                                   playback=playback_settings(data))
            return _deliver(made, job_key, directory, log) if made is not None else None
    except Exception as exc:  # the completion post must never see this fail
        log(f"captured timelapse unavailable for {job_key!r}: {exc!r}")
        return None
