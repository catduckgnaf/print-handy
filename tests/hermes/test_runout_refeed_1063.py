"""Runout recovery follow-up (#1063): identity, refeed, echo, no predicted IDs.

Offline throughout: the Discord answer path runs against mock or fake
Spoolman/Moonraker seams, the lane watcher against in-memory fakes, and
the full chain (runout -> new spool -> second feed failure -> same-spool
retry) against the real spool_sync functions bound to a stateful fake
Spoolman. No printer is touched.
"""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.runtime import lane_data_sync
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import spool_sync

SPEC = importlib.util.spec_from_file_location(
    "print_feedback_1063", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

RUNOUT = {"filename": "a.gcode", "slot": 3, "prior_spool_id": 20, "cause": "runout",
          "recorded_at": 1.0, "usage_suspended": True, "suspended_spool_id": 20}


# ------------------------------------------------------------ question text

def test_discord_question_text_has_no_command_or_format_help():
    text = mel.spool_question_text(dict(RUNOUT), buttons=True)
    assert "/spool" not in text
    assert "brand, material, color, grams" not in text
    assert "`" not in text  # no typed recipes at all on the button surface


def test_discord_question_text_distinguishes_new_roll_from_refeed():
    text = mel.spool_question_text(dict(RUNOUT), buttons=True)
    assert "New roll, same filament" in text
    assert "Same spool — fix feed or jam" in text
    assert "#20 is marked depleted" in text          # the new-roll fate is stated
    assert "not marked depleted" in text             # the refeed fate is stated
    assert "remaining weight is unchanged" in text   # the jam branch keeps the record whole
    assert "Existing spool" in text and "New spool" in text
    assert "Cancel print" in text


def test_discord_question_text_separates_inventory_feed_and_resume():
    """#1063 jam follow-up: the question itself says answering settles only
    the records, never resumes, does not prove the path is clear, and
    names that Print Handy cannot drive a paused-print load/unload —
    the unsupported-manual-recovery explanation, not a silent funnel
    back into new-spool creation."""
    text = mel.spool_question_text(dict(RUNOUT), buttons=True)
    assert "never resumes the print" in text
    assert "does not mean the feed path is clear" in text
    assert "cannot load or unload filament while the print is paused" in text
    assert "does not prove the roll is empty" in text


def test_discord_question_text_for_swap_keeps_true_same_spool():
    text = mel.spool_question_text({**RUNOUT, "cause": "swap"}, buttons=True)
    assert "Same spool" in text
    assert "refeed" not in text.casefold()
    assert "/spool" not in text


def test_plain_question_text_drops_slash_command_line_and_names_refeed():
    text = mel.spool_question_text(dict(RUNOUT))
    assert "/spool list" not in text
    assert "`refeed`" in text and "`fix feed`" in text and "#20" in text
    assert "never resumes the print" in text
    assert "same filament" in text  # the fresh-roll typed answer is explained too


# ------------------------------------------------------------ buttons / parse

def test_runout_question_buttons_offer_refeed_beside_new_roll():
    labels = [b["label"] for b in pab.spool_question_components("x", 20, "runout")[0]["components"]]
    assert labels == ["New roll, same filament", "Same spool — fix feed or jam",
                      "Existing spool", "New spool", "Cancel print"]
    parsed = pab.parse_spool_question_custom_id("pbspool:refeed:abc")
    assert parsed == ("refeed", "abc")


def test_swap_question_buttons_have_no_refeed():
    labels = [b["label"] for b in pab.spool_question_components("x", 20, "swap")[0]["components"]]
    assert labels == ["Same spool", "Existing spool", "New spool", "Cancel print"]


def test_parse_runout_reply_refeed_and_same_filament():
    assert mel.parse_runout_reply("refeed") == {"status": "refeed"}
    assert mel.parse_runout_reply("Retry feed") == {"status": "refeed"}
    assert mel.parse_runout_reply("fix feed") == {"status": "refeed"}
    assert mel.parse_runout_reply("Fix feed or jam") == {"status": "refeed"}
    assert mel.parse_runout_reply("jam") == {"status": "refeed"}
    assert mel.parse_runout_reply("same filament") == {"status": "same"}
    assert mel.parse_runout_reply("same") == {"status": "same"}


def test_apply_runout_choice_refeed_writes_nothing():
    calls = []

    def client(method, path, body=None):
        calls.append((method, path))
        return {}

    result = mel.apply_runout_choice(None, {"slot": 3, "prior_spool_id": 21},
                                     {"status": "refeed"}, client=client)
    assert result["action"] == "refeed" and result["spool_id"] == 21
    assert calls == []  # no creation, no bind, no depletion patch


# ------------------------------------------------------------ the answer path

def _listener(record):
    return S(load_runout_pause=lambda: record, clear_runout_pause=mock.Mock())


def _sync(**overrides):
    base = dict(SpoolError=spool_sync.SpoolError,
                fresh_spool_like=mock.Mock(return_value={"spool_id": 31, "filament_id": 13, "grams": 1000.0}),
                bind_slot=mock.Mock(return_value={"slot": 3, "spool_id": 31, "prior_spool_id": 20}),
                bind_text=lambda r: "Spool #31 is in slot 3.",
                resume_usage=mock.Mock())
    base.update(overrides)
    return S(**base)


def test_refeed_answer_creates_binds_and_depletes_nothing():
    record = {**RUNOUT, "prior_spool_id": 21, "suspended_spool_id": 21}
    lis, sync = _listener(dict(record)), _sync()
    text = plugin._answer_spool_question(lis, sync, dict(record), {"status": "refeed"})
    sync.fresh_spool_like.assert_not_called()
    sync.bind_slot.assert_not_called()
    sync.resume_usage.assert_called_once_with(21)
    lis.clear_runout_pause.assert_called_once()
    assert "same physical spool #21" in text
    assert "not marked depleted" in text and "No new spool was created" in text
    assert "remaining weight record is unchanged" in text
    # Inventory, feed state and resume stay separate, and the unsupported
    # in-bot recovery is named rather than funneled into a replacement.
    assert "does not mean the feed path is clear" in text
    assert "does not resume the print" in text
    assert "cannot load or unload filament for you while the print is paused" in text


def test_refeed_answer_without_a_known_spool_refuses():
    record = {**RUNOUT, "prior_spool_id": None, "suspended_spool_id": None}
    lis, sync = _listener(dict(record)), _sync()
    with pytest.raises(spool_sync.SpoolError):
        plugin._answer_spool_question(lis, sync, dict(record), {"status": "refeed"})
    sync.resume_usage.assert_not_called()


def test_screen_feed_of_the_ran_out_spool_is_a_refeed(tmp_path, monkeypatch):
    """feed_screen_runout_load: the screen showing the prior spool back in the
    slot answers the question as a refeed (#1063), not a refusal."""
    state = tmp_path / "runout_pause.json"
    state.write_text(json.dumps(RUNOUT))
    monkeypatch.setattr(mel, "RUNOUT_PAUSE_STATE", state)
    resumed = []
    monkeypatch.setattr(spool_sync, "resume_usage", lambda sid, mr=None: resumed.append(sid))
    bound = mock.Mock(side_effect=AssertionError("refeed must not bind"))
    monkeypatch.setattr(spool_sync, "bind_slot", bound)
    assert mel.feed_screen_runout_load(3, 20, dict(RUNOUT), client=mock.Mock(), mr=mock.Mock()) is True
    assert resumed == [20]
    assert not state.exists()  # the question is closed


# ------------------------------------------------------------ lane watcher echo

PID = "ad5x"


class FakeMoonraker:
    def __init__(self, lanes=None, state="paused"):
        self.lanes = lanes
        self.state = state
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.startswith("/server/database/item"):
            if method == "GET":
                return {"result": {"value": copy.deepcopy(self.lanes)} if self.lanes is not None else {}}
            if method == "POST":
                self.lanes[body["key"]] = copy.deepcopy(body["value"])
                return {"result": {"value": copy.deepcopy(body["value"])}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": self.state}}}}
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                return {}
            return {"result": {"spool_id": None}}
        return {"result": "ok"}


class FakeSpoolman:
    def __init__(self, spools=()):
        self.spools = {int(s["id"]): copy.deepcopy(s) for s in spools}
        self.fields = []
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/api/v1/field/spool":
            return copy.deepcopy(self.fields)
        if method == "POST" and path == "/api/v1/field/spool/ph_slot":
            self.fields.append({"key": "ph_slot", **(body or {})})
            return {}
        if method == "GET" and path == "/api/v1/spool?allow_archived=true":
            return [copy.deepcopy(s) for s in self.spools.values()]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            row = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    row.setdefault("extra", {}).update(value)
                else:
                    row[key] = value
            return {}
        return {}


def _spool(sid, ph_slot=None, remaining=800):
    row = {"id": sid, "location": "Shelf", "archived": False, "remaining_weight": remaining,
           "filament": {"name": "Black", "material": "PLA+", "color_hex": "161616",
                        "vendor": {"id": 2, "name": "eSun"}}}
    if ph_slot is not None:
        row["extra"] = {"ph_slot": json.dumps(ph_slot)}
    return row


def _lane(spool_id, *, slot=3, scan_time=100):
    return {"spool_id": spool_id, "lane": slot - 1, "scan_time": scan_time,
            "color": "161616", "material": "PLA+", "name": "Black PLA+",
            "helix_locked_color": False, "helix_locked_material": False}


def _watcher(tmp_path, mr, client):
    applied = []
    watcher = lane_data_sync.LaneDataWatcher(
        tmp_path / "lane_watch.json", mr=mr, client=client, printer_id=PID,
        printer_state_fn=lambda _mr: mr.state,
        apply_fn=lambda slot, sid: applied.append((slot, sid)),
        runout_context_fn=lambda: None)
    return watcher, applied


def test_watcher_adopts_print_handys_own_bind_silently(tmp_path):
    """The lane record Print Handy wrote for its own Discord bind (the spool
    already claims the slot through ph_slot) is an echo: no announcement,
    no mid-print note, no re-apply — even while the print is still paused."""
    mr = FakeMoonraker({"lane3": _lane(20)}, state="paused")
    client = FakeSpoolman([_spool(20, ph_slot="ad5x:3"), _spool(21, ph_slot="ad5x:3")])
    watcher, applied = _watcher(tmp_path, mr, client)
    assert watcher.poll() == []  # seed
    mr.lanes = {"lane3": _lane(21, scan_time=200)}
    assert watcher.poll() == []
    assert watcher.poll() == []
    assert applied == []


def test_watcher_still_notes_a_genuine_screen_change_mid_print(tmp_path):
    mr = FakeMoonraker({"lane3": _lane(20)}, state="paused")
    client = FakeSpoolman([_spool(20, ph_slot="ad5x:3"), _spool(21)])
    watcher, applied = _watcher(tmp_path, mr, client)
    assert watcher.poll() == []
    mr.lanes = {"lane3": _lane(21, scan_time=200)}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["noted_mid_print"]
    assert applied == []


# ------------------------------------------------------------ no predicted IDs

def test_intake_texts_never_name_a_number_before_creation():
    preview = pab.intake_preview_text(2)
    assert "#" not in preview and "Nothing is created until you confirm" in preview
    state = {"step": "confirm", "slot": 1, "predicted_id": 18, "brand": None,
             "material": "Silk PLA", "color": "Rose Red", "grams": 500,
             "old_spool_id": 13, "disposition": "shelf"}
    confirm = pab.intake_step_text(state)  # even a stale predicted id is never shown
    assert "#18" not in confirm and "a new spool" in confirm


# ------------------------------------------------------------ the full chain

class ChainSpoolman:
    """Stateful fake Spoolman for the real spool_sync functions."""

    def __init__(self, spools):
        self.spools = {int(s["id"]): copy.deepcopy(s) for s in spools}
        self.fields = []
        self.posts = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/field/spool":
            return copy.deepcopy(self.fields)
        if method == "POST" and path == "/api/v1/field/spool/ph_slot":
            self.fields.append({"key": "ph_slot", **(body or {})})
            return {}
        if method == "GET" and path.startswith("/api/v1/spool?"):
            archived = "allow_archived=true" in path
            return [copy.deepcopy(s) for s in self.spools.values() if archived or not s["archived"]]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])
        if method == "POST" and path == "/api/v1/spool":
            self.posts.append(copy.deepcopy(body))
            new_id = max(self.spools) + 1
            filament = {"id": body["filament_id"], "name": "Black", "material": "PLA+",
                        "color_hex": "161616", "weight": body["initial_weight"]}
            self.spools[new_id] = {"id": new_id, "archived": False, **copy.deepcopy(body),
                                   "filament": filament}
            return {"id": new_id}
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            row = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    row.setdefault("extra", {}).update(value)
                else:
                    row[key] = value
            return {}
        return {}


class ChainMoonraker:
    def __init__(self):
        self.active = [None]
        self.lanes = {}

    def __call__(self, method, path, body=None):
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                self.active[0] = (body or {}).get("spool_id")
            return {"result": {"spool_id": self.active[0]}}
        if path.startswith("/server/database/item"):
            if method == "GET":
                return {"result": {"value": copy.deepcopy(self.lanes)} if self.lanes else {}}
            if method == "POST":
                self.lanes[body["key"]] = copy.deepcopy(body["value"])
                return {"result": {"value": copy.deepcopy(body["value"])}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {}}}
        return {"result": "ok"}


def _chain_spool(sid, location, remaining):
    return {"id": sid, "initial_weight": 1000, "remaining_weight": remaining,
            "location": location, "archived": False,
            "filament": {"id": 13, "name": "Black", "material": "PLA+", "color_hex": "161616",
                         "weight": 1000}}


def test_chain_runout_new_spool_second_failure_refeed(tmp_path):
    """runout -> new spool (same filament) -> jam pause -> same-spool fix.

    The state machine the #1063 jam follow-up requires: after the
    replacement, a repeated sensor event whose physical cause is a stuck
    feed is answered with the same-spool fix — twice — asserting exactly
    one spool is ever created, the replacement is never depleted or
    duplicated by a fix, its weight record is untouched, usage resumes on
    it, and a genuine second replacement still works afterwards."""
    hw = tmp_path / "printer_hardware.json"
    hw.write_text(json.dumps({
        "spool": {"spoolman_id": 20, "name": "Black", "material": "PLA+"},
        "filament_slots": {"3": {"spoolman_id": 20, "name": "Black PLA+", "material": "PLA+",
                                 "color": "Black", "remaining_grams": 0.0}}}))
    client = ChainSpoolman([_chain_spool(20, "IFS Slot 3", 0), _chain_spool(19, "Shelf", 900)])
    mr = ChainMoonraker()
    mr.active[0] = 20
    sync = S(SpoolError=spool_sync.SpoolError,
             fresh_spool_like=lambda sid: spool_sync.fresh_spool_like(sid, client=client),
             bind_slot=lambda slot, sid, **kw: spool_sync.bind_slot(
                 slot, sid, client=client, mr=mr, state_path=hw, **kw),
             bind_text=spool_sync.bind_text,
             resume_usage=lambda sid: spool_sync.resume_usage(sid, mr=mr),
             load_state=lambda: spool_sync.load_state(hw),
             create_spool=lambda spec: spool_sync.create_spool(spec, client=client),
             screen_hex=spool_sync.screen_hex)
    holder = {"record": dict(RUNOUT), "clears": 0}

    def clear():
        holder["record"] = None
        holder["clears"] += 1

    listener = S(load_runout_pause=lambda: holder["record"], clear_runout_pause=clear)

    # 1. The runout answer "same filament": one new roll, the old one depleted.
    text = plugin._answer_spool_question(listener, sync, dict(RUNOUT), {"status": "same"})
    assert "Created spool #21" in text
    assert len(client.posts) == 1
    assert client.spools[20]["archived"] is True and client.spools[20]["remaining_weight"] == 0
    assert json.loads(hw.read_text())["filament_slots"]["3"]["spoolman_id"] == 21
    assert mr.active[0] == 21

    # 2. A second sensor event opens a new question against #21. The roll
    # is not exhausted — the owner found the filament stuck (the #1063 jam
    # follow-up's shape). The fix-feed answer keeps #21 whole: no second
    # creation, no depletion, weight record untouched.
    second = {**RUNOUT, "prior_spool_id": 21, "suspended_spool_id": 21}
    holder["record"] = dict(second)
    text = plugin._answer_spool_question(listener, sync, dict(second), {"status": "refeed"})
    assert "same physical spool #21" in text
    assert len(client.posts) == 1, "refeed must not create another spool"
    assert client.spools[21]["archived"] is False
    assert client.spools[21]["remaining_weight"] == 1000, "refeed must not deplete the new roll"
    assert json.loads(hw.read_text())["filament_slots"]["3"]["spoolman_id"] == 21
    assert mr.active[0] == 21

    # 2b. The feed fails again / the sensor trips again: the same-spool
    # retry stays available and the invariants hold a second time.
    holder["record"] = dict(second)
    plugin._answer_spool_question(listener, sync, dict(second), {"status": "refeed"})
    assert len(client.posts) == 1, "a retry must not create another spool either"
    assert client.spools[21]["archived"] is False
    assert client.spools[21]["remaining_weight"] == 1000
    assert json.loads(hw.read_text())["filament_slots"]["3"]["spoolman_id"] == 21
    assert mr.active[0] == 21

    # 3. A genuine second replacement remains available.
    holder["record"] = dict(second)
    plugin._answer_spool_question(listener, sync, dict(second),
                                  {"status": "existing", "spool_id": 19})
    assert json.loads(hw.read_text())["filament_slots"]["3"]["spoolman_id"] == 19
    assert client.spools[21]["archived"] is True  # depleted by the real replacement bind
    assert len(client.posts) == 1
