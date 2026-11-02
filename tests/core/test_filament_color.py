"""Filament color codes are always recorded, resolved and never overwritten.

Live 2026-10-29: two spools created through the conversational intake were
saved with a color name but no hex, and an AD5X multicolor slice died on
"filament color must be an explicit six-digit RGB value" (PB_SLICING_UNKNOWN).
A one-color job on a slot bound through the insertion prompt was refused as
"unverified" the same way. These tests pin the fix: derivation from names,
the slicing fallback chain, never-overwrite, and the plain refusal path.
"""
import json

import pytest

from print_handy import diagnostics
from print_handy.core import filament_color as fc
from print_handy.core import slice_refusal
from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import spool_sync


# ------------------------------------------------------------- derivation

@pytest.mark.parametrize("name, material, expected", [
    ("matte white", None, "FFFFFF"),
    ("Matte White", "PLA", "FFFFFF"),
    ("Black", "PLA", "161616"),
    ("Pink PLA", "PLA", "F48FB1"),
    ("Coffee PLA+", "PLA+", "6F4E37"),
    ("Sky Blue", "PETG", "64B5F6"),
    ("Glossy Grey", None, "8E8E8E"),
])
def test_color_names_derive_a_code(name, material, expected):
    assert fc.derive_hex(name, material) == expected


def test_multicolor_silk_name_records_a_representative_and_every_color():
    guess = fc.derive("rose red blue green", "Silk PLA")
    assert guess.hex == "C2185B"  # "rose red" is one color, not rose + red
    assert guess.multi and guess.multi_hexes == ("C2185B", "1E88E5", "388E3C")


@pytest.mark.parametrize("name", ["Galaxy", "", None, "Silk PLA", "PLA"])
def test_unreadable_names_derive_nothing(name):
    assert fc.derive(name, "Silk PLA") is None


def test_hex_normalization():
    assert fc.normalize_hex("#0c6283") == "0C6283"
    assert fc.normalize_hex("FF0000,00FF00") is None
    assert fc.first_hex("FF0000,00FF00") == "FF0000"
    assert fc.normalize_hex("") is None and fc.normalize_hex(None) is None


# ------------------------------------------------------------- fallback chain

def test_resolve_follows_slot_spoolman_printer_name_default_order():
    rec = {"hex": "", "color": "rose red blue green", "material": "Silk PLA"}
    assert fc.resolve({**rec, "hex": "#123456"}, spoolman_hex="AAAAAA", printer_hex="BBBBBB") == ("123456", "slot")
    assert fc.resolve(rec, spoolman_hex="AAAAAA", printer_hex="BBBBBB") == ("AAAAAA", "spoolman")
    assert fc.resolve(rec, spoolman_hex=None, printer_hex="0C6283") == ("0C6283", "printer")
    assert fc.resolve(rec) == ("C2185B", "name")
    assert fc.resolve({"hex": "", "color": "galaxy"}) == (fc.NEUTRAL_DEFAULT, "default")


def test_resolve_prefer_printer_puts_the_screens_hex_ahead_of_spoolmans():
    rec = {"hex": "", "color": "rose red blue green", "material": "Silk PLA"}
    assert fc.resolve(rec, spoolman_hex="AAAAAA", printer_hex="BBBBBB", prefer_printer=True) == ("BBBBBB", "printer")
    assert fc.resolve(rec, spoolman_hex="AAAAAA", printer_hex=None, prefer_printer=True) == ("AAAAAA", "spoolman")
    assert fc.resolve({**rec, "hex": "123456"}, printer_hex="BBBBBB", prefer_printer=True) == ("123456", "slot")


def test_printer_slot_hexes_reads_zmod_color():
    status = {"slots": [{"ID": 1, "HEX": "FFFFFF"}, {"ID": 2, "HEX": "#0c6283"}, {"ID": 3, "HEX": ""}]}
    assert fc.printer_slot_hexes(status) == {"1": "FFFFFF", "2": "0C6283"}


def test_slot_hex_never_raises_and_says_when_it_guessed(capsys):
    assert fc.slot_hex(1, {"hex": "ffffff"}) == "#FFFFFF"
    assert fc.slot_hex(2, {"hex": "", "name": "Rose Red Blue Green Silk PLA", "material": "Silk PLA"}) == "#C2185B"
    assert fc.slot_hex(3, {"hex": None, "name": "Galaxy PLA"}) == "#" + fc.NEUTRAL_DEFAULT
    assert fc.slot_hex(4, "") == "#" + fc.NEUTRAL_DEFAULT
    out = capsys.readouterr().out
    assert "Slot 2 (Rose Red Blue Green Silk PLA) has no color code recorded" in out
    assert "Slot 3 (Galaxy PLA) has no color code and none could be worked out" in out


# ------------------------------------------------------------- backfill

def _bound(**extra):
    """A slot record as /spool or the IFS insertion prompt writes it."""
    return {"slot": 2, "material": "Silk PLA", "color": "rose red blue green", "hex": "",
            "spoolman_id": 17, "name": "Rose Red Blue Green Silk PLA", "quantity_status": "spoolman",
            "physical_color_verified": False, "physical_loading_verified": True, **extra}


def test_backfill_fills_blank_hex_and_confirms_an_operator_bound_slot():
    state = {"filament_slots": {"2": _bound()}}
    changes = fc.fill_missing_slot_hexes(state)
    rec = state["filament_slots"]["2"]
    assert rec["hex"] == "C2185B" and rec["hex_source"] == "name"
    assert rec["physical_color_verified"] is True
    assert changes == [{"slot": "2", "hex": "C2185B", "source": "name", "previous": ""}]


def test_backfill_never_overwrites_a_valid_hex():
    state = {"filament_slots": {"1": _bound(hex="F95D73", physical_color_verified=True),
                           "3": {"spoolman_id": 4, "hex": "000000", "color": "white",
                                 "physical_color_verified": True}}}
    before = json.loads(json.dumps(state))
    assert fc.fill_missing_slot_hexes(state, spoolman_hexes={17: "AAAAAA", 4: "BBBBBB"},
                                      printer_hexes={"1": "CCCCCC", "3": "DDDDDD"}) == []
    assert state == before


def test_backfill_upgrades_only_its_own_guess_to_an_authoritative_source():
    state = {"filament_slots": {"2": _bound(hex="C2185B", hex_source="name", physical_color_verified=True)}}
    assert fc.fill_missing_slot_hexes(state) == []  # a second name read changes nothing
    fc.fill_missing_slot_hexes(state, printer_hexes={"2": "0C6283"})
    assert state["filament_slots"]["2"]["hex"] == "0C6283"
    assert state["filament_slots"]["2"]["hex_source"] == "printer"


def test_insertion_bound_slot_with_hex_but_unverified_color_is_confirmed():
    # Live 2026-10-29: Matte white PLA, spool 16, "Created and bound from
    # Discord IFS insertion": loading verified, color not. Picking the
    # spool by name for the slot is the confirmation.
    state = {"filament_slots": {"1": _bound(slot=1, material="PLA", color="matte white", hex="FFFFFF",
                                       spoolman_id=16, name="Matte white PLA")}}
    fc.fill_missing_slot_hexes(state)
    assert state["filament_slots"]["1"]["physical_color_verified"] is True
    assert state["filament_slots"]["1"]["hex"] == "FFFFFF"


def test_a_slot_not_bound_by_the_operator_stays_unverified():
    state = {"filament_slots": {"1": {"spoolman_id": 16, "material": "PLA", "hex": "FFFFFF",
                                 "physical_color_verified": False, "physical_loading_verified": False}}}
    fc.fill_missing_slot_hexes(state)
    assert state["filament_slots"]["1"]["physical_color_verified"] is False


def test_load_state_backfills_in_memory(tmp_path):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"filament_slots": {"2": _bound()}}))
    assert spool_sync.load_state(path)["filament_slots"]["2"]["hex"] == "C2185B"


def test_backfill_slot_colors_prefers_the_printers_hex_then_spoolman_and_persists(tmp_path):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"filament_slots": {
        "1": _bound(slot=1, spoolman_id=16, color="matte white", name="Matte white PLA", material="PLA"),
        "2": _bound(),
        "3": _bound(slot=3, spoolman_id=9, hex="111111", physical_color_verified=True),
        "4": _bound(slot=4, spoolman_id=16, color="matte white", name="Matte white PLA", material="PLA")}}))
    writes = []
    printer_calls = []

    def client(method, path_, body=None):
        if method == "PATCH":
            writes.append((path_, body))
            return {}
        sid = int(path_.rsplit("/", 1)[1])
        return {"id": sid, "filament": {"id": 100 + sid, "color_hex": "FAFAFA" if sid == 16 else None}}

    def mr(method, path_, body=None):
        printer_calls.append((method, path_))
        assert "zmod_color" in path_
        return {"result": {"status": {"zmod_color": {"slots": [
            {"ID": 1, "HEX": "EEEEEE"}, {"ID": 2, "HEX": "0C6283"}, {"ID": 3, "HEX": "222222"}]}}}}

    changes = spool_sync.backfill_slot_colors(client=client, mr=mr, state_path=path)
    saved = json.loads(path.read_text())["filament_slots"]
    # The printer already shows EEEEEE for slot 1: adopting it (not Spoolman's
    # FAFAFA) means mapped launch cannot see a "live RGB mapping mismatch".
    assert saved["1"]["hex"] == "EEEEEE" and saved["1"]["hex_source"] == "printer"
    assert saved["2"]["hex"] == "0C6283" and saved["2"]["hex_source"] == "printer"
    assert saved["3"]["hex"] == "111111"  # a valid hex is never overwritten
    assert saved["4"]["hex"] == "FAFAFA" and saved["4"]["hex_source"] == "spoolman"  # printer reports none
    assert {c["slot"] for c in changes} == {"1", "2", "4"}
    screens = {c["slot"]: c["screen"] for c in changes}
    # Only the slot whose code differs from the screen is flagged for screen sync.
    assert screens == {"1": "matches", "2": "matches", "4": "needs_sync"}
    # Spoolman's empty color for spool 17 is filled in; spool 16 already had one.
    assert writes == [("/api/v1/filament/117", {"color_hex": "0C6283"})]
    # Backfill only reads the printer: no CHANGE_ZCOLOR (or any gcode) from here.
    assert printer_calls == [("GET", "/printer/objects/query?zmod_color")]


def test_backfill_slot_colors_reads_legacy_ifs_slots_records(tmp_path):
    """P2-268: a hardware record written before the #672 rename carries
    only ifs_slots; backfill must adopt the renamed key the way both
    state loaders do, not silently find no slots to fill."""
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"ifs_slots": {"2": _bound()}}))

    def client(method, path_, body=None):
        return {"id": 17, "filament": {"id": 117, "color_hex": None}}

    def mr(method, path_, body=None):
        return {"result": {"status": {"zmod_color": {"slots": [{"ID": 2, "HEX": "0C6283"}]}}}}

    changes = spool_sync.backfill_slot_colors(client=client, mr=mr, state_path=path)
    assert {c["slot"] for c in changes} == {"2"}
    saved = json.loads(path.read_text())
    assert saved["filament_slots"]["2"]["hex"] == "0C6283"
    assert "ifs_slots" not in saved


def test_backfill_adopts_the_printers_hex_over_an_earlier_name_guess():
    state = {"filament_slots": {"2": _bound(hex="C2185B", hex_source="name", physical_color_verified=True)}}
    fc.fill_missing_slot_hexes(state, spoolman_hexes={17: "AAAAAA"}, printer_hexes={"2": "0C6283"})
    assert (state["filament_slots"]["2"]["hex"], state["filament_slots"]["2"]["hex_source"]) == ("0C6283", "printer")


def test_a_derived_code_reaches_the_screen_through_bind_slot_screen_sync(tmp_path, monkeypatch):
    # bind_slot derives a code for a Spoolman filament with no color_hex and
    # tells the screen the same code through push_screen (CHANGE_ZCOLOR; it
    # defers while printing). The record and the screen label therefore agree.
    from print_handy.printers.flashforge.zmod import screen

    state = tmp_path / "hw.json"
    state.write_text(json.dumps({"filament_slots": {}}))
    spool = {"id": 17, "location": "Shelf",
             "filament": {"name": "Rose Red Blue Green Silk PLA", "material": "Silk PLA", "color_hex": None}}

    def client(method, path_, body=None):
        return spool if method == "GET" else {}

    pushed = []

    def spy(slot, material, hex_code, **kwargs):
        pushed.append(screen.screen_command(slot, material, hex_code))
        return "pushed"

    monkeypatch.setattr(spool_sync, "push_screen", spy)
    result = spool_sync.bind_slot(2, 17, client=client, mr=lambda *a, **k: {"result": {"spool_id": None}},
                                  state_path=state)
    assert result["record"]["hex"] == "C2185B" and result["record"]["hex_source"] == "name"
    assert pushed == ["CHANGE_ZCOLOR SLOT=2 TYPE=SILK HEX=C2185B"]
    assert result["screen"] == "pushed"


def test_backfill_slot_colors_survives_spoolman_and_printer_being_down(tmp_path):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"filament_slots": {"2": _bound()}}))

    def down(*args, **kwargs):
        raise OSError("down")

    spool_sync.backfill_slot_colors(client=down, mr=down, state_path=path)
    assert json.loads(path.read_text())["filament_slots"]["2"]["hex"] == "C2185B"


# ------------------------------------------------------------- intake

def _client(existing, captured):
    def client(method, path, body=None):
        captured.append((method, path, body))
        if method == "GET" and path == "/api/v1/filament":
            return existing
        if method == "GET" and path == "/api/v1/vendor":
            return []
        if method == "POST" and path == "/api/v1/filament":
            return {"id": 9}
        if method == "POST" and path == "/api/v1/spool":
            return {"id": 18}
        return {}
    return client


def test_create_spool_derives_the_code_from_the_name():
    captured = []
    made = spool_sync.create_spool({"brand": None, "material": "PLA", "color": "Matte White", "grams": 1000.0},
                                   client=_client([], captured))
    body = next(b for m, p, b in captured if m == "POST" and p == "/api/v1/filament")
    assert body["color_hex"] == "FFFFFF"
    assert made["hex"] == "FFFFFF" and made["hex_source"] == "name"


def test_create_spool_records_multi_color_hexes_for_a_silk_blend():
    captured = []
    spool_sync.create_spool({"brand": None, "material": "Silk PLA", "color": "Rose Red Blue Green",
                             "grams": 1000.0}, client=_client([], captured))
    body = next(b for m, p, b in captured if m == "POST" and p == "/api/v1/filament")
    assert body["color_hex"] == "C2185B"
    patch = next(b for m, p, b in captured if m == "PATCH" and p == "/api/v1/filament/9")
    assert patch["multi_color_hexes"] == "C2185B,1E88E5,388E3C"


def test_create_spool_fills_but_never_replaces_a_reused_filament_color():
    captured = []
    blank = [{"id": 4, "name": "Matte White", "material": "PLA", "vendor": None, "color_hex": None}]
    spool_sync.create_spool({"brand": None, "material": "PLA", "color": "Matte White", "grams": 500.0},
                            client=_client(blank, captured))
    assert ("PATCH", "/api/v1/filament/4", {"color_hex": "FFFFFF"}) in captured
    captured.clear()
    colored = [{**blank[0], "color_hex": "F0F0F0"}]
    spool_sync.create_spool({"brand": None, "material": "PLA", "color": "Matte White", "grams": 500.0},
                            client=_client(colored, captured))
    assert not any(m == "PATCH" for m, _, _ in captured)


def test_create_spool_with_an_unreadable_name_leaves_spoolman_color_empty():
    captured = []
    made = spool_sync.create_spool({"brand": None, "material": "PLA", "color": "Galaxy", "grams": 1000.0},
                                   client=_client([], captured))
    body = next(b for m, p, b in captured if m == "POST" and p == "/api/v1/filament")
    assert "color_hex" not in body and made["hex"] is None


def test_slot_record_always_has_a_code_and_is_confirmed():
    rec = spool_sync._slot_record(1, {"id": 16, "filament": {"name": "Matte White", "material": "PLA",
                                                              "color_hex": None}}, None, "test")
    assert rec["hex"] == "FFFFFF" and rec["hex_source"] == "name"
    assert rec["physical_color_verified"] is True
    galaxy = spool_sync._slot_record(1, {"id": 5, "filament": {"name": "Galaxy", "material": "PLA"}}, None, "t")
    assert galaxy["hex"] == fc.NEUTRAL_DEFAULT and galaxy["hex_source"] == "default"
    assert fc.chat_notes({"filament_slots": {"1": galaxy}}, [1])[0].startswith("Slot 1 (Galaxy PLA) has no color")


def test_walkthrough_asks_swatch_buttons_only_when_the_name_is_unreadable():
    readable = {"step": "color", "origin": "insertion", "material": "PLA", "color": "Matte White"}
    assert pab.intake_next_step(readable) == "weight"
    unreadable = {**readable, "color": "Galaxy"}
    assert pab.intake_next_step(unreadable) == "swatch"
    swatch = {**unreadable, "step": "swatch"}
    options = pab.intake_step_components("aid", swatch)[0]["components"][0]["options"]
    assert {"label": "White (#FFFFFF)", "value": "FFFFFF"} in options
    assert "hex" not in pab.intake_step_text(swatch).lower()
    picked = {**swatch, "hex": "1E88E5"}
    assert pab.intake_next_step(picked) == "weight"
    confirm = {**picked, "step": "confirm", "slot": 2, "predicted_id": 18, "grams": 1000}
    assert "Galaxy (#1E88E5)" in pab.intake_step_text(confirm)


def test_photo_prefill_with_unreadable_color_still_asks_the_swatch():
    filled = {"step": "weight", "origin": "insertion", "old_spool_id": 13, "material": "PLA",
              "color": "Nebula", "grams": 1000}
    assert pab.intake_next_step(filled) == "swatch"
    assert pab.intake_next_step({**filled, "step": "swatch", "hex": "161616"}) == "disposition"


def test_create_and_bind_passes_the_picked_swatch():
    seen = {}

    class Sync:
        @staticmethod
        def parse_new(text):
            return {"material": "PLA", "color": "Galaxy", "grams": 1000}

        @staticmethod
        def create_spool(spec):
            seen.update(spec)
            return {"spool_id": 18}

        @staticmethod
        def bind_slot(*args, **kwargs):
            return {"record": {"name": "Galaxy PLA"}, "prior_spool_id": None}

    pab.create_and_bind_insertion(2, {"material": "PLA", "color": "Galaxy", "grams": "1000", "hex": "1E88E5"}, Sync)
    assert seen["hex"] == "1E88E5"


# ------------------------------------------------------------- refusal

def test_unconfirmed_slot_refusal_is_plain_classified_and_fixable():
    exc = slice_refusal.unconfirmed_slot_color(
        2, _bound(hex="C2185B", hex_source="name"), "IFS slot color/material mapping is unverified")
    assert isinstance(exc, RuntimeError) and str(exc) == "IFS slot color/material mapping is unverified"
    assert exc.plain.startswith("Slot 2 (Rose Red Blue Green Silk PLA) was never confirmed as loaded")
    assert exc.next_step == "Is Rose Red Blue Green Silk PLA loaded in slot 2?"
    assert exc.fix == {"kind": "confirm_slot_color", "slot": "2", "hex": "C2185B"}
    assert diagnostics.marker("slicing", diagnostics.classify(exc)) == "PH_SLICING_CONFIGURATION"


def test_apply_fix_confirms_the_slot_and_never_overwrites_a_valid_hex(monkeypatch):
    from print_handy.core import printer_state
    store = {"filament_slots": {"1": {"spoolman_id": 16, "material": "PLA", "hex": "F0F0F0",
                                 "physical_color_verified": False}}}
    saved = []
    monkeypatch.setattr(printer_state, "load", lambda *a: json.loads(json.dumps(store)))
    monkeypatch.setattr(printer_state, "save", lambda st, *a: saved.append(st))
    line = slice_refusal.apply_fix({"kind": "confirm_slot_color", "slot": "1", "hex": "FFFFFF"})
    assert saved[0]["filament_slots"]["1"]["hex"] == "F0F0F0"
    assert saved[0]["filament_slots"]["1"]["physical_color_verified"] is True
    assert line == "Slot 1 color confirmed as #F0F0F0."


def test_validate_slot_selection_material_mismatch_is_never_waved_through():
    from print_handy.core import model_job
    state = {"filament_slots": {"1": {"spoolman_id": 16, "material": "PETG", "hex": "FFFFFF",
                                 "physical_color_verified": False}}}
    with pytest.raises(RuntimeError, match="selected IFS slot 1") as caught:
        model_job._validate_slot_selection(state, {"slot": 1, "spoolman_id": 16, "material": "PLA"})
    assert caught.value.fix is None  # never a one-tap override for a different material
    assert caught.value.plain == ("Slot 1 (PETG) does not match the spool chosen for this job (PLA), "
                                  "so I stopped before slicing.")
    state["filament_slots"]["1"]["material"] = "PLA"
    with pytest.raises(slice_refusal.SliceRefusal) as refusal:
        model_job._validate_slot_selection(state, {"slot": 1, "spoolman_id": 16, "material": "PLA"})
    assert refusal.value.fix["slot"] == "1"
