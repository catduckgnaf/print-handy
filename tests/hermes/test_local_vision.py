"""Local ONNX failure detector (#455 Stage 1): verdict mapping, fail-soft
paths, route precedence and the settings/bot-check surfaces.

All offline. The detector runs against a tiny synthetic ONNX fixture
(fixtures/tiny_failure_detector.onnx, built by
fixtures/build_tiny_detector.py) that speaks the real model's I/O
contract — input [1, 3, H, W], outputs boxes [1, N, 1, 4] / confs
[1, N, 1] — with confidences driven by frame brightness, so each verdict
band is reachable with a solid-colour frame. Downloads are faked at the
urlopen seam; no test touches the network or the real 202 MB model.
"""
import copy
import hashlib
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from print_handy import settings
from print_handy.hermes import _config, local_vision as lv, visual_watch as vw

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_failure_detector.onnx"
FIXTURE_SHA256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _fresh_detector_state(monkeypatch):
    """The detector caches sessions/failures per process; every test
    starts from a clean slate and no PRINT_VISION_URL leaks in."""
    monkeypatch.setattr(lv, "_sessions", {})
    monkeypatch.setattr(lv, "_active_providers", {})
    monkeypatch.setattr(lv, "_failures", {})
    monkeypatch.setattr(lv, "_verified_files", set())
    monkeypatch.setattr(lv, "_ort", None)
    monkeypatch.setattr(lv, "_ort_error", None)
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)


def frame(value, size=(120, 90)):
    buf = io.BytesIO()
    Image.new("RGB", size, (value, value, value)).save(buf, "JPEG")
    return buf.getvalue()


def cfg_with(**vision):
    base = {"local_detector_enabled": True, "local_model_path": str(FIXTURE)}
    base.update(vision)
    return {"vision": base, "paths": {"hermes_home": "/nonexistent-home"}}


@pytest.fixture
def process_cfg():
    """Install a full process config (deep copy of the live one) with the
    detector enabled against the fixture; restored on teardown."""
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    data["vision"]["local_detector_enabled"] = True
    data["vision"]["local_model_path"] = str(FIXTURE)
    _config.set_override(data)
    yield data
    _config.set_override(previous)


# --------------------------------------------------------------- verdict mapping


def test_bright_frame_is_a_failure_with_areas_counted():
    verdict, detail = lv.local_verdict(frame(235), cfg_with())
    assert verdict == "spaghetti"
    assert "100%" in detail and "2 areas" in detail


def test_mid_frame_is_uncertain_not_a_pass():
    verdict, detail = lv.local_verdict(frame(84), cfg_with())
    assert verdict == "uncertain"
    assert "35%" in detail


def test_dark_frame_is_ok():
    verdict, detail = lv.local_verdict(frame(13), cfg_with())
    assert verdict == "ok"
    assert "no failure detected" in detail


def test_detector_never_produces_endpoint_only_verdicts():
    for value in (0, 40, 84, 160, 235, 255):
        verdict, _ = lv.local_verdict(frame(value), cfg_with())
        assert verdict in ("ok", "uncertain", "spaghetti")


# --------------------------------------------------------------- post-processing


def _boxes(pairs):
    arr = np.zeros((1, len(pairs), 1, 4), dtype=np.float64)
    for i, (box, _conf) in enumerate(pairs):
        arr[0, i, 0] = box
    confs = np.array([[[c]] for _b, c in pairs], dtype=np.float64).reshape(1, len(pairs), 1)
    return arr, confs


def test_nms_suppresses_overlapping_weaker_boxes():
    boxes, confs = _boxes([
        ([0.10, 0.10, 0.50, 0.50], 0.9),
        ([0.15, 0.15, 0.55, 0.55], 0.8),   # IoU ~0.62 with box 0: suppressed
        ([0.70, 0.70, 0.95, 0.95], 0.7),   # disjoint: kept
        ([0.30, 0.60, 0.40, 0.70], 0.05),  # below the positive floor: dropped
    ])
    kept = lv._postprocess(boxes, confs)
    assert [round(c, 2) for c, _b in kept] == [0.9, 0.7]


def test_postprocess_rejects_unexpected_shapes():
    with pytest.raises(lv.LocalVisionError):
        lv._postprocess(np.zeros((3, 4)), np.zeros((3,)))


def test_preprocess_shape_range_and_dtype(monkeypatch):
    seen = {}

    class RecordingSession:
        def get_inputs(self):
            return [SimpleNamespace(name="input", shape=[1, 3, 64, 64])]

        def run(self, _outputs, feed):
            seen["tensor"] = feed["input"]
            boxes, confs = _boxes([([0.1, 0.1, 0.5, 0.5], 0.9)])
            return [boxes, confs]

    monkeypatch.setattr(lv, "_session", lambda path: RecordingSession())
    verdict, _detail = lv.local_verdict(frame(200, size=(37, 211)), cfg_with())
    assert verdict == "spaghetti"
    tensor = seen["tensor"]
    assert tensor.shape == (1, 3, 64, 64) and tensor.dtype == np.float32
    assert 0.0 <= float(tensor.min()) and float(tensor.max()) <= 1.0


# --------------------------------------------------------------- fail-soft paths


def test_missing_runtime_is_a_clean_failure(monkeypatch):
    monkeypatch.setattr(lv, "_ort_error", RuntimeError("no onnxruntime here"))
    with pytest.raises(lv.LocalVisionError, match="onnxruntime is not available"):
        lv.local_verdict(frame(200), cfg_with())
    state, detail = lv.detector_status(cfg_with())
    assert state == "unavailable" and "onnxruntime" in detail


def test_corrupt_model_fails_soft_and_sticks(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.onnx"
    bad.write_bytes(b"this is not an onnx model")
    cfg = cfg_with(local_model_path=str(bad))
    with pytest.raises(lv.LocalVisionError):
        lv.local_verdict(frame(200), cfg)
    # The failure is remembered: the second call fails the same way
    # without another load attempt (no repeated work per camera check).
    loads = []
    monkeypatch.setattr(lv, "_runtime", lambda: loads.append(1) or (_ for _ in ()).throw(
        AssertionError("runtime must not be retried")))
    with pytest.raises(lv.LocalVisionError):
        lv.local_verdict(frame(200), cfg)
    assert loads == []


def test_inference_error_degrades_to_local_vision_error(monkeypatch):
    class ExplodingSession:
        def get_inputs(self):
            return [SimpleNamespace(name="input", shape=[1, 3, 64, 64])]

        def run(self, _outputs, _feed):
            raise RuntimeError("tensor exploded")

    monkeypatch.setattr(lv, "_session", lambda path: ExplodingSession())
    with pytest.raises(lv.LocalVisionError, match="inference failed"):
        lv.local_verdict(frame(200), cfg_with())


def test_unreadable_frame_is_a_failed_check_not_a_crash():
    with pytest.raises(lv.LocalVisionError):
        lv.local_verdict(b"not a jpeg at all", cfg_with())


# --------------------------------------------------------------- model download


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_missing_model_downloads_once_and_verifies(monkeypatch, tmp_path):
    target = tmp_path / "models" / "detector.onnx"
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        return _FakeResponse(FIXTURE.read_bytes())

    monkeypatch.setattr(lv, "MODEL_SHA256", FIXTURE_SHA256)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = cfg_with(local_model_path=str(target))
    verdict, _ = lv.local_verdict(frame(235), cfg)
    assert verdict == "spaghetti"
    assert target.read_bytes() == FIXTURE.read_bytes()
    assert calls == [lv.MODEL_URL]  # the pinned source, fetched exactly once
    verdict, _ = lv.local_verdict(frame(235), cfg)
    assert verdict == "spaghetti" and len(calls) == 1  # file + session are reused


def test_download_with_wrong_checksum_is_refused(monkeypatch, tmp_path):
    target = tmp_path / "detector.onnx"
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _FakeResponse(FIXTURE.read_bytes()))
    cfg = cfg_with(local_model_path=str(target))  # real MODEL_SHA256 stays: mismatch
    with pytest.raises(lv.LocalVisionError, match="SHA-256"):
        lv.local_verdict(frame(235), cfg)
    assert not target.exists() and not target.with_name("detector.onnx.part").exists()


def test_model_download_waits_for_another_process_and_rechecks(monkeypatch, tmp_path):
    """P2-221: supervise.sh runs one listener process per printer, all
    sharing the model directory. The download must serialize across
    processes (flock, not just the in-process threading lock), and a
    process that waited must recheck for the finished file instead of
    downloading over it — the old shared-.part race ended in a failed
    os.replace that the loser then remembered as a permanent failure."""
    import threading

    target = tmp_path / "models" / "detector.onnx"
    target.parent.mkdir(parents=True)
    cfg = cfg_with(local_model_path=str(target))
    downloads = []
    monkeypatch.setattr(lv, "_download_model", lambda path: downloads.append(path))

    holder = lv._model_lock(target)  # a separate lock instance: a peer process
    holder.acquire()
    worker = threading.Thread(target=lv._ensure_model_file, args=(target, cfg), daemon=True)
    try:
        worker.start()
        worker.join(timeout=0.5)
        assert worker.is_alive() and downloads == []  # still waiting on the peer
        target.write_bytes(b"model")  # the peer finished and published the file
    finally:
        holder.release()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert downloads == []  # the recheck saw the file: no second download
    assert lv._remembered_failure(f"file:{target}") is None


def test_download_uses_a_process_unique_part_file(monkeypatch, tmp_path):
    """P2-221: the in-flight download never shares one .part path across
    processes; a foreign .part (a peer's, under the old shared name) is
    left untouched and nothing of ours is left behind."""
    target = tmp_path / "detector.onnx"
    shared = target.with_name("detector.onnx.part")
    shared.write_bytes(b"a peer is mid-download here")
    monkeypatch.setattr(lv, "MODEL_SHA256", FIXTURE_SHA256)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _FakeResponse(FIXTURE.read_bytes()))
    lv._download_model(target)
    assert target.read_bytes() == FIXTURE.read_bytes()
    assert shared.read_bytes() == b"a peer is mid-download here"
    assert [p.name for p in tmp_path.glob("*.part")] == ["detector.onnx.part"]


def test_existing_model_needs_no_download_lock(monkeypatch, tmp_path):
    """2.2.5 review: a present model is used without the download flock, so a
    read-only model directory (no lock file can be created) works on every frame."""
    target = tmp_path / "detector.onnx"
    target.write_bytes(FIXTURE.read_bytes())

    def read_only_lock(path):
        raise PermissionError("read-only model directory")

    monkeypatch.setattr(lv, "_model_lock", read_only_lock)
    cfg = cfg_with(local_model_path=str(target))
    for _ in range(2):
        verdict, _ = lv.local_verdict(frame(235), cfg)
        assert verdict == "spaghetti"


def test_download_lock_failure_is_a_local_vision_error(monkeypatch, tmp_path):
    """2.2.5 review: an OSError taking the download lock degrades like any other
    unprepared model (LocalVisionError, remembered), never a raw OSError."""
    target = tmp_path / "detector.onnx"

    def read_only_lock(path):
        raise PermissionError("read-only model directory")

    monkeypatch.setattr(lv, "_model_lock", read_only_lock)
    with pytest.raises(lv.LocalVisionError, match="could not be prepared"):
        lv._ensure_model_file(target)
    assert lv._remembered_failure(f"file:{target}")


def test_failed_publish_leaves_no_part_file(monkeypatch, tmp_path):
    """2.2.5 review: a failed os.replace removes this process's .part."""
    target = tmp_path / "detector.onnx"
    monkeypatch.setattr(lv, "MODEL_SHA256", FIXTURE_SHA256)
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _FakeResponse(FIXTURE.read_bytes()))

    def failing_replace(src, dst):
        raise OSError("cross-device")

    monkeypatch.setattr(lv.os, "replace", failing_replace)
    with pytest.raises(lv.LocalVisionError, match="publishing"):
        lv._download_model(target)
    assert list(tmp_path.glob("*.part")) == []


def test_failed_download_is_not_retried_every_check(monkeypatch, tmp_path):
    target = tmp_path / "detector.onnx"
    calls = []

    def failing_urlopen(request, timeout=None):
        calls.append(1)
        raise OSError("network is down")

    monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
    cfg = cfg_with(local_model_path=str(target))
    for _ in range(2):
        with pytest.raises(lv.LocalVisionError, match="network is down"):
            lv.local_verdict(frame(235), cfg)
    assert len(calls) == 1


def test_preexisting_default_model_must_match_the_pinned_checksum(monkeypatch, tmp_path):
    home = tmp_path / "home"
    target = home / "models" / lv.MODEL_FILENAME
    target.parent.mkdir(parents=True)
    target.write_bytes(FIXTURE.read_bytes())  # a valid model, but not the pinned one
    cfg = {"vision": {"local_detector_enabled": True, "local_model_path": ""},
           "paths": {"hermes_home": home}}
    with pytest.raises(lv.LocalVisionError, match="does not match the pinned"):
        lv.local_verdict(frame(235), cfg)


# --------------------------------------------------------------- detector status


def test_status_off_by_default():
    state, detail = lv.detector_status(cfg_with(local_detector_enabled=False))
    assert state == "off" and "local_detector_enabled" in detail


def test_status_ready_with_model_present():
    state, detail = lv.detector_status(cfg_with())
    assert state == "ready" and "present" in detail


def test_status_ready_model_downloads_on_first_use(tmp_path):
    cfg = cfg_with(local_model_path=str(tmp_path / "not-yet.onnx"))
    state, detail = lv.detector_status(cfg)
    assert state == "ready" and "downloads on first use" in detail


# --------------------------------------------------------------- settings


def test_settings_defaults_and_env_form():
    data = settings.load(home=settings.HOME, local=False, environ={})
    assert data["vision"]["local_detector_enabled"] is False
    assert data["vision"]["local_model_path"] == ""
    env = {"PRINT_HANDY__VISION__LOCAL_DETECTOR_ENABLED": "true",
           "PRINT_HANDY__VISION__LOCAL_MODEL_PATH": "/models/mine.onnx"}
    data = settings.load(home=settings.HOME, local=False, environ=env)
    assert data["vision"]["local_detector_enabled"] is True
    assert data["vision"]["local_model_path"] == "/models/mine.onnx"


def test_settings_reject_wrong_types(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("vision:\n  local_detector_enabled: sometimes\n")
    with pytest.raises(settings.SettingsError, match="local_detector_enabled"):
        settings.load(home=settings.HOME, local=local, environ={})
    local.write_text("vision:\n  local_model_path: 42\n")
    with pytest.raises(settings.SettingsError, match="local_model_path"):
        settings.load(home=settings.HOME, local=local, environ={})


# --------------------------------------------------------------- verdict routing


def _verdict(image, **overrides):
    call = dict(chat_url=lambda: "", model="m", api_key=lambda: "", context="")
    call.update(overrides)
    return vw.visual_verdict(image, "prompt", **call)


def _chat_response(verdict="ok", reason="chat endpoint"):
    body = {"choices": [{"message": {
        "content": json.dumps({"verdict": verdict, "reason": reason})}}]}
    return _FakeResponse(json.dumps(body).encode())


def _url_response(verdict="ok", reason="custom endpoint"):
    return _FakeResponse(json.dumps({"verdict": verdict, "reason": reason}).encode())


def test_print_vision_url_wins_and_local_is_not_consulted(monkeypatch, process_cfg):
    monkeypatch.setenv("PRINT_VISION_URL", "http://detector.invalid/judge")
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _url_response())
    consulted = []
    monkeypatch.setattr(lv, "local_verdict",
                        lambda image: consulted.append(1) or ("spaghetti", "local"))
    verdict, detail = _verdict(frame(235))
    assert (verdict, detail) == ("ok", "custom endpoint")
    assert consulted == []


def test_chat_endpoint_wins_when_configured(monkeypatch, process_cfg):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda request, timeout=None: _chat_response())
    consulted = []
    monkeypatch.setattr(lv, "local_verdict",
                        lambda image: consulted.append(1) or ("spaghetti", "local"))
    verdict, detail = _verdict(
        frame(235), chat_url=lambda: "http://vision.invalid/v1/chat/completions",
        api_key=lambda: "key")
    assert (verdict, detail) == ("ok", "chat endpoint")
    assert consulted == []


def test_configured_chat_endpoint_failure_does_not_fall_through(monkeypatch, process_cfg):
    monkeypatch.setattr(vw._config, "service_url",
                        lambda key: "http://vision.invalid/v1" if key == "vision_chat_url" else "")

    def failing_urlopen(request, timeout=None):
        raise OSError("endpoint down")

    monkeypatch.setattr("urllib.request.urlopen", failing_urlopen)
    consulted = []
    monkeypatch.setattr(lv, "local_verdict",
                        lambda image: consulted.append(1) or ("spaghetti", "local"))
    verdict, detail = _verdict(
        frame(235), chat_url=lambda: "http://vision.invalid/v1/chat/completions",
        api_key=lambda: "key")
    assert (verdict, detail) == ("uncertain", "the vision check could not be completed")
    assert consulted == []


def test_no_key_and_local_enabled_local_detector_answers(process_cfg):
    verdict, detail = _verdict(frame(235))
    assert verdict == "spaghetti" and "local failure detector" in detail


def test_half_configured_chat_route_local_detector_answers(monkeypatch, process_cfg):
    """CLIPROXY_API_KEY present but services.vision_chat_url unset: the
    injected chat_url raises (the listener's _config.require), which is
    'no endpoint route configured', so the local detector stands in."""
    monkeypatch.setattr(vw._config, "service_url",
                        lambda key: "" if key == "vision_chat_url" else "")

    def raising_chat_url():
        raise RuntimeError("services.vision_chat_url is not configured")

    verdict, detail = _verdict(frame(13), chat_url=raising_chat_url, api_key=lambda: "key")
    assert verdict == "ok" and "local failure detector" in detail


def test_no_key_and_local_disabled_is_the_existing_missing_key_result(monkeypatch):
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    data["vision"]["local_detector_enabled"] = False
    _config.set_override(data)
    try:
        verdict, detail = _verdict(frame(235))
    finally:
        _config.set_override(previous)
    assert (verdict, detail) == ("uncertain", "vision key is not configured")


def test_local_failure_inside_verdict_degrades_generically(monkeypatch, process_cfg):
    monkeypatch.setattr(lv, "_ort_error", RuntimeError("no runtime"))
    verdict, detail = _verdict(frame(235))
    assert (verdict, detail) == ("uncertain", "the vision check could not be completed")


def test_visual_extract_never_uses_the_local_detector(monkeypatch, process_cfg):
    consulted = []
    monkeypatch.setattr(lv, "local_verdict",
                        lambda image: consulted.append(1) or ("spaghetti", "local"))
    result = vw.visual_extract(frame(235), "read the label",
                               chat_url=lambda: "", model="m",
                               api_key=lambda: "", context="")
    assert result is None and consulted == []


# --------------------------------------------------------------- temporal escalation


def _kept(conf):
    return [(conf, [0.1, 0.1, 0.5, 0.5])]


def test_streak_weak_positive_escalates_on_the_second_consecutive_check():
    streak = lv.FailureStreak()
    verdict, detail = streak.note(_kept(0.35))
    assert verdict == "uncertain" and "first consecutive detection" in detail
    verdict, detail = streak.note(_kept(0.35))
    assert verdict == "spaghetti" and "2 consecutive checks" in detail


def test_streak_clean_frame_clears_and_the_count_restarts():
    streak = lv.FailureStreak()
    streak.note(_kept(0.35))
    verdict, detail = streak.note([])
    assert verdict == "ok" and "did not persist" in detail
    assert streak.positives == 0
    verdict, _ = streak.note(_kept(0.35))
    assert verdict == "uncertain"  # a fresh first detection, not an escalation


def test_streak_clean_frame_without_a_streak_is_the_plain_ok():
    streak = lv.FailureStreak()
    verdict, detail = streak.note([])
    assert (verdict, detail) == ("ok", "local failure detector: no failure detected")


def test_streak_strong_frame_is_spaghetti_at_once_then_streak_confirmed():
    streak = lv.FailureStreak()
    verdict, detail = streak.note(_kept(0.9))
    assert verdict == "spaghetti" and "consecutive" not in detail
    verdict, detail = streak.note(_kept(0.9))
    assert verdict == "spaghetti" and "confirmed across 2 consecutive checks" in detail


def test_streak_weak_then_strong_is_streak_confirmed():
    streak = lv.FailureStreak()
    streak.note(_kept(0.35))
    verdict, detail = streak.note(_kept(0.9))
    assert verdict == "spaghetti" and "confirmed across 2 consecutive checks" in detail


def test_streak_counter_is_bounded_and_reset_clears():
    streak = lv.FailureStreak()
    for _ in range(lv.STREAK_CAP + 50):
        streak.note(_kept(0.35))
    assert streak.positives == lv.STREAK_CAP
    streak.reset()
    assert streak.positives == 0
    verdict, _ = streak.note(_kept(0.35))
    assert verdict == "uncertain"


def test_local_verdict_with_streak_escalates_across_frames():
    streak = lv.FailureStreak()
    verdict, _ = lv.local_verdict(frame(84), cfg_with(), streak=streak)
    assert verdict == "uncertain"
    verdict, detail = lv.local_verdict(frame(84), cfg_with(), streak=streak)
    assert verdict == "spaghetti" and "consecutive checks" in detail
    # Without a streak the same frames stay single-frame verdicts.
    assert lv.local_verdict(frame(84), cfg_with())[0] == "uncertain"
    assert lv.local_verdict(frame(84), cfg_with())[0] == "uncertain"


def test_detector_failure_neither_builds_nor_clears_the_streak(monkeypatch):
    streak = lv.FailureStreak()
    verdict, _ = lv.local_verdict(frame(84), cfg_with(), streak=streak)
    assert verdict == "uncertain" and streak.positives == 1
    real_detect = lv.detect
    calls = []

    def exploding_detect(image, cfg=None):
        calls.append(1)
        raise lv.LocalVisionError("inference exploded")

    monkeypatch.setattr(lv, "detect", exploding_detect)
    with pytest.raises(lv.LocalVisionError):
        lv.local_verdict(frame(84), cfg_with(), streak=streak)
    assert streak.positives == 1  # the failed check left the streak untouched
    monkeypatch.setattr(lv, "detect", real_detect)
    verdict, _ = lv.local_verdict(frame(84), cfg_with(), streak=streak)
    assert verdict == "spaghetti"  # the next real positive completes the streak


def test_visual_verdict_threads_the_streak(process_cfg):
    streak = lv.FailureStreak()
    verdict, _ = _verdict(frame(84), local_streak=streak)
    assert verdict == "uncertain"
    verdict, detail = _verdict(frame(84), local_streak=streak)
    assert verdict == "spaghetti" and "consecutive checks" in detail
    # No streak passed: single-frame behaviour, however often it repeats.
    assert _verdict(frame(84))[0] == "uncertain"
    assert _verdict(frame(84))[0] == "uncertain"


def test_listener_verdict_threads_its_streak(process_cfg):
    from print_handy.hermes import moonraker_event_listener as mel
    listener = mel.Listener()
    assert isinstance(listener.local_streak, lv.FailureStreak)
    verdict, _ = mel._visual_verdict(frame(84), "prompt", listener.local_streak)
    assert verdict == "uncertain"
    verdict, _ = mel._visual_verdict(frame(84), "prompt", listener.local_streak)
    assert verdict == "spaghetti"
    # The idle-check form (no streak) stays single-frame.
    assert mel._visual_verdict(frame(84), "prompt")[0] == "uncertain"


def test_bambu_monitor_verdict_threads_its_streak(process_cfg):
    from print_handy.hermes import bambu_watch
    adapter = SimpleNamespace(label="Test Bambu")
    streak = lv.FailureStreak()
    verdict = bambu_watch._default_verdict(adapter, streak=streak)
    assert verdict(frame(84), "prompt")[0] == "uncertain"
    assert verdict(frame(84), "prompt")[0] == "spaghetti"
    single = bambu_watch._default_verdict(adapter)
    assert single(frame(84), "prompt")[0] == "uncertain"
    assert single(frame(84), "prompt")[0] == "uncertain"


# --------------------------------------------------------------- execution providers


class _FakeSession:
    def __init__(self, providers):
        self._providers = providers

    def get_providers(self):
        return list(self._providers)


class _FakeOrt:
    """onnxruntime stand-in: reports a fixed provider list and records
    every session request. No GPU exists in the test environment, so
    accelerated providers are only ever simulated here — live GPU proof
    is an operator follow-up, as the docs state."""

    def __init__(self, available, refuse_accelerated=False):
        self._available = available
        self._refuse = refuse_accelerated
        self.requested = []

    def get_available_providers(self):
        return list(self._available)

    def InferenceSession(self, _path, providers=None):
        providers = list(providers or [])
        self.requested.append(providers)
        if self._refuse and providers != ["CPUExecutionProvider"]:
            raise RuntimeError("accelerator cannot initialise")
        return _FakeSession(providers)


def test_select_providers_prefers_in_the_documented_order():
    both = ["CPUExecutionProvider", "TensorRTExecutionProvider", "CUDAExecutionProvider"]
    assert lv.select_providers(both) == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert lv.select_providers(["OpenVINOExecutionProvider"]) == [
        "OpenVINOExecutionProvider", "CPUExecutionProvider"]
    assert lv.select_providers(["CoreMLExecutionProvider", "CPUExecutionProvider"]) == [
        "CoreMLExecutionProvider", "CPUExecutionProvider"]
    assert lv.select_providers(["DmlExecutionProvider"]) == [
        "DmlExecutionProvider", "CPUExecutionProvider"]


def test_select_providers_cpu_only_and_degenerate_inputs():
    assert lv.select_providers(["CPUExecutionProvider"]) == ["CPUExecutionProvider"]
    assert lv.select_providers([]) == ["CPUExecutionProvider"]
    assert lv.select_providers(None) == ["CPUExecutionProvider"]
    assert lv.select_providers(["AzureExecutionProvider"]) == ["CPUExecutionProvider"]


def test_session_uses_the_selected_accelerated_provider(monkeypatch, tmp_path):
    fake = _FakeOrt(["CPUExecutionProvider", "CUDAExecutionProvider"])
    monkeypatch.setattr(lv, "_ort", fake)
    path = tmp_path / "model.onnx"
    lv._session(path)
    assert fake.requested == [["CUDAExecutionProvider", "CPUExecutionProvider"]]
    assert lv.active_provider(cfg_with(local_model_path=str(path))) == "CUDAExecutionProvider"


def test_session_logs_the_active_provider_once(monkeypatch, tmp_path, caplog):
    fake = _FakeOrt(["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(lv, "_ort", fake)
    path = tmp_path / "model.onnx"
    with caplog.at_level(logging.INFO, logger=lv.__name__):
        lv._session(path)
        lv._session(path)  # cached: no second session, no second line
    assert len(fake.requested) == 1
    lines = [r for r in caplog.records
             if "execution provider: CUDAExecutionProvider" in r.getMessage()]
    assert len(lines) == 1


def test_session_falls_back_to_cpu_when_the_accelerator_cannot_initialise(
        monkeypatch, tmp_path):
    fake = _FakeOrt(["CUDAExecutionProvider", "CPUExecutionProvider"],
                    refuse_accelerated=True)
    monkeypatch.setattr(lv, "_ort", fake)
    path = tmp_path / "model.onnx"
    lv._session(path)
    assert fake.requested == [["CUDAExecutionProvider", "CPUExecutionProvider"],
                              ["CPUExecutionProvider"]]
    assert lv.active_provider(cfg_with(local_model_path=str(path))) == "CPUExecutionProvider"


def test_session_on_a_cpu_only_host_requests_cpu(monkeypatch, tmp_path):
    fake = _FakeOrt(["CPUExecutionProvider"])
    monkeypatch.setattr(lv, "_ort", fake)
    path = tmp_path / "model.onnx"
    lv._session(path)
    assert fake.requested == [["CPUExecutionProvider"]]
    assert lv.active_provider(cfg_with(local_model_path=str(path))) == "CPUExecutionProvider"


def test_active_provider_is_none_before_any_session():
    assert lv.active_provider(cfg_with()) is None


def test_selected_provider_reports_the_head_without_a_session(monkeypatch):
    fake = _FakeOrt(["OpenVINOExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(lv, "_ort", fake)
    assert lv.selected_provider() == "OpenVINOExecutionProvider"
    assert fake.requested == []  # no session was created to find out


def test_selected_provider_is_none_when_the_runtime_is_unusable(monkeypatch):
    monkeypatch.setattr(lv, "_ort_error", RuntimeError("no onnxruntime here"))
    assert lv.selected_provider() is None


def test_status_names_the_execution_provider():
    state, detail = lv.detector_status(cfg_with())
    assert state == "ready" and "execution provider: CPUExecutionProvider" in detail


# --------------------------------------------------------------- vision_ready


def test_vision_ready_with_local_detector(process_cfg):
    assert vw.vision_ready(lambda: "", None) is True


def test_vision_ready_local_unavailable_stays_off(monkeypatch, process_cfg):
    monkeypatch.setattr(lv, "_ort_error", RuntimeError("no runtime"))
    monkeypatch.setattr(vw, "_vision_missing_logged", False)
    lines = []
    assert vw.vision_ready(lambda: "", lines.append) is False
    assert len(lines) == 1 and "vision.local_detector_enabled" in lines[0]


# --------------------------------------------------------------- GPU visible, CPU-only build (#455)


def test_accelerator_hint_names_a_cpu_only_build_with_a_gpu_visible():
    hint = lv.accelerator_hint(["CPUExecutionProvider"], device_visible=True)
    assert "GPU device is visible" in hint and "only CPUExecutionProvider" in hint
    assert "docs/DOCKER.md" in hint


def test_accelerator_hint_silent_without_a_gpu_or_with_an_accelerated_build():
    assert lv.accelerator_hint(["CPUExecutionProvider"], device_visible=False) == ""
    assert lv.accelerator_hint(["OpenVINOExecutionProvider", "CPUExecutionProvider"],
                               device_visible=True) == ""
    assert lv.accelerator_hint(["CUDAExecutionProvider"], device_visible=True) == ""


def test_gpu_device_visible_reads_render_and_nvidia_nodes(tmp_path):
    dri, dev = tmp_path / "dri", tmp_path / "dev"
    dri.mkdir()
    dev.mkdir()
    assert lv.gpu_device_visible(dri, dev) is False
    (dev / "nvidiactl").write_text("")
    assert lv.gpu_device_visible(dri, dev) is False
    (dri / "renderD128").write_text("")
    assert lv.gpu_device_visible(dri, dev) is True
    (dri / "renderD128").unlink()
    (dev / "nvidia0").write_text("")
    assert lv.gpu_device_visible(dri, dev) is True
    assert lv.gpu_device_visible(tmp_path / "missing", tmp_path / "missing") is False


def test_status_names_the_cpu_only_build_when_a_gpu_is_visible(monkeypatch):
    monkeypatch.setattr(lv, "_ort", _FakeOrt(["CPUExecutionProvider"]))
    monkeypatch.setattr(lv, "gpu_device_visible", lambda *a, **k: True)
    state, detail = lv.detector_status(cfg_with())
    assert state == "ready" and "execution provider: CPUExecutionProvider" in detail
    assert "GPU device is visible" in detail


def test_status_has_no_gpu_note_without_a_gpu(monkeypatch):
    monkeypatch.setattr(lv, "_ort", _FakeOrt(["CPUExecutionProvider"]))
    monkeypatch.setattr(lv, "gpu_device_visible", lambda *a, **k: False)
    state, detail = lv.detector_status(cfg_with())
    assert state == "ready" and "GPU" not in detail
