"""Offline tests for the /print-plate-calibrate handler (#455 item 4, Stage 1).

The bot-command calibration surface: capture shows the frame back with
a Make this my empty plate / Retake confirm before anything is stored;
retaking keeps the previous reference; status reports the reference
age. No Discord, no network: the dispatch runs against a fake adapter
and gateway in the /print-live test style, with the camera snapshot and
the prompt answer faked.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S

import pytest
from PIL import Image, ImageDraw
import io

from print_handy.hermes import _config, local_empty

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_plate", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def _jpeg(color=(120, 122, 125)):
    image = Image.new("RGB", (320, 240), (16, 16, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 30, 300, 225], fill=color)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


EMPTY = _jpeg()
PART = _jpeg((200, 60, 40))


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarifies = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, chat_id=None, question=None, choices=None,
                           clarify_id=None, session_key=None, metadata=None):
        self.clarifies.append((question, choices))
        return S(success=True)


def _event(text, platform="discord"):
    source = S(platform=S(value=platform), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="42", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "sess")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = {"paths": {"hermes_home": str(tmp_path)},
           "vision": {"local_empty_enabled": True, "watch_source": "local"}}
    monkeypatch.setattr(local_empty, "capture_frame",
                        lambda c=None: EMPTY)
    previous = _config.cfg()
    _config.set_override(cfg)
    yield cfg
    _config.set_override(previous)


def _run(text, adapter=None, *, confirm_answer="Make this my empty plate"):
    adapter = adapter or FakeAdapter()
    cm = S(register=lambda *a, **k: None,
           wait_for_response=lambda cid, timeout: confirm_answer,
           resolve_gateway_clarify=lambda cid, value: None,
           CANCELLED="cancelled")
    monkeypatch_cm(cm)
    out = asyncio.run(plugin.plate_calibrate_dispatch(
        _event(text), _gateway(adapter)))
    return out, adapter


def monkeypatch_cm(cm):
    # _prompts() returns prompts.gateway() — the module itself when the own
    # registry is active, whose register/wait_for_response are the names
    # the plugin calls. Patch the module's functions for the fake.
    import print_handy.core.prompts as prompts_pkg
    prompts_pkg.register = cm.register
    prompts_pkg.wait_for_response = cm.wait_for_response
    prompts_pkg.resolve_gateway_clarify = cm.resolve_gateway_clarify


def test_status_without_a_reference_names_the_remedy(env):
    out, adapter = _run("/print-plate-calibrate status")
    assert out["reason"] == "plate calibration status shown"
    assert "no reference" in adapter.sent[-1]
    assert "/print-plate-calibrate capture" in adapter.sent[-1]


def test_capture_shows_the_frame_and_stores_only_on_confirm(env):
    out, adapter = _run("/print-plate-calibrate capture")
    assert out["reason"] == "plate reference captured"
    shown = [c for c in adapter.sent if c and "MEDIA:" in c]
    assert shown, "the captured frame must be shown back before it takes effect"
    question, choices = adapter.clarifies[-1]
    assert "empty plate" in question
    assert "Make this my empty plate" in choices and "Retake" in choices
    state, _detail = local_empty.reference_status(env)
    assert state == "ready"
    verdict, _ = local_empty.empty_verdict(EMPTY, env)
    assert verdict == "empty_idle"


def test_retake_keeps_the_previous_reference(env):
    local_empty.begin_calibration(env, EMPTY)
    assert local_empty.confirm_calibration(env, EMPTY, force=True)
    _run("/print-plate-calibrate capture", confirm_answer="Retake")
    verdict, _ = local_empty.empty_verdict(EMPTY, env)
    assert verdict == "empty_idle"


def test_capture_refuses_when_the_feature_is_off(env):
    env["vision"]["local_empty_enabled"] = False
    out, adapter = _run("/print-plate-calibrate capture")
    assert out["reason"] == "plate calibration not enabled"
    assert "vision.local_empty_enabled" in adapter.sent[-1]
    state, _ = local_empty.reference_status(env)
    assert state == "off" and not local_empty.reference_path(env).exists()


def test_unknown_verb_shows_usage(env):
    out, adapter = _run("/print-plate-calibrate dance")
    assert out["reason"] == "plate calibration usage shown"
    assert "capture" in adapter.sent[-1] and "status" in adapter.sent[-1]


def test_unauthorized_source_falls_through(env):
    adapter = FakeAdapter()
    gateway = S(_is_user_authorized_for_source=lambda s: False,
                _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: "sess")
    out = asyncio.run(plugin.plate_calibrate_dispatch(
        _event("/print-plate-calibrate status"), gateway))
    assert out is None
    assert adapter.sent == []
