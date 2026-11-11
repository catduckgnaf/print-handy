"""Native 9:16 vertical short from a finished timelapse (#317 stage 1).

A finished timelapse (printer-rendered today; #246 in-container capture and
#295's other vendor sources later — the input is just an MP4 path) is
re-framed into a vertical 720x1280 MP4 suitable for Shorts/Reels/TikTok and
for posting alongside the timelapse in the chat platforms:

- ``pad`` mode (default): the frame is scaled to fit the width and centred
  over a blurred, enlarged copy of itself filling the 9:16 canvas — the
  "GENERAL" layout OpenShorts uses, because a bed camera loses too much of
  the print to a hard crop.
- ``crop`` mode: scale to fill and centre-crop.

An optional title overlay (ffmpeg drawtext) is built from job metadata the
listener already knows — print title, printer display name, filament
material, elapsed print time, outcome — and the final frame is held for
about a second as a reveal. Everything here is a pure command builder plus
one runner; tests pin the command construction offline and no live
rendering is claimed.

Encoder seam: :func:`encoder_args` (with :func:`encoder_input_args` and
:func:`encoder_filter`) is the single place the codec is chosen. The
default is CPU libx264; the timelapse sizing shrink (#314) passes the
operator's ``[timelapse] encoder`` choice through the seam — the vendor
names (``vaapi`` / ``qsv`` / ``nvenc`` / ``cpu``) and the ffmpeg encoder
names are the ones `print_handy/live_stream.py`'s codec-aware plan (#311)
uses, and a hardware encode that fails falls back to CPU in the caller.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

OUT_W = 720
OUT_H = 1280
HOLD_S = 1.0
MODES = ("pad", "crop")


# The encoder choices an operator can pick for ffmpeg encodes (#314):
# live_stream's vendor names (#311) mapped to the same ffmpeg encoder
# names its plan uses. "cpu" is the default; the hardware choices need
# the GPU passed through to the container (docs/DOCKER.md) and are only
# ever used when configured.
ENCODERS = ("cpu", "vaapi", "qsv", "nvenc")
VAAPI_DEVICE = "/dev/dri/renderD128"  # default only; live_stream.vaapi_device() picks the real node (#1262)

_CODEC_ARGS = {
    "cpu": ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"],
    "vaapi": ["-c:v", "h264_vaapi"],
    "qsv": ["-c:v", "h264_qsv", "-preset", "veryfast"],
    "nvenc": ["-c:v", "h264_nvenc", "-preset", "fast", "-pix_fmt", "yuv420p"],
}


def encoder_args(encoder: str = "cpu") -> list[str]:
    """ffmpeg output codec args for an encoder choice — the #246 encoder-helper seam.

    An unknown choice yields the CPU args: the seam never raises, so a
    bad value can cost hardware acceleration but never an encode.
    """
    return list(_CODEC_ARGS.get(str(encoder or "cpu").strip().lower(), _CODEC_ARGS["cpu"]))


def encoder_input_args(encoder: str = "cpu") -> list[str]:
    """ffmpeg args that must precede ``-i`` for an encoder choice (VAAPI's device)."""
    if str(encoder or "").strip().lower() != "vaapi":
        return []
    from .. import live_stream  # lazy: keep this module's import cheap

    return ["-vaapi_device", live_stream.vaapi_device()]


def encoder_filter(encoder: str, base_filter: str) -> str:
    """The ``-vf`` chain for an encoder choice: VAAPI needs frames uploaded to the device."""
    if str(encoder or "").strip().lower() == "vaapi":
        return f"{base_filter},format=nv12,hwupload"
    return base_filter


def escape_drawtext(text) -> str:
    """Escape one string for an ffmpeg drawtext ``text=`` value inside a filter graph.

    The graph is passed as a single argv element (no shell), so the escapes
    are ffmpeg's own, and they must survive TWO parsing layers: the
    filtergraph parser and the per-filter option parser. Inside the
    single-quoted value, ``:`` is escaped and ``%`` doubled for the option
    layer (``%`` also drives drawtext's strftime-style expansion). An
    apostrophe is the hard case: a quote cannot be escaped inside quotes
    at either layer — ``\\'`` silently swallows the quote and, in a
    multi-filter graph, the rest of the graph with it (P2-111, verified
    against ffmpeg: the old form lost the ``[vout]`` label entirely).
    The working form closes the quote, emits the apostrophe unquoted as
    ``\\\\\\'`` (backslash-escaped for both layers), and reopens the
    quote; it renders pixel-identical to the same title passed via
    ``textfile=``. Backslash is doubled first, before any replacement
    that introduces backslashes of its own.
    """
    out = str(text).replace("\\", "\\\\")
    out = out.replace("'", "'\\\\\\''").replace(":", "\\:").replace("%", "%%")
    return out.replace("\r", " ").replace("\n", " ")


def format_elapsed(seconds) -> str | None:
    """A short human duration for the overlay ('2h 05m', '12m', '45s'), or None."""
    if seconds is None:
        return None
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def overlay_lines(meta) -> list[str]:
    """The overlay text lines for a job metadata dict (title first, details second).

    Recognised keys: ``title``, ``printer``, ``material``, ``elapsed_s``
    (or a pre-formatted ``elapsed``), ``outcome``. Missing pieces are
    simply left out; an empty metadata dict yields just the title fallback.
    """
    meta = meta or {}
    title = str(meta.get("title") or "").strip()
    lines = [title] if title else []
    details = []
    if meta.get("printer"):
        details.append(str(meta["printer"]).strip())
    if meta.get("material"):
        details.append(str(meta["material"]).strip())
    elapsed = meta.get("elapsed") or format_elapsed(meta.get("elapsed_s"))
    if elapsed:
        details.append(str(elapsed))
    if meta.get("outcome"):
        details.append(str(meta["outcome"]).strip())
    details = [part for part in details if part]
    if details:
        lines.append(" · ".join(details))
    return lines


def _drawtext_filters(lines) -> list[str]:
    filters = []
    for index, line in enumerate(lines[:2]):
        size = 44 if index == 0 else 30
        y = 48 if index == 0 else 108
        filters.append(
            f"drawtext=text='{escape_drawtext(line)}':fontsize={size}:fontcolor=white"
            f":borderw=2:bordercolor=black@0.6:x=(w-text_w)/2:y={y}")
    return filters


def filter_graph(in_w, in_h, mode="pad", *, lines=(), hold_s=HOLD_S) -> str:
    """The -filter_complex graph turning an in_w x in_h frame into the 9:16 short.

    Aspect math is delegated to ffmpeg's scale/crop options
    (``force_original_aspect_ratio``), so landscape, portrait and square
    inputs all land on exactly OUT_W x OUT_H; the input dimensions are
    accepted for validation and for callers that log them. The graph takes
    ``[0:v]`` in and leaves the result on ``[vout]``.
    """
    if mode not in MODES:
        raise ValueError(f"vertical short mode must be one of {MODES}, got {mode!r}")
    if not (isinstance(in_w, int) and isinstance(in_h, int)) or in_w <= 0 or in_h <= 0:
        raise ValueError(f"input dimensions must be positive, got {in_w!r}x{in_h!r}")
    extra = _drawtext_filters(lines)
    if hold_s:
        extra.append(f"tpad=stop_mode=clone:stop_duration={float(hold_s):g}")
    if mode == "crop":
        chain = (f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                 f"crop={OUT_W}:{OUT_H}")
        if extra:
            chain = f"{chain},{','.join(extra)}"
        return f"{chain}[vout]"
    segments = [
        "[0:v]split=2[bgin][fgin]",
        f"[bgin]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},boxblur=20:5[bg]",
        f"[fgin]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fg]",
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[vframed]",
    ]
    if extra:
        segments.append(f"[vframed]{','.join(extra)}[vout]")
    else:
        segments.append("[vframed]null[vout]")
    return ";".join(segments)


def build_command(source, target, *, in_w, in_h, mode="pad", lines=(), hold_s=HOLD_S,
                  ffmpeg="ffmpeg") -> list[str]:
    """The full ffmpeg argv for one short. Pure: no filesystem or process use."""
    graph = filter_graph(in_w, in_h, mode, lines=lines, hold_s=hold_s)
    return [ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-filter_complex", graph, "-map", "[vout]",
            "-an", *encoder_args(), "-movflags", "+faststart", str(target)]


def probe_video(path, *, ffprobe="ffprobe") -> tuple[int, int]:
    """(width, height) of the first video stream, via ffprobe. Raises on failure."""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def build_short(source, target=None, *, meta=None, mode="pad", overlay=True,
                hold_s=HOLD_S, ffmpeg="ffmpeg", ffprobe="ffprobe",
                probe=probe_video, run=subprocess.run) -> Path:
    """Render one vertical short next to the source (``<stem>.vertical.mp4``).

    Raises on any probe/encode failure; the caller (the listener's
    timelapse postprocess) treats that as warn-only and posts the
    completion announcement without the short.
    """
    source = Path(str(source))
    target = Path(str(target)) if target is not None else source.with_name(f"{source.stem}.vertical.mp4")
    in_w, in_h = probe(source, ffprobe=ffprobe)
    lines = overlay_lines(meta) if overlay else []
    partial = target.with_name(f".{target.name}.partial.mp4")
    command = build_command(source, partial, in_w=in_w, in_h=in_h, mode=mode,
                            lines=lines, hold_s=hold_s, ffmpeg=ffmpeg)
    try:
        run(command, capture_output=True, timeout=600, check=True)
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target
