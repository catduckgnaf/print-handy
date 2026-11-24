"""A screen lane relink sticks after HelixScreen puts the old link back (#1885).

2026-11-21 23:08: "Change spool" created spool 23 for slot 3 and deleted spool 21.
Print Handy wrote lane3, sent CHANGE_ZCOLOR and read the link back once. HelixScreen
keeps its own copy of the lane and wrote it back on a later screen command, so lane3
named the deleted spool 21 again; it did so for 44 hours, through two relinks, with
about 13 "needs attention" posts. The fakes below model that screen: every screen
command after the first makes it write its cached lane back. No network, no printer.
"""
import json
import urllib.parse

import pytest

from print_handy.runtime import lane_data_sync, spool_sync
from tests.runtime.test_lane_data_sync import PID, FakeMoonraker, FakeSpoolman, lane, make_watcher, seed, spool


class CachingScreen(FakeMoonraker):
    """A screen that writes its cached lane back on every screen command after the first.

    ``cache`` is what the screen believes lane3 holds. Print Handy's own database write
    does not reach that cache, so a later CHANGE_ZCOLOR or SET_ZCOLOR puts it back.
    """

    def __init__(self, *args, cache=None, spare=1, **kw):
        super().__init__(*args, **kw)
        self.cache = cache
        self.spare = spare  # screen commands answered without a write-back (the bind's own)

    def __call__(self, method, path, body=None):
        res = super().__call__(method, path, body)
        if method == "POST" and path.startswith("/printer/gcode/script") and "ZCOLOR" in path:
            if self.spare > 0:
                self.spare -= 1
            elif self.cache is not None:
                self.lanes["lane3"] = dict(self.cache)
        return res

    def screen_command(self, script="SET_ZCOLOR FILENAME=next.gcode"):
        self("POST", "/printer/gcode/script?script=" + urllib.parse.quote(script), None)


@pytest.fixture
def printer_id(monkeypatch):
    monkeypatch.setattr(spool_sync, "_default_printer_id", lambda: PID)
    return PID


def _state(tmp_path, slot3=21):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "filament_slots": {
        "3": {"slot": 3, "spoolman_id": slot3, "material": "PETG", "hex": "D2B48C"}}}))
    return path


def _change_spool(tmp_path, disposition="delete"):
    """The live replay: slot 3 holds spool 21, "Change spool" binds spool 23 and deletes 21."""
    client = FakeSpoolman([spool(21, "Tan", "PETG", "D2B48C", ph_slot=f"{PID}:3"),
                           spool(23, "Coffee", "PLA", "6F4E37")])
    client.spools[21]["location"] = "IFS Slot 3"
    old_lane = {**lane(21, slot=3, material="PETG", color="D2B48C", name="Tan PETG"),
                "helix_catalog_id": "generic-petg"}
    mr = CachingScreen(lanes={"lane3": dict(old_lane)}, cache=dict(old_lane))
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, mr.lanes)
    result = spool_sync.bind_slot(3, 23, client=client, mr=mr, state_path=_state(tmp_path),
                                  prior_disposition=disposition, expected_prior_spool_id=21)
    return client, mr, watcher, applied, result


def test_a_lane_the_screen_puts_back_after_the_readback_is_relinked(tmp_path, printer_id):
    client, mr, watcher, applied, result = _change_spool(tmp_path)
    assert result["lane_link"]["status"] == "verified"  # the bind's one readback passes
    assert 21 not in client.spools  # nothing named spool 21 at delete time
    assert watcher.poll() == []  # Print Handy's own bind, adopted silently
    mr.screen_command()  # the next print start: the screen writes its cache back
    assert mr.lanes["lane3"]["spool_id"] == 21
    assert watcher.poll() == []  # relinked, not announced
    assert mr.lanes["lane3"]["spool_id"] == 23
    assert mr.lanes["lane3"]["helix_catalog_id"] == "generic-petg"  # screen-owned field kept
    assert watcher.poll() == []  # the repaired record reads as Print Handy's own bind
    for _ in range(3):  # every later screen command, every time
        mr.screen_command("CHANGE_ZCOLOR SLOT=3 TYPE=PLA HEX=6F4E37")
        assert watcher.poll() == []
        assert mr.lanes["lane3"]["spool_id"] == 23
    assert applied == []
    assert spool_sync.spool_ph_slot(client.spools[23]) == (PID, 3)


def test_the_relink_sends_no_printer_command(tmp_path, printer_id):
    _client, mr, watcher, _applied, _result = _change_spool(tmp_path)
    watcher.poll()
    mr.screen_command()
    before = len(mr.gcode_scripts())
    watcher.poll()
    assert len(mr.gcode_scripts()) == before


def test_a_delete_the_screen_lane_still_blocks_archives_instead(tmp_path, printer_id):
    client = FakeSpoolman([spool(21, "Tan", "PETG", "D2B48C", ph_slot=f"{PID}:3"),
                           spool(23, "Coffee", "PLA", "6F4E37")])
    mr = FakeMoonraker(lanes={"lane3": lane(21, slot=3, material="PETG", color="D2B48C")})
    real_call = mr.__call__

    def screen_keeps_21(method, path, body=None):
        res = real_call(method, path, body)
        if method == "POST" and path == "/server/database/item" and body["key"] == "lane3":
            mr.lanes["lane3"]["spool_id"] = 21  # a second writer wins every time
        return res

    result = spool_sync.bind_slot(3, 23, client=client, mr=screen_keeps_21,
                                  state_path=_state(tmp_path), prior_disposition="delete",
                                  expected_prior_spool_id=21)
    assert 21 in client.spools and client.spools[21]["archived"] is True
    assert not [c for c in client.calls if c[0] == "DELETE"]
    assert result["prior_disposition"] == "archive"
    text = spool_sync.bind_text(result)
    assert "archived instead of deleted" in text and "`lane3`" in text and "/spool sync" in text
    assert "was deleted from Spoolman" not in text


def test_a_delete_without_a_lane_naming_the_spool_still_deletes(tmp_path, printer_id):
    client = FakeSpoolman([spool(21, ph_slot=f"{PID}:3"), spool(23, "Coffee", "PLA", "6F4E37")])
    mr = FakeMoonraker(lanes=None)  # no screen publishes lane_data
    result = spool_sync.bind_slot(3, 23, client=client, mr=mr, state_path=_state(tmp_path),
                                  prior_disposition="delete", expected_prior_spool_id=21)
    assert 21 not in client.spools
    assert result["prior_disposition"] == "delete"
    assert "Spool #21 was deleted from Spoolman." in spool_sync.bind_text(result)


def test_a_stale_link_with_no_recorded_spool_is_named_once(tmp_path, printer_id):
    """No live spool claims the slot, so nothing can be relinked: the notice posts once."""
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    events = watcher.poll()
    assert [e["kind"] for e in events] == ["stale_spool"]
    watcher.delivered(events[0]["key"])
    # The screen keeps rewriting the record: a new material string, an echo, the link again.
    for n, material in enumerate(("PLA+", "PLA", "PLA Basic")):
        mr.lanes = {"lane1": lane(99, scan_time=300 + n, material=material)}
        assert watcher.poll() == []
        mr.lanes = {"lane1": lane(None, scan_time=400 + n, material=material)}
        watcher.poll()
    mr.lanes = {"lane1": lane(99, scan_time=500)}
    assert [e for e in watcher.poll() if e["kind"] == "stale_spool"] == []
    assert applied == []


def test_a_different_stale_link_is_still_named(tmp_path, printer_id):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    first = watcher.poll()
    watcher.delivered(first[0]["key"])
    mr.lanes = {"lane1": lane(98, scan_time=300)}
    assert [(e["kind"], e["spool_id"]) for e in watcher.poll()] == [("stale_spool", 98)]


def test_a_locked_stale_lane_is_named_not_rewritten(tmp_path, printer_id):
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(18, "Old", "PLA", "111111", archived=True),
                           spool(19, "White", "PLA", "FFFFFF", ph_slot=f"{PID}:2")])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane2": lane(19, slot=2, color="FFFFFF", name="White PLA")})
    mr.lanes = {"lane2": lane(18, slot=2, color="111111", scan_time=200,
                              locked_color=True, locked_material=True, name="Old PLA")}
    assert [e["kind"] for e in watcher.poll()] == ["stale_spool"]
    assert mr.lanes["lane2"]["spool_id"] == 18
    assert not [c for c in mr.calls if c[0] == "POST" and c[1] == "/server/database/item"]


def test_the_watcher_state_keeps_what_it_already_said(tmp_path, printer_id):
    """A listener restart reads the memory back: the same notice is not posted again."""
    mr = FakeMoonraker()
    client = FakeSpoolman([spool(11)])
    watcher, _applied = make_watcher(tmp_path, mr, client)
    seed(watcher, mr, {"lane1": lane(11)})
    mr.lanes = {"lane1": lane(99, scan_time=200)}
    watcher.delivered(watcher.poll()[0]["key"])
    restarted = lane_data_sync.LaneDataWatcher(
        tmp_path / "lane_watch.json", mr=mr, client=client, printer_id=PID,
        printer_state_fn=lambda _mr: "standby", apply_fn=lambda *_a: None)
    mr.lanes = {"lane1": lane(99, scan_time=300, material="PLA+")}
    assert restarted.poll() == []
