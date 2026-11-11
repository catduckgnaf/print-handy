"""The per-model color architecture table (issue #656, Stage 1; #671 for Bambu).

Every registered model names exactly one color_system class; the adapter
properties and the counts in its own defaults must agree with it. This is a
characterization surface only: it pins the classification, not any behavior.
"""
import pytest

from print_handy import printers
from print_handy.printers.base import COLOR_SYSTEMS, PrinterModel

# The only non-single models. The Prusa XL joined multi_tool with #670 (the
# adapter-contract driving landed with the classification). The single-nozzle
# Bambu AMS models are multi_slot since #671 (four AMS slots each, from their
# profile facts); the dual-nozzle Bambu models (H2D/H2C family) stay "single"
# by #671's explicit treatment — two independent extruders, each AMS-fed, are
# neither the multi_slot shape nor the multi_tool contract — and
# test_bambu_ams_mapping pins their (slot_count, multi_slot).
EXPECTED = {
    "flashforge.ad5x": "multi_slot",
    "flashforge_stock.ad5x": "multi_slot",
    "flashforge.creator5": "multi_tool",
    "flashforge.creator5_pro": "multi_tool",
    "bambulab.a1": "multi_slot",
    "bambulab.a1mini": "multi_slot",
    "bambulab.a2l": "multi_slot",
    "bambulab.h2s": "multi_slot",
    "bambulab.p1p": "multi_slot",
    "bambulab.p1s": "multi_slot",
    "bambulab.p2s": "multi_slot",
    "bambulab.x1": "multi_slot",
    "bambulab.x1c": "multi_slot",
    "bambulab.x1e": "multi_slot",
    "prusa.xl": "multi_tool",
    "snapmaker.u1": "multi_tool",
}


def test_every_model_names_its_class():
    assert len(printers.MODELS) == 84
    for key, model in printers.MODELS.items():
        assert model.color_system in COLOR_SYSTEMS, key
        assert model.color_system == EXPECTED.get(key, "single"), key
    counts = {name: sum(1 for m in printers.MODELS.values() if m.color_system == name)
              for name in COLOR_SYSTEMS}
    # "shared_tool" (#1198) is accessory-conferred only: no model declares it.
    assert counts == {"single": 68, "multi_slot": 12, "multi_tool": 4, "shared_tool": 0}


def test_class_agrees_with_the_model_counts():
    for key, model in printers.MODELS.items():
        printer = model.defaults()["printer"]
        filament_slots = int(printer.get("filament_slots", 1))
        tool_count = int(printer.get("tool_count", 1))
        assert (filament_slots > 1) == (model.color_system == "multi_slot"), key
        assert (tool_count > 1) == (model.color_system == "multi_tool"), key


def test_adapter_properties_derive_from_the_class():
    for key, model in printers.MODELS.items():
        adapter = model.adapter({"printer": {"model": key}}, model)
        assert adapter.color_system == model.color_system, key
        assert adapter.multi_slot == (model.color_system == "multi_slot"), key
        assert adapter.multi_tool == (model.color_system == "multi_tool"), key


def test_bambu_ams_slot_counts_come_from_the_facts():
    # #671: a multi_slot Bambu model's slots are its AMS unit's trays, counted
    # from the model's own profile facts — never a hard-coded number.
    for key, model in printers.MODELS.items():
        if not key.startswith("bambulab.") or model.color_system != "multi_slot":
            continue
        adapter = model.adapter({"printer": {"model": key}}, model)
        assert adapter.slot_count == adapter.fact("ams")[1] == 4, key


def test_slots_bound_by_record_is_the_ifs_vocabulary_only():
    # Only the AD5X families bind slots in the hardware record (the spool
    # lifecycle writes the slot map, and intake/slicing validate against it).
    # Every other adapter leaves the flag off — including the multi_slot Bambu
    # AMS models (#671), whose printer reports its own trays, and the
    # multi_tool pair, whose positions are toolheads, not record slots.
    for key, model in printers.MODELS.items():
        adapter = model.adapter({"printer": {"model": key}}, model)
        assert adapter.slots_bound_by_record == key.startswith(("flashforge.", "flashforge_stock.")), key


def test_native_or_none_matches_the_native_contract():
    # Only the ZMod AD5X has a native contract; every other multi_slot adapter
    # (stock AD5X, Bambu AMS) reports None, so shared code can consult the
    # native mapping by contract instead of by class (#671).
    for key, model in printers.MODELS.items():
        adapter = model.adapter({"printer": {"model": key}}, model)
        if key == "flashforge.ad5x":
            assert adapter.native_or_none is not None, key
        elif adapter.multi_slot or adapter.multi_tool:
            assert adapter.native_or_none is None, key


def _model(**overrides):
    base = dict(key="test.model", label="Test", adapter=printers.MODELS["klipper.generic"].adapter,
                templates="klipper_generic", settings={"printer": {}})
    base.update(overrides)
    return PrinterModel(**base)


def test_contradictory_models_are_refused():
    with pytest.raises(ValueError, match="color_system"):
        _model(color_system="rainbow")
    with pytest.raises(ValueError, match="multi_slot"):
        _model(settings={"printer": {"filament_slots": 4}})  # slots without the class
    with pytest.raises(ValueError, match="multi_tool"):
        _model(settings={"printer": {"tool_count": 4}})  # tools without the class
    with pytest.raises(ValueError, match="multi_slot"):
        _model(color_system="multi_slot")  # the class without the slots
    with pytest.raises(ValueError, match="multi_tool"):
        _model(color_system="multi_tool")  # the class without the tools
