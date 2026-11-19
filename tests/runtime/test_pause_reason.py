"""Print Handy tells the printer why it paused (prestonbrown/helixscreen#2130)."""
from unittest.mock import MagicMock, call, patch

import pytest

from print_handy import printer_control
from print_handy.runtime import pause_reason as pr


def _status(message):
    return {"result": {"status": {"display_status": {"message": message}}}}


def test_message_has_the_fixed_shape_other_screens_parse():
    assert pr.message("possible spaghetti") == "Paused by Print Handy: possible spaghetti"
    assert pr.is_ours(pr.message("x"))
    assert not pr.is_ours("Layer 12 of 300")
    assert not pr.is_ours(None)


def test_message_is_one_gcode_safe_line():
    text = pr.message('first; second #third "quoted"\nnext\tline')
    assert ";" not in text and "#" not in text and '"' not in text
    assert "\n" not in text and "\t" not in text
    assert text == "Paused by Print Handy: first, second ,third ,quoted, next line"
    long = pr.message("x" * 500)
    assert len(long) <= len(pr.PREFIX) + pr.MAX_REASON and long.endswith("...")
    assert pr.message("   ") == "Paused by Print Handy: paused"


@pytest.mark.parametrize("verdict, reason", [
    ("spaghetti", "possible spaghetti"),
    ("detached", "the part may have come loose"),
    ("air", "possibly printing in mid-air"),
    ("uncertain", pr.CAMERA_REASON),
    (None, pr.CAMERA_REASON),
])
def test_each_auto_pause_verdict_has_a_plain_reason(verdict, reason):
    assert pr.verdict_reason(verdict) == reason


def test_announce_sends_m117_only_by_default():
    send = MagicMock()
    assert pr.announce(send, "possible spaghetti", printer_id="p2") is True
    send.assert_called_once_with("/printer/gcode/script", method="POST",
                                 data={"script": "M117 Paused by Print Handy: possible spaghetti"},
                                 timeout=10, printer_id="p2")


def test_announce_adds_an_action_prompt_when_opted_in():
    send = MagicMock()
    pr.announce(send, "possible spaghetti", prompt=True)
    lines = send.call_args.kwargs["data"]["script"].split("\n")
    assert lines[0] == "M117 Paused by Print Handy: possible spaghetti"
    assert lines[1:] == [
        'RESPOND TYPE=command MSG="action:prompt_begin Paused by Print Handy"',
        'RESPOND TYPE=command MSG="action:prompt_text possible spaghetti"',
        'RESPOND TYPE=command MSG="action:prompt_footer_button OK|RESPOND TYPE=command MSG=action:prompt_end"',
        'RESPOND TYPE=command MSG="action:prompt_show"',
    ]


def test_announce_never_raises():
    send = MagicMock(side_effect=OSError("printer unreachable"))
    assert pr.announce(send, "possible spaghetti") is False


def test_clear_only_clears_our_own_message():
    send = MagicMock(return_value=_status("Paused by Print Handy: possible spaghetti"))
    assert pr.clear(send, printer_id="p2") is True
    assert send.call_args_list == [
        call(pr.STATUS_PATH, printer_id="p2"),
        call("/printer/gcode/script", method="POST", data={"script": "M117"}, timeout=10, printer_id="p2"),
    ]

    macro = MagicMock(return_value=_status("Changing to tool 2"))
    assert pr.clear(macro) is False
    macro.assert_called_once_with(pr.STATUS_PATH)  # read only: a macro's message stays


def test_clear_closes_the_prompt_when_opted_in():
    send = MagicMock(return_value=_status("Paused by Print Handy: x"))
    pr.clear(send, prompt=True)
    assert send.call_args.kwargs["data"]["script"] == 'M117\nRESPOND TYPE=command MSG="action:prompt_end"'


def test_clear_never_raises():
    assert pr.clear(MagicMock(side_effect=OSError("down"))) is False
    send = MagicMock(side_effect=[_status("Paused by Print Handy: x"), OSError("down")])
    assert pr.clear(send) is False


def test_prompt_setting_is_off_unless_exactly_true():
    assert pr.prompt_enabled({"printer": {"pause_prompt": True}}) is True
    for view in ({"printer": {"pause_prompt": "true"}}, {"printer": {}}, {}, None):
        assert pr.prompt_enabled(view) is False


def test_send_pause_sets_the_reason_then_pauses_once():
    send = MagicMock()
    with patch("print_handy.runtime.print_control._send", send):
        control = printer_control.for_printer("p2", view={"printer": {"pause_prompt": False}})
        control.send_pause("possible spaghetti")
    assert [c.args[0] for c in send.call_args_list] == ["/printer/gcode/script", "/printer/print/pause"]


def test_send_pause_still_pauses_when_the_reason_and_settings_fail():
    def send(path, **kw):
        if path == "/printer/gcode/script":
            raise OSError("M117 refused")
        sent.append(path)
    sent = []
    control = printer_control.for_printer("p2")
    with patch("print_handy.runtime.print_control._send", send), \
            patch.object(printer_control.PrinterControl, "view",
                         new_callable=lambda: property(lambda self: (_ for _ in ()).throw(KeyError("model")))):
        control.send_pause("possible spaghetti")
    assert sent == ["/printer/print/pause"]


def test_send_pause_without_a_reason_is_unchanged():
    send = MagicMock()
    with patch("print_handy.runtime.print_control._send", send):
        printer_control.for_printer(None, view={}).send_pause()
    send.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)


def test_a_confirmed_resume_clears_the_reason():
    send = MagicMock(return_value=_status("Paused by Print Handy: possible spaghetti"))
    with patch("print_handy.runtime.print_control.resume", return_value=(True, "Resumed.")), \
            patch("print_handy.runtime.print_control._send", send):
        out = printer_control.for_printer("p2", view={}).resume("f.gcode")
    assert out == (True, "Resumed.")
    assert send.call_args_list[-1].kwargs["data"] == {"script": "M117"}


def test_a_resume_that_did_not_take_leaves_the_reason():
    send = MagicMock()
    with patch("print_handy.runtime.print_control.resume", return_value=(False, "Still paused.")), \
            patch("print_handy.runtime.print_control._send", send):
        printer_control.for_printer("p2", view={}).resume("f.gcode")
    send.assert_not_called()


def test_the_listener_clears_after_a_resume_from_the_printer_screen():
    from print_handy.runtime import moonraker_event_listener as mel
    with patch.object(printer_control.PrinterControl, "clear_pause_reason", return_value=True) as clear, \
            patch.object(mel, "_watch_printer_id", return_value="p2"):
        assert mel._clear_pause_reason() is True
    clear.assert_called_once_with()


def _load(key, **env):
    from print_handy import settings
    from tests.printers.test_adapters import a1_fixture
    return settings.load(local=False, environ={
        "PRINT_HANDY__PRINTER__MODEL": key,
        "PRINT_HANDY__PATHS__STATE_HOME": str(a1_fixture.HERMES), **env})


def test_pause_prompt_setting_defaults_off_and_is_klipper_only():
    from print_handy import settings
    assert _load("klipper.generic")["printer"]["pause_prompt"] is False
    assert _load("klipper.generic", PRINT_HANDY__PRINTER__PAUSE_PROMPT="true")["printer"]["pause_prompt"] is True
    _load("bambulab.a1", PRINT_HANDY__PRINTER__PAUSE_PROMPT="false")  # off is fine anywhere
    with pytest.raises(settings.SettingsError, match="only applies to Klipper printers"):
        _load("bambulab.a1", PRINT_HANDY__PRINTER__PAUSE_PROMPT="true")


def test_a_resume_from_the_printer_screen_clears_the_reason(monkeypatch):
    import asyncio
    from print_handy.runtime import moonraker_event_listener as mel
    cleared = []
    monkeypatch.setattr(mel, "_clear_pause_reason", lambda: cleared.append(True) or True)
    listener = mel.Listener()
    listener.filename = "f.gcode"
    asyncio.run(listener._apply_resume_or_start("paused", "printing", False))
    assert cleared == [True]
