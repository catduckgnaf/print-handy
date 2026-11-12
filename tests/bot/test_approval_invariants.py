"""Cross-modality approval / yolo invariants (testing-hardening pass).

Two audit findings motivate this suite:

* #790: a typed answer once bypassed the approval check that buttons
  enforced. The check now lives in one place
  (``BasePlatformAdapter.may_approve_id``); this suite drives an approval
  prompt through EVERY answer path the platform seam offers — button
  index, typed label, typed "yes", typed number — for an approver, a
  plain member and a denied user, plus the ordinary-prompt control cells,
  so a future seam cannot quietly re-open one path.
* #978 finding F1: under yolo, ``prepared_intake._ask`` auto-answered via
  ``yolo.auto_answer``. The AI-fix offer's labels matched neither the
  preferred-go labels nor the never-auto list, so ``auto_answer`` returned
  index 0 — the fix was applied with no human answer — even though
  ``ai_fix`` documents that a fix "is never recorded as a yolo waiver".
  This suite was written with that invariant as a registered strict
  xfail; PR #987 (the #978 remediation) then landed the guard in
  ``_ask`` itself — a yolo pick naming a fix label is refused and the
  question goes to a person — and the tripwire fired on rebase, so the
  marker and its register entry were removed and the invariant is a
  plain passing test below. That is the register working as designed.

The matrix uses the platform-neutral seam fake; Discord and Telegram
concrete-adapter cells for the same invariants live in
``tests/bot/test_approval_buttons.py`` and are not duplicated here.
"""
import asyncio
from types import SimpleNamespace

import pytest

from print_handy.bot import platform
from print_handy.bot.platform import SendResult
from print_handy.core import ai_fix, prompts, yolo
from print_handy.runtime import print_next

OPERATOR = "200000000000000002"
MEMBER = "200000000000000777"
APPROVER = "200000000000000555"
DENIED = "200000000000000999"

APPROVAL_CHOICES = ["Print anyway (accept the risk)", "Cancel"]
ORDINARY_CHOICES = ["PLA", "PETG", "Cancel"]

_counter = [0]


def run(coro):
    return asyncio.run(coro)


class OpenAdapter(platform.BasePlatformAdapter):
    """A channel-boundary fake platform: presses arrive via answer_choice."""

    name = "fake"
    channel_is_boundary = True
    supports_buttons = True
    max_buttons = 10

    def __init__(self, allowed=(), *, approver=None, denied=(), approve_all=False):
        super().__init__(allowed, denied=denied, approver=approver,
                         approve_all=approve_all)
        self.clarifies = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key, metadata=None):
        labels = [str(c) for c in choices]
        self.clarifies.append({"clarify_id": str(clarify_id), "choices": labels})
        self._record_prompt(clarify_id, {"choices": labels, "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)


def open_prompt(adapter, choices, *, approval):
    """Register a fresh prompt in the real registry and the fake adapter."""
    _counter[0] += 1
    cid = f"e1{_counter[0]:030d}"
    prompts.register(cid, "sess", "Question?", list(choices), approval=approval)
    adapter._record_prompt(cid, {"choices": list(choices), "question": "Question?"})
    return cid


@pytest.fixture(autouse=True)
def _no_leaked_prompts():
    yield
    # Leave the shared registry as found: other suites assert on the count
    # of open prompts.
    for cid in list(prompts._prompts):
        if str(cid).startswith("e1") and prompts.is_open(cid):
            prompts.resolve_gateway_clarify(cid, prompts.CANCELLED)


# --------------------------------------------------------------- the matrix
#
# Every cell uses a FRESH prompt: a resolution closes it, so a refusal cell
# and a success cell can never share one.

@pytest.mark.parametrize("user,allowed", [(APPROVER, True), (MEMBER, False)])
@pytest.mark.parametrize("path", ["button", "typed_label", "typed_yes", "typed_number"])
def test_approval_prompt_matrix(user, allowed, path):
    adapter = OpenAdapter(approver=APPROVER, denied=[DENIED])
    cid = open_prompt(adapter, APPROVAL_CHOICES, approval=True)
    if path == "button":
        result = run(adapter.answer_choice(cid, 0, user_id=user))
        resolved = result == APPROVAL_CHOICES[0]
    elif path == "typed_label":
        resolved = run(adapter.answer_typed("sess", APPROVAL_CHOICES[0], user_id=user))
    elif path == "typed_yes":
        resolved = run(adapter.answer_typed("sess", "yes", user_id=user))
    else:
        resolved = run(adapter.answer_typed("sess", "1", user_id=user))
    assert resolved is allowed, (path, user)
    assert prompts.is_open(cid) is (not allowed), (path, user)


def test_approval_prompt_refuses_a_denied_user_on_every_path():
    adapter = OpenAdapter(approver=APPROVER, denied=[DENIED])
    cid = open_prompt(adapter, APPROVAL_CHOICES, approval=True)
    assert run(adapter.answer_choice(cid, 0, user_id=DENIED)) is None
    assert run(adapter.answer_typed("sess", "yes", user_id=DENIED)) is False
    assert prompts.is_open(cid)


@pytest.mark.parametrize("path", ["button", "typed_label", "typed_number"])
def test_ordinary_prompt_stays_open_to_a_member(path):
    adapter = OpenAdapter(approver=APPROVER, denied=[DENIED])
    cid = open_prompt(adapter, ORDINARY_CHOICES, approval=False)
    if path == "button":
        assert run(adapter.answer_choice(cid, 0, user_id=MEMBER)) == "PLA"
    elif path == "typed_label":
        assert run(adapter.answer_typed("sess", "PETG", user_id=MEMBER)) is True
    else:
        assert run(adapter.answer_typed("sess", "2", user_id=MEMBER)) is True
    assert not prompts.is_open(cid)


def test_typed_and_button_paths_share_one_may_approve_check():
    """The #790 shape: typed must consult the same predicate as buttons."""
    adapter = OpenAdapter(approver=APPROVER)
    assert adapter.typed_approval_blocked("sess", MEMBER) is False  # nothing open
    cid = open_prompt(adapter, APPROVAL_CHOICES, approval=True)
    assert adapter.typed_approval_blocked("sess", MEMBER) is True
    assert adapter.typed_approval_blocked("sess", APPROVER) is False
    assert adapter.may_approve_id(MEMBER) is False
    assert prompts.is_open(cid)


# ------------------------------------------------------------- yolo seam
#
# What yolo MAY auto-answer (pinned as correct): a preferred go label, and
# nothing when every label is negative or never-auto. What it must never
# do is select a fix/waiver application — see the strict xfail below.

def test_yolo_prefers_the_go_label_for_a_print_offer():
    assert yolo.auto_answer(["Print anyway", "Cancel"]) == 0
    assert yolo.auto_answer(["Cancel", "Print anyway"]) == 1
    assert yolo.auto_answer(
        ["Start next plate · bed is clear", "Cancel"]) == 0


def test_yolo_answers_nothing_when_a_never_auto_action_is_offered():
    assert yolo.auto_answer(["Restart the printer", "Cancel"]) is None
    assert yolo.auto_answer(["Delete the file", "Cancel"]) is None
    assert yolo.auto_answer(["Make it bigger", "Cancel"]) is None


def test_yolo_falls_back_to_the_safe_side_when_all_labels_are_negative():
    # Documented fallback in auto_answer: with no go label and no
    # never-auto label, the first label wins — for an all-negative offer
    # that is the cancel side, never an action.
    assert yolo.auto_answer(["Cancel", "Not now"]) == 0


def test_yolo_never_auto_applies_an_ai_fix(monkeypatch):
    """Driving the REAL question seam: with yolo active, asking the
    fix-application question must not return the Apply index without a
    human answer. The human path is stubbed at the seam ``_ask`` falls
    back to, answering Cancel — any outcome except "returned 0" passes:
    a refusal, the human path, or a cancel are all correct; silently
    applying the fix is the bug (#978 F1, fixed by PR #987's guard in
    ``_ask``; this test is its regression pin)."""
    from print_handy.core import prepared_intake

    human_calls = []

    async def fake_human_ask(adapter, source, key, question, choices,
                             *args, **kwargs):
        human_calls.append(list(choices))
        return list(choices).index(print_next.CANCEL_LABEL)

    monkeypatch.setattr(prepared_intake, "ask", fake_human_ask)
    adapter = OpenAdapter(approver=APPROVER)
    source = SimpleNamespace(chat_id="chan", thread_id=None,
                             user_id=APPROVER, printer_id=None)
    token = yolo.ACTIVE.set(True)
    try:
        pick = run(prepared_intake._ask(
            adapter, source, "sess",
            "Apply the AI-suggested fix?",
            [ai_fix.APPLY_LABEL, print_next.CANCEL_LABEL], timeout=5))
    finally:
        yolo.ACTIVE.reset(token)
    assert pick != 0, "yolo auto-applied an AI fix with no human answer"
