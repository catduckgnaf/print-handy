"""Audit evidence for OpenCreator 0.0.7.4 through 0.0.7.6.

Release notes state 0.0.7.5 and 0.0.7.6 retain the 0.0.7.4 MCU firmware;
their changes are host runtime and HelixScreen installation/update behavior.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "tests" / "fixtures" / "opencreator" / "releases.json"


def test_release_fixture_has_exact_order_versions_and_tags():
    evidence = json.loads(CAPTURE.read_text(encoding="utf-8"))
    assert [item["tag"] for item in evidence["releases"]] == ["0.0.7.4", "0.0.7.5", "0.0.7.6"]
    assert [item["tag_commit"] for item in evidence["releases"]] == [
        "d3e9125a4f45a2ab832abf478d254bd9b43780fb",
        "08ee1668ac3e60d8ae296005330596c69467a600",
        "30d58984465bb55c61b437865b9e4b794cc4b666",
    ]
    assert all(item["url"].endswith("/tag/" + item["tag"]) for item in evidence["releases"])


def test_versions_75_and_76_do_not_claim_mcu_contract_changes():
    evidence = json.loads(CAPTURE.read_text(encoding="utf-8"))
    assert evidence["releases"][1]["mcu_firmware"] == "Kalico 7ce49713 (unchanged from 0.0.7.4)"
    assert evidence["releases"][2]["mcu_firmware"] == "Kalico 7ce49713 (unchanged from 0.0.7.4 and 0.0.7.5)"
    assert "network time at boot" in evidence["releases"][1]["scope"]
    assert "HelixScreen updater beta channel" in evidence["releases"][2]["scope"]
    assert evidence["audit"]["hardware_unverified"]


def test_audit_does_not_treat_upstream_runout_report_as_ph_verified_behavior():
    evidence = json.loads(CAPTURE.read_text(encoding="utf-8"))
    assert "AFC and ACE runout failover and dock temperatures" in evidence["audit"]["hardware_unverified"]
    assert "Print Handy runout alert behavior during a physical print" in evidence["audit"]["hardware_unverified"]
    assert "No Print Handy contract change established" in evidence["audit"]["conclusion"]
