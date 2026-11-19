"""HelixScreen update alerts from the Moonraker component status."""
import asyncio
import copy
from types import SimpleNamespace as S

import pytest

from print_handy import firmware_alerts as fa
from print_handy import firmware_update as fw
from print_handy.bot import firmware_alert as fal

from test_firmware_alerts import AD5X, PID, Adapter, Clock


def payload(remote="1.0.9", is_valid=True):
    return {"result": {"version_info": {
        "Z-Mod": {"version": "1.7.5-2", "remote_version": "1.7.6-17",
                 "owner": "ghzserg", "repo": "z_ad5x", "channel": "dev",
                 "is_valid": True},
        "helixscreen": {"version": "v1.0.8", "remote_version": remote,
                        "is_valid": is_valid},
    }}}


class Printer:
    def __init__(self, status):
        self.payload = status
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "GET":
            raise AssertionError(f"unexpected {method} {path}")
        if path == fw.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        if path == fw.UPDATE_STATUS_PATH:
            return copy.deepcopy(self.payload)
        if path == fw.SYSTEM_INFO_PATH:
            return {"result": {"system_info": {"distribution": {
                "name": "zmod 1.7.5 -> 1.7.5-2", "version": "1.7.5"}}}}
        raise AssertionError(f"unexpected GET {path}")


def test_helixscreen_offer_is_applicable_and_has_product_heading():
    obs = fa.observe(Printer(payload()))
    offers = [o for o in fa.offers(obs) if o.component == "helixscreen"]
    [offer] = offers
    assert offer.applicable
    assert offer.key == "helixscreen:1.0.9"
    assert offer.text("Garage AD5X").startswith(
        "**HelixScreen update available for Garage AD5X:** v1.0.8 -> 1.0.9.")


@pytest.mark.parametrize("remote", ["1.0.8", "?", None])
def test_helixscreen_same_or_unknown_remote_does_not_alert(remote):
    status = payload(remote)
    if remote is None:
        del status["result"]["version_info"]["helixscreen"]["remote_version"]
    obs = fa.observe(Printer(status))
    assert not [o for o in fa.offers(obs) if o.component == "helixscreen"]


def test_helixscreen_invalid_component_has_block_and_no_button():
    [offer] = [o for o in fa.offers(fa.observe(Printer(payload(is_valid=False))))
               if o.component == "helixscreen"]
    assert not offer.applicable
    assert offer.block == fa.BLOCK_HELIXSCREEN_INVALID
    assert "HelixScreen" in offer.text("Garage AD5X")
    assert "Press **Update**" not in offer.text("Garage AD5X")


def test_alerter_announces_helixscreen_once_by_key(tmp_path):
    from test_firmware_alerts import _config
    cfg = copy.deepcopy(_config.cfg())
    cfg["paths"]["state_home"] = tmp_path / "home"
    cfg["printers"] = [dict(cfg["printers"][0], id=PID, model="flashforge.ad5x",
                             name="Garage AD5X")]
    printer = Printer(payload())
    clock = Clock()
    core = S(cfg=cfg, adapter=Adapter(), platform_name="discord", chat_id="chan",
             gateway=S(setup_only=False))
    al = fal.FirmwareAlerter(core, transport_for=lambda _pid: printer,
                             releases=S(get=lambda: None), clock=clock,
                             confirm_timeout_s=5)
    async def flow():
        found = await al.check(PID, AD5X)
        for task in list(al.tasks):
            task.cancel()
        await asyncio.gather(*list(al.tasks), return_exceptions=True)
        return found

    def helix_questions():
        return [q for q in core.adapter.questions
                if "• HelixScreen:" in q["question"]]

    first = asyncio.run(flow())
    assert [o.component for o in first].count("helixscreen") == 1
    assert len(helix_questions()) == 1
    assert helix_questions()[0]["choices"] == [fal.DEFER_LABEL]
    second = asyncio.run(flow())
    assert not [o for o in second if o.component == "helixscreen"]
    assert len(helix_questions()) == 1



def test_deferring_a_digest_does_not_skip_helixscreen(tmp_path):
    from test_firmware_alerts import _config, settle
    from print_handy.core import prompts
    cfg = copy.deepcopy(_config.cfg())
    cfg["paths"]["state_home"] = tmp_path / "home"
    cfg["printers"] = [dict(cfg["printers"][0], id=PID, model="flashforge.ad5x",
                             name="Garage AD5X")]
    core = S(cfg=cfg, adapter=Adapter(), platform_name="discord", chat_id="chan",
             gateway=S(setup_only=False))
    al = fal.FirmwareAlerter(core, transport_for=lambda _pid: Printer(payload()),
                             releases=S(get=lambda: None), clock=Clock(),
                             confirm_timeout_s=5)

    async def flow():
        await al.check(PID, AD5X)
        [digest] = core.adapter.questions
        assert "• HelixScreen:" in digest["question"]
        prompts.resolve_gateway_clarify(digest["id"], fal.DEFER_LABEL)
        await settle(al)

    asyncio.run(flow())
    from print_handy import settings
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert "helixscreen:1.0.9" not in state["announced"]
    assert "helixscreen:1.0.9" not in state.get("skipped", {})
