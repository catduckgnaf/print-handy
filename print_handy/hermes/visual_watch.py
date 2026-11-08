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
import json
import logging
import os
import urllib.request

from .. import ai_routes
from . import _config, local_empty, local_vision

logger = logging.getLogger(__name__)

VISUAL_VERDICTS = {"ok", "air", "spaghetti", "detached", "empty_idle", "uncertain"}
VISUAL_ALERT_VERDICTS = {"air", "spaghetti", "detached", "uncertain"}
VISUAL_HEALTHY_VERDICTS = {"ok", "safe"}
VISUAL_SUSPICIOUS_VERDICTS = {"air", "spaghetti", "detached", "uncertain", "no", "unsafe"}

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
# An empty-bed verdict while print_stats says printing is suspicious, not
# healthy: it is re-checked this many seconds later (#913).
EMPTY_IDLE_RECHECK_S = 60
# Verdicts that count toward the opt-in early-layer auto-pause (#913).
AUTO_PAUSE_VERDICTS = frozenset({"spaghetti", "detached", "air"})
AUTO_PAUSE_STREAK = 2
WATCH_MODE_NORMAL = "normal"
WATCH_MODE_EARLY = "early_layer"


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
        from PIL import Image, ImageStat
        import io as _io
        gray = Image.open(_io.BytesIO(image)).convert("L")
        stat = ImageStat.Stat(gray)
        return stat.mean[0] < DARK_MEAN_MAX and stat.extrema[0][1] <= DARK_PIXEL_MAX
    except Exception:
        return False


def _frame_changed(before, after, threshold=None):
    """Cheap whole-frame perceptual difference; false means visually unchanged."""
    from PIL import Image, ImageChops, ImageStat
    import io as _io
    a = Image.open(_io.BytesIO(before)).convert("L").resize((64, 48))
    b = Image.open(_io.BytesIO(after)).convert("L").resize((64, 48))
    if threshold is None:
        threshold = _visual_profile()["diff"]
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] >= threshold


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


def vision_ready(api_key, log=None):
    """True when a frame can actually be judged: PRINT_VISION_URL, vision_chat_url with
    its key, or the built-in local failure detector (#455).

    Without one, every verdict degrades to "uncertain", and "uncertain"
    alerts, so the watch would post a false alert at every check. The
    watch stays off instead, as docs/DOCKER.md describes, and says so once.
    """
    global _vision_missing_logged
    if os.environ.get("PRINT_VISION_URL"):
        return True
    if (ai_routes.overrides_route("small") or _config.service_url("vision_chat_url")) and chat_key_ready(api_key):
        return True
    if local_vision.detector_ready():
        return True
    if local_empty.reference_ready():
        # Stage 1 (#455): a calibrated plate reference can answer idle
        # plate checks, which is what the idle consumers gate on here.
        return True
    if log is not None and not _vision_missing_logged:
        _vision_missing_logged = True
        log("camera watch skipped: no vision endpoint (set services.vision_chat_url with "
            "CLIPROXY_API_KEY, which a local or LAN endpoint does not need, or "
            "PRINT_VISION_URL, or enable the local detector with "
            "vision.local_detector_enabled)")
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
            compat=ai_routes.Compat(url, model, key, timeout_s=60), purpose="vision",
            max_tokens=200, temperature=0)
    except ai_routes.AIKeyMissing as exc:
        raise _VisionKeyMissing("vision key is not configured") from exc
    logger.debug("vision answer served by %s", served_by)
    content = _strip_json_fence(content)
    try:
        return json.loads(content)
    except ValueError:
        return content


def _local_verdict(image, streak=None):
    """The built-in local detector's verdict (#455), or None when it is not enabled.

    Consulted only when no endpoint route is configured — the endpoint
    routes keep their precedence untouched, and a configured-but-failing
    endpoint still degrades generically rather than falling through. The
    detector's own failures degrade exactly like a broken endpoint's:
    ("uncertain", ...) with the real error logged.

    ``streak`` is the calling print watch's per-print
    :class:`local_vision.FailureStreak` (temporal escalation); callers
    that are not a print watch (idle plate checks) pass none and get
    single-frame verdicts.
    """
    if not local_vision.enabled():
        return None
    try:
        return local_vision.local_verdict(image, streak=streak)
    except Exception as exc:
        logger.warning("vision check failed via local detector: %s: %s",
                       type(exc).__name__, exc)
        return "uncertain", "the vision check could not be completed"


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

    ``idle`` marks an idle plate check (the auto light-off and
    next-plate consumers). Only an idle call may be answered by the
    Stage 1 plate-reference producer (``local_empty``), and only at the
    point the local detector would be consulted — no endpoint route
    configured — so an endpoint's answer always wins and the in-print
    watch (which never passes ``idle``) can never receive a local
    ``empty_idle``.
    """
    endpoint = os.environ.get("PRINT_VISION_URL")
    try:
        parsed = _vision_answer(image, prompt, chat_url=chat_url, model=model,
                                api_key=api_key, context=context)
        if not isinstance(parsed, dict):
            raise TypeError(f"vision answer is not a JSON object ({type(parsed).__name__})")
    except _VisionKeyMissing:
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
        # in. A configured endpoint that fails does NOT fall through.
        if not endpoint and not _chat_route_configured(api_key):
            if idle:
                empty = local_empty.idle_verdict(image)
                if empty is not None:
                    return empty
            local = _local_verdict(image, local_streak)
            if local is not None:
                return local
        elif local_fallback:
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
        return "uncertain", "the vision check could not be completed"
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

    def in_early_window(self, layer):
        """True while a risky print is inside its first early_layers layers."""
        return (self.mode == WATCH_MODE_EARLY and isinstance(layer, int)
                and 1 <= layer <= self.early_layers)

    def note_empty_idle(self, now):
        """An empty-bed verdict while printing: re-check soon (#913).

        Only the first pending verdict sets the deadline: early layers can
        take less than the recheck interval, and letting every repeat push
        the deadline out would postpone the recheck (and its alert) for as
        long as the bed keeps reading empty (Codex, #920).
        """
        if not self.recheck_at:
            self.recheck_at = now + EMPTY_IDLE_RECHECK_S

    def note_verdict(self, verdict, layer):
        """Track consecutive auto-pause verdicts inside the early window.

        Returns the streak length. Any other verdict, or a check outside the
        early window, resets it: only consecutive suspicious frames count.
        """
        if verdict in AUTO_PAUSE_VERDICTS and self.in_early_window(layer):
            self.suspicious_streak += 1
        else:
            self.suspicious_streak = 0
        return self.suspicious_streak

    def tick(self, layer, now):
        """Time-based schedule, independent of layer events (#913).

        Moonraker sends print_stats only when it changes, so a 40-minute
        layer produced no event and therefore no periodic check. A timer
        calls this; it answers the checks owed by the clock alone.
        """
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
        self.auto_paused = False

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
