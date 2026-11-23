"""Explicit default-printer routing regressions for #1849 M10."""
from contextlib import nullcontext

from print_handy import settings
from print_handy.core import slice_refusal
from print_handy.gcode import verify as gcode_verify


def test_default_slot_color_fix_loads_locks_and_saves_explicit_printer(monkeypatch):
    from print_handy.core import printer_state

    seen = []
    record = {"filament_slots": {"2": {"hex": None, "material": "PLA"}}}
    monkeypatch.setattr(printer_state, "hardware_lock",
                        lambda printer_id: seen.append(("lock", printer_id)) or nullcontext())
    monkeypatch.setattr(printer_state, "load",
                        lambda printer_id: seen.append(("load", printer_id)) or record)
    monkeypatch.setattr(printer_state, "save",
                        lambda state, printer_id: seen.append(("save", printer_id)))
    result = slice_refusal.apply_fix({"kind": "confirm_slot_color", "slot": "2",
                                     "hex": "C2185B"})
    printer_id = settings.default_printer_id()
    assert seen == [("lock", printer_id), ("load", printer_id), ("save", printer_id)]
    assert result == "Slot 2 color confirmed as #C2185B."


def test_verify_state_routes_default_and_explicit_ids(monkeypatch):
    seen = []
    sentinel = {"bed_mm": [200, 200]}
    monkeypatch.setattr(gcode_verify.printer_state, "load",
                        lambda printer_id: seen.append(printer_id) or sentinel)
    assert gcode_verify._verify_state(None, None) is sentinel
    assert gcode_verify._verify_state(None, "second") is sentinel
    assert seen == [settings.default_printer_id(), "second"]


def test_verify_state_returns_none_on_missing_printer(monkeypatch):
    def unavailable(printer_id):
        raise OSError(f"{printer_id} missing")
    monkeypatch.setattr(gcode_verify.printer_state, "load", unavailable)
    assert gcode_verify._verify_state(None, None) is None
