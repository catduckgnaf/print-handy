"""ZMod update alerts on every transport, with the existing transport fakes.

Discord: the alert is a real button question through BotCore's adapter;
pressing Update reaches the existing confirmation (the fake Discord presses
"Update now"), and exactly one upgrade POST is sent. Telegram: the same
question as an inline keyboard. Matrix has no buttons: the alert is plain
text naming the typed command, which runs the same confirmed path.
"""
import asyncio
import copy
from types import SimpleNamespace as S

from corpus import fake_discord
from print_handy import firmware_update as fw
from print_handy.bot import firmware_alert as fal
from print_handy.bot import matrix, runtime, telegram
from print_handy.core import prompts

from .test_matrix import HOMESERVER, ROOM, HomeserverStub
from .test_matrix import TOKEN as MX_TOKEN
from .test_matrix import USER as MX_USER
from .test_telegram import CHAT, BotApiStub
from .test_telegram import TOKEN as TG_TOKEN
from .test_telegram import USER as TG_USER

AD5X = fw.printer_entry("flashforge.ad5x")


def run(coro):
    return asyncio.run(coro)


class Printer:
    """A ZMod Moonraker with a Z-Mod update; the upgrade POST installs it."""

    def __init__(self):
        self.zmod = {"version": "1.7.5-2", "remote_version": "1.7.6-17", "is_valid": True}
        self.posts = []

    def __call__(self, method, path, body=None):
        if path == fw.UPDATE_UPGRADE_PATH:
            self.posts.append(body)
            self.zmod["version"] = self.zmod["remote_version"]
            return {"result": "ok"}
        if path == fw.UPDATE_STATUS_PATH:
            return {"result": {"version_info": {"Z-Mod": dict(self.zmod)}}}
        if path == fw.SYSTEM_INFO_PATH:
            return {"result": {"system_info": {"distribution": {
                "name": "zmod 1.7.5 -> 1.7.5-2", "version": "1.7.5"}}}}
        if path == fw.PRINT_STATE_PATH:
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        raise AssertionError(f"unexpected {method} {path}")


def _alerter(core, printer):
    return fal.FirmwareAlerter(core, transport_for=lambda pid: printer,
                               releases=S(get=lambda: None), confirm_timeout_s=5)


async def _settle(al):
    while al.tasks:
        await asyncio.gather(*list(al.tasks), return_exceptions=True)


def _pid(cfg):
    return cfg["printers"][0]["id"]


def test_discord_alert_button_runs_the_confirmed_update(cfg, tmp_path):
    cfg["paths"]["state_home"] = tmp_path / "home"
    discord = fake_discord.FakeDiscord(
        clicks=[{"question": "ZMod update available", "button": "Update"},
                {"question": "Update printer firmware?", "button": "Update now"}],
        chat_id=int(cfg["discord"]["chat_id"]), operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    printer = Printer()
    al = _alerter(core, printer)

    async def flow():
        await al.check(_pid(cfg), AD5X)
        for _ in range(500):
            await asyncio.sleep(0.01)
            if not al.tasks and not discord.pending:
                break
        await _settle(al)

    run(flow())
    assert discord.errors == []
    alert, confirm = discord.questions[:2]
    assert alert["choices"] == [fal.UPDATE_LABEL, fal.SKIP_LABEL]
    assert "1.7.5-2 -> 1.7.6-17" in alert["question"]
    assert confirm["choices"] == [fw.UPDATE_LABEL, fw.CANCEL_LABEL]
    assert printer.posts == [{"name": "Z-Mod"}]
    assert any("Updated **Z-Mod** to 1.7.6-17" in str(text) for text in discord.sent)


def test_discord_alert_press_by_a_stranger_changes_nothing(cfg, tmp_path):
    cfg["paths"]["state_home"] = tmp_path / "home"
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    printer = Printer()
    al = _alerter(core, printer)

    async def flow():
        await al.check(_pid(cfg), AD5X)
        message = discord.last_question()
        _label, custom_id = discord.prompt_buttons(message)[0]
        stranger = fake_discord.FakeUser(999999999999999999, "stranger")
        clicked = await discord.press(message, custom_id, user=stranger)
        for task in list(al.tasks):
            task.cancel()
        await _settle(al)
        return clicked

    clicked = run(flow())
    assert clicked.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
    assert printer.posts == []


def test_alert_prompt_closes_when_the_waiter_is_cancelled_before_starting(cfg, tmp_path):
    """Regression: the alert prompt is registered before its waiter task is
    spawned, and the waiter's own cleanup runs only once the task has
    started. A waiter cancelled before its first step (the stranger-press
    test above ends exactly that way) must not leave the prompt open in the
    shared registry: a leaked prompt poisons the prompt bookkeeping of
    unrelated features for the rest of the process."""
    cfg["paths"]["state_home"] = tmp_path / "home"
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    al = _alerter(core, Printer())

    async def flow():
        await al.check(_pid(cfg), AD5X)
        for task in list(al.tasks):
            task.cancel()
        await _settle(al)

    run(flow())
    assert prompts.open_count() == 0


def test_telegram_alert_is_an_inline_keyboard(cfg, tmp_path):
    cfg["paths"]["state_home"] = tmp_path / "home"
    stub = BotApiStub()
    adapter = telegram.StandaloneAdapter(None, [TG_USER], token=TG_TOKEN, opener=stub)
    core = S(cfg=cfg, adapter=adapter, platform_name="telegram", chat_id=CHAT,
             gateway=S(setup_only=False))
    al = _alerter(core, Printer())

    async def flow():
        await al.check(_pid(cfg), AD5X)
        for task in list(al.tasks):
            task.cancel()
        await _settle(al)

    run(flow())
    payload = stub.payloads("sendMessage")[-1]
    assert "1.7.5-2 -> 1.7.6-17" in payload["text"]
    labels = [row[0]["text"] for row in payload["reply_markup"]["inline_keyboard"]]
    assert labels == [fal.UPDATE_LABEL, fal.SKIP_LABEL]


def test_matrix_alert_names_the_typed_command(cfg, tmp_path):
    cfg = copy.deepcopy(cfg)
    cfg["paths"]["state_home"] = tmp_path / "home"
    stub = HomeserverStub()
    adapter = matrix.StandaloneAdapter(None, [MX_USER], homeserver=HOMESERVER, token=MX_TOKEN,
                                       opener=stub)
    core = S(cfg=cfg, adapter=adapter, platform_name="matrix", chat_id=ROOM,
             gateway=S(setup_only=False))
    al = _alerter(core, Printer())
    run(al.check(_pid(cfg), AD5X))
    [body] = stub.sent_bodies()
    assert "1.7.5-2 -> 1.7.6-17" in body
    assert "`/print-firmware update zmod`" in body and "Press **Update**" not in body
    assert al.tasks == set()  # no button question is waiting on Matrix


def test_the_bot_heartbeat_schedules_alerts_on_every_platform(cfg, tmp_path):
    import inspect

    from print_handy.bot import client
    for module in (client, telegram, matrix):
        assert "await self.core.check_firmware_updates()" in inspect.getsource(module), \
            module.__name__
    cfg["paths"]["state_home"] = tmp_path / "home"
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    # Just started: nothing is due yet, and the call never raises.
    assert run(core.check_firmware_updates()) == []
    cfg["firmware"]["update_alerts"] = False
    assert run(core.check_firmware_updates()) == []


# ------------------------------------------------------------------ Web UI

def _web(cfg, tmp_path, monkeypatch):
    from print_handy.bot import web
    from print_handy.runtime import _config
    cfg["paths"]["state_home"] = tmp_path / "home"
    monkeypatch.setattr(_config, "_override", cfg)
    service = web.WebChatService(cfg)
    return web, service


def _on_loop(service, coro, timeout=30):
    return asyncio.run_coroutine_threadsafe(coro, service._loop).result(timeout=timeout)


def _wait_prompt(view, question, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        prompt = view.open_prompt()
        if prompt and question in prompt["question"]:
            return prompt
        time.sleep(0.02)
    raise AssertionError(f"no open prompt {question!r}: {view.entries()}")


def _wait_message(view, needle, timeout=10.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in view.entries():
            if entry["kind"] == "message" and needle in (entry.get("content") or ""):
                return entry
        time.sleep(0.02)
    raise AssertionError(f"no message {needle!r}: {view.entries()}")


def test_web_chat_alert_has_update_and_skip_and_runs_the_confirmed_update(
        cfg, tmp_path, monkeypatch):
    """The Web UI gets the same alert, buttons and confirmed apply as Discord.

    The alert belongs to no Web UI session, so it is a shared post every
    session sees; the confirmation that Update opens is the same
    confirmed_update prompt, answered through the web chat press path.
    """
    web, service = _web(cfg, tmp_path, monkeypatch)
    printer = Printer()
    al = service.core.firmware_alerts = _alerter(service.core, printer)
    _on_loop(service, al.check(_pid(cfg), AD5X))
    one = service.view(web.scope_for("browser-one"))
    two = service.view(web.scope_for("browser-two"))
    alert = _wait_prompt(one, "ZMod update available")
    assert alert["choices"] == [fal.UPDATE_LABEL, fal.SKIP_LABEL]
    assert "1.7.5-2 -> 1.7.6-17" in alert["question"]
    assert two.open_prompt()["clarify_id"] == alert["clarify_id"]  # shared
    assert printer.posts == []
    one.answer(alert["clarify_id"], 0)
    confirm = _wait_prompt(two, "Update printer firmware?")
    assert confirm["choices"] == [fw.UPDATE_LABEL, fw.CANCEL_LABEL]
    assert printer.posts == []  # one press is never enough
    two.answer(confirm["clarify_id"], 0)
    _wait_message(one, "Updated **Z-Mod** to 1.7.6-17")
    _on_loop(service, _settle(al))
    assert printer.posts == [{"name": "Z-Mod"}]


def test_web_chat_skip_this_version_is_remembered_on_every_surface(cfg, tmp_path, monkeypatch):
    from print_handy import firmware_alerts as fa
    web, service = _web(cfg, tmp_path, monkeypatch)
    printer = Printer()
    al = service.core.firmware_alerts = _alerter(service.core, printer)
    _on_loop(service, al.check(_pid(cfg), AD5X))
    view = service.view(web.scope_for("browser"))
    alert = _wait_prompt(view, "ZMod update available")
    view.answer(alert["clarify_id"], 1)
    _on_loop(service, _settle(al))
    state_dir = al._state_dir(_pid(cfg))
    assert "zmod:1.7.6" in fa.load(state_dir, fa.WEBUI_SURFACE)["skipped"]
    assert printer.posts == []
    # The chat bot's own state never saw this alert, and still honors the skip.
    assert fa.load(state_dir) == {}
    observation = fa.observe(printer, None)
    assert fa.to_announce(fa.offers(observation), fa.load(state_dir),
                          skipped=fa.skipped_everywhere(state_dir)) == []


def test_web_and_chat_alerts_keep_separate_announcement_state(cfg, tmp_path, monkeypatch):
    """Each surface announces a release once on its own: the bot and the
    Web UI run in different processes and must not suppress each other."""
    from print_handy import firmware_alerts as fa
    web, service = _web(cfg, tmp_path, monkeypatch)
    printer = Printer()
    al = service.core.firmware_alerts = _alerter(service.core, printer)
    state_dir = al._state_dir(_pid(cfg))
    fa.save(state_dir, fa.record_announced({}, fa.Offer("zmod", "1.7.5-2", "1.7.6-17",
                                                        "zmod:1.7.6"), 1.0))
    announced = _on_loop(service, al.check(_pid(cfg), AD5X))
    assert [offer.key for offer in announced] == ["zmod:1.7.6"]
    assert "zmod:1.7.6" in fa.load(state_dir, fa.WEBUI_SURFACE)["announced"]
    for task in list(al.tasks):
        service._loop.call_soon_threadsafe(task.cancel)


def test_web_chat_print_firmware_update_helix_runs_the_confirmed_update(
        cfg, tmp_path, monkeypatch):
    """`/print-firmware update helix` in web chat: the alias resolves to the
    live helixscreen component, the confirmation is a web chat prompt, and
    exactly one upgrade POST names the live component."""
    from unittest import mock

    from print_handy.runtime import spool_sync
    web, service = _web(cfg, tmp_path, monkeypatch)

    class Helix:
        def __init__(self):
            self.version = "v1.0.7"
            self.posts = []

        def __call__(self, method, path, body=None, timeout=None):
            if path == fw.UPDATE_UPGRADE_PATH:
                self.posts.append(body)
                self.version = "v1.0.8"
                return {"result": "ok"}
            if path in (fw.UPDATE_STATUS_PATH, fw.UPDATE_REFRESH_PATH):
                return {"result": {"version_info": {
                    "Z-Mod": {"version": "1.7.6", "remote_version": "1.7.6"},
                    "helixscreen": {"version": self.version, "remote_version": "v1.0.8",
                                    "configured_type": "web"}}}}
            if path == fw.SYSTEM_INFO_PATH:
                return {"result": {"system_info": {"distribution": {"version": "1.7.6"}}}}
            if path == fw.PRINT_STATE_PATH:
                return {"result": {"status": {"print_stats": {"state": "standby"}}}}
            raise AssertionError(f"unexpected {method} {path}")

    printer = Helix()
    view = service.view(web.scope_for("browser"))
    with mock.patch.object(spool_sync, "moonraker", printer), \
            mock.patch.object(fw, "send_update", _send_fast):
        assert view.submit("/print-firmware update helix")
        confirm = _wait_prompt(view, "Update printer firmware?")
        assert "**helixscreen**: v1.0.7 -> v1.0.8" in confirm["question"]
        assert printer.posts == []
        view.answer(confirm["clarify_id"], 0)
        _wait_message(view, "Updated **helixscreen** to v1.0.8")
    assert printer.posts == [{"name": "helixscreen"}]


_REAL_SEND_UPDATE = fw.send_update


def _send_fast(transport, prepared):
    return _REAL_SEND_UPDATE(transport, prepared, verify_delay_s=0, sleep=lambda _s: None)
