"""Built-in local failure detector for the camera watchdog (#455, Stage 1).

The watchdog's verdicts normally come from a vision endpoint:
``PRINT_VISION_URL`` or the OpenAI-compatible chat route
(``services.vision_chat_url``). An install with neither configured used to
get no detection at all — ``vision_ready`` kept the watch off. This module
is the strictly-local third route from the #455 verdict: an ONNX failure
detector that runs on this machine, so a no-LLM, no-cloud install still
gets bad-print detection. Nothing leaves the house; no key is needed.

It is a failure detector only, not a vision model. It answers exactly one
question — "does this frame show a print failure?" — so it can produce
``spaghetti``, ``uncertain`` and ``ok`` verdicts, never ``air``,
``detached`` or ``empty_idle``, and it never answers label/extraction
reads (those still need an endpoint). Because it cannot certify an empty
plate, auto light-off (#516) still requires an endpoint.

Model provenance (pinned; the weights are fetched, never vendored —
the same posture as the AGPL slicers the image downloads at build time):

* Obico / The Spaghetti Detective failure-detection model, ONNX export
  (YOLOv2-style region detector, single ``failure`` class), from the
  obico-server project's ``ml_api/model/model-weights.onnx.url``.
* URL: https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx
* SHA-256: 0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa
* Size: 202,223,918 bytes (~202 MB). The #455 Stage 0 report estimated
  15–30 MB; the real export is much larger, which is why the weights are
  downloaded once at runtime into the model directory instead of being
  baked into the container image.
* Licence: AGPL-3.0 (the obico-server project's licence).

Fail-soft discipline (#387): a missing runtime, a missing or corrupt
model, a failed download or an inference error raises
:class:`LocalVisionError`, which the caller turns into an ``uncertain``
verdict (which alerts) with the real error logged — never a crash, never
a silent pass, never a blocked print. A failure is remembered for a
while (FAILURE_RETRY_BASE_S, doubling per repeat up to
FAILURE_RETRY_MAX_S), so a broken setup does not retry a 202 MB
download on every camera check, but a transient one (a network blip
during the first download) recovers without a restart. The model is
fetched off the check path (:func:`prefetch_model`, a background
thread with an overall deadline), never inside a camera check.

Temporal escalation (later stage): a single frame is weak evidence, so
a print watch can feed its frames through a :class:`FailureStreak` —
the sustained-streak pattern ``AutoLightPolicy`` uses — and a possible
failure that keeps being seen across consecutive checks escalates to a
failure verdict, the multi-frame escalation Obico's own serving applies
and Stage 1 noted as missing. The single-frame bands below keep their
meaning; the streak only judges persistence.

GPU execution providers (later stage): session creation asks
onnxruntime which providers the host offers and picks the most
preferred accelerated one (:data:`PROVIDER_PREFERENCE`), with CPU
always in the list as the fallback; a provider that fails to
initialise retries once on CPU alone. The active provider is logged
once at load and reported by :func:`detector_status` (the bot check
row). Selection is automatic — there is no provider setting. The
wiring is tested against fake provider lists; live GPU proof is an
operator follow-up (see #455), so no host is claimed GPU-verified.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

from ..core.control_lock import PrinterControlLock
from . import _config

logger = logging.getLogger(__name__)

# --- Pinned model provenance (see module docstring) --------------------------
MODEL_NAME = "Obico failure-detection model"
MODEL_URL = "https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx"
MODEL_SHA256 = "0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa"
MODEL_SIZE_BYTES = 202_223_918
MODEL_FILENAME = "obico-failure-detector.onnx"

# --- Verdict mapping (single still frame) ------------------------------------
# Obico's own serving code treats a detection above 0.08 confidence as a
# positive (ml_api THRESH). One frame is weaker evidence than Obico's
# multi-frame escalation, so the verdict needs a stronger single frame:
# at or above FAILURE_AT the frame is judged a failure (``spaghetti``);
# a weaker positive stays ``uncertain``. A weak positive on its own is
# noted, not posted: one 10-20% box on a healthy print is common, and an
# alert for it teaches the operator to ignore the camera. It is posted
# (without a ping) only once it persists (WEAK_ALERT_AFTER, below).
POSITIVE_FLOOR = 0.08
FAILURE_AT = 0.5
NMS_IOU = 0.45  # Obico's OnnxNet.detect default

# --- Temporal escalation (FailureStreak) --------------------------------------
# Independent consecutive positive checks (FailureStreak._independent)
# before a weak (below FAILURE_AT) detection is posted as a "possible
# failure", low confidence, with no ping and no auto-pause.
WEAK_ALERT_AFTER = 2
# Independent consecutive positive checks that confirm a weak detection
# as a failure (``spaghetti``). Watchdog checks are minutes apart
# (450-1800 s by sensitivity), so three in a row means the detector kept
# seeing it across a sustained span of the print, not one odd frame.
ESCALATE_AFTER = 3
# The streak counter is capped so the state stays bounded however long
# a print runs; the cap is far above ESCALATE_AFTER, so the "confirmed
# across N consecutive checks" wording stays truthful.
STREAK_CAP = 100

DOWNLOAD_TIMEOUT_S = 180
# The whole download, not one read: a slow-drip server cannot hold it open
# for ever. 202 MB in 20 minutes needs about 170 KB/s.
DOWNLOAD_DEADLINE_S = 1200
# A remembered failure is retried after this long, doubling per repeat.
FAILURE_RETRY_BASE_S = 60.0
FAILURE_RETRY_MAX_S = 3600.0


class LocalVisionError(Exception):
    """The local detector cannot judge this frame."""


# --- Settings -----------------------------------------------------------------


def enabled(cfg=None) -> bool:
    cfg = _config.cfg() if cfg is None else cfg
    return bool((cfg.get("vision") or {}).get("local_detector_enabled", False))


def watch_source(cfg=None) -> str:
    """The operator's verdict-source choice for the camera watchdog
    (#455): ``vision.watch_source`` — "auto" (the default route
    precedence), "endpoint" (endpoint routes only) or "local" (this
    detector only). Settings validation guarantees the value in a loaded
    config; anything unexpected in a hand-built dict reads as "auto", so
    a partial config can never break the watch."""
    cfg = _config.cfg() if cfg is None else cfg
    source = str((cfg.get("vision") or {}).get("watch_source") or "auto").strip().lower()
    return source if source in ("auto", "endpoint", "local") else "auto"


def watch_uses_detector(cfg=None) -> bool:
    """Whether the watchdog may judge frames with this detector at all:
    the detector switch is on, or the operator pinned the watch to the
    local source (``watch_source = "local"`` is itself the enablement
    for the watch; the switch keeps its auto-mode meaning)."""
    return enabled(cfg) or watch_source(cfg) == "local"


def model_path(cfg=None) -> Path:
    """Where the ONNX weights live: the operator's
    ``vision.local_model_path`` when set, else ``models/`` under the
    data folder (persistent in the container's ``/data/state`` volume)."""
    cfg = _config.cfg() if cfg is None else cfg
    raw = str((cfg.get("vision") or {}).get("local_model_path") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    home = Path(os.path.expanduser(str(cfg["paths"]["state_home"])))
    return home / "models" / MODEL_FILENAME


def _is_default_path(path: Path, cfg=None) -> bool:
    cfg = _config.cfg() if cfg is None else cfg
    return not str((cfg.get("vision") or {}).get("local_model_path") or "").strip() \
        and path.name == MODEL_FILENAME


# --- Runtime / session caches (per process; failures expire and retry) --------

_lock = threading.Lock()
_file_lock = threading.Lock()  # model file preparation; never held by a session load
_prefetch_lock = threading.Lock()
_prefetching: dict[str, threading.Thread] = {}
_ort = None
_ort_error: Exception | None = None
_sessions: dict[str, object] = {}
_active_providers: dict[str, str] = {}
# key -> (message, monotonic retry time, consecutive failures)
_failures: dict[str, tuple[str, float, int]] = {}
_verified_files: set[str] = set()
# Chat notices about the detector (#1194): a failure is announced once when
# it starts, and once more when that same thing works again. The watches
# drain them with take_notices() and post them; this module has no chat.
_notice_lock = threading.Lock()
_announced: set[str] = set()
_notices: list[str] = []


def _runtime():
    global _ort, _ort_error
    if _ort is None:
        if _ort_error is not None:
            raise LocalVisionError(
                f"onnxruntime is not available: {_ort_error}")
        try:
            import onnxruntime

            _ort = onnxruntime
        except Exception as exc:  # noqa: BLE001 - reported, then verdicts degrade
            _ort_error = exc
            raise LocalVisionError(
                f"onnxruntime is not available: {exc}") from exc
    return _ort


def _remembered_failure(key: str) -> str | None:
    """The remembered failure message for ``key`` while it is still current.

    Once its retry time has passed it reads as None, so the next attempt
    runs; the repeat count is kept so a failure that recurs backs off
    further (:func:`_fail`).
    """
    entry = _failures.get(key)
    if not entry:
        return None
    message, retry_at, _count = entry
    if time.monotonic() >= retry_at:
        return None
    return message


def _fail(key: str, message: str) -> LocalVisionError:
    previous = _failures.get(key)
    count = (previous[2] if previous else 0) + 1
    delay = min(FAILURE_RETRY_BASE_S * (2 ** (count - 1)), FAILURE_RETRY_MAX_S)
    _failures[key] = (message, time.monotonic() + delay, count)
    logger.warning("local detector: %s (retrying in %ds)", message, int(delay))
    with _notice_lock:
        if key not in _announced:
            _announced.add(key)
            _notices.append(
                f"Local failure detector unavailable: {message}. Camera checks that rely on "
                f"it are uncertain until it recovers; Print Handy retries on its own, first "
                f"in about {_span(delay)}, then less often. Nothing was sent to the printer.")
    return LocalVisionError(message)


def _clear_failure(key: str) -> None:
    _failures.pop(key, None)
    with _notice_lock:
        if key in _announced:
            _announced.discard(key)
            _notices.append("Local failure detector is working again; camera checks "
                            "that rely on it are judged normally.")


def _span(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes >= 1 else f"{int(seconds)} seconds"


def take_notices() -> list[str]:
    """The pending detector failure and recovery notices, oldest first; each is returned once."""
    with _notice_lock:
        pending = list(_notices)
        _notices.clear()
    return pending


# --- Model file ----------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _model_lock(path: Path) -> PrinterControlLock:
    """The interprocess lock serializing model downloads for one path.

    supervise.sh runs one listener process per printer, all sharing the
    model directory, so the in-process threading lock cannot coordinate
    them (P2-221); this flock (the control lock's machinery) can, and it
    is released by the kernel if a holder dies mid-download.
    """
    return PrinterControlLock(lambda: path.with_name(path.name + ".download.lock"))


def _download_model(path: Path) -> None:
    """Fetch the pinned weights into ``path``, SHA-256 verified. A
    mismatch or a network error leaves no file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-process temp name (P2-221): two processes must never write
    # one shared .part file, and publication stays one atomic replace.
    part = path.with_name(f"{path.name}.{os.getpid()}.part")
    digest = hashlib.sha256()
    deadline = time.monotonic() + DOWNLOAD_DEADLINE_S
    received = 0
    try:
        request = urllib.request.Request(
            MODEL_URL, headers={"User-Agent": "print-handy-local-vision"})
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response, \
                part.open("wb") as out:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                received += len(chunk)
                if received > MODEL_SIZE_BYTES + 1024 * 1024:
                    raise ValueError(f"the download is larger than the pinned model "
                                     f"({MODEL_SIZE_BYTES} bytes)")
                if time.monotonic() > deadline:
                    raise TimeoutError(f"the download did not finish within "
                                       f"{DOWNLOAD_DEADLINE_S}s")
                digest.update(chunk)
                out.write(chunk)
    except Exception as exc:  # noqa: BLE001 - turned into a verdict-level failure
        part.unlink(missing_ok=True)
        raise LocalVisionError(
            f"downloading the local detector model from {MODEL_URL} failed: {exc}") from exc
    if digest.hexdigest() != MODEL_SHA256:
        part.unlink(missing_ok=True)
        raise LocalVisionError(
            "downloaded local detector model failed its SHA-256 check "
            f"(expected {MODEL_SHA256}, got {digest.hexdigest()}); refusing to use it")
    try:
        os.replace(part, path)
    except OSError as exc:
        part.unlink(missing_ok=True)  # never leave this process's .part behind (2.2.5 review)
        raise LocalVisionError(
            f"publishing the downloaded local detector model to {path} failed: {exc}") from exc
    logger.info("local detector model downloaded to %s (SHA-256 verified)", path)


def _ensure_model_file(path: Path, cfg=None) -> None:
    key = f"file:{path}"
    remembered = _remembered_failure(key)
    if remembered:
        raise LocalVisionError(remembered)
    # The threading lock covers this process's threads; the flock covers
    # the sibling listener processes sharing the model directory, but only
    # for a download (P2-221): an existing model needs no lock, so a
    # read-only model directory works on every frame (2.2.5 review). The
    # existence check reruns inside the flock, so a process that waited on
    # a peer's download reuses the published file instead of downloading
    # over it (or failing its replace and remembering that race as a
    # permanent failure).
    with _file_lock:
        remembered = _remembered_failure(key)
        if remembered:
            raise LocalVisionError(remembered)
        try:
            if not path.is_file():
                with _model_lock(path):
                    if not path.is_file():
                        logger.info(
                            "local detector model not present; downloading the pinned "
                            "%s (~%d MB, one time) from %s",
                            MODEL_NAME, MODEL_SIZE_BYTES // 1_000_000, MODEL_URL)
                        _download_model(path)
                        _verified_files.add(key)  # the download itself was SHA-256 verified
            if _is_default_path(path, cfg) and key not in _verified_files:
                # A pre-existing file at the pinned location is only trusted
                # after its checksum matches, checked once per process.
                if _sha256_of(path) != MODEL_SHA256:
                    raise LocalVisionError(
                        f"the local detector model at {path} does not match the pinned "
                        "SHA-256; refusing to use it (delete the file to re-download, "
                        "or set vision.local_model_path to your own model)")
                _verified_files.add(key)
        except LocalVisionError as exc:
            raise _fail(key, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - an unreadable model dir degrades the same way
            raise _fail(key, f"the local detector model at {path} could not be "
                             f"prepared: {exc}") from exc
        _clear_failure(key)


def _prefetch_worker(path: Path, cfg) -> None:
    try:
        _ensure_model_file(path, cfg)
    except LocalVisionError as exc:
        logger.warning("local detector model download failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 - a background thread must never die loudly
        logger.warning("local detector model download failed: %r", exc)


def prefetching(cfg=None) -> bool:
    """True while the background model download for the configured path runs."""
    try:
        thread = _prefetching.get(str(model_path(cfg)))
    except Exception:  # noqa: BLE001 - status helpers must never raise
        return False
    return bool(thread and thread.is_alive())


def prefetch_model(cfg=None, *, wait=False) -> bool:
    """Fetch the model in the background when the watch will need it. Never raises.

    Runs only when the watchdog uses the detector, the model file is
    missing and no failure is waiting out its retry time. The fetch is the
    SHA-256-verified download of :func:`_ensure_model_file`, on its own
    daemon thread with an overall deadline, so a camera check never waits
    on it. Returns True when a download is running (started now or before).
    ``wait`` joins it (tests, a one-shot command).
    """
    try:
        cfg = _config.cfg() if cfg is None else cfg
        if not watch_uses_detector(cfg):
            return False
        path = model_path(cfg)
        if path.is_file() or _remembered_failure(f"file:{path}"):
            return False
        with _prefetch_lock:
            thread = _prefetching.get(str(path))
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=_prefetch_worker, args=(path, cfg),
                                          name="local-detector-download", daemon=True)
                _prefetching[str(path)] = thread
                thread.start()
        if wait:
            thread.join()
        return True
    except Exception as exc:  # noqa: BLE001 - starting a download must never break a caller
        logger.warning("local detector model download could not start: %r", exc)
        return False


# --- Execution providers ---------------------------------------------------------
# Documented preference order for accelerated providers, most preferred
# first. CUDA before TensorRT: the CUDA provider is a plain runtime,
# while the TensorRT provider first builds an engine (which can take
# minutes) — a watchdog's first check must not pay that. OpenVINO covers
# Intel/AMD integrated GPUs (the #246 VAAPI hosts), CoreML Apple
# Silicon, DirectML Windows GPUs, ROCm AMD on Linux. Only one
# accelerated provider is requested, plus CPU: the choice is
# deterministic, CPU stays the per-node and whole-session fallback, and
# the active provider is a single name the operator can be shown.
PROVIDER_PREFERENCE = (
    "CUDAExecutionProvider",
    "TensorRTExecutionProvider",
    "OpenVINOExecutionProvider",
    "CoreMLExecutionProvider",
    "DmlExecutionProvider",
    "ROCMExecutionProvider",
    "CPUExecutionProvider",
)


def select_providers(available) -> list[str]:
    """The provider list for a new session, from what onnxruntime
    reports as available on this host: the most preferred accelerated
    provider offered, then CPU. Pure and total — unknown or empty input
    yields CPU only."""
    offered = set(available or ())
    for provider in PROVIDER_PREFERENCE:
        if provider != "CPUExecutionProvider" and provider in offered:
            return [provider, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def selected_provider(cfg=None) -> str | None:
    """The provider a session would run on on this host — the head of
    :func:`select_providers` over what onnxruntime reports — computed
    without creating a session or touching the model. None when
    onnxruntime is unusable. (``cfg`` is accepted for symmetry with the
    other status helpers; selection depends on the host, not the
    config.)"""
    try:
        ort = _runtime()
        return select_providers(ort.get_available_providers())[0]
    except Exception:  # noqa: BLE001 - status surfaces must never raise
        return None


def gpu_device_visible(dri_dir="/dev/dri", dev_dir="/dev") -> bool:
    """Whether a GPU device node is visible in this process: an Intel/AMD
    render node or an NVIDIA device. Presence only — never opens one."""
    try:
        return any(Path(dri_dir).glob("renderD*")) or any(Path(dev_dir).glob("nvidia[0-9]*"))
    except Exception:  # noqa: BLE001 - status helpers must never raise
        return False


def accelerator_hint(available, *, device_visible=None) -> str:
    """A named note when a GPU device is visible but the installed
    onnxruntime build offers no accelerated provider (#455), else "".

    The published image installs the PyPI ``onnxruntime`` wheel, which is
    the CPU build: passing a GPU through for timelapse encoding does not
    accelerate the detector. Saying so keeps the operator from reading a
    granted GPU as an accelerated watchdog.
    """
    offered = set(available or ())
    if any(p in offered for p in PROVIDER_PREFERENCE if p != "CPUExecutionProvider"):
        return ""
    visible = gpu_device_visible() if device_visible is None else device_visible
    if not visible:
        return ""
    return ("a GPU device is visible, but this onnxruntime build offers only "
            "CPUExecutionProvider, so detection runs on the CPU (GPU inference "
            "needs an accelerated onnxruntime build; see docs/DOCKER.md, GPU passthrough)")


def active_provider(cfg=None) -> str | None:
    """The execution provider the live session for the configured model
    actually runs on in this process, or None before the first session
    (or when its load failed)."""
    return _active_providers.get(f"session:{model_path(cfg)}")


def _session_options(ort):
    """SessionOptions whose thread pools follow the container's CPU quota, or None when it has none.

    onnxruntime sizes its own pools from the host CPU count and ignores OMP_NUM_THREADS.
    """
    from ..core import resources
    budget = resources.thread_budget()
    if not budget:
        return None
    options = ort.SessionOptions()
    options.intra_op_num_threads = budget
    options.inter_op_num_threads = 1
    return options


def _create_session(ort, path: Path, providers: list[str]):
    """One InferenceSession attempt, with a single CPU-only retry when
    an accelerated provider was requested but cannot initialise — an
    accelerator that is present-but-broken (driver mismatch, missing
    libraries) degrades to CPU, never to a dead detector."""
    options = _session_options(ort)
    extra = {"sess_options": options} if options is not None else {}
    try:
        return ort.InferenceSession(str(path), providers=providers, **extra)
    except Exception:
        if providers == ["CPUExecutionProvider"]:
            raise
        logger.warning(
            "local detector: session with execution providers %s failed; "
            "falling back to CPUExecutionProvider", providers)
        return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], **extra)


def _session(path: Path):
    key = f"session:{path}"
    remembered = _remembered_failure(key)
    if remembered:
        raise LocalVisionError(remembered)
    with _lock:
        if key in _sessions:
            return _sessions[key]
        try:
            ort = _runtime()
            providers = select_providers(ort.get_available_providers())
            session = _create_session(ort, path, providers)
        except LocalVisionError:
            raise
        except Exception as exc:  # noqa: BLE001 - corrupt model etc. degrade the same way
            raise _fail(key, f"the local detector model at {path} could not be "
                             f"loaded: {exc}") from exc
        _sessions[key] = session
        _clear_failure(key)
        try:
            active = (session.get_providers() or ["CPUExecutionProvider"])[0]
        except Exception:  # noqa: BLE001 - a session that cannot name its providers still judges frames
            active = providers[-1]
        _active_providers[key] = active
        logger.info("local detector session ready at %s (execution provider: %s)",
                    path, active)
        return session


# --- Detection -------------------------------------------------------------------


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _postprocess(boxes, confs):
    """The model's output contract: ``boxes`` [1, N, 1, 4] relative xyxy
    and ``confs`` [1, N, 1], decoded in-graph. Returns the kept
    (confidence, box) pairs after the positive floor and NMS, strongest
    first — the same post-processing shape as Obico's own server code."""
    import numpy as np

    box_arr = np.asarray(boxes, dtype=np.float64)
    conf_arr = np.asarray(confs, dtype=np.float64)
    if box_arr.ndim != 4 or conf_arr.ndim != 3 or box_arr.shape[1] != conf_arr.shape[1]:
        raise LocalVisionError(
            f"unexpected local detector output shapes: boxes {box_arr.shape}, "
            f"confs {conf_arr.shape}")
    flat_boxes = box_arr[0, :, 0, :]
    flat_confs = conf_arr[0, :, 0]
    order = sorted(
        (i for i in range(len(flat_confs)) if flat_confs[i] > POSITIVE_FLOOR),
        key=lambda i: -flat_confs[i])
    kept: list[int] = []
    for i in order:
        if all(_iou(flat_boxes[i], flat_boxes[j]) <= NMS_IOU for j in kept):
            kept.append(i)
    return [(float(flat_confs[i]), flat_boxes[i]) for i in kept]


def _preprocess(image: bytes, width: int, height: int):
    """JPEG bytes -> the model's input tensor: RGB, resized to the
    model's input size, CHW, scaled to 0..1 (Obico's own preprocessing)."""
    import io

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(image)) as frame:
        rgb = frame.convert("RGB").resize((width, height), Image.BILINEAR)
        array = np.asarray(rgb, dtype=np.float32)
    tensor = np.transpose(array, (2, 0, 1))[np.newaxis, ...]
    return np.ascontiguousarray(tensor) / 255.0


def detect(image: bytes, cfg=None):
    """Run the detector on one JPEG frame. Returns the kept detections,
    strongest first. Raises :class:`LocalVisionError` on any
    infrastructure failure (runtime, model, inference)."""
    cfg = _config.cfg() if cfg is None else cfg
    path = model_path(cfg)
    if not path.is_file():
        # Never download inside a camera check: start (or keep) the
        # background fetch and fail this check soft.
        remembered = _remembered_failure(f"file:{path}")
        prefetch_model(cfg)
        raise LocalVisionError(remembered or (
            f"the local detector model is not downloaded yet; it is downloading in the "
            f"background into {path}"))
    _ensure_model_file(path, cfg)
    session = _session(path)
    spec = session.get_inputs()[0]
    try:
        height, width = int(spec.shape[2]), int(spec.shape[3])
    except (TypeError, ValueError, IndexError) as exc:
        raise LocalVisionError(
            f"the local detector model at {path} has an unexpected input "
            f"shape {spec.shape!r}") from exc
    try:
        tensor = _preprocess(image, width, height)
    except LocalVisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable frame is a failed check
        raise LocalVisionError(f"the camera frame could not be read: {exc}") from exc
    try:
        outputs = session.run(None, {spec.name: tensor})
    except LocalVisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - inference errors degrade like endpoint errors
        raise LocalVisionError(f"local detector inference failed: {exc}") from exc
    if len(outputs) < 2:
        raise LocalVisionError(
            f"the local detector model returned {len(outputs)} outputs, expected 2")
    try:
        return _postprocess(outputs[0], outputs[1])
    except LocalVisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed outputs are a failed check
        raise LocalVisionError(f"local detector outputs could not be read: {exc}") from exc


#: The words a weak ("possible failure") detection's detail carries.
#: Display only: callers decide on :class:`Detection`, never on this text.
BELOW_THRESHOLD = "below the single-frame failure threshold"


def percent(confidence) -> int:
    """A confidence in whole percent, rounded down.

    Rounded down so a sub-threshold value never reads as the threshold:
    0.4999 shows as 49%, not 50%.
    """
    return math.floor(float(confidence) * 100 + 1e-9)


class Detection(NamedTuple):
    """The detector's structured result for one frame with a positive box.

    ``confidence`` is the strongest kept box (0..1), ``seen`` how many
    independent consecutive checks saw a positive (this one included, 1
    for a single-frame judgement), ``counted`` whether this frame was one
    of them (a frame too soon after, or unchanged from, the last counted
    one is not), and ``verdict`` the verdict it produced.
    """
    confidence: float
    seen: int
    counted: bool
    verdict: str

    @property
    def weak(self) -> bool:
        """A weak detection: below FAILURE_AT and not escalated (verdict ``uncertain``)."""
        return self.verdict == "uncertain" and self.confidence < FAILURE_AT

    @property
    def posts(self) -> bool:
        """Whether a weak detection is posted (without a ping) rather than only noted.

        False for anything that is not weak: an escalated or strong
        detection is a failure verdict, not a weak post.
        """
        return self.weak and self.counted and self.seen >= WEAK_ALERT_AFTER


class Verdict(tuple):
    """A local detector verdict: the ``(verdict, detail)`` pair every route returns.

    It unpacks as the pair, so it passes unchanged through the verdict
    routes, and carries ``detection`` (a :class:`Detection`, or None when
    nothing was detected). ``detail`` is display text only.
    """

    def __new__(cls, verdict, detail, detection=None):
        self = super().__new__(cls, (verdict, detail))
        self.detection = detection
        return self

    @property
    def verdict(self):
        return self[0]

    @property
    def detail(self):
        return self[1]

    def with_detail(self, detail):
        """The same verdict and detection with another detail (a caller's prefix)."""
        return Verdict(self[0], detail, self.detection)


def detection_of(result):
    """The :class:`Detection` a verdict route's result carries, or None.

    None for anything that is not a local detector :class:`Verdict` (an
    endpoint answer, a detector failure, a plain tuple).
    """
    return getattr(result, "detection", None)


def _weak_detail(detection: Detection) -> str:
    """The display detail of a weak (``uncertain``) detection."""
    seen = detection.seen
    text = (f"local failure detector: possible failure at {percent(detection.confidence)}% "
            f"confidence, {BELOW_THRESHOLD}; seen in {seen} consecutive "
            f"check{'s' if seen != 1 else ''}")
    if not detection.counted:
        return (f"{text}; this frame was not counted (too soon after, or unchanged from, "
                "the last detection)")
    if seen < WEAK_ALERT_AFTER:
        return f"{text} (noted, not posted: one weak detection is usually nothing)"
    return (f"{text} (posted without a ping; {ESCALATE_AFTER} in a row make it "
            "a failure verdict)")


def _strong_detail(kept, positives) -> str:
    confidence = kept[0][0]
    areas = len(kept)
    reason = (f"local failure detector: print failure at {confidence:.0%} "
              f"confidence ({areas} area{'s' if areas != 1 else ''})")
    if positives >= 2:
        reason += f", confirmed across {positives} consecutive checks"
    return reason


def _single_frame_verdict(kept) -> Verdict:
    """One frame's verdict, Stage 1 semantics: the mapping the bands
    above define, with no memory of earlier frames (a weak detection is
    one sighting, so it is noted, never posted)."""
    if kept:
        confidence = kept[0][0]
        if confidence >= FAILURE_AT:
            return Verdict("spaghetti", _strong_detail(kept, 1),
                           Detection(confidence, 1, True, "spaghetti"))
        detection = Detection(confidence, 1, True, "uncertain")
        return Verdict("uncertain", _weak_detail(detection), detection)
    return Verdict("ok", "local failure detector: no failure detected")


class FailureStreak:
    """Temporal escalation state for one printer's current print.

    Pure policy, no I/O (the ``AutoLightPolicy`` shape): the print watch
    that owns the printer creates one when a print starts, feeds it
    every local-detector frame (via :func:`local_verdict`), and resets
    it when the print ends. The state is a single capped counter, so it
    stays bounded however long a print runs, and frames are counted in
    independent checks (:meth:`_independent`): the watchdog's cadence
    (minutes apart) is what makes a run of consecutive positives
    *sustained* evidence rather than a burst.

    * no positive detection: the streak clears and the frame is ``ok``
      (the reason notes it when a streak was actually cleared);
    * a strong frame (>= ``FAILURE_AT``) is ``spaghetti`` at once, as a
      single frame always was; when it lands on an existing streak the
      reason says the failure is streak-confirmed, naming the count;
    * a weak frame (``POSITIVE_FLOOR``..``FAILURE_AT``) is ``uncertain``
      and its :class:`Detection` says how often it was seen: the first
      sighting is only noted, the ``WEAK_ALERT_AFTER``-th consecutive one
      is posted as a low confidence "possible failure", and the
      ``ESCALATE_AFTER``-th escalates to ``spaghetti``. A weak frame that
      is not independent of the last counted one is noted and counts for
      nothing.

    Every verdict is a :class:`Verdict`; the decision lives in its
    ``detection``, the detail is display text. A detector failure never
    reaches this policy (``detect`` raises first and the caller degrades
    to ``uncertain``), so a broken check neither builds nor clears the
    streak.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.positives = 0
        self.last_at = None
        self.last_signature = None

    def _independent(self, now, signature):
        """Whether a positive frame is a new view (visual_watch's evidence rule).

        A positive only counts toward escalation when it is at least
        EVIDENCE_MIN_SPACING_S after the last counted positive and its frame
        visibly changed: early-layer checks can fire seconds apart on the
        same or a frozen frame, and that is one view, not two. A caller that
        passes neither time nor frame keeps the plain per-check count.
        """
        if now is None and signature is None:
            return True
        if self.last_at is None:
            return True
        from . import visual_watch as _vw
        if now is None or now - self.last_at < _vw.EVIDENCE_MIN_SPACING_S:
            return False
        return _vw.frames_differ(self.last_signature, signature) is True

    def note(self, kept, *, now=None, signature=None) -> Verdict:
        """Feed one frame's kept detections; returns its :class:`Verdict`.

        ``now`` and ``signature`` (visual_watch.frame_signature) let the
        streak count only independent views; see :meth:`_independent`.
        """
        if not kept:
            self.last_at = None
            self.last_signature = None
            if self.positives:
                self.positives = 0
                return Verdict("ok", "local failure detector: no failure detected; "
                                     "the earlier possible failure did not persist")
            return Verdict("ok", "local failure detector: no failure detected")
        confidence = kept[0][0]
        counted = self._independent(now, signature)
        if counted:
            self.positives = min(self.positives + 1, STREAK_CAP)
            self.last_at = now
            self.last_signature = signature
        seen = max(self.positives, 1)
        if confidence >= FAILURE_AT:
            return Verdict("spaghetti", _strong_detail(kept, self.positives),
                           Detection(confidence, seen, counted, "spaghetti"))
        if counted and self.positives >= ESCALATE_AFTER:
            return Verdict("spaghetti", (
                f"local failure detector: possible failure at {percent(confidence)}% "
                f"confidence, {BELOW_THRESHOLD}, but now seen in {self.positives} "
                "consecutive checks; sustained detections escalate to a failure verdict"),
                Detection(confidence, seen, counted, "spaghetti"))
        detection = Detection(confidence, seen, counted, "uncertain")
        return Verdict("uncertain", _weak_detail(detection), detection)


def local_verdict(image: bytes, cfg=None, streak: FailureStreak | None = None, *,
                  now=None) -> Verdict:
    """The detector's verdict for one frame, in the watchdog's
    vocabulary. Raises :class:`LocalVisionError` when it cannot judge.

    Without ``streak`` the frame is judged on its own (Stage 1). With a
    :class:`FailureStreak` (one per printer per print, owned by the
    calling watch) the frame also feeds the streak, and a sustained
    weak detection escalates as the streak's rules define. ``now`` is
    the check's time (default the wall clock; a replay passes its
    simulated clock). A detector failure raises before the streak is
    fed, so it never disturbs it."""
    kept = detect(image, cfg)
    if streak is None:
        return _single_frame_verdict(kept)
    from . import visual_watch as _vw
    return streak.note(kept, now=time.time() if now is None else now,
                       signature=_vw.frame_signature(image))


# --- Status (bot check / vision_ready) -------------------------------------------


def detector_status(cfg=None) -> tuple[str, str]:
    """``(state, detail)`` for surfaces that report the detector without
    judging a frame: ``off`` / ``ready`` / ``unavailable``. Never touches
    the network and never downloads."""
    cfg = _config.cfg() if cfg is None else cfg
    if not watch_uses_detector(cfg):
        return "off", ("vision.local_detector_enabled is false; the camera watchdog "
                       "needs a vision endpoint, PRINT_VISION_URL or this detector")
    try:
        _runtime()
    except LocalVisionError as exc:
        return "unavailable", str(exc)
    path = model_path(cfg)
    key = f"file:{path}"
    if _remembered_failure(key):
        return "unavailable", _remembered_failure(key)
    if _remembered_failure(f"session:{path}"):
        return "unavailable", _remembered_failure(f"session:{path}")
    provider = active_provider(cfg) or selected_provider(cfg)
    suffix = f"; execution provider: {provider}" if provider else ""
    if provider == "CPUExecutionProvider":
        try:
            hint = accelerator_hint(_runtime().get_available_providers())
        except Exception:  # noqa: BLE001 - status surfaces must never raise
            hint = ""
        if hint:
            suffix += f" ({hint})"
    if path.is_file():
        return "ready", f"{MODEL_NAME} present at {path}{suffix}"
    if prefetching(cfg):
        return "unavailable", (f"the model is downloading in the background "
                               f"(~{MODEL_SIZE_BYTES // 1_000_000} MB from the pinned source, "
                               f"SHA-256 verified) into {path}")
    return "unavailable", (f"the model is not downloaded yet; the camera watch downloads it in "
                           f"the background (~{MODEL_SIZE_BYTES // 1_000_000} MB from the pinned "
                           f"source, SHA-256 verified) into {path}{suffix}")


def detector_ready(cfg=None) -> bool:
    return detector_status(cfg)[0] == "ready"
