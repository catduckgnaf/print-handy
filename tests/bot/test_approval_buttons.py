"""Print approval stays a separate check from open channel access (P1-93).

With only ``approval_user_id`` set, the print channel is open for intake
(#386): any member may chat, drop links and answer ordinary questions. A
prompt marked as a print approval is different — it starts a print, so only
the approval user may resolve it. With no approval user configured, an
approval prompt behaves like any other prompt, as before.
"""
import asyncio

from corpus import fake_discord
from print_handy.bot import platform, runtime
from print_handy.bot.adapter import parse_custom_id
from print_handy.bot.platform import SendResult
from print_handy.core import link_intake, prompts

OPERATOR = "200000000000000002"
MEMBER = "200000000000000777"
APPROVER = "200000000000000555"


def run(coro):
    return asyncio.run(coro)


class OpenAdapter(platform.BasePlatformAdapter):
    """A channel-boundary fake platform: presses arrive via answer_choice."""

    name = "fake"
    channel_is_boundary = True
    supports_buttons = True
    max_buttons = 10

    def __init__(self, allowed=(), *, approver=None, denied=(), approve_all=False):
        super().__init__(allowed, denied=denied, approver=approver, approve_all=approve_all)
        self.clarifies = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        labels = [str(c) for c in choices]
        self.clarifies.append({"clarify_id": str(clarify_id), "choices": labels})
        self._record_prompt(clarify_id, {"choices": labels, "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)


def register(adapter, cid, *, approval):
    prompts.register(cid, "sess", "Start the print?", ["Print it", "Cancel"], approval=approval)
    adapter._record_prompt(cid, {"choices": ["Print it", "Cancel"], "question": "Start the print?"})


# ---------------------------------------------------------------------- the platform-neutral press

def test_member_cannot_resolve_an_approval_prompt_in_an_open_channel():
    adapter = OpenAdapter(approver=APPROVER)
    assert adapter.channel_open and adapter.is_allowed_id(MEMBER)  # intake stays open
    register(adapter, "a1" * 16, approval=True)
    assert run(adapter.answer_choice("a1" * 16, 0, user_id=MEMBER)) is None
    assert prompts.is_open("a1" * 16)  # the refusal changed nothing
    assert run(adapter.answer_choice("a1" * 16, 0, user_id=APPROVER)) == "Print it"


def test_member_still_answers_an_ordinary_prompt_in_an_open_channel():
    adapter = OpenAdapter(approver=APPROVER)
    register(adapter, "b2" * 16, approval=False)
    assert run(adapter.answer_choice("b2" * 16, 1, user_id=MEMBER)) == "Cancel"


def test_no_approval_user_configured_keeps_approval_prompts_as_before():
    adapter = OpenAdapter()
    register(adapter, "c3" * 16, approval=True)
    assert run(adapter.answer_choice("c3" * 16, 0, user_id=MEMBER)) == "Print it"


def test_a_denied_approval_user_cannot_approve():
    adapter = OpenAdapter(approver=APPROVER, denied=[APPROVER])
    register(adapter, "d4" * 16, approval=True)
    assert run(adapter.answer_choice("d4" * 16, 0, user_id=APPROVER)) is None
    assert prompts.is_open("d4" * 16)
    prompts.resolve_gateway_clarify("d4" * 16, prompts.CANCELLED)  # leave no open prompt behind


def test_may_approve_names_the_check():
    adapter = OpenAdapter(approver=APPROVER)
    assert adapter.may_approve_id(APPROVER)
    assert not adapter.may_approve_id(MEMBER)
    assert adapter.approval_refusal_reason(MEMBER) == "only an approval user may approve prints"
    assert adapter.approval_refusal_reason(APPROVER) is None
    assert OpenAdapter().may_approve_id(MEMBER)  # no approver: the general check decides


# ---------------------------------------------------------------------- the Discord button path

def test_discord_approval_button_refuses_a_member_and_takes_the_approver(cfg):
    cfg["discord"].update(allowed_user_ids="", approval_user_id=APPROVER)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    core = runtime.BotCore(discord.client, cfg)

    async def flow():
        source = runtime.make_source(OPERATOR, cfg["discord"]["chat_id"])
        task = asyncio.create_task(link_intake.ask(
            core.adapter, source, runtime.session_key(source),
            "Ready to slice. Start?", ["Slice and print now (bed is clear)"], approval=True))
        message = None
        for _ in range(200):
            message = discord.last_question()
            if message is not None:
                break
            await asyncio.sleep(0.01)
        assert message is not None
        label, custom_id = next((text, cid) for text, cid in discord.prompt_buttons(message)
                                if "print now" in text)
        member = fake_discord.FakeUser(int(MEMBER), "member")
        clicked = await discord.press(message, custom_id, user=member)
        assert clicked.replies == [{"content": "Not allowed. Nothing changed.", "ephemeral": True}]
        assert prompts.is_open(parse_custom_id(custom_id)[0])  # still waiting for the approver
        approver = fake_discord.FakeUser(int(APPROVER), "approver")
        await discord.press(message, custom_id, user=approver)
        return await task

    assert run(flow()) == 0


def test_discord_ordinary_button_belongs_to_the_person_asked(cfg):
    """#1373 review: Discord clicks follow the #1146 initiator rule like every other
    platform. A member cannot answer the operator's question; an approver can."""
    cfg["discord"].update(allowed_user_ids="", approval_user_id=APPROVER)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    core = runtime.BotCore(discord.client, cfg)

    async def flow():
        source = runtime.make_source(OPERATOR, cfg["discord"]["chat_id"])
        task = asyncio.create_task(link_intake.ask(
            core.adapter, source, runtime.session_key(source), "Which color?", ["Black", "White"]))
        message = None
        for _ in range(200):
            message = discord.last_question()
            if message is not None:
                break
            await asyncio.sleep(0.01)
        buttons = dict(discord.prompt_buttons(message))
        await discord.press(message, buttons["White"], user=fake_discord.FakeUser(int(MEMBER), "member"))
        await asyncio.sleep(0.05)
        assert not task.done()  # the member's press answered nothing
        await discord.press(message, buttons["Black"], user=fake_discord.FakeUser(int(APPROVER), "approver"))
        return await task

    assert run(flow()) == 0


# ---------------------------------------------------------------------- the Telegram button path

def test_telegram_approval_button_refuses_a_whitelisted_member():
    from tests.bot.test_telegram import BotApiStub, CHAT, TOKEN
    from print_handy.bot import telegram

    stub = BotApiStub()
    adapter = telegram.StandaloneAdapter(None, [MEMBER], approver=APPROVER, token=TOKEN, opener=stub)
    cid = "e5" * 16
    prompts.register(cid, "telegram:chat::user", "Start the print?", ["Print it", "Cancel"],
                     approval=True)
    run(adapter.send_clarify(chat_id=CHAT, question="Start the print?", choices=["Print it", "Cancel"],
                             clarify_id=cid, session_key="flow"))

    def callback(index, user):
        return {"id": "q1", "from": {"id": int(user), "first_name": "X"},
                "data": f"pbq:{cid}:{index}",
                "message": {"message_id": 5, "chat": {"id": int(CHAT)}}}

    async def flow():
        assert await adapter.handle_callback_query(callback(0, MEMBER)) is True
        assert prompts.is_open(cid)  # a member's press resolved nothing
        assert await adapter.handle_callback_query(callback(0, APPROVER)) is True
        return await asyncio.to_thread(prompts.wait_for_response, cid, 1)

    assert run(flow()) == "Print it"
    answers = stub.payloads("answerCallbackQuery")
    assert answers[0]["text"] == "Not allowed. Nothing changed."


# ---------------------------------------------------------------------- typed answers (2.2.5 review)

def test_member_cannot_approve_by_typing_in_an_open_channel():
    adapter = OpenAdapter(approver=APPROVER)
    register(adapter, "f6" * 16, approval=True)
    assert adapter.typed_approval_blocked("sess", MEMBER)
    assert run(adapter.answer_typed("sess", "yes", user_id=MEMBER)) is False
    assert prompts.is_open("f6" * 16)  # the typed refusal changed nothing
    assert not adapter.typed_approval_blocked("sess", APPROVER)
    assert run(adapter.answer_typed("sess", "Print it", user_id=APPROVER)) is True
    assert run(adapter.answer_typed("sess", "x")) is False  # nothing open any more


def test_member_still_answers_an_ordinary_prompt_by_typing():
    adapter = OpenAdapter(approver=APPROVER)
    register(adapter, "a7" * 16, approval=False)
    assert run(adapter.answer_typed("sess", "Cancel", user_id=MEMBER)) is True


def test_telegram_typed_approval_refuses_a_member():
    from tests.bot.test_telegram import BotApiStub, CHAT, TOKEN
    from print_handy.bot import telegram

    adapter = telegram.StandaloneAdapter(None, [MEMBER], approver=APPROVER, token=TOKEN, opener=BotApiStub())
    cid = "b8" * 16
    prompts.register(cid, "telegram:chat::user", "Start the print?", ["Print it", "Cancel"], approval=True)
    run(adapter.send_clarify(chat_id=CHAT, question="Start the print?", choices=["Print it", "Cancel"],
                             clarify_id=cid, session_key="flow"))
    assert run(adapter.answer_typed("telegram:chat::user", "yes", user_id=MEMBER)) is False
    assert prompts.is_open(cid)
    assert run(adapter.answer_typed("telegram:chat::user", "Print it", user_id=APPROVER)) is True


def test_discord_typed_approval_is_refused_with_a_notice_and_the_prompt_stays_open(cfg):
    cfg["discord"].update(allowed_user_ids="", approval_user_id=APPROVER)
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR))
    core = runtime.BotCore(discord.client, cfg)
    member = runtime.make_source(MEMBER, cfg["discord"]["chat_id"])
    cid = "c9" * 16

    async def flow():
        prompts.register(cid, runtime.session_key(member), "Start?", ["Print it"], approval=True)
        await core.adapter.send_clarify(chat_id=str(cfg["discord"]["chat_id"]), question="Start?",
                                        choices=["Print it"], clarify_id=cid, session_key="flow")
        try:
            out = await core.process(runtime.make_event(member, "yes", 1))
            assert out == {"action": "skip", "reason": "typed approval refused"}
            assert prompts.is_open(cid)
        finally:
            # Never leave a registry prompt open for later tests (open_count checks).
            prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)

    run(flow())


# ---------------------------------------------------------------------- several approvers / approve all

def test_several_approvers_may_all_approve_and_a_member_still_may_not():
    second = "222222222222222222"
    adapter = OpenAdapter(approver=[APPROVER, second])
    assert adapter.may_approve_id(APPROVER) and adapter.may_approve_id(second)
    assert not adapter.may_approve_id(MEMBER)
    assert adapter.is_allowed_id(second)  # an approver may always act
    assert adapter.approval_refusal_reason(second) is None


def test_approve_all_lets_any_allowed_user_approve_but_deny_still_wins():
    adapter = OpenAdapter(approver=APPROVER, approve_all=True, denied=["9"])
    assert adapter.may_approve_id(MEMBER) and adapter.may_approve_id(APPROVER)
    assert not adapter.may_approve_id("9")


def test_approver_ids_split_on_commas_and_spaces():
    from print_handy import settings
    assert settings.approver_ids({"approval_user_id": "1, 2  3,4"}) == ["1", "2", "3", "4"]
    assert settings.approver_ids({"approval_user_id": ""}) == [] and settings.approver_ids({}) == []
