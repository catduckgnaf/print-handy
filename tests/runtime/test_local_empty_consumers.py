"""Stage 1 consumer wiring (#455 item 4): the Moonraker listener's idle
plate checks and the family driver's idle check consume the local
reference verdict when the endpoint routes cannot answer — and only
there. The in-print empty recheck path is untouched: it never consults
the reference producer.

Offline: verdicts, snapshots and sends are faked; the producer itself is
exercised for real against a calibrated synthetic reference.
"""
import asyncio
import io
import time

from PIL import Image, ImageDraw

from print_handy.runtime import family_control as fc
from print_handy.runtime import light_control
from print_handy.runtime import local_empty as le
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import visual_watch as vw


def _jpeg(part=None):
    image = Image.new("RGB", (320, 240), (16, 16, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 30, 300, 225], fill=(120, 122, 125))
    draw.rectangle([20, 30, 300, 44], fill=(60, 60, 64))
    draw.rectangle([270, 44, 292, 96], fill=(40, 40, 44))
    if part:
        draw.rectangle(part, fill=(200, 60, 40))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


EMPTY = _jpeg()
PART = _jpeg(part=(110, 110, 210, 175))


def _calibrated_cfg(tmp_path):
    cfg = {"paths": {"state_home": str(tmp_path)},
           "vision": {"local_empty_enabled": True, "watch_source": "local"}}
    le.begin_calibration(cfg, EMPTY)
    assert le.confirm_calibration(cfg, EMPTY, force=True)
    return cfg


# ------------------------------------------------------- Moonraker listener

def _listener(monkeypatch, tmp_path, *, frame, endpoint_verdict):
    """The listener stack with the REAL visual_watch verdict router.

    The reference producer answers inside visual_watch.visual_verdict
    (idle + no endpoint route), so the fake sits one layer down: the
    endpoint answer is stubbed at _vision_answer and the local
    detector at _local_verdict; the router itself runs for real.
    """
    listener = mel.Listener()
    listener.state = "standby"
    policy = light_control.AutoLightPolicy(10, check_interval_s=0)
    policy.print_finished(time.time() - 3600)
    policy.note_verdict("empty_idle", time.time() - 3600)
    listener._auto_light = policy
    listener.light_target = "led chamber_led"
    listener.light_target_resolved = True
    sent = []
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(mel, "_send_light_script", lambda script: sent.append(script))
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: frame)
    monkeypatch.setattr(mel._visual_watch, "frame_dark", lambda image: False)
    monkeypatch.setattr(mel._config, "cfg", lambda: _calibrated_cfg(tmp_path))
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)

    def endpoint(image, prompt, **kwargs):
        result = endpoint_verdict(image, prompt, idle=True)
        if result[0] == "uncertain":
            raise RuntimeError("no endpoint route configured")
        return {"verdict": result[0], "reason": result[1]}

    monkeypatch.setattr(vw, "_vision_answer", endpoint)
    monkeypatch.setattr(vw, "_local_verdict",
                        lambda image, streak=None: ("uncertain", "detector cannot certify empty"))
    monkeypatch.setattr(vw, "_chat_route_configured", lambda key: False)
    return listener, policy, sent


def test_idle_tick_uses_the_reference_when_no_endpoint_route_answers(
        monkeypatch, tmp_path):
    """No endpoint route (the detector answers 'uncertain' today): the
    calibrated reference certifies the empty plate and the light goes off."""
    listener, _policy, sent = _listener(
        monkeypatch, tmp_path, frame=EMPTY,
        endpoint_verdict=lambda image, prompt, idle=False: ("uncertain", "no route"))
    asyncio.run(listener.light_idle_tick())
    _on, off = light_control.light_scripts("led chamber_led")
    assert sent == [off]


def test_idle_tick_part_present_keeps_the_light_on(monkeypatch, tmp_path):
    listener, policy, sent = _listener(
        monkeypatch, tmp_path, frame=PART,
        endpoint_verdict=lambda image, prompt, idle=False: ("uncertain", "no route"))
    asyncio.run(listener.light_idle_tick())
    assert sent == []
    assert policy.empty_since is None  # the streak reset: not empty


def test_idle_tick_endpoint_empty_answer_is_unchanged(monkeypatch, tmp_path):
    listener, _policy, sent = _listener(
        monkeypatch, tmp_path, frame=EMPTY,
        endpoint_verdict=lambda image, prompt, idle=False: ("empty_idle", "endpoint says clear"))
    asyncio.run(listener.light_idle_tick())
    _on, off = light_control.light_scripts("led chamber_led")
    assert sent == [off]


def test_next_plate_offer_uses_the_reference_verdict(monkeypatch, tmp_path):
    listener = mel.Listener()
    listener.state = "standby"
    observed = []
    monkeypatch.setattr(mel, "_vision_ready", lambda: True)
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "_camera_snapshot_bytes", lambda: EMPTY)
    monkeypatch.setattr(mel._visual_watch, "frame_dark", lambda image: False)
    monkeypatch.setattr(mel._config, "cfg", lambda: _calibrated_cfg(tmp_path))
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw, "_vision_answer",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no route")))
    monkeypatch.setattr(vw, "_local_verdict",
                        lambda image, streak=None: ("uncertain", "detector"))
    monkeypatch.setattr(vw, "_chat_route_configured", lambda key: False)
    from print_handy.runtime import next_plate_offer
    monkeypatch.setattr(next_plate_offer, "due", lambda: True)
    monkeypatch.setattr(next_plate_offer, "observe",
                        lambda verdict: observed.append(verdict) or {"action": "none"})
    asyncio.run(listener.offer_next_plate_if_clear())
    assert observed == ["empty_idle"]


def test_in_print_visual_check_never_uses_the_reference(monkeypatch, tmp_path):
    """The in-print path stays endpoint-only in Stage 1: visual_check's
    verdict call carries no idle flag, so a calibrated reference cannot
    feed the empty recheck."""
    called = []
    monkeypatch.setattr(le, "idle_verdict",
                        lambda *a, **k: called.append(a) or None)
    monkeypatch.setattr(vw, "_vision_answer",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(vw, "_local_verdict",
                        lambda image, streak=None: ("uncertain", "detector"))
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.ai_routes, "overrides_route", lambda tier: False)
    monkeypatch.setattr(vw._config, "service_url", lambda name: "")
    verdict, _ = vw.visual_verdict(EMPTY, "watch", chat_url="", model="m",
                                   api_key=lambda: "", context="")
    assert verdict == "uncertain"
    assert called == []


# ------------------------------------------------------------ family driver

class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_family_idle_check_uses_the_reference_when_vision_is_not_ready(
        monkeypatch, tmp_path):
    cfg = _calibrated_cfg(tmp_path)
    monkeypatch.setattr(le._config, "cfg", lambda: cfg)
    sends = []
    clock = Clock()
    driver = fc.FamilyAutoLight(
        "bambulab_lan",
        send=lambda on: sends.append(on),
        snapshot=lambda: EMPTY,
        verdict=lambda image, prompt: ("uncertain", "no route"),
        available=lambda: True,
        vision_ready=lambda: False,
        policy=light_control.AutoLightPolicy(30, check_interval_s=300),
        clock=clock)
    driver.print_finished()
    clock.now += 301
    driver.idle_tick("complete")
    # The reference verdict fed the policy; the streak needs the full
    # window, so nothing switches yet — but a second sustained window does.
    clock.now += 301 + 30 * 60
    driver.idle_tick("complete")
    assert sends == [False]


def test_family_idle_check_without_reference_stays_blocked(monkeypatch, tmp_path):
    cfg = {"paths": {"state_home": str(tmp_path)},
           "vision": {"local_empty_enabled": True, "watch_source": "local"}}
    monkeypatch.setattr(le._config, "cfg", lambda: cfg)
    sends = []
    clock = Clock()
    driver = fc.FamilyAutoLight(
        "bambulab_lan",
        send=lambda on: sends.append(on),
        snapshot=lambda: EMPTY,
        verdict=lambda image, prompt: ("uncertain", "no route"),
        available=lambda: True,
        vision_ready=lambda: False,
        policy=light_control.AutoLightPolicy(30, check_interval_s=300),
        clock=clock)
    driver.print_finished()
    clock.now += 301
    driver.idle_tick("complete")
    clock.now += 301 + 30 * 60
    driver.idle_tick("complete")
    assert sends == []
