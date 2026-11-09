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
