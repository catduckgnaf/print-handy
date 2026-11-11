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
    cfg["paths"]["hermes_home"] = tmp_path / "home"
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
    cfg["paths"]["hermes_home"] = tmp_path / "home"
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


def test_telegram_alert_is_an_inline_keyboard(cfg, tmp_path):
    cfg["paths"]["hermes_home"] = tmp_path / "home"
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
    cfg["paths"]["hermes_home"] = tmp_path / "home"
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
    cfg["paths"]["hermes_home"] = tmp_path / "home"
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    # Just started: nothing is due yet, and the call never raises.
    assert run(core.check_firmware_updates()) == []
    cfg["firmware"]["update_alerts"] = False
    assert run(core.check_firmware_updates()) == []
