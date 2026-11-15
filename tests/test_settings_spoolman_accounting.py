"""#1536: per-printer Spoolman settings — an outside Spoolman URL and the
accounting authority, in the roster entry and in the global [spool]
section, with view_for projection and capture round-trips."""
import pytest

from print_handy import settings
from print_handy.core import printer_state


def _local(tmp_path, text, name="local.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


ROSTER = (
    "services:\n"
    "  spoolman_url: http://global-spoolman.test:7912\n"
    "printers:\n"
    "  - id: bench\n"
    "    model: flashforge.ad5x\n"
    "  - id: workshop\n"
    "    model: flashforge.ad5x\n"
    "    spoolman_url: http://outside.test:7912\n"
    "    spoolman_accounting: printer\n"
)


def test_global_spool_accounting_loads_and_defaults_to_unset(tmp_path):
    data = settings.load(local=False, environ={}, user=False)
    assert data["spool"]["accounting"] == ""
    local = _local(tmp_path, "spool:\n  accounting: printer\n")
    assert settings.load(local=local, environ={}, user=False)["spool"]["accounting"] == "printer"
    env = {"PRINT_HANDY__SPOOL__ACCOUNTING": "Print_Handy"}
    assert settings.load(local=False, environ=env, user=False)["spool"]["accounting"] == "print_handy"


def test_global_spool_accounting_rejects_unknown_values(tmp_path):
    local = _local(tmp_path, "spool:\n  accounting: sometimes\n")
    with pytest.raises(settings.SettingsError, match="accounting"):
        settings.load(local=local, environ={}, user=False)
    with pytest.raises(settings.SettingsError, match="accounting"):
        settings.load(local=False, environ={
            "PRINT_HANDY__SPOOL__ACCOUNTING": "sometimes"}, user=False)


def test_entry_spoolman_settings_are_captured_and_projected(tmp_path):
    data = settings.load(local=_local(tmp_path, ROSTER), environ={}, user=False)
    bench, workshop = data["printers"]
    # The first entry mirrors the shared sections, exactly like its
    # moonraker_url capture: it holds the global Spoolman URL.
    assert bench["spoolman_url"] == "http://global-spoolman.test:7912"
    assert bench["spoolman_accounting"] == ""
    assert workshop["spoolman_url"] == "http://outside.test:7912"
    assert workshop["spoolman_accounting"] == "printer"

    view = settings.view_for("workshop", data)
    assert view["services"]["spoolman_url"] == "http://outside.test:7912"
    assert view["spool"]["accounting"] == "printer"
    # The default printer keeps the shared service and the global value.
    assert settings.view_for("bench", data)["services"]["spoolman_url"] == \
        "http://global-spoolman.test:7912"
    assert settings.view_for("bench", data)["spool"]["accounting"] == ""


def test_entry_spoolman_settings_come_from_the_environment(tmp_path):
    env = {"PRINT_HANDY__PRINTERS__WORKSHOP__SPOOLMAN_URL": "http://env.test:7912",
           "PRINT_HANDY__PRINTERS__WORKSHOP__SPOOLMAN_ACCOUNTING": "printer"}
    data = settings.load(local=_local(tmp_path, ROSTER), environ=env, user=False)
    assert settings.view_for("workshop", data)["services"]["spoolman_url"] == \
        "http://env.test:7912"


def test_entry_spoolman_url_must_be_a_url_and_names_the_entry(tmp_path):
    bad = ROSTER.replace("http://outside.test:7912", "outside.test")
    with pytest.raises(settings.SettingsError, match="workshop.*spoolman_url"):
        settings.load(local=_local(tmp_path, bad), environ={}, user=False)


def test_entry_spoolman_accounting_rejects_unknown_values(tmp_path):
    bad = ROSTER.replace("spoolman_accounting: printer", "spoolman_accounting: sometimes")
    with pytest.raises(settings.SettingsError, match="workshop.*accounting"):
        settings.load(local=_local(tmp_path, bad), environ={}, user=False)


def test_printer_state_spoolman_url_prefers_the_printers_own_entry(tmp_path):
    data = settings.load(local=_local(tmp_path, ROSTER), environ={}, user=False)
    settings.set_override(data)
    try:
        assert printer_state.spoolman_url(printer_id="workshop") == "http://outside.test:7912"
        assert printer_state.spoolman_url(printer_id="bench") == \
            "http://global-spoolman.test:7912"
        # No printer named: the shared services URL, as before #1536.
        assert printer_state.spoolman_url() == "http://global-spoolman.test:7912"
        # Settings first, then the hardware record, as before #1536.
        assert printer_state.spoolman_url(
            {"spoolman_url": "http://recorded.test:7912"}) == \
            "http://global-spoolman.test:7912"
    finally:
        settings.set_override(None)
    bare = settings.load(local=False, environ={}, user=False)
    settings.set_override(bare)
    try:
        # With nothing configured, a hardware record's URL is the fallback.
        assert printer_state.spoolman_url(
            {"spoolman_url": "http://recorded.test:7912"}) == "http://recorded.test:7912"
    finally:
        settings.set_override(None)
