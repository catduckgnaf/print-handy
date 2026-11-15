"""Forge-X firmware updates (#1506 tranche 3), offline.

- Version ordering: Forge-X's prereleases are 1.4.2-beta-N, and beta-4 is
  older than beta-10 (both orderings, the alert's and the CI watch's).
- Guarded apply, shipped off: [firmware] forgex_apply false refuses every
  Forge-X apply; on, only the forge-x component, only within its version
  line (both versions readable, same major.minor), behind the existing
  confirmation, both idle checks and the verified re-read.
- The weekly watch follows Forge-X's final releases from 1.4.1.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from print_handy import firmware_alerts as fa
from print_handy import firmware_update as fw
from print_handy import settings

from test_firmware_update import ApplyTransport, entry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FORGEX = entry("flashforge_forgex.ad5m")
ON = {"firmware": {"forgex_apply": True}}
OFF = {"firmware": {"forgex_apply": False}}


def _watch():
    spec = importlib.util.spec_from_file_location(
        "check_firmware_releases", ROOT / "scripts/check_firmware_releases.py")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def _status(current, remote, name="forge-x"):
    return {"result": {"version_info": {
        name: {"version": current, "remote_version": remote, "is_valid": True},
        "fluidd": {"version": "v1.30.0", "remote_version": "v1.31.0"},
    }}}


# ------------------------------------------------------------------ ordering

@pytest.mark.parametrize("key", [fa.version_key, lambda v: _watch().firmware_version_key(v)])
def test_beta_builds_order_by_their_number(key):
    order = ["1.4.1", "1.4.2-beta-2", "1.4.2-beta-4", "1.4.2-beta-10", "1.4.2"]
    assert sorted(order, key=key) == order
    assert key("1.4.2-beta-4") < key("1.4.2-beta-10")


def test_the_two_orderings_still_agree():
    watch = _watch()
    for version in ("1.4.2-beta-4", "1.4.2-beta-10", "1.7.6-beta.2", "1.7.6-rc1", "1.7.5-2"):
        assert fa.version_key(version) == watch.firmware_version_key(version), version


# ------------------------------------------------------------------ guarded apply

def test_off_by_default_refuses_before_reading_anything():
    transport = ApplyTransport(status=_status("1.4.2-beta-4", "1.4.2-beta-10"))
    assert not fw.is_apply_eligible(FORGEX)
    assert not fw.is_apply_eligible(FORGEX, OFF)
    with pytest.raises(fw.FirmwareRefusal, match="forgex_apply"):
        fw.prepare_update(transport, FORGEX, "forge-x", OFF)
    assert transport.calls == []


def test_on_prepares_a_same_line_forge_x_update():
    transport = ApplyTransport(status=_status("1.4.2-beta-4", "1.4.2-beta-10"))
    prepared = fw.prepare_update(transport, FORGEX, "forge-x", ON)
    assert prepared.component.name == "forge-x"
    assert prepared.component.remote == "1.4.2-beta-10"
    assert transport.posts == []


def test_a_new_version_line_is_refused_as_a_usb_reflash():
    transport = ApplyTransport(status=_status("1.4.2-beta-10", "1.5.0"))
    with pytest.raises(fw.FirmwareRefusal, match="USB re-flash"):
        fw.prepare_update(transport, FORGEX, "forge-x", ON)
    assert transport.posts == []


@pytest.mark.parametrize("current,remote", [("abc1234-dirty", "1.4.2-beta-10"),
                                            ("1.4.2-beta-4", "main-77")])
def test_an_unreadable_version_refuses(current, remote):
    transport = ApplyTransport(status=_status(current, remote))
    with pytest.raises(fw.FirmwareRefusal, match="cannot read both"):
        fw.prepare_update(transport, FORGEX, "forge-x", ON)


def test_other_components_are_left_to_forge_x():
    transport = ApplyTransport(status=_status("1.4.2-beta-4", "1.4.2-beta-10"))
    with pytest.raises(fw.FirmwareRefusal, match="only the \\*\\*forge-x\\*\\* component"):
        fw.prepare_update(transport, FORGEX, "fluidd", ON)


def test_confirmed_update_sends_one_upgrade_and_verifies():
    after = _status("1.4.2-beta-10", "1.4.2-beta-10")
    transport = ApplyTransport(status=_status("1.4.2-beta-4", "1.4.2-beta-10"), post_status=after)

    async def ask(_question, _choices):
        return fw.UPDATE_LABEL

    async def run():
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fw.time, "sleep", lambda _s: None)
            return await fw.confirmed_update(transport, FORGEX, "forge-x", ask, ON)

    message = asyncio.run(run())
    assert [path for path, _ in transport.posts] == [fw.UPDATE_UPGRADE_PATH]
    assert "verified" in message


def test_cancel_sends_nothing():
    transport = ApplyTransport(status=_status("1.4.2-beta-4", "1.4.2-beta-10"))

    async def ask(_question, _choices):
        return fw.CANCEL_LABEL

    message = asyncio.run(fw.confirmed_update(transport, FORGEX, "forge-x", ask, ON))
    assert transport.posts == [] and message == fw.NOT_APPLIED_TEXT


def test_zmod_eligibility_is_unchanged_by_the_setting():
    zmod = entry("flashforge.ad5x")
    assert fw.is_apply_eligible(zmod) and fw.is_apply_eligible(zmod, OFF)


def test_setting_defaults_off_and_must_be_a_bool():
    loaded = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge_forgex.ad5m"})
    assert loaded["firmware"]["forgex_apply"] is False
    with pytest.raises(settings.SettingsError, match="(?i)forgex_apply"):
        settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge_forgex.ad5m",
                                            "PRINT_HANDY__FIRMWARE__FORGEX_APPLY": "maybe"})


# ------------------------------------------------------------------ watch entry

def test_the_weekly_watch_follows_forge_x_finals_from_1_4_1():
    watched = {w.name: w for w in _watch().read_baselines(ROOT)}
    forgex = watched["Forge-X"]
    assert (forgex.repo, forgex.feed, forgex.baseline) == ("DrA1ex/ff5m", "releases", "1.4.1")
    assert "#1506" in forgex.baseline_source
