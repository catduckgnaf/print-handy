"""Discord's own no-LLM proofs (#466), mirroring the Telegram/Matrix ones.

Telegram and Matrix are pure bots by adapter (``uses_llm = False``); Discord
is the platform whose chat *may* use the LLM, so its no-LLM proofs come in
two configurations, both Discord-named and both driven through the real
Discord adapter on a fake discord.py client:

* no ``[llm]`` configured at all, and
* ``[llm]`` configured (for /print-design, which inherits it) with the
  operator switch ``[discord] chat_llm_enabled`` set false.

In both, an exploding stub stands in for ``llm.ask``: any chat-LLM call
fails the test. Deterministic work — commands, links, typed answers,
button flows, ``print <number>`` (#436) — must be unchanged, unmatched
text gets the short not-understood reply (#467), and ``bot check`` / ``status()``
state the chat-LLM state in both modes. Turning chat off must not touch
the separately gated model touchpoints: design (its own endpoint
inheritance and switch), the roast (``vision.roast_enabled``) and the
camera watchdog (``visual_watch.enabled`` on the vision endpoint).
"""
import asyncio
import json
import os
import time
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import runtime
from print_handy.bot.adapter import StandaloneAdapter, parse_custom_id
from print_handy.bot.router import Router
from print_handy.core import design, link_intake, prompts
from print_handy.runtime import discord_print_summary as summary
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_control as pc
from print_handy.runtime import print_next

LLM_URL = "http://llm.invalid/v1"


def run(coro):
    return asyncio.run(coro)


def make(cfg, router=None):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=router if router is not None else Router())
    return discord, core


def exploding_ask(calls):
    def ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the Discord bot called the chat LLM in a no-LLM configuration")
    return ask


async def press(discord, label):
    """Wait for the open question carrying this button label, then press it."""
    for _ in range(300):
        message = discord.last_question()
        if message is not None:
            for text, custom_id in discord.prompt_buttons(message):
                if text == label and prompts.is_open(parse_custom_id(custom_id)[0]):
                    return await discord.press(message, custom_id)
        await asyncio.sleep(0.01)
    raise AssertionError(f"no open question with {label!r}: {[m.content for m in discord.messages]}")


# ---------------------------------------------------------------------- the switch itself


def test_discord_adapter_can_use_the_llm_and_the_setting_defaults_off(cfg):
    """Unlike Telegram/Matrix, the Discord adapter itself stays uses_llm=True:
    the operator's per-install choice is [discord] chat_llm_enabled (#466),
    off by default since #1376 and on only when the operator says so."""
    assert StandaloneAdapter.uses_llm is True
    assert runtime.discord_chat_llm_enabled(cfg) is False
    assert runtime.discord_chat_llm_enabled({"discord": {}}) is False
    cfg["discord"]["chat_llm_enabled"] = True
    assert runtime.discord_chat_llm_enabled(cfg) is True
    cfg["llm"].update(chat_url=LLM_URL, model="m")
    discord, core = make(cfg)
    assert core.use_llm is True and core.chat_llm_enabled is True
    assert core.status(True)["llm"] is True


def test_discord_chat_llm_setting_loads_from_env_and_validates():
    data = settings.load(local=False, environ={})
    assert data["discord"]["chat_llm_enabled"] is False  # off by default (#1376)
    data = settings.load(local=False, environ={"PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED": "true"})
    assert data["discord"]["chat_llm_enabled"] is True
    data = settings.load(local=False, environ={"PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED": "false"})
    assert data["discord"]["chat_llm_enabled"] is False
    with pytest.raises(settings.SettingsError, match="CHAT_LLM_ENABLED must be true or false"):
        settings.load(local=False, environ={"PRINT_HANDY__DISCORD__CHAT_LLM_ENABLED": "sometimes"})


# ---------------------------------------------------------------------- no [llm] configured


def test_discord_no_llm_configured_never_calls_the_llm(cfg):
    """Discord-named version of the generic proof: no [llm], exploding stub,
    a deterministic command still answers, unmatched text gets the command
    list exactly once and no second reply follows."""
    # Chat AI on (it is off by default, #1376): use_llm is the platform/
    # operator capability; with nothing configured the fallback still never
    # fires, which status() reports and the exploding stub below proves.
    cfg["discord"]["chat_llm_enabled"] = True
    discord, core = make(cfg)
    assert core.use_llm is True and core.status(True)["llm"] is False
    calls = []
    with mock.patch.object(runtime.llm, "ask", exploding_ask(calls)), \
            mock.patch("print_handy.runtime.spool_sync.list_text", return_value="Slot 1: #7 black PLA"):
        out = run(core.handle_message(discord.message("/spool list")))
        assert discord.sent == ["Slot 1: #7 black PLA"]
        out = run(core.handle_message(discord.message("can you make my printer go brrr")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == ["Slot 1: #7 black PLA", core.unmatched]  # the short reply (#467), once, nothing after it


# ---------------------------------------------------------------------- no-LLM mode, [llm] configured


@pytest.fixture
def no_llm_cfg(cfg):
    """[llm] configured (as a /print-design setup leaves it), Discord chat LLM off by name."""
    cfg["llm"].update(chat_url=LLM_URL, model="m")
    cfg["discord"]["chat_llm_enabled"] = False
    return cfg


def test_discord_chat_llm_off_never_calls_the_llm_even_when_configured(no_llm_cfg):
    discord, core = make(no_llm_cfg)
    assert core.platform_name == "discord"
    assert core.chat_llm_enabled is False and core.use_llm is False
    status = core.status(True)
    assert status["platform"] == "discord" and status["llm"] is False
    calls = []
    with mock.patch.object(runtime.llm, "ask", exploding_ask(calls)), \
            mock.patch.object(runtime.llm, "configured", return_value=True), \
            mock.patch("print_handy.runtime.spool_sync.list_text", return_value="Slot 1: #7 black PLA"):
        out = run(core.handle_message(discord.message("/spool list")))
        assert discord.sent == ["Slot 1: #7 black PLA"]  # deterministic commands unchanged
        out = run(core.handle_message(discord.message("what is the printer doing?")))
    assert calls == []
    assert out == {"action": "skip", "reason": "command list"}
    assert discord.sent == ["Slot 1: #7 black PLA", core.unmatched]


def test_discord_chat_llm_off_typed_answers_and_buttons_still_work(no_llm_cfg):
    """Typed answers to an open question and button presses are deterministic
    paths: both resolve with the exploding stub in place."""
    discord, core = make(no_llm_cfg)
    calls = []

    async def flow():
        source = runtime.make_source(discord.operator.id, discord.channel.id)
        cid = "discord-nollm-typed"
        prompts.register(cid, runtime.session_key(source), "How many copies?", ["1", "2"])
        await core.adapter.send_clarify(chat_id=str(discord.channel.id), question="How many copies?",
                                        choices=["1", "2"], clarify_id=cid, session_key="flow")
        await press(discord, "2")
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    with mock.patch.object(runtime.llm, "ask", exploding_ask(calls)), \
            mock.patch.object(runtime.llm, "configured", return_value=True):
        assert run(flow()) == "2"
    assert calls == []


# ---------------------------------------------------------------------- bot check + touchpoint inventory


def test_discord_bot_check_llm_row_pins_every_mode(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    cfg["discord"]["chat_llm_enabled"] = True  # off by default (#1376); on, the rows follow [llm]
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["llm"] == (True, "off (unmatched messages get the short not-understood reply)")
    cfg["llm"].update(chat_url=LLM_URL, model="m")
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["llm"] == (True, f"m at {LLM_URL}")
    cfg["discord"]["chat_llm_enabled"] = False
    rows = {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}
    assert rows["llm"] == (True, "off (discord.chat_llm_enabled is false: no chat LLM; "
                                 "unmatched messages get the short not-understood reply)")
    # The roast row is a separate gate and does not follow the chat switch.
    assert rows["roast"][0] is True and rows["roast"][1].startswith("on (local corpus, ")


def test_chat_llm_off_leaves_design_roast_and_watchdog_gates_alone(no_llm_cfg):
    """The LLM touchpoint inventory, pinned: each keeps its own gate when
    Discord's chat fallback is turned off.

    * chat fallback — [discord] chat_llm_enabled (this issue), off here;
    * /print-design — [design] inheriting [llm], resolved independently;
    * the roast — vision.roast_enabled, local corpus, no endpoint (#1002);
    * the camera watchdog — visual_watch.enabled on the vision endpoint.
    """
    resolved = design.resolve_http_config(no_llm_cfg)
    assert resolved is not None and resolved["chat_url"] == LLM_URL and resolved["model"] == "m"
    assert design.effective_enabled(no_llm_cfg) is True
    assert mel.roast_enabled(no_llm_cfg) is True  # on by default; its own switch turns it off
    assert no_llm_cfg["visual_watch"]["enabled"] is True
    assert no_llm_cfg["services"]["vision_chat_url"]  # the vision endpoint both of them read


# ---------------------------------------------------------------------- #436 stays deterministic


def _printed(tmp_path, slug, title, age_s):
    d = tmp_path / slug / "sliced/orca/x1"
    d.mkdir(parents=True)
    (d / "plate_1.gcode").write_text(f";{slug}\nG1 X1\n")
    (tmp_path / slug / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(d / "plate_1.gcode")}))
    rec = d / "plate_1.launch.json"
    rec.write_text(json.dumps({"file": f"{slug}.gcode"}))
    t = time.time() - age_s
    os.utime(rec, (t, t))
    return d / "plate_1.gcode"


def test_discord_print_number_still_works_with_chat_llm_off(no_llm_cfg, tmp_path, monkeypatch):
    """#436's `print 57` form is deterministic: in no-LLM Discord mode it
    offers the same print behind the same confirmation, exploding stub in
    place (the registry here holds two prints, so the text is `print 2`)."""
    monkeypatch.setattr(pc, "ARCHIVE", tmp_path)
    monkeypatch.setattr(pc, "PRINT_NUMBERS", tmp_path / "print_numbers.json")
    monkeypatch.setattr(pc, "REPRINT_QUEUE", tmp_path / "reprint_queue.json")
    _printed(tmp_path, "older-widget--a", "Older Widget", 200)
    _printed(tmp_path, "newer-widget--b", "Newer Widget", 100)
    router = Router()
    discord, core = make(no_llm_cfg, router=router)
    started = []

    async def fake_start(target):
        started.append(target)
        return True, "started"

    calls = []
    confirm = mock.AsyncMock(return_value=pc.AGAIN_LABEL)
    with mock.patch.object(router.plugin, "_confirm", confirm), \
            mock.patch.object(print_next, "start", side_effect=fake_start), \
            mock.patch.object(summary, "gcode_metadata", return_value=(60, 1.0)), \
            mock.patch.object(summary, "_print_number", return_value=2), \
            mock.patch.object(runtime.llm, "ask", exploding_ask(calls)), \
            mock.patch.object(runtime.llm, "configured", return_value=True):
        out = run(core.handle_message(discord.message("print 2")))
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    assert calls == [] and confirm.await_count == 1
    assert len(started) == 1 and started[0]["title"] == "Newer Widget"


# ---------------------------------------------------------------------- full intake, no-LLM mode


def test_discord_adapter_drives_a_full_intake_in_no_llm_mode(no_llm_cfg):
    """The #73 roll-test standard, applied to Discord itself (#466 scope 3):
    link -> colour prompt -> confirmation -> start, through the real
    handlers and the real Discord adapter's buttons, with [llm] configured,
    chat_llm_enabled=false and the exploding stub in place."""
    discord, core = make(no_llm_cfg)
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        seen["adapter"] = adapter_arg
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key, "Which color?", ["Black", "White"])
        await adapter_arg.send(chat_id=source.chat_id, content=f"Sliced in {['Black', 'White'][color]}.")
        start = await link_intake.ask(adapter_arg, source, key, "Start the print?", ["Print it"])
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    async def flow():
        task = asyncio.create_task(core.handle_message(
            discord.message("https://makerworld.com/en/models/931302-holder")))
        await press(discord, "White")
        await press(discord, "Print it")
        return await task

    calls = []
    with mock.patch.object(runtime.llm, "ask", exploding_ask(calls)), \
            mock.patch.object(runtime.llm, "configured", return_value=True), \
            mock.patch("print_handy.core.prepared_intake.prepare_link", fake_prepare), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        out = run(flow())
    assert calls == []
    assert out == {"action": "skip", "reason": "test intake"}
    assert seen["adapter"] is core.adapter and seen["answers"] == (1, 0)
    assert core.adapter.name == "discord"
    assert any("Sliced in White." in t for t in discord.sent) and discord.sent[-1] == "Started."
    assert not discord.errors
