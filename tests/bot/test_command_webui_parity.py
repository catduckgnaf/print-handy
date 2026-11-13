"""Web UI parity for every command and every proactive alert: the merge-time guard.

The rule (catduck, 2026-11-11): "make sure the update keeps parity with
webui, that should also be a check that is done on merging". Web chat is
a chat platform like the others (print_handy/bot/web.py), so parity is
checked the way a person would notice its absence:

* **Commands.** This suite enumerates ``commands.COMMANDS`` itself. Each
  command's typed form goes through a chat transport (the fake Discord
  client with the real Discord adapter) and through web chat (the real
  WebChatService, its own loop thread, its real adapter and button
  press path). Both must reach a handler, never the unmatched reply,
  and end with the same outcome kind (BotCore's skip reason). A button
  question either side asks is answered the safe way on both (Cancel,
  Not now, ...), so nothing is ever applied. A new command that web chat
  cannot answer like chat fails here until it can, or until it is
  exempted below with the reason.
* **Proactive alerts.** The bot heartbeat on Discord, Telegram and
  Matrix runs BotCore's ``check_*`` alert methods; every one of them
  must run in web chat too (``web.HEARTBEAT_CHECKS``) or be exempted
  below with the reason. The ZMod update alert is driven end to end in
  tests/bot/test_firmware_alert_transports.py.
* **The listener's print posts.** Started, paused, complete, cancelled
  and error posts (and the camera checks and emergency stops) are sent
  by the listener process through the platform REST senders, not
  BotCore; they reach web chat through the feed file (bot/web_feed.py).
  Below, each lifecycle kind is posted through the real listener post
  path on every chat platform, and web chat must show the same core
  facts (the outcome verb, the model title, the printer name on a
  multi-printer roster) with no starter mention; an alert must arrive
  with its actions as buttons that claim once through the alert store;
  and a source scan fails any listener chat sender that bypasses the
  path. Each printer family's own sender is driven in
  tests/bot/test_webui_print_event_families.py.

It runs in the offline-tests job, a required check, so it gates merges.
CONTRIBUTING.md states the rule beside the fuzzy-equivalent one.
"""
import asyncio
import inspect
import re
import time

import pytest

from corpus import fake_discord
from print_handy.bot import client, commands, matrix, platform, runtime, telegram, web
from print_handy.runtime import _config

# Commands whose web chat outcome legitimately differs from chat's. Each
# entry names why; an entry is still driven through web chat and must get
# an honest answer there (never the unmatched reply), only the outcome
# kind is not compared.
COMMAND_EXEMPTIONS = {
    "print-live": ("Live viewing is a Discord Activity in a voice channel (#311). Web chat's "
                   "adapter reports supports_live_stream=False like Telegram's and Matrix's, "
                   "so, as there, the command does not exist (P2-123: no stub, not in help, "
                   "tests/test_live_dispatch.py) and typing it gets the unmatched reply. The "
                   "Web UI's own camera view is the browser equivalent."),
}

# Heartbeat alert checks a chat platform runs that web chat does not.
HEARTBEAT_EXEMPTIONS = {
    "check_listener_outages": (
        "The outage notice exists because the listener is what posts every print alert "
        "to the chat, so its death is the one failure it cannot report there (#1020). "
        "The Web UI does not get its alerts from the listener: its status page reads "
        "each listener's state live (webui/status.py _listener_section). The notice's "
        "delivered and escalation record is one shared file in the state folder, so a "
        "second process ticking it would race the bot's."),
}

# What of the listener's chat posts does not reach web chat as it reaches
# the chat. The listener's print posts themselves do reach it (bot/web_feed.py;
# proved below for every lifecycle kind, platform and printer family).
LISTENER_EXEMPTIONS = {
    "attachments": (
        "The chat page cannot show files. A timelapse video, a completion snapshot or a "
        "camera check photo goes to the chat platform with its post; web chat posts the same "
        "text with one line naming the file and where it went (web_feed.render). Showing the "
        "file would need a media route the Web UI does not have."),
    "inventory and next-plate questions on Discord": (
        "Spool, insertion, new-spool and next-plate picker questions are Discord component "
        "flows (selects, modals, a multi-step walkthrough) that the gateway plugin resolves, "
        "so on a Discord install they go to Discord only. On Telegram and Matrix they are plain "
        "posts through the listener's post(), so they reach web chat there, and the commands "
        "behind them (/print-next, the spool commands) answer in web chat as in chat."),
    "a retried alert's fresh buttons": (
        "When an alert action does not take (#837), handle_click re-posts the alert with fresh "
        "buttons to the chat (print_alert_buttons._repost_alert). A press from web chat gets "
        "the same outcome text there, and the fresh buttons are in the chat."),
}

# The listener's chat button posts that bypass post(): each must hand its
# alert to web chat (_web_alert) in the same function. The others are the
# Discord-only component flows exempted above.
ALERT_BUTTON_POSTS = {"post_with_buttons", "post_visual_alert", "post_visual_text_alert",
                      "post_visual_dark_notice", "post_telegram_with_buttons",
                      "post_matrix_with_reactions", "post_plate_confirm"}
DISCORD_ONLY_BUTTON_POSTS = {"post_plate_picker", "post_insertion_confirmation",
                             "post_spool_question", "post_new_spool"}

SAFE_LABELS = ("cancel", "not now", "keep", "skip", "no", "later", "done")


def _safe_index(labels):
    for want in SAFE_LABELS:
        for index, label in enumerate(labels):
            if want in str(label).casefold():
                return index
    return None


def _command_text(command):
    """The typed form a person sends with no arguments."""
    return "/" + command.name


# ------------------------------------------------------------------ chat side

def _expire(registry, clarify_id):
    """What a question with no safe button does when nobody answers: expire."""
    registry.resolve_gateway_clarify(clarify_id, registry.CANCELLED)


class _SafeDiscord(fake_discord.FakeDiscord):
    """The fake Discord pressing the safe button, or letting the question expire."""

    registry = None

    def on_send(self, message):
        buttons = self.prompt_buttons(message)
        index = _safe_index([label for label, _ in buttons]) if buttons else None
        if not buttons or index is not None:
            self.clicks = [{"question": "", "button": buttons[index][0]}] if buttons else []
            return super().on_send(message)
        self.messages.append(message)
        self.questions.append({"question": message.content,
                               "choices": [label for label, _ in buttons], "picked": None})
        clarify_id, _index = platform.parse_custom_id(buttons[0][1])
        asyncio.get_running_loop().call_soon(_expire, self.registry, clarify_id)


def _chat_outcome(cfg, text):
    discord = _SafeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                           operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg)
    discord.registry = core.adapter.registry

    async def flow():
        out = await asyncio.wait_for(core.handle_message(discord.message(text)), 60)
        for _ in range(200):
            if not discord.pending:
                break
            await asyncio.sleep(0.01)
        return out

    out = asyncio.run(flow())
    replies = [m.content for m in discord.messages if m.content]
    return out, replies


# ------------------------------------------------------------------ web side

class _WebDriver:
    """One WebChatService; records every turn's outcome and presses safely."""

    def __init__(self, cfg):
        self.service = web.WebChatService(cfg)
        self.results = []
        process = self.service.core.process

        async def recording(event, **kwargs):
            out = await process(event, **kwargs)
            self.results.append(out)
            return out

        self.service.core.process = recording
        self.scope = web.scope_for("parity-session")
        self.view = self.service.view(self.scope)

    def outcome(self, text, timeout=60.0):
        seen = len(self.results)
        since = (self.view.entries() or [{"seq": 0}])[-1]["seq"]
        assert self.view.submit(text)
        deadline = time.time() + timeout
        handled = set()
        while time.time() < deadline:
            prompt = self.view.open_prompt()
            if prompt and prompt["clarify_id"] not in handled:
                handled.add(prompt["clarify_id"])
                index = _safe_index(prompt["choices"])
                if index is None:
                    _expire(self.service.adapter.registry, prompt["clarify_id"])
                else:
                    self.view.answer(prompt["clarify_id"], index)
            elif not prompt and len(self.results) > seen and not self.view.busy():
                break
            time.sleep(0.02)
        else:
            raise AssertionError(f"web chat never finished {text!r}: {self.view.entries()}")
        replies = [e.get("content") or e.get("question") or ""
                   for e in self.view.entries(since) if e["kind"] in ("message", "prompt")]
        return self.results[seen], replies


@pytest.fixture
def parity_cfg(cfg, tmp_path, monkeypatch):
    cfg["paths"]["state_home"] = tmp_path / "home"
    cfg.setdefault("firmware", {})["update_alerts"] = False
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


@pytest.fixture
def web_driver(parity_cfg):
    return _WebDriver(parity_cfg)


def _unmatched(core_text, replies):
    return any(core_text in reply for reply in replies)


def test_the_guard_enumerates_the_registry():
    names = {c.name for c in commands.COMMANDS}
    assert set(COMMAND_EXEMPTIONS) <= names, (
        f"stale exemptions: {sorted(set(COMMAND_EXEMPTIONS) - names)}")
    for name, reason in COMMAND_EXEMPTIONS.items():
        assert len(reason) > 40, f"exemption for {name} must say why"


@pytest.mark.parametrize("command", commands.COMMANDS, ids=lambda c: c.name)
def test_every_command_answers_in_web_chat_like_chat(command, parity_cfg, web_driver):
    text = _command_text(command)
    chat_out, chat_replies = _chat_outcome(parity_cfg, text)
    web_out, web_replies = web_driver.outcome(text)
    unmatched = web_driver.service.core.unmatched
    assert web_replies, f"{text} got no answer in web chat"
    assert web_out and web_out.get("reason"), f"{text}: no outcome in web chat: {web_out!r}"
    if command.name in COMMAND_EXEMPTIONS:
        # Web chat behaves like the other platforms without the capability
        # (Telegram, Matrix): no command there, so the short unmatched
        # reply, never a crash and never a half-run flow.
        assert (web_out or {}).get("reason") == "command list", web_out
        return
    assert not _unmatched(unmatched, web_replies), (
        f"{text} fell through to the unmatched reply in web chat: {web_replies}")
    assert chat_replies and not _unmatched(unmatched, chat_replies), (
        f"{text} did not answer on the chat transport: {chat_replies}")
    assert (web_out or {}).get("reason") == (chat_out or {}).get("reason"), (
        f"{text}: web chat ended {web_out!r}, chat ended {chat_out!r}")


# ------------------------------------------------------------------ alerts

_CHECK_CALL = re.compile(r"await\s+self\.core\.(check_\w+)\(\)")


def _chat_heartbeat_checks():
    found = {}
    for module in (client, telegram, matrix):
        found[module.__name__] = set(_CHECK_CALL.findall(inspect.getsource(module)))
    return found


def test_every_heartbeat_alert_check_reaches_web_chat():
    per_platform = _chat_heartbeat_checks()
    assert all(per_platform.values()), per_platform  # the scan found the heartbeats
    union = set().union(*per_platform.values())
    covered = set(web.HEARTBEAT_CHECKS) | set(HEARTBEAT_EXEMPTIONS)
    assert union <= covered, (
        f"alert checks chat runs that web chat neither runs nor exempts: "
        f"{sorted(union - covered)}; add them to web.HEARTBEAT_CHECKS or, with "
        "the reason, to HEARTBEAT_EXEMPTIONS here")
    assert not set(web.HEARTBEAT_CHECKS) & set(HEARTBEAT_EXEMPTIONS)
    assert set(HEARTBEAT_EXEMPTIONS) <= union, "stale heartbeat exemptions"
    for name in web.HEARTBEAT_CHECKS:
        assert inspect.iscoroutinefunction(getattr(runtime.BotCore, name)), name
    for reason in (*HEARTBEAT_EXEMPTIONS.values(), *LISTENER_EXEMPTIONS.values()):
        assert len(reason) > 40


def test_web_chat_heartbeat_runs_the_alert_checks(parity_cfg, web_driver):
    service = web_driver.service
    calls = []

    async def fake_check():
        calls.append("check_firmware_updates")
        return []

    service.core.check_firmware_updates = fake_check
    future = asyncio.run_coroutine_threadsafe(service.heartbeat_once(), service._loop)
    future.result(timeout=10)
    # (The service's own background heartbeat may run a pass too.)
    assert set(calls) == set(web.HEARTBEAT_CHECKS)
    # Off, the page shows nothing, so the heartbeat delivers nothing.
    parity_cfg.setdefault("webui", {})["chat_enabled"] = False
    calls.clear()
    asyncio.run_coroutine_threadsafe(service.heartbeat_once(), service._loop).result(timeout=10)
    assert calls == []


# ------------------------------------------------------------------ the listener's print posts

# Every lifecycle kind the listener's chat senders deliver, with the verb
# format_event renders. (A resume posts nothing of its own: it retires
# the pause alert.)
LIFECYCLE_VERBS = {"started": "Print started", "paused": "Print paused",
                   "complete": "Print complete", "cancelled": "Print cancelled",
                   "error": "Print stopped with an error"}
CHAT_PLATFORMS = ("discord", "telegram", "matrix")


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
@pytest.mark.parametrize("kind", sorted(LIFECYCLE_VERBS))
def test_every_lifecycle_post_reaches_web_chat_with_the_same_facts(print_event_rig, platform, kind):
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig
    rig.install(platform)
    text = rig.render(kind)
    assert mel.post(text, ping_kind=kind, ping_job_key="plate.gcode")
    sent = [row for row in rig.sent if row[0] == platform]
    assert sent and sent[-1][1] == text
    shown = rig.wait_web(lambda body: body.startswith(f"{LIFECYCLE_VERBS[kind]}: Widget"))
    # The same core facts the chat got: verb, model title, printer name.
    assert "Printer: Workshop AD5X" in shown
    # No web post ever carries the starter mention, even where Discord pinged.
    assert all("<@" not in body for body in rig.web_texts())
    if platform == "discord" and kind in ("complete", "cancelled", "error"):
        assert sent[-1][2] == rig.STARTER  # the ping stayed a Discord send-path decision
    # Exactly once: a print post is a shared post every session sees.
    other = rig.service.view(web.scope_for("another-session"))
    assert sum(shown == (e.get("content") or "") for e in other.entries()) == 1


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_a_pause_alert_reaches_web_chat_with_its_buttons_and_claims_once(print_event_rig,
                                                                         platform, monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    from print_handy.runtime import print_alert_buttons as pab
    rig = print_event_rig
    rig.install(platform)
    clicks = []
    monkeypatch.setattr(pab, "handle_click",
                        lambda action, alert: clicks.append((action, alert)) or "Cancel sent.")
    assert mel.post_alert(rig.render("paused"), "paused", "plate.gcode")
    prompt = rig.wait_prompt("Print paused: Widget")
    assert prompt["choices"] == ["Resume print", "Cancel print"]
    assert "<@" not in prompt["question"]
    # The chat got it once, and web chat got it once (never as a second plain post).
    assert sum(t.startswith("Print paused: Widget") for t in rig.web_texts()) == 1
    assert rig.view.answer(prompt["clarify_id"], 1) == "Cancel print"
    rig.wait_web(lambda body: body == "Cancel sent.")
    assert [action for action, _alert in clicks] == ["cancel"]
    assert clicks[0][1]["kind"] == "paused" and clicks[0][1]["filename"] == "plate.gcode"
    # Taken once: the prompt is closed, a second press resolves nothing.
    assert rig.view.answer(prompt["clarify_id"], 0) is None
    assert len(clicks) == 1


def test_an_alert_retired_at_print_end_closes_its_web_buttons(print_event_rig, monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    from print_handy.runtime import print_alert_buttons as pab
    rig = print_event_rig
    monkeypatch.setattr(pab, "handle_click", lambda *a: pytest.fail("nothing may be sent"))
    mel.post_alert(rig.render("paused"), "paused", "plate.gcode")
    prompt = rig.wait_prompt("Print paused: Widget")
    pab.retire_alerts("", "plate.gcode", "Print ended.")
    deadline = time.time() + 10
    while rig.service.adapter.registry.is_open(prompt["clarify_id"]) and time.time() < deadline:
        time.sleep(0.02)
    assert not rig.service.adapter.registry.is_open(prompt["clarify_id"])
    assert rig.view.answer(prompt["clarify_id"], 1) is None


def test_a_camera_check_reaches_web_chat_with_its_actions_and_names_the_photo(print_event_rig,
                                                                              monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    from print_handy.runtime import print_alert_buttons as pab
    rig = print_event_rig
    frame = rig.archive.parent / "frame.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    monkeypatch.setattr(mel._visual_watch, "write_frame", lambda filename, image: frame)
    monkeypatch.setattr(mel.print_markers, "record", lambda *a, **k: None)
    monkeypatch.setattr("print_handy.bot.rest.attachable", lambda path: frame)
    clicks = []
    monkeypatch.setattr(pab, "handle_click",
                        lambda action, alert: clicks.append(action) or "Monitoring continues.")
    mel._visual_alert("plate.gcode", b"img", "Looks like spaghetti.", identity="job-1")
    prompt = rig.wait_prompt("Print camera check")
    assert prompt["choices"] == ["Pause", "It's fine", "Stop asking for this print"]
    assert "Looks like spaghetti." in prompt["question"]
    # The photo cannot be shown here: one line says so, and no server path leaks.
    assert "frame.jpg went to Discord" in prompt["question"]
    assert str(frame) not in prompt["question"]
    rig.view.answer(prompt["clarify_id"], 1)
    rig.wait_web(lambda body: body == "Monitoring continues.")
    assert clicks == ["acknowledge"]


def test_a_completion_with_a_timelapse_names_the_video_in_web_chat(print_event_rig, monkeypatch):
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig
    video = rig.archive.parent / "timelapse.mp4"
    video.write_bytes(b"mp4")
    monkeypatch.setattr("print_handy.bot.rest.attachable", lambda path: video)
    assert mel.post(rig.render("complete") + f"\nMEDIA:{video}")
    shown = rig.wait_web(lambda body: body.startswith("Print complete: Widget"))
    assert "timelapse.mp4 went to Discord with this post" in shown
    assert "MEDIA:" not in shown and str(video) not in shown


@pytest.mark.parametrize("platform", CHAT_PLATFORMS)
def test_the_next_plate_offer_reaches_web_chat_naming_the_confirmed_command(print_event_rig,
                                                                            platform):
    """The empty-bed plate confirm: on Discord a button post, elsewhere the
    text the caller posts. Web chat gets the text either way, and it names
    `/print-next`, which web chat runs through the same confirmed flow."""
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig
    rig.install(platform)
    choice = {"action": "offer", "plan_id": "plan1", "part_id": "p2",
              "text": "Bed looks clear. Send plate 2?\nUse `/print-next plan1` to start it. "
                      "Nothing starts until you confirm."}
    if not mel.post_bed_clear_offer(choice):
        assert platform != "discord"
        assert mel.post(choice["text"])
    rig.wait_web(lambda body: body.startswith("Bed looks clear.") and "/print-next plan1" in body)
    assert sum(t.startswith("Bed looks clear.") for t in rig.web_texts()) == 1


def test_a_failed_web_delivery_never_breaks_the_chat_post(print_event_rig, monkeypatch):
    from print_handy.bot import web_feed
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(web_feed, "feed_path", broken)
    monkeypatch.setattr(web_feed, "alert_spec", broken)
    assert mel.post(rig.render("complete"), ping_kind="complete", ping_job_key="plate.gcode")
    assert rig.sent and rig.sent[-1][1].startswith("Print complete: Widget")
    assert mel.post_alert(rig.render("paused"), "paused", "plate.gcode")
    assert rig.button_posts


def test_a_refused_chat_post_is_not_mirrored(print_event_rig, monkeypatch):
    """Web chat shows what the chat got: a refused completion is retried
    by the listener later, and is mirrored then, once."""
    from print_handy.bot import web_feed
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig
    monkeypatch.setattr(mel, "_post_rest", lambda message, mention_user_id=None: False)
    assert not mel.post(rig.render("complete"))
    assert not [row for row in web_feed.FeedReader(mel._config.state_dir(), since=0).poll()
                if "Widget" in row.get("text", "")]


def test_the_listener_writes_nothing_with_the_chat_page_off(print_event_rig):
    from print_handy.bot import web_feed
    from print_handy.runtime import moonraker_event_listener as mel
    rig = print_event_rig
    rig.cfg.setdefault("webui", {})["chat_enabled"] = False
    assert mel.post(rig.render("started"))
    mel.post_alert(rig.render("paused"), "paused", "plate.gcode")
    assert not [row for row in web_feed.FeedReader(mel._config.state_dir(), since=0).poll()
                if "Widget" in row.get("text", "")]


# Listener modules whose chat sends the scan below checks.
_LISTENER_MODULES = ("listener_common", "moonraker_event_listener", "family_control", "family_event_listener",
                     "bambu_watch", "prusa_watch", "octo_watch", "elegoo_watch", "stock_watch",
                     "moonraker_discord_announcements")
_REST_SENDS = {("rest", "send"), ("telegram_rest", "send"), ("matrix_rest", "send")}


def _calls(tree):
    import ast
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = set()
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    owner = node.func.value
                    names.add((owner.id if isinstance(owner, ast.Name) else "", node.func.attr))
            yield func, names


def test_no_listener_chat_send_bypasses_web_chat():
    """A new listener sender fails here until its posts reach web chat.

    A REST send is allowed only inside the one post path (post's
    _post_rest, which mirrors what it delivered); a chat button post must
    hand its alert to web chat (_web_alert) in the same function, or be
    one of the Discord-only component flows exempted above."""
    import ast
    import importlib
    for name in _LISTENER_MODULES:
        module = importlib.import_module(f"print_handy.runtime.{name}")
        source = inspect.getsource(module)
        for func, calls in _calls(ast.parse(source)):
            body = ast.get_source_segment(source, func) or ""
            rest_sends = calls & _REST_SENDS
            assert not rest_sends or func.name in ("_post_rest", "_post_rest_discord"), (
                f"{name}.{func.name} sends to the chat outside post(): {sorted(rest_sends)}")
            buttons = {attr for _owner, attr in calls} & ALERT_BUTTON_POSTS
            if buttons:
                assert "_web_alert(" in body or "_web_publish(" in body, (
                    f"{name}.{func.name} posts an alert with buttons ({sorted(buttons)}) "
                    "but never hands it to web chat (_web_alert)")
            other = {attr for owner, attr in calls if owner == "pab" and attr.startswith("post_")}
            assert other <= ALERT_BUTTON_POSTS | DISCORD_ONLY_BUTTON_POSTS, (
                f"{name}.{func.name}: a new button post {sorted(other)} needs web chat "
                "delivery or a reasoned entry in LISTENER_EXEMPTIONS")
