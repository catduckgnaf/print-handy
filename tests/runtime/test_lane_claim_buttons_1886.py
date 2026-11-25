"""The claimed_elsewhere notice has a chat repair path (#1886).

Live case, 2026-11-23 and 11-24: the screen's lane 1 named spool 19, while Print
Handy and Spoolman had spool 16 in slot 1 and spool 19 in slot 2. The notice was
text only, suggested `/spool change 1 19` (siding with the screen), and posted again
after every HelixScreen restart. Print Handy's record wins until the operator says
otherwise (#1816), so the notice now leads with keeping it, names `/spool sync`, offers
Keep / Move / Pick another as buttons on Discord, and posts once per problem.
Fakes only: no network, no printer.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

from print_handy.runtime import lane_data_sync
from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import spool_sync
from tests.runtime.test_issue_792_picker_hardening import _custom_ids, _interaction
from tests.handler_namespace import plugin
from tests.runtime.test_lane_data_sync import PID, FakeMoonraker, FakeSpoolman, lane, make_watcher, seed, spool


def _live(tmp_path):
    """Spool 16 claims slot 1 and spool 19 slot 2; the screen's lane 1 names 19."""
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(16, "White", "PLA", "FFFFFF", ph_slot=f"{PID}:1"),
                           spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2")])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(16, slot=1, color="FFFFFF", name="White PLA"),
                       "lane2": lane(19, slot=2, material="PLA+", color="CD7F32")})
    mr.lanes = {"lane1": lane(19, slot=1, material="PLA+", color="CD7F32", scan_time=200),
                "lane2": lane(19, slot=2, material="PLA+", color="CD7F32")}
    return mr, client, watcher, applied


def test_the_notice_keeps_the_record_and_names_spool_sync(tmp_path):
    _mr, _client, watcher, applied = _live(tmp_path)
    events = watcher.poll()
    assert [(e["kind"], e["slot"], e["spool_id"]) for e in events] == [("claimed_elsewhere", 1, 19)]
    event = events[0]
    assert event["other_slot"] == 2 and event["recorded_spool_id"] == 16
    text = event["text"]
    assert "Print Handy has spool #16 in slot 1" in text and "spool #19 in slot 2" in text
    assert "Print Handy's record is kept" in text
    assert "To keep spool #16 and fix the screen lane, run `/spool sync`." in text
    assert "`/spool shelve 2`, then `/spool change 1 19`" in text
    assert applied == []


def test_the_same_unresolved_notice_is_not_posted_after_every_screen_restart(tmp_path):
    mr, _client, watcher, applied = _live(tmp_path)
    first = watcher.poll()
    watcher.delivered(first[0]["key"])
    # Each HelixScreen restart writes the record back, here in a new color spelling
    # (the 1.0.11 to 1.1.0 update), which used to read as a new change.
    for n, color in enumerate(("#CD7F32", "cd7f33", "CD7F32", "#cd7f31")):
        mr.lanes["lane1"] = lane(19, slot=1, material="PLA+", color=color, scan_time=300 + n)
        assert watcher.poll() == []
    # A listener restart reads the memory back from the state file.
    again = lane_data_sync.LaneDataWatcher(
        tmp_path / "lane_watch.json", mr=mr, client=_client, printer_id=PID,
        printer_state_fn=lambda _mr: "standby", apply_fn=lambda *_a: None)
    mr.lanes["lane1"] = lane(19, slot=1, material="PLA", color="CD7F30", scan_time=400)
    assert again.poll() == []
    assert applied == []


def test_a_different_claim_on_the_slot_is_posted(tmp_path):
    mr, client, watcher, _applied = _live(tmp_path)
    watcher.delivered(watcher.poll()[0]["key"])
    client.spools[21] = spool(21, "Black", "PLA", "161616", ph_slot=f"{PID}:3")
    mr.lanes["lane3"] = lane(21, slot=3, color="161616")
    watcher.poll()  # lane 3 appears: its own spool, nothing to say about slot 1
    mr.lanes["lane1"] = lane(21, slot=1, color="161616", scan_time=500)
    events = [e for e in watcher.poll() if e["kind"] == "claimed_elsewhere"]
    assert [(e["slot"], e["spool_id"], e["other_slot"]) for e in events] == [(1, 21, 3)]


# ------------------------------------------------------------------ the post

def _event(tmp_path):
    _mr, _client, watcher, _applied = _live(tmp_path)
    return watcher.poll()[0]


def _post(event, *, buttons_ok=True, buttonless=None):
    with mock.patch.object(listener, "_buttonless_standalone", return_value=buttonless), \
            mock.patch.object(listener, "_bot_token", return_value="token"), \
            mock.patch.object(pab, "post_insertion_confirmation", return_value=buttons_ok) as buttons, \
            mock.patch.object(listener, "post", return_value=True) as text:
        assert listener.post_lane_event(event) is True
    return buttons, text


def test_the_notice_posts_its_three_answers_on_discord(tmp_path):
    event = _event(tmp_path)
    buttons, text = _post(event)
    text.assert_not_called()
    _token, _channel, content, slot = buttons.call_args.args
    assert slot == 1
    assert buttons.call_args.kwargs["lane_claim"] == {"spool_id": 19, "other_slot": 2,
                                                      "recorded_spool_id": 16,
                                                      "target_empty": False}
    assert content.startswith(event["text"])
    assert "**Keep spool #16** keeps Print Handy's record" in content
    assert "**Move spool #19 here** takes it out of slot 2 into slot 1" in content


def test_a_failed_button_post_or_a_buttonless_platform_gets_the_text(tmp_path):
    event = _event(tmp_path)
    buttons, text = _post(event, buttons_ok=False)
    buttons.assert_called_once()
    text.assert_called_once_with(event["text"])
    buttons, text = _post(event, buttonless="telegram")
    buttons.assert_not_called()
    text.assert_called_once_with(event["text"])


def test_the_buttons_lead_with_keep():
    components = pab.lane_claim_components("a1", {"spool_id": 19, "other_slot": 2,
                                                  "recorded_spool_id": 16})[0]["components"]
    assert [b["label"] for b in components] == ["Keep spool #16", "Move spool #19 here", "Pick another"]
    assert [pab.parse_insertion_custom_id(b["custom_id"]) for b in components] == [
        ("keep", "a1"), ("move", "a1"), ("existing", "a1")]
    assert components[0]["style"] == 1  # the primary answer
    # No recorded spool: nothing to keep, so no Keep button.
    bare = pab.lane_claim_components("a2", {"spool_id": 19, "other_slot": 2,
                                            "recorded_spool_id": None})[0]["components"]
    assert [b["label"] for b in bare] == ["Move spool #19 here", "Pick another"]


# ------------------------------------------------------------------ the clicks

class _ShelfSpoolman(FakeSpoolman):
    """Also answers the live-only list the shelf picker reads."""

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/spool?allow_archived=false":
            return [dict(s) for s in self.spools.values() if not s.get("archived")]
        return super().__call__(method, path, body)


def _rig(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    client = _ShelfSpoolman([spool(16, "White", "PLA", "FFFFFF", ph_slot=f"{PID}:1"),
                             spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2"),
                             spool(25, "Gray", "PETG", "808080")])
    mr = FakeMoonraker(lanes={"lane1": lane(19, slot=1, material="PLA+", color="CD7F32"),
                              "lane2": lane(19, slot=2, material="PLA+", color="CD7F32")})
    monkeypatch.setattr(spool_sync, "spoolman", client)
    monkeypatch.setattr(spool_sync, "moonraker", mr)
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {"filament_slots": {
        "1": {"spoolman_id": 16}, "2": {"spoolman_id": 19}}})
    moves = []
    monkeypatch.setattr(spool_sync, "move_spool", lambda *a, **k: moves.append((a, k)) or {
        "from_slot": a[0], "to_slot": a[1], "spool_id": a[2], "bound": {"prior_spool_id": 16}})
    bot = type("Bot", (), {})()
    bot.listeners = {}
    bot.add_listener = lambda fn, name: bot.listeners.__setitem__(name, fn)
    plugin.alert_button_handlers(bot, SimpleNamespace())
    aid = pab.record("insertion", "", slot=1,
                     lane_claim={"spool_id": 19, "other_slot": 2, "recorded_spool_id": 16})
    return bot.listeners["on_interaction"], aid, mr, moves


def test_keep_rewrites_the_screen_lane_to_the_recorded_spool(tmp_path, monkeypatch):
    on_interaction, aid, mr, moves = _rig(tmp_path, monkeypatch)
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:keep:{aid}")))
    assert mr.lanes["lane1"]["spool_id"] == 16
    assert mr.lanes["lane2"]["spool_id"] == 19  # only the one lane key is written
    assert mr.gcode_scripts() == []  # no printer command
    assert sink[-1][1].startswith("Slot 1: kept spool #16, Print Handy's record.")
    assert moves == []


def test_keep_on_a_slot_that_moved_on_writes_nothing(tmp_path, monkeypatch):
    on_interaction, aid, mr, _moves = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **k: {"filament_slots": {
        "1": {"spoolman_id": 23}}})
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:keep:{aid}")))
    assert mr.lanes["lane1"]["spool_id"] == 19
    assert "now holds spool #23, not spool #16" in sink[-1][1]


def test_move_asks_once_then_moves_the_claimed_spool(tmp_path, monkeypatch):
    on_interaction, aid, _mr, moves = _rig(tmp_path, monkeypatch)
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:move:{aid}")))
    _kind, content, view = sink[-1]
    assert content == ("Move spool #19 from slot 2 to slot 1? Slot 2 is left empty. Spool #16 goes "
                       "back on the shelf. Nothing changes until you confirm.")
    assert moves == []  # the question alone changes nothing
    confirm = next(cid for cid in _custom_ids(view) if cid.startswith("pbbindconfirm:confirm:"))
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    assert moves == [((2, 1, 19), {"expected_prior_spool_id": 16})]
    assert sink[-1][1].startswith("Moved spool #19 from slot 2 to slot 1; slot 2 is now empty.")


def test_pick_another_opens_the_shelf_picker(tmp_path, monkeypatch):
    on_interaction, aid, _mr, _moves = _rig(tmp_path, monkeypatch)
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:existing:{aid}")))
    assert any(cid.startswith("pbinsertselect:") for cid in _custom_ids(sink[-1][2]))


def _real_move_rig(tmp_path, monkeypatch, lane_claim, slots):
    """The Move click and confirmation through the router, with the real move_spool."""
    on_interaction, _aid, _mr, _moves = _rig(tmp_path, monkeypatch)
    monkeypatch.setattr(spool_sync, "_default_printer_id", lambda: PID)
    client = FakeSpoolman([spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2"),
                           spool(30, "Black", "PLA", "000000", ph_slot=f"{PID}:1")])
    client.spools[30]["location"] = "IFS Slot 1"
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": slots}))
    real_move = _REAL_MOVE_SPOOL
    monkeypatch.setattr(spool_sync, "load_state", _REAL_LOAD_STATE)
    monkeypatch.setattr(spool_sync, "move_spool", lambda *a, **k: real_move(
        *a, client=client, mr=FakeMoonraker(lanes={}), state_path=state, **k))
    aid = pab.record("insertion", "", slot=1, lane_claim=lane_claim)
    return on_interaction, aid, client, state


_REAL_MOVE_SPOOL = spool_sync.move_spool
_REAL_LOAD_STATE = spool_sync.load_state


def test_move_refuses_when_an_empty_target_slot_gained_a_spool(tmp_path, monkeypatch):
    # The notice saw slot 1 empty; spool 30 was recorded there before the confirm.
    claim = {"spool_id": 19, "other_slot": 2, "recorded_spool_id": None, "target_empty": True}
    slots = {"1": {"slot": 1, "spoolman_id": 30, "material": "PLA", "hex": "000000", "name": "Black PLA"},
             "2": {"slot": 2, "spoolman_id": 19, "material": "PLA+", "hex": "CD7F32", "name": "Bronze PLA+"}}
    on_interaction, aid, client, state = _real_move_rig(tmp_path, monkeypatch, claim, slots)
    before = state.read_text()
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:move:{aid}")))
    question = sink[-1][1]
    confirm = next(cid for cid in _custom_ids(sink[-1][2]) if cid.startswith("pbbindconfirm:confirm:"))
    sink.clear()
    asyncio.run(on_interaction(_interaction(sink, confirm)))
    assert client.spools[30]["location"] == "IFS Slot 1"  # not sent to the shelf
    assert not [c for c in client.calls if c[0] in ("PATCH", "DELETE")]
    assert state.read_text() == before
    assert "slot 1 now holds spool #30" in sink[-1][1] and "Nothing changed" in sink[-1][1]
    assert "Slot 1 is empty now" in question


def test_move_is_refused_when_the_notice_could_not_name_the_target_slot(tmp_path, monkeypatch):
    # Two spools claimed slot 1 (or Spoolman was silent): nothing recorded, not known empty.
    claim = {"spool_id": 19, "other_slot": 2, "recorded_spool_id": None}
    slots = {"1": {"slot": 1, "spoolman_id": 30, "material": "PLA", "hex": "000000", "name": "Black PLA"},
             "2": {"slot": 2, "spoolman_id": 19, "material": "PLA+", "hex": "CD7F32", "name": "Bronze PLA+"}}
    on_interaction, aid, client, state = _real_move_rig(tmp_path, monkeypatch, claim, slots)
    sink = []
    asyncio.run(on_interaction(_interaction(sink, f"pbinsert:move:{aid}")))
    assert sink[-1][1].startswith("Print Handy could not tell what slot 1 holds")
    assert not [cid for cid in _custom_ids(sink[-1][2]) if cid.startswith("pbbindconfirm:")]
    assert not [c for c in client.calls if c[0] in ("PATCH", "DELETE")]


def test_the_notice_says_when_the_target_slot_is_empty(tmp_path):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2")])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(19, slot=2, material="PLA+", color="CD7F32")})
    mr.lanes = {"lane1": lane(19, slot=1, material="PLA+", color="CD7F32", scan_time=200),
                "lane2": lane(19, slot=2, material="PLA+", color="CD7F32")}
    events = [e for e in watcher.poll() if e["kind"] == "claimed_elsewhere"]
    assert events and events[0]["recorded_spool_id"] is None and events[0]["target_empty"] is True


def test_the_notice_does_not_call_an_ambiguous_slot_empty(tmp_path):
    # Spools 16 and 17 both claim slot 1: nothing unique is recorded, and the slot is not empty.
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(16, ph_slot=f"{PID}:1"), spool(17, ph_slot=f"{PID}:1"),
                           spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2")])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(19, slot=2, material="PLA+", color="CD7F32")})
    mr.lanes = {"lane1": lane(19, slot=1, material="PLA+", color="CD7F32", scan_time=200),
                "lane2": lane(19, slot=2, material="PLA+", color="CD7F32")}
    events = [e for e in watcher.poll() if e["kind"] == "claimed_elsewhere"]
    assert events and events[0]["recorded_spool_id"] is None and not events[0]["target_empty"]
    text = events[0]["text"]
    assert "nothing in" not in text
    assert "could not tell what" in text and "`/spool sync`" in text


def test_the_text_calls_a_slot_empty_only_when_it_is_known_empty():
    known = lane_data_sync.claimed_elsewhere_text(1, 19, 2, None, target_empty=True)
    unknown = lane_data_sync.claimed_elsewhere_text(1, 19, 2, None)
    assert "nothing in" in known and "could not tell" not in known
    assert "nothing in" not in unknown and "could not tell" in unknown


# ------------------------------------------------------------------ move_spool itself

def _record(tmp_path):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "filament_slots": {
        "1": {"slot": 1, "spoolman_id": 16, "material": "PLA", "hex": "FFFFFF", "name": "White PLA"},
        "2": {"slot": 2, "spoolman_id": 19, "material": "PLA+", "hex": "CD7F32", "name": "Bronze PLA+"}}}))
    return path


def test_move_spool_empties_the_old_slot_and_shelves_the_replaced_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(spool_sync, "_default_printer_id", lambda: PID)
    client = FakeSpoolman([spool(16, "White", "PLA", "FFFFFF", ph_slot=f"{PID}:1"),
                           spool(19, "Bronze", "PLA+", "CD7F32", ph_slot=f"{PID}:2")])
    client.spools[16]["location"], client.spools[19]["location"] = "IFS Slot 1", "IFS Slot 2"
    mr = FakeMoonraker(lanes={})
    state = _record(tmp_path)
    result = spool_sync.move_spool(2, 1, 19, expected_prior_spool_id=16, client=client, mr=mr,
                                   state_path=state)
    slots = json.loads(state.read_text())["filament_slots"]
    assert slots["1"]["spoolman_id"] == 19 and "2" not in slots
    assert client.spools[19]["location"] == "IFS Slot 1"
    assert spool_sync.spool_ph_slot(client.spools[19]) == (PID, 1)
    assert client.spools[16]["location"] == spool_sync.SHELF
    assert spool_sync.spool_ph_slot(client.spools[16]) is None
    assert mr.gcode_scripts() == []
    assert "slot 2 is now empty" in spool_sync.move_text(result)


def test_move_spool_refuses_a_slot_that_changed_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(spool_sync, "_default_printer_id", lambda: PID)
    client = FakeSpoolman([spool(16, ph_slot=f"{PID}:1"), spool(19, ph_slot=f"{PID}:2")])
    state = _record(tmp_path)
    before = state.read_text()
    for args, kw in (((2, 1, 19), {"expected_prior_spool_id": 23}), ((3, 1, 19), {})):
        try:
            spool_sync.move_spool(*args, client=client, mr=FakeMoonraker(lanes={}), state_path=state, **kw)
        except spool_sync.SpoolError as exc:
            assert "Nothing changed" in str(exc)
        else:
            raise AssertionError("move_spool did not refuse")
    assert state.read_text() == before
    assert not [c for c in client.calls if c[0] in ("PATCH", "DELETE")]
