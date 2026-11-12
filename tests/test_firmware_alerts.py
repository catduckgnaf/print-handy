"""ZMod update alerts ([firmware] update_alerts): detection, one alert per
release, the Update button into the confirmed apply, and the no-button cases.

Offline: a fake Moonraker transport replays recorded ZMod shapes (the live
AD5X reports a ``Z-Mod`` git_repo component, version 1.7.5-2, remote
1.7.6-17) and a fake GitHub opener answers the releases read. The adapter
is a minimal platform fake; per-transport rendering is in
tests/bot/test_firmware_alert_transports.py.
"""
import asyncio
import copy
import importlib.util
import io
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import firmware_alerts as fa
from print_handy import firmware_update as fw
from print_handy import knowledge, settings
from print_handy.bot import firmware_alert as fal
from print_handy.bot import routing
from print_handy.bot.platform import SendResult
from print_handy.core import prompts
from print_handy.hermes import _config

ROOT = Path(__file__).parents[1]
AD5X = fw.printer_entry("flashforge.ad5x")
PID = "flashforge-ad5x"


def run(coro):
    return asyncio.run(coro)


def status(version="1.7.5-2", remote="1.7.6-17", **extra):
    zmod = {"version": version, "remote_version": remote, "owner": "ghzserg",
            "repo": "z_ad5x", "channel": "dev", "is_valid": True, **extra}
    return {"result": {"version_info": {
        "Z-Mod": zmod,
        "klippy": {"version": "?", "remote_version": "?"},
        "system": {"package_count": 0, "package_list": []}}}}


SYSINFO = {"result": {"system_info": {"distribution": {
    "name": "zmod 1.7.5 -> 1.7.5-2", "version": "1.7.5"}}}}


class Printer:
    """A ZMod Moonraker: status, system info, print state, and the one upgrade POST."""

    def __init__(self, payload=None, *, states=("standby",), system_info=SYSINFO, error=None):
        self.payload = status() if payload is None else payload
        self.system_info = system_info
        self.states = list(states)
        self.error = error
        self.calls = []
        self.posts = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if self.error is not None:
            raise self.error
        if path == fw.UPDATE_UPGRADE_PATH:
            self.posts.append(body)
            zmod = self.payload["result"]["version_info"]["Z-Mod"]
            zmod["version"] = zmod["remote_version"]
            return {"result": "ok"}
        if method != "GET":
            raise AssertionError(f"unexpected {method} {path}")
        if path == fw.UPDATE_STATUS_PATH:
            return copy.deepcopy(self.payload)
        if path == fw.SYSTEM_INFO_PATH:
            if self.system_info is None:
                raise OSError("no system info")
            return self.system_info
        if path == fw.PRINT_STATE_PATH:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        raise AssertionError(f"unexpected GET {path}")


class Adapter:
    """Records sends and button questions; presses are resolved by the test."""

    name = "fake"

    def __init__(self, buttons=True, result=None):
        self.supports_buttons = buttons
        self.result = result or SendResult(True, message_id="1")
        self.sent = []
        self.questions = []
        self.closed = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        return self.result

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key,
                           metadata=None):
        self.questions.append({"chat": chat_id, "question": question,
                               "choices": list(choices), "id": clarify_id})
        return self.result

    async def close_prompt(self, clarify_id, note):
        self.closed.append((clarify_id, note))


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["paths"]["hermes_home"] = tmp_path / "home"
    data["printers"] = [dict(data["printers"][0], id=PID, model="flashforge.ad5x",
                             name="Garage AD5X")]
    return data


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def alerter(cfg, printer, adapter=None, *, release=None, clock=None):
    core = S(cfg=cfg, adapter=adapter or Adapter(), platform_name="discord", chat_id="chan",
             gateway=S(setup_only=False))
    return fal.FirmwareAlerter(core, transport_for=lambda pid: printer,
                               releases=S(get=lambda: release), clock=clock or Clock(),
                               confirm_timeout_s=5)


async def settle(al):
    while al.tasks:
        await asyncio.gather(*list(al.tasks), return_exceptions=True)


async def wait_for(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


# ------------------------------------------------------------------ versions

def test_version_order_matches_the_ci_release_watch():
    spec = importlib.util.spec_from_file_location(
        "check_firmware_releases", ROOT / "scripts/check_firmware_releases.py")
    watch = sys.modules.get(spec.name) or importlib.util.module_from_spec(spec)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = watch
        spec.loader.exec_module(watch)
    versions = ["1.7.5", "1.7.5-2", "1.7.6-rc1", "1.7.6", "1.7.6-17", "1.9.9", "1.9.15",
                "v1.8.0", "2.0", "1.7.6-beta.2"]
    for version in versions:
        assert fa.version_key(version) == watch.firmware_version_key(version), version
    assert fa.version_key("1.7.5") < fa.version_key("1.7.5-2") < fa.version_key("1.7.6")
    assert fa.release_of("1.7.6-17") == "1.7.6"


# ------------------------------------------------------------------ detection

def test_moonraker_real_remote_gives_one_applicable_offer():
    obs = fa.observe(Printer())
    assert (obs.current, obs.remote, obs.valid, obs.base) == ("1.7.5-2", "1.7.6-17", True, "1.7.5")
    [offer] = fa.offers(obs)
    assert (offer.current, offer.available, offer.key) == ("1.7.5-2", "1.7.6-17", "zmod:1.7.6")
    assert offer.applicable


def test_observe_is_read_only_and_never_refreshes():
    printer = Printer()
    fa.observe(printer)
    assert all(method == "GET" for method, _ in printer.calls)
    assert (("POST", fw.UPDATE_REFRESH_PATH) not in printer.calls)


def test_builds_inside_the_installed_release_do_not_alert():
    assert fa.offers(fa.observe(Printer(status("1.7.6-3", "1.7.6-17")))) == []
    assert fa.offers(fa.observe(Printer(status("1.7.6", "1.7.6")))) == []


def test_question_mark_remote_falls_back_to_the_github_release_with_no_button():
    obs = fa.observe(Printer(status(remote="?")), latest_release="1.7.6")
    assert obs.remote is None
    [offer] = fa.offers(obs)
    assert offer.available == "1.7.6" and offer.block == fa.BLOCK_UNREPORTED
    text = offer.text("Garage AD5X")
    assert "Not from chat" in text and "Fluidd or Mainsail: Settings, Software Update" in text
    assert "Press **Update**" not in text


def test_question_mark_remote_and_no_release_alerts_nothing():
    assert fa.offers(fa.observe(Printer(status(remote="?")), latest_release=None)) == []


def test_an_invalid_component_says_why_and_has_no_button():
    [offer] = fa.offers(fa.observe(Printer(status(is_valid=False, is_dirty=True))))
    assert offer.block == fa.BLOCK_INVALID
    assert "not valid" in offer.text("Garage AD5X")


def test_a_release_on_a_new_base_is_usb_only():
    [offer] = fa.offers(fa.observe(Printer(status("1.7.6-17", "1.7.6-17")),
                                   latest_release="1.8.0"))
    assert offer.available == "1.8.0" and offer.block == fa.BLOCK_USB
    assert "ZFLASH" in offer.text("Garage AD5X")


def test_moonraker_and_release_for_the_same_version_are_one_offer():
    [offer] = fa.offers(fa.observe(Printer(), latest_release="1.7.6"))
    assert offer.available == "1.7.6-17" and offer.applicable


def test_both_a_fluidd_update_and_a_usb_release_are_offered_separately():
    found = fa.offers(fa.observe(Printer(), latest_release="1.8.0"))
    assert [(o.key, o.block) for o in found] == [("zmod:1.7.6", None), ("zmod:1.8.0", fa.BLOCK_USB)]


def test_a_base_pairing_mismatch_is_usb_first():
    # The printer's OS base was moved to 1.8 from USB, but the zmod component
    # still reports 1.7: ZMod's pairing rule fails, so the base comes first.
    info = {"result": {"system_info": {"distribution": {"name": "zmod 1.8.0 -> 1.7.5-2",
                                                        "version": "1.8.0"}}}}
    [offer] = fa.offers(fa.observe(Printer(status("1.7.5-2", "1.8.0-3"), system_info=info)))
    assert offer.block == fa.BLOCK_PAIRING
    assert "ZFLASH" in offer.text("Garage AD5X")


def test_a_moonraker_remote_on_another_base_is_usb_only():
    [offer] = fa.offers(fa.observe(Printer(status("1.7.5-2", "1.8.0-3"))))
    assert offer.block == fa.BLOCK_USB


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_latest_release_ignores_prereleases_and_drafts():
    releases = [{"tag_name": "1.8.0-rc1", "prerelease": True},
                {"tag_name": "1.9.0", "draft": True},
                {"tag_name": "1.7.6", "prerelease": False},
                {"tag_name": "1.7.5", "prerelease": False}]
    seen = []

    def opener(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        return _Resp(json.dumps(releases).encode())

    assert fa.latest_release(opener) == "1.7.6"
    [(url, auth, timeout)] = seen
    assert url == fa.ZMOD_RELEASES_URL and auth is None and timeout == fa.GITHUB_TIMEOUT_S


@pytest.mark.parametrize("failure", [OSError("offline"), TimeoutError("slow"), ValueError("bad")])
def test_latest_release_fails_soft(failure):
    def opener(request, timeout=None):
        raise failure

    assert fa.latest_release(opener) is None
    assert fa.latest_release(lambda r, timeout=None: _Resp(b"{not json")) is None


def test_release_cache_reads_github_once_per_hour():
    calls = []
    now = [0.0]
    cache = fa.ReleaseCache(fetch=lambda: calls.append(1) or "1.7.6", clock=lambda: now[0])
    assert cache.get() == "1.7.6" and cache.get() == "1.7.6"
    now[0] = fa.RELEASE_CACHE_S + 1
    cache.get()
    assert len(calls) == 2


# ------------------------------------------------------------------ the alert flow

def test_one_alert_per_release_survives_a_restart(cfg):
    clock = Clock()
    first = alerter(cfg, Printer(), clock=clock)

    async def flow(al):
        found = await al.check(PID, AD5X)
        for task in list(al.tasks):
            task.cancel()
        await settle(al)
        return found

    assert len(run(flow(first))) == 1
    assert len(first.core.adapter.questions) == 1
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert state["announced"]["zmod:1.7.6"]["available"] == "1.7.6-17"
    assert state["next_check_at"] > clock.now
    # A restart: a new alerter, the same state folder, the same update.
    second = alerter(cfg, Printer(), clock=clock)
    assert run(flow(second)) == []
    assert second.core.adapter.questions == [] and second.core.adapter.sent == []


def test_a_failed_delivery_is_not_remembered(cfg):
    al = alerter(cfg, Printer(), Adapter(result=SendResult(False, error="down")))
    run(al.check(PID, AD5X))
    assert "announced" not in fa.load(settings.printer_state_dir(PID, cfg))


def test_update_press_runs_the_confirmed_apply(cfg):
    printer = Printer()
    al = alerter(cfg, printer)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, AD5X)
        alert = adapter.questions[0]
        assert alert["choices"] == [fal.UPDATE_LABEL, fal.SKIP_LABEL]
        assert "Garage AD5X" in alert["question"] and "1.7.5-2 -> 1.7.6-17" in alert["question"]
        assert printer.posts == []  # the alert itself sends nothing
        prompts.resolve_gateway_clarify(alert["id"], fal.UPDATE_LABEL)
        await wait_for(lambda: len(adapter.questions) == 2)
        confirm = adapter.questions[1]
        # The existing confirmation, unchanged: the exact one-component change.
        assert confirm["question"].startswith("Update printer firmware?")
        assert "**Z-Mod**: 1.7.5-2 -> 1.7.6-17" in confirm["question"]
        assert confirm["choices"] == [fw.UPDATE_LABEL, fw.CANCEL_LABEL]
        assert printer.posts == []  # still nothing until the confirmation
        prompts.resolve_gateway_clarify(confirm["id"], fw.UPDATE_LABEL)
        await settle(al)

    run(flow())
    assert printer.posts == [{"name": "Z-Mod"}]
    assert "Updated **Z-Mod** to 1.7.6-17" in adapter.sent[-1][1]


def test_cancel_at_the_confirmation_sends_nothing(cfg):
    printer = Printer()
    al = alerter(cfg, printer)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, AD5X)
        prompts.resolve_gateway_clarify(adapter.questions[0]["id"], fal.UPDATE_LABEL)
        await wait_for(lambda: len(adapter.questions) == 2)
        prompts.resolve_gateway_clarify(adapter.questions[1]["id"], fw.CANCEL_LABEL)
        await settle(al)

    run(flow())
    assert printer.posts == []
    assert adapter.sent[-1][1] == fw.NOT_APPLIED_TEXT


def test_update_press_while_printing_is_refused_by_the_confirmed_path(cfg):
    printer = Printer(states=("standby", "printing"))
    al = alerter(cfg, printer)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, AD5X)
        prompts.resolve_gateway_clarify(adapter.questions[0]["id"], fal.UPDATE_LABEL)
        await settle(al)

    run(flow())
    assert printer.posts == [] and len(adapter.questions) == 1
    assert "printing" in adapter.sent[-1][1] and "Nothing was sent" in adapter.sent[-1][1]


def test_skip_this_version_is_remembered(cfg):
    clock = Clock()
    al = alerter(cfg, Printer(), clock=clock)
    adapter = al.core.adapter

    async def flow():
        await al.check(PID, AD5X)
        prompts.resolve_gateway_clarify(adapter.questions[0]["id"], fal.SKIP_LABEL)
        await settle(al)

    run(flow())
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert "zmod:1.7.6" in state["skipped"]
    assert "Skipped" in adapter.closed[-1][1]
    assert fa.to_announce([fa.Offer("zmod", "1.7.5-2", "1.7.6-20", "zmod:1.7.6")], state) == []


def test_unpressed_alert_expires_without_sending(cfg, monkeypatch):
    monkeypatch.setattr(fal, "interval_s", lambda cfg: 0.05)
    printer = Printer()
    al = alerter(cfg, printer)

    async def flow():
        await al.check(PID, AD5X)
        await settle(al)

    run(flow())
    assert printer.posts == []
    assert "expired" in al.core.adapter.closed[-1][1]


def test_no_button_cases_post_plain_text(cfg):
    al = alerter(cfg, Printer(status(remote="?")), release="1.7.6")
    run(al.check(PID, AD5X))
    adapter = al.core.adapter
    assert adapter.questions == []
    [(chat, text)] = adapter.sent
    assert chat == (routing.chat_for_printer(cfg, "discord", PID) or "chan")
    assert "Not from chat" in text and "1.7.5-2 -> 1.7.6" in text


def check_now(al):
    async def flow():
        found = await al.check(PID, AD5X)
        for task in list(al.tasks):
            task.cancel()
        await settle(al)
        return found
    return run(flow())


def test_a_no_button_release_is_announced_again_once_it_becomes_applicable(cfg):
    clock = Clock()
    assert len(check_now(alerter(cfg, Printer(status(remote="?")), release="1.7.6",
                                 clock=clock))) == 1
    later = alerter(cfg, Printer(), release="1.7.6", clock=clock)
    [offer] = check_now(later)
    assert offer.applicable and len(later.core.adapter.questions) == 1
    assert check_now(alerter(cfg, Printer(), release="1.7.6", clock=clock)) == []


def test_never_checks_during_a_print(cfg):
    clock = Clock()
    printer = Printer(states=("printing",))
    al = alerter(cfg, printer, clock=clock)
    assert run(al.check(PID, AD5X)) == []
    assert printer.calls == [("GET", fw.PRINT_STATE_PATH)]  # nothing else was read
    assert al.core.adapter.sent == [] and al.core.adapter.questions == []
    state = fa.load(settings.printer_state_dir(PID, cfg))
    assert state["next_check_at"] == clock.now + fal.BUSY_RETRY_S


def test_a_failed_check_logs_once_and_never_alerts(cfg, caplog):
    printer = Printer(error=OSError("printer offline"))
    al = alerter(cfg, printer)
    with caplog.at_level(logging.DEBUG, logger=fal.__name__):
        run(al.check(PID, AD5X))
        run(al.check(PID, AD5X))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "printer offline" in warnings[0].getMessage()
    assert al.core.adapter.sent == [] and al.core.adapter.questions == []


def test_a_github_failure_still_alerts_from_moonraker(cfg):
    al = alerter(cfg, Printer(), release=None)

    async def flow():
        found = await al.check(PID, AD5X)
        for task in list(al.tasks):
            task.cancel()
        await settle(al)
        return found

    assert len(run(flow())) == 1


# ------------------------------------------------------------------ scheduling and opt-out

def test_opt_out_starts_no_check(cfg):
    cfg["firmware"]["update_alerts"] = False
    printer = Printer()
    clock = Clock(10 ** 9)
    al = alerter(cfg, printer, clock=clock)

    async def flow():
        return al.tick()

    assert run(flow()) == [] and printer.calls == []


def test_tick_waits_for_the_first_due_time_then_checks(cfg):
    clock = Clock()
    printer = Printer(states=("printing",))
    al = alerter(cfg, printer, clock=clock)

    async def flow():
        assert al.tick() == []  # just started: the first check waits a little
        clock.now += fal.FIRST_CHECK_S + fal.FIRST_JITTER_S + 1
        started = al.tick()
        await settle(al)
        return started

    assert len(run(flow())) == 1
    assert printer.calls == [("GET", fw.PRINT_STATE_PATH)]


def test_next_check_is_the_interval_with_jitter(cfg):
    al = alerter(cfg, Printer())
    hours = cfg["firmware"]["update_check_hours"] * 3600
    for _ in range(50):
        gap = al._next(0.0)
        assert hours * 0.9 <= gap <= hours * 1.1


def test_only_zmod_printers_get_alerts(cfg):
    registry = knowledge.default_registry()
    rows = [dict(cfg["printers"][0], id=f"p{n}", model=e.key)
            for n, e in enumerate(registry.printers)]
    cfg["printers"] = rows
    chosen = {entry.key for _pid, entry in fal.alert_printers(cfg)}
    assert chosen == {"flashforge.ad5x", "flashforge.ad5m", "flashforge.ad5m_pro",
                      "flashforge.creator5", "flashforge.creator5_pro"}


# ------------------------------------------------------------------ settings

def test_settings_default_on_daily():
    data = _config.cfg()
    assert data["firmware"] == {"update_alerts": True, "update_check_hours": 24}


@pytest.mark.parametrize("bad", [{"update_alerts": "yes"}, {"update_check_hours": 0},
                                 {"update_check_hours": 169}, {"update_check_hours": True}])
def test_settings_refuse_bad_values(bad):
    data = {"firmware": dict(bad)}
    with pytest.raises(settings.SettingsError):
        settings._validate_firmware(data)


# ------------------------------------------------------------------ review hardening

def test_the_apply_gate_refuses_a_new_base_from_any_entry_point():
    """The USB-only rule is enforced in prepare_update, not only in the alert
    text: a typed /print-firmware update on a 1.8 remote is refused."""
    printer = Printer(status(remote="1.8.0-1"))
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(printer, AD5X, "zmod")
    assert "new ZMod base" in str(excinfo.value)
    assert printer.posts == []


def test_the_apply_gate_allows_an_update_on_the_installed_base():
    prepared = fw.prepare_update(Printer(), AD5X, "zmod")
    assert prepared.component.remote == "1.7.6-17"


def test_a_failing_state_write_does_not_re_announce_every_heartbeat(cfg, monkeypatch):
    clock = Clock()
    al = alerter(cfg, Printer(), clock=clock)

    def broken_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(fa, "save", broken_save)
    check_now(al)  # posts the alert, then the state write fails
    assert len(al.core.adapter.questions) == 1
    clock.now += 20  # the next heartbeat
    assert al.tick() == []
    assert check_now(al) == []
    assert len(al.core.adapter.questions) == 1


def test_a_skip_pressed_during_a_check_is_kept(cfg, monkeypatch):
    state_dir = settings.printer_state_dir(PID, cfg)
    al = alerter(cfg, Printer())
    real_offers = fa.offers

    def offers_then_skip(observation):
        # The operator presses Skip (on any surface) while this check runs.
        fa.save(state_dir, fa.record_skipped(fa.load(state_dir), "zmod:1.7.5", 1.0))
        return real_offers(observation)

    monkeypatch.setattr(fa, "offers", offers_then_skip)
    check_now(al)
    assert "zmod:1.7.5" in fa.load(state_dir).get("skipped", {})
    assert "zmod:1.7.6" in fa.load(state_dir).get("announced", {})
