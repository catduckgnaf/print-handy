"""Local empty-plate producer (#455 item 4, Stage 1): ``empty_idle``
from a calibrated plate reference.

The failure detector (:mod:`local_vision`) can never answer
``empty_idle`` — it detects failures, not an empty plate. The one
deterministic signal the item-4 scoping found is a difference against a
per-install empty-plate reference frame: the operator captures the
plate empty (the frame is shown back and confirmed before it takes
effect), and later idle frames are compared against it with
illumination normalization. Only a frame that matches the reference
closely is ``empty_idle``; anything else is simply *not produced*
locally — this module returns None rather than guessing, and the
caller's normal route (endpoint, then the failure detector) answers as
it does today.

Contract, from the scoping gates:

- Idle consumers only. The producer is consulted for idle plate checks
  (auto light-off #516, the next-plate offer #621) through
  ``visual_watch``'s idle path; the in-print watch never sees it, so the
  in-print empty recheck stays endpoint-only.
- Default off: ``vision.local_empty_enabled`` is a separate switch from
  ``vision.local_detector_enabled`` — a different mechanism with a
  different failure mode. It also respects ``vision.watch_source``
  (PR #1034's routing rule): never local under ``endpoint``; eligible
  under ``local``; under ``auto`` only when no endpoint route is
  configured (the routing decision lives in ``visual_watch``).
- Fail-soft: no reference, a stale reference (older than
  :data:`REFERENCE_MAX_AGE_DAYS`), an unreadable reference, a dark
  frame, or any comparison error produces nothing, with the reason
  visible in ``bot check`` via :func:`reference_status`.

The calibration flow is surface-agnostic — :func:`begin_calibration`
stages a captured frame (kept apart from the live reference until
confirmed), :func:`confirm_calibration` promotes it,
:func:`discard_calibration` drops it — so the bot command in
``plugins/print-feedback`` and a future Web UI page call the same
routines. Storage sits with the install's state, alongside the detector
model directory (``<state_home>/models/``).
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from . import _config

REFERENCE_FILENAME = "plate-reference.jpg"
PENDING_FILENAME = "plate-reference.pending.jpg"
META_FILENAME = "plate-reference.json"

# A reference older than this is stale: the camera or plate has likely
# moved since capture, so the producer stops answering until the
# operator recalibrates. Age and drift health surface in bot check.
REFERENCE_MAX_AGE_DAYS = 30

# Difference budget, in 0-255 grayscale units after illumination
# fitting (a least-squares affine gain+offset fit of the reference onto
# the frame's lighting, see difference()): a cell of the comparison
# grid counts as changed when its fitted mean moves more than
# CELL_DELTA, and the frame is "not the reference" when more than
# CHANGED_FRACTION of cells changed. The fit removes global lighting
# and exposure shifts; what remains is structure — a part on the
# plate. The Stage 1 gate (zero false-empty across >= 300 part-present
# frames, >= 95% true-empty across lighting states) is measured
# against these constants in tests/hermes/test_local_empty.py on
# synthetic fixtures.
GRID_COLS = 24
GRID_ROWS = 18
CELL_DELTA = 14.0
CHANGED_FRACTION = 0.012


class LocalEmptyError(Exception):
    """The plate-reference producer cannot judge this frame."""


# --- Settings -----------------------------------------------------------------


def empty_enabled(cfg=None) -> bool:
    cfg = _config.cfg() if cfg is None else cfg
    return bool((cfg.get("vision") or {}).get("local_empty_enabled", False))


def reference_dir(cfg=None) -> Path:
    """Where the reference lives: the models directory under the Hermes
    home, alongside the detector weights (the install's state)."""
    cfg = _config.cfg() if cfg is None else cfg
    home = Path(os.path.expanduser(str(cfg["paths"]["state_home"])))
    return home / "models"


def reference_path(cfg=None) -> Path:
    return reference_dir(cfg) / REFERENCE_FILENAME


def _pending_path(cfg=None) -> Path:
    return reference_dir(cfg) / PENDING_FILENAME


def _meta_path(cfg=None) -> Path:
    return reference_dir(cfg) / META_FILENAME


# --- Reference storage ----------------------------------------------------------


def _read_meta(cfg=None) -> dict | None:
    try:
        data = json.loads(_meta_path(cfg).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _reference_age_s(cfg=None, now=None) -> float | None:
    meta = _read_meta(cfg)
    if not meta:
        return None
    try:
        captured = float(meta.get("captured_at"))
    except (TypeError, ValueError):
        return None
    return (time.time() if now is None else now) - captured


def begin_calibration(cfg, image: bytes, now=None) -> Path:
    """Stage a captured frame as the pending reference.

    The frame is validated as a decodable image and stored apart from
    the live reference: it judges nothing until
    :func:`confirm_calibration` promotes it. Returns the pending path
    so the calling surface can show the exact frame back. Raises
    :class:`LocalEmptyError` for an unusable frame.
    """
    _grid(image)  # raises on undecodable input
    cfg = _config.cfg() if cfg is None else cfg
    pending = _pending_path(cfg)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_bytes(image)
    return pending


def confirm_calibration(cfg, image: bytes, now=None, force: bool = False) -> bool:
    """Promote the staged frame to the live reference.

    ``image`` must be the frame the operator was shown (the staged
    bytes); a mismatch — the pending file changed under the prompt —
    refuses and keeps the previous reference. So does a staged frame
    that contradicts the reference it would replace: when an earlier
    reference exists and the staged frame differs from it as much as a
    plate with something on it, the capture cannot be an empty plate
    and is refused (the staged frame stays for a retake). A deliberate
    recalibration — the operator replaced the plate or moved the
    camera, so the new empty plate genuinely differs from the old
    reference — passes ``force=True``: that is exactly what
    recalibration is for, and the shown-frame confirmation remains
    the guard. Returns True when the reference took effect.
    """
    cfg = _config.cfg() if cfg is None else cfg
    pending = _pending_path(cfg)
    try:
        staged = pending.read_bytes()
    except Exception:
        return False
    if staged != image:
        return False
    grid = _grid(staged)
    try:
        previous = _load_reference(cfg)
    except LocalEmptyError:
        previous = None
    if not force and previous is not None and not matches_grid(previous, grid):
        return False
    target = reference_path(cfg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(staged)
    meta = {"captured_at": time.time() if now is None else now,
            "format": "print-handy-plate-reference/1"}
    _meta_path(cfg).write_text(json.dumps(meta), encoding="utf-8")
    pending.unlink(missing_ok=True)
    return True


def discard_calibration(cfg=None) -> None:
    """Drop a staged capture; the live reference (if any) is untouched."""
    cfg = _config.cfg() if cfg is None else cfg
    _pending_path(cfg).unlink(missing_ok=True)


def _load_reference(cfg) -> bytes:
    """The live reference bytes, or raise LocalEmptyError naming why not."""
    path = reference_path(cfg)
    if not path.is_file():
        raise LocalEmptyError("no plate reference captured")
    age = _reference_age_s(cfg)
    if age is None:
        raise LocalEmptyError("the plate reference record is unreadable")
    if age > REFERENCE_MAX_AGE_DAYS * 86400:
        raise LocalEmptyError(
            f"the plate reference is {age / 86400:.0f} days old "
            f"(over the {REFERENCE_MAX_AGE_DAYS}-day limit); recalibrate")
    return path.read_bytes()


# --- Comparison -----------------------------------------------------------------


def _grid(image: bytes):
    """(cell means, frame mean) for one JPEG frame on the comparison grid.

    The resize uses an area-averaging filter: each grid cell averages
    the pixels it covers (BOX), which is the same cell mean an
    arithmetic block average computes — and far cheaper than resizing
    with a default interpolation kernel.
    """
    from PIL import Image
    try:
        frame = Image.open(io.BytesIO(image)).convert("L")
    except Exception as exc:
        raise LocalEmptyError(f"frame is not a decodable image: {exc}") from exc
    small = frame.resize((GRID_COLS, GRID_ROWS), Image.Resampling.BOX)
    cells = list(small.getdata())
    return cells, sum(cells) / len(cells)


def _fit_gain_shift(cells, ref_cells):
    """(gain, shift) mapping the reference's illumination onto the frame's.

    Least squares over the grid cells (closed form, robust-checked by
    the caller): the affine map between a scene under one lighting
    state and the same scene under another. A global lighting change
    is gain (lamp dimmer/exposure) and/or shift (ambient offset); a
    part is a local change no global map can fit away.
    """
    n = len(ref_cells)
    mean_r = sum(ref_cells) / n
    mean_c = sum(cells) / n
    var_r = sum((r - mean_r) ** 2 for r in ref_cells)
    if var_r <= 0:
        return 1.0, mean_c - mean_r
    cov = sum((r - mean_r) * (c - mean_c) for r, c in zip(ref_cells, cells))
    gain = cov / var_r
    return gain, mean_c - gain * mean_r


def _changed_fraction(cells, ref_cells) -> float:
    gain, shift = _fit_gain_shift(cells, ref_cells)
    if not (0.25 <= gain <= 4.0):
        gain, shift = 1.0, (sum(cells) - sum(ref_cells)) / len(cells)
    changed = sum(1 for cell, ref in zip(cells, ref_cells)
                  if abs(cell - (gain * ref + shift)) > CELL_DELTA)
    return changed / len(cells)


def difference(image: bytes, reference: bytes) -> float:
    """Fraction of grid cells that differ after illumination fitting.

    The reference is mapped onto the frame's lighting by a least-
    squares affine fit (gain + offset) over all cells, so a global
    lighting or exposure change between the reference capture and this
    frame does not read as a changed plate; a part — a local change —
    still does, because no global map fits it.
    """
    cells, _mean = _grid(image)
    ref_cells, _ref_mean = _grid(reference)
    return _changed_fraction(cells, ref_cells)


def matches_reference(image: bytes, reference: bytes) -> bool:
    return difference(image, reference) <= CHANGED_FRACTION


def matches_grid(reference: bytes, grid) -> bool:
    """True when a precomputed ``_grid`` matches the reference frame."""
    cells, _mean = grid
    ref_cells, _ref_mean = _grid(reference)
    return _changed_fraction(cells, ref_cells) <= CHANGED_FRACTION


# --- The producer -----------------------------------------------------------------


def empty_verdict(image: bytes, cfg=None):
    """``("empty_idle", detail)`` when the frame matches the calibrated
    empty-plate reference, else None.

    None is the only other answer this producer ever gives: it never
    claims a plate is occupied, and it never guesses empty. Every
    precondition failure (feature off, dark frame, no or stale
    reference, undecodable frame, comparison error) is None, with the
    reason available from :func:`reference_status` for bot check.
    Callers must apply the routing rules first (``visual_watch``'s idle
    path does: setting on, watch_source eligible, no endpoint route).
    """
    cfg = _config.cfg() if cfg is None else cfg
    if not empty_enabled(cfg):
        return None
    try:
        from . import visual_watch
        if visual_watch.frame_dark(image):
            return None  # #476: a dark frame is never judged
        reference = _load_reference(cfg)
        fraction = difference(image, reference)
    except Exception:
        return None
    if fraction > CHANGED_FRACTION:
        return None
    return ("empty_idle",
            f"plate matches the calibrated empty reference "
            f"({fraction * 100:.1f}% of the frame differs)")


def idle_verdict(image: bytes, cfg=None):
    """The idle-check entry point: the reference verdict when this
    install may produce one locally, else None.

    Applies the full local gate on top of :func:`empty_verdict`: the
    feature switch, and ``watch_source`` — under ``endpoint`` the
    reference is never consulted. Under ``auto``/``local`` the caller
    (``visual_watch``) still owns the endpoint-precedence rule; this
    function is also the consumers' last-resort consultation when no
    route answered, which is exactly the no-endpoint-route case.
    """
    cfg = _config.cfg() if cfg is None else cfg
    if not empty_enabled(cfg):
        return None
    source = str((cfg.get("vision") or {}).get("watch_source") or "auto")
    if source.strip().lower() == "endpoint":
        return None
    return empty_verdict(image, cfg)


# --- Status (bot check) -----------------------------------------------------------


def _age_text(age_s: float) -> str:
    if age_s < 3600:
        return f"{max(1, round(age_s / 60))} minutes"
    if age_s < 86400:
        return f"{age_s / 3600:.1f} hours"
    return f"{age_s / 86400:.1f} days"


def reference_status(cfg=None, now=None) -> tuple[str, str]:
    """``(state, detail)`` for surfaces that report the reference without
    judging a frame: ``off`` / ``missing`` / ``pending`` / ``stale`` /
    ``unavailable`` / ``ready``. Never raises, never touches a camera.

    Drift beyond age is not measurable offline (it needs a live frame),
    so the health signal is the reference's age plus the producer's own
    fail-soft behavior: a plate that no longer matches simply stops
    producing ``empty_idle``, and this row says how old the reference is.
    """
    cfg = _config.cfg() if cfg is None else cfg
    if not empty_enabled(cfg):
        return "off", ("vision.local_empty_enabled is false; idle plate checks "
                       "need a vision endpoint for empty_idle")
    path = reference_path(cfg)
    if _pending_path(cfg).is_file():
        return "pending", ("a captured reference is waiting for confirmation; "
                           "it takes effect only when confirmed")
    if not path.is_file():
        return "missing", ("no reference captured yet; run "
                           "/print-plate-calibrate capture with the plate empty")
    age = _reference_age_s(cfg, now=now)
    if age is None:
        return "unavailable", ("the plate reference record is unreadable; "
                               "recalibrate with /print-plate-calibrate capture")
    if age > REFERENCE_MAX_AGE_DAYS * 86400:
        return "stale", (f"reference captured {_age_text(age)} ago, over the "
                         f"{REFERENCE_MAX_AGE_DAYS}-day limit; the producer is "
                         "off until you recalibrate "
                         "(/print-plate-calibrate capture)")
    try:
        _grid(path.read_bytes())
    except Exception:
        return "unavailable", ("the stored plate reference is not a readable "
                               "image; recalibrate with "
                               "/print-plate-calibrate capture")
    return "ready", (f"reference captured {_age_text(age)} ago "
                     f"(reference age {age / 86400:.1f} days); idle checks "
                     "compare against it (auto light-off and the next-plate "
                     "offer can use it when no vision endpoint answers)")


def reference_ready(cfg=None) -> bool:
    return reference_status(cfg)[0] == "ready"


# --- Capture (the calibration surface's frame source) ------------------------------


def capture_frame(cfg=None) -> bytes:
    """One frame from the configured camera snapshot URL.

    The same bounded fetch the Moonraker listener uses for its idle
    checks (services.camera_snapshot_url, JPEG, size-capped). Raises
    :class:`LocalEmptyError` when no frame can be fetched.
    """
    cfg = _config.cfg() if cfg is None else cfg
    url = str((cfg.get("services") or {}).get("camera_snapshot_url") or "").strip()
    if not url:
        raise LocalEmptyError("services.camera_snapshot_url is not configured")
    import urllib.request
    try:
        from .. import camera_url
        request = camera_url.request(url)
        with urllib.request.urlopen(request, timeout=8) as response:
            image = response.read(8 * 1024 * 1024 + 1)
    except Exception as exc:
        raise LocalEmptyError(f"camera snapshot failed: {exc}") from exc
    if len(image) > 8 * 1024 * 1024 or not image.startswith(bytes((0xFF, 0xD8))):
        raise LocalEmptyError("camera response is not a bounded JPEG snapshot")
    return image


# Re-exported for callers that prefer the module's own vocabulary.
__all__ = [
    "CELL_DELTA", "CHANGED_FRACTION", "GRID_COLS", "GRID_ROWS",
    "LocalEmptyError", "META_FILENAME", "PENDING_FILENAME",
    "REFERENCE_FILENAME", "REFERENCE_MAX_AGE_DAYS", "begin_calibration",
    "capture_frame", "confirm_calibration", "difference",
    "discard_calibration", "empty_enabled", "empty_verdict", "idle_verdict",
    "matches_reference", "reference_dir", "reference_path",
    "reference_ready", "reference_status",
]
