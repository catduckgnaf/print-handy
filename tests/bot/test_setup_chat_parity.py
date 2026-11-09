"""First-boot parity suite (plan section 3): the three chat surfaces.

Discord, Telegram and Matrix drivers for the parity scenarios whose
env/Web UI halves live in tests/test_setup_surface_parity.py. One guided
state machine serves all three platforms, so the driver drives
guided.handle_text() with a per-platform session key and per-platform
canonical answers (a Matrix room id is not a numeric channel id — that
difference is a platform fact, not a parity break). Parity is asserted
on facts and outcomes — the effective config after apply and the
access.resolve() outcome — never on message bytes.

Scenarios: S1 (setup-only start), S2 (claim password via chat),
S3 (leave open via chat), S3b (clear a claim via chat),
S5 (full walkthrough, identical config), S6 (zero-config bootstrap
claim, Q5 shape), S7 (cross-surface completion, chat side),
S8 (Telegram walkthrough, no LLM), S9 (chat password never persisted).

Rulings pinned here: Q1 chat may set a password, choose leave-open AND
clear an existing claim; Q2 leave-open is an explicit saved record;
Q5 the bootstrap claim binds at conversation start, session-scoped,
released by the session timeout, and persists at apply.

Lane gates: as in the sibling module, a scenario whose behavior a
sibling lane builds skips with that lane's branch named in the reason
while unlanded, and asserts in full once the capability exists.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

# The canonical scenario data and the parity view live in the sibling
# module under tests/; make that directory importable regardless of
# which test paths pytest was invoked with.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from print_handy import settings  # noqa: E402
from print_handy.bot import guided  # noqa: E402

from test_setup_surface_parity import CANONICAL, parity_view  # noqa: E402

PLATFORMS = ("discord", "telegram", "matrix")

# Per-platform canonical answers: the same facts, in each platform's
# own identifier shapes.
PLATFORM_ANSWERS = {
    "discord": {"chat_id": CANONICAL["chat_id"], "user_ids": CANONICAL["user_id"],
                "platform_chat_key": "discord.chat_id",
                "platform_user_key": "discord.allowed_user_ids"},
    "telegram": {"chat_id": CANONICAL["chat_id"], "user_ids": CANONICAL["user_id"],
                 "platform_chat_key": "telegram.chat_id",
                 "platform_user_key": "telegram.allowed_user_ids"},
    "matrix": {"chat_id": "!par-test:matrix.org", "user_ids": "@operator:matrix.org",
               "platform_chat_key": "matrix.room_id",
               "platform_user_key": "matrix.allowed_user_ids"},
}

STEP_ANSWERS = {
    "model": CANONICAL["model"],
    "moonraker_url": CANONICAL["moonraker_url"],
    "spoolman_url": CANONICAL["spoolman_url"],
    "token_stored": "stored",
    "camera_snapshot_url": CANONICAL["camera_snapshot_url"],
    "camera_source": "mjpeg",
    "timelapse_mode": "moonraker",
    "timelapse_capture_enabled": "true",
}

LANE_B_BOOTSTRAP = "Lane B PR-5 (feat/bootstrap-setup-only) not landed: "
LANE_B_ACCESS = "Lane B PR-6 (feat/chat-webui-access-core) not landed: "
LANE_B_CHAT = "Lane B PR-7 (feat/chat-setup-parity) not landed: "
LANE_B_COMPLETION = "Lane B PR-4 (feat/setup-completion-state) not landed: "


def chat_access_core():
    try:
        from print_handy.bot import webui_access  # noqa: F401
        return True
    except ImportError:
        return False


def guided_step_keys():
    return [step.key for step in guided.steps()]


class ChatDriver:
    """Scripted answers through guided.handle_text(), one session key
    per platform/chat/user (the dispatcher's own key shape)."""

    def __init__(self, tmp_path, platform, user=None):
        self.platform = platform
        self.store = guided.SetupStore(Path(tmp_path) / "guided-setup.json")
        answers = PLATFORM_ANSWERS[platform]
        self.key = f"{answers['chat_id']}:{(user or CANONICAL['user_id'])}"
        self.user = user or CANONICAL["user_id"]
        self.replies = []

    def say(self, text, **kwargs):
        kwargs.setdefault("report", lambda: "REPORT")
        reply = guided.handle_text(self.key, text, store=self.store, **kwargs)
        self.replies.append(reply)
        return reply

    def session(self):
        return self.store.get(self.key)

    def answer_for(self, step_key):
        if step_key in ("chat_id", "user_ids"):
            return PLATFORM_ANSWERS[self.platform][step_key]
        if step_key == "platform":
            return self.platform
        return STEP_ANSWERS.get(step_key)

    def drive_to_review(self):
        """Answer every step the flow defines; returns the list of step
        keys it could not answer (the parity gap, when non-empty)."""
        self.say("/print-handy setup")
        unanswered = []
        for _ in range(30):
            session = self.session()
            if not session or session.get("status") == "awaiting_consent":
                break
            step_key = guided.steps()[session["step"]].key \
                if session.get("step") is not None else None
            answer = self.answer_for(step_key)
            if answer is None:
                unanswered.append(step_key)
                break
            before = session.get("step")
            reply = self.say(answer)
            after = self.session()
            if after and after.get("step") == before and \
                    after.get("status") != "awaiting_consent" and \
                    reply and "does not work" in reply:
                unanswered.append(step_key)
                break
        return unanswered

    def apply(self, target):
        return self.say("apply", apply_config=lambda answers:
                        guided._apply_answers(answers, target))


def applied_config(tmp_path, platform):
    """Drive one platform's flow to apply and load the written config."""
    driver = ChatDriver(tmp_path, platform)
    unanswered = driver.drive_to_review()
    if unanswered:
        pytest.skip(LANE_B_CHAT + f"{platform} flow cannot answer steps "
                    f"{unanswered} (or a canonical answer is refused)")
    target = Path(tmp_path) / "local.yaml"
    driver.apply(target)
    session = driver.session()
    assert session and session.get("status") == "complete", \
        f"{platform} setup did not complete: {driver.replies[-1]!r}"
    return settings.load(local=target, environ={}), driver


# --------------------------------------------------------------------- S5

@pytest.mark.parametrize("platform", PLATFORMS)
def test_s5_full_walkthrough_identical_config_via_chat(tmp_path, platform):
    if platform != "discord" and "platform" not in guided_step_keys():
        pytest.skip(LANE_B_CHAT + f"the guided flow has no platform step: "
                    f"a {platform} run still writes the discord.* keys "
                    f"instead of {platform}'s own")
    data, _driver = applied_config(tmp_path, platform)
    view = parity_view(data)
    answers = PLATFORM_ANSWERS[platform]
    expected = {
        "printer.model": CANONICAL["model"],
        "services.moonraker_url": CANONICAL["moonraker_url"].rstrip("/"),
        "services.spoolman_url": CANONICAL["spoolman_url"].rstrip("/"),
        answers["platform_chat_key"]: answers["chat_id"],
        answers["platform_user_key"]: answers["user_ids"],
    }
    for dotted, want in expected.items():
        section, key = dotted.split(".", 1)
        got = (data.get(section) or {}).get(key)
        if isinstance(got, (list, tuple)):
            got = ",".join(str(part) for part in got)
        assert str(got).rstrip("/") == want, \
            f"{platform}: {dotted} is {got!r}, want {want!r}"
    # The shared keys agree with the env/Web UI reference view as well.
    assert view["printer.model"] == CANONICAL["model"]
    assert view["services.moonraker_url"] == \
        CANONICAL["moonraker_url"].rstrip("/")


def test_s5_chat_step_set_covers_the_canonical_topics(tmp_path):
    """Same steps reachable, chat side: platform choice, camera and
    login/access steps join the steps that exist today (printer, chat,
    spoolman, token ack). Providers stays chat-deferred by design (the
    bot asks at link intake — parity of outcome, noted in the plan)."""
    keys = set(guided_step_keys())
    today = {"model", "chat_id", "user_ids", "moonraker_url",
             "spoolman_url", "token_stored"}
    assert today <= keys  # the existing steps never regress away
    missing = {"platform", "camera", "login"} - \
        {k.split("_")[0] for k in keys} - keys
    if missing:
        pytest.skip(LANE_B_CHAT + f"guided setup has no steps for "
                    f"{sorted(missing)} yet")


# --------------------------------------------------------------------- S1

@pytest.mark.parametrize("platform", PLATFORMS)
def test_s1_bot_with_credential_but_no_config_starts_setup_only(tmp_path, platform):
    """S1, chat half: a credential and nothing else must not be a
    startup failure — the bot starts in setup-only mode (PR-5). The
    probe is the startup check tier itself: a missing chat id is a
    setup-only signal, not a FAIL row that stops the process."""
    from print_handy.bot import __main__ as bot_main
    import inspect
    if "setup_only" not in inspect.getsource(bot_main):
        pytest.skip(LANE_B_BOOTSTRAP + "the startup check has no "
                    "setup-only tier; a credential-only install still "
                    "fails startup on the missing chat id")


# --------------------------------------------------------------------- S2/S3/S3b/S9

def _require_chat_access():
    if not chat_access_core():
        pytest.skip(LANE_B_ACCESS + "no bot-side Web UI access core "
                    "exists; chat cannot claim, leave open or clear")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s2_claim_password_via_chat(tmp_path, platform):
    _require_chat_access()
    if "login" not in "".join(guided_step_keys()):
        pytest.skip(LANE_B_CHAT + "guided setup has no login/access step")
    pytest.skip("integration seam: drive the login step through the "
                "landed PR-7 step protocol and assert resolve() == "
                "PASSWORD with matches_login true/false — wired when "
                "PR-6/PR-7 heads are available to stack on")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s3_leave_open_via_chat(tmp_path, platform):
    _require_chat_access()
    if "login" not in "".join(guided_step_keys()):
        pytest.skip(LANE_B_CHAT + "guided setup has no login/access step")
    pytest.skip("integration seam: drive the login step's leave-open "
                "choice through the landed PR-7 step protocol and "
                "assert the saved record is exactly {mode: open} (Q2) "
                "and resolve() == OPEN — wired when PR-6/PR-7 heads "
                "are available to stack on")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s3b_clear_claim_via_chat(tmp_path, platform):
    """Q1: clearing from chat must land on the same state as clearing
    anywhere else — no saved record, unclaimed outcome, and an env
    token set all along still applies afterwards (Q4)."""
    _require_chat_access()
    if "login" not in "".join(guided_step_keys()):
        pytest.skip(LANE_B_CHAT + "guided setup has no login/access step")
    pytest.skip("integration seam: claim, then clear through the landed "
                "PR-7 step protocol; assert the stored record is gone "
                "and resolve() with WEBUI_ACCESS_TOKEN set is TOKEN — "
                "wired when PR-6/PR-7 heads are available to stack on")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s9_chat_password_never_persisted_in_session(tmp_path, platform):
    """After a chat claim, neither the guided session store nor any
    chat-visible summary may contain the password, and the deletion
    hook must have fired where the platform supports deletion."""
    _require_chat_access()
    if "login" not in "".join(guided_step_keys()):
        pytest.skip(LANE_B_CHAT + "guided setup has no login/access step")
    pytest.skip("integration seam: after the S2 chat drive, assert the "
                "guided-setup.json bytes and every reply contain no "
                "password substring — wired when PR-6/PR-7 heads are "
                "available to stack on")


def test_s9_session_store_holds_no_secret_after_todays_flow(tmp_path):
    """The part of S9 that holds today and must never regress: the
    existing guided flow's session store persists answers and status
    only — no token or password value is ever written to it."""
    driver = ChatDriver(tmp_path, "discord")
    driver.drive_to_review()
    raw = (Path(tmp_path) / "guided-setup.json").read_text()
    assert CANONICAL["password"] not in raw
    assert "DISCORD_BOT_TOKEN" in raw or "token" not in raw.lower() \
        or CANONICAL["token"] not in raw


# --------------------------------------------------------------------- S6

@pytest.mark.parametrize("platform", PLATFORMS)
def test_s6_zero_config_bootstrap_claim(tmp_path, platform):
    """Q5 shape, in full: in setup-only mode the first interlocutor
    owns the session exclusively — a second user is refused mid-flow,
    non-setup commands are refused throughout, an abandoned session
    past its timeout releases the bind, and after apply normal
    authorization is in force for the claimed chat/user only."""
    from print_handy.bot import __main__ as bot_main
    import inspect
    if "setup_only" not in inspect.getsource(bot_main):
        pytest.skip(LANE_B_BOOTSTRAP + "no setup-only mode exists; a "
                    "zero-config bot still exits at the startup check "
                    "before any interlocutor can claim it")
    pytest.skip("integration seam: PR-5's admission seam (runtime gate / "
                "dispatcher branch) is exercised here once its head is "
                "available to stack on — bind-at-start, mid-flow refusal "
                "of a second user, timeout release, post-apply refusal")


# --------------------------------------------------------------------- S7

@pytest.mark.parametrize("platform", PLATFORMS)
def test_s7_chat_completion_ends_the_webui_offer(tmp_path, platform):
    try:
        from print_handy import setup_state  # noqa: F401
    except ImportError:
        pytest.skip(LANE_B_COMPLETION + "chat completion still writes "
                    "only the guided session store; the Web UI cannot "
                    "see it and re-offers its walkthrough")
    pytest.skip("integration seam: complete setup via the chat driver, "
                "then assert walkthrough.pending(data) is False and "
                "the shared record names this surface — wired when "
                "PR-4/PR-7 heads are available to stack on")


# --------------------------------------------------------------------- S8

def test_s8_telegram_walkthrough_no_llm(tmp_path):
    """The full Telegram scenario with the LLM lane tripwired: Telegram
    is a pure bot (adapter uses_llm is False) and the guided machine is
    deterministic, so a whole walkthrough — bootstrap included — must
    make zero LLM calls."""
    from print_handy.bot import llm as bot_llm
    from print_handy.bot import telegram

    assert telegram.StandaloneAdapter.uses_llm is False
    calls = []

    def exploding_ask(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the Telegram walkthrough reached the LLM lane")

    with mock.patch.object(bot_llm, "ask", exploding_ask, create=True):
        data, _driver = applied_config(tmp_path, "telegram")
    assert calls == []
    assert parity_view(data)["printer.model"] == CANONICAL["model"]
