"""Web chat: BotCore hosted in the Web UI process, full parity rules.

The web layer only delivers text and button presses; everything runs
through BotCore.process. These tests drive the real service (its own
event-loop thread, the real BotCore, the real prompts registry) with
the standalone cfg fixture: help answers, the rate limit refuses,
chat_llm_enabled gates the core's LLM lane, and the operator is the
only allowed user and the approval user.
"""
import time

from print_handy.bot import web


def wait_for(service, kind, needle="", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in service.entries():
            if entry["kind"] == kind and needle in (entry.get("content")
                                                    or entry.get("question")
                                                    or ""):
                return entry
        time.sleep(0.02)
    raise AssertionError(f"no {kind} entry containing {needle!r}; "
                         f"entries: {service.entries()}")


def test_help_flows_through_botcore(cfg):
    service = web.WebChatService(cfg)
    assert service.submit("help") is True
    entry = wait_for(service, "message")
    assert entry["content"]  # the bot's own help text, not a web page string


def test_operator_is_the_only_user_and_the_approver(cfg):
    service = web.WebChatService(cfg)
    assert service.adapter.is_allowed_id(web.OPERATOR_ID)
    assert not service.adapter.is_allowed_id("someone-else")
    assert service.adapter.may_approve_id(web.OPERATOR_ID)
    assert not service.adapter.may_approve_id("someone-else")
    assert service.adapter.supports_buttons
    assert service.core.platform_name == "webui"
    assert service.core.client is None


def wait_prompt(service, question, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        prompt = service.open_prompt()
        if prompt and prompt["question"].startswith(question):
            return prompt
        time.sleep(0.02)
    raise AssertionError(f"no open prompt {question!r}; "
                         f"entries: {service.entries()}")


def test_model_link_flows_through_intake_to_the_confirm_prompt(cfg, monkeypatch):
    """A model link in web chat reaches the handlers (PR #1106 review).

    The print-feedback handlers gate on BOT_PLATFORMS; 'webui' was
    missing, so a link fell through to the unmatched reply and intake
    never ran. With prepare_link stubbed the way the other platforms'
    intake tests stub it, the link must reach the color question and
    then the confirm-before-print question, answered by button press.
    """
    from unittest import mock

    from print_handy.bot import runtime
    from print_handy.core import link_intake, prepared_intake, printer_state
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key,
                                      "Which color?", ["Black", "White"])
        await adapter_arg.send(chat_id=source.chat_id,
                               content=f"Sliced in {['Black', 'White'][color]}.")
        start = await link_intake.ask(adapter_arg, source, key,
                                      "Start the print?", ["Print it"])
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    service = web.WebChatService(cfg)
    with mock.patch.object(prepared_intake, "prepare_link", fake_prepare), \
            mock.patch.object(printer_state, "load", return_value={}):
        assert service.submit(
            "https://www.printables.com/model/123456-web-chat-widget") is True
        prompt = wait_prompt(service, "Which color?")
        service.answer(prompt["clarify_id"], 1)
        prompt = wait_prompt(service, "Start the print?")
        service.answer(prompt["clarify_id"], 0)
        wait_for(service, "message", "Started.")
    assert seen["answers"] == (1, 0)


def test_print_source_command_reaches_its_handler(cfg, monkeypatch):
    """/print-source is a deterministic handler, not stray chat.

    Before 'webui' joined BOT_PLATFORMS this fell through every
    handler and landed on the unmatched reply.
    """
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    service = web.WebChatService(cfg)
    assert service.submit("/print-source") is True
    entry = wait_for(service, "message")
    assert "didn't understand" not in entry["content"].casefold()
    assert "printables" in entry["content"].casefold()


def test_print_design_reaches_the_design_handler(cfg, monkeypatch):
    """/print-design runs design_dispatch, which answers honestly here.

    The test config configures no design endpoint, so the handler's
    reply is its deterministic not-set-up refusal — the point is that
    the handler produced it, not the stray-chat fallthrough.
    """
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    service = web.WebChatService(cfg)
    assert service.submit("/print-design a small storage box") is True
    entry = wait_for(service, "message", "Design is not set up")
    assert "didn't understand" not in entry["content"].casefold()


def test_a_handler_error_posts_a_failure_entry(cfg, monkeypatch):
    """submit() must not swallow a process() exception (PR #1106 review).

    The future from run_coroutine_threadsafe was discarded, so a
    handler exception produced no reply and no log. The service now
    logs it and posts a failure entry the page renders.
    """
    service = web.WebChatService(cfg)

    async def boom(event, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(service.core, "process", boom)
    assert service.submit("anything") is True
    entry = wait_for(service, "message", "went wrong")
    assert "kaboom" not in entry["content"]


def test_media_markers_become_a_filename_note_not_a_path(cfg, tmp_path):
    """Web chat cannot attach files; a MEDIA: marker must not leak paths.

    Other platforms turn the marker into an attachment. The web
    adapter renders the file's name only — never the server path the
    marker carries — and a marker for a missing file is dropped.
    """
    import asyncio

    target = tmp_path / "plate-1.gcode"
    target.write_text("gcode")
    service = web.WebChatService(cfg)
    asyncio.run(service.adapter.send(
        web.CHAT_ID, f"Roast time.\nMEDIA:{target}\nMEDIA:{tmp_path}/gone.gcode"))
    contents = [e.get("content") or "" for e in service.entries()]
    assert any("Roast time." in c for c in contents)
    assert any("plate-1.gcode" in c for c in contents)
    assert all(str(tmp_path) not in c for c in contents)
    assert all("MEDIA:" not in c for c in contents)
    assert all("gone.gcode" not in c for c in contents)


def test_chat_page_refreshes_only_while_a_reply_is_pending(cfg, monkeypatch):
    """The 3-second meta refresh must not wipe a half-typed message.

    It fires only while the bot is working on a reply with no question
    open: idle pages and pages waiting on the operator's button press
    render without it (PR #1106 review).
    """
    import asyncio

    from print_handy.core import prompts
    from print_handy.webui.chat import render_chat_page

    service = web.WebChatService(cfg)
    assert 'http-equiv="refresh"' not in render_chat_page(service, form_token="t")

    async def slow(event, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(service.core, "process", slow)
    assert service.submit("a slow one") is True
    deadline = time.time() + 5
    while not service.busy() and time.time() < deadline:
        time.sleep(0.02)
    assert service.busy()
    assert 'http-equiv="refresh"' in render_chat_page(service, form_token="t")

    # A question is open: the page already shows it, and the operator
    # may be typing — no refresh even though the turn is still running.
    prompts.register("cid-refresh", "webui:webui::webui", "Which one?",
                     ["Small", "Large"])
    service.adapter._record_prompt(
        "cid-refresh", {"choices": ["Small", "Large"], "question": "Which one?"})
    assert 'http-equiv="refresh"' not in render_chat_page(service, form_token="t")


def test_rate_limit_refuses_beyond_the_minute_budget(cfg):
    service = web.WebChatService(cfg)
    results = [service.submit("help") for _ in range(web.RATE_LIMIT_PER_MINUTE + 1)]
    assert results[-1] is False
    assert all(results[:-1])


def test_chat_llm_enabled_gates_the_core_lane(cfg):
    cfg["webui"]["chat_llm_enabled"] = False
    service = web.WebChatService(cfg)
    assert service.core.use_llm is False
    assert web.chat_enabled(cfg) is False
    cfg["webui"]["chat_llm_enabled"] = True
    assert web.chat_enabled(cfg) is True


def test_adapter_records_prompts_and_answers_choices(cfg):
    import asyncio

    from print_handy.core import prompts

    service = web.WebChatService(cfg)
    adapter = service.adapter

    async def ask_and_answer():
        result = await adapter.send_clarify(
            web.CHAT_ID, "Which one?", ["Small", "Large"], "cid-web-1",
            "webui:webui::webui")
        assert result.success
        assert service.open_prompt()["choices"] == ["Small", "Large"]
        label = await adapter.answer_choice("cid-web-1", 1,
                                            user_id=web.OPERATOR_ID)
        return label

    # Register the prompt in the registry the way ask() does (post-#1109
    # prompts.gateway() is always this core registry; there is no
    # activate() switch left to flip).
    prompts.register("cid-web-1", "webui:webui::webui", "Which one?",
                     ["Small", "Large"])
    label = asyncio.run(ask_and_answer())
    assert label == "Large"


def test_open_prompt_survives_a_prompt_arriving_mid_snapshot(cfg):
    """open_prompt() renders while the bot loop records a new prompt.

    The /chat page calls open_prompt() on a Web UI request thread while
    _record_prompt runs on the bot event-loop thread. Building the
    snapshot from the live prompts dict raised RuntimeError
    ("dictionary changed size during iteration") and failed the request
    instead of rendering the page (PR #1106 review 5821794465).
    """
    import threading

    from print_handy.core import prompts

    service = web.WebChatService(cfg)
    adapter = service.adapter
    for index in range(8):
        cid = f"cid-race-{index}"
        prompts.register(cid, "webui:webui::webui", "Which one?",
                         ["Small", "Large"])
        adapter._record_prompt(cid, {"choices": ["Small", "Large"],
                                     "question": "Which one?"})

    class RacyPrompts(dict):
        """A prompts dict whose snapshot iteration is interrupted, once
        underway, by a new prompt arriving on another thread."""

        def items(self):
            iterator = iter(super().items())

            def iterate():
                yield next(iterator)
                prompts.register("cid-race-late", "webui:webui::webui",
                                 "Late?", ["Yes", "No"])

                def arrive():
                    adapter._record_prompt(
                        "cid-race-late", {"choices": ["Yes", "No"],
                                          "question": "Late?"})

                thread = threading.Thread(target=arrive)
                thread.start()
                # Unguarded, arrive() mutates the dict at once and the
                # iteration below raises RuntimeError; guarded, it waits
                # on the prompts lock until the snapshot is complete, so
                # this join simply times out and the snapshot stands.
                thread.join(timeout=3)
                yield from iterator

            return iterate()

    adapter._prompts = RacyPrompts(adapter._prompts)
    result = service.open_prompt()
    assert result is not None
    assert result["clarify_id"] == "cid-race-7"
    assert result["choices"] == ["Small", "Large"]
