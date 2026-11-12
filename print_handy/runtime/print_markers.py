"""Per-print event markers (#317): timestamped things that happened during a print.

While a print runs, the Moonraker listener records markers for the moments
a later blooper reel (#317 stage 2) will want to find again: print start,
camera-watchdog alert, pause, resume, cancel, printer error, and completion
with its outcome. This module is only the recorder and the timelapse-time
mapping helper; no reel is built here yet.

Storage follows the plan_timelapse pattern: one JSON file per print under
``<state_dir>/print_markers/<slug>.json``, written atomically (partial file
+ ``os.replace``), keyed by the print's job key (the G-code filename the
listener already tracks). No new database and no new source of truth — the
file is a derived log next to the job/plan records, and losing it loses
nothing but future blooper material.

Record format::

    {
      "job_key": "benchy.gcode",
      "started_wall": 1760000000.0,          # epoch of the start marker
      "markers": [
        {"event": "start", "at": "2026-…Z", "wall_s": 1760000000.0,
         "elapsed_s": 0.0, "detail": ""},
        {"event": "watchdog_alert", "at": "…", "wall_s": …,
         "elapsed_s": 612.4, "detail": "spaghetti at layer 48: …"},
        {"event": "complete", "at": "…", "wall_s": …,
         "elapsed_s": 3600.0, "detail": "", "outcome": "success"}
      ]
    }

Each marker carries both the wall-clock time (``wall_s`` / ``at``) and the
print-elapsed offset (``elapsed_s``, seconds of print time when known, else
derived from ``started_wall``). A blooper builder maps an elapsed offset
into a position in the rendered timelapse with :func:`map_to_video_time`:
for the #246 in-container capture the elapsed offset is the capture clock;
for printer-rendered timelapses the mapping is duration-proportional.

#299 (listener per domain) is not on main yet (its registry + normalised
status/event contract are in draft PR #320, with coarser transport event
kinds: status / completion / error / snapshot_available), so markers are
recorded from the Moonraker listener path in this shape. When #320 lands,
its normalised events should feed :func:`record` unchanged — the record
format here is the stable, durable part; the marker kinds are the
print-lifecycle vocabulary a blooper builder needs, finer than the
transport kinds on purpose.

Recording is fail-safe by contract: :func:`record` returns ``False``
instead of raising for any storage problem, so a marker failure can never
break the listener, a post, or printer control (the #235 warn-only
precedent). Unknown event/outcome names are programming errors and raise
``ValueError``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import _config
from .print_records import _safe_slug

logger = logging.getLogger(__name__)

START = "start"
WATCHDOG_ALERT = "watchdog_alert"
PAUSE = "pause"
RESUME = "resume"
CANCEL = "cancel"
ERROR = "error"
COMPLETE = "complete"

EVENT_KINDS = frozenset({START, WATCHDOG_ALERT, PAUSE, RESUME, CANCEL, ERROR, COMPLETE})
OUTCOMES = frozenset({"success", "failed", "cancelled"})

# A pathological print (pause/resume flapping) must not grow the file forever.
MAX_MARKERS = 500
_DETAIL_LIMIT = 500


# record() is called from the listener's event-loop thread and from
# asyncio.to_thread watchdog workers; the read-modify-write must be one
# transaction or concurrent appends overwrite each other.
_RECORD_LOCK = threading.Lock()


def _root() -> Path:
    return _config.state_dir() / "print_markers"


def _path(job_key, root=None) -> Path:
    base = Path(root) if root is not None else _root()
    # The slug alone collides ('part one.gcode' and 'part-one.gcode' slugify
    # identically, as do long names past the slug's cut), so the filename
    # also carries a digest of the full job key.
    digest = hashlib.sha256(str(job_key).encode("utf-8")).hexdigest()[:12]
    return base / f"{_safe_slug(str(job_key), 'print')}-{digest}.json"


def _iso(wall_s: float) -> str:
    return datetime.fromtimestamp(wall_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _read(job_key, root=None) -> dict:
    try:
        data = json.loads(_path(job_key, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"job_key": str(job_key), "markers": []}
    if not isinstance(data, dict) or not isinstance(data.get("markers"), list):
        return {"job_key": str(job_key), "markers": []}
    data.setdefault("job_key", str(job_key))
    return data


def _write(job_key, data, root=None) -> None:
    path = _path(job_key, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-writer partial name: two writers must never share one temp file
    # and consume each other's replacement.
    partial = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.partial")
    partial.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(partial, path)


def load(job_key, root=None) -> dict:
    """The stored marker record for a print (empty record when none exists)."""
    return _read(job_key, root)


def markers(job_key, root=None) -> list:
    """The print's markers in wall-clock order (a copy; safe to mutate)."""
    rows = [dict(row) for row in _read(job_key, root)["markers"] if isinstance(row, dict)]
    return sorted(rows, key=lambda row: row.get("wall_s") or 0.0)


def record(job_key, event, *, at=None, elapsed_s=None, detail="", outcome=None, root=None) -> bool:
    """Append one marker for a print. Returns True when it was stored.

    Never raises for storage problems (returns False and logs instead);
    raises ValueError only for an unknown event or outcome name.
    ``elapsed_s`` may be omitted: for a print with a recorded start marker
    it is derived from the wall-clock offset since that start.
    """
    if event not in EVENT_KINDS:
        raise ValueError(f"unknown print marker event {event!r}")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown print marker outcome {outcome!r}")
    if not job_key:
        return False
    try:
        wall_s = time.time() if at is None else float(at)
        with _RECORD_LOCK:
            return _record_locked(job_key, event, wall_s=wall_s, elapsed_s=elapsed_s,
                                  detail=detail, outcome=outcome, root=root)
    except Exception as exc:  # fail-safe by contract: see module docstring
        logger.warning("print marker %s for %r not recorded: %r", event, job_key, exc)
        return False


def _record_locked(job_key, event, *, wall_s, elapsed_s, detail, outcome, root) -> bool:
    """The read-modify-write behind record(), run under _RECORD_LOCK."""
    data = _read(job_key, root)
    if event == START:
        # A re-print of the same file starts a new elapsed clock even
        # though earlier markers stay in the shared record; keeping the
        # first start would fold the previous print and the idle gap
        # into this print's offsets.
        data["started_wall"] = wall_s
    if elapsed_s is None and data.get("started_wall") is not None:
        derived = wall_s - float(data["started_wall"])
        elapsed_s = derived if derived >= 0 else None
    marker = {
        "event": event,
        "at": _iso(wall_s),
        "wall_s": wall_s,
        "elapsed_s": None if elapsed_s is None else max(0.0, float(elapsed_s)),
        "detail": str(detail or "")[:_DETAIL_LIMIT],
    }
    if outcome is not None:
        marker["outcome"] = outcome
    rows = [row for row in data["markers"] if isinstance(row, dict)]
    rows.append(marker)
    rows.sort(key=lambda row: row.get("wall_s") or 0.0)
    data["markers"] = rows[-MAX_MARKERS:]
    _write(job_key, data, root)
    return True


def map_to_video_time(elapsed_s, print_duration_s, video_duration_s):
    """Where an elapsed print offset falls in a rendered timelapse, in seconds.

    Duration-proportional: a marker halfway through the print lands halfway
    through the video. Returns None when the mapping cannot be made (any
    input missing, or a non-positive duration). An offset before the start
    clamps to 0 and one past the end clamps to the video duration, so a
    marker recorded slightly after the terminal event still maps onto the
    final frame instead of falling off the reel.
    """
    if elapsed_s is None or print_duration_s is None or video_duration_s is None:
        return None
    try:
        elapsed, total, video = float(elapsed_s), float(print_duration_s), float(video_duration_s)
    except (TypeError, ValueError):
        return None
    if total <= 0 or video <= 0:
        return None
    clamped = min(max(elapsed, 0.0), total)
    return clamped / total * video


def marker_video_time(marker, *, print_duration_s, video_duration_s, started_wall=None):
    """Map one stored marker into timelapse time (see map_to_video_time).

    Prefers the marker's own ``elapsed_s``; a marker recorded without one
    (for example a watchdog alert, which has no print clock at hand) falls
    back to its wall-clock offset from ``started_wall`` (the record's
    start marker time, passed by the caller).
    """
    if not isinstance(marker, dict):
        return None
    elapsed = marker.get("elapsed_s")
    if elapsed is None and started_wall is not None and marker.get("wall_s") is not None:
        try:
            elapsed = float(marker["wall_s"]) - float(started_wall)
        except (TypeError, ValueError):
            elapsed = None
    return map_to_video_time(elapsed, print_duration_s, video_duration_s)
