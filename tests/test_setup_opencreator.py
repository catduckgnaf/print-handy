"""Setup production questions with scripted Moonraker responses, never a printer."""
import io

import pytest

from print_handy import setup_probe as probe, setup_wizard as wizard


@pytest.mark.parametrize("suffix", ["creator5", "creator5_pro"])
@pytest.mark.parametrize("vendor,objects,target", [
    ("flashforge", ["creator5_toolchanger"], "flashforge_opencreator"),
    ("flashforge_opencreator", ["zmod_color"], "flashforge"),
])
def test_answers_choose_matching_firmware(monkeypatch, suffix, vendor, objects, target):
    monkeypatch.setattr(probe, "read_result", lambda *a: {"objects": objects})
    output = io.StringIO()
    w = wizard.Wizard({"brand": vendor, "model": vendor + "." + suffix,
                       "use_detected_firmware": "yes"}, output=output, yes=True, environ={})
    rec = wizard._ask_printer(w)
    assert rec["key"] == target + "." + suffix
    assert rec["printer"]["model"] == rec["key"]
    assert rec["model"].vendor == rec["vendor"]
    assert "Firmware mismatch" in output.getvalue()


@pytest.mark.parametrize("objects", [[], ["zmod_color", "creator5_toolchanger"], None, "zmod_color"])
def test_ambiguous_identity_never_switches(objects):
    assert probe.matching_creator("flashforge_opencreator.creator5", objects) is None


def test_unattended_never_silently_changes_firmware(monkeypatch):
    monkeypatch.setattr(probe, "read_result", lambda *a: {"objects": ["creator5_toolchanger"]})
    w = wizard.Wizard({"brand": "flashforge", "model": "flashforge.creator5"},
                      output=io.StringIO(), yes=True, environ={})
    assert wizard._ask_printer(w)["key"] == "flashforge.creator5"


@pytest.mark.parametrize("base,path,expected", [
    ("http://printer.local:7125", "/webcam/?action=snapshot", "http://printer.local/webcam/?action=snapshot"),
    ("https://printer.local:8443/moonraker", "camera/snapshot", "https://printer.local:8443/camera/snapshot"),
    ("http://[::1]:7125", "/snapshot", "http://[::1]/snapshot"),
    ("http://printer.local:7125", "https://camera.local/snap", "https://camera.local/snap"),
])
def test_camera_question_uses_moonraker_snapshot(monkeypatch, base, path, expected):
    calls = []
    def read(url, endpoint):
        calls.append((url, endpoint))
        return {"webcams": [{"snapshot_url": "/disabled", "enabled": False},
                            {"snapshot_url": path}]}
    monkeypatch.setattr(probe, "read_result", read)
    rec = {"services": {"moonraker_url": base}, "notes": [], "connection": "moonraker"}
    wizard._ask_camera(wizard.Wizard({"camera": "yes"}, yes=True, output=io.StringIO()), rec)
    assert rec["services"]["camera_snapshot_url"] == expected
    assert calls == [(base, "/server/webcams/list")]


def test_missing_snapshot_is_not_guessed(monkeypatch):
    monkeypatch.setattr(probe, "read_result", lambda *a: {"webcams": [{"stream_url": "/stream"}]})
    rec = {"services": {"moonraker_url": "http://printer.local:7125"}, "notes": [], "connection": "moonraker"}
    wizard._ask_camera(wizard.Wizard({"camera": "yes"}, yes=True, output=io.StringIO()), rec)
    assert "camera_snapshot_url" not in rec["services"]
    assert "No enabled" in rec["notes"][0]


def test_discovery_network_failure_is_unknown(monkeypatch):
    def fail(*a, **kw):
        raise OSError("unreachable")
    monkeypatch.setattr(probe, "urlopen", fail)
    assert probe.read_result("http://printer.local:7125", "/printer/objects/list") == {}
