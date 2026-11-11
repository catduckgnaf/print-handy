"""Shared camera-watchdog pieces: sensitivity profiles, the vision verdict call, and the trigger policy.

This module is the one implementation of the visual check the Moonraker
listener has always run, lifted out unchanged (#294) so the Bambu camera
watch (bambu_watch.py) runs the *same* verdict path, the *same*
[visual_watch] sensitivity profiles, and the *same* trigger policy instead
of a Bambu-specific copy. The listener imports these names back, so its
behaviour — and every existing reference to them — is unchanged.

Nothing here controls a printer: the verdict path only looks at a frame, and
VisualPrintWatch only decides when a check is due.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

from .. import ai_routes
from . import _config, local_empty, local_vision

logger = logging.getLogger(__name__)

VISUAL_VERDICTS = {"ok", "air", "spaghetti", "detached", "empty_idle", "uncertain"}
VISUAL_ALERT_VERDICTS = {"air", "spaghetti", "detached", "uncertain"}
VISUAL_HEALTHY_VERDICTS = {"ok", "safe"}
VISUAL_SUSPICIOUS_VERDICTS = {"air", "spaghetti", "detached", "uncertain", "no", "unsafe"}

# The detail of the "uncertain" a verdict call returns when it could not judge
# the frame at all (a broken endpoint or detector, #467's generic wording).
# Watches treat it as a watch failure, part of a failure episode, not as a
# fresh judgement of the print.
VISION_FAILED_DETAIL = "the vision check could not be completed"


class FailureEpisode:
    """One print's run of consecutive camera-watch failures, for the alert throttle.

    A failure is a check that produced no usable judgement: the snapshot
    failed, the whole check overran its deadline, or the verdict call
    could not judge the frame. The first failure of an episode alerts at
    once; while the episode lasts, at most one more alert per
    ``interval_s`` (the watch's periodic interval) goes out, so a dead
    camera or endpoint nags no more often than a healthy print is
    checked. A check that judges a frame ends the episode, and
    :meth:`recovered` says whether the operator heard about it, so the
    watch can say once that it is working again. Pure policy, no I/O;
    shared by every family with an in-print watch.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.failures = 0
        self.alerted_at = None

    def failed(self, now, interval_s):
        """Count one failure. True when it should alert now."""
        self.failures += 1
        if self.alerted_at is not None and now - self.alerted_at < interval_s:
            return False
        self.alerted_at = now
        return True

    def recovered(self):
        """End the episode. True when it had alerted (a recovery notice is owed)."""
        owed = self.failures > 0 and self.alerted_at is not None
        self.reset()
        return owed


def recovery_text(label):
    """The one notice that ends an alerted failure episode."""
    return (f"Camera watch: {label} is being checked again; the camera and the vision "
            "check answered. Nothing was sent to the printer.")


TIMEOUT_ALERT_TEXT = ("Camera watch check timed out; this print is unmonitored while the watch "
                      "retries. Check the camera and vision endpoint.")


# Alert frames (the photos attached to camera alerts and status answers) are
# written to one folder shared by every watch. They are only needed until the
# post that carries them is delivered, so the folder keeps the newest
# FRAME_KEEP files and drops any older than FRAME_KEEP_DAYS.
FRAME_KEEP = 200
FRAME_KEEP_DAYS = 14


def frame_dir():
    """Where alert frames are written: PRINT_WATCH_FRAME_DIR, else <cache>/print-watch."""
    return Path(os.environ.get("PRINT_WATCH_FRAME_DIR") or _config.cache_dir() / "print-watch")


def prune_frames(directory, *, keep=None, max_age_s=None, now=None):
    """Delete alert frames beyond the newest ``keep`` or older than ``max_age_s``. Never raises.

    The defaults are FRAME_KEEP and FRAME_KEEP_DAYS. Only ``*.jpg`` files
    directly in ``directory`` are considered. Returns the removed paths.
    """
    now = time.time() if now is None else now
    keep = FRAME_KEEP if keep is None else keep
    max_age_s = FRAME_KEEP_DAYS * 86400 if max_age_s is None else max_age_s
    try:
        frames = []
        for item in Path(directory).glob("*.jpg"):
            try:
                frames.append((item.stat().st_mtime, item))
            except OSError:
                continue
    except OSError:
        return []
    frames.sort(key=lambda pair: pair[0], reverse=True)
    removed = []
    for index, (mtime, item) in enumerate(frames):
        if index >= keep or now - mtime > max_age_s:
            try:
                item.unlink()
                removed.append(item)
            except OSError:
                pass  # another process removed it, or the folder is read only
    return removed


def write_frame(filename, image):
    """Save one alert frame where a chat post can attach it, then prune the folder."""
    target = frame_dir()
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{int(time.time())}-{Path(filename or 'print').stem}.jpg"
    path.write_bytes(image)
    prune_frames(target)
    return path


# Which printer families have the in-print camera watch, by adapter
# connection. Everything else gets idle plate checks only (auto light-off,
# the next-plate offer), never in-print verdicts. Auto-pause is the same
# opt-in decision (auto_pause_decision) on every watched family.
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


def coverage_report(cfg, api_key):
    """``[(ok, detail)]``: the camera watch per configured printer, for ``bot check``.

    Each printer's family coverage (:func:`watch_coverage`) and whether the
    watch can run at all (a vision route, :func:`watch_unavailable_reason`).
    Offline: no camera, endpoint or download is touched. A route pinned with
    ``vision.watch_source`` that cannot work is a failed row; an install with
    no route at all keeps the watch off by design, so it only says so.
    """
    section = (cfg or {}).get("visual_watch") or {}
    if section.get("enabled") is False:
        return [(True, "off (visual_watch.enabled is false)")]
    try:
        reason = watch_unavailable_reason(api_key, prefetch=False)
    except Exception as exc:
        reason = f"the vision route could not be checked ({exc})"
    pinned = local_vision.watch_source(cfg) in ("endpoint", "local")
    entries = [e for e in (cfg or {}).get("printers") or [] if isinstance(e, dict)]
    if not entries:
        entries = [{"id": "", "model": ((cfg or {}).get("printer") or {}).get("model")}]
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
            rows.append((not pinned, f"{label}{text}; not running: {reason}"))
        elif watched:
            mode = "auto-pause on" if watch_settings(cfg)["auto_pause_early"] else "alert-only"
            rows.append((True, f"{label}{text} ({mode})"))
        else:
            rows.append((True, f"{label}{text}"))
    return rows


def watch_coverage(connection):
    """``(watched, text)``: whether a printer family has the in-print watch, in plain words."""
    name = FAMILY_NAMES.get(connection, connection or "this printer family")
    scope = IN_PRINT_WATCH.get(connection)
    if scope:
        return True, f"{name}: in-print watch for {scope}"
    return False, f"{name}: no in-print watch (idle plate checks only)"

# Stage 2 stall wording (#455 item 4): a stall alert names its differential
# instead of implying one cause. The stall signal itself — layers advanced
# over an unchanged frame — is only ever evidence that *something* stopped
# changing; what stopped it is one of these possibilities, and the alert
# lists them rather than letting the vision verdict read as the diagnosis.
STALL_DIFFERENTIAL = ("the nozzle may be printing in air, the nozzle may be "
                      "blocked, the toolhead may be parked, or the camera "
                      "view may be dark")


def stall_alert_text(layer, verdict, detail):
    """The operator text for a stall alert, shared by every watch surface.

    Wording only: the caller decides whether an alert fires, exactly as
    before; this renders the stall case. The layer, the vision verdict
    and its detail ride along as evidence, after the named observation
    and its differential — the verdict is not presented as the cause.
    Where the check's own facts settle part of the differential (a dark
    frame, an air verdict), the text says which possibility they
    support and which they rule out, instead of listing all four flat.
    """
    confirmed, ruled_out = stall_candidates(verdict, detail)
    if confirmed and ruled_out:
        differential = (f"of the usual possibilities, the evidence supports: "
                        f"{confirmed}; ruled out by this check: "
                        f"{', '.join(ruled_out)}")
    elif confirmed:
        differential = (f"of the usual possibilities ({STALL_DIFFERENTIAL}), "
                        f"the vision verdict supports: {confirmed}")
    else:
        differential = STALL_DIFFERENTIAL
    return (f"Print may have stalled at layer {layer or 'unknown'}: the camera frame "
            f"stopped changing while the layer count advanced — {differential}. "
            f"Vision verdict: {verdict}; {detail or 'visual check needs operator review'}")


def stall_candidates(verdict, detail):
    """Refine the stall differential with facts the check already has.

    Returns ``(confirmed, ruled_out)``: possibility labels the evidence
    settles. A dark verdict confirms the dark view and rules out the
    rest (a dark frame's stillness says nothing about the print); an
    ``air`` verdict confirms printing in air; ``empty_idle`` while
    printing confirms the same fact in the local route's words. Any
    other verdict settles nothing — the full differential stands.
    """
    text = f"{verdict} {detail or ''}".lower()
    if verdict == "dark" or "dark" in text or "too dark" in text:
        return ("camera view dark", ("printing in air", "blocked nozzle", "toolhead parked"))
    if verdict in {"air", "empty_idle", "no"}:
        return ("printing in air", ())
    return ("", ())

# Camera stall alert presets for [visual_watch] sensitivity, 1 (quietest) to 5 (loudest).
# Level 3 is the original hard-coded behavior. diff is the mean 64x48 grayscale difference a
# frame needs to count as changed; stall_layers is how many layers an unchanged frame may span;
# vision_veto lets an "ok" or "safe" vision verdict cancel a pixel-diff stall alert.
VISUAL_SENSITIVITY = {
    1: {"diff": 1.5, "stall_layers": 8, "periodic_s": 1800, "vision_veto": True},
    2: {"diff": 2.0, "stall_layers": 5, "periodic_s": 1200, "vision_veto": True},
    3: {"diff": 3.0, "stall_layers": 3, "periodic_s": 900, "vision_veto": False},
    4: {"diff": 4.0, "stall_layers": 3, "periodic_s": 600, "vision_veto": False},
    5: {"diff": 5.0, "stall_layers": 2, "periodic_s": 450, "vision_veto": False},
}


def _visual_level(level=None):
    """The configured sensitivity level, 1 to 5 (3 when unset)."""
    if level is None:
        level = _config.cfg().get("visual_watch", {}).get("sensitivity", 3)
    level = int(level)
    if level not in VISUAL_SENSITIVITY:
        raise ValueError(f"visual_watch sensitivity must be 1 to 5, got {level!r}")
    return level


# Early-layer watch for risky prints (#913). A print is risky when it carries
# a slicer support finding (#911), a "print anyway" override, or an operator
# supports decision (#904). Risky prints are checked on every layer change for
# the first EARLY_LAYERS layers and at least every EARLY_INTERVAL_S seconds
# until then. Normal prints keep the sensitivity schedule above.
EARLY_LAYERS = 10
EARLY_INTERVAL_S = 120
# Prints that report no layer number (Klipper prints sliced without the
# SET_PRINT_STATS_INFO layer macro) are watched on a timer instead, as the
# Bambu watch already is. The first timed check waits for this much printing
# time (print_stats.print_duration, which only counts from the first
# extrusion), so heat-up and the purge line are never judged. The early-layer
# caution for a risky print then lasts EARLY_SECONDS_PER_LAYER per configured
# early layer of printing time (20 minutes at the default 10 layers).
LAYERLESS_FIRST_CHECK_S = 180
EARLY_SECONDS_PER_LAYER = 120
# An empty-bed verdict while print_stats says printing is suspicious, not
# healthy: it is re-checked this many seconds later (#913).
EMPTY_IDLE_RECHECK_S = 60
# Verdicts that count toward the opt-in early-layer auto-pause (#913).
AUTO_PAUSE_VERDICTS = frozenset({"spaghetti", "detached", "air"})
AUTO_PAUSE_STREAK = 2
WATCH_MODE_NORMAL = "normal"
WATCH_MODE_EARLY = "early_layer"

# Independent evidence (auto-pause and the local detector's escalation).
# A streak must be made of distinct views of the print, not of checks: in
# early-layer mode a check fires on every layer change, so two checks can
# land seconds apart on the same, or a frozen, camera frame. A suspicious
# frame only joins a streak when it is at least EVIDENCE_MIN_SPACING_S after
# the previous member, visibly differs from that member's frame, and is no
# older than FRAME_MAX_AGE_S when the decision is made. A frame that did not
# change is a camera fault (a frozen or repeating feed), never evidence.
EVIDENCE_MIN_SPACING_S = 60.0
FRAME_MAX_AGE_S = 90.0
# The comparison signature: a 32x24 grayscale thumbnail. Frames differ when
# at least FRAME_CHANGE_MIN_FRACTION of its cells moved by more than
# FRAME_CHANGE_PIXEL_DELTA grey levels (JPEG re-encoding noise of a frozen
# scene stays far below that after the downscale).
FRAME_SIGNATURE_SIZE = (32, 24)
FRAME_CHANGE_PIXEL_DELTA = 6
FRAME_CHANGE_MIN_FRACTION = 0.01


# The 64x48 grayscale thumbnail _frame_changed compares (the stall test).
FRAME_THUMB_SIZE = (64, 48)


class FrameFacts(NamedTuple):
    """What one camera check needs from a frame, from a single decode.

    ``dark`` is the frame_dark answer, ``thumb`` the FRAME_THUMB_SIZE
    grayscale bytes _frame_changed compares, ``signature`` the
    frame_signature bytes. ``thumb`` and ``signature`` are None when the
    frame cannot be decoded (and ``dark`` is then False, frame_dark's rule).
    """
    dark: bool
    thumb: bytes | None
    signature: bytes | None


# A check asks three questions of the same frame (is it dark, did it change
# since the last one, what is its signature), and the last check's frame is
# asked again as the "before" of the next comparison. Each used to decode the
# full-resolution JPEG on its own. The facts are computed once per distinct
# frame and kept for the few most recent frames (small: two thumbnails each).
_FACTS_CACHE_SIZE = 8
_facts_cache = {}
_facts_lock = threading.Lock()


def _frame_key(image):
    return hashlib.blake2b(image, digest_size=16).digest()


def _compute_facts(image):
    from PIL import Image, ImageStat
    import io as _io
    try:
        gray = Image.open(_io.BytesIO(image)).convert("L")
    except Exception:
        return FrameFacts(False, None, None)
    stat = ImageStat.Stat(gray)
    dark = stat.mean[0] < DARK_MEAN_MAX and stat.extrema[0][1] <= DARK_PIXEL_MAX
    return FrameFacts(bool(dark), gray.resize(FRAME_THUMB_SIZE).tobytes(),
                      gray.resize(FRAME_SIGNATURE_SIZE).tobytes())


def frame_facts(image):
    """The :class:`FrameFacts` of one JPEG frame; the frame is decoded at most once.

    Anything that is not bytes, or cannot be decoded, has no thumbnail or
    signature and is not dark. Never raises.
    """
    if isinstance(image, FrameFacts):
        return image
    if not isinstance(image, (bytes, bytearray)) or not image:
        return FrameFacts(False, None, None)
    image = bytes(image)
    key = _frame_key(image)
    with _facts_lock:
        cached = _facts_cache.get(key)
        if cached is not None:
            _facts_cache[key] = _facts_cache.pop(key)  # most recent last
            return cached
    facts = _compute_facts(image)
    with _facts_lock:
        _facts_cache[key] = facts
        while len(_facts_cache) > _FACTS_CACHE_SIZE:
            _facts_cache.pop(next(iter(_facts_cache)))
    return facts


def frame_signature(image):
    """The comparison signature of one frame (bytes), or None when it cannot be decoded."""
    return frame_facts(image).signature


def frames_differ(before, after):
    """True when two frame signatures show a visible change; None when either is unknown."""
    if not before or not after or len(before) != len(after):
        return None
    moved = sum(1 for a, b in zip(before, after) if abs(a - b) > FRAME_CHANGE_PIXEL_DELTA)
    return moved >= max(1, int(len(before) * FRAME_CHANGE_MIN_FRACTION))


class PauseEvidence(NamedTuple):
    """One member of an auto-pause streak: a distinct suspicious view of one print."""
    printer_id: object
    print_identity: object
    signature: bytes
    at: float


def independent_evidence(previous, *, printer_id, print_identity, signature, frame_age_s, now):
    """Whether a suspicious frame is a new, independent view after ``previous``.

    Returns ``(counts, why)``. ``previous`` is the streak's last member
    (None when the streak is empty). A frame of a different printer or
    print starts a new streak rather than extending this one; the caller
    sees that as ``why == "new print"``.
    """
    if not isinstance(frame_age_s, (int, float)) or frame_age_s > FRAME_MAX_AGE_S:
        return False, "the frame is too old to count"
    if not signature:
        return False, "the frame could not be compared"
    if previous is None:
        return True, "first view"
    if (previous.printer_id, previous.print_identity) != (printer_id, print_identity):
        return True, "new print"
    if frames_differ(previous.signature, signature) is not True:
        return False, ("the camera frame did not change since the last suspicious check "
                       "(a frozen or repeating camera), so it is not new evidence")
    if now - previous.at < EVIDENCE_MIN_SPACING_S:
        return False, (f"less than {EVIDENCE_MIN_SPACING_S:.0f}s after the last suspicious "
                       "check, so it is not an independent view")
    return True, "independent view"


def auto_pause_decision(members, *, enabled, already_paused, printer_id, print_identity, state,
                        verdict, frame_counted, frame_age_s, mode, in_early_window):
    """Decide the opt-in early-layer auto-pause (#913). Returns ``(pause, reason)``.

    Every input is explicit: ``members`` is the streak of independent
    suspicious views so far (:class:`PauseEvidence`, oldest first, this
    frame included when it counted), ``frame_counted`` says whether this
    frame joined the streak as independent evidence, ``state`` is the
    printer state at decision time, and ``frame_age_s`` is the age of the
    frame being judged. A pause needs AUTO_PAUSE_STREAK members, all of this
    printer and print, each at least EVIDENCE_MIN_SPACING_S after the one
    before with a visibly different frame, the newest being this fresh
    frame, while the printer still reports printing.
    """
    if not enabled:
        return False, "auto-pause is off"
    if already_paused:
        return False, "auto-pause already acted once on this print"
    if mode != WATCH_MODE_EARLY or not in_early_window:
        return False, "outside the early-layer window of a risky print"
    if verdict not in AUTO_PAUSE_VERDICTS:
        return False, f"verdict {verdict} does not count toward auto-pause"
    if state != "printing":
        return False, f"the printer is {state or 'in an unknown state'}, not printing"
    if not isinstance(frame_age_s, (int, float)) or frame_age_s > FRAME_MAX_AGE_S:
        return False, "the frame is too old to act on"
    members = list(members or ())
    if not frame_counted or not members:
        return False, "this frame is not new evidence"
    if any((m.printer_id, m.print_identity) != (printer_id, print_identity) for m in members):
        return False, "the streak spans another printer or print"
    for before, after in zip(members, members[1:]):
        if after.at - before.at < EVIDENCE_MIN_SPACING_S or frames_differ(before.signature, after.signature) is not True:
            return False, "the streak is not made of independent views"
    if len(members) < AUTO_PAUSE_STREAK:
        return False, f"{len(members)} of {AUTO_PAUSE_STREAK} independent suspicious views"
    return True, (f"{len(members)} independent suspicious views at least "
                  f"{EVIDENCE_MIN_SPACING_S:.0f}s apart in the early layers")


def risky_job(job):
    """Why this job gets the early-layer watch, or "" for a normal print (#913).

    Reads only recorded facts: #911 findings (``slice_findings`` /
    ``underside_findings``), a recorded "print anyway" override, a support
    warning shown for the slice (top level or under a provenance's
    ``slice_warnings`` with ``support_needed``), an operator supports
    decision (#904, including one settled through ``orientation``), and
    the configurable ``risky`` marker on the job.
    """
    if not isinstance(job, dict):
        return ""
    if job.get("risky") is True or job.get("watch_mode") == WATCH_MODE_EARLY:
        return "risky marker"
    for key in ("slice_findings", "underside_findings", "support_findings"):
        if job.get(key):
            return "slicer finding"
    for key in ("print_anyway", "warning_override", "override_print_anyway"):
        if job.get(key):
            return "print anyway override"
    decision = job.get("support_decision")
    if isinstance(decision, dict) and isinstance(decision.get("supports"), bool):
        return "supports decision"
    warnings = job.get("slice_warnings")
    if isinstance(warnings, dict) and (warnings.get("support") or warnings.get("support_needed")):
        return "support warning"
    # The shapes core/slice_warnings actually records: warnings live under
    # each artifact's provenance (the job's, and any derived plate's) with a
    # support_needed flag, and an operator can settle supports through the
    # orientation choice that carried the rotation (#904).
    orientation = job.get("orientation")
    if (isinstance(orientation, dict) and orientation.get("rotation")
            and isinstance(orientation.get("supports"), bool)):
        return "supports decision"
    provenances = [job.get("provenance")]
    provenances += [plate.get("provenance") for plate in job.get("derived_plates") or []
                    if isinstance(plate, dict)]
    for prov in provenances:
        if isinstance(prov, dict):
            found = prov.get("slice_warnings")
            if isinstance(found, dict) and found.get("support_needed"):
                return "support warning"
    return ""


def watch_settings(cfg=None):
    """The [visual_watch] early-layer and auto-pause settings with defaults."""
    if cfg is None:
        try:
            cfg = _config.cfg()
        except Exception:
            cfg = {}
    section = (cfg or {}).get("visual_watch") or {}
    def _int(key, default):
        value = section.get(key, default)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default
    return {"early_layers": _int("early_layers", EARLY_LAYERS),
            "early_interval_s": _int("early_interval_s", EARLY_INTERVAL_S),
            "auto_pause_early": section.get("auto_pause_early") is True}


def _visual_profile(level=None):
    return VISUAL_SENSITIVITY[_visual_level(level)]


# "Completely dark" thresholds for frame_dark: a frame counts as dark only
# when its mean grayscale luminance is below DARK_MEAN_MAX (of 255) AND its
# brightest pixel is at most DARK_PIXEL_MAX. Both bounds together keep a dim
# but real scene (a dark room still has a lit screen, a status LED, plate
# reflections) out of the dark class: this is for frames that carry no image
# at all — a black go2rtc placeholder, a covered lens, a dead camera.
DARK_MEAN_MAX = 4.0
DARK_PIXEL_MAX = 24


def frame_dark(image):
    """True when a JPEG frame is completely dark, i.e. it carries no usable image.

    A camera fact, not a print verdict (#476): a dark frame must never be
    judged by the vision endpoint or posted as a print alert. Anything that
    cannot be decoded is *not* dark — undecodable frames keep their existing
    handling (a vision failure alerts, it never silently passes).
    """
    try:
        return frame_facts(image).dark
    except Exception:
        return False


def _frame_changed(before, after, threshold=None):
    """Cheap whole-frame perceptual difference; false means visually unchanged.

    The mean grey-level difference of the two frames' FRAME_THUMB_SIZE
    thumbnails (:func:`frame_facts`, so neither frame is decoded again for
    this). Raises when either frame cannot be decoded, as before.
    """
    a, b = frame_facts(before).thumb, frame_facts(after).thumb
    if a is None or b is None:
        raise ValueError("a camera frame could not be decoded for comparison")
    if threshold is None:
        threshold = _visual_profile()["diff"]
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) >= threshold


def _strip_json_fence(text):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return raw.strip()


_vision_missing_logged = False


# The endpoint-locality rule lives with the AI routes (#818); this name stays for existing callers.
keyless_ok = ai_routes.keyless_ok


def chat_key_ready(api_key) -> bool:
    """Whether the chat route has what it needs: its key, or a local endpoint."""
    if ai_routes.overrides_route("small"):
        return ai_routes.ready("small")
    if keyless_ok(_config.service_url("vision_chat_url")):
        return True
    return bool(api_key())


def watch_unavailable_reason(api_key, *, prefetch=True):
    """Why no frame can be judged right now, in plain words; "" when one can.

    The judging routes are PRINT_VISION_URL, vision_chat_url with its key,
    or the built-in local failure detector (#455). ``vision.watch_source``
    narrows what counts (#455): "local" counts the detector (plus a
    calibrated plate reference for the idle checks), "endpoint" counts
    only the endpoint routes, and "auto" (the default) counts any of them.
    When the detector would be the route and its model is missing, this
    also starts the background model download (local_vision.prefetch_model),
    so the download never happens inside a camera check. ``prefetch=False``
    (the offline ``bot check``) only reports.
    """
    if not prefetch:
        def _start_download():
            return None
    else:
        _start_download = local_vision.prefetch_model
    source = local_vision.watch_source()
    if source == "local":
        if local_vision.detector_ready():
            return ""
        if local_empty.reference_ready():
            # Stage 1 (#455): a calibrated plate reference can answer the
            # idle checks the idle consumers gate on here, even when the
            # detector is not ready; the in-print watch under "local"
            # still judges with the detector only.
            return ""
        _start_download()
        return ("vision.watch_source is local but the local failure detector is not "
                f"ready: {local_vision.detector_status()[1]}")
    if os.environ.get("PRINT_VISION_URL"):
        return ""
    if (ai_routes.overrides_route("small") or _config.service_url("vision_chat_url")) and chat_key_ready(api_key):
        return ""
    if source == "auto" and local_vision.detector_ready():
        return ""
    if local_empty.reference_ready():
        # Stage 1 (#455): a calibrated plate reference can answer idle
        # plate checks, which is what the idle consumers gate on here.
        return ""
    if source == "endpoint":
        return ("vision.watch_source is endpoint but no vision endpoint is configured "
                "(set services.vision_chat_url with CLIPROXY_API_KEY, which a local or "
                "LAN endpoint does not need, or PRINT_VISION_URL)")
    if local_vision.enabled():
        _start_download()
        return f"the local failure detector is not ready: {local_vision.detector_status()[1]}"
    return ("no vision endpoint (set services.vision_chat_url with CLIPROXY_API_KEY, which a "
            "local or LAN endpoint does not need, or PRINT_VISION_URL, or enable the local "
            "detector with vision.local_detector_enabled)")


def vision_ready(api_key, log=None):
    """True when a frame can actually be judged (see :func:`watch_unavailable_reason`).

    Without a route, every verdict degrades to "uncertain", and "uncertain"
    alerts, so the watch would post a false alert at every check. The
    watch stays off instead, as docs/DOCKER.md describes, and says so once
    per outage: the log line repeats only after the watch was ready again.
    """
    global _vision_missing_logged
    reason = watch_unavailable_reason(api_key)
    if not reason:
        _vision_missing_logged = False
        return True
    if log is not None and not _vision_missing_logged:
        _vision_missing_logged = True
        log(f"camera watch skipped: {reason}")
    return False


class _VisionKeyMissing(Exception):
    """The chat vision route is configured but its API key is not."""


def _vision_answer(image, prompt, *, chat_url, model, api_key, context):
    """One vision call: POST ``image`` + ``prompt`` and return the endpoint's answer.

    This is the one transport both vision consumers share (#294's rule):
    ``visual_verdict`` judges a frame with it, and ``visual_extract`` reads a
    spool label with it (#514). ``chat_url`` and ``api_key`` are zero-argument
    callables (so a missing setting fails inside the call exactly as the
    verdict path has always behaved), ``model`` and ``context`` are the
    caller's strings. A PRINT_VISION_URL endpoint, when set, takes the simple
    {image, prompt} shape (the operator-facing contract is documented in
    docs/SETTINGS.md, "Custom vision endpoint") and its response body is
    parsed as JSON; otherwise the OpenAI-compatible chat endpoint is used
    with the env-named key and its message content is parsed as JSON when it
    parses, else returned as the raw string. Transport and parse failures
    raise; a missing chat key raises _VisionKeyMissing.
    """
    endpoint = os.environ.get("PRINT_VISION_URL")
    if endpoint:
        payload = json.dumps({"image": base64.b64encode(image).decode(), "prompt": prompt}).encode()
        req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())
    key = api_key()
    try:
        url = chat_url()
    except Exception:
        # An unset services.vision_chat_url: [ai.small] may still name the
        # endpoint, and the call reports a missing one itself.
        url = ""
    if not ai_routes.overrides_route("small") and not key and not keyless_ok(url):
        # The long-standing rule for the legacy vision settings: no key for a hosted
        # (or unset) endpoint is the missing-key result, before any route lookup.
        raise _VisionKeyMissing("vision key is not configured")
    try:
        content, served_by = ai_routes.call(
            "small", [{"role": "user", "content": context + str(prompt)}], images=[image],
            compat=ai_routes.Compat(url, model, key, timeout_s=60,
                                   slot="services.vision_chat_url"), purpose="vision",
            max_tokens=200, temperature=0)
    except ai_routes.AIKeyMissing as exc:
        raise _VisionKeyMissing("vision key is not configured") from exc
    logger.debug("vision answer served by %s", served_by)
    content = _strip_json_fence(content)
    try:
        return json.loads(content)
    except ValueError:
        return content


def _detector_verdict(image, streak=None):
    """The built-in local detector's verdict (#455), fail-soft.

    The detector's own failures degrade exactly like a broken endpoint's:
    ("uncertain", ...) with the real error logged. ``streak`` is the
    calling print watch's per-print :class:`local_vision.FailureStreak`
    (temporal escalation); callers that are not a print watch (idle plate
    checks) pass none and get single-frame verdicts.
    """
    try:
        return local_vision.local_verdict(image, streak=streak)
    except Exception as exc:
        logger.warning("vision check failed via local detector: %s: %s",
                       type(exc).__name__, exc)
        return "uncertain", VISION_FAILED_DETAIL


def _local_verdict(image, streak=None):
    """The local detector as the auto route's stand-in (#455), or None
    when the detector is not enabled.

    Consulted only when no endpoint route is configured — the endpoint
    routes keep their precedence untouched, and a configured-but-failing
    endpoint still degrades generically rather than falling through.
    """
    if not local_vision.enabled():
        return None
    return _detector_verdict(image, streak)


def _chat_route_configured(api_key):
    """Whether the chat endpoint route is fully configured — the URL set
    and its key present, the same rule ``vision_ready`` applies. Every
    caller injects a chat_url built on ``services.vision_chat_url``, so
    the setting is the honest test; a key that cannot even be read counts
    as configured (endpoint semantics win ties)."""
    if not (ai_routes.overrides_route("small") or _config.service_url("vision_chat_url")):
        return False
    try:
        return chat_key_ready(api_key)
    except Exception:
        return True


def _local_fallback_verdict(image, streak=None):
    """The local detector as a backup for a failed endpoint call (#913).

    Used only in early-layer mode, and only when the detector model file is
    already present: this never downloads, and it does not need
    ``vision.local_detector_enabled`` (that switch keeps its meaning, the
    detector as the primary route). Returns None when it cannot stand in.
    """
    try:
        if not local_vision.model_path().is_file():
            return None
        verdict, detail = local_vision.local_verdict(image, streak=streak)
    except Exception as exc:
        logger.warning("local detector fallback failed: %s: %s", type(exc).__name__, exc)
        return None
    return verdict, f"local detector fallback: {detail}"[:300]


# The idle plate-check prompt (auto light-off #516, next-plate offer
# #621). The Moonraker listener and the family driver both judge idle
# frames with this exact text (light_control.IDLE_CHECK_PROMPT); it is
# kept here so the idle-check call sites share one literal.
IDLE_CHECK_PROMPT_TEXT = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"


def visual_verdict(image, prompt, *, chat_url, model, api_key, context, local_streak=None,
                   local_fallback=False, idle=False):
    """Judge one frame. Returns (verdict, detail).

    Route order: PRINT_VISION_URL, then the chat endpoint
    (``services.vision_chat_url``), then — only when neither endpoint
    route is configured — the built-in local failure detector (#455).
    This is the listener's verdict call with its process-specific values
    injected (see ``_vision_answer`` for the shared transport). Any failure
    degrades to ("uncertain", ...) — an uncertain frame alerts, it never
    silently passes. Label reads (``visual_extract``) never use the local
    detector: it detects failures, it cannot read.

    ``local_streak`` is the calling print watch's per-print temporal
    escalation state for the local route; the endpoint routes ignore it.
    ``local_fallback`` (early-layer mode, #913) lets a failed configured
    endpoint fall back to the local detector when its model is present.

    ``vision.watch_source`` overrides the route order (#455): "local"
    judges with the detector directly and never calls an endpoint;
    "endpoint" never consults the detector for the watch (including the
    #913 fallback); "auto" (the default) is the order above.

    ``idle`` marks an idle plate check (the auto light-off and
    next-plate consumers). Only an idle call may be answered by the
    Stage 1 plate-reference producer (``local_empty``), and only at the
    point the local detector would be consulted — no endpoint route
    configured — so an endpoint's answer always wins and the in-print
    watch (which never passes ``idle``) can never receive a local
    ``empty_idle``.
    """
    source = local_vision.watch_source()
    if source == "local":
        if idle:
            empty = local_empty.idle_verdict(image)
            if empty is not None:
                return empty
        return _detector_verdict(image, local_streak)
    endpoint = os.environ.get("PRINT_VISION_URL")
    try:
        parsed = _vision_answer(image, prompt, chat_url=chat_url, model=model,
                                api_key=api_key, context=context)
        if not isinstance(parsed, dict):
            raise TypeError(f"vision answer is not a JSON object ({type(parsed).__name__})")
    except _VisionKeyMissing:
        if source == "auto":
            if idle:
                empty = local_empty.idle_verdict(image)
                if empty is not None:
                    return empty
            local = _local_verdict(image, local_streak)
            if local is not None:
                return local
        return "uncertain", "vision key is not configured"
    except Exception as exc:
        # The chat route can also fail simply by being half-configured
        # (its URL unset raises inside the call when a key exists): that
        # is "no endpoint route configured", so the local detector stands
        # in (auto source only — "endpoint" never consults it). A
        # configured endpoint that fails does NOT fall through.
        if source == "auto" and not endpoint and not _chat_route_configured(api_key):
            if idle:
                empty = local_empty.idle_verdict(image)
                if empty is not None:
                    return empty
            local = _local_verdict(image, local_streak)
            if local is not None:
                return local
        elif local_fallback and source == "auto":
            # #913: in early-layer mode a failed or timed-out endpoint call
            # falls back to the local detector when its model is present.
            logger.warning("vision check failed via %s: %s: %s; trying the local detector",
                           "PRINT_VISION_URL" if endpoint else "services.vision_chat_url",
                           type(exc).__name__, exc)
            local = _local_fallback_verdict(image, local_streak)
            if local is not None:
                return local
        # The returned detail stays generic on purpose (#467: raw error text
        # does not belong in a chat alert), but the underlying error is
        # logged: without it a mis-configured endpoint — above all a custom
        # PRINT_VISION_URL (#501) — is undiagnosable from the alert alone.
        logger.warning("vision check failed via %s: %s: %s",
                       "PRINT_VISION_URL" if endpoint else "services.vision_chat_url",
                       type(exc).__name__, exc)
        return "uncertain", VISION_FAILED_DETAIL
    verdict = str(parsed.get("verdict", "uncertain")).casefold()
    if verdict not in VISUAL_VERDICTS:
        # Model-output drift fails closed: an unrecognized verdict is
        # "uncertain" (which alerts), never a value a caller's
        # suspicious-verdict set silently treats as safe.
        verdict = "uncertain"
    return verdict, str(parsed.get("reason", ""))[:300]


def visual_extract(image, prompt, *, chat_url, model, api_key, context):
    """Ask the configured vision endpoint to extract data from one image (#514).

    The same call shape as ``visual_verdict`` — image bytes plus a JSON-only
    prompt, with the caller's values injected — but the answer is data, not a
    verdict: the endpoint's parsed JSON (a dict when it answered a JSON
    object, the raw string when its answer was not JSON), or None when the
    call could not be completed at all. Never raises; the failure is logged
    with its route, like the verdict path. Interpreting the answer — and
    refusing it when it is unusable — is the caller's job (spool_label).
    """
    try:
        return _vision_answer(image, prompt, chat_url=chat_url, model=model,
                              api_key=api_key, context=context)
    except Exception as exc:
        logger.warning("vision extraction failed via %s: %s: %s",
                       "PRINT_VISION_URL" if os.environ.get("PRINT_VISION_URL")
                       else "services.vision_chat_url",
                       type(exc).__name__, exc)
        return None


class VisualPrintWatch:
    """Pure trigger policy for periodic visual checks; never controls the printer."""
    def __init__(self, sensitivity=None, *, early_layers=EARLY_LAYERS, early_interval_s=EARLY_INTERVAL_S):
        self.sensitivity = _visual_level(sensitivity)
        self.profile = VISUAL_SENSITIVITY[self.sensitivity]
        self.early_layers = early_layers
        self.early_interval_s = early_interval_s
        self.reset()

    def set_mode(self, mode):
        """Pick the schedule for this print: normal or early_layer (#913)."""
        self.mode = WATCH_MODE_EARLY if mode == WATCH_MODE_EARLY else WATCH_MODE_NORMAL

    def in_early_window(self, layer, elapsed_s=None):
        """True while a risky print is inside its first early_layers layers.

        A print that reports no layer (``layer`` None) is judged by printing
        time instead: the first early_layers * EARLY_SECONDS_PER_LAYER
        seconds. ``elapsed_s`` defaults to the time the last timer beat saw.
        """
        if self.mode != WATCH_MODE_EARLY:
            return False
        if isinstance(layer, int):
            return 1 <= layer <= self.early_layers
        if layer is None:
            elapsed = self.last_elapsed_s if elapsed_s is None else elapsed_s
            return (isinstance(elapsed, (int, float))
                    and 0 <= elapsed < self.early_layers * EARLY_SECONDS_PER_LAYER)
        return False

    def failure_alert_due(self, now):
        """Count one watch failure for this print; True when it should alert now."""
        return self.failures.failed(now, self.profile["periodic_s"])

    def note_empty_idle(self, now):
        """An empty-bed verdict while printing: re-check soon (#913).

        Only the first pending verdict sets the deadline: early layers can
        take less than the recheck interval, and letting every repeat push
        the deadline out would postpone the recheck (and its alert) for as
        long as the bed keeps reading empty (Codex, #920).
        """
        if not self.recheck_at:
            self.recheck_at = now + EMPTY_IDLE_RECHECK_S

    def note_verdict(self, verdict, layer, *, printer_id=None, print_identity=None,
                     signature=None, frame_age_s=None, now=None):
        """Track consecutive independent auto-pause views inside the early window.

        Returns the streak length. Any other verdict, or a check outside the
        early window, resets it: only consecutive suspicious views count. A
        suspicious frame joins the streak only as independent evidence
        (:func:`independent_evidence`): too soon, unchanged, stale or
        undecodable frames neither extend nor reset it. ``self.last_evidence``
        says why the last suspicious frame did or did not count.
        """
        now = time.time() if now is None else now
        if verdict in AUTO_PAUSE_VERDICTS and self.in_early_window(layer):
            previous = self.pause_members[-1] if self.pause_members else None
            counts, why = independent_evidence(
                previous, printer_id=printer_id, print_identity=print_identity,
                signature=signature, frame_age_s=frame_age_s, now=now)
            self.last_evidence = why
            self.last_counted = counts
            if counts:
                member = PauseEvidence(printer_id, print_identity, signature, now)
                self.pause_members = ([member] if why == "new print"
                                      else (self.pause_members + [member])[-AUTO_PAUSE_STREAK * 4:])
        else:
            self.pause_members = []
            self.last_evidence = ""
            self.last_counted = False
        self.suspicious_streak = len(self.pause_members)
        return self.suspicious_streak

    def tick(self, layer, now, *, elapsed_s=None):
        """Time-based schedule, independent of layer events (#913).

        Moonraker sends print_stats only when it changes, so a 40-minute
        layer produced no event and therefore no periodic check. A timer
        calls this; it answers the checks owed by the clock alone.

        ``layer`` None means the print reports no layer number at all: the
        watch then runs on the clock alone (see :meth:`_layerless_tick`),
        with ``elapsed_s`` the printing time so far when the caller knows it.
        """
        if layer is None:
            return self._layerless_tick(now, elapsed_s)
        if not isinstance(layer, int) or layer < 1:
            return None
        if self.recheck_at and now >= self.recheck_at:
            self.recheck_at = 0.0
            self.last_check_at = now
            return "empty_idle_recheck"
        if self.in_early_window(layer) and now - self.last_check_at >= self.early_interval_s:
            self.last_check_at = now
            return "early_periodic"
        if now - self.last_check_at >= self.profile["periodic_s"]:
            self.last_check_at = now
            return "periodic"
        return None

    def _layerless_tick(self, now, elapsed_s):
        """The clock-only schedule for a print that reports no layer number.

        Without layers the layer 2 / layer 5 / every-layer checks cannot
        run, so the watch checks once after LAYERLESS_FIRST_CHECK_S of
        printing ("layerless_start"), then on the sensitivity profile's
        periodic interval; a risky print is checked every early_interval_s
        while its printing time is inside the early window. ``elapsed_s``
        is print_stats.print_duration when known; without it the watch
        counts from the first beat that saw the layerless print.
        """
        if not isinstance(elapsed_s, (int, float)) or isinstance(elapsed_s, bool):
            if self.layerless_since is None:
                self.layerless_since = now
            elapsed_s = now - self.layerless_since
        self.last_elapsed_s = float(elapsed_s)
        early = self.in_early_window(None, elapsed_s)
        first = (min(self.early_interval_s, LAYERLESS_FIRST_CHECK_S)
                 if self.mode == WATCH_MODE_EARLY else LAYERLESS_FIRST_CHECK_S)
        if elapsed_s < first:
            return None
        if self.recheck_at and now >= self.recheck_at:
            self.recheck_at = 0.0
            self.last_check_at = now
            return "empty_idle_recheck"
        if not self.layerless:
            self.layerless = True
            self.last_check_at = now
            return "layerless_start"
        if early and now - self.last_check_at >= self.early_interval_s:
            self.last_check_at = now
            return "early_periodic"
        if now - self.last_check_at >= self.profile["periodic_s"]:
            self.last_check_at = now
            return "periodic"
        return None

    def reset(self):
        self.last_layer = None
        self.last_check_at = 0.0
        self.last_snapshot = None
        self.last_snapshot_layer = 0
        self.progress_seen_layer = 0
        self.alerted_stall_layer = None
        self.layer2_checked = False
        self.layer5_checked = False
        self.last_alert_at = 0.0
        # The one dark-camera notice for this print (#476): dark frames are
        # a camera problem, so the operator hears about them at most once
        # per print, never as a repeating print alert.
        self.dark_noticed = False
        self.mode = WATCH_MODE_NORMAL
        self.early_layer_seen = 0
        self.recheck_at = 0.0
        self.suspicious_streak = 0
        self.pause_members = []
        self.last_evidence = ""
        self.last_counted = False
        self.auto_paused = False
        # This print's camera-watch failure episode (FailureEpisode): failed
        # snapshots, check timeouts and verdict calls that could not judge
        # share one throttle, at most one alert per periodic interval, and
        # one notice when checks work again.
        self.failures = FailureEpisode()
        # The clock-only schedule for a print without layer numbers: whether
        # it has started, the one notice saying so, the fallback clock, and
        # the printing time the last beat saw (for the early window).
        self.layerless = False
        self.layerless_noticed = False
        self.layerless_since = None
        self.last_elapsed_s = None

    def due(self, layer, now, *, snapshot_changed=None):
        if not isinstance(layer, int) or layer < 1:
            return None
        if self.in_early_window(layer) and layer > self.early_layer_seen:
            # Risky print: every layer change in the early window is checked.
            self.early_layer_seen = layer
            self.layer2_checked = self.layer2_checked or layer >= 2
            self.layer5_checked = self.layer5_checked or layer >= 5
            self.last_check_at = now
            return "early_layer"
        if layer >= 2 and not self.layer2_checked:
            self.layer2_checked = True
            self.last_check_at = now
            return "layer2"
        if layer >= 5 and not self.layer5_checked:
            self.layer5_checked = True
            self.last_check_at = now
            return "layer5"
        stall_layers = self.profile["stall_layers"]
        if now - self.last_check_at >= self.profile["periodic_s"]:
            self.last_check_at = now
            reason = "stalled_layers" if snapshot_changed is False and self.last_layer is not None and layer - self.last_layer >= stall_layers else "periodic"
            return reason
        if snapshot_changed is False and self.last_snapshot_layer and layer - self.last_snapshot_layer >= stall_layers and self.alerted_stall_layer is None:
            self.alerted_stall_layer = layer
            self.last_layer = layer
            self.last_check_at = now
            return "stalled_layers"
        if snapshot_changed is None and layer >= 5 and self.alerted_stall_layer is None and self.last_snapshot_layer and layer - self.last_snapshot_layer >= stall_layers:
            self.last_layer = layer
            return "layer_progress"
        return None

    def record(self, layer, now, snapshot=None):
        if isinstance(layer, int):
            self.last_layer = layer
        self.last_check_at = now
        self.last_snapshot = snapshot
        if isinstance(layer, int):
            self.last_snapshot_layer = layer
