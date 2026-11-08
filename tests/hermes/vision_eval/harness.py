"""Stage 0 measurement harness for watchdog verdict producers (#455 item 4).

The Stage 0 evaluation harness from the item-4 scoping: it replays labeled
session fixtures through any verdict producer — today's local detector
route, a future plate-reference producer, the box-geometry heuristic, a
later classifier — and reports what the watchdog's *consumers* would have
experienced: per-print outcomes, not per-frame confusion.

Why per-print: a watchdog verdict is consumed per print, and frames from
one camera are correlated — a confusing scene stays confusing for many
checks in a row. A producer is therefore judged on sessions:

* **false alert** — the session is healthy (label ``ok``) but the
  replayed alert policy fires at least once;
* **false confirmation** — a healthy session produces a *streak-confirmed*
  named verdict (two consecutive verdicts in the #913 auto-pause set), the
  strictest consumer a named verdict has;
* **detection** — a session labeled with a failure class is judged with
  that class at least once (for the classes a producer can name) or with
  any alert verdict (for classes it cannot).

Cadence is simulated, not slept: the replay walks each session's frames
in order and asks the watch schedule (``VisualPrintWatch.due`` at the
session's sensitivity profile, plus the #913 early-layer window for risky
prints) which frames the watchdog would actually have checked. A
manifest's ``cadence_s`` is the spacing the session was recorded at; the
schedule decides which of those frames become checks, so a session
recorded denser than the cadence still replays honestly.

Producers are plain callables ``producer(frame_bytes, ctx)`` returning
``(verdict, detail, boxes)`` — ``boxes`` is the producer's kept
detections as ``(confidence, (x1, y1, x2, y2))`` in image pixels (empty
when the producer exposes none). :func:`local_detector_producer` adapts
the real local route; :func:`box_geometry_producer` wraps any producer
with the Stage 0 detached heuristic.
"""
from __future__ import annotations

import json
from pathlib import Path

from print_handy.hermes import local_vision, visual_watch

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "vision_eval"

MANIFEST_NAME = "manifest.json"
REQUIRED_FRAME_FIELDS = ("file", "label", "source", "license", "synthetic")
SESSION_LABELS = frozenset(
    {"ok", "empty_idle", "air", "detached", "spaghetti", "uncertain"})
# Named verdicts a session can carry (its per-frame labels use the same set,
# minus the session-level reading of "ok").
FRAME_LABELS = SESSION_LABELS
ALERT_VERDICTS = visual_watch.VISUAL_ALERT_VERDICTS


class EvalSetError(ValueError):
    """A fixture manifest is missing, malformed, or mislabeled."""


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------

def load_session(session_dir):
    """Load one session fixture: its manifest plus the frame bytes in order.

    Returns ``{"manifest": ..., "frames": [bytes, ...]}``. Raises
    :class:`EvalSetError` on any contract violation — a fixture that
    cannot be trusted cannot gate anything.
    """
    session_dir = Path(session_dir)
    manifest_path = session_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise EvalSetError(f"{session_dir}: no {MANIFEST_NAME}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise EvalSetError(f"{session_dir}: manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise EvalSetError(f"{session_dir}: manifest is not an object")
    for field in ("session", "label", "source", "license", "synthetic"):
        if field not in manifest:
            raise EvalSetError(f"{session_dir}: manifest missing {field!r}")
    if manifest["label"] not in SESSION_LABELS:
        raise EvalSetError(
            f"{session_dir}: session label {manifest['label']!r} not in "
            f"{sorted(SESSION_LABELS)}")
    frames_meta = manifest.get("frames")
    if not isinstance(frames_meta, list) or not frames_meta:
        raise EvalSetError(f"{session_dir}: manifest frames must be a non-empty list")
    frames = []
    for index, entry in enumerate(frames_meta):
        if not isinstance(entry, dict):
            raise EvalSetError(f"{session_dir}: frame {index} is not an object")
        for field in REQUIRED_FRAME_FIELDS:
            if field not in entry:
                raise EvalSetError(
                    f"{session_dir}: frame {index} missing {field!r}")
        if entry["label"] not in FRAME_LABELS:
            raise EvalSetError(
                f"{session_dir}: frame {index} label {entry['label']!r} unknown")
        if bool(entry["synthetic"]) != bool(manifest["synthetic"]):
            raise EvalSetError(
                f"{session_dir}: frame {index} synthetic flag disagrees with "
                "the session manifest — a session is synthetic or it is not")
        frame_path = session_dir / entry["file"]
        if not frame_path.is_file():
            raise EvalSetError(f"{session_dir}: frame file {entry['file']!r} missing")
        frames.append(frame_path.read_bytes())
    return {"manifest": manifest, "frames": frames}


def discover_sessions(root=FIXTURES_ROOT):
    """Every session directory under ``root``, sorted by name."""
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p / MANIFEST_NAME).is_file())


def load_eval_set(root=FIXTURES_ROOT):
    """Load and validate every session under ``root``."""
    return [load_session(p) for p in discover_sessions(root)]


def _is_dark(frame_bytes):
    """frame_dark on raw bytes, without raising on a non-image fixture."""
    try:
        from PIL import Image
        import io
        return visual_watch.frame_dark(Image.open(io.BytesIO(frame_bytes)))
    except Exception:
        return False


# --------------------------------------------------------------------------
# Cadence simulation
# --------------------------------------------------------------------------

def _due_reason(watch, layer, now, changed):
    """One scheduler step, mirroring the listeners' promotion rule.

    ``VisualPrintWatch.due`` answers the layer-event side; the listeners
    additionally promote a periodic/layer-progress check to a stall when
    the layers advanced over an unchanged frame. The replay applies the
    same promotion so stall-adjacent producers see the same reasons.
    """
    reason = watch.due(layer, now, snapshot_changed=changed)
    if (reason in {"layer_progress", "periodic"} and changed is False
            and layer >= 5 and watch.last_snapshot_layer
            and layer - watch.last_snapshot_layer >= watch.profile["stall_layers"]
            and watch.alerted_stall_layer is None):
        watch.alerted_stall_layer = layer
        return "stalled_layers"
    return reason


def replay_session(session, producer, *, sensitivity=3, risky=True):
    """Replay one session through ``producer`` at watchdog cadence.

    The replay arms the #913 early-layer window by default: the harness
    measures a producer against its strictest consumer (the early
    auto-pause), and a producer that is safe there is safe for the alert
    policy everywhere. Pass ``risky=False`` to measure the plain
    schedule instead.

    Returns the per-print outcome record: the checks the schedule
    actually made (frame index, simulated time, layer, reason, verdict),
    whether the alert policy fired, the named verdicts seen, whether a
    named verdict was streak-confirmed, and — when the producer exposes
    boxes — the box-geometry experiment's per-check record.
    """
    manifest = session["manifest"]
    frames = session["frames"]
    cadence_s = float(manifest.get("cadence_s") or 60)
    layers = manifest.get("layers") or [1] * len(frames)
    if len(layers) != len(frames):
        raise EvalSetError(
            f"{manifest.get('session')}: layers list does not match frame count")
    watch = visual_watch.VisualPrintWatch(sensitivity=sensitivity)
    if risky:
        watch.set_mode(visual_watch.WATCH_MODE_EARLY)
    watch.last_check_at = 0.0  # the print starts now; the first frame is t=0
    streak = local_vision.FailureStreak()
    checks = []
    geometry = []
    previous = None
    for index, frame in enumerate(frames):
        now = index * cadence_s
        layer = layers[index]
        changed = None if previous is None else visual_watch._frame_changed(previous, frame)
        reason = _due_reason(watch, layer, now, changed)
        if reason is None:
            previous = frame
            continue
        ctx = {"session": manifest["session"], "frame_index": index,
               "layer": layer, "reason": reason, "streak": streak,
               "sensitivity": sensitivity}
        verdict, detail, boxes = producer(frame, ctx)
        if _is_dark(frame):
            # Mirrors the listeners: on a dark frame the route is skipped
            # and the verdict is reported as dark (visual_watch's own
            # consumer vocabulary), whatever the producer said.
            verdict, detail = "dark", "frame too dark to judge"
        watch.note_verdict(verdict, layer)
        checks.append({"frame_index": index, "t": now, "layer": layer,
                       "reason": reason, "verdict": verdict, "detail": detail})
        geometry.append(box_geometry_step(geometry[-1]["state"] if geometry else None,
                                          boxes, verdict))
        if reason != "layer_progress":
            previous = frame
            # record() moves the snapshot baseline, exactly as the listeners do.
            watch.record(layer, now, frame)
    named = [c["verdict"] for c in checks]
    alert_fired = any(v in ALERT_VERDICTS for v in named)
    confirmed = _streak_confirmed(checks, watch)
    return {"session": manifest["session"], "label": manifest["label"],
            "synthetic": bool(manifest["synthetic"]),
            "checks": checks, "alert_fired": alert_fired,
            "named_verdicts": sorted(set(named)),
            "streak_confirmed": confirmed,
            "streak_verdict": confirmed,
            "geometry": geometry,
            "producer_outcome": _session_outcome(manifest["label"], checks, confirmed)}


def _streak_confirmed(checks, watch):
    """The named verdict streak-confirmed in this replay, or "".

    Mirrors the listener's auto-pause accounting: consecutive checks
    inside the early-layer window with verdicts in the #913 auto-pause
    set; two in a row confirm the *second* verdict's name.
    """
    streak = 0
    confirmed = ""
    for check in checks:
        if check["verdict"] in visual_watch.AUTO_PAUSE_VERDICTS and \
                watch.in_early_window(check["layer"]):
            streak += 1
            if streak >= visual_watch.AUTO_PAUSE_STREAK:
                confirmed = check["verdict"]
        else:
            streak = 0
    return confirmed


def _session_outcome(label, checks, confirmed):
    """Classify one replayed session for the report."""
    if label == "ok":
        if confirmed:
            return "false_confirmation"
        if any(c["verdict"] in ALERT_VERDICTS for c in checks):
            return "false_alert"
        return "quiet"
    named = {c["verdict"] for c in checks}
    if label in named:
        return "detected_named"
    if named & ALERT_VERDICTS:
        return "detected_alert_only"
    return "missed"


# --------------------------------------------------------------------------
# Box-geometry discontinuity experiment (detached candidate)
# --------------------------------------------------------------------------

def _box_center(box):
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _box_area(box):
    x1, y1, x2, y2 = box
    return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))


def box_geometry_step(state, boxes, verdict):
    """One step of the box-geometry discontinuity heuristic.

    The heuristic from the scoping: a strong failure box vanishing or
    relocating discontinuously between consecutive checks, while the
    print continues, is a candidate ``detached`` signal. ``state`` is the
    previous step's returned record (its ``state`` field), ``boxes`` the
    producer's kept detections for this check, ``verdict`` the producer's
    own verdict for context. Returns a record with the candidate signal
    for this check (``candidate``: True when the discontinuity fired).
    """
    strong = [(conf, box) for conf, box in (boxes or []) if conf >= 0.5]
    prev_strong = (state or {}).get("strong") or []
    candidate = False
    kind = ""
    if prev_strong and strong:
        prev_center = _box_center(max(prev_strong, key=lambda cb: cb[0])[1])
        center = _box_center(max(strong, key=lambda cb: cb[0])[1])
        jump = max(abs(center[0] - prev_center[0]), abs(center[1] - prev_center[1]))
        if jump >= 160:  # a quarter of a 640-wide frame, in one check
            candidate, kind = True, "relocated"
    elif prev_strong and not strong:
        candidate, kind = True, "vanished"
    return {"candidate": candidate, "kind": kind,
            "strong_boxes": len(strong),
            "state": {"strong": strong}}


def box_geometry_producer(base_producer):
    """Wrap ``base_producer``: verdicts pass through, and the wrapped
    producer's record gains the geometry experiment's trace. The
    heuristic never changes a verdict — Stage 0 measures it."""
    def produce(frame, ctx):
        verdict, detail, boxes = base_producer(frame, ctx)
        return verdict, detail, boxes
    return produce


# --------------------------------------------------------------------------
# Producers
# --------------------------------------------------------------------------

def local_detector_producer(frame, ctx):
    """Today's local route: the real detector + per-print streak.

    An unavailable detector is a named outcome, not a crash: without
    onnxruntime or a model the local route answers ``unavailable`` and
    the replay scores the session missed, which is what an install in
    that state would experience."""
    try:
        verdict, detail = local_vision.local_verdict(frame, streak=ctx["streak"])
    except local_vision.LocalVisionError as exc:
        return "unavailable", str(exc), []
    try:
        boxes = local_vision.detect(frame)
    except local_vision.LocalVisionError:
        boxes = []
    return verdict, detail, boxes


def scripted_producer(verdicts):
    """A producer that answers from a fixed script, for harness tests:
    ``verdicts`` maps *check number* (0 for the first check the schedule
    makes, 1 for the next, ...) -> (verdict, detail), or is a list of
    (verdict, detail) in check order. Keying by check number keeps a
    script honest about the schedule: the producer only answers when the
    watchdog would actually have looked."""
    calls = {"n": 0}

    def produce(frame, ctx):
        n = calls["n"]
        calls["n"] += 1
        if isinstance(verdicts, dict):
            verdict, detail = verdicts.get(n, ("ok", "scripted ok"))
        else:
            verdict, detail = verdicts[min(n, len(verdicts) - 1)]
        return verdict, detail, []
    return produce


# --------------------------------------------------------------------------
# Reporting: per-print outcomes, per-class precision/recall
# --------------------------------------------------------------------------

def evaluate(producer, sessions=None, **replay_kwargs):
    """Run ``producer`` over the eval set and build the report.

    The report is per-print first: one outcome row per session, then
    aggregate rates — per-class precision/recall over sessions, the
    healthy-set false-alert and false-confirmation session counts, and
    the box-geometry experiment's precision on labeled detached sessions
    vs its firing rate on healthy ones.
    """
    if sessions is None:
        sessions = load_eval_set()
    rows = [replay_session(s, producer, **replay_kwargs) for s in sessions]
    healthy = [r for r in rows if r["label"] == "ok"]
    report = {
        "sessions": rows,
        "session_count": len(rows),
        "checks_total": sum(len(r["checks"]) for r in rows),
        "healthy_sessions": len(healthy),
        "false_alert_sessions": sum(1 for r in healthy if r["alert_fired"]),
        "false_confirmation_sessions": sum(1 for r in healthy if r["streak_confirmed"]),
        "per_class": {},
        "geometry_experiment": _geometry_report(rows),
    }
    labels = sorted({r["label"] for r in rows})
    for label in labels:
        group = [r for r in rows if r["label"] == label]
        named = sum(1 for r in group if label in r["named_verdicts"])
        alerted = sum(1 for r in group if r["alert_fired"])
        report["per_class"][label] = {
            "sessions": len(group),
            "detected_named_rate": (named / len(group)) if group else 0.0,
            "alerted_rate": (alerted / len(group)) if group else 0.0,
            "outcomes": sorted(r["producer_outcome"] for r in group),
        }
    return report


def _geometry_report(rows):
    """Score the box-geometry heuristic against the session labels."""
    fired_healthy = 0
    fired_detached = 0
    detached_sessions = 0
    healthy_sessions = 0
    for row in rows:
        fired = any(g["candidate"] for g in row["geometry"])
        if row["label"] == "ok":
            healthy_sessions += 1
            fired_healthy += int(fired)
        elif row["label"] == "detached":
            detached_sessions += 1
            fired_detached += int(fired)
    return {"sessions_fired_healthy": fired_healthy,
            "healthy_sessions": healthy_sessions,
            "sessions_fired_detached": fired_detached,
            "detached_sessions": detached_sessions}


def format_report(report):
    """The report as plain text, for a test log or a PR body."""
    lines = [f"sessions: {report['session_count']} "
             f"(healthy {report['healthy_sessions']}), "
             f"checks replayed: {report['checks_total']}",
             f"healthy false alerts: {report['false_alert_sessions']} session(s), "
             f"false confirmations: {report['false_confirmation_sessions']} session(s)"]
    for label, stats in sorted(report["per_class"].items()):
        lines.append(
            f"  {label}: {stats['sessions']} session(s), named detection "
            f"{stats['detected_named_rate']:.0%}, alerted {stats['alerted_rate']:.0%}, "
            f"outcomes {stats['outcomes']}")
    geo = report["geometry_experiment"]
    lines.append(
        f"box-geometry detached heuristic: fired on {geo['sessions_fired_detached']}/"
        f"{geo['detached_sessions']} detached session(s), "
        f"{geo['sessions_fired_healthy']}/{geo['healthy_sessions']} healthy session(s)")
    return "\n".join(lines)
