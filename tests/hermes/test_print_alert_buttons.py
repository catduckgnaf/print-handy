"""print_alert_buttons: cause text, buttons, click safety. Offline; no printer traffic."""
from unittest.mock import patch

import pytest

from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import moonraker_event_listener as mel

LIVE = ("Nozzle hit bed or part detachment. Weight 1512>1500.0. Z=10. FIRMWARE_RESTART. "
        "https://wiki.zmod.link/Global/#nozzle_control\nOnce the underlying issue is corrected, use the\n")


def test_explain_nozzle_collision_live_text():
    out = pab.explain(LIVE)
    assert "1512 g" in out and "1500 g" in out and "Z=10 mm" in out and "blob" in out


def test_explain_old_format_and_unknown_and_empty():
    assert "1512 g" in pab.explain("Nozzle hit the model or bed! Weight 1512 > 1500; Extruder Off!")
    assert pab.explain("MCU 'mcu' shutdown: Timer too close\nmore") == "ZMod Klipper says: MCU 'mcu' shutdown: Timer too close"
    assert "didn't say why" in pab.explain("")


def test_components_shutdown_has_no_resume(tmp_path):
    assert [b["label"] for b in pab.components("a", "shutdown")[0]["components"]] == [pab.RECOVER_LABEL]
    assert [b["label"] for b in pab.components("a", "paused")[0]["components"]] == ["Resume print", "Cancel print"]
    assert [b["label"] for b in pab.components("a", "visual")[0]["components"]] == [
        "Pause", "It's fine", "Stop asking for this print"]
    # The dark-camera notice carries the mute button and nothing else (#476).
    assert [b["label"] for b in pab.components("a", "visual-dark")[0]["components"]] == [
        "Stop asking for this print"]


def test_visual_button_ids_parse():
    assert pab.parse_custom_id("pbalert:pause:ab12") == ("pause", "ab12")
    assert pab.parse_custom_id("pbalert:acknowledge:ab12") == ("acknowledge", "ab12")
    assert pab.parse_custom_id("pbalert:mute:ab12") == ("mute", "ab12")


def test_mute_visual_is_per_print_identity_not_filename(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"):
        assert not pab.visual_muted("f.gcode", "job:1")
        pab.mute_visual("f.gcode", "job:1")
        assert pab.visual_muted("f.gcode", "job:1")
        # The next print of the same file asks again.
        assert not pab.visual_muted("f.gcode", "job:2")
        assert not pab.visual_muted("other.gcode", "job:1")


def test_mute_store_survives_alert_recording(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"):
        pab.mute_visual("f.gcode", "job:1")
        aid = pab.record("visual", "g.gcode")
        assert pab.visual_muted("f.gcode", "job:1")
        assert pab.take(aid)["filename"] == "g.gcode"
        assert pab.visual_muted("f.gcode", "job:1")


def test_mute_click_records_without_touching_the_printer(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch("print_handy.hermes.print_control.status",
               side_effect=AssertionError("printer read for a mute")), \
         patch("print_handy.hermes.print_stop._lan_adapter",
               side_effect=AssertionError("printer read for a mute")):
        out = pab.handle_click("mute", {"kind": "visual", "filename": "f.gcode", "identity": "job:9"})
        assert pab.visual_muted("f.gcode", "job:9")
        out_dark = pab.handle_click("mute", {"kind": "visual-dark", "filename": "f.gcode", "identity": "job:9"})
    assert "muted for the rest of this print" in out and "untouched" in out
    assert out_dark == out


def test_dark_notice_rejects_other_actions(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"):
        out = pab.handle_click("pause", {"kind": "visual-dark", "filename": "f.gcode"})
    assert "not recognized" in out and "Nothing sent" in out


def test_post_visual_dark_notice_posts_only_the_mute_button(tmp_path, monkeypatch):
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"] = request
        return Response()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_visual_dark_notice("token", "channel", "Camera is dark", "f.gcode", identity="job:3")
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual-dark"]
    import json as _json
    body = _json.loads(seen["request"].data)
    labels = [b["label"] for row in body["components"] for b in row["components"]]
    assert labels == ["Stop asking for this print"]
    assert rows and rows[0]["identity"] == "job:3"


def test_post_visual_text_alert_posts_the_visual_buttons_without_an_image(tmp_path, monkeypatch):
    # The snapshot-failure alert (#765): post_visual_alert's buttons on a
    # plain JSON message — no multipart upload, because there is no frame.
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"], seen["timeout"] = request, timeout
        return Response()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_visual_text_alert("token", "channel", "Camera snapshot failed", "f.gcode", identity="job:3")
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    import json as _json
    body = _json.loads(seen["request"].data)
    assert seen["request"].headers["Content-type"] == "application/json"
    assert body["content"] == "Camera snapshot failed"
    labels = [b["label"] for row in body["components"] for b in row["components"]]
    assert labels == ["Pause", "It's fine", "Stop asking for this print"]
    assert rows and rows[0]["identity"] == "job:3"


def test_post_visual_text_alert_failure_drops_the_record_and_returns_false(tmp_path):
    def fake_open(request, timeout):
        raise OSError("discord unreachable")

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", fake_open):
        assert not pab.post_visual_text_alert("token", "channel", "Camera snapshot failed", "f.gcode")
        assert pab._load() == {}
    # The return-bool contract: missing routing is a False, never a raise.
    assert not pab.post_visual_text_alert("", "channel", "x", "f.gcode")
    assert not pab.post_visual_text_alert("token", "", "x", "f.gcode")


def test_no_hardcoded_discord_api_base():
    # #502: every request URL must be built from the shared bot.rest.API base.
    import pathlib
    source = pathlib.Path(pab.__file__).read_text(encoding="utf-8")
    assert "discord.com/api" not in source


def test_posts_follow_the_shared_rest_api_base(tmp_path, monkeypatch):
    # Redirecting rest.API (the #358 sandbox approach) must redirect button posts too.
    from print_handy.bot import rest
    monkeypatch.setattr(rest, "API", "http://sandbox.invalid/api")
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"] = request
        return Response()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_visual_dark_notice("token", "channel", "Camera is dark", "f.gcode", identity="job:3")
    assert seen["request"].full_url == "http://sandbox.invalid/api/channels/channel/messages"


def test_post_visual_alert_uses_one_multipart_message_with_components(tmp_path, monkeypatch):
    image = tmp_path / "camera.jpg"
    image.write_bytes(b"jpeg-data")
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"], seen["timeout"] = request, timeout
        return Response()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_visual_alert("token", "channel", "Camera check", "f.gcode", image)
    request = seen["request"]
    assert seen["timeout"] == 20
    assert request.headers["Content-type"].startswith("multipart/form-data; boundary=")
    assert b'"label": "Pause"' in request.data and b"\"label\": \"It's fine\"" in request.data
    assert b"jpeg-data" in request.data


def test_record_take_once(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "s.json"):
        aid = pab.record("paused", "f.gcode")
        assert pab.take(aid)["filename"] == "f.gcode"
        assert pab.take(aid) is None
        assert "already answered" in pab.handle_click("cancel", None)


def test_retry_button_custom_id_is_explicit_and_parseable():
    components = pab.retry_components('de' * 16)
    button = components[0]['components'][0]
    assert button['label'] == 'Retry'
    assert button['custom_id'] == 'pbretry:' + 'de' * 16
    assert pab.parse_retry_custom_id(button['custom_id']) == 'de' * 16
    assert pab.parse_retry_custom_id('pbretry:') is None


def test_parse_custom_id():
    assert pab.parse_custom_id("pbalert:cancel:ab12") == ("cancel", "ab12")
    assert pab.parse_custom_id("clarify:x:y") is None
    assert pab.parse_custom_id("pbalert:explode:ab") is None


def test_insertion_components_and_parse_are_button_only():
    components = pab.insertion_components("deadbeef")
    assert [button["label"] for button in components[0]["components"]] == ["Same spool", "Existing spool", "New spool"]
    assert pab.parse_insertion_custom_id("pbinsert:new:deadbeef") == ("new", "deadbeef")
    assert pab.parse_insertion_custom_id("pbinsert:bad:deadbeef") is None


def test_spool_question_buttons_hide_same_without_prior_and_parse():
    assert [button["label"] for button in pab.spool_question_components("deadbeef")[0]["components"]] == ["Existing spool", "New spool", "Cancel print"]
    assert [button["label"] for button in pab.spool_question_components("deadbeef", 8)[0]["components"]] == ["Same spool", "Existing spool", "New spool", "Cancel print"]
    assert pab.parse_spool_question_custom_id("pbspool:new:deadbeef") == ("new", "deadbeef")
    assert pab.parse_spool_question_custom_id("pbspool:cancel:deadbeef") == ("cancel", "deadbeef")
    assert pab.parse_spool_question_custom_id("pbspool:bad:deadbeef") is None


def test_insertion_select_orders_closest_material_and_color_first():
    options = pab.insertion_select_options([
        {"id": 3, "filament": {"material": "PLA", "name": "Black"}},
        {"id": 4, "filament": {"material": "PLA+", "name": "Pink"}},
        {"id": 5, "filament": {"material": "PLA+", "name": "Black"}},
    ], {"material": "PLA+", "color": "black"}, {4})
    assert [option["value"] for option in options] == ["5", "3"]


def test_new_insertion_fixture_preserves_lot_and_never_pushes_printer_screen():
    calls = []

    class Sync:
        @staticmethod
        def parse_new(text):
            assert text == ", PLA+, Black, 1000"
            return {"material": "PLA+", "color": "Black", "grams": 1000}

        @staticmethod
        def create_spool(spec):
            calls.append(("create", spec))
            return {"spool_id": 31}

        @staticmethod
        def bind_slot(slot, spool_id, **kwargs):
            calls.append(("bind", slot, spool_id, kwargs))
            return {"record": {"name": "Black PLA+"}}

    out = pab.create_and_bind_insertion(3, {"brand": "", "material": "PLA+", "color": "Black",
                                            "grams": "1000", "lot": "XHXf1821-ty-21"}, Sync)
    assert "#31" in out and "printer untouched" in out
    assert calls[0][1]["lot"] == "XHXf1821-ty-21"
    assert calls[1][3]["push_screen_update"] is False


def test_visual_acknowledgement_never_contacts_printer():
    assert pab.handle_click("acknowledge", {"kind": "visual", "filename": "f.gcode"}) == (
        "Visual check acknowledged. Monitoring continues; nothing sent to the printer.")


def test_visual_pause_requires_current_matching_print():
    with patch("print_handy.hermes.print_control.status", return_value={"klippy": "ready", "state": "printing", "filename": "other.gcode"}), \
         patch("print_handy.hermes.print_control._call") as call:
        out = pab.handle_click("pause", {"kind": "visual", "filename": "f.gcode"})
    call.assert_not_called()
    assert "different print" in out

    seq = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"},
                {"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    with patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
         patch("print_handy.hermes.print_control._call") as call:
        out = pab.handle_click("pause", {"kind": "visual", "filename": "f.gcode"},
                               pause_wait_s=2, pause_poll_s=0.01)
    assert out.startswith("Paused.")
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)


def _no_discord_route():
    return patch.object(pab._config, "discord_token", lambda: ""), \
        patch.object(pab._config, "discord_chat_id", lambda: "")


def test_visual_pause_stays_printing_says_did_not_pause_and_records_a_fresh_alert(tmp_path):
    """#837 (TESTING-AUDIT #5): no readback used to mean 'Pause sent' forever.

    The printer keeps printing after the POST: the reply must say it did
    not pause, and a fresh visual alert must be recorded so a Pause
    button survives the consumed one.
    """
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode", "identity": "job:7"}
    ready = {"klippy": "ready", "state": "printing", "filename": "f.gcode"}
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", return_value=ready), \
         patch("print_handy.hermes.print_control._call") as call, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=0.03, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)
    assert "did not pause" in out and "still reports printing" in out
    assert len(rows) == 1 and rows[0]["filename"] == "f.gcode" and rows[0]["identity"] == "job:7"


def test_visual_pause_post_failure_returns_a_string_and_records_a_fresh_alert(tmp_path):
    """#837: the pause POST raising used to send an uncaught exception
    to the button handler. It now returns a named reply instead."""
    import urllib.error
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode"}
    ready = {"klippy": "ready", "state": "printing", "filename": "f.gcode"}
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", return_value=ready), \
         patch("print_handy.hermes.print_control._call",
               side_effect=urllib.error.URLError("refused")), \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=0.03, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    assert isinstance(out, str) and "not confirmed" in out and "did not pause" in out
    assert len(rows) == 1 and rows[0]["filename"] == "f.gcode"


def test_visual_pause_print_completes_during_confirmation_reports_actual_state(tmp_path):
    """Codex on #844: a print that completes inside the confirmation
    window is not 'still printing' — the reply must name the actual
    terminal state and no still-printing Pause alert may be re-posted
    (there is nothing left to pause)."""
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode"}
    seq = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"},
                {"klippy": "ready", "state": "complete", "filename": "f.gcode"}])
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
         patch("print_handy.hermes.print_control._call") as call, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=2, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)
    assert "complete" in out
    assert "still reports printing" not in out and "still printing" not in out
    assert rows == []


def test_visual_pause_unreadable_during_confirmation_reports_unknown_not_still_printing(tmp_path):
    """Codex on #844: status reads returning 'unknown' inside the
    confirmation window are not a printing readback — the reply must
    say the outcome could not be confirmed, and the replacement alert
    must not claim the print is still printing."""
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode"}
    seq = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"}]
               + [{"klippy": "unknown", "state": "unknown", "filename": ""}] * 10)
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
         patch("print_handy.hermes.print_control._call"), \
         patch.object(pab, "_repost_alert", wraps=pab._repost_alert) as repost, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=0.05, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    assert "could not be confirmed" in out or "unknown" in out
    assert "still reports printing" not in out and "did not pause" not in out
    assert len(rows) == 1
    assert "still printing" not in repost.call_args[0][3]


def test_visual_pause_post_failure_but_readback_paused_reports_paused(tmp_path):
    """Codex on #844: Moonraker may apply the pause even when the HTTP
    response is lost. The exception path must confirm by readback
    (without resending) and report the pause that actually happened."""
    import urllib.error
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode"}
    seq = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"},
                {"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
         patch("print_handy.hermes.print_control._call",
               side_effect=urllib.error.URLError("response lost")) as call, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=2, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)
    assert out.startswith("Paused.") and "did not pause" not in out
    assert rows == []


def test_visual_pause_post_failure_and_unreadable_reports_unknown_outcome(tmp_path):
    """Codex on #844: a failed POST plus unreadable status is an UNKNOWN
    outcome — never a definitive 'did not pause' — and the replacement
    alert must not claim the print is still printing."""
    import urllib.error
    token_patch, chat_patch = _no_discord_route()
    alert = {"kind": "visual", "filename": "f.gcode"}
    reads = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"}])

    def status():
        try:
            return next(reads)
        except StopIteration:
            raise RuntimeError("printer unreachable")

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status", side_effect=status), \
         patch("print_handy.hermes.print_control._call",
               side_effect=urllib.error.URLError("response lost")) as call, \
         patch.object(pab, "_repost_alert", wraps=pab._repost_alert) as repost, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert, pause_wait_s=0.05, pause_poll_s=0.01)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "visual"]
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)
    assert "unknown" in out and "did not pause" not in out
    assert len(rows) == 1
    assert "still printing" not in repost.call_args[0][3]


def test_visual_pause_controller_not_ready_sends_nothing():
    alert = {"kind": "visual", "filename": "f.gcode"}
    with patch("print_handy.hermes.print_control.status",
               return_value={"klippy": "shutdown", "state": "printing", "filename": "f.gcode"}), \
         patch("print_handy.hermes.print_control._call") as call, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert)
    call.assert_not_called()
    assert "controller is shutdown" in out and "Nothing sent" in out


def test_visual_pause_when_already_paused_sends_nothing():
    alert = {"kind": "visual", "filename": "f.gcode"}
    with patch("print_handy.hermes.print_control.status",
               return_value={"klippy": "ready", "state": "paused", "filename": "f.gcode"}), \
         patch("print_handy.hermes.print_control._call") as call, \
         patch("print_handy.hermes.print_stop._lan_adapter", return_value=None):
        out = pab.handle_click("pause", alert)
    call.assert_not_called()
    assert "Nothing to pause" in out and "Nothing sent" in out


def test_resume_still_paused_reposts_the_paused_alert(tmp_path):
    """#837: the alert record was already claimed when Resume was tapped,
    so a resume that did not take must leave a fresh paused alert with
    Resume/Cancel buttons behind."""
    token_patch, chat_patch = _no_discord_route()
    msg = ("Resume sent, but the printer still reports paused after 20s. "
           "Check the printer screen. Not retrying.")
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), token_patch, chat_patch, \
         patch("print_handy.hermes.print_control.status",
               return_value={"klippy": "ready", "state": "paused", "filename": "f.gcode"}), \
         patch("print_handy.hermes.print_control.resume", return_value=(False, msg)):
        out = pab.handle_click("resume", {"kind": "paused", "filename": "f.gcode"})
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "paused"]
    assert out == msg
    assert len(rows) == 1 and rows[0]["filename"] == "f.gcode"


def test_resume_success_records_no_fresh_alert(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch("print_handy.hermes.print_control.status",
               return_value={"klippy": "ready", "state": "paused", "filename": "f.gcode"}), \
         patch("print_handy.hermes.print_control.resume", return_value=(True, "Resumed.")):
        out = pab.handle_click("resume", {"kind": "paused", "filename": "f.gcode"})
        load_after = pab._load()
    assert out == "Resumed."
    assert load_after == {}


def test_cancel_after_shutdown_refuses_instead_of_restarting():
    """#838 item 9: alert Cancel during a Klipper shutdown no longer runs
    clear_shutdown (FIRMWARE_RESTART) as a side effect; it refuses and
    points at /print-reboot. clear_shutdown itself is unchanged."""
    st = {"klippy": "shutdown", "state": "paused", "filename": "f.gcode"}
    with patch("print_handy.hermes.print_control.status", return_value=st), \
         patch.object(pab, "clear_shutdown") as clr, \
         patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.handle_click("cancel", {"filename": "f.gcode"})
    clr.assert_not_called(); cancel.assert_not_called()
    assert "/print-reboot" in out


def test_cancel_paused_ready_cancels_same_file_only():
    st = {"klippy": "ready", "state": "paused", "filename": "other.gcode"}
    with patch("print_handy.hermes.print_control.status", return_value=st), patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.handle_click("cancel", {"filename": "f.gcode"})
    cancel.assert_not_called(); assert "different print" in out
    st["filename"] = "f.gcode"
    with patch("print_handy.hermes.print_control.status", return_value=st), patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel:
        assert pab.handle_click("cancel", {"filename": "f.gcode"}) == "Stopped."
    cancel.assert_called_once_with("f.gcode")


def test_resume_after_shutdown_refuses_without_restart():
    with patch("print_handy.hermes.print_control.status", return_value={"klippy": "shutdown", "state": "paused", "filename": "f"}), \
         patch("print_handy.hermes.print_control._call") as call, patch.object(pab, "clear_shutdown") as clr:
        out = pab.handle_click("resume", {"filename": "f"})
    call.assert_not_called(); clr.assert_not_called()
    assert "Nothing sent" in out


def test_clear_shutdown_reports_timeout_without_retry():
    calls = []
    ok, msg = pab.clear_shutdown(wait_s=0.05, poll_s=0.01, caller=lambda *a, **k: calls.append(a),
                                 reader=lambda: {"klippy": "shutdown"})
    assert not ok and len(calls) == 1 and "Not retrying" in msg


def test_clear_shutdown_uses_non_klipper_profile_firmware_name(monkeypatch):
    from print_handy import printers, settings

    config = settings.load(home=settings.HOME, local=False,
                           environ={"PRINT_HANDY__PRINTER__MODEL": "bambulab.a1"})
    profile = printers.current(config)
    monkeypatch.setattr(printers, "current", lambda: profile)
    ok, message = pab.clear_shutdown(wait_s=0.05, poll_s=0.001, caller=lambda *a, **k: None,
                                     reader=lambda: {"klippy": "ready"})

    assert ok
    assert "Bambu Lab (offline handoff only)" in message
    assert "Klipper" not in message


def test_listener_shutdown_text_has_reason():
    text = mel.shutdown_alert_text("Hello-Kitty_plate_1_" + "ab" * 32 + ".gcode", LIVE)
    assert "**Why:**" in text and "1512 g" in text and "Cancel print" not in text and "restarts the printer" in text


def test_listener_falls_back_to_plain_post():
    with patch.object(pab, "post_with_buttons", return_value=False), patch.object(mel, "post", return_value=True) as p:
        assert mel.post_alert("hi", "paused", "f") is True
    p.assert_called_once_with("hi")


class _RetireResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _record_with_message(kind, filename, message_id="m1", channel_id="c1"):
    aid = pab.record(kind, filename)
    data = pab._load()
    data[aid].update(message_id=message_id, channel_id=channel_id)
    pab._save(data)
    return aid


def test_retire_alerts_strips_paused_prompt_by_default(tmp_path):
    # #370: the paused prompt (kind "paused") must be retired by the default
    # kinds, or its Resume/Cancel buttons stay live on a dead job.
    seen = {}

    def fake_open(request, timeout):
        seen["request"] = request
        return _RetireResponse()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            patch.object(pab.urllib.request, "urlopen", fake_open):
        aid = _record_with_message("paused", "f.gcode")
        assert pab.retire_alerts("token", "f.gcode", "This prompt is closed.") == 1
        assert pab.peek(aid) is None
    import json as _json
    body = _json.loads(seen["request"].data)
    assert body["components"] == []
    assert body["content"].startswith("~~Printer firmware stopped.~~")
    assert seen["request"].method == "PATCH"
    assert "/channels/c1/messages/m1" in seen["request"].full_url


def test_retire_alerts_custom_headline(tmp_path):
    def fake_open(request, timeout):
        fake_open.body = request.data
        return _RetireResponse()

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            patch.object(pab.urllib.request, "urlopen", fake_open):
        _record_with_message("paused", "f.gcode")
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",),
                                 headline="Print cancelled.") == 1
    import json as _json
    assert _json.loads(fake_open.body)["content"].startswith("~~Print cancelled.~~ Closed.")


def test_retire_alerts_leaves_other_jobs_and_kinds_alone(tmp_path):
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            patch.object(pab.urllib.request, "urlopen", lambda req, timeout: _RetireResponse()):
        other = _record_with_message("paused", "other.gcode")
        visual = _record_with_message("visual", "f.gcode")
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",)) == 0
        assert pab.peek(other) is not None and pab.peek(visual) is not None


# -- #460: family pause alerts route to the family's own control --------------

class _FakeFamilyControl:
    connection = "prusalink"

    def __init__(self):
        self.calls = []

    def resume(self, filename):
        self.calls.append(("resume", filename))
        return True, "Resumed. Printer now reports printing."

    def cancel(self, filename):
        self.calls.append(("cancel", filename))
        return True, "Stopped. Printer now reports standby."


def test_family_alert_clicks_route_to_family_control_never_moonraker():
    from unittest.mock import Mock
    control = _FakeFamilyControl()
    alert = {"kind": "paused", "filename": "f.gcode", "connection": "prusalink"}
    with patch("print_handy.hermes.family_control.control_for", return_value=control) as resolve, \
         patch("print_handy.hermes.print_control.status", Mock(side_effect=AssertionError("Moonraker path taken"))):
        assert pab.handle_click("resume", alert).startswith("Resumed.")
        assert pab.handle_click("cancel", alert).startswith("Stopped.")
    assert control.calls == [("resume", "f.gcode"), ("cancel", "f.gcode")]
    resolve.assert_called_with(None)


def test_family_alert_click_refuses_when_the_printer_is_not_controllable():
    alert = {"kind": "paused", "filename": "f.gcode", "connection": "prusalink"}
    with patch("print_handy.hermes.family_control.control_for", return_value=None):
        out = pab.handle_click("resume", alert)
    assert "can't control" in out and "Nothing sent" in out

    wrong = _FakeFamilyControl()
    wrong.connection = "octoprint"
    with patch("print_handy.hermes.family_control.control_for", return_value=wrong):
        out = pab.handle_click("cancel", alert)
    assert "Nothing sent" in out and wrong.calls == []


def test_family_alert_record_carries_its_connection(tmp_path):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"id": "4242"}'

    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", lambda req, timeout: Response()):
        assert pab.post_with_buttons("token", "chan", "Paused: f.gcode", "paused",
                                     "f.gcode", connection="prusalink",
                                     printer_id="prusa-two")
        assert pab.post_with_buttons("token", "chan", "Paused: g.gcode", "paused", "g.gcode")
    import json as _json
    data = _json.loads((tmp_path / "alerts.json").read_text())
    family = next(r for r in data.values() if r["filename"] == "f.gcode")
    moonraker = next(r for r in data.values() if r["filename"] == "g.gcode")
    assert family["connection"] == "prusalink" and family["printer_id"] == "prusa-two"
    # Moonraker alert records carry no connection marker, but they do name their printer (the
    # process's own, here the default), so the buttons act on the printer that raised them.
    from print_handy import settings
    assert "connection" not in moonraker and moonraker["printer_id"] == settings.default_printer_id()


def test_insertion_options_name_the_material_once():
    from print_handy.hermes import print_alert_buttons as pab_
    spools = [{"id": 21, "remaining_weight": 800,
               "filament": {"name": "Coffee PLA+", "material": "PLA+"}}]
    label = pab_.insertion_select_options(spools)[0]["label"]
    assert label == "Coffee · PLA+ · #21 · 800 g left"


def test_plate_picker_maps_long_part_ids_instead_of_truncating(tmp_path):
    """A part id over Discord's 100-char select-value cap must travel as a
    short opaque token mapped to the full id in the alert record; a
    truncated id can never match resolve_next's exact-id lookup (P2-12)."""
    import json as _json
    long_id = "part-" + "x" * 140
    seen = {}

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def fake_open(request, timeout):
        seen["request"] = request
        return Response()

    options = [{"label": "Plate 1", "value": "short-id"},
               {"label": "Plate 2", "value": long_id}]
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
         patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_plate_picker("token", "channel", "Which plate?", "plan1", options)
        rows = [r for r in pab._load().values() if isinstance(r, dict) and r.get("kind") == "plate-picker"]
        assert len(rows) == 1
        alert = rows[0]
    body = _json.loads(seen["request"].data)
    posted = body["components"][0]["components"][0]["options"]
    assert posted[0]["value"] == "short-id"
    token = posted[1]["value"]
    assert token != long_id[:100] and len(token) <= 100
    # The pick handler resolves the token back to the exact full id.
    assert pab.plate_part_id(alert, token) == long_id
    assert pab.plate_part_id(alert, "short-id") == "short-id"
    assert pab.plate_part_id({}, token) == token  # pre-mapping alerts pass through


def _insertion_sync(*, slot_prior=7, bind_error=None):
    """A fake spool_sync for the insertion create+bind path (P2-204)."""
    from print_handy.hermes import spool_sync as real_sync

    class Sync:
        SpoolError = real_sync.SpoolError
        calls = []

        @staticmethod
        def parse_new(text):
            return {"brand": "B", "material": "PLA", "color": "Black", "grams": 1000}

        @staticmethod
        def load_state():
            return {"filament_slots": {"2": {"spoolman_id": slot_prior}}}

        @staticmethod
        def create_spool(spec):
            Sync.calls.append("create")
            return {"spool_id": 42}

        @staticmethod
        def bind_slot(slot, spool_id, **kwargs):
            Sync.calls.append("bind")
            if bind_error is not None:
                raise bind_error
            return {"record": {"name": "Black PLA"}, "prior_spool_id": slot_prior}

    return Sync


def test_stale_confirmation_creates_nothing():
    """The expected prior spool is re-checked against the slot's live
    mapping BEFORE the new spool is created (P2-204)."""
    from print_handy.hermes import spool_sync as real_sync
    Sync = _insertion_sync(slot_prior=9)
    with pytest.raises(real_sync.SpoolError) as excinfo:
        pab.create_and_bind_insertion(2, {"material": "PLA", "color": "Black", "grams": "1000"},
                                      Sync, expected_prior_spool_id=7)
    assert Sync.calls == [], "a spool was created for a stale confirmation"
    assert "#7" in str(excinfo.value) and "Nothing was created" in str(excinfo.value)


def test_bind_failure_reports_the_created_spool():
    """When the bind refuses after creation, the error names the created
    spool and that it stays on the shelf — never a bare failure that makes
    a retry create a duplicate (P2-204)."""
    from print_handy.hermes import spool_sync as real_sync
    Sync = _insertion_sync(bind_error=real_sync.SpoolError("spool #42 is archived"))
    with pytest.raises(real_sync.SpoolError) as excinfo:
        pab.create_and_bind_insertion(2, {"material": "PLA", "color": "Black", "grams": "1000"},
                                      Sync, expected_prior_spool_id=7)
    message = str(excinfo.value)
    assert "#42" in message and "shelf" in message and "slot 2" in message
def test_retire_alerts_scoped_to_the_owning_printer(tmp_path):
    """P2-176: two printers can pause the same filename in one channel.
    One printer's terminal transition retires only its own alerts; the
    other printer's live Resume/Cancel buttons survive."""
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            patch.object(pab.urllib.request, "urlopen", lambda req, timeout: _RetireResponse()):
        mine = pab.record("paused", "f.gcode", printer_id="printer-a")
        theirs = pab.record("paused", "f.gcode", printer_id="printer-b")
        data = pab._load()
        data[mine].update(message_id="m1", channel_id="c1")
        data[theirs].update(message_id="m2", channel_id="c1")
        pab._save(data)
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",),
                                 printer_id="printer-a") == 1
        assert pab.peek(mine) is None
        assert pab.peek(theirs) is not None
        # Unscoped retirement still clears the rest — the Moonraker
        # listener retires its own printer's alerts that way.
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",)) == 1
        assert pab.peek(theirs) is None


def test_retire_alerts_legacy_unscoped_alert_retires_for_the_default_printer_only(tmp_path):
    """2.2.5 review (P2-176): an alert stored without a printer_id belongs to
    the default printer; another printer's scoped retirement leaves it alone."""
    with patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            patch("print_handy.settings.default_printer_id", return_value="printer-a"), \
            patch.object(pab.urllib.request, "urlopen", lambda req, timeout: _RetireResponse()):
        legacy = _record_with_message("paused", "f.gcode")
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",),
                                 printer_id="printer-b") == 0
        assert pab.peek(legacy) is not None
        assert pab.retire_alerts("token", "f.gcode", "Closed.", ("paused",),
                                 printer_id="printer-a") == 1
        assert pab.peek(legacy) is None


def test_resume_click_with_empty_head_reposts_paused_alert():
    """2026-11-04: a Resume tap while the head sensor reads empty sends nothing and keeps the buttons."""
    from print_handy.hermes import print_alert_buttons as pab, print_control as pc
    alert = {"filename": "foo.gcode"}
    with patch.object(pc, "status", return_value={"state": "paused", "klippy": "ready", "filename": "foo.gcode"}), \
         patch.object(pc, "resume", return_value=(False, pc.HEAD_EMPTY_TEXT)), \
         patch.object(pab, "_repost_alert") as repost:
        msg = pab.handle_click("resume", alert)
    assert msg == pc.HEAD_EMPTY_TEXT
    repost.assert_called_once()


def test_shutdown_alert_offers_only_the_recover_button():
    aid = "abc"
    btns = pab.components(aid, "shutdown")[0]["components"]
    assert [b["label"] for b in btns] == [pab.RECOVER_LABEL]
    assert pab.parse_custom_id(btns[0]["custom_id"]) == ("recover", aid)


def test_recover_restarts_then_cancels_loaded_job():
    """Shutdown stop followed by the button: restart first, then cancel the still-loaded job."""
    order = []
    sts = iter([{"klippy": "shutdown", "state": "paused", "filename": "f.gcode"},
                {"klippy": "ready", "state": "paused", "filename": "f.gcode"},
                {"klippy": "ready", "state": "cancelled", "filename": ""}])
    def clear(*a, **k):
        order.append("restart"); return True, "Cleared the emergency stop."
    def cancel(name, **k):
        order.append(("cancel", name)); return True, "Stopped."
    with patch("print_handy.hermes.print_control.status", side_effect=lambda **k: next(sts)), \
         patch.object(pab, "clear_shutdown", side_effect=clear), \
         patch("print_handy.hermes.print_stop.cancel", side_effect=cancel):
        out = pab.handle_click("recover", {"kind": "shutdown", "filename": "f.gcode"})
    assert order == ["restart", ("cancel", "f.gcode")]
    assert "Cleared the emergency stop." in out and "Stopped." in out and pab.MAINTENANCE_FOLLOW_UP in out


def test_recover_withholds_clearance_when_cancel_does_not_end_the_job():
    """A cancel that leaves the job printing or paused must not claim the print is cleared."""
    sts = iter([{"klippy": "ready", "state": "paused", "filename": "f.gcode"},
                {"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    with patch("print_handy.hermes.print_control.status", side_effect=lambda **k: next(sts)), \
         patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Cancel sent, but the printer still reports paused after 20s.")):
        out = pab.recover_from_stop("f.gcode")
    assert "still reports paused" in out
    assert pab.MAINTENANCE_FOLLOW_UP not in out


def test_recover_withholds_clearance_when_a_different_file_is_loaded():
    sts = [{"klippy": "ready", "state": "printing", "filename": "other.gcode"}]
    with patch("print_handy.hermes.print_control.status", side_effect=lambda **k: sts[0]), \
         patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.recover_from_stop("f.gcode")
    cancel.assert_not_called()
    assert "Not cancelling it" in out
    assert pab.MAINTENANCE_FOLLOW_UP not in out


def test_recover_withholds_clearance_when_the_outcome_cannot_be_read():
    sts = iter([{"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    def status(**k):
        row = next(sts, None)
        if row is None:
            raise OSError("status unread")
        return row
    with patch("print_handy.hermes.print_control.status", side_effect=status), \
         patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped. Printer now reports cancelled.")):
        out = pab.recover_from_stop("f.gcode")
    assert "Stopped." in out
    assert pab.MAINTENANCE_FOLLOW_UP not in out


def test_recover_does_not_cancel_when_restart_fails():
    with patch("print_handy.hermes.print_control.status", return_value={"klippy": "shutdown", "state": "paused", "filename": "f.gcode"}), \
         patch.object(pab, "clear_shutdown", return_value=(False, "not ready")), \
         patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.handle_click("recover", {"kind": "shutdown", "filename": "f.gcode"})
    cancel.assert_not_called(); assert out == "not ready"


def test_recover_refuses_a_non_shutdown_alert():
    with patch.object(pab, "clear_shutdown") as clr:
        assert "not recognized" in pab.handle_click("recover", {"kind": "paused", "filename": "f"})
    clr.assert_not_called()


# --- an alert's buttons act on the printer that raised it, never the default printer ---

TWO_PRINTERS = [{"id": "printer-a"}, {"id": "printer-b"}]


def _roster(entries):
    return patch("print_handy.settings.printers", return_value=entries), \
        patch("print_handy.settings.default_printer_id", return_value=entries[0]["id"])


def test_every_printer_alert_records_the_printer_that_raised_it(tmp_path, monkeypatch):
    """Visual alerts and the listener's posts used to be recorded without printer_id."""
    from print_handy import listeners
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: "printer-b")
    monkeypatch.setattr(pab.urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(OSError("offline")))
    seen = []
    real_record = pab.record
    monkeypatch.setattr(pab, "record", lambda kind, filename, **extra: seen.append(extra) or real_record(kind, filename, **extra))
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpeg")
    pab.post_visual_alert("t", "c", "x", "f.gcode", image)
    pab.post_visual_text_alert("t", "c", "x", "f.gcode")
    pab.post_visual_dark_notice("t", "c", "x", "f.gcode")
    pab.post_with_buttons("t", "c", "x", "paused", "f.gcode")
    pab.post_with_buttons("t", "c", "x", "paused", "f.gcode", printer_id="printer-a")
    assert [e.get("printer_id") for e in seen] == ["printer-b"] * 4 + ["printer-a"]


def test_a_process_without_an_instance_records_the_default_printer(tmp_path, monkeypatch):
    from print_handy import listeners
    monkeypatch.setattr(listeners, "instance_printer_id", lambda: None)
    roster, default = _roster(TWO_PRINTERS)
    with roster, default:
        assert pab._owner() == "printer-a" and pab._owner("printer-b") == "printer-b"


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "recover"])
def test_buttons_act_on_the_alerts_printer(action):
    kind = {"pause": "visual", "recover": "shutdown"}.get(action, "paused")
    alert = {"kind": kind, "filename": "f.gcode", "printer_id": "printer-b"}
    printing = {"klippy": "ready", "state": "printing" if action == "pause" else "paused", "filename": "f.gcode"}
    roster, default = _roster(TWO_PRINTERS)
    with roster, default, \
            patch("print_handy.hermes.print_stop._lan_adapter", return_value=None) as lan, \
            patch("print_handy.hermes.print_control.status", return_value=printing) as status, \
            patch("print_handy.hermes.print_control._send") as send, \
            patch("print_handy.hermes.print_control.resume", return_value=(True, "Resumed.")) as resume, \
            patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel, \
            patch.object(pab, "recover_from_stop", return_value="Recovered.") as recover:
        pab.handle_click(action, alert, pause_wait_s=0.05, pause_poll_s=0.01)
    if action == "pause":
        lan.assert_called_with(printer_id="printer-b")
        assert all(c.args == ("printer-b",) or c.kwargs == {"printer_id": "printer-b"} for c in status.call_args_list)
        send.assert_called_once_with("/printer/print/pause", method="POST", timeout=15, printer_id="printer-b")
    elif action == "resume":
        resume.assert_called_once_with("f.gcode", printer_id="printer-b")
        status.assert_called_once_with(printer_id="printer-b")
    elif action == "cancel":
        cancel.assert_called_once_with("f.gcode", printer_id="printer-b")
        status.assert_called_once_with(printer_id="printer-b")
    else:
        recover.assert_called_once_with("f.gcode", "printer-b")


@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "recover"])
def test_an_old_alert_without_a_printer_is_refused_when_several_are_configured(action):
    kind = {"pause": "visual", "recover": "shutdown"}.get(action, "paused")
    roster, default = _roster(TWO_PRINTERS)
    with roster, default, patch("print_handy.hermes.print_control.status") as status, \
            patch("print_handy.hermes.print_stop.cancel") as cancel, \
            patch("print_handy.hermes.print_control.resume") as resume:
        out = pab.handle_click(action, {"kind": kind, "filename": "f.gcode"})
    assert out == pab.ALERT_PRINTER_UNKNOWN
    status.assert_not_called(); cancel.assert_not_called(); resume.assert_not_called()


def test_an_old_alert_without_a_printer_keeps_working_with_one_printer():
    st = {"klippy": "ready", "state": "paused", "filename": "f.gcode"}
    roster, default = _roster([{"id": "printer-a"}])
    with roster, default, patch("print_handy.hermes.print_control.status", return_value=st), \
            patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel:
        assert pab.handle_click("cancel", {"kind": "paused", "filename": "f.gcode"}) == "Stopped."
    cancel.assert_called_once_with("f.gcode")


def test_the_default_printers_alert_keeps_the_configured_printer_call_shape():
    st = {"klippy": "ready", "state": "paused", "filename": "f.gcode"}
    roster, default = _roster(TWO_PRINTERS)
    with roster, default, patch("print_handy.hermes.print_control.status", return_value=st), \
            patch("print_handy.hermes.print_stop.cancel", return_value=(True, "Stopped.")) as cancel:
        pab.handle_click("cancel", {"kind": "paused", "filename": "f.gcode", "printer_id": "printer-a"})
    cancel.assert_called_once_with("f.gcode")
