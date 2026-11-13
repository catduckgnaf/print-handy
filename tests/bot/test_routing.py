"""Channel → printer routing (#335, multi-printer Stage 2c): offline, two printers, two channels.

Pins the stage's acceptance properties at the bot layer: each channel binds
its flows to its own printer, an unmapped channel gets silence, a duplicate
channel claim refuses startup naming both printers, a prompt is answered
only in the channel it was asked in, Matrix serves every mapped room, and
the plugin's routed calls carry the channel's printer while the default
printer keeps the exact legacy call shapes.
"""
import asyncio
import copy
import json
from types import SimpleNamespace as S
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy import settings
from print_handy.bot import matrix, routing, runtime, telegram
from print_handy.bot.router import Router
from print_handy.core import prompts
from print_handy.runtime import _config, print_next, print_stop

OPERATOR = "200000000000000002"
CHAT_B = "100000000000000777"
CHAT_X = "100000000000000999"


def run(coro):
    return asyncio.run(coro)


def two_printer_cfg(cfg):
    """The bot conftest cfg with a two-printer roster: garage (default, home-chat fallback) + workshop."""
    base = copy.deepcopy(cfg["printers"][0])
    garage = {**base, "id": "flashforge-ad5x", "name": "Garage AD5X", "chat_id": ""}
    workshop = {**base, "id": "workshop-ad5x", "name": "Workshop AD5X", "chat_id": CHAT_B}
    cfg["printers"] = [garage, workshop]
    return cfg


def make(cfg, auto=False):
    discord = fake_discord.FakeDiscord(auto=auto, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    discord.add_channel(int(CHAT_B))
    discord.add_channel(int(CHAT_X))
    core = runtime.BotCore(discord.client, cfg, router=Router())
    return discord, core


# ---------------------------------------------------------------------- the map itself


def plain_cfg(printers, **sections):
    cfg = {"printers": printers,
           "discord": {"chat_id": "111"}, "telegram": {"chat_id": "-100"},
           "matrix": {"room_id": "!home:example.org"}}
    cfg.update(sections)
    return cfg


def test_channel_map_claims_and_platform_home_fallbacks():
    cfg = plain_cfg([{"id": "a", "chat_id": ""}, {"id": "b", "chat_id": "222"}])
    assert routing.channel_map(cfg, "discord") == {"111": "a", "222": "b"}
    # The same roster on Telegram: the fallback is the Telegram home chat.
    assert routing.channel_map(cfg, "telegram") == {"-100": "a", "222": "b"}
    # On Matrix the home is the configured room.
    assert routing.channel_map(cfg, "matrix") == {"!home:example.org": "a", "222": "b"}
    assert routing.printer_for_chat(cfg, "discord", "222") == "b"
    assert routing.printer_for_chat(cfg, "discord", "333") is None
    assert routing.chat_for_printer(cfg, "discord", "a") == "111"


def test_channel_map_without_a_roster_is_empty():
    assert routing.channel_map({}, "discord") == {}
    assert routing.channel_map({"discord": {"chat_id": "1"}}, "discord") == {}


def test_channel_map_duplicate_claim_makes_a_shared_channel():
    # #536: a duplicate claim is no longer an error — it is a shared
    # channel, routed to neither printer and listed for both.
    cfg = plain_cfg([{"id": "a", "chat_id": "111"}, {"id": "b", "chat_id": ""}])  # b falls back to the home a claimed
    assert routing.channel_map(cfg, "discord") == {}
    assert routing.shared_channel_map(cfg, "discord") == {"111": ["a", "b"]}
    assert routing.channel_claims(cfg, "discord") == {"111": ["a", "b"]}


def test_channel_map_telegram_home_fallback_can_share_too():
    # Settings see claims against every platform home the same way now:
    # two entries are distinct on Discord but share the Telegram home.
    cfg = plain_cfg([{"id": "a", "chat_id": "-100"}, {"id": "b", "chat_id": ""}])
    assert routing.channel_map(cfg, "discord") == {"111": "b", "-100": "a"}
    assert routing.shared_channel_map(cfg, "discord") == {}
    assert routing.channel_map(cfg, "telegram") == {}
    assert routing.shared_channel_map(cfg, "telegram") == {"-100": ["a", "b"]}


# ---------------------------------------------------------------------- BotCore routing


class RecordingRouter:
    """A router stand-in that records the events BotCore dispatches, sources included."""

    def __init__(self):
        self.events = []
        self.platform_handlers = {}
        self.dispatch_hooks = []

    def attach_platform(self, platform_name, client, adapter):
        pass

    async def dispatch(self, event, gateway):
        self.events.append(event)
        return {"action": "skip", "reason": "recorded"}


def test_botcore_routes_each_channel_to_its_printer_and_stamps_sources(cfg):
    cfg = two_printer_cfg(cfg)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    chan_b = discord.add_channel(int(CHAT_B))
    router = RecordingRouter()
    core = runtime.BotCore(discord.client, cfg, router=router)
    assert core.channel_printers == {cfg["discord"]["chat_id"]: "flashforge-ad5x", CHAT_B: "workshop-ad5x"}

    assert run(core.handle_message(discord.message("hello", channel=chan_b))) == {"action": "skip", "reason": "recorded"}
    assert run(core.handle_message(discord.message("hello"))) == {"action": "skip", "reason": "recorded"}
    by_chat = {event.source.chat_id: event.source.printer_id for event in router.events}
    assert by_chat == {CHAT_B: "workshop-ad5x", cfg["discord"]["chat_id"]: "flashforge-ad5x"}
    # The gateway agrees, and says which printer a source belongs to.
    assert core.gateway.printer_id_for(router.events[0].source) == "workshop-ad5x"
    assert core.status(False)["channel_printers"] == core.channel_printers


def test_botcore_unmapped_channel_is_silent(cfg):
    cfg = two_printer_cfg(cfg)
    discord, core = make(cfg)
    chan_x = discord.client.get_channel(int(CHAT_X))
    assert run(core.handle_message(discord.message("/print-help", channel=chan_x))) is None
    assert run(core.handle_message(discord.message("hello", channel=chan_x))) is None
    assert discord.messages == []  # nothing posted anywhere


def test_botcore_both_mapped_channels_serve_commands(cfg):
    cfg = two_printer_cfg(cfg)
    discord, core = make(cfg)
    run(core.handle_message(discord.message("/print-help")))
    chan_b = discord.client.get_channel(int(CHAT_B))
    run(core.handle_message(discord.message("/print-help", channel=chan_b)))
    channels = {m.channel.id for m in discord.messages}
    assert channels == {int(cfg["discord"]["chat_id"]), int(CHAT_B)}


def test_botcore_starts_on_a_duplicate_claim_and_serves_it_shared(cfg):
    # #536: the bot starts; the shared channel is served undecided — the
    # source carries the candidates, not a printer.
    cfg = two_printer_cfg(cfg)
    cfg["printers"][1]["chat_id"] = cfg["discord"]["chat_id"]  # workshop claims the home chat too
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    router = RecordingRouter()
    core = runtime.BotCore(discord.client, cfg, router=router)
    home = cfg["discord"]["chat_id"]
    assert core.channel_printers == {}
    assert core.shared_printers == {home: ["flashforge-ad5x", "workshop-ad5x"]}
    run(core.handle_message(discord.message("hello")))
    assert router.events, "the shared home channel must still serve"
    source = router.events[-1].source
    assert source.printer_id is None
    assert list(source.shared_printer_ids) == ["flashforge-ad5x", "workshop-ad5x"]
    assert core.gateway.printer_id_for(source) is None
    assert core.gateway.shared_printer_ids_for(source) == ["flashforge-ad5x", "workshop-ad5x"]
    assert core.status(False)["shared_printers"] == core.shared_printers


def test_botcore_without_a_roster_keeps_single_chat_behaviour(cfg):
    cfg["printers"] = []  # a config dict carrying no roster at all
    discord, core = make(cfg)
    assert core.channel_printers == {}
    run(core.handle_message(discord.message("/print-help")))
    assert discord.messages  # the one configured chat still serves


def test_run_slash_in_an_unmapped_channel_is_refused_not_routed(cfg):
    cfg = two_printer_cfg(cfg)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    chan_b = discord.add_channel(int(CHAT_B))
    chan_x = discord.add_channel(int(CHAT_X))
    router = RecordingRouter()
    core = runtime.BotCore(discord.client, cfg, router=router)

    async def slash(channel, name="print-help"):
        interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=channel)
        return await core.run_slash(interaction, name, {}), interaction

    out, interaction = run(slash(chan_x))
    assert out is None and "only works in its print channel" in interaction.replies[0]["content"]
    assert router.events == []
    out, _interaction = run(slash(chan_b, name="print-stop"))
    assert out == {"action": "skip", "reason": "recorded"}
    assert router.events[0].source.printer_id == "workshop-ad5x"


# ---------------------------------------------------------------------- prompt chat binding (Discord)


def printing(state="printing"):
    return {"state": state, "filename": "a.gcode", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


def test_a_button_press_in_another_channel_never_resolves_the_prompt(cfg):
    cfg = two_printer_cfg(cfg)
    discord, core = make(cfg)
    chan_b = discord.client.get_channel(int(CHAT_B))
    with mock.patch.object(print_stop, "status", return_value=printing()) as status, \
            mock.patch.object(print_stop, "cancel", return_value=(True, "Stopped.")) as cancel:
        async def flow():
            task = asyncio.create_task(core.handle_message(discord.message("/print-stop")))
            question = None
            for _ in range(200):
                question = discord.last_question()
                if question is not None:
                    break
                await asyncio.sleep(0.01)
            assert question is not None and question.channel is discord.channel
            _label, cid = next(iter(discord.prompt_buttons(question)))
            clarify_id = cid.split(":")[1]
            # The same button, pressed as an interaction arriving in channel B.
            foreign = fake_discord.FakeInteraction(discord, discord.operator, custom_id=cid,
                                                   message=question, channel=chan_b)
            await discord.client.dispatch_interaction(foreign)
            assert foreign.replies and "Not allowed" in foreign.replies[0]["content"]
            assert prompts.is_open(clarify_id)  # the prompt in channel A is untouched
            cancel.assert_not_called()
            # The proper press in the prompt's own channel resolves it.
            keep = next(c for label, c in discord.prompt_buttons(question) if label == print_stop.KEEP_LABEL)
            await discord.press(question, keep)
            return await task

        out = run(flow())
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    status.assert_called_once_with()  # the default printer's channel: legacy call shape
    cancel.assert_not_called()


# ---------------------------------------------------------------------- prompt chat binding (Telegram)


class _TgStub:
    """The minimum Bot API surface send_clarify/answerCallbackQuery need."""

    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=None):
        import io
        import json as _json
        method = req.full_url.rsplit("/", 1)[-1]
        fields = _json.loads(req.data or b"{}") if req.data else {}
        self.calls.append((method, fields))
        return _Response(_json.dumps({"ok": True, "result": {"message_id": 42}}).encode())

    def payloads(self, method):
        return [fields for name, fields in self.calls if name == method]


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_telegram_callback_from_another_chat_never_resolves_the_prompt():
    stub = _TgStub()
    adapter = telegram.StandaloneAdapter(None, [OPERATOR], token="123:abc", opener=stub)
    cid = "ef" * 16
    prompts.register(cid, f"telegram:chat::{OPERATOR}", "Stop the print?", ["Stop", "Keep"])
    run(adapter.send_clarify(chat_id="-100111", question="Stop the print?", choices=["Stop", "Keep"],
                             clarify_id=cid, session_key="flow"))

    def callback(chat_id):
        return {"id": "q1", "from": {"id": int(OPERATOR), "first_name": "Cat"},
                "data": f"pbq:{cid}:0",
                "message": {"message_id": 5, "chat": {"id": int(chat_id)}}}

    async def flow():
        assert await adapter.handle_callback_query(callback("-100222")) is True
        assert prompts.is_open(cid)  # the other chat's press changed nothing
        assert await adapter.handle_callback_query(callback("-100111")) is True
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "Stop"
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."


# ---------------------------------------------------------------------- Matrix rooms


def matrix_cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["bot"].update(mode="standalone", platform="matrix")
    data["matrix"].update(room_id="!garage:example.org", allowed_user_ids="@catduck:example.org",
                          approval_user_id="")
    base = copy.deepcopy(data["printers"][0])
    data["printers"] = [{**base, "id": "garage-ad5x", "name": "Garage AD5X", "chat_id": ""},
                        {**base, "id": "workshop-ad5x", "name": "Workshop AD5X",
                         "chat_id": "!workshop:example.org"}]
    data["paths"]["cache"] = tmp_path / "cache"
    return data


def room_event(body, sender="@catduck:example.org"):
    return {"type": "m.room.message", "sender": sender, "event_id": "$e1",
            "content": {"msgtype": "m.text", "body": body}}


def test_matrix_client_serves_every_mapped_room_and_no_other(tmp_path):
    cfg = matrix_cfg(tmp_path)
    client = matrix.MatrixClient(cfg, "https://matrix.example.org", "tok", state_path=tmp_path / "since")
    seen = []

    async def record(message):
        seen.append((str(message.channel.id), message.content))
        return {"action": "skip"}

    client.core = S(channel_printers={"!garage:example.org": "garage-ad5x",
                                      "!workshop:example.org": "workshop-ad5x"},
                    handle_message=record)
    sync = {"rooms": {"join": {
        "!garage:example.org": {"timeline": {"events": [room_event("hello garage")]}},
        "!workshop:example.org": {"timeline": {"events": [room_event("hello workshop")]}},
        "!elsewhere:example.org": {"timeline": {"events": [room_event("not ours")]}},
    }}}
    pairs = client._room_events(sync)
    assert [(room, event["content"]["body"]) for room, event in pairs] == [
        ("!garage:example.org", "hello garage"), ("!workshop:example.org", "hello workshop")]

    async def flow():
        for room_id, event in pairs:
            await client.handle_event(event, room_id)

    run(flow())
    assert seen == [("!garage:example.org", "hello garage"), ("!workshop:example.org", "hello workshop")]
    assert client._served_rooms() == {"!garage:example.org", "!workshop:example.org"}


def test_matrix_client_without_a_map_serves_the_home_room_only(tmp_path):
    cfg = matrix_cfg(tmp_path)
    cfg["printers"] = []
    client = matrix.MatrixClient(cfg, "https://matrix.example.org", "tok", state_path=tmp_path / "since")
    client.core = S(channel_printers={}, handle_message=None)
    assert client._served_rooms() == {"!garage:example.org"}


# ---------------------------------------------------------------------- plugin-level binding


class StubGateway:
    """The gateway surface the plugin handlers use, with an explicit channel map."""

    def __init__(self, adapter, mapping):
        self._adapter = adapter
        self._map = mapping

    def _is_user_authorized_for_source(self, source):
        return str(source.chat_id) in self._map

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        return self._map.get(str(source.chat_id))


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((str(chat_id), content))
        return S(success=True)

    async def send_clarify(self, chat_id, question, choices, clarify_id=None, session_key=None,
                           metadata=None):
        from print_handy.core import prompts as _prompts
        _prompts.resolve_gateway_clarify(clarify_id, choices[0])
        return S(success=True)


def plugin_event(text, chat_id, printer_id):
    source = runtime.make_source(OPERATOR, chat_id, platform_name="discord", printer_id=printer_id)
    return S(source=source, text=text, internal=False, metadata={})


@pytest.fixture
def plugin_cfg(monkeypatch):
    """Install the two-printer roster as the process config the plugin reads."""
    data = two_printer_cfg(copy.deepcopy(_config.cfg()))
    monkeypatch.setattr(_config, "_override", data)
    return data


def _plugin():
    from print_handy.bot.router import load_plugin
    return load_plugin()


def test_plugin_stop_in_a_routed_channel_acts_on_that_printer(plugin_cfg):
    plugin = _plugin()

    mapping = {"111": "flashforge-ad5x", "222": "workshop-ad5x"}
    adapter = RecordingAdapter()
    gateway = StubGateway(adapter, mapping)
    calls = []

    def fake_status(**kwargs):
        calls.append(("status", kwargs))
        return printing()

    def fake_cancel(filename, **kwargs):
        calls.append(("cancel", filename, kwargs))
        return True, "Stopped."

    with mock.patch.object(print_stop, "status", fake_status), \
            mock.patch.object(print_stop, "cancel", fake_cancel), \
            mock.patch("print_handy.runtime.discord_print_summary.format_event",
                       return_value="Print started: a.gcode"):
        out = run(plugin.print_stop_dispatch(plugin_event("/print-stop", "222", "workshop-ad5x"), gateway))
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    assert ("status", {"printer_id": "workshop-ad5x"}) in calls
    assert ("cancel", "a.gcode", {"printer_id": "workshop-ad5x"}) in calls
    assert adapter.sent and adapter.sent[-1][0] == "222"


def test_plugin_stop_in_the_default_channel_keeps_legacy_call_shapes(plugin_cfg):
    plugin = _plugin()

    mapping = {"111": "flashforge-ad5x", "222": "workshop-ad5x"}
    adapter = RecordingAdapter()
    gateway = StubGateway(adapter, mapping)
    calls = []

    def fake_status(*args, **kwargs):
        calls.append(("status", args, kwargs))
        return printing(state="standby")

    with mock.patch.object(print_stop, "status", fake_status):
        out = run(plugin.print_stop_dispatch(plugin_event("/print-stop", "111", "flashforge-ad5x"), gateway))
    assert out == {"action": "skip", "reason": "deterministic print stop handled"}
    # Routed to the DEFAULT printer == unrouted: the call is exactly today's.
    assert calls == [("status", (), {})]
    assert "Nothing to stop" in adapter.sent[-1][1]


def test_plugin_print_next_refuses_a_plan_belonging_to_the_other_printer(plugin_cfg, tmp_path):
    plugin = _plugin()

    job_json = tmp_path / "JOB.json"
    job_json.write_text(json.dumps({"printer_id": "flashforge-ad5x", "title": "Garage job"}))
    target = {"plan_id": "p1", "part_id": "plate1", "copy": 1, "quantity": 1, "where": "",
              "label": "plate 1", "title": "Garage job", "job_json": str(job_json),
              "gcode": str(tmp_path / "a.gcode"), "gcode_sha256": "x"}
    mapping = {"111": "flashforge-ad5x", "222": "workshop-ad5x"}
    adapter = RecordingAdapter()
    gateway = StubGateway(adapter, mapping)
    with mock.patch.object(print_next, "load_plan", return_value={}), \
            mock.patch.object(print_next, "resolve_next", return_value=target), \
            mock.patch.object(print_next, "start", return_value=(True, "")) as start:
        out = run(plugin.print_next_dispatch(plugin_event("/print-next p1", "222", "workshop-ad5x"), gateway))
    assert out == {"action": "skip", "reason": "deterministic next plate handled"}
    start.assert_not_called()  # channel B's button can never start printer A's job
    message = adapter.sent[-1][1]
    assert "Garage AD5X" in message and "Workshop AD5X" in message  # both printers named


def test_print_next_start_checks_the_target_jobs_printer(tmp_path):
    """print_next.start's idle check reads the printer the JOB belongs to, not the configured one."""
    gcode = tmp_path / "a.gcode"
    gcode.write_text("G1 X1\n")
    from print_handy.runtime import print_next as pn
    job_json = tmp_path / "JOB.json"
    job_json.write_text(json.dumps({"printer_id": "workshop-ad5x"}))
    target = {"plan_id": "p", "part_id": "plate1", "copy": 1, "gcode": str(gcode),
              "gcode_sha256": pn._sha256(gcode), "job_json": str(job_json)}
    checked = []
    stub = tmp_path / "print-now-stub"
    stub.write_text("#!/bin/sh\necho started\n")
    stub.chmod(0o755)

    async def flow():
        with mock.patch.object(pn, "live_idle_check", lambda pid=None: checked.append(pid)):
            return await pn.start(target, model_job=stub, claims_dir=tmp_path / "claims", timeout=5)

    ok, _detail = run(flow())
    assert ok is True
    assert checked == ["workshop-ad5x"]


def test_settings_roster_still_validates():
    # The roster the bot tests use is a valid settings roster shape.
    data = settings.load(local=False, environ={})
    assert settings.default_printer_id(data)
