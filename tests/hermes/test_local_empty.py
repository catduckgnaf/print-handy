"""Local empty-plate producer (#455 item 4, Stage 1): the calibrated
plate-reference producer, its calibration store/status, and the routing
rules that keep ``empty_idle`` an idle-only, reference-only verdict.

Offline throughout: frames are synthetic JPEGs drawn with PIL in the
Stage 0 fixture layout (a session manifest plus per-frame entries with
label/source/license/session/order), so the accuracy gates below are
measured on synthetic frames, not on a real camera — the scope's gate
numbers are the contract, and the PR body states what they were
measured on. The producer itself never guesses: anything it cannot
compare cleanly (no reference, a stale reference, a route the setting
does not allow, a dark frame, a decode failure) comes back as "no local
empty verdict", and the caller falls back to today's behavior.
"""
import io
import json
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from print_handy.hermes import local_empty as le
from print_handy.hermes import visual_watch as vw


def jpeg(draw_fn, size=(320, 240)):
    image = Image.new("RGB", size, (16, 16, 18))
    draw_fn(ImageDraw.Draw(image), size)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def scene(part=None, *, gain=1.0, offset=0, plate=(120, 122, 125)):
    """One synthetic camera scene: plate, parked gantry, optional part.

    ``gain``/``offset`` simulate lighting states; ``part`` is
    (x0, y0, x1, y1, color) in frame coordinates.
    """
    def draw(d, size):
        w, h = size
        def c(rgb):
            return tuple(max(0, min(255, round(v * gain) + offset)) for v in rgb)
        d.rectangle([0, 0, w, h], fill=c((16, 16, 18)))
        d.rectangle([20, 30, w - 20, h - 15], fill=c(plate))          # plate
        d.rectangle([20, 30, w - 20, 44], fill=c((60, 60, 64)))       # gantry bar
        d.rectangle([270, 44, 292, 96], fill=c((40, 40, 44)))         # parked toolhead
        if part is not None:
            d.rectangle(part[:4], fill=c(part[4]))
    return jpeg(draw)


EMPTY = scene()
PART_BIG = scene(part=(110, 110, 210, 175, (200, 60, 40)))
PART_SMALL_LIGHT = scene(part=(150, 120, 176, 142, (238, 236, 230)))


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    data = {"paths": {"hermes_home": str(tmp_path)},
            "vision": {"local_empty_enabled": True, "watch_source": "local"}}
    # visual_watch's vision_ready reads the process-global config, so
    # the calibrated fixture config must be the global one too.
    from print_handy.hermes import _config as _hc
    monkeypatch.setattr(_hc, "cfg", lambda: data)
    return data


def calibrate(cfg, frame=EMPTY, now=None):
    le.begin_calibration(cfg, frame, now=now)
    return le.confirm_calibration(cfg, frame, now=now, force=True)


# ---------------------------------------------------------------- producer

def test_calibrated_empty_plate_is_empty_idle(cfg):
    calibrate(cfg)
    verdict, detail = le.empty_verdict(EMPTY, cfg)
    assert verdict == "empty_idle"
    assert "reference" in detail


def test_part_present_is_not_empty(cfg):
    calibrate(cfg)
    assert le.empty_verdict(PART_BIG, cfg) is None


def test_small_light_part_is_not_empty(cfg):
    calibrate(cfg)
    assert le.empty_verdict(PART_SMALL_LIGHT, cfg) is None


@pytest.mark.parametrize("gain,offset", [(0.7, 0), (1.3, 0), (1.0, 18), (1.0, -12)])
def test_empty_plate_stays_empty_across_lighting_states(cfg, gain, offset):
    calibrate(cfg)
    result = le.empty_verdict(scene(gain=gain, offset=offset), cfg)
    assert result is not None and result[0] == "empty_idle"


def test_no_reference_produces_nothing(cfg):
    assert le.empty_verdict(EMPTY, cfg) is None
    state, detail = le.reference_status(cfg)
    assert state == "missing"
    assert "calibrate" in detail


def test_reference_nearly_empty_change_still_empty(cfg):
    """A faint residue smear under the difference threshold stays empty."""
    calibrate(cfg)
    smear = scene(part=(60, 60, 96, 84, (126, 128, 130)))
    result = le.empty_verdict(smear, cfg)
    assert result is not None and result[0] == "empty_idle"


def test_undecodable_frame_produces_nothing(cfg):
    calibrate(cfg)
    assert le.empty_verdict(b"not a jpeg", cfg) is None


def test_dark_frame_produces_nothing(cfg):
    calibrate(cfg)
    dark = jpeg(lambda d, size: d.rectangle([0, 0, *size], fill=(0, 0, 0)))
    assert le.empty_verdict(dark, cfg) is None


def test_reference_size_mismatch_produces_nothing(cfg):
    calibrate(cfg)
    other = jpeg(lambda d, size: d.rectangle([0, 0, *size], fill=(120, 122, 125)),
                 size=(160, 120))
    assert le.empty_verdict(other, cfg) is None


# ------------------------------------------------------------- staleness

def test_stale_reference_produces_nothing(cfg):
    old = time.time() - (le.REFERENCE_MAX_AGE_DAYS + 1) * 86400
    calibrate(cfg, now=old)
    assert le.empty_verdict(EMPTY, cfg) is None
    state, detail = le.reference_status(cfg)
    assert state == "stale"
    assert "recalibrate" in detail


def test_fresh_reference_is_ready(cfg):
    calibrate(cfg)
    state, detail = le.reference_status(cfg)
    assert state == "ready"
    assert "age" in detail


# ------------------------------------------------------------ calibration

def test_capture_is_not_effective_until_confirmed(cfg):
    le.begin_calibration(cfg, EMPTY)
    assert le.empty_verdict(EMPTY, cfg) is None
    state, _detail = le.reference_status(cfg)
    assert state == "pending"


def test_refused_capture_keeps_the_previous_reference(cfg):
    calibrate(cfg)
    le.begin_calibration(cfg, PART_BIG)
    assert le.confirm_calibration(cfg, PART_BIG) is False
    # The old (empty) reference still judges.
    assert le.empty_verdict(EMPTY, cfg)[0] == "empty_idle"
    assert le.empty_verdict(PART_BIG, cfg) is None


def test_recalibration_replaces_the_reference(cfg):
    calibrate(cfg, frame=scene(plate=(235, 233, 230)))
    le.begin_calibration(cfg, EMPTY)
    # A plain confirm is refused: the new capture contradicts the
    # trusted reference (that guard is what stops a part photo becoming
    # the reference). Recalibration is the deliberate operator act —
    # the plate or camera changed — so it confirms with force.
    assert le.confirm_calibration(cfg, EMPTY) is False
    assert le.confirm_calibration(cfg, EMPTY, force=True) is True
    assert le.empty_verdict(EMPTY, cfg)[0] == "empty_idle"


def test_corrupt_reference_file_reports_unavailable(cfg, tmp_path):
    path = le.reference_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"junk")
    (path.parent / "plate_reference.json").write_text("{broken", encoding="utf-8")
    assert le.empty_verdict(EMPTY, cfg) is None
    state, _detail = le.reference_status(cfg)
    assert state in ("unavailable", "missing")


# ------------------------------------------------------- setting and routes

def test_setting_off_produces_nothing(cfg):
    calibrate(cfg)
    cfg["vision"]["local_empty_enabled"] = False
    assert le.empty_verdict(EMPTY, cfg) is None
    assert le.empty_enabled(cfg) is False


def test_watch_source_endpoint_never_local(cfg):
    calibrate(cfg)
    cfg["vision"]["watch_source"] = "endpoint"
    assert le.idle_verdict(EMPTY, cfg) is None


def test_watch_source_local_is_eligible(cfg):
    calibrate(cfg)
    cfg["vision"]["watch_source"] = "local"
    assert le.idle_verdict(EMPTY, cfg)[0] == "empty_idle"


def test_visual_verdict_idle_consults_the_reference_when_no_endpoint_route(
        cfg, monkeypatch):
    """Under auto with no endpoint route configured, the idle prompt is
    answered by the reference producer — and only by it: an endpoint
    answer, when one exists, always wins (see the next test)."""
    calibrate(cfg)
    monkeypatch.setattr(vw, "_local_verdict", lambda image, streak=None: None)
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    verdict, detail = vw.visual_verdict(
        EMPTY, vw.IDLE_CHECK_PROMPT_TEXT, chat_url="", model="m",
        api_key=lambda: "", context="", idle=True)
    assert verdict == "empty_idle"
    assert "reference" in detail


def test_visual_verdict_print_watch_never_consults_the_reference(cfg, monkeypatch):
    """The in-print path (no idle flag) stays endpoint/detector-only:
    Stage 1 wires idle consumers only. The fixture pins
    watch_source "local", whose route is the detector seam
    (_detector_verdict); had the reference been consulted, the
    calibrated EMPTY frame would have answered "empty_idle"."""
    calibrate(cfg)
    monkeypatch.setattr(vw, "_detector_verdict",
                        lambda image, streak=None: ("uncertain", "no route"))
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    verdict, _detail = vw.visual_verdict(
        EMPTY, "watch prompt", chat_url="", model="m",
        api_key=lambda: "", context="")
    assert verdict == "uncertain"


def test_visual_verdict_idle_endpoint_answer_wins(cfg, monkeypatch):
    calibrate(cfg)
    # The shared fixture forces the local route. This case exercises auto
    # routing with a configured endpoint, not the explicit local override.
    monkeypatch.setattr(vw.local_vision, "watch_source", lambda: "auto")
    monkeypatch.setattr(vw, "_vision_answer",
                        lambda *a, **k: {"verdict": "ok", "reason": "part there"})
    monkeypatch.setattr(vw, "_local_verdict",
                        lambda image, streak=None: ("uncertain", "x"))
    verdict, _detail = vw.visual_verdict(
        PART_BIG, vw.IDLE_CHECK_PROMPT_TEXT, chat_url="http://vision.test",
        model="m", api_key=lambda: "k", context="", idle=True)
    assert verdict == "ok"


def test_visual_verdict_idle_part_present_falls_through_to_detector(
        cfg, monkeypatch):
    """A frame the reference cannot call empty is not forced empty: the
    normal route's answer (here the detector's) stands."""
    calibrate(cfg)
    monkeypatch.setattr(vw, "_local_verdict",
                        lambda image, streak=None: ("ok", "detector: no failure"))
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    verdict, _detail = vw.visual_verdict(
        PART_BIG, vw.IDLE_CHECK_PROMPT_TEXT, chat_url="", model="m",
        api_key=lambda: "", context="", idle=True)
    assert verdict == "ok"


def test_vision_ready_counts_a_calibrated_reference(cfg, monkeypatch):
    calibrate(cfg)
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    monkeypatch.setattr(vw.local_vision, "detector_ready", lambda cfg=None: False)
    assert vw.vision_ready(lambda: "") is True


def test_vision_ready_without_reference_or_routes_stays_false(cfg, monkeypatch):
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    monkeypatch.setattr(vw.local_vision, "detector_ready", lambda cfg=None: False)
    assert vw.vision_ready(lambda: "") is False


# ------------------------------------------------------- accuracy gates
#
# The scope's Stage 1 ship gate, run over a synthetic fixture set in the
# Stage 0 layout: sessions of ordered frames with a manifest carrying
# label/source/license/session/order per frame. Zero false-empty across
# >= 300 part-present idle frames; >= 95% true-empty across the empty
# frames in every lighting state. Synthetic frames are the honest limit
# of what this machine can measure — catduck's own camera captures are
# the scope's decisive data and remain his follow-up on #455.

def _write_fixture_set(root: Path):
    sessions = []
    manifest = {"format": "print-handy-vision-fixtures/1", "frames": []}

    def add(session, label, frames, note):
        sessions.append(session)
        for order, frame in enumerate(frames):
            name = f"{session}-{order:03d}.jpg"
            (root / name).write_bytes(frame)
            manifest["frames"].append({
                "file": name, "label": label, "source": "synthetic (PIL scene)",
                "license": "generated in-repo test fixture; no third-party data",
                "session": session, "order": order, "note": note})

    parts = [(110, 110, 210, 175, (200, 60, 40)),      # large dark part
             (150, 120, 176, 142, (238, 236, 230)),    # small light part
             (40, 150, 120, 205, (30, 90, 160)),       # medium part, low on plate
             (240, 60, 290, 120, (120, 122, 125)),     # plate-colored block
             (60, 60, 90, 90, (200, 200, 205))]        # small pale cube
    add("part-present", "part_present",
        [scene(part=parts[i % len(parts)],
               gain=(0.85, 1.0, 1.15)[i % 3],
               offset=(0, 8, -6)[i % 3]) for i in range(320)],
        "finished parts left on the plate, varied size/color/light")
    for label, kwargs in (("lights_on", {}), ("lights_dim", {"gain": 0.7}),
                          ("lights_bright", {"gain": 1.3}),
                          ("exposure_up", {"offset": 18}),
                          ("exposure_down", {"offset": -12})):
        add(f"empty-{label}", "empty",
            [scene(**kwargs) for _ in range(12)], f"empty plate, {label}")
    add("empty-residue", "empty",
        [scene(part=(60, 60, 96, 84, (126, 128, 130))) for _ in range(10)],
        "empty plate with a faint residue smear")
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1),
                                        encoding="utf-8")
    return manifest


def test_accuracy_gates_on_the_synthetic_fixture_set(cfg, tmp_path):
    root = tmp_path / "fixtures"
    root.mkdir()
    manifest = _write_fixture_set(root)
    calibrate(cfg)
    false_empty = 0
    part_frames = 0
    empty_total = 0
    empty_hit = 0
    by_state = {}
    for entry in manifest["frames"]:
        frame = (root / entry["file"]).read_bytes()
        verdict = le.empty_verdict(frame, cfg)
        if entry["label"] == "part_present":
            part_frames += 1
            if verdict is not None and verdict[0] == "empty_idle":
                false_empty += 1
        else:
            empty_total += 1
            hit = verdict is not None and verdict[0] == "empty_idle"
            empty_hit += hit
            state = entry["session"]
            total, hits = by_state.get(state, (0, 0))
            by_state[state] = (total + 1, hits + hit)
    assert part_frames >= 300
    assert false_empty == 0, f"{false_empty} false-empty verdicts on part-present frames"
    assert empty_total >= 30
    rate = empty_hit / empty_total
    assert rate >= 0.95, f"true-empty rate {rate:.3f} across lighting states"
    for state, (total, hits) in by_state.items():
        assert hits / total >= 0.95, f"lighting state {state}: {hits}/{total}"
