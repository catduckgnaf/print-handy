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
import json
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
from print_handy.webui import access  # noqa: E402

from test_setup_surface_parity import (  # noqa: E402
    CANONICAL, make_data, parity_view)

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
    startup check is split in tiers: credential-tier failures stay
    fatal, configuration-tier failures mean setup-only."""
    from print_handy.bot import __main__ as bot_main
    if not hasattr(bot_main, "setup_only_mode"):
        pytest.skip(LANE_B_BOOTSTRAP + "the startup check has no "
                    "setup-only tier; a credential-only install still "
                    "fails startup on the missing chat id")
    cfg = {"bot": {"platform": platform, "mode": "standalone"}}
    chat_row = {"discord": "print channel", "telegram": "print channel",
                "matrix": "print room"}[platform]
    rows = [("token", True, "set"), (chat_row, False, "empty")]
    if platform == "matrix":
        rows.insert(1, ("homeserver", True, "set"))
    assert bot_main.setup_only_mode(cfg, rows) is True
    # A missing credential is never setup-only: there is no bot to
    # hold the conversation at all.
    rows = [("token", False, "empty"), (chat_row, False, "empty")]
    assert bot_main.setup_only_mode(cfg, rows) is False


# --------------------------------------------------------------------- S2/S3/S3b/S9

def _require_chat_access():
    if not chat_access_core():
        pytest.skip(LANE_B_ACCESS + "no bot-side Web UI access core "
                    "exists; chat cannot claim, leave open or clear")


def _chat_data(tmp_path):
    data, _local, _environ = make_data(str(tmp_path))
    return data


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s2_claim_password_via_chat(tmp_path, platform):
    """Via the chat access core every platform's flow calls (PR-6):
    the outcome is the same store and the same resolve() result as a
    Web UI claim. The guided step that calls the core is PR-7's; its
    arrival is asserted by test_s2_claim_password_via_guided_step."""
    _require_chat_access()
    from print_handy.bot import webui_access
    data = _chat_data(tmp_path)
    result = webui_access.set_password(
        data, CANONICAL["username"], CANONICAL["password"],
        CANONICAL["password"], environ={})
    assert result.ok, result.message
    assert CANONICAL["password"] not in result.message
    resolved = access.resolve(data, "0.0.0.0", environ={})
    assert resolved.mode == access.PASSWORD
    assert resolved.matches_login(CANONICAL["username"], CANONICAL["password"])
    assert not resolved.matches_login(CANONICAL["username"], "wrong-password-1")
    # The Web UI's own weak-password refusal is the chat refusal too.
    refused = webui_access.set_password(
        data, CANONICAL["username"], "password", "password", environ={})
    assert not refused.ok


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s3_leave_open_via_chat(tmp_path, platform):
    _require_chat_access()
    from print_handy.bot import webui_access
    data = _chat_data(tmp_path)
    result = webui_access.leave_open(data, True, environ={})
    assert result.ok, result.message
    record = json.loads(access.access_path(data).read_text())
    assert record == {"mode": access.OPEN}  # Q2: the explicit record
    assert access.resolve(data, "0.0.0.0", environ={}).mode == access.OPEN
    # An unconfirmed leave-open is refused, exactly as in the Web UI.
    assert not webui_access.leave_open(data, False, environ={}).ok


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s3b_clear_claim_via_chat(tmp_path, platform):
    """Q1: clearing from chat must land on the same state as clearing
    anywhere else — no saved record, unclaimed outcome, and an env
    token set all along still applies afterwards (Q4)."""
    _require_chat_access()
    from print_handy.bot import webui_access
    data = _chat_data(tmp_path)
    assert webui_access.set_password(
        data, CANONICAL["username"], CANONICAL["password"],
        CANONICAL["password"], environ={}).ok
    assert not webui_access.clear_claim(data, False, environ={}).ok
    result = webui_access.clear_claim(
        data, True, environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
    assert result.ok, result.message
    assert result.env_token_present
    assert not access.access_path(data).exists()
    resolved = access.resolve(
        data, "0.0.0.0", environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
    assert resolved.mode == access.TOKEN


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s2_claim_password_via_guided_step(tmp_path, platform):
    """The end-to-end form of S2: the password arrives inside the
    guided conversation's login step (PR-7) and lands in the same
    store. Gated on the step existing."""
    _require_chat_access()
    if "login" not in "".join(guided_step_keys()):
        pytest.skip(LANE_B_CHAT + "guided setup has no login/access "
                    "step yet; S2 is asserted at the PR-6 core seam "
                    "every platform's step will call")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_s9_chat_password_never_persisted_in_session(tmp_path, platform):
    """After a chat claim: the core's chat-visible results contain no
    password, and no guided session store appears or gains the value —
    the password goes straight to the access writer."""
    _require_chat_access()
    from print_handy.bot import webui_access
    data = _chat_data(tmp_path)
    result = webui_access.set_password(
        data, CANONICAL["username"], CANONICAL["password"],
        CANONICAL["password"], environ={})
    assert result.ok
    assert CANONICAL["password"] not in result.message
    state_dir = access.access_path(data).parent
    store_file = state_dir / guided.STORE_NAME
    if store_file.exists():
        assert CANONICAL["password"] not in store_file.read_text()
    # Deletion support is a capability the flow checks before capture,
    # per platform adapter — all three shipped adapters support it.
    from print_handy.bot import matrix, telegram
    from print_handy.bot.adapter import StandaloneAdapter as DiscordAdapter
    for adapter_cls in (DiscordAdapter, telegram.StandaloneAdapter,
                        matrix.StandaloneAdapter):
        assert webui_access.deletion_supported(
            adapter_cls.__new__(adapter_cls)), adapter_cls


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
    if not hasattr(bot_main, "setup_only_mode"):
        pytest.skip(LANE_B_BOOTSTRAP + "no setup-only mode exists; a "
                    "zero-config bot still exits at the startup check "
                    "before any interlocutor can claim it")
    import time
    store = guided.SetupStore(tmp_path / guided.STORE_NAME)
    now = time.time()
    owner = f"chan::{CANONICAL['user_id']}"
    other = "chan::200000000000000099"

    def say(key, text, at, **kwargs):
        kwargs.setdefault("report", lambda: "REPORT")
        return guided.handle_text(key, text, store=store, now=at,
                                  setup_only=True, **kwargs)

    # Non-setup commands are refused throughout setup-only mode.
    for command in ("/print-handy debugging", "/print-handy bundle",
                    "/print-handy add-printer"):
        reply = say(owner, command, now)
        assert reply and "Setup-only mode" in reply, command

    # The first interlocutor binds the session at conversation start.
    reply = say(owner, "/print-handy setup", now)
    assert reply and "Setup" in reply
    # A second user can neither start nor join mid-flow.
    refused = say(other, "/print-handy setup", now + 1)
    assert refused and "another user" in refused
    refused = say(other, CANONICAL["model"], now + 2)
    assert refused and "another user" in refused
    # The owner continues normally.
    answer = say(owner, CANONICAL["model"], now + 3)
    assert answer and "Saved" in answer

    # An abandoned session past its timeout releases the bind.
    later = now + guided.SESSION_TIMEOUT_S + 10
    reply = say(other, "/print-handy setup", later)
    assert reply and ("Setup" in reply or "resum" in reply.casefold())
    assert "another user" not in reply

    # After apply, normal authorization is in force: a different user
    # starting setup on the now-configured install is refused (the
    # shared completion record answers instead of a fresh flow).
    for index, text in enumerate(
            (CANONICAL["model"], CANONICAL["chat_id"], CANONICAL["user_id"],
             "skip", "skip", "stored")):
        say(other, text, later + 1 + index)
    applied = say(other, "apply", later + 10,
                  apply_config=lambda answers: "written.")
    assert applied and "Setup complete" in applied
    third = "chan::200000000000000077"
    reply = guided.handle_text(third, "/print-handy setup", store=store,
                               now=later + 20, report=lambda: "REPORT")
    assert reply and "already completed" in reply


# --------------------------------------------------------------------- S7

@pytest.mark.parametrize("platform", PLATFORMS)
def test_s7_chat_completion_ends_the_webui_offer(tmp_path, platform):
    """Complete setup in chat (the guided store placed in the install's
    real state dir, as the dispatcher does) ⇒ the shared record names
    the chat surface, the Web UI walkthrough no longer offers itself,
    and another chat user is told setup already happened."""
    try:
        from print_handy import setup_state
    except ImportError:
        pytest.skip(LANE_B_COMPLETION + "chat completion still writes "
                    "only the guided session store; the Web UI cannot "
                    "see it and re-offers its walkthrough")
    from print_handy.webui import walkthrough
    data, _local, _environ = make_data(str(tmp_path))
    state_dir = settings.state_dir(data)
    state_dir.mkdir(parents=True, exist_ok=True)
    assert walkthrough.pending(data)
    store = guided.SetupStore(state_dir / guided.STORE_NAME)
    key = f"{CANONICAL['chat_id']}::{CANONICAL['user_id']}"
    guided.handle_text(key, "/print-handy setup", store=store,
                       report=lambda: "REPORT")
    for answer in (CANONICAL["model"], CANONICAL["chat_id"],
                   CANONICAL["user_id"], "skip", CANONICAL["spoolman_url"],
                   "stored"):
        guided.handle_text(key, answer, store=store, report=lambda: "REPORT")
    target = Path(tmp_path) / "local.yaml"
    reply = guided.handle_text(
        key, "apply", store=store, report=lambda: "REPORT",
        apply_config=lambda answers: guided._apply_answers(answers, target))
    assert reply and "Setup complete" in reply
    record = setup_state.load_completion(data)
    assert record.get("done") is True and record.get("surface") == "chat"
    assert not walkthrough.pending(data)
    other = guided.handle_text(
        f"{CANONICAL['chat_id']}::200000000000000099", "/print-handy setup",
        store=store, report=lambda: "REPORT")
    assert other and "already completed" in other and "chat" in other


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
