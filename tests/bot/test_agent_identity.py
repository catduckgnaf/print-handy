"""The stray-chat agent lane's identity layer (#902, the Discord Assist
model): a mapped user runs as their own identity, an allowlisted user as
the least-privilege fallback identity, and everyone else gets no agent
call at all — fail closed, per-identity keys, per-user conversations
with an idle reset, a per-user rate limit, replies that can never ping,
and bot check / doctor rows that say who may use the lane and what a
Hermes target profile can do.

Every endpoint is a fake opener in the FakeLLM pattern; profile configs
are files under tmp_path. No network, no live Hermes.
"""
import asyncio
import io
import json
import logging
import urllib.error
from types import SimpleNamespace as S

import pytest

from corpus import fake_discord
from print_handy import doctor, settings
from print_handy.bot import __main__ as bot_main
from print_handy.bot import agent, agent_identity, runtime

OPERATOR_URL = "http://agent.test/p/operator/v1/chat/completions"
GUEST_URL = "http://agent.test/p/guest/v1/chat/completions"
LLM_URL = "http://llm.test/v1/chat/completions"
OPERATOR_ID = "200000000000000002"   # the cfg fixture's allowed operator
STRANGER_ID = "200000000000000099"
GUEST_ID = "200000000000000055"


class FakeEndpoint:
    """An opener double keyed by URL: records (url, headers, body) and
    answers with canned content per URL."""

    def __init__(self, contents):
        self.contents = contents  # url -> reply text
        self.requests = []

    def __call__(self, req, timeout=None):
        headers = {key.lower(): value for key, value in req.header_items()}
        self.requests.append({"url": req.full_url, "headers": headers,
                              "body": json.loads(req.data) if req.data else None,
                              "timeout": timeout})
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": self.contents[req.full_url]}}]}).encode())


def layer_cfg(cfg, **overrides):
    """The bot-test cfg with the identity layer switched on: the operator
    is mapped to a powerful identity, GUEST_ID is allowlisted onto a weak
    fallback, STRANGER_ID is nobody."""
    cfg["discord"]["allowed_user_ids"] = ""  # channel (everyone): the identity layer is the gate
    cfg["agent"].update(
        identities={
            "operator": {"chat_url": OPERATOR_URL, "api_key_env": "OPERATOR_AGENT_KEY"},
            "guest": {"chat_url": GUEST_URL, "api_key_env": "GUEST_AGENT_KEY"},
        },
        users={OPERATOR_ID: "operator"},
        allowlist=[GUEST_ID],
        fallback_identity="guest")
    cfg["agent"].update(overrides)
    cfg["llm"].update(chat_url=LLM_URL, model="small-model")
    return cfg


def make(cfg, agent_opener=None, llm_opener=None):
    discord = fake_discord.FakeDiscord(auto=False, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(OPERATOR_ID))
    core = runtime.BotCore(discord.client, cfg, llm_opener=llm_opener, agent_opener=agent_opener)
    return discord, core


def user(user_id):
    return fake_discord.FakeUser(int(user_id), f"user-{user_id}")


class FakeLLM:
    def __init__(self, reply="llm words"):
        self.content = json.dumps({"reply": reply, "command": None})
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.content}}]}).encode())


# ---------------------------------------------------------------------- settings


def _load_agent(tmp_path, agent_yaml):
    local = tmp_path / "local.yaml"
    local.write_text(agent_yaml)
    return settings.load(local=local, environ={})


def test_identity_settings_validate_and_normalize(tmp_path):
    data = _load_agent(tmp_path, """
agent:
  identities:
    operator: {chat_url: "http://h:8642/p/operator/v1/chat/completions", api_key_env: OP_KEY}
    guest: {chat_url: "http://h:8642/p/guest/v1/chat/completions", api_key_env: GUEST_KEY}
  users: {"123": operator, "telegram:456": operator}
  allowlist: [789]
  fallback_identity: guest
""")
    section = data["agent"]
    assert section["users"] == {"123": "operator", "telegram:456": "operator"}
    assert section["allowlist"] == ["789"]
    assert section["identities"]["operator"]["timeout_s"] == 120


@pytest.mark.parametrize("yaml_text,match", [
    ('agent:\n  users: {"1": ghost}\n', "not defined"),
    ('agent:\n  fallback_identity: ghost\n', "not defined"),
    ('agent:\n  identities: {op: {api_key_env: K}}\n', "chat_url must be a URL"),
    ('agent:\n  identities: {op: {chat_url: "http://h/v1", bogus: 1}}\n', "unknown setting"),
    ('agent:\n  identities: {op: {chat_url: "http://h/v1", api_key_env: "not a name"}}\n',
     "environment variable name"),
    ('agent:\n  allowlist: [{}]\n', "must be a list"),
])
def test_identity_settings_refuse_bad_tables(tmp_path, yaml_text, match):
    with pytest.raises(settings.SettingsError, match=match):
        _load_agent(tmp_path, yaml_text)


# ---------------------------------------------------------------------- resolution


def test_resolve_maps_allowlist_and_fail_closed(cfg):
    cfg = layer_cfg(cfg)
    assert agent_identity.resolve(cfg, "discord", OPERATOR_ID).name == "operator"
    assert agent_identity.resolve(cfg, "discord", f"{OPERATOR_ID}").chat_url == OPERATOR_URL
    assert agent_identity.resolve(cfg, "discord", GUEST_ID).name == "guest"
    assert agent_identity.resolve(cfg, "discord", STRANGER_ID) is None
    assert agent_identity.resolve(cfg, "telegram", OPERATOR_ID).name == "operator"  # bare id, any platform
    assert agent_identity.resolve(cfg, "discord", "") is None


def test_resolve_platform_qualified_user_keys(cfg):
    cfg = layer_cfg(cfg)
    cfg["agent"]["users"] = {"discord:42": "operator", "telegram:42": "guest"}
    assert agent_identity.resolve(cfg, "discord", "42").name == "operator"
    assert agent_identity.resolve(cfg, "telegram", "42").name == "guest"
    assert agent_identity.resolve(cfg, "matrix", "42") is None


def test_resolve_legacy_and_unconfigured(cfg):
    assert agent_identity.resolve(cfg, "discord", STRANGER_ID) is None  # nothing configured
    cfg["agent"].update(chat_url=OPERATOR_URL)
    legacy = agent_identity.resolve(cfg, "discord", STRANGER_ID)
    assert legacy.name == "legacy" and legacy.chat_url == OPERATOR_URL  # today's behavior


def test_allowlist_without_a_fallback_fails_closed(cfg):
    cfg = layer_cfg(cfg)
    cfg["agent"]["fallback_identity"] = ""
    cfg["agent"]["identities"].pop("guest")
    assert agent_identity.resolve(cfg, "discord", GUEST_ID) is None
    assert agent_identity.nobody_authorized(cfg) is False  # the mapped operator still resolves
    cfg["agent"]["users"] = {}
    assert agent_identity.nobody_authorized(cfg) is True


# ---------------------------------------------------------------------- the lane in BotCore


def test_unmapped_user_gets_no_agent_call_and_the_llm_answers(cfg):
    cfg = layer_cfg(cfg)
    endpoint = FakeEndpoint({OPERATOR_URL: "operator words", GUEST_URL: "guest words"})
    llm = FakeLLM()
    discord, core = make(cfg, agent_opener=endpoint, llm_opener=llm)
    out = asyncio.run(core.handle_message(
        discord.message("hello?", author=user(STRANGER_ID))))
    assert endpoint.requests == []  # the HTTP client was never invoked for the agent
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == ["llm words"]


def test_mapped_user_runs_as_their_identity_with_its_own_key(cfg, monkeypatch):
    cfg = layer_cfg(cfg)
    monkeypatch.setenv("OPERATOR_AGENT_KEY", "operator-secret")
    monkeypatch.setenv("GUEST_AGENT_KEY", "guest-secret")
    endpoint = FakeEndpoint({OPERATOR_URL: "operator words", GUEST_URL: "guest words"})
    discord, core = make(cfg, agent_opener=endpoint)
    out = asyncio.run(core.handle_message(discord.message("status?")))
    assert out == {"action": "skip", "reason": "agent reply"}
    assert discord.sent == ["operator words"]
    (request,) = endpoint.requests
    assert request["url"] == OPERATOR_URL
    assert request["headers"]["authorization"] == "Bearer operator-secret"


def test_allowlisted_user_runs_as_the_fallback_identity(cfg, monkeypatch):
    cfg = layer_cfg(cfg)
    monkeypatch.setenv("OPERATOR_AGENT_KEY", "operator-secret")
    monkeypatch.setenv("GUEST_AGENT_KEY", "guest-secret")
    endpoint = FakeEndpoint({OPERATOR_URL: "operator words", GUEST_URL: "guest words"})
    discord, core = make(cfg, agent_opener=endpoint)
    asyncio.run(core.handle_message(discord.message("hi", author=user(GUEST_ID))))
    (request,) = endpoint.requests
    assert request["url"] == GUEST_URL
    assert request["headers"]["authorization"] == "Bearer guest-secret"
    assert discord.sent == ["guest words"]


def test_keys_are_never_logged(cfg, monkeypatch, caplog):
    cfg = layer_cfg(cfg)
    monkeypatch.setenv("OPERATOR_AGENT_KEY", "operator-secret-value")
    endpoint = FakeEndpoint({OPERATOR_URL: "fine"})
    discord, core = make(cfg, agent_opener=endpoint)
    with caplog.at_level(logging.DEBUG):
        asyncio.run(core.handle_message(discord.message("status?")))
    assert "operator-secret-value" not in caplog.text
    assert "identity operator served" in caplog.text  # who served, never what was said
    assert "status?" not in caplog.text


def test_agent_failure_posts_one_line_then_the_llm_tier_answers(cfg):
    cfg = layer_cfg(cfg)

    def broken(req, timeout=None):
        raise OSError("endpoint down")

    llm = FakeLLM()
    discord, core = make(cfg, agent_opener=broken, llm_opener=llm)
    out = asyncio.run(core.handle_message(discord.message("status?")))
    assert out == {"action": "skip", "reason": "llm reply"}
    assert discord.sent == [core.agent_unavailable, "llm words"]


# ---------------------------------------------------------------------- reply safety


def test_sanitize_reply_neutralizes_every_ping():
    text = "hey @everyone and @here, <@123> and <@&456> and <@!789>"
    out = agent_identity.sanitize_reply(text)
    assert "@everyone" not in out and "@here" not in out
    assert "<@123>" not in out and "<@&456>" not in out and "<@!789>" not in out
    assert "everyone" in out and "123" in out  # the words survive; the pings do not


def test_agent_reply_with_mentions_is_posted_sanitized(cfg):
    cfg = layer_cfg(cfg)
    endpoint = FakeEndpoint({OPERATOR_URL: "done @everyone <@123>"})
    discord, core = make(cfg, agent_opener=endpoint)
    asyncio.run(core.handle_message(discord.message("status?")))
    (posted,) = discord.sent
    assert "@everyone" not in posted and "<@123>" not in posted


# ---------------------------------------------------------------------- conversation + rate limit


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_conversation_id_is_stable_until_the_idle_timeout():
    clock = Clock()
    book = agent_identity.ConversationBook(idle_timeout_s=1800, clock=clock)
    first = book.conversation_id("k")
    clock.now += 1799
    assert book.conversation_id("k") == first  # still inside the idle window
    clock.now += 1801  # now idle longer than the timeout since the last message
    assert book.conversation_id("k") != first


def test_conversation_ids_differ_per_user_and_channel():
    book = agent_identity.ConversationBook()
    assert book.conversation_id(("legacy", "discord", "1", "7")) != \
        book.conversation_id(("legacy", "discord", "1", "8"))
    assert book.conversation_id(("legacy", "discord", "1", "7")) != \
        book.conversation_id(("legacy", "discord", "2", "7"))


def test_rate_limiter_allows_the_limit_then_one_notice_then_silence():
    clock = Clock()
    limiter = agent_identity.RateLimiter(limit=3, window_s=60, clock=clock)
    assert [limiter.check("u") for _ in range(3)] == ["ok"] * 3
    assert limiter.check("u") == "notice"
    assert limiter.check("u") == "drop"
    clock.now += 61
    assert limiter.check("u") == "ok"


def test_rate_limit_in_the_lane_posts_one_notice(cfg):
    cfg = layer_cfg(cfg, rate_limit_per_minute=2)
    endpoint = FakeEndpoint({OPERATOR_URL: "words"})
    discord, core = make(cfg, agent_opener=endpoint)
    for _ in range(2):
        asyncio.run(core.handle_message(discord.message("status?")))
    out = asyncio.run(core.handle_message(discord.message("status?")))
    assert out == {"action": "skip", "reason": "agent rate limited"}
    asyncio.run(core.handle_message(discord.message("status?")))
    notices = [text for text in discord.sent if "faster than it can answer" in text]
    assert len(notices) == 1
    assert len(endpoint.requests) == 2  # the excess never reached the endpoint


# ---------------------------------------------------------------------- probe + capability


class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_verdicts_from_recorded_responses():
    identity = agent_identity.Identity("op", OPERATOR_URL, api_key_env="K")
    ok = agent_identity.probe(identity, opener=lambda req, timeout=None: _Resp(200), environ={"K": "x"})
    assert ok[0] == "ok"

    def unauthorized(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

    verdict = agent_identity.probe(identity, opener=unauthorized, environ={"K": "x"})
    assert verdict[0] == "unauthorized" and "401" in verdict[1]

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")

    verdict = agent_identity.probe(identity, opener=down, environ={"K": "x"})
    assert verdict[0] == "unreachable"
    assert agent_identity.models_url(identity) == "http://agent.test/p/operator/v1/models"


def test_probe_never_puts_the_key_in_the_detail():
    identity = agent_identity.Identity("op", OPERATOR_URL, api_key_env="K")
    _verdict, detail = agent_identity.probe(
        identity, opener=lambda req, timeout=None: (_ for _ in ()).throw(OSError("nope")),
        environ={"K": "the-secret-key"})
    assert "the-secret-key" not in detail


def _profile_cfg(tmp_path, profile, toolsets_yaml):
    home = tmp_path / "profiles" / "hosting"
    target = tmp_path / "profiles" / profile
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.yaml").write_text(
        f"platform_toolsets:\n  api_server: {toolsets_yaml}\n")
    return {"paths": {"hermes_home": str(home)}}


def test_capability_report_reads_the_profile_allowlist(tmp_path):
    identity = agent_identity.Identity("guest", GUEST_URL)
    level, detail, dangerous = agent_identity.capability_report(
        identity, _profile_cfg(tmp_path, "guest", "[web]"))
    assert level == "ok" and dangerous == []
    level, detail, dangerous = agent_identity.capability_report(
        identity, _profile_cfg(tmp_path, "guest", "[web, terminal, cron]"))
    assert level == "warn" and dangerous == ["cron", "terminal"]
    assert "terminal" in detail


def test_capability_report_warns_when_no_allowlist_exists(tmp_path):
    identity = agent_identity.Identity("guest", GUEST_URL)
    cfg = _profile_cfg(tmp_path, "guest", "[web]")
    (tmp_path / "profiles" / "guest" / "config.yaml").write_text("platforms: {}\n")
    level, _detail, dangerous = agent_identity.capability_report(identity, cfg)
    assert level == "warn" and "terminal" in dangerous  # Hermes defaults to everything


def test_capability_report_unknown_for_plain_endpoints_and_missing_configs(tmp_path):
    plain = agent_identity.Identity("plain", "http://agent.test/v1/chat/completions")
    assert agent_identity.capability_report(plain, {})[0] == "unknown"
    hermes = agent_identity.Identity("guest", GUEST_URL)
    level, detail, _ = agent_identity.capability_report(
        hermes, {"paths": {"hermes_home": str(tmp_path / "nowhere")}})
    assert level == "unknown" and "not readable" in detail


# ---------------------------------------------------------------------- bot check + doctor rows


def _ok_probe(identity):
    return "ok", f"{identity.host}: reachable, key accepted"


def test_bot_check_rows_for_the_identity_layer(cfg, tmp_path):
    cfg = layer_cfg(cfg)
    cfg["paths"]["hermes_home"] = str(tmp_path / "profiles" / "hosting")
    rows = bot_main.agent_identity_rows(cfg, probe_fn=_ok_probe)
    by_label = {label: (ok, detail) for label, ok, detail in rows}
    assert "2 mapped" not in by_label["agent identities"][1]  # counts are users, not endpoints
    assert "1 mapped user(s), 1 allowlisted" in by_label["agent identities"][1]
    assert "fallback identity: guest" in by_label["agent identities"][1]
    assert by_label["agent identity operator"] == (True, "agent.test: reachable, key accepted")
    assert "agent authorization" not in by_label  # somebody IS authorized


def test_bot_check_rows_warn_when_nobody_is_authorized(cfg):
    cfg = layer_cfg(cfg)
    cfg["agent"]["users"] = {}
    cfg["agent"]["allowlist"] = []
    rows = bot_main.agent_identity_rows(cfg, probe_fn=_ok_probe)
    by_label = {label: (ok, detail) for label, ok, detail in rows}
    assert by_label["agent authorization"][0] is False
    assert "nobody is authorized" in by_label["agent authorization"][1]


def test_bot_check_rows_capability_warning_for_the_fallback_profile(cfg, tmp_path):
    cfg = layer_cfg(cfg)
    cfg["paths"]["hermes_home"] = str(tmp_path / "profiles" / "hosting")
    guest_dir = tmp_path / "profiles" / "guest"
    guest_dir.mkdir(parents=True)
    (guest_dir / "config.yaml").write_text("platform_toolsets:\n  api_server: [web, terminal]\n")
    rows = bot_main.agent_identity_rows(cfg, probe_fn=_ok_probe)
    by_label = {label: (ok, detail) for label, ok, detail in rows}
    ok, detail = by_label["agent capability guest"]
    assert ok is False and detail.startswith("WARNING") and "terminal" in detail


def test_bot_check_rows_legacy_config_gets_the_deprecation_row(cfg):
    cfg["agent"].update(chat_url=OPERATOR_URL)
    rows = bot_main.agent_identity_rows(cfg)
    assert len(rows) == 1
    assert "legacy single endpoint" in rows[0][2] and "fallback identity" in rows[0][2]


def test_doctor_and_status_summary_name_the_nobody_authorized_warning(cfg):
    cfg = layer_cfg(cfg)
    cfg["agent"]["users"] = {}
    cfg["agent"]["allowlist"] = []
    detail, warning = agent_identity.status_summary(cfg)
    assert warning and "nobody is authorized" in warning
    assert "WARNING" in doctor.check_agent_lane(cfg)


def test_status_summary_unconfigured(cfg):
    detail, warning = agent_identity.status_summary(cfg)
    assert detail.startswith("not configured") and warning is None


# ---------------------------------------------------------------------- DM + mention doors


def test_dm_messages_get_no_agent_call_unless_dm_enabled(cfg):
    cfg = layer_cfg(cfg)
    endpoint = FakeEndpoint({OPERATOR_URL: "operator words"})
    llm = FakeLLM()
    discord, core = make(cfg, agent_opener=endpoint, llm_opener=llm)
    source = runtime.make_source(OPERATOR_ID, "4242", platform_name="discord", is_dm=True)
    event = runtime.make_event(source, "status?", "m-dm")
    out = asyncio.run(core.fallback(event))
    assert endpoint.requests == []  # DMs are off by default
    assert out == {"action": "skip", "reason": "llm reply"}
    cfg["agent"]["dm_enabled"] = True
    out = asyncio.run(core.fallback(event))
    assert len(endpoint.requests) == 1
    assert out == {"action": "skip", "reason": "agent reply"}


def test_lane_door_opens_for_a_mention_only_when_enabled(cfg):
    cfg = layer_cfg(cfg)
    endpoint = FakeEndpoint({OPERATOR_URL: "operator words"})
    discord, core = make(cfg, agent_opener=endpoint)
    foreign = discord.add_channel(777000111)
    message = discord.message(f"<@{discord.bot_user.id}> status?", channel=foreign)
    message.mentions = [discord.bot_user]
    out = asyncio.run(core.handle_message(message))
    assert out is None and endpoint.requests == []  # mentions are off by default
    cfg["agent"]["mention_enabled"] = True
    out = asyncio.run(core.handle_message(message))
    assert out == {"action": "skip", "reason": "agent reply"}
    assert len(endpoint.requests) == 1


def test_lane_door_never_runs_llm_named_commands(cfg):
    cfg = layer_cfg(cfg)
    cfg["agent"]["mention_enabled"] = True

    class CommandLLM:
        def __call__(self, req, timeout=None):
            body = json.dumps({"reply": "", "command": "/print-status"})
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": body}}]}).encode())

    endpoint = FakeEndpoint({})  # the stranger has no identity; the LLM tier answers
    discord, core = make(cfg, agent_opener=endpoint, llm_opener=CommandLLM())
    foreign = discord.add_channel(777000222)
    message = discord.message(f"<@{discord.bot_user.id}> do a thing",
                              author=user(STRANGER_ID), channel=foreign)
    message.mentions = [discord.bot_user]
    out = asyncio.run(core.handle_message(message))
    # The LLM's named command is NOT re-dispatched from a lane-door event:
    # the only output is the lane's own terminal reply.
    assert out == {"action": "skip", "reason": "lane only"}
