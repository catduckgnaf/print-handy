"""Offline tests for Phase 3a (paused alert) and 3b (/print-stop)."""
import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

HERE = Path(__file__).resolve().parent
from print_handy.runtime import print_stop
from print_handy.runtime import moonraker_event_listener as mel

SPEC = importlib.util.spec_from_file_location("pf_stop", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def st(state, filename="a.gcode", **kw):
    return {"state": state, "filename": filename, "layer": kw.get("layer"), "total_layer": kw.get("total"),
            "progress": kw.get("progress"), "print_duration": None}


def test_cancel_refuses_when_idle():
    with mock.patch.object(print_stop, "status", return_value=st("standby", "")), \
            mock.patch.object(print_stop, "_call") as call:
        sent, text = print_stop.cancel("a.gcode")
    assert not sent and "Nothing sent" in text
    call.assert_not_called()


def test_cancel_refuses_when_file_changed():
    with mock.patch.object(print_stop, "status", return_value=st("printing", "b.gcode")), \
            mock.patch.object(print_stop, "_call") as call:
        sent, text = print_stop.cancel("a.gcode")
    assert not sent and "different print" in text
    call.assert_not_called()


def heaters(nozzle=0.0, bed=0.0):
    """Moonraker's reply to the heater-target read cancel makes after the stop (#1147)."""
    return {"result": {"status": {"extruder": {"target": nozzle}, "heater_bed": {"target": bed}}}}


HEATER_QUERY = "/printer/objects/query?extruder=target&heater_bed=target"


def moonraker_calls(*heater_replies):
    """A fake _call: the cancel POST answers {}, each heater read the next reply (the last repeats)."""
    replies = list(heater_replies) or [heaters()]
    calls = []

    def call(path, method="GET", timeout=10, **kw):
        calls.append((path, method))
        if path == HEATER_QUERY:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return {}
    return call, calls


def cancel_posts(calls):
    return [c for c in calls if c == ("/printer/print/cancel", "POST")]


def test_cancel_sends_once_and_reports_real_state():
    seq = [st("printing"), st("printing"), st("cancelled")]
    fake, calls = moonraker_calls(heaters())
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_call", side_effect=fake), mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and text == "Stopped. Printer now reports cancelled."
    assert cancel_posts(calls) == [("/printer/print/cancel", "POST")]
    assert calls[-1] == (HEATER_QUERY, "GET")  # read back, never written


def test_1147_cancel_reports_a_heater_left_on_and_sends_nothing_to_change_it():
    """Item 1: a printer that keeps a heater target after the stop is reported, by name and value."""
    seq = [st("printing"), st("cancelled")]
    fake, calls = moonraker_calls(heaters(nozzle=215.0, bed=0.0))
    clock = iter(range(0, 1000))
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_call", side_effect=fake), \
            mock.patch.object(print_stop.time, "sleep"), \
            mock.patch.object(print_stop.time, "monotonic", side_effect=lambda: float(next(clock))):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and text.startswith("Stopped. Printer now reports cancelled. But the nozzle heater is still set to 215 C")
    assert "did not send anything" in text
    assert cancel_posts(calls) == [("/printer/print/cancel", "POST")]  # one cancel, no heater write
    assert all(method == "GET" for path, method in calls if path != "/printer/print/cancel")


def test_1147_cancel_waits_for_the_cancel_macro_to_turn_the_heaters_off():
    seq = [st("printing"), st("cancelled")]
    fake, calls = moonraker_calls(heaters(nozzle=215.0, bed=60.0), heaters(nozzle=0.0, bed=0.0))
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_call", side_effect=fake), mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and text == "Stopped. Printer now reports cancelled."
    assert calls.count((HEATER_QUERY, "GET")) == 2


def test_1147_unreadable_heater_targets_are_not_reported_as_off():
    seq = [st("printing"), st("cancelled")]
    fake, _calls = moonraker_calls(OSError("connection reset"))
    clock = iter(range(0, 1000))
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_call", side_effect=fake), \
            mock.patch.object(print_stop.time, "sleep"), \
            mock.patch.object(print_stop.time, "monotonic", side_effect=lambda: float(next(clock))):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and "could not be read back" in text and "cannot say the heaters are off" in text


def test_1147_ad5x_zmod_cancel_sequence_is_unchanged():
    """AD5X/ZMod pin: one /printer/print/cancel after a state re-read, then read-only polls.

    The heater read-back adds GETs only; the write sequence the AD5X sees is exactly the
    one cancel it always got, with the bot-cancel marker set first.
    """
    from print_handy.runtime import klipper_report
    seq = [st("printing", "plate_1.gcode.3mf"), st("printing", "plate_1.gcode.3mf"), st("cancelled", "plate_1.gcode.3mf")]
    fake, calls = moonraker_calls(heaters())
    order = []
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(klipper_report, "mark_bot_cancel", side_effect=lambda: order.append("marker")), \
            mock.patch.object(print_stop, "_call", side_effect=lambda *a, **k: order.append(a[0]) or fake(*a, **k)), \
            mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.cancel("plate_1.gcode.3mf", wait_s=5)
    assert sent and text == "Stopped. Printer now reports cancelled."
    assert order[:2] == ["marker", "/printer/print/cancel"]
    assert [c for c in calls if c[1] == "POST"] == [("/printer/print/cancel", "POST")]


def test_cancel_never_retries_when_state_sticks():
    with mock.patch.object(print_stop, "status", return_value=st("paused")), \
            mock.patch.object(print_stop, "_call") as call, mock.patch.object(print_stop.time, "sleep"), \
            mock.patch.object(print_stop.time, "monotonic", side_effect=[0, 1, 99]):
        sent, text = print_stop.cancel("a.gcode", wait_s=5)
    assert sent and "Not retrying" in text
    assert call.call_count == 1


def test_where_text():
    assert print_stop.where_text(st("paused", layer=12, total=80, progress=0.25)) == "layer 12/80, 25% done"
    assert print_stop.where_text(st("paused")) == ""


def test_listener_cancel_posts_one_message_without_waiting_for_diagnostics(monkeypatch):
    # One post per cancel: post_cancel_report leads with the cancelled summary
    # (and falls back to it alone), so the listener posts no separate ack that
    # read as a duplicate. The slow report runs in the background: the
    # transition finishes while it is still collecting.
    lis = mel.Listener()
    lis.state, lis.filename = "printing", "a.gcode"
    posted = []
    diagnostic_started = threading.Event()
    release_diagnostic = threading.Event()

    def slow_report(filename, reason=None):
        diagnostic_started.set()
        assert release_diagnostic.wait(timeout=2)
        return True

    with mock.patch.object(mel, "post") as post,             mock.patch.object(mel, "run_announcer"),             mock.patch.object(mel, "maintenance_cancel_reminder", new=lambda: asyncio.sleep(0)),             mock.patch.object(mel, "post_cancel_report", side_effect=slow_report) as report,             mock.patch.object(lis, "after_print", new=lambda state: asyncio.sleep(0)):
        post.side_effect = lambda message: posted.append(message) or True

        async def exercise():
            try:
                await asyncio.wait_for(lis.on_status({"state": "cancelled"}, "event"), timeout=1.5)
                assert diagnostic_started.wait(0.5)
                assert not any("Print cancelled" in message for message in posted)
            finally:
                release_diagnostic.set()
                await asyncio.gather(*list(lis.background_tasks))

        asyncio.run(exercise())
    report.assert_called_once_with("a.gcode", mock.ANY)


def test_listener_cancel_does_not_auto_restart_firmware(monkeypatch):
    lis = mel.Listener()
    lis.state, lis.filename = "printing", "a.gcode"
    with mock.patch.object(mel, "post"), mock.patch.object(mel, "run_announcer"), \
            mock.patch.object(mel, "maintenance_cancel_reminder", new=lambda: asyncio.sleep(0)), \
            mock.patch.object(mel, "post_cancel_report"), \
            mock.patch.object(lis, "start_screen_cancel_reboot") as reboot, \
            mock.patch.object(lis, "after_print", new=lambda state: asyncio.sleep(0)):
        asyncio.run(lis.on_status({"state": "cancelled"}, "event"))
    reboot.assert_not_called()


def test_listener_posts_once_on_pause_only():
    lis = mel.Listener()
    lis.state, lis.filename = "printing", "a.gcode"
    with mock.patch.object(mel, "post") as post, mock.patch.object(mel, "run_announcer"), \
            mock.patch.object(mel, "paused_message", return_value="Print paused: X") as pm, \
            mock.patch.object(lis, "classify_runout", return_value=(False, None)):
        asyncio.run(lis.on_status({"state": "paused"}, "event"))
        asyncio.run(lis.on_status({"state": "printing"}, "event"))
    pm.assert_called_once_with("a.gcode")
    post.assert_called_once_with("Print paused: X")


def test_listener_posts_tracking_prompt_on_runout_pause():
    lis = mel.Listener()
    lis.state, lis.filename = "printing", "a.gcode"
    with mock.patch.object(mel, "post") as post, mock.patch.object(mel, "run_announcer"), \
            mock.patch.object(mel, "paused_message", return_value="Print paused: X"), \
            mock.patch.object(lis, "classify_runout", return_value=(True, 2)), \
            mock.patch.object(lis, "open_question", return_value={"slot": 2}) as oq:
        asyncio.run(lis.on_status({"state": "paused"}, "event"))
    oq.assert_called_once_with(2, "runout")
    post.assert_called_once_with("Print paused: X")


def test_paused_message_text():
    with mock.patch.object(print_stop, "status", return_value=st("paused", layer=3, total=9, progress=0.3)):
        msg = mel.paused_message("unknown_file.gcode")
    assert msg.startswith("Print paused:") and "layer 3/9" in msg and "/print-stop" in msg


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kw):
        return S(success=True)


def _event(text="/print-stop"):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False, profile_route_rejected=False,
            user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def test_stop_command_idle_sends_nothing():
    ad = FakeAdapter()
    with mock.patch.object(print_stop, "status", return_value=st("standby", "")), \
            mock.patch.object(print_stop, "cancel") as cancel:
        out = asyncio.run(plugin.print_stop_dispatch(_event(), _gateway(ad)))
    assert out["action"] == "skip" and "Nothing to stop" in ad.sent[-1]
    cancel.assert_not_called()


def test_stop_command_confirmed_cancels_confirmed_file():
    ad = FakeAdapter()
    from print_handy.core import prompts as cm
    with mock.patch.object(print_stop, "status", return_value=st("printing", "a.gcode")), \
            mock.patch.object(print_stop, "cancel", return_value=(True, "Stopped. Printer now reports cancelled.")) as cancel, \
            mock.patch.object(cm, "wait_for_response", return_value=print_stop.STOP_LABEL):
        asyncio.run(plugin.print_stop_dispatch(_event(), _gateway(ad)))
    cancel.assert_called_once_with("a.gcode")
    assert ad.sent[-1] == "Stopped. Printer now reports cancelled."


def test_stop_command_declined_sends_nothing():
    ad = FakeAdapter()
    from print_handy.core import prompts as cm
    with mock.patch.object(print_stop, "status", return_value=st("printing", "a.gcode")), \
            mock.patch.object(print_stop, "cancel") as cancel, \
            mock.patch.object(cm, "wait_for_response", return_value=print_stop.KEEP_LABEL):
        asyncio.run(plugin.print_stop_dispatch(_event(), _gateway(ad)))
    cancel.assert_not_called()
    assert ad.sent[-1] == "Kept printing. Nothing sent."


def test_other_text_ignored():
    assert asyncio.run(plugin.print_stop_dispatch(_event("/print-stopx"), _gateway(FakeAdapter()))) is None


def _bambu(monkeypatch, snapshots, configured=True):
    """Route print_stop through a fake Bambu LAN adapter (#276): snapshots are the normalized status dicts."""
    from print_handy import printers
    client = mock.Mock()
    client.snapshot.side_effect = list(snapshots)
    adapter = S(transport="bambulab_lan", lan_configured=lambda: configured, client=lambda: client,
                normalize_status=lambda raw: raw, control=mock.Mock())
    monkeypatch.setattr(printers, "current", lambda: adapter)
    return adapter, client


def bst(state, filename="plate_1.gcode.3mf", layer=1, total=513, progress=0.01):
    return {"state": state, "filename": filename, "layer": layer, "total_layer": total, "progress": progress}


def test_bambu_status_reads_the_lan_snapshot_not_moonraker(monkeypatch):
    _bambu(monkeypatch, [bst("printing")])
    with mock.patch.object(print_stop, "_call") as call:
        st_now = print_stop.status()
    assert st_now["state"] == "printing" and st_now["filename"] == "plate_1.gcode.3mf" and st_now["layer"] == 1
    call.assert_not_called()


def test_bambu_cancel_sends_one_lan_stop_and_explains_the_error_state(monkeypatch):
    cold = {**bst("error"), "hotend_target_c": 0, "bed_target_c": 0}
    adapter, _ = _bambu(monkeypatch, [bst("printing"), bst("printing"), bst("error"), cold])
    with mock.patch.object(print_stop, "_call") as call, mock.patch.object(print_stop.time, "sleep"):
        sent, text = print_stop.cancel("plate_1.gcode.3mf", wait_s=5)
    assert sent and "acknowledge it on its screen" in text and "still set" not in text
    adapter.control.assert_called_once_with("stop")
    call.assert_not_called()


def test_1147_bambu_cancel_reads_the_heater_targets_back_and_sends_nothing_more(monkeypatch):
    """Bambu gets the same heater read-back as Moonraker and the other families (#1147 printer scope)."""
    warm = {**bst("finish"), "hotend_target_c": 220, "bed_target_c": 0}
    adapter, client = _bambu(monkeypatch, [bst("printing"), bst("finish")] + [warm] * 40)
    clock = iter(range(0, 1000))
    with mock.patch.object(print_stop, "_call") as call, mock.patch.object(print_stop.time, "sleep"),             mock.patch.object(print_stop.time, "monotonic", side_effect=lambda: float(next(clock))):
        sent, text = print_stop.cancel("plate_1.gcode.3mf", wait_s=5)
    assert sent and "the nozzle heater is still set to 220 C" in text
    assert "did not send anything to change it" in text
    adapter.control.assert_called_once_with("stop")  # the one stop; nothing sent for the heater
    call.assert_not_called()


def test_bambu_cancel_refuses_a_different_file_without_sending(monkeypatch):
    adapter, _ = _bambu(monkeypatch, [bst("printing", filename="other.gcode.3mf")])
    sent, text = print_stop.cancel("plate_1.gcode.3mf")
    assert not sent and "different print" in text
    adapter.control.assert_not_called()


def test_bambu_status_refuses_an_unconfigured_lan(monkeypatch):
    _bambu(monkeypatch, [], configured=False)
    import pytest
    with pytest.raises(RuntimeError, match="not configured"):
        print_stop.status()



# --- /print-stop on the families without Moonraker: their own adapter control, never Moonraker URLs ---

import pytest  # noqa: E402

FAMILY_STATUS_READS = {"octoprint": "status", "prusalink": "snapshot", "elegoo_sdcp": "status",
                       "flashforge_stock": "detail"}


def _family_adapter(monkeypatch, connection, states):
    """A fake roster adapter of one family: its client read returns `states` in order, control records."""
    from print_handy.runtime import family_control
    reads = iter(states)
    client = mock.Mock()
    setattr(client, FAMILY_STATUS_READS[connection], lambda: next(reads))
    adapter = mock.Mock(connection=connection, transport=connection, label="Shelf printer")
    adapter.client.return_value = client
    adapter.normalize_status.side_effect = lambda raw: raw
    monkeypatch.setattr(print_stop, "_lan_adapter", lambda printer_id=None: None)
    monkeypatch.setattr(family_control, "adapter_for", lambda printer_id=None: adapter)
    monkeypatch.setattr(family_control.time, "sleep", lambda s: None)
    return adapter


@pytest.mark.parametrize("connection", sorted(FAMILY_STATUS_READS))
def test_family_status_reads_the_adapter_not_moonraker(monkeypatch, connection):
    _family_adapter(monkeypatch, connection, [st("printing", "part.gcode", layer=3, total=90)])
    with mock.patch.object(print_stop, "_call") as call:
        now = print_stop.status("shelf")
    assert now["state"] == "printing" and now["filename"] == "part.gcode" and now["layer"] == 3
    call.assert_not_called()


@pytest.mark.parametrize("connection", sorted(FAMILY_STATUS_READS))
def test_family_cancel_sends_one_adapter_stop_and_reads_back(monkeypatch, connection):
    adapter = _family_adapter(monkeypatch, connection,
                              [st("printing", "part.gcode"), st("printing", "part.gcode"), st("cancelled", "part.gcode")])
    with mock.patch.object(print_stop, "_call") as call:
        sent, text = print_stop.cancel("part.gcode", wait_s=5, printer_id="shelf")
    assert sent and text == "Stopped. Printer now reports cancelled."
    adapter.control.assert_called_once_with("stop")
    call.assert_not_called()


@pytest.mark.parametrize("connection", sorted(FAMILY_STATUS_READS))
def test_family_cancel_refuses_a_different_file_without_sending(monkeypatch, connection):
    adapter = _family_adapter(monkeypatch, connection, [st("printing", "other.gcode")])
    sent, text = print_stop.cancel("part.gcode", printer_id="shelf")
    assert not sent and "different print" in text
    adapter.control.assert_not_called()


def test_a_printer_with_no_control_connection_says_so_plainly(monkeypatch):
    from print_handy.runtime import family_control
    adapter = mock.Mock(connection="offline", transport="offline", label="Shelf printer")
    monkeypatch.setattr(print_stop, "_lan_adapter", lambda printer_id=None: None)
    monkeypatch.setattr(family_control, "adapter_for", lambda printer_id=None: adapter)
    with mock.patch.object(print_stop, "_call") as call:
        assert print_stop.status("shelf")["state"] == print_stop.NO_CONTROL
        sent, text = print_stop.cancel("part.gcode", printer_id="shelf")
    assert not sent and "cannot be stopped from here" in text and "Nothing sent" in text
    adapter.control.assert_not_called()
    call.assert_not_called()


def test_a_moonraker_roster_printer_keeps_the_moonraker_cancel(monkeypatch):
    from print_handy.runtime import family_control
    monkeypatch.setattr(print_stop, "_lan_adapter", lambda printer_id=None: None)
    monkeypatch.setattr(family_control, "adapter_for",
                        lambda printer_id=None: mock.Mock(connection="moonraker", transport="moonraker"))
    seq = [st("printing"), st("cancelled")]
    with mock.patch.object(print_stop, "status", side_effect=seq), \
            mock.patch.object(print_stop, "_send", side_effect=lambda path, **kw: heaters() if path == HEATER_QUERY else {}) as send, \
            mock.patch.object(print_stop.time, "sleep"):
        sent, _text = print_stop.cancel("a.gcode", wait_s=5, printer_id="voron")
    assert sent
    posts = [c for c in send.call_args_list if c.kwargs.get("method") == "POST"]
    assert posts == [mock.call("/printer/print/cancel", method="POST", timeout=15, printer_id="voron")]
    # The heater read-back goes to the same roster printer.
    send.assert_called_with(HEATER_QUERY, printer_id="voron")


@pytest.mark.parametrize("connection", sorted(FAMILY_STATUS_READS))
def test_1147_family_cancel_reports_a_heater_its_status_still_shows_on(monkeypatch, connection):
    hot = {**st("cancelled", "part.gcode"), "hotend_target_c": 0, "bed_target_c": 90}
    adapter = _family_adapter(monkeypatch, connection, [st("printing", "part.gcode")] + [hot] * 50)
    clock = iter(range(0, 1000))
    from print_handy.runtime import family_control
    monkeypatch.setattr(family_control.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(print_stop.time, "monotonic", lambda: float(next(clock)))
    sent, text = print_stop.cancel("part.gcode", wait_s=5, printer_id="shelf")
    assert sent and "the bed heater is still set to 90 C" in text
    adapter.control.assert_called_once_with("stop")
