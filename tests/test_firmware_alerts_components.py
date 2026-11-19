"""Generic Moonraker component update alerts (#1280 section I).

The ZMod and HelixScreen sources have their own files
(tests/test_firmware_alerts.py, tests/test_firmware_alerts_helixscreen.py);
this one covers the generic source: every other Update Manager
component with a real available version, on every apply-eligible
printer, with the Update button running the existing confirmed apply.

Offline: a fake Moonraker transport replays a vanilla Klipper host's
status shape (klipper / moonraker / fluidd components, a `system`
component reporting package counts), and the same platform fake as the
ZMod alert tests records the questions.
"""
import asyncio
import copy
from types import SimpleNamespace as S

import pytest

from print_handy import firmware_alerts as fa
from print_handy import firmware_update as fw
from print_handy import settings
from print_handy.bot import firmware_alert as fal
from print_handy.core import prompts

from test_firmware_alerts import Adapter, Clock, _config, settle, wait_for

GENERIC = fw.printer_entry("klipper.generic")
FORGEX = fw.printer_entry("flashforge_forgex.ad5m")
PID = "klipper-generic"

SYSINFO = {"result": {"system_info": {"distribution": {
    "name": "Debian GNU/Linux", "version": "12"}}}}


def payload(components=None):
    if components is None:
        components = {
            "klipper": {"version": "v0.12.0", "remote_version": "v0.13.0",
                        "is_valid": True},
            "moonraker": {"version": "v0.8.0", "remote_version": "v0.9.0",
                          "is_valid": True},
            "fluidd": {"version": "v1.29.0", "remote_version": "v1.30.0",
                       "is_valid": True},
            "mainsail": {"version": "v2.5.0", "remote_version": "v2.5.0",
                         "is_valid": True},
            "system": {"package_count": 4, "package_list": []},
        }
    return {"result": {"version_info": components}}


class Printer:
    """A vanilla Klipper Moonraker: status, system info, print state, upgrades."""

    def __init__(self, status_payload=None, *, error=None):
        self.payload = payload() if status_payload is None else status_payload
        self.error = error
        self.calls = []
        self.posts = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if self.error is not None:
            raise self.error
        if path == fw.UPDATE_UPGRADE_PATH:
            self.posts.append(body)
            component = self.payload["result"]["version_info"][body["name"]]
            component["version"] = component["remote_version"]
            return {"result": "ok"}
        if method != "GET":
            raise AssertionError(f"unexpected {method} {path}")
        if path == fw.UPDATE_STATUS_PATH:
            return copy.deepcopy(self.payload)
        if path == fw.SYSTEM_INFO_PATH:
            return copy.deepcopy(SYSINFO)
        if path == fw.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        raise AssertionError(f"unexpected GET {path}")


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["paths"]["state_home"] = tmp_path / "home"
    data["printers"] = [dict(data["printers"][0], id=PID, model="klipper.generic",
                             name="Shop Klipper")]
    return data


def alerter(cfg, printer, adapter=None, clock=None):
    core = S(cfg=cfg, adapter=adapter or Adapter(), platform_name="discord",
             chat_id="chan", gateway=S(setup_only=False))
    return fal.FirmwareAlerter(core, transport_for=lambda pid: printer,
                               releases=S(get=lambda: None),
                               clock=clock or Clock(), confirm_timeout_s=5)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ detection

def test_components_with_real_versions_offer_with_buttons():
    obs = fa.observe(Printer(), vendor="klipper")
    offers = fa.component_offers(obs)
    assert {o.component: o for o in offers} == {
        "klipper": fa.Offer("klipper", "v0.12.0", "v0.13.0", "klipper:0.13.0",
                            None, "Klipper"),
        "moonraker": fa.Offer("moonraker", "v0.8.0", "v0.9.0", "moonraker:0.9.0",
                              None, "Moonraker"),
        "fluidd": fa.Offer("fluidd", "v1.29.0", "v1.30.0", "fluidd:1.30.0",
                           None, "Fluidd"),
    }
    # mainsail is up to date and system reports packages, not a version:
    # neither alerts. Every offer is applicable (carries the button).
    assert all(o.applicable for o in offers)
    klipper = offers[0]
    assert klipper.text("Shop Klipper").startswith(
        "**Klipper update available for Shop Klipper:** v0.12.0 -> v0.13.0.")
    assert "/print-firmware update klipper" in klipper.text("Shop Klipper",
                                                           buttons=False)


def test_zmod_component_names_resolve_to_the_canonical_token():
    """ZMod names its Klipper `klippy`: the offer is for `klipper`, the
    token the confirmed apply resolves against the live status."""
    obs = fa.observe(Printer(payload({
        "klippy": {"version": "v0.12.0", "remote_version": "v0.13.0",
                   "is_valid": True}})), vendor="flashforge")
    [offer] = fa.component_offers(obs)
    assert offer.component == "klipper" and offer.key == "klipper:0.13.0"


def test_two_live_names_for_one_component_offer_once():
    obs = fa.observe(Printer(payload({
        "klipper": {"version": "v0.12.0", "remote_version": "v0.13.0",
                    "is_valid": True},
        "klippy": {"version": "v0.12.0", "remote_version": "v0.13.0",
                   "is_valid": True}})), vendor="klipper")
    assert [o.component for o in fa.component_offers(obs)] == ["klipper"]


@pytest.mark.parametrize("remote", ["?", None])
def test_unknown_remote_does_not_alert(remote):
    components = {"klipper": {"version": "v0.12.0", "is_valid": True}}
    if remote is not None:
        components["klipper"]["remote_version"] = remote
    obs = fa.observe(Printer(payload(components)), vendor="klipper")
    assert fa.component_offers(obs) == []


@pytest.mark.parametrize("current,remote", [
    ("v0.13.0", "v0.13.0-rc1"),   # a prerelease of the installed release
    ("v0.13.0", "v0.12.0"),       # older than installed
    ("v0.13.0", "v0.13.0"),       # the installed version itself
])
def test_not_newer_remote_does_not_alert(current, remote):
    obs = fa.observe(Printer(payload({
        "klipper": {"version": current, "remote_version": remote,
                    "is_valid": True}})), vendor="klipper")
    assert fa.component_offers(obs) == []


def test_invalid_component_is_announced_without_a_button():
    obs = fa.observe(Printer(payload({
        "fluidd": {"version": "v1.29.0", "remote_version": "v1.30.0",
                   "is_valid": False, "warnings": ["repo is dirty"]}})),
        vendor="klipper")
    [offer] = fa.component_offers(obs)
    assert not offer.applicable
    assert offer.block == fa.BLOCK_COMPONENT_INVALID
    text = offer.text("Shop Klipper")
    assert "not valid" in text and "Press **Update**" not in text


def test_opencreator_components_and_printers_never_offer():
    obs = fa.observe(Printer(payload({
        "opencreator": {"version": "1.0", "remote_version": "2.0",
                        "is_valid": True},
        "klipper": {"version": "v0.12.0", "remote_version": "v0.13.0",
                    "is_valid": True}})), vendor="klipper")
    assert [o.component for o in fa.component_offers(obs)] == ["klipper"]
    obs = fa.observe(Printer(), vendor=fw.OPENCREATOR_VENDOR)
    assert fa.component_offers(obs) == []


def test_forgex_offers_only_forgex_within_its_line():
    obs = fa.observe(Printer(payload({
        "forge-x": {"version": "1.4.2-beta-2", "remote_version": "1.4.2-beta-4",
                    "is_valid": True},
        "fluidd": {"version": "v1.29.0", "remote_version": "v1.30.0",
                   "is_valid": True}})), vendor=fw.FORGEX_VENDOR)
    [offer] = fa.component_offers(obs)
    assert offer.component == "forgex" and offer.display_name == "Forge-X"
    assert offer.applicable
    new_line = fa.observe(Printer(payload({
        "forge-x": {"version": "1.4.2-beta-2", "remote_version": "1.5.0-beta-1",
                    "is_valid": True}})), vendor=fw.FORGEX_VENDOR)
    assert fa.component_offers(new_line) == []


def test_forgex_printers_alert_only_when_forgex_apply_is_on(cfg):
    cfg["printers"] = [dict(cfg["printers"][0], id="forgex-ad5m",
                            model="flashforge_forgex.ad5m", name="Forge AD5M")]
    assert fal.alert_printers(cfg) == []
    cfg["firmware"]["forgex_apply"] = True
    assert [entry.key for _pid, entry in fal.alert_printers(cfg)] == [
        "flashforge_forgex.ad5m"]


# ------------------------------------------------------------------ the alert flow

def test_one_alert_per_version_survives_a_restart(cfg):
    clock = Clock()
    first = alerter(cfg, Printer(), clock=clock)

    async def flow(al):
        found = await al.check(PID, GENERIC)
        for task in list(al.tasks):
            task.cancel()
        await settle(al)
        return found

    assert {o.component for o in run(flow(first))} == {
        "klipper", "moonraker", "fluidd"}
    assert len(first.core.adapter.questions) == 1
    assert "(ZMod)" not in first.core.adapter.questions[0]["question"]
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert state["announced"]["klipper:0.13.0"]["available"] == "v0.13.0"
    # A restart: a new alerter, the same state folder, the same updates.
    second = alerter(cfg, Printer(), clock=clock)
    assert run(flow(second)) == []
    assert second.core.adapter.questions == [] and second.core.adapter.sent == []


def test_a_network_failure_sends_no_alert_and_fails_soft(cfg):
    al = alerter(cfg, Printer(error=OSError("unreachable")))
    assert run(al.check(PID, GENERIC)) == []
    assert al.core.adapter.questions == [] and al.core.adapter.sent == []
    with pytest.raises(fw.FirmwareError):
        fa.observe(Printer(error=OSError("unreachable")))


def test_update_press_runs_the_confirmed_apply(cfg):
    printer = Printer(payload({"klipper": {"version": "v0.12.0", "remote_version": "v0.13.0"}}))
    al = alerter(cfg, printer)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, GENERIC)
        alert = adapter.questions[0]
        assert alert["choices"] == ["Update klipper", fal.DEFER_LABEL]
        assert "Shop Klipper" in alert["question"]
        assert "v0.12.0 → v0.13.0" in alert["question"]
        assert printer.posts == []  # the alert itself sends nothing
        prompts.resolve_gateway_clarify(alert["id"], "Update klipper")
        await wait_for(lambda: len(adapter.questions) == 2)
        confirm = adapter.questions[-1]
        # The existing confirmation, unchanged: the exact one-component change.
        assert confirm["question"].startswith("Update printer firmware?")
        assert "**klipper**: v0.12.0 -> v0.13.0" in confirm["question"]
        assert confirm["choices"] == [fw.UPDATE_LABEL, fw.CANCEL_LABEL]
        assert printer.posts == []  # still nothing until the confirmation
        prompts.resolve_gateway_clarify(confirm["id"], fw.UPDATE_LABEL)
        await wait_for(lambda: bool(printer.posts))
        for task in list(al.tasks):
            task.cancel()
        await settle(al)

    run(flow())
    assert printer.posts == [{"name": "klipper"}]
    assert any("Updated **klipper** to v0.13.0" in text
               for _chat, text in adapter.sent)


def test_deferring_a_multi_component_digest_keeps_every_release_pending(cfg):
    clock = Clock()
    printer = Printer()
    al = alerter(cfg, printer, clock=clock)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, GENERIC)
        [digest] = adapter.questions
        assert digest["choices"] == [fal.DEFER_LABEL]
        prompts.resolve_gateway_clarify(digest["id"], fal.DEFER_LABEL)
        await settle(al)

    run(flow())
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert state["defer_updates_until"] == clock.now + 7 * 86400
    assert not state["announced"] and not state.get("skipped")
    assert not printer.posts


def test_zmod_printer_alerts_its_other_components_too():
    """A ZMod printer whose Moonraker reports a real Klipper remote gets
    a Klipper offer beside the ZMod source's own offers."""
    zmod_payload = {"result": {"version_info": {
        "Z-Mod": {"version": "1.7.6-17", "remote_version": "1.7.6-17",
                  "is_valid": True},
        "klippy": {"version": "v0.12.0", "remote_version": "v0.13.0",
                   "is_valid": True}}}}
    obs = fa.observe(Printer(zmod_payload), vendor="flashforge")
    assert [(o.component, o.key) for o in fa.offers(obs)] == [
        ("klipper", "klipper:0.13.0")]
