"""Built-in local failure detector for the camera watchdog (#455, #1899).

The watchdog's verdicts normally come from the one AI endpoint (the
[ai.small] tier over [llm]). An install with it not configured used to
get no detection at all — ``vision_ready`` kept the watch off. This module
is the strictly-local route: an ONNX failure detector that runs on this
machine, so a no-LLM, no-cloud install still gets bad-print detection.
Nothing leaves the house; no key is needed.

It is a failure detector only, not a vision model. It answers exactly one
question — "does this frame show a print failure?" — so it can produce
``spaghetti``, ``uncertain`` and ``ok`` verdicts, never ``air``,
``detached`` or ``empty_idle``, and it never answers label/extraction
reads (those still need an endpoint). Because it cannot certify an empty
plate, auto light-off (#516) still requires an endpoint or a calibrated
local plate reference.

Model provenance (pinned by upstream release, commit SHA and SHA-256;
bundled in the Docker image at build time and verified offline, #1844,
#1899):

* PrintGuard Standard failure-detection model (ConvNeXt-Atto, distilled
  binary classifier producing a calibrated failure probability in [0, 1]),
  from https://github.com/oliverbravery/PrintGuard (``models/standard/model.onnx``,
  pinned release ``v3.0.1``, commit ``b45ff6096e994936a72129e05b851bd18635758c``).
* The Docker image bundles the unmodified weights file at
  :data:`DEFAULT_BUNDLED_MODEL_PATH`, verified by SHA-256 at build time,
  so a container never downloads it. A source install without the
  bundled file downloads it once from :data:`MODEL_URL` into the model
  directory and verifies the pinned SHA-256.
* SHA-256: 9265f8fdf80fb07302ff03d0adaa7a3fdb4eda9c21d0f38546870d1ef395e889
* Size: 13,563,144 bytes (~13 MB).
* License: GPL-2.0-only (the PrintGuard project's license for the
  unmodified standalone weights file; see docs/COMPONENTS.md and NOTICE).
  This loader module itself is original Print Handy code under AGPL-3.0-only.

Fail-soft discipline (#387): a missing runtime, a missing or corrupt
model, a failed download or an inference error raises
:class:`LocalVisionError`, which the caller turns into an ``uncertain``
verdict (which alerts) with the real error logged — never a crash, never
a silent pass, never a blocked print. A failure is remembered for a
while (FAILURE_RETRY_BASE_S, doubling per repeat up to
FAILURE_RETRY_MAX_S), so a broken setup does not retry a download on
every camera check, but a transient one (a network blip during the first
download) recovers without a restart. The model is fetched off the check
path (:func:`prefetch_model`, a background thread with an overall
deadline), never inside a camera check.

Temporal escalation: a single frame is weak evidence, so a print watch
feeds its frames through a :class:`FailureStreak` — the sustained-streak
pattern ``AutoLightPolicy`` uses — and a possible failure that keeps
being seen across consecutive independent checks escalates to a failure
verdict. The single-frame bands below keep their meaning; the streak only
judges persistence.

GPU execution providers: session creation asks onnxruntime which
providers the host offers and picks the most preferred accelerated one
(:data:`PROVIDER_PREFERENCE`), with CPU always in the list as the
fallback; a provider that fails to initialize retries once on CPU alone.
The active provider is logged once at load and reported by
:func:`detector_status` (the bot check row). Selection is automatic —
there is no provider setting. The wiring is tested against fake provider
lists; live GPU proof is an operator follow-up (see #455), so no host is
claimed GPU-verified.
"""
from __future__ import annotations

import hashlib
import io
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
MODEL_NAME = "PrintGuard Standard failure-detection model"
MODEL_UPSTREAM_REPO = "oliverbravery/PrintGuard"
MODEL_VERSION = "3.0.1"
MODEL_REVISION = "b45ff6096e994936a72129e05b851bd18635758c"
MODEL_UPSTREAM_PATH = "models/standard/model.onnx"
MODEL_URL = (
    f"https://raw.githubusercontent.com/{MODEL_UPSTREAM_REPO}/"
    f"{MODEL_REVISION}/{MODEL_UPSTREAM_PATH}"
)
MODEL_SHA256 = "9265f8fdf80fb07302ff03d0adaa7a3fdb4eda9c21d0f38546870d1ef395e889"
MODEL_SIZE_BYTES = 13_563_144
MODEL_FILENAME = "printguard-standard.onnx"
# Where the Docker image bundles the weights (Dockerfile, vision-model
# stage). PRINT_HANDY_BUNDLED_MODEL overrides it (tests point it away).
DEFAULT_BUNDLED_MODEL_PATH = "/opt/print-handy/models/" + MODEL_FILENAME

# --- Preprocessing contract (from PrintGuard Standard metadata.json) ---------
# Standard ConvNeXt-Atto takes NCHW float32 RGB squares of 224x224,
# produced by bilinear shortest-edge resize to 256 px, centered 224 px
# crop, and ImageNet channel normalization. Wide camera frames (such as
# 16:9 webcams) are tiled along the long edge into overlapping squares
# (at least 20% overlap, bounded to the central 4:1 span) and scored by
# their highest-risk tile.
INPUT_RESIZE_SHORTEST = 256
INPUT_CROP_SIZE = 224
NORMALIZE_MEAN = (0.485, 0.456, 0.406)
NORMALIZE_STD = (0.229, 0.224, 0.225)
TILE_OVERLAP = 0.20
MAX_ASPECT_RATIO = 4

# --- Verdict mapping (single still frame) ------------------------------------
# PrintGuard Standard outputs a calibrated failure probability in [0, 1]
# with 0.50 as its decision boundary and 0.75 as PrintGuard's own default
# alert threshold:
# * below POSITIVE_FLOOR (0.50): clean frame (``ok``);
# * POSITIVE_FLOOR..FAILURE_AT (0.50..0.75): weak positive (``uncertain``) —
#   noted on a single check, posted without a ping once it persists
#   (WEAK_ALERT_AFTER), and escalated to ``spaghetti`` on ESCALATE_AFTER
#   consecutive independent checks;
# * at or above FAILURE_AT (0.75): strong single-frame failure (``spaghetti``).
POSITIVE_FLOOR = 0.50
FAILURE_AT = 0.75

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
# forever. ~13.6 MB in 5 minutes needs about 45 KB/s.
DOWNLOAD_DEADLINE_S = 300
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


def bundled_model_path() -> Path:
    """Where an install ships the weights (the Docker image does, #1844)."""
    return Path(os.environ.get("PRINT_HANDY_BUNDLED_MODEL") or DEFAULT_BUNDLED_MODEL_PATH)


def model_path(cfg=None) -> Path:
    """Where the ONNX weights live: the operator's
    ``vision.local_model_path`` when set, else the copy bundled in the
    image (#1844), else ``models/`` under the data folder (a source
    install downloads it there once from :data:`MODEL_URL`)."""
    cfg = _config.cfg() if cfg is None else cfg
    raw = str((cfg.get("vision") or {}).get("local_model_path") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    bundled = bundled_model_path()
    if bundled.is_file():
        return bundled
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
    an accelerated provider was requested but cannot initialize — an
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


def _tile_windows(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Overlapping square pixel windows covering a frame along its long edge.

    Near-square frames (aspect ratio at most ``1 + TILE_OVERLAP``) yield
    one centered square. Wider or taller frames are split into square
    tiles with at least ``TILE_OVERLAP`` overlap across the central span
    bounded by ``MAX_ASPECT_RATIO``.
    """
    if width <= 0 or height <= 0:
        raise LocalVisionError(f"invalid camera frame dimensions: {width}x{height}")
    long_side = max(width, height)
    short_side = min(width, height)
    span = min(long_side, short_side * MAX_ASPECT_RATIO)
    ratio = span / short_side
    if ratio <= 1.0 + TILE_OVERLAP:
        starts = [(long_side - short_side) // 2]
    else:
        count = math.ceil((ratio - TILE_OVERLAP) / (1.0 - TILE_OVERLAP))
        margin = (long_side - span) // 2
        step = (span - short_side) / (count - 1)
        starts = [margin + round(i * step) for i in range(count)]
    if width >= height:
        return [(s, 0, s + height, height) for s in starts]
    return [(0, s, width, s + width) for s in starts]


def _preprocess_tile(tile, crop_size: int):
    """Square RGB PIL image -> NCHW float32 tensor normalized with ImageNet stats."""
    import numpy as np
    from PIL import Image

    resize_shortest = (
        INPUT_RESIZE_SHORTEST
        if crop_size == INPUT_CROP_SIZE
        else max(crop_size, round(crop_size * INPUT_RESIZE_SHORTEST / INPUT_CROP_SIZE))
    )
    resized = tile.resize((resize_shortest, resize_shortest), Image.Resampling.BILINEAR)
    offset = int(round((resize_shortest - crop_size) / 2.0))
    cropped = resized.crop((offset, offset, offset + crop_size, offset + crop_size))
    pixels = np.asarray(cropped, dtype=np.float32) / np.float32(255.0)
    mean = np.asarray(NORMALIZE_MEAN, dtype=np.float32)
    std = np.asarray(NORMALIZE_STD, dtype=np.float32)
    normalized = (pixels - mean) / std
    tensor = np.transpose(normalized, (2, 0, 1))[np.newaxis, ...]
    return np.ascontiguousarray(tensor, dtype=np.float32)


def _preprocess(image: bytes, width: int, height: int):
    """JPEG/PNG bytes -> list of ``(tensor, normalized_box)`` tiles for the classifier."""
    from PIL import Image

    if width != height or width <= 0:
        raise LocalVisionError(f"local detector requires square input, got {width}x{height}")
    with Image.open(io.BytesIO(image)) as frame:
        rgb = frame.convert("RGB")
        img_w, img_h = rgb.size
        tiles = []
        for x1, y1, x2, y2 in _tile_windows(img_w, img_h):
            tile = rgb.crop((x1, y1, x2, y2))
            tensor = _preprocess_tile(tile, width)
            norm_box = (x1 / img_w, y1 / img_h, x2 / img_w, y2 / img_h)
            tiles.append((tensor, norm_box))
    return tiles


def _read_probability(output) -> float:
    """Validate and extract the single scalar failure probability from ``outputs[0]``."""
    import numpy as np

    arr = np.asarray(output, dtype=np.float64)
    if arr.size != 1 or arr.ndim > 2:
        raise LocalVisionError(
            f"unexpected local detector probability output shape: {arr.shape}")
    score = float(arr.reshape(-1)[0])
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        raise LocalVisionError(
            f"local detector returned an out-of-range probability: {score!r}")
    return score


def _postprocess(tile_scores):
    """Keep tiles at or above :data:`POSITIVE_FLOOR`, strongest first."""
    kept = [
        (float(score), box)
        for score, box in tile_scores
        if float(score) >= POSITIVE_FLOOR
    ]
    kept.sort(key=lambda pair: -pair[0])
    return kept


def detect(image: bytes, cfg=None):
    """Run the detector on one camera frame. Returns kept positive
    ``(confidence, normalized_tile_box)`` pairs strongest first (empty
    when all tiles score below :data:`POSITIVE_FLOOR`). Raises
    :class:`LocalVisionError` on any infrastructure failure (runtime,
    model, inference)."""
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
        tiles = _preprocess(image, width, height)
    except LocalVisionError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable frame is a failed check
        raise LocalVisionError(f"the camera frame could not be read: {exc}") from exc
    scored = []
    for tensor, norm_box in tiles:
        try:
            outputs = session.run(None, {spec.name: tensor})
        except LocalVisionError:
            raise
        except Exception as exc:  # noqa: BLE001 - inference errors degrade like endpoint errors
            raise LocalVisionError(f"local detector inference failed: {exc}") from exc
        if not outputs:
            raise LocalVisionError("the local detector model returned no outputs")
        try:
            score = _read_probability(outputs[0])
        except LocalVisionError:
            raise
        except Exception as exc:  # noqa: BLE001 - malformed outputs are a failed check
            raise LocalVisionError(f"local detector outputs could not be read: {exc}") from exc
        scored.append((score, norm_box))
    return _postprocess(scored)


#: The words a weak ("possible failure") detection's detail carries.
#: Display only: callers decide on :class:`Detection`, never on this text.
BELOW_THRESHOLD = "below the single-frame failure threshold"


def percent(confidence) -> int:
    """A confidence in whole percent, rounded down.

    Rounded down so a sub-threshold value never reads as the threshold:
    0.7499 shows as 74%, not 75%.
    """
    return math.floor(float(confidence) * 100 + 1e-9)


class Detection(NamedTuple):
    """The detector's structured result for one frame with a positive score.

    ``confidence`` is the strongest tile probability (0..1), ``seen`` how
    many independent consecutive checks saw a positive (this one included,
    1 for a single-frame judgement), ``counted`` whether this frame was
    one of them (a frame too soon after, or unchanged from, the last
    counted one is not), and ``verdict`` the verdict it produced.
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
    regions = len(kept)
    region_note = f" ({regions} regions)" if regions > 1 else ""
    reason = f"local failure detector: print failure at {confidence:.0%} confidence{region_note}"
    if positives >= 2:
        reason += f", confirmed across {positives} consecutive checks"
    return reason


def _single_frame_verdict(kept) -> Verdict:
    """One frame's verdict, single-frame semantics: the mapping the bands
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

    Without ``streak`` the frame is judged on its own. With a
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
                       "needs the AI endpoint or this detector")
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


def model_pending(cfg=None) -> bool:
    """Whether the detector waits only on its first model download: on,
    the runtime present, no remembered failure, and the model file not
    there yet. The detector is on by default, so a fresh install is in
    this state until the background download finishes; it is not a
    fault. Never touches the network."""
    cfg = _config.cfg() if cfg is None else cfg
    if not watch_uses_detector(cfg):
        return False
    try:
        _runtime()
    except LocalVisionError:
        return False
    path = model_path(cfg)
    if _remembered_failure(f"file:{path}") or _remembered_failure(f"session:{path}"):
        return False
    return not path.is_file()
