"""#1536: Moonraker Spoolman-link detection and the one-accounting-authority
resolution. Pure parsers and an injectable transport, so every case runs
offline."""
import pytest

from print_handy import settings
from print_handy.core import spoolman_link


# --- URL normalization / same-instance (checklist item 1: pointing Print
# Handy at the instance the printer already uses) ---------------------------

@pytest.mark.parametrize("raw,expected", [
    ("http://nas.local:7912", "http://nas.local:7912"),
    ("HTTP://NAS.local:7912/", "http://nas.local:7912"),
    ("nas.local:7912", "http://nas.local:7912"),
    ("http://nas.local:80/", "http://nas.local"),
    ("https://spool.example.com:443/", "https://spool.example.com"),
    ("http://nas.local:7912///", "http://nas.local:7912"),
    ("", ""),
    ("  ", ""),
    (None, ""),
])
def test_normalize_spoolman_url(raw, expected):
    assert spoolman_link.normalize_spoolman_url(raw) == expected


def test_same_spoolman_instance_compares_normalized_urls():
    assert spoolman_link.same_spoolman_instance(
        "HTTP://NAS.local:7912/", "http://nas.local:7912")
    assert not spoolman_link.same_spoolman_instance(
        "http://nas.local:7912", "http://other.local:7912")
    assert not spoolman_link.same_spoolman_instance("", "http://nas.local:7912")
    assert not spoolman_link.same_spoolman_instance("", "")


# --- Moonraker payload parsing (checklist item 2) ---------------------------

def test_parse_moonraker_spoolman_config_reads_the_server_option():
    payload = {"result": {"config": {"spoolman": {"server": "http://nas.local:7912"}}}}
    assert spoolman_link.parse_moonraker_spoolman_config(payload) == "http://nas.local:7912"
    direct = {"config": {"spoolman": {"server": "https://spool.example.com"}}}
    assert spoolman_link.parse_moonraker_spoolman_config(direct) == "https://spool.example.com"


def test_parse_moonraker_spoolman_config_absent_section_is_empty():
    assert spoolman_link.parse_moonraker_spoolman_config({"result": {"config": {}}}) == ""
    assert spoolman_link.parse_moonraker_spoolman_config({"result": {"config": {"spoolman": {}}}}) == ""
    assert spoolman_link.parse_moonraker_spoolman_config(None) == ""
    assert spoolman_link.parse_moonraker_spoolman_config("junk") == ""


def test_parse_active_spool_payload_shapes():
    assert spoolman_link.parse_active_spool_payload({"result": {"spool_id": 7}}) == 7
    assert spoolman_link.parse_active_spool_payload({"result": 7}) == 7
    assert spoolman_link.parse_active_spool_payload({"spool_id": "9"}) == 9
    assert spoolman_link.parse_active_spool_payload({"result": {"spool_id": None}}) is None
    assert spoolman_link.parse_active_spool_payload({"result": {"spool_id": True}}) is None
    assert spoolman_link.parse_active_spool_payload(None) is None


# --- Detection (checklist item 2: every Moonraker printer, not only
# OpenCreator) ---------------------------------------------------------------

def _transport(payloads, failing=()):
    def call(method, path):
        if path in failing:
            raise ConnectionError("printer unreachable")
        return payloads[path]
    return call


LINKED_CONFIG = {"/server/config": {"result": {"config": {
    "spoolman": {"server": "http://nas.local:7912"}}}},
    "/server/spoolman/spool_id": {"result": {"spool_id": 4}}}


def test_detect_linked_via_config_and_active_spool():
    found = spoolman_link.detect_spoolman_link(_transport(LINKED_CONFIG))
    assert found == {"linked": True, "server_url": "http://nas.local:7912",
                     "active_spool_id": 4, "error": ""}


def test_detect_linked_via_spool_id_endpoint_alone():
    payloads = {"/server/config": {"result": {"config": {}}},
                "/server/spoolman/spool_id": {"result": {"spool_id": 2}}}
    found = spoolman_link.detect_spoolman_link(_transport(payloads))
    assert found["linked"] is True and found["server_url"] == ""
    assert found["active_spool_id"] == 2 and found["error"] == ""


def test_detect_unlinked_when_neither_signal_answers():
    # No [spoolman] section in the config, and the spool_id endpoint does
    # not answer at all (Moonraker without the spoolman component 404s).
    payloads = {"/server/config": {"result": {"config": {}}}}
    found = spoolman_link.detect_spoolman_link(
        _transport(payloads, failing=("/server/spoolman/spool_id",)))
    assert found["linked"] is False and found["active_spool_id"] is None
    assert "unreachable" in found["error"]


def test_detect_linked_when_the_endpoint_answers_with_no_spool_set():
    # The endpoint answering at all means the spoolman component is
    # loaded, even when no spool is currently pointed at.
    payloads = {"/server/config": {"result": {"config": {}}},
                "/server/spoolman/spool_id": {"result": {"spool_id": None}}}
    found = spoolman_link.detect_spoolman_link(_transport(payloads))
    assert found["linked"] is True and found["active_spool_id"] is None


def test_detect_reports_an_error_only_when_nothing_came_back():
    found = spoolman_link.detect_spoolman_link(
        _transport({}, failing=("/server/config", "/server/spoolman/spool_id")))
    assert found["linked"] is False and "unreachable" in found["error"]
    partial = spoolman_link.detect_spoolman_link(
        _transport(LINKED_CONFIG, failing=("/server/spoolman/spool_id",)))
    assert partial["linked"] is True and partial["error"] == ""


# --- Authority resolution (checklist item 3) --------------------------------

def _linked(server="http://nas.local:7912"):
    return {"linked": True, "server_url": server, "active_spool_id": 1, "error": ""}


UNLINKED = {"linked": False, "server_url": "", "active_spool_id": None, "error": ""}


def test_explicit_setting_wins_over_detection():
    assert spoolman_link.resolve_accounting_authority(
        "print_handy", _linked())["authority"] == "print_handy"
    assert spoolman_link.resolve_accounting_authority(
        "printer", _linked())["authority"] == "printer"
    # Without a link, an explicit printer authority falls back to the only
    # available counter so the print is never silently unaccounted.
    assert spoolman_link.resolve_accounting_authority(
        "printer", UNLINKED)["authority"] == "print_handy"
    assert spoolman_link.resolve_accounting_authority(
        "print_handy", _linked())["source"] == "setting"


def test_auto_keeps_print_handy_counting_on_a_linked_printer_that_does_not_assign_spools():
    # The usual setup: Moonraker is linked, and Print Handy clears the active
    # spool for a multi-slot print and charges each slot itself. Handing that
    # print to the printer would count nothing.
    assert spoolman_link.resolve_accounting_authority(
        "auto", _linked())["authority"] == "print_handy"
    assert spoolman_link.resolve_accounting_authority(
        "", _linked())["authority"] == "print_handy"


def test_auto_follows_the_detected_link_when_the_firmware_assigns_spools():
    assert spoolman_link.resolve_accounting_authority(
        "auto", _linked(), firmware_assigns_spools=True)["authority"] == "printer"
    assert spoolman_link.resolve_accounting_authority(
        "auto", UNLINKED, firmware_assigns_spools=True)["authority"] == "print_handy"
    assert spoolman_link.resolve_accounting_authority(
        "", UNLINKED)["authority"] == "print_handy"
    assert spoolman_link.resolve_accounting_authority(
        "auto", UNLINKED)["source"] == "auto"


def test_same_instance_is_reported_when_both_urls_are_known():
    resolved = spoolman_link.resolve_accounting_authority(
        "auto", _linked(), configured_url="HTTP://NAS.local:7912/", firmware_assigns_spools=True)
    assert resolved["authority"] == "printer"
    assert resolved["same_instance"] is True and resolved["warnings"] == []


def test_print_handy_with_a_different_linked_instance_warns_about_double_counting():
    resolved = spoolman_link.resolve_accounting_authority(
        "print_handy", _linked("http://printer-side:7912"),
        configured_url="http://nas.local:7912",
        identity_probe=lambda url, path: ({} if path.endswith("info") else [{"id": 1, "registered": "2020-01-01"}] if "nas.local" in url else [{"id": 2, "registered": "2021-01-01"}]))
    assert resolved["same_instance"] is False
    assert any("counted twice" in warning for warning in resolved["warnings"])


def test_printer_authority_without_a_link_warns_and_falls_back_to_print_handy():
    resolved = spoolman_link.resolve_accounting_authority("printer", UNLINKED)
    assert resolved["authority"] == "print_handy"
    assert any("will count filament usage instead" in warning for warning in resolved["warnings"])


def test_printer_authority_with_a_different_instance_warns_the_weights_diverge():
    resolved = spoolman_link.resolve_accounting_authority(
        "printer", _linked("http://printer-side:7912"),
        configured_url="http://nas.local:7912",
        identity_probe=lambda url, path: ({} if path.endswith("info") else [{"id": 1 if "printer-side" in url else 2, "registered": "2024"}]))
    assert any("will not line up" in warning for warning in resolved["warnings"])


def test_accounting_authority_for_uses_settings_and_live_detection():
    data = settings.load(local=False, environ={}, user=False)
    data["services"]["spoolman_url"] = "http://nas.local:7912"
    settings.set_override(data)
    try:
        resolved = spoolman_link.accounting_authority_for(
            moonraker_call=_transport(LINKED_CONFIG))
        # The default printer's firmware does not assign spools: Print Handy keeps counting.
        assert resolved["authority"] == "print_handy" and resolved["same_instance"] is True
        from print_handy import printers
        opencreator = printers.get("flashforge_opencreator.creator5_pro", data)
        assert opencreator.firmware_assigns_spools is True
        handed = spoolman_link.accounting_authority_for(
            moonraker_call=_transport(LINKED_CONFIG), adapter=opencreator)
        assert handed["authority"] == "printer" and handed["source"] == "auto"
        offline = spoolman_link.accounting_authority_for()
        assert offline["authority"] == "print_handy" and offline["moonraker_linked"] is False
    finally:
        settings.set_override(None)


def test_only_a_real_true_hands_counting_to_the_firmware():
    from unittest import mock
    assert spoolman_link.firmware_assigns_spools(mock.Mock()) is False
    assert spoolman_link.firmware_assigns_spools(object()) is False
    assert spoolman_link.firmware_assigns_spools(mock.Mock(firmware_assigns_spools=True)) is True
    from print_handy.printers.base import PrinterAdapter
    from print_handy.printers.flashforge_opencreator.adapter import OpenCreatorAdapter
    assert PrinterAdapter.firmware_assigns_spools is False
    assert OpenCreatorAdapter.firmware_assigns_spools is True


def test_different_urls_with_matching_spoolman_fingerprint_are_same_instance():
    def probe(url, path):
        if path.endswith("info"):
            return {"version": "1.0"}
        return [{"id": 12, "registered": "2024-01-02T03:04:05"}]
    resolved = spoolman_link.resolve_accounting_authority(
        "print_handy", _linked("http://printer-side:7912"),
        configured_url="http://container-local:8000", identity_probe=probe)
    assert resolved["same_instance"] is True
    assert not any("counted twice" in warning for warning in resolved["warnings"])

def test_different_fingerprints_keep_double_count_warning():
    def probe(url, path):
        return {"version": "1.0"} if path.endswith("info") else [
            {"id": 1 if "printer-side" in url else 2, "registered": "2024-01-02T03:04:05"}]
    resolved = spoolman_link.resolve_accounting_authority(
        "print_handy", _linked("http://printer-side:7912"),
        configured_url="http://container-local:8000", identity_probe=probe)
    assert resolved["same_instance"] is False
    assert any("counted twice" in warning for warning in resolved["warnings"])

def test_failed_identity_probe_keeps_unverified_warning():
    def fail(url, path):
        raise ConnectionError("offline")
    resolved = spoolman_link.resolve_accounting_authority(
        "print_handy", _linked("http://printer-side:7912"),
        configured_url="http://container-local:8000", identity_probe=fail)
    assert resolved["same_instance"] is None
    assert any("could not confirm they are the same Spoolman" in warning
               and "may be counted twice" in warning for warning in resolved["warnings"])
    assert all("; " not in warning for warning in resolved["warnings"])


def test_warning_text_reads_as_full_sentences():
    def probe(url, path):
        return {"version": "1.0"} if path.endswith("info") else [
            {"id": 1 if "printer-side" in url else 2, "registered": "2024-01-02T03:04:05"}]
    counted = spoolman_link.resolve_accounting_authority(
        "print_handy", _linked("http://printer-side:7912"),
        configured_url="http://container-local:8000", identity_probe=probe)["warnings"]
    assert any("the same print can be counted twice, once by each side" in w for w in counted)
    split = spoolman_link.resolve_accounting_authority(
        "printer", _linked("http://printer-side:7912"),
        configured_url="http://container-local:8000", identity_probe=probe)["warnings"]
    assert any("two Spoolman instances are in play" in w for w in split)
