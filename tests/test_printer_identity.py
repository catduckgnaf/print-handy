"""Stable printer ids, per-vendor settings views and the process-global override (P1).

Offline only: settings are loaded from temporary files, nothing contacts a printer.

Pins three behaviours the multi-printer design relies on but no test stated:
1. a roster-less config derives its id from printer.name on every load, so the id
   is only stable once it is written into a [[printers]] entry (rename procedure,
   docs/SETTINGS.md);
2. settings.view_for projects each vendor family's own connection and nothing else;
3. settings.set_override is process-global, so a per-printer view installed by a
   listener is what settings.current() returns until it is cleared.
"""
import tempfile
from pathlib import Path

import pytest

from print_handy import settings


def load_local(text):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td, "local.yaml")
        path.write_text(text)
        return settings.load(home=settings.HOME, local=path, environ={})


# ------------------------------------------------------------ stable ids

def test_synthesised_id_follows_the_printer_name():
    """Documented hazard: with no [[printers]], renaming printer.name changes the id,
    and with it the state folder (leases, hardware record)."""
    before = load_local('printer:\n  name: "Garage Printer"\n')
    after = load_local('printer:\n  name: "Shop Printer"\n')
    assert settings.default_printer_id(before) == "garage-printer"
    assert settings.default_printer_id(after) == "shop-printer"
    assert settings.printer_state_dir(None, before) != settings.printer_state_dir(None, after)


def test_pinned_id_survives_a_display_name_change():
    """The rename procedure: pin the old slug as an entry id, then rename freely."""
    old = load_local('printer:\n  name: "Garage Printer"\n')
    pinned = load_local(
        'printers:\n  - id: garage-printer\n    name: "Shop Printer"\n    model: klipper.generic\n'
    )
    assert settings.default_printer_id(pinned) == settings.default_printer_id(old)
    assert settings.printer_state_dir(None, pinned) == settings.printer_state_dir(None, old)
    assert settings.by_id("garage-printer", pinned)["name"] == "Shop Printer"


def test_changing_an_entry_id_is_a_different_printer():
    one = load_local('printers:\n  - id: old-id\n    model: klipper.generic\n')
    two = load_local('printers:\n  - id: new-id\n    model: klipper.generic\n')
    assert settings.printer_state_dir(None, one) != settings.printer_state_dir(None, two)
    with pytest.raises(settings.SettingsError):
        settings.by_id("old-id", two)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "Upper", "-lead", "has space", "", "..", "a\\b"])
def test_unsafe_ids_are_refused(bad):
    with pytest.raises(settings.SettingsError, match="slug"):
        load_local(f'printers:\n  - id: "{bad}"\n    model: klipper.generic\n')


def test_duplicate_ids_are_refused():
    with pytest.raises(settings.SettingsError):
        load_local(
            "printers:\n  - id: same\n    model: klipper.generic\n"
            "  - id: same\n    model: klipper.generic\n"
        )


# ------------------------------------------------- per-vendor view_for

ONE_PER_FAMILY = """
printers:
  - id: moon
    model: klipper.generic
    moonraker_url: http://10.0.0.1:7125
  - id: bambu
    model: bambulab.a1
    lan_host: 10.0.0.2
    lan_serial: "00M00A000000000"
  - id: prusa
    model: prusa.mk4s
    link_host: 10.0.0.3
  - id: sdcp
    model: elegoo_sdcp.centauricarbon
    host: 10.0.0.4
  - id: stock
    model: flashforge_stock.ad5x
    host: 10.0.0.5
  - id: octo
    model: octoprint.ender3
    octoprint_url: http://10.0.0.6:5000
"""

# printer id -> (section, key, expected value): the one connection each view must carry.
OWN_CONNECTION = {
    "moon": ("services", "moonraker_url", "http://10.0.0.1:7125"),
    "bambu": ("bambu", "lan_host", "10.0.0.2"),
    "prusa": ("prusa", "link_host", "10.0.0.3"),
    "sdcp": ("elegoo_sdcp", "host", "10.0.0.4"),
    "stock": ("flashforge_stock", "host", "10.0.0.5"),
    "octo": ("services", "octoprint_url", "http://10.0.0.6:5000"),
}

ALL_CONNECTION_KEYS = [
    ("services", "moonraker_url"), ("services", "octoprint_url"), ("bambu", "lan_host"),
    ("prusa", "link_host"), ("elegoo_sdcp", "host"), ("flashforge_stock", "host"),
]


@pytest.fixture(scope="module")
def roster():
    return load_local(ONE_PER_FAMILY)


@pytest.mark.parametrize("printer_id", sorted(OWN_CONNECTION))
def test_view_carries_only_its_own_vendor_connection(roster, printer_id):
    view = settings.view_for(printer_id, roster)
    assert view["printer"]["model"] == settings.by_id(printer_id, roster)["model"]
    own = OWN_CONNECTION[printer_id]
    for section, key in ALL_CONNECTION_KEYS:
        value = view.get(section, {}).get(key, "")
        if (section, key) == own[:2]:
            assert value == own[2]
        else:
            assert not value, f"{printer_id} view leaked {section}.{key}={value!r}"


def test_default_view_is_the_resolved_settings_and_others_are_copies(roster):
    """Documented: the default printer's view IS the resolved settings; every other
    view is a deep copy, so editing it cannot leak into the shared settings."""
    assert settings.view_for("moon", roster) is roster
    snapshot = repr(roster)
    for printer_id in OWN_CONNECTION:
        if printer_id != "moon":
            settings.view_for(printer_id, roster)["printer"]["name"] = "mutated"
    assert repr(roster) == snapshot


def test_global_sections_are_shared_and_state_dirs_are_per_printer(roster):
    views = [settings.view_for(pid, roster) for pid in OWN_CONNECTION]
    assert all(v["paths"] == roster["paths"] for v in views)
    dirs = {settings.printer_state_dir(pid, roster) for pid in OWN_CONNECTION}
    assert len(dirs) == len(OWN_CONNECTION)


# ------------------------------------- process-global override invariant

@pytest.fixture
def clean_override():
    settings.set_override(None)
    yield
    settings.set_override(None)


def test_override_is_what_current_returns_until_cleared(roster, clean_override):
    base = settings.current()
    view = settings.view_for("bambu", roster)
    settings.set_override(view)
    assert settings.current() is view
    settings.set_override(None)
    assert settings.current() is base


def test_second_override_replaces_the_first_so_one_process_serves_one_printer(roster, clean_override):
    """The invariant: one listener process, one printer. A later set_override wins
    process-wide, so two printers must never share a process (docs/SETTINGS.md)."""
    settings.set_override(settings.view_for("bambu", roster))
    settings.set_override(settings.view_for("prusa", roster))
    assert settings.current()["printer"]["model"] == "prusa.mk4s"
    assert settings.current()["bambu"]["lan_host"] == ""


def test_explicit_data_is_unaffected_by_an_installed_override(roster, clean_override):
    settings.set_override(settings.view_for("octo", roster))
    assert settings.view_for("moon", roster)["services"]["moonraker_url"] == "http://10.0.0.1:7125"
    assert settings.by_id("sdcp", roster)["host"] == "10.0.0.4"
