"""Bot check's local detector row (#455) and the auto light row's
local-only state: every state names itself and the remedy, in the roast
row's style. Offline — the detector is pointed at the tiny test fixture,
and bot check never downloads or infers."""
import pytest
from pathlib import Path

from print_handy.bot import __main__ as bot_main
from print_handy.runtime import local_vision as lv


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")

FIXTURE = Path(__file__).parent.parent / "hermes" / "fixtures" / "tiny_failure_detector.onnx"


def rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_local_detector_row_off_by_default(cfg, monkeypatch):
    ok, detail = rows(cfg, monkeypatch)["local detector"]
    assert ok is True
    assert detail.startswith("off (vision.local_detector_enabled is false")


def test_local_detector_row_on_and_ready(cfg, monkeypatch):
    cfg["vision"].update(local_detector_enabled=True, local_model_path=str(FIXTURE))
    ok, detail = rows(cfg, monkeypatch)["local detector"]
    assert ok is True
    assert detail.startswith("on (")
    assert "present" in detail and "no vision endpoint is configured" in detail
    assert "execution provider: CPUExecutionProvider" in detail


def test_local_detector_row_on_but_runtime_missing(cfg, monkeypatch):
    cfg["vision"].update(local_detector_enabled=True, local_model_path=str(FIXTURE))
    monkeypatch.setattr(lv, "_ort", None)
    monkeypatch.setattr(lv, "_ort_error", RuntimeError("no onnxruntime here"))
    ok, detail = rows(cfg, monkeypatch)["local detector"]
    assert ok is False
    assert detail.startswith("on, but unavailable:") and "onnxruntime" in detail


def test_auto_light_row_names_the_local_detector_limit(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led",
                         local_detector_enabled=True, local_model_path=str(FIXTURE))
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but the local failure detector cannot certify an empty plate")
    assert "vision endpoint" in detail


def test_auto_light_row_without_local_detector_keeps_the_endpoint_text(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led",
                         local_detector_enabled=False)
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but no vision endpoint is available")
