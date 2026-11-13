"""Bot check's auto light row (#516): every state names itself and the remedy.

States pinned, in the roast row's style: off by switch / on with the
target and minutes / on but no vision endpoint / on but no light target
named (bot check is offline — auto-discovery is the listener's job, so
an unnamed target is reported as unresolved-with-remedy, not verified).
"""
import pytest
from print_handy.bot import __main__ as bot_main


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")


def rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_auto_light_row_off_by_default(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=0, light_object="")
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("off (vision.auto_light_off_minutes is 0")


def test_auto_light_row_on_with_target_and_minutes(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led")
    cfg["services"]["vision_chat_url"] = "http://vision.invalid/v1"
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert "led chamber_led" in detail and "30 minutes" in detail


def test_auto_light_row_on_but_no_vision_endpoint(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led")
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but no vision endpoint is available")


def test_auto_light_row_print_vision_url_counts_as_endpoint(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led")
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.setenv("PRINT_VISION_URL", "http://detector.invalid/judge")
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert "led chamber_led" in detail


def test_auto_light_row_on_but_no_light_target(cfg, monkeypatch):
    cfg["vision"].update(auto_light_off_minutes=30, light_object="")
    cfg["services"]["vision_chat_url"] = "http://vision.invalid/v1"
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but no light target is named")
    assert "vision.light_object" in detail


# ---------------------------------------------- family rows (#516 follow-up)

def test_auto_light_row_bambu_chamber_light(cfg, monkeypatch):
    cfg["printer"]["model"] = "bambulab.a1"
    cfg["vision"].update(auto_light_off_minutes=30, light_object="")
    cfg["services"]["vision_chat_url"] = "http://vision.invalid/v1"
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert "Bambu chamber light" in detail and "30 minutes" in detail
    assert "vision.light_object applies to Moonraker printers only" in detail


def test_auto_light_row_bambu_still_needs_a_vision_endpoint(cfg, monkeypatch):
    cfg["printer"]["model"] = "bambulab.p1s"
    cfg["vision"].update(auto_light_off_minutes=30, light_object="")
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but no vision endpoint is available")


def test_auto_light_row_sdcp_names_the_camera_gap(cfg, monkeypatch):
    cfg["printer"]["model"] = "elegoo_sdcp.centauricarbon"
    cfg["vision"].update(auto_light_off_minutes=30, light_object="")
    cfg["services"]["vision_chat_url"] = "http://vision.invalid/v1"
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert "SDCP chamber light" in detail and "Cmd 403" in detail
    assert "no SDCP camera source is verified" in detail


def test_auto_light_row_family_without_a_light_is_unavailable(cfg, monkeypatch):
    cfg["printer"]["model"] = "octoprint.ender3"
    cfg["vision"].update(auto_light_off_minutes=30, light_object="")
    cfg["services"]["vision_chat_url"] = "http://vision.invalid/v1"
    ok, detail = rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert detail.startswith("on, but unavailable for this printer")
    assert "'octoprint'" in detail
