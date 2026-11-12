"""The in-print camera watch core, shared by every printer family (#1320).

Before this module the Moonraker listener (``moonraker_event_listener``)
and the Bambu Lab launched-print monitor (``bambu_watch``) each carried a
copy of the same per-check rules, and the copies drifted (#1160). The
pieces that were already shared stay where they are, in ``visual_watch``:
the trigger schedule (``VisualPrintWatch.due`` and ``tick``), the failure
throttle (``FailureEpisode`` behind ``failure_alert_due``) and the
auto-pause evidence rule (``auto_pause_decision``). This module holds the
rest, so every family runs one implementation:

* **Deadline.** ``CHECK_DEADLINE_S`` bounds one whole camera check; a
  check that overruns is abandoned and the operator is told once.
* **Per-check rules.** Stall promotion (``promote_stall``), the baseline
  frame (``keep_frame``), what counts as suspicious (``is_suspicious``,
  with each family's recorded rule in ``FAMILY_RULES``), the alert and
  failure texts, and the once-per-print auto-pause reservation
  (``decide_auto_pause``).
* **Coverage.** Which families have the in-print watch, and per printer
  whether it is active or off and why (``coverage``, ``coverage_report``,
  ``live_coverage``); listeners put their own ``coverage`` in their
  heartbeat, and ``bot check`` and the diagnostics bundle read it.

Pure decisions only: nothing here reads a camera, calls a vision
endpoint, posts, or sends anything to a printer. The callers own the I/O
(the Moonraker listener on its event loop, the Bambu monitor on a thread).
"""
from __future__ import annotations

import json
import time
from typing import NamedTuple

from . import visual_watch as _vw

# One whole camera check (snapshot plus verdict) may take this long before
# it is abandoned: a camera or vision endpoint that never answers must not
# hang the watch, and the single-flight check slot must free up.
CHECK_DEADLINE_S = 150.0

# The question every in-print check asks the verdict route.
WATCH_PROMPT = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"


# -- family rules --------------------------------------------------------------

class FamilyRules(NamedTuple):
    """Where the families' in-print rules still differ, recorded instead of hidden.

    ``stall_veto``: "always" lets a healthy verdict (ok/safe) veto a stall
    alert at every sensitivity; "profile" only where the sensitivity
    profile's ``vision_veto`` allows it. ``empty_idle``: "recheck" treats
    an empty-bed verdict while printing as owed a re-check (an alert only
    when the re-check reads empty again); "alert" alerts at once.

    Unifying them would loosen one family's gate, which #1320 rules out;
    a later change can do it on purpose, with its own tests.
    """
    stall_veto: str
    empty_idle: str


FAMILY_RULES = {
    "moonraker": FamilyRules(stall_veto="always", empty_idle="recheck"),
    "bambulab_lan": FamilyRules(stall_veto="profile", empty_idle="alert"),
}


# -- per-check rules -----------------------------------------------------------

def promote_stall(watch, reason, changed, layer):
    """``(reason, stall)``: a probe whose layers advanced over an unchanged frame is a stall.

    The scheduler cannot see the camera before capture, so a
    ``layer_progress`` or ``periodic`` check is promoted here once the
    frame is known not to have changed for the profile's stall layers.
    At most one stall alert per print: the stall layer is recorded on the
    watch.
    """
    stall = (reason in {"layer_progress", "periodic"} and changed is False
             and isinstance(layer, int) and layer >= 5
             and watch.last_snapshot_layer
             and layer - watch.last_snapshot_layer >= watch.profile["stall_layers"]
             and watch.alerted_stall_layer is None)
    if stall:
        watch.alerted_stall_layer = layer
        return "stalled_layers", True
    return reason, False


def keep_frame(watch, reason, layer, image, now):
    """Adopt ``image`` as the watch's baseline. True when the check ends here (no verdict).

    A ``layer_progress`` probe that is not a stall adopts the frame as the
    baseline without moving the periodic clock, and asks no verdict. Any
    other check records the frame and its layer (``VisualPrintWatch.record``).
    The caller keeps its own copy of the frame for the next comparison.
    """
    if reason == "layer_progress":
        watch.last_snapshot = image
        if isinstance(layer, int):
            watch.last_snapshot_layer = max(watch.last_snapshot_layer, layer)
            watch.progress_seen_layer = layer
        return True
    watch.record(layer, now, image)
    return False


def is_suspicious(verdict, *, reason, changed, profile, rules, printing=True):
    """Whether a verdict alerts the operator, by the family's recorded rules.

    Missing plastic, spaghetti, detached, uncertain and the safety
    answers no/unsafe alert. An empty bed while printing is suspicious
    (at once, or on its re-check, per ``rules.empty_idle``). On a stall
    over an unchanged frame a healthy verdict may veto the alert
    (``rules.stall_veto``). "uncertain" always alerts.
    """
    suspicious = verdict in _vw.VISUAL_SUSPICIOUS_VERDICTS
    if verdict == "empty_idle":
        if rules.empty_idle == "alert":
            suspicious = True
        elif printing and reason == "empty_idle_recheck":
            suspicious = True
    if reason == "stalled_layers" and changed is False:
        veto = rules.stall_veto == "always" or bool(profile.get("vision_veto"))
        suspicious = not (veto and verdict in _vw.VISUAL_HEALTHY_VERDICTS)
    if verdict == "uncertain":
        suspicious = True
    return suspicious


def watch_failed(verdict, detail):
    """True when the verdict call could not judge the frame: a watch failure, not a print verdict."""
    return verdict == "uncertain" and detail == _vw.VISION_FAILED_DETAIL


def alert_text(reason, layer, verdict, detail, *, pause_why=""):
    """The camera alert's text; with ``pause_why`` the auto-pause line is appended."""
    if reason == "stalled_layers":
        text = _vw.stall_alert_text(layer, verdict, detail)
    else:
        text = (f"{reason} at layer {layer or 'unknown'}: {verdict}; "
                f"{detail or 'visual check needs operator review'}")
    if pause_why:
        text += (f"\nAuto-pause (visual_watch.auto_pause_early): {pause_why}, "
                 "so I am pausing the print. It is not cancelled.")
    return text


def vision_failed_text(reason, layer):
    """The alert when a frame was captured but the verdict call failed."""
    return (f"{reason} at layer {layer or 'unknown'}: {_vw.VISION_FAILED_DETAIL}, "
            "so treat this as unreviewed and check the print on the printer screen")


def camera_failed_text(reason, layer):
    """The alert when the snapshot itself failed: a dead camera must not leave a print silently unwatched."""
    return (f"{reason} at layer {layer or 'unknown'}: the camera snapshot could not be captured, "
            "so this print is unmonitored until the camera answers again; check the camera")


def decide_auto_pause(watch, *, enabled, printer_id, identity, state, verdict, frame_age_s, early,
                      frame_counted=None):
    """``(pause_now, why)`` for the opt-in early-layer auto-pause, reserved once per print.

    The rule is ``visual_watch.auto_pause_decision`` (a risky print, two
    independent suspicious views, the printer still printing this print).
    A yes is reserved on the watch before the caller posts or yields, so
    two overlapping checks can never both send a pause.

    ``frame_counted`` is whether THIS check's frame counted as an
    independent view, captured right after its ``note_verdict``. Pass it:
    ``watch.last_counted`` is shared by every check of the print, so an
    overlapping check that records a not-independent frame in between
    would otherwise make this check decide on that check's answer, and
    no check would pause (the #1330 regression).
    """
    if frame_counted is None:
        frame_counted = watch.last_counted
    pause_now, why = _vw.auto_pause_decision(
        watch.pause_members, enabled=enabled, already_paused=watch.auto_paused,
        printer_id=printer_id, print_identity=identity, state=state, verdict=verdict,
        frame_counted=frame_counted, frame_age_s=frame_age_s, mode=watch.mode,
        in_early_window=early)
    if pause_now:
        watch.auto_paused = True
    return pause_now, why


# -- coverage -------------------------------------------------------------------

# Which printer families have the in-print camera watch, by adapter
# connection. Everything else gets idle plate checks only (auto light-off,
# the next-plate offer), never in-print verdicts. Auto-pause is the same
# opt-in decision (decide_auto_pause) on every watched family.
IN_PRINT_WATCH = {
    "moonraker": "every print on the printer, from the listener",
    "bambulab_lan": "prints Print Handy launched, from the launched-print monitor",
}
FAMILY_NAMES = {
    "moonraker": "Moonraker (Klipper)",
    "bambulab_lan": "Bambu Lab",
    "prusalink": "PrusaLink",
    "octoprint": "OctoPrint",
    "elegoo_sdcp": "Elegoo SDCP",
    "flashforge_stock": "stock Flashforge",
    "offline": "offline (no printer connection)",
}


def watch_coverage(connection):
    """``(watched, text)``: whether a printer family has the in-print watch, in plain words."""
    name = FAMILY_NAMES.get(connection, connection or "this printer family")
    scope = IN_PRINT_WATCH.get(connection)
    if scope:
        return True, f"{name}: in-print watch for {scope}"
    return False, f"{name}: no in-print watch (idle plate checks only)"


def coverage(connection, *, enabled=True, unavailable_reason=""):
    """One printer's watch coverage as a listener reports it: active, or off and why.

    ``{"family", "watched", "active", "why"}``: ``watched`` says whether the
    family has the in-print watch at all; ``active`` whether it runs now;
    ``why`` names the reason when it does not (empty when active). The
    caller passes what it already knows (``visual_watch.enabled`` and its
    own unavailable reason), so this never touches a camera or endpoint.
    """
    watched, _text = watch_coverage(connection)
    family = FAMILY_NAMES.get(connection, connection or "unknown")
    if not watched:
        why = "this printer family has no in-print watch (idle plate checks only)"
    elif not enabled:
        why = "visual_watch.enabled is false"
    else:
        why = str(unavailable_reason or "")
    return {"family": family, "watched": watched, "active": not why, "why": why}


def listener_coverage(connection, cfg, api_key, *, camera_reason=""):
    """:func:`coverage` for a listener from its configuration, offline.

    ``camera_reason`` is the listener's own "no camera" reason (the
    Moonraker webcam can be unconfigured); the vision route is checked
    with ``prefetch=False``, so a heartbeat never starts a model download.
    """
    enabled = (cfg or {}).get("visual_watch", {}).get("enabled") is not False
    reason = ""
    if enabled and watch_coverage(connection)[0]:
        try:
            reason = camera_reason or _vw.watch_unavailable_reason(api_key, prefetch=False)
        except Exception as exc:
            reason = f"the vision route could not be checked ({exc})"
    return coverage(connection, enabled=enabled, unavailable_reason=reason)


# A listener refreshes its own coverage at most this often: the heartbeat
# runs every few seconds and must never wait on configuration reads.
COVERAGE_REFRESH_S = 300.0


class CoverageReporter:
    """A listener's cached coverage for its heartbeat. ``current()`` never raises.

    ``compute`` returns a :func:`coverage` record. It runs at most once per
    ``ttl_s``; a compute that raises keeps the last good record (None before
    the first), so a heartbeat is never lost to a coverage problem.
    """

    def __init__(self, compute, *, ttl_s=COVERAGE_REFRESH_S, clock=time.monotonic):
        self._compute = compute
        self._ttl_s = ttl_s
        self._clock = clock
        self._record = None
        self._at = None

    def current(self):
        now = self._clock()
        if self._at is None or now - self._at >= self._ttl_s:
            self._at = now
            try:
                self._record = self._compute()
            except Exception:
                pass
        return self._record


def coverage_text(record):
    """A heartbeat's coverage record in plain words: "active", or "off (why)"."""
    if not isinstance(record, dict):
        return ""
    if record.get("active"):
        return "active"
    return f"off ({record.get('why') or 'no reason recorded'})"


def live_coverage(data=None, printer_id=None):
    """The coverage this printer's listener last reported in its heartbeat, or None.

    Read from the heartbeat file only; a printer whose listener has not
    reported (no file, an older listener, a family without a listener)
    reads None.
    """
    try:
        from .. import healthcheck
        record = json.loads(healthcheck.heartbeat_path(data, printer_id).read_text())
    except Exception:
        return None
    entry = record.get("camera_watch") if isinstance(record, dict) else None
    return entry if isinstance(entry, dict) else None


def coverage_report(cfg, api_key, *, live=None):
    """``[(ok, detail)]``: the camera watch per configured printer, for ``bot check``.

    Each printer's family coverage (:func:`watch_coverage`) and whether the
    watch can run at all (a vision route, ``visual_watch.watch_unavailable_reason``).
    Offline: no camera, endpoint or download is touched. A route pinned with
    ``vision.watch_source`` that cannot work is a failed row; an install with
    no route at all keeps the watch off by design, so it only says so.
    When the printer's listener reported its own coverage in its heartbeat,
    the row adds it ("listener reports: active" or "off (why)").
    ``live`` is ``printer_id -> record or None`` (default :func:`live_coverage`).
    """
    from . import local_vision
    section = (cfg or {}).get("visual_watch") or {}
    if section.get("enabled") is False:
        return [(True, "off (visual_watch.enabled is false)")]
    try:
        reason = _vw.watch_unavailable_reason(api_key, prefetch=False)
    except Exception as exc:
        reason = f"the vision route could not be checked ({exc})"
    pinned = local_vision.watch_source(cfg) in ("endpoint", "local")
    entries = [e for e in (cfg or {}).get("printers") or [] if isinstance(e, dict)]
    if not entries:
        entries = [{"id": "", "model": ((cfg or {}).get("printer") or {}).get("model")}]
    if live is None:
        def live(printer_id):
            return live_coverage(cfg, printer_id) if printer_id else None
    rows = []
    for entry in entries:
        connection = None
        try:
            from .. import printers as _printers
            model = _printers.MODELS.get(str(entry.get("model") or ""))
            connection = getattr(getattr(model, "vendor", None), "connection", None)
        except Exception:
            connection = None
        watched, text = watch_coverage(connection)
        label = f"{entry['id']}: " if entry.get("id") and len(entries) > 1 else ""
        if watched and reason:
            ok, detail = not pinned, f"{label}{text}; not running: {reason}"
        elif watched:
            mode = "auto-pause on" if _vw.watch_settings(cfg)["auto_pause_early"] else "alert-only"
            ok, detail = True, f"{label}{text} ({mode})"
        else:
            ok, detail = True, f"{label}{text}"
        try:
            reported = coverage_text(live(entry.get("id")))
        except Exception:
            reported = ""
        if reported:
            detail += f"; listener reports: {reported}"
        rows.append((ok, detail))
    return rows
