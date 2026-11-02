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
    assert [b["label"] for b in pab.components("a", "shutdown")[0]["components"]] == ["Cancel print"]
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

    with patch("print_handy.hermes.print_control.status", return_value={"klippy": "ready", "state": "printing", "filename": "f.gcode"}), \
         patch("print_handy.hermes.print_control._call") as call:
        assert pab.handle_click("pause", {"kind": "visual", "filename": "f.gcode"}).startswith("Pause sent.")
    call.assert_called_once_with("/printer/print/pause", method="POST", timeout=15)


def test_cancel_after_shutdown_clears_then_finds_job_gone():
    seq = iter([{"klippy": "shutdown", "state": "paused", "filename": "f.gcode"},
                {"klippy": "ready", "state": "standby", "filename": ""}])
    with patch("print_handy.hermes.print_control.status", side_effect=lambda: next(seq)), \
         patch.object(pab, "clear_shutdown", return_value=(True, "Cleared.")) as clr, \
         patch("print_handy.hermes.print_stop.cancel") as cancel:
        out = pab.handle_click("cancel", {"filename": "f.gcode"})
    clr.assert_called_once(); cancel.assert_not_called()
    assert "Cleared." in out and "already gone" in out


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
    assert "**Why:**" in text and "1512 g" in text and "Cancel print" in text


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
    # Moonraker alert records are unchanged: no connection marker at all.
    assert "connection" not in moonraker and "printer_id" not in moonraker


def test_insertion_options_name_the_material_once():
    from print_handy.hermes import print_alert_buttons as pab_
    spools = [{"id": 21, "remaining_weight": 800,
               "filament": {"name": "Coffee PLA+", "material": "PLA+"}}]
    label = pab_.insertion_select_options(spools)[0]["label"]
    assert label == "#21 Coffee PLA+ 800 g"


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
