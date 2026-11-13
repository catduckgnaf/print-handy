"""Every printer family's print posts reach web chat (printer-scope rule).

The Web UI parity guard (tests/bot/test_command_webui_parity.py) proves
the listener's post path delivers every lifecycle kind to web chat. Here
each listener family's own production sender is driven, with the
existing fakes, so a family that ever stops posting through that path
fails by name:

* Moonraker/Klipper, the AD5X with ZMod included (the roster here is two
  AD5X printers): the listener's own ``post`` and ``post_alert``.
* Bambu, PrusaLink, OctoPrint, SDCP (Elegoo) and stock Flashforge: each
  launched-print monitor's ``_default_post``; PrusaLink end to end
  through a real monitor run; Bambu's camera check with its buttons.
* The family event listener (external prints on every non-Moonraker
  family): ``family_control.post_family_event`` and its pause alert,
  whose web chat buttons route to that family's own control.

No socket, no printer, no chat: the REST senders are recorders and web
chat reads the feed file in a temporary state folder.
"""
import pytest

from print_handy.runtime import bambu_watch, elegoo_watch, family_control
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import octo_watch, print_alert_buttons as pab, prusa_watch, stock_watch

FAMILY_SENDERS = {
    "bambu": bambu_watch._default_post,
    "prusalink": prusa_watch._default_post,
    "octoprint": octo_watch._default_post,
    "sdcp": elegoo_watch._default_post,
    "flashforge": stock_watch._default_post,
    "moonraker": mel.post,
}


def _facts(rig, verb):
    shown = rig.wait_web(lambda body: body.startswith(f"{verb}: Widget"))
    assert "Printer: Workshop AD5X" in shown
    assert all("<@" not in body for body in rig.web_texts())
    return shown


@pytest.mark.parametrize("family", sorted(FAMILY_SENDERS))
@pytest.mark.parametrize("kind,verb", [("complete", "Print complete"),
                                       ("cancelled", "Print cancelled"),
                                       ("error", "Print stopped with an error")])
def test_each_familys_terminal_post_reaches_web_chat(print_event_rig, family, kind, verb):
    rig = print_event_rig
    send = FAMILY_SENDERS[family]
    assert send(rig.render(kind), ping_kind=kind, ping_job_key="plate.gcode")
    assert rig.sent[-1][1].startswith(f"{verb}: Widget")
    _facts(rig, verb)


def test_prusalink_monitor_run_reaches_web_chat(print_event_rig):
    """A real PrusaLink monitor run, its production sender, no camera."""
    rig = print_event_rig
    states = iter([{"state": "printing", "filename": "plate.gcode", "progress": 0.5},
                   {"state": "complete", "filename": "plate.gcode", "progress": 1.0}])
    clock = [0.0]
    monitor = prusa_watch.PrusaPrintMonitor(
        "plate.gcode", status_fn=lambda: next(states), camera_source_fn=lambda: ("", None),
        post=prusa_watch._default_post, clock=lambda: clock[0], wall=lambda: 1.0,
        sleep=lambda s: clock.__setitem__(0, clock[0] + s), poll_s=30.0)
    assert monitor.run() == "complete"
    _facts(rig, "Print complete")


@pytest.mark.parametrize("platform", ["discord", "telegram", "matrix"])
def test_family_event_listener_posts_reach_web_chat(print_event_rig, platform):
    rig = print_event_rig
    rig.install(platform)
    assert family_control.post_family_event("started", "plate.gcode")
    _facts(rig, "Print started")


@pytest.mark.parametrize("connection", ["bambu", "prusalink", "octoprint", "sdcp", "flashforge"])
def test_family_pause_alert_buttons_route_to_the_family(print_event_rig, monkeypatch, connection):
    rig = print_event_rig
    clicks = []
    monkeypatch.setattr(pab, "handle_click",
                        lambda action, alert: clicks.append((action, alert)) or "Resume sent.")
    family_control.post_pause_alert("plate.gcode", connection, printer_id=rig.WORKSHOP)
    prompt = rig.wait_prompt("Print paused: Widget")
    assert prompt["choices"] == ["Resume print", "Cancel print"]
    rig.view.answer(prompt["clarify_id"], 0)
    rig.wait_web(lambda body: body == "Resume sent.")
    (action, alert), = clicks
    assert action == "resume"
    assert alert["connection"] == connection and alert["printer_id"] == rig.WORKSHOP


def test_moonraker_pause_alert_reaches_web_chat(print_event_rig):
    rig = print_event_rig
    assert mel.post_alert(rig.render("paused"), "paused", "plate.gcode")
    prompt = rig.wait_prompt("Print paused: Widget")
    assert "Printer: Workshop AD5X" in prompt["question"]


def test_bambu_camera_check_reaches_web_chat_with_its_buttons(print_event_rig):
    rig = print_event_rig
    bambu_watch.deliver_visual_text_alert("plate.gcode", "The camera could not be read.",
                                          identity="bambu-job-1", printer_id=rig.WORKSHOP)
    prompt = rig.wait_prompt("Print camera check")
    assert prompt["choices"] == ["Pause", "It's fine", "Stop asking for this print"]
    stored = [row for row in pab._load().values()
              if isinstance(row, dict) and row.get("kind") == "visual"]
    assert any(row.get("identity") == "bambu-job-1" and row.get("printer_id") == rig.WORKSHOP
               for row in stored)
