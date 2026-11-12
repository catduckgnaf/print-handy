"""Per-plan timelapse clips: keep each plate's clip, then post one combined video.

A multi-plate plan posts no video after a non-final plate. Each plate's clip is
copied under ``<state_dir>/timelapse_plans/<plan_id>/`` and listed in
``<plan_id>.json``. After the last plate (or on cancel) the clips are joined in
plate order with ffmpeg's concat demuxer (stream copy, re-encoded when the codecs
differ) and the caller posts the result once. Stored clips are removed after.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import _config, vertical_short
from .print_records import _plan_parts, _safe_slug

# Stdout line from the announcer: TIMELAPSE_PLAN:<plan_id>:<more|final|partial>:<order>
PLAN_MARKER = "TIMELAPSE_PLAN:"
CLIP_ORDER_STRIDE = 1000


def _ffmpeg():
    name = _config.ffmpeg_bin()
    return name if "/" in name else (shutil.which(name) or name)


def _root():
    return _config.state_dir() / "timelapse_plans"


def _paths(plan_id):
    slug = _safe_slug(str(plan_id), "plan")
    return _root() / f"{slug}.json", _root() / slug


def required_parts(plan):
    """Required parts of a plan in plan order (same filter as print_next.remaining_parts)."""
    return [p for p in _plan_parts(plan.get("parts"))
            if not (p.get("optional") is True or p.get("required") is False
                    or (p.get("alternative_group") and p.get("selected") is not True))]


def plan_stage(plan, part_id=None):
    """(stage, order) for a plate that just completed, or None for a single-plate plan.

    stage is "final" when no required plate has copies left, else "more". order sorts clips in
    plan order (part position, then copy) however the plates were picked.
    """
    from .print_next import remaining_parts

    parts = required_parts(plan)
    if sum(p["quantity"] for p in parts) <= 1:
        return None
    order = None
    for index, part in enumerate(parts):
        if part_id is not None and str(part_id) in (str(part.get("id")), str(part.get("label"))):
            order = index * CLIP_ORDER_STRIDE + max(0, part["completed_quantity"] - 1)
            break
    if order is None:
        order = sum(p["completed_quantity"] for p in parts)
    return ("more" if remaining_parts(plan) else "final"), order


def _load_plan(plan_id):
    from .print_next import load_plan
    return load_plan(plan_id)


def expected_clips(plan_id):
    """How many plate clips a complete plan video has (copies count), or None when the plan is unreadable."""
    try:
        return sum(int(p["quantity"]) for p in required_parts(_load_plan(plan_id)))
    except Exception:
        return None


def _read(plan_id):
    path, _ = _paths(plan_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"clips": []}
    return data if isinstance(data, dict) and isinstance(data.get("clips"), list) else {"clips": []}


def _write(plan_id, data):
    path, _ = _paths(plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def add_clip(plan_id, order, source):
    """Copy one plate's rendered clip into the plan store; the same order replaces its clip."""
    _, directory = _paths(plan_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{int(order):08d}-{Path(str(source)).name}"
    shutil.copyfile(source, target)
    data = _read(plan_id)
    for row in data["clips"]:
        if row.get("order") == int(order) and row.get("path") != str(target):
            Path(row["path"]).unlink(missing_ok=True)
    data["clips"] = [r for r in data["clips"] if r.get("order") != int(order)]
    data["clips"].append({"order": int(order), "path": str(target)})
    data["clips"].sort(key=lambda r: r["order"])
    _write(plan_id, data)
    return target


def clips(plan_id):
    """Stored clip paths in plate order, skipping any that are gone."""
    return [Path(r["path"]) for r in sorted(_read(plan_id)["clips"], key=lambda r: r["order"])
            if Path(r["path"]).is_file()]


def clear(plan_id):
    path, directory = _paths(plan_id)
    shutil.rmtree(directory, ignore_errors=True)
    path.unlink(missing_ok=True)


def prune(now=None):
    """Drop plan stores untouched for timelapse.plan_clip_retention_days (a plan nobody finished)."""
    days = float(_config.cfg()["timelapse"]["plan_clip_retention_days"])
    root = _root()
    if days <= 0 or not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = []
    for path in root.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                clear(path.stem)
                removed.append(path.stem)
        except OSError:
            continue
    return removed


def combine(plan_id):
    """Join the stored clips in plate order into one MP4 and return its path (a lone clip is returned as is)."""
    paths = clips(plan_id)
    if not paths:
        raise RuntimeError("no stored clips for this plan")
    if len(paths) == 1:
        return paths[0]
    _, directory = _paths(plan_id)
    listing = directory / "concat.txt"
    listing.write_text("".join("file '%s'\n" % str(p.resolve()).replace("'", "'\\''") for p in paths),
                       encoding="utf-8")
    target = directory / "combined.mp4"
    partial = directory / ".combined.partial.mp4"
    base = [_ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
    try:
        subprocess.run(base + ["-c", "copy", "-an", "-movflags", "+faststart", str(partial)],
                       capture_output=True, timeout=600, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Codecs, sizes or timebases differ: re-encode every clip to one
        # common format, through the shared encoder seam (#246 stage 3:
        # the [timelapse] encoder choice, CPU by default — the same
        # libx264 argv as before — with a hardware failure retrying on
        # CPU, never failing the combine over an encoder).
        filt = ("scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
        encoder = _combine_encoder()
        try:
            _reencode(listing, filt, encoder, partial)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if encoder == "cpu":
                raise
            _reencode(listing, filt, "cpu", partial)
    os.replace(partial, target)
    return target


def _combine_encoder():
    """The [timelapse] encoder choice for the combine re-encode (#314 names).

    "cpu" unless configured; anything unexpected resolves to "cpu" so a
    bad value can cost hardware acceleration but never the combine.
    """
    try:
        encoder = str((_config.cfg().get("timelapse") or {}).get("encoder") or "cpu").strip().lower()
    except Exception:
        return "cpu"
    return encoder if encoder in vertical_short.ENCODERS else "cpu"


def _reencode(listing, filt, encoder, partial):
    # Built from scratch rather than extending the stream-copy command:
    # the seam's input args (VAAPI's -vaapi_device) must precede -i.
    subprocess.run([_ffmpeg(), "-y", "-v", "error", *vertical_short.encoder_input_args(encoder),
                    "-f", "concat", "-safe", "0", "-i", str(listing), "-an",
                    "-vf", vertical_short.encoder_filter(encoder, filt),
                    *vertical_short.encoder_args(encoder),
                    "-movflags", "+faststart", str(partial)],
                   capture_output=True, timeout=1800, check=True)
