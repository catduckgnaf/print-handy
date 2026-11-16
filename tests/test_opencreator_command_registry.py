"""OpenCreator registry and fail-closed macro dispatch coverage."""
from pathlib import Path

import pytest

from print_handy import knowledge
from print_handy import macros


OPENCREATOR_MODELS = (
    "flashforge_opencreator.creator5",
    "flashforge_opencreator.creator5_pro",
)


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == macros.OBJECTS_LIST_PATH:
            return {"result": {"objects": ["gcode_macro C5_DOORS", "gcode_macro NOZZLE_HOURS"]}}
        if path == macros.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        raise AssertionError(f"unexpected transport request: {method} {path}")


def test_opencreator_registry_is_loaded_in_shared_klipper_family():
    registry = knowledge.load_registry()
    entries = [entry for entry in registry.firmware_entries if entry.id.startswith("klipper.opencreator-")]
    assert {entry.name for entry in entries} == {"C5_DOORS", "NOZZLE_HOURS"}
    assert all(entry.firmware == "klipper" and entry.kind == "macro" for entry in entries)
    assert {printer for entry in entries for printer in entry.printers} == set(OPENCREATOR_MODELS)
    assert all("hardware" in entry.safety.casefold() for entry in entries)


@pytest.mark.parametrize("printer", OPENCREATOR_MODELS)
def test_opencreator_registry_dispatch_fails_closed_before_state_or_send(printer):
    transport = Transport()
    with pytest.raises(macros.MacroRefusal, match="not enabled.*hardware"):
        macros.prepare_run(transport, "C5_DOORS", registry=knowledge.load_registry(), printer=printer)
    assert [call[1] for call in transport.calls] == [macros.OBJECTS_LIST_PATH]
    assert all(method != "POST" for method, _, _ in transport.calls)


@pytest.mark.parametrize("printer", OPENCREATOR_MODELS)
def test_opencreator_unregistered_macro_dispatch_also_fails_closed(printer):
    transport = Transport()
    with pytest.raises(macros.MacroRefusal, match="not enabled.*hardware"):
        macros.prepare_run(transport, "C5_PRINT_START", registry=knowledge.load_registry(), printer=printer)
    assert [call[1] for call in transport.calls] == [macros.OBJECTS_LIST_PATH]
    assert all(method != "POST" for method, _, _ in transport.calls)
