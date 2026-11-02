import json
from pathlib import Path
from unittest import mock
import pytest

from print_handy.hermes import spool_sync
from print_handy.printers.flashforge.zmod import screen
from print_handy.hermes.moonraker_event_listener import MidPrintSlotTracker, load_runout_pause
from print_handy.hermes.print_next import NextPlateError


def test_load_state_adopts_the_pre_rename_slots_key(tmp_path):
    # #672: a hardware record written before the rename names the slot map
    # ifs_slots; the spool_sync loader adopts it, like printer_state.load.
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "ifs_slots": {"1": {"spoolman_id": 7}}}))
    state = spool_sync.load_state(path)
    assert state["filament_slots"]["1"]["spoolman_id"] == 7
    assert "ifs_slots" not in state


def _elsewhere_layout(tmp_path, monkeypatch, default_id="ad5x"):
    """A state root holding the default printer's legacy root record (its
    pre-adoption spelling, retained as the rollback copy) plus per-printer
    records. Returns the default printer's per-printer record path."""
    monkeypatch.setattr(spool_sync._config, "cfg", lambda: {"printers": [{"id": default_id}]})
    root_record = {"spool": {"spoolman_id": 7, "material": "PLA"},
                   "filament_slots": {"1": {"spoolman_id": 7, "material": "PLA"}}}
    (tmp_path / "printer_hardware.json").write_text(json.dumps(root_record))
    mine = tmp_path / "printers" / default_id
    mine.mkdir(parents=True)
    (mine / "printer_hardware.json").write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    return mine / "printer_hardware.json"


def test_loaded_elsewhere_ignores_the_root_record_for_the_default_printer(tmp_path, monkeypatch):
    # P2-127: binding to the default printer's per-printer record must not
    # treat that same printer's root-level record as another printer — a
    # spool already loaded on the default printer can be rebound to it.
    target = _elsewhere_layout(tmp_path, monkeypatch)
    assert spool_sync.spool_loaded_elsewhere(7, state_path=target, printer_id="ad5x") is None


def test_loaded_elsewhere_still_reports_a_real_other_printer(tmp_path, monkeypatch):
    # P2-127 guard: the root-record exclusion is only the default printer's
    # own alias; a different printer loading the spool is still reported.
    target = _elsewhere_layout(tmp_path, monkeypatch)
    other = tmp_path / "printers" / "other"
    other.mkdir(parents=True)
    (other / "printer_hardware.json").write_text(json.dumps(
        {"spool": {}, "filament_slots": {"2": {"spoolman_id": 7, "material": "PLA"}}}))
    assert spool_sync.spool_loaded_elsewhere(7, state_path=target, printer_id="ad5x") == "other"


def test_loaded_elsewhere_root_target_ignores_the_default_printers_copy(tmp_path, monkeypatch):
    # The pre-existing direction of the alias exclusion stays: binding the
    # root record does not see the default printer's per-printer copy.
    _elsewhere_layout(tmp_path, monkeypatch)
    per_printer = tmp_path / "printers" / "ad5x" / "printer_hardware.json"
    per_printer.write_text(json.dumps(
        {"spool": {}, "filament_slots": {"3": {"spoolman_id": 8, "material": "PLA"}}}))
    assert spool_sync.spool_loaded_elsewhere(
        8, state_path=tmp_path / "printer_hardware.json") is None


def test_loaded_elsewhere_adopts_legacy_ifs_slots_of_other_printers(tmp_path, monkeypatch):
    # P2-267: another printer's pre-#672 record names its slot map ifs_slots;
    # the cross-printer scan must adopt that key or spools in its non-active
    # slots are invisible and the same spool can be bound twice.
    monkeypatch.setattr(spool_sync._config, "cfg", lambda: {"printers": [{"id": "ad5x"}]})
    other = tmp_path / "printers" / "other"
    other.mkdir(parents=True)
    (other / "printer_hardware.json").write_text(json.dumps(
        {"spool": {"spoolman_id": 3, "material": "PLA"},
         "ifs_slots": {"2": {"spoolman_id": 9, "material": "PLA"}}}))
    mine = tmp_path / "printers" / "ad5x"
    mine.mkdir(parents=True)
    (mine / "printer_hardware.json").write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    assert spool_sync.spool_loaded_elsewhere(
        9, state_path=mine / "printer_hardware.json", printer_id="ad5x") == "other"
    # The other printer's active spool was always visible; it still is.
    assert spool_sync.spool_loaded_elsewhere(
        3, state_path=mine / "printer_hardware.json", printer_id="ad5x") == "other"


def test_slot_record_strips_repeated_trailing_material_case_insensitively():
    spool = {"id": 14, "filament": {"name": "Pink PLA", "material": "PLA", "color_hex": "F95D73"}}
    record = spool_sync._slot_record(2, spool, None, "test")
    assert record["color"] == "pink"
    assert record["name"] == "Pink PLA"

    distinct = spool_sync._slot_record(2, {"id": 15, "filament": {"name": "Pink", "material": "PLA"}}, None, "test")
    assert distinct["color"] == "pink"
    assert distinct["name"] == "Pink PLA"

    material_only = spool_sync._slot_record(2, {"id": 16, "filament": {"name": "PLA", "material": "PLA"}}, None, "test")
    assert material_only["color"] == ""
    assert material_only["name"] == "PLA"

    plus = spool_sync._slot_record(
        1, {"id": 13, "filament": {"name": "Matte White PLA+", "material": "PLA+"}}, None, "test")
    assert plus["color"] == "matte white"
    assert plus["name"] == "Matte White PLA+"


def test_screen_material_coercion():
    assert screen.screen_type("PLA+") == "PLA"
    assert screen.screen_type("pla") == "PLA"
    assert screen.screen_type("PETG-CF") == "PETG-CF"
    assert screen.screen_type("UnknownType") == "?"  # no type for it: the screen's placeholder (#798)
    assert screen.screen_type("") is None
    assert screen.screen_type(None) is None


@pytest.mark.parametrize("material, expected, note", [
    ("ASA", "ABS", "screen shows ABS for ASA"),
    ("ASA-CF", "ABS", "screen shows ABS for ASA-CF"),
    ("ABS-CF", "ABS", "screen shows ABS for ABS-CF"),
    ("PA", "?", "screen shows ? for PA"),
    ("Nylon", "?", "screen shows ? for Nylon"),
    ("PC", "?", "screen shows ? for PC"),
    ("UnknownType", "?", "screen shows ? for UnknownType"),
    # Mapped already, so no fallback phrase: the screen's PLA is the PLA family.
    ("PLA+", "PLA", None),
    ("PLA Pro", "PLA", None),
    ("Matte PLA", "PLA", None),
    ("ABS", "ABS", None),
    ("Silk PLA", "SILK", None),
    ("", None, None),
])
def test_screen_type_falls_back_to_the_nearest_type_and_says_so(material, expected, note):
    assert screen.screen_type(material) == expected
    assert screen.fallback_note(material) == note


def test_screen_command_uses_the_fallback_type_instead_of_refusing():
    assert screen.screen_command(1, "ASA", "#112233") == "CHANGE_ZCOLOR SLOT=1 TYPE=ABS HEX=112233"
    assert screen.screen_command(2, "PA", "112233") == "CHANGE_ZCOLOR SLOT=2 TYPE=? HEX=112233"
    with pytest.raises(spool_sync.SpoolError, match="no type"):
        screen.screen_command(1, "", "112233")  # a blank material is still left alone


def test_screen_hex_format():
    assert spool_sync.screen_hex("#ff00aa") == "FF00AA"
    assert spool_sync.screen_hex("112233") == "112233"
    assert spool_sync.screen_hex("bad") is None  # a word, not #BBAADD
    assert spool_sync.screen_hex("nope") is None
    assert spool_sync.screen_hex("#ace") == "AACCEE"
    assert spool_sync.screen_hex(None) is None


def test_suspend_and_resume_usage():
    client = mock.MagicMock()
    client.side_effect = [{"result": {"spool_id": 42}}, {}, {"result": {"spool_id": None}}]
    prior = spool_sync.suspend_usage(mr=client)
    assert prior == 42
    client.assert_any_call("POST", "/server/spoolman/spool_id", {})

    client2 = mock.MagicMock()
    client2.side_effect = [{}, {"result": {"spool_id": 99}}]
    spool_sync.resume_usage(99, mr=client2)
    client2.assert_any_call("POST", "/server/spoolman/spool_id", {"spool_id": 99})


def _sync_state_file(tmp_path, *, with_spool=True):
    state = {"filament_slots": {
        "1": {"spoolman_id": 11, "material": "PLA", "hex": "F95D73", "name": "New PLA"},
        "2": {"spoolman_id": 12, "material": "PLA", "hex": "067749", "name": "Other PLA"}}}
    if with_spool:
        state["spool"] = {"spoolman_id": 11, "name": "New PLA", "material": "PLA",
                          "density": 1.24, "diameter_mm": 1.75}
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps(state))
    return path


def _sync_mr(active, *, printer="standby", stuck=False):
    """Moonraker fake for /spool sync: print state, the spool pointer, screen posts."""
    calls = []

    def mr(method, path, body=None):
        calls.append((method, path, body))
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": printer}}}}
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                if not stuck:
                    active[0] = (body or {}).get("spool_id")
                return {}
            return {"result": {"spool_id": active[0]}}
        return {"result": "ok"}

    return mr, calls


def _run_sync(tmp_path, mr, state_path):
    with mock.patch.object(spool_sync, "push_screen", return_value="pushed"):
        return spool_sync.sync_all_screens(mr=mr, state_path=state_path,
                                           pending_path=tmp_path / "pending.json")


def test_spool_sync_repairs_a_stale_usage_pointer(tmp_path):
    # P2-179: bind_slot saved the new mapping but re-pointing Moonraker's
    # active spool failed, and its message told the user to run /spool sync.
    # The pointer still names spool #10, which is loaded nowhere; sync must
    # actually repair the charging, not just refresh the screens.
    active = [10]
    mr, _calls = _sync_mr(active)
    lines = _run_sync(tmp_path, mr, _sync_state_file(tmp_path))
    assert active[0] == 11
    assert any("charging" in line and "#11" in line for line in lines)


def test_spool_sync_leaves_a_matching_usage_pointer_alone(tmp_path):
    active = [11]
    mr, calls = _sync_mr(active)
    lines = _run_sync(tmp_path, mr, _sync_state_file(tmp_path))
    assert active[0] == 11
    assert not any(method == "POST" and path == "/server/spoolman/spool_id" for method, path, _ in calls)
    assert any("charging" in line and "#11" in line for line in lines)


def test_spool_sync_reports_a_stale_pointer_with_no_recorded_replacement(tmp_path):
    active = [10]
    mr, calls = _sync_mr(active)
    lines = _run_sync(tmp_path, mr, _sync_state_file(tmp_path, with_spool=False))
    assert active[0] == 10
    assert not any(method == "POST" and path == "/server/spoolman/spool_id" for method, path, _ in calls)
    assert any("charging" in line and "#10" in line for line in lines)


def test_spool_sync_never_repairs_charging_while_printing(tmp_path):
    active = [10]
    mr, calls = _sync_mr(active, printer="printing")
    lines = _run_sync(tmp_path, mr, _sync_state_file(tmp_path))
    assert active[0] == 10
    assert not any(method == "POST" and path == "/server/spoolman/spool_id" for method, path, _ in calls)
    assert any("charging" in line for line in lines)


def test_spool_sync_reports_a_failed_repair_without_raising(tmp_path):
    active = [10]
    mr, _calls = _sync_mr(active, stuck=True)  # POST accepted, pointer never moves
    lines = _run_sync(tmp_path, mr, _sync_state_file(tmp_path))
    assert active[0] == 10
    assert any("charging" in line and "could not" in line for line in lines)


def test_mid_print_slot_tracker():
    tracker = MidPrintSlotTracker(stable_samples=2)

    # Initial observations: need 2 stable samples to confirm
    assert tracker.observe({"1": True, "2": True}) == []
    assert tracker.observe({"1": True, "2": True}) == []
    assert tracker.confirmed == {1: True, 2: True}

    # Slot 2 runs out: sample 1 (candidate changes)
    assert tracker.observe({"1": True, "2": False}) == []
    # Sample 2 (stable change -> event fired)
    events = tracker.observe({"1": True, "2": False})
    assert events == [("emptied", 2)]
    assert 2 in tracker.emptied

    # Slot 2 refilled: sample 1
    assert tracker.observe({"1": True, "2": True}) == []
    # Sample 2
    events = tracker.observe({"1": True, "2": True})
    assert events == [("refilled", 2)]
    assert 2 not in tracker.emptied


def test_split_count_copies():
    from print_handy.hermes import print_control as pc
    assert pc.split_count("benchy x3") == ("benchy", 3)
    assert pc.split_count("shelf 5 copies") == ("shelf", 5)
    assert pc.split_count("part 20 times") == ("part", 20)
    # Beyond max copies raises NextPlateError
    with pytest.raises(NextPlateError, match="copies must be 1-20"):
        pc.split_count("part x50")
    # Normal title ending in number is NOT parsed as count
    assert pc.split_count("funko shelf v2") == ("funko shelf v2", 1)


def test_slot_record_missing_color_never_reuses_cached_screen_color():
    record = spool_sync._slot_record(2, {"id": 7, "filament": {"name": "Coffee PLA+", "material": "PLA+", "color_hex": None}}, {"hex": "F95D73"}, "test")
    assert record["spoolman_id"] == 7
    # Never the cached screen color: the code comes from this spool's own name.
    assert record["hex"] == "6F4E37" and record["hex_source"] == "name"


def test_list_text_shelf_names_the_material_once(tmp_path):
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"filament_slots": {}}))
    client = lambda method, path, body=None: [
        {"id": 21, "remaining_weight": 800, "filament": {"name": "Coffee PLA+", "material": "PLA+"}}]
    text = spool_sync.list_text(client=client, state_path=state)
    assert "#21 Coffee PLA+, 800 g" in text and "PLA+ PLA+" not in text


def test_screen_label_matches_the_type_print_handy_writes():
    from print_handy.printers.flashforge.zmod import screen
    for material in ("Silk PLA", "PLA Silk", "Shiny Silk PLA"):
        assert screen.screen_type(material) == "SILK"
        assert screen.label_matches("SILK", material)
    assert screen.label_matches("PLA", "PLA+")
    assert screen.label_matches("PLA-CF", "PLA-CF")
    assert not screen.label_matches("SILK", "PLA")  # a plain spool in a slot marked silk
    assert not screen.label_matches("SILK", "PETG")
    assert not screen.label_matches("PLA", "PLA-CF")
    assert not screen.label_matches("?", "PLA")
    assert not screen.label_matches(None, "PLA")


def test_screen_label_matches_the_fallback_type_it_wrote():
    # #798: the screen shows ABS for an ASA spool and "?" for PA. A mapped launch must accept
    # exactly the label Print Handy wrote, or it would refuse its own sync.
    assert screen.label_matches("ABS", "ASA")
    assert screen.label_matches("ABS", "ASA-CF")
    assert screen.label_matches("ABS", "ABS-CF")
    assert screen.label_matches("?", "PA")
    assert screen.label_matches("?", "PC")
    assert screen.label_matches("?", "Nylon")
    # ...and nothing wider.
    assert not screen.label_matches("?", "PETG")
    assert not screen.label_matches("?", "PLA+")
    assert not screen.label_matches("ABS", "PLA")
    assert not screen.label_matches("PETG", "ASA")
    assert not screen.label_matches("", "PA")
    assert not screen.label_matches(None, "ASA")
    # The adapter's native hook is the one model_job.print_mapped_native calls.
    from print_handy.printers.flashforge import zmod
    assert zmod.screen_label_matches("ABS", "ASA")
    assert zmod.screen_label_matches("?", "PA")


def test_screen_fallback_note_comes_from_the_adapter_and_never_raises():
    zmod_like = mock.Mock(screen_fallback_note=screen.fallback_note)
    assert spool_sync.screen_fallback_note("ASA", zmod_like) == "screen shows ABS for ASA"
    assert spool_sync.screen_fallback_note("PLA+", zmod_like) is None
    assert spool_sync.screen_fallback_note("ASA", mock.Mock(screen_fallback_note=mock.Mock(side_effect=OSError))) is None
    assert spool_sync.screen_fallback_note("ASA", mock.Mock()) is None  # not a phrase


def test_bind_text_says_when_the_screen_shows_a_stand_in():
    result = {"slot": 2, "spool_id": 9, "record": {"name": "Gray ASA"}, "prior_spool_id": None,
              "screen": "pushed", "screen_note": "screen shows ABS for ASA", "usage": "unchanged"}
    assert spool_sync.bind_text(result) == (
        "Slot 2 → spool #9 (Gray ASA). Printer screen updated (screen shows ABS for ASA).")
    deferred = {**result, "screen": "deferred"}
    assert "ends (screen shows ABS for ASA)." in spool_sync.bind_text(deferred)
    plain = {**result, "screen_note": None}
    assert spool_sync.bind_text(plain) == "Slot 2 → spool #9 (Gray ASA). Printer screen updated."


def test_bind_slot_carries_the_fallback_note_only_when_the_screen_was_told(tmp_path):
    state = tmp_path / "printer_hardware.json"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    spool = {"id": 9, "remaining_weight": 800, "filament": {
        "name": "Gray ASA", "material": "ASA", "color_hex": "808080", "weight": 1000}}
    client = lambda method, path, body=None: spool if method == "GET" else {}
    adapter = mock.Mock(screen_fallback_note=screen.fallback_note)
    with mock.patch.object(spool_sync, "_printer_adapter", return_value=adapter), \
            mock.patch.object(spool_sync, "push_screen", return_value="pushed"), \
            mock.patch.object(spool_sync, "active_usage_spool", return_value=None):
        told = spool_sync.bind_slot(2, 9, client=client, mr=mock.Mock(), state_path=state)
    assert told["screen"] == "pushed" and told["screen_note"] == "screen shows ABS for ASA"
    state.write_text(json.dumps({"spool": {}, "filament_slots": {}}))
    with mock.patch.object(spool_sync, "_printer_adapter", return_value=adapter), \
            mock.patch.object(spool_sync, "active_usage_spool", return_value=None):
        quiet = spool_sync.bind_slot(2, 9, client=client, mr=mock.Mock(), state_path=state,
                                     push_screen_update=False)
    assert quiet["screen"] == "not requested" and quiet["screen_note"] is None


def test_spool_sync_names_the_stand_in_the_screen_shows(tmp_path):
    state = {"filament_slots": {
        "1": {"spoolman_id": 11, "material": "ASA", "hex": "F95D73", "name": "Red ASA"},
        "2": {"spoolman_id": 12, "material": "PLA", "hex": "067749", "name": "Other PLA"}}}
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps(state))
    active = [11]
    mr, _calls = _sync_mr(active)
    adapter = mock.Mock(screen_fallback_note=screen.fallback_note)
    with mock.patch.object(spool_sync, "_printer_adapter", return_value=adapter), \
            mock.patch.object(spool_sync.printers, "current", return_value=adapter):
        lines = _run_sync(tmp_path, mr, path)
    assert lines[0] == "Slot 1: pushed (screen shows ABS for ASA)"
    assert lines[1] == "Slot 2: pushed"
