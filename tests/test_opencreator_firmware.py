"""OpenCreator firmware: reported, never applied (#1490).

Decided on #1490 (2026-11-15): OpenCreator updates are alert and report
only. The registry profile is a Klipper-family Moonraker printer, so
before this an update request was accepted for it, although an
OpenCreator update can flash the printer's boards.

The update status is a live capture shared on #1490
(tests/fixtures/opencreator/update_status.json).
"""
import json
from pathlib import Path

import pytest

from print_handy import firmware_update as fw

from test_firmware_update import ApplyTransport, entry  # noqa: E402

KEYS = ("flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro")
CAPTURE = Path(__file__).parent / "fixtures" / "opencreator" / "update_status.json"


def _payload():
    payload = json.loads(CAPTURE.read_text(encoding="utf-8"))
    return payload if "result" in payload else {"result": payload}


@pytest.mark.parametrize("key", KEYS)
def test_apply_is_refused_by_name_whatever_the_settings_say(key):
    reason = fw.apply_block_reason(entry(key), {"firmware": {"forgex_apply": True}})
    assert reason is not None
    assert "never applies" in reason and "Fluidd or Mainsail" in reason
    assert "flash the printer's boards" in reason
    assert not fw.is_apply_eligible(entry(key))


@pytest.mark.parametrize("component", ["opencreator", "klipper", "moonraker", "mainsail"])
def test_prepare_refuses_every_component_before_reading_the_printer(component):
    transport = ApplyTransport(_payload())
    with pytest.raises(fw.FirmwareRefusal, match="never applies"):
        fw.prepare_update(transport, entry(KEYS[1]), component)
    assert transport.calls == []


@pytest.mark.parametrize("key", KEYS)
def test_guidance_names_the_manual_path(key):
    guidance = fw.guidance_for(entry(key))
    assert guidance is fw.GUIDANCE["flashforge_opencreator"]
    text = fw.guidance_text(entry(key))
    assert "Fluidd or Mainsail" in text and "no version number" in text


def test_capture_parses_with_commits_behind():
    status = fw.parse_update_status(_payload())
    assert status.component("opencreator").current == "v0.0.0-8"
    assert status.component("opencreator").remote == "v0.0.0-26"
    assert status.component("opencreator").behind == 18
    assert status.component("klipper").invalid
    assert status.component("mainsail").behind is None


def test_report_states_no_version_and_commits_behind_and_offers_no_update():
    text = fw.report_text(fw.parse_update_status(_payload()), entry(KEYS[1]))
    assert ("OpenCreator (opencreator) publishes no version number. Moonraker reports its "
            "checkout as v0.0.0-8, 18 commits behind its source.") in text
    assert "/print-firmware update" not in text
    assert "does not apply firmware updates on this printer" in text
    # The Kalico checkout in the capture is dirty: Moonraker's refusal is shown.
    assert "Moonraker will not apply it" in text


def _status(**raw):
    base = {"configured_type": "git_repo", "version": "v0.0.0-8", "remote_version": "v0.0.0-8",
            "is_valid": True, "commits_behind": []}
    return fw.parse_update_status({"result": {"version_info": {"opencreator": {**base, **raw}}}})


def test_a_level_checkout_is_not_called_up_to_date_by_version():
    text = fw.report_text(_status(), entry(KEYS[0]))
    assert "checkout as v0.0.0-8, level with its source." in text


def test_one_commit_behind_is_singular():
    text = fw.report_text(_status(remote_version="v0.0.0-9", commits_behind=[{}]), entry(KEYS[0]))
    assert "1 commit behind its source" in text


@pytest.mark.parametrize("raw, expected", [
    ({"remote_version": "?"}, "does not say how far behind it is"),
    ({"commits_behind": None, "remote_version": "v0.0.0-9"}, "does not say how far behind it is"),
    ({"is_valid": False, "warnings": ["Repo is dirty"]}, "marks it not valid (Repo is dirty), so its state is unknown"),
])
def test_an_unreadable_state_is_unknown_never_current(raw, expected):
    text = fw.report_text(_status(**raw), entry(KEYS[0]))
    assert expected in text
    assert "level with its source" not in text


def test_the_sbc_edition_entry_name_is_recognized():
    status = fw.parse_update_status({"result": {"version_info": {"opencreator-updater": {
        "version": "v0.0.0-3", "remote_version": "v0.0.0-5", "commits_behind_count": 2}}}})
    assert "OpenCreator (opencreator-updater)" in fw.report_text(status, entry(KEYS[0]))
    assert "2 commits behind" in fw.report_text(status, entry(KEYS[0]))


def test_a_printer_without_the_entry_says_so():
    status = fw.parse_update_status({"result": {"version_info": {"klipper": {"version": "v1"}}}})
    assert "OpenCreator: not listed by this printer's Update Manager" in fw.report_text(
        status, entry(KEYS[0]))


def test_other_families_get_no_opencreator_line():
    text = fw.report_text(fw.parse_update_status(_payload()), entry("flashforge.ad5x"))
    assert "publishes no version number" not in text
    assert fw.is_apply_eligible(entry("flashforge.ad5x"))


def test_zero_commits_behind_with_a_different_remote_is_stated_as_unclear():
    text = fw.report_text(_status(remote_version="v0.0.0-9", commits_behind=[]), entry(KEYS[0]))
    assert "with no commits behind, but its source reads v0.0.0-9" in text
    assert "0 commits behind" not in text and "level with its source" not in text
