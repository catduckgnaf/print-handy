"""#620: the progress control lists both objects and sends one name only after confirm."""
from pathlib import Path

from print_handy.core.exclude_object import exclude_plan
from print_handy.runtime.exclude_chat import (
    components, confirm_send, parse_custom_id, prepare, surface_text,
)


def _live(state="printing", filename="plate.gcode", excluded=()):
    return lambda: {"state": state, "filename": filename, "excluded": list(excluded)}


def test_two_objects_render_and_a_file_with_no_metadata_does_not():
    names = ["hinge_left", "hinge_right"]
    text = surface_text(names)
    assert "hinge_left" in text and "hinge_right" in text
    options = components("abc", names)[0]["components"][0]["options"]
    assert [row["label"] for row in options] == ["Cancel hinge_left", "Cancel hinge_right"]
    assert options[0]["value"] == "0"
    assert parse_custom_id("pbexclude:pick:abc") == ("pick", "abc")
    assert surface_text([]) == ""
    assert components("abc", []) == []
    assert parse_custom_id("pbplate:go:abc") is None


def test_choose_asks_and_confirm_sends_only_that_name():
    alert = {"kind": "exclude-pick", "filename": "plate.gcode", "names": ["hinge_left", "hinge_right"]}
    asked = prepare(alert, 0, _live())
    assert asked["step"] == "confirm" and asked["name"] == "hinge_left"
    unconfirmed = exclude_plan(alert["names"], "hinge_left", confirmed=False)
    assert unconfirmed["body"] is None
    sent_to = []

    def sender(script):
        sent_to.append(script)

    gone = confirm_send({"kind": "exclude-go", "name": "hinge_left", "names": alert["names"],
                         "filename": "plate.gcode"}, sender, _live())
    assert gone["sent"] is True
    assert sent_to == ["EXCLUDE_OBJECT NAME=hinge_left"]
    assert "hinge_right" not in sent_to[0]


def test_last_object_is_the_stop_confirm_and_a_failed_send_is_not_cancelled():
    last = prepare({"kind": "exclude-pick", "names": ["only"], "filename": "plate.gcode"}, 0, _live())
    assert last["step"] == "cancel_print"
    sent_to = []
    refused = confirm_send({"kind": "exclude-go", "name": "only", "names": ["only"],
                            "filename": "plate.gcode"},
                           lambda script: sent_to.append(script), _live())
    assert refused["sent"] is False and sent_to == []
    assert "excluded" in refused["text"].lower() or "Nothing" in refused["text"]

    def boom(script):
        raise OSError("refused")

    failed = confirm_send({"kind": "exclude-go", "name": "hinge_left",
                           "names": ["hinge_left", "hinge_right"],
                           "filename": "plate.gcode"}, boom, _live())
    assert failed["sent"] is False
    assert "not cancelled" in failed["text"]
    assert "Exclude sent" not in failed["text"]


_GO = {"kind": "exclude-go", "name": "B", "names": ["A", "B"], "filename": "plate.gcode"}


def test_stale_confirm_sends_nothing_when_the_print_changed():
    for live in (_live(filename="other.gcode"), _live(state="standby"), _live(state="complete"),
                 lambda: (_ for _ in ()).throw(OSError("down")), None):
        sent_to = []
        out = confirm_send(dict(_GO), sent_to.append, live)
        assert out["sent"] is False and sent_to == []
        assert "Nothing was sent" in out["text"] or "Nothing" in out["text"]
    sent_to = []
    assert confirm_send(dict(_GO), sent_to.append, _live(state="paused"))["sent"] is True
    assert sent_to == ["EXCLUDE_OBJECT NAME=B"]


def test_last_object_guard_counts_what_klipper_already_excluded():
    alert = {"kind": "exclude-pick", "filename": "plate.gcode", "names": ["A", "B"]}
    assert prepare(alert, 1, _live())["step"] == "confirm"
    assert prepare(alert, 1, _live(excluded=["A"]))["step"] == "cancel_print"
    assert prepare(alert, 0, _live(excluded=["A"]))["step"] == "refuse"
    assert prepare(alert, 1)["step"] == "refuse"
    assert prepare(alert, 1, _live(filename="other.gcode"))["step"] == "refuse"
    sent_to = []
    out = confirm_send(dict(_GO), sent_to.append, _live(excluded=["A"]))
    assert out["sent"] is False and sent_to == []
    ok = confirm_send({**_GO, "names": ["A", "B", "C"]}, sent_to.append, _live(excluded=["A"]))
    assert ok["sent"] is True and "Still printing: C." in ok["text"]


def test_klipper_uppercased_excluded_names_still_count():
    # Klipper stores and reports object names uppercased; Orca writes them lowercase.
    names = ["cube_id_0_copy_0", "cube_id_1_copy_0"]
    alert = {"kind": "exclude-pick", "filename": "plate.gcode", "names": names}
    live = _live(excluded=["CUBE_ID_0_COPY_0"])
    assert prepare(alert, 1, live)["step"] == "cancel_print"
    assert prepare(alert, 0, live)["step"] == "refuse"


def test_read_live_fails_closed_without_excluded_list(monkeypatch):
    from print_handy.runtime import exclude_chat, print_stop
    monkeypatch.setattr(print_stop, "status", lambda pid=None: {"state": "printing", "filename": "p.gcode"})
    monkeypatch.setattr(print_stop, "_send", lambda *a, **k: {"result": {"status": {"exclude_object": {
        "excluded_objects": ["A"]}}}})
    assert exclude_chat.read_live()["excluded"] == ["A"]
    monkeypatch.setattr(print_stop, "_send", lambda *a, **k: {"result": {"status": {}}})
    import pytest
    with pytest.raises(RuntimeError):
        exclude_chat.read_live()


def test_watchdog_check_does_not_exclude_or_stop():
    path = Path(__file__).resolve().parents[2] / "print_handy/runtime/moonraker_event_listener.py"
    text = path.read_text(encoding="utf-8")
    start = text.index("    async def visual_check")
    end = text.index("    async def on_status", start)
    body = text[start:end]
    assert "exclude_object" not in body
    assert "EXCLUDE_OBJECT" not in body
    assert "print/cancel" not in body
    assert "print_stop" not in body


def _exclude_click(monkeypatch, tmp_path, custom_id, values=None):
    """The real plugin button handler with a fake Discord that records call order."""
    import asyncio
    from print_handy.runtime import exclude_chat, print_alert_buttons as pab
    from tests.hermes.test_print_next import Interaction
    from tests.hermes.test_stop_button_view import _install
    monkeypatch.setattr(pab, 'STATE', tmp_path / 'alerts.json')
    _sent, on_interaction, _plugin = _install(monkeypatch)
    import sys
    sys.modules['discord'].ButtonStyle.secondary = 2
    order = []
    click = Interaction(custom_id, values)

    async def defer(**_kw):
        order.append('defer')

    async def edit_original_response(content=None, view=None, **_kw):
        order.append(('edit', content))
    click.response.defer = defer
    click.edit_original_response = edit_original_response

    def live(printer_id=None):
        order.append('live')
        return {"state": "printing", "filename": "plate.gcode", "excluded": []}
    monkeypatch.setattr(exclude_chat, 'read_live', live)
    return pab, click, order, lambda: asyncio.run(on_interaction(click))


def test_stale_picker_answers_without_crashing(monkeypatch, tmp_path):
    _pab, click, order, run = _exclude_click(monkeypatch, tmp_path, "pbexclude:pick:missing", ["0"])
    run()
    assert click.out == [('send', 'That object list is old or was already used. Nothing sent.')]
    assert order == []  # no defer and no printer read for a stale picker


def test_picker_acknowledges_before_reading_the_printer(monkeypatch, tmp_path):
    pab, click, order, run = _exclude_click(monkeypatch, tmp_path, "", ["1"])
    aid = pab.record("exclude-pick", "plate.gcode", names=["A", "B", "C"])
    click.data["custom_id"] = f"pbexclude:pick:{aid}"
    run()
    assert order[0] == 'defer' and order[1] == 'live'
    assert order[-1][0] == 'edit' and "Cancel B" in order[-1][1]
