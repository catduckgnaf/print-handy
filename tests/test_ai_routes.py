"""The AI tiers after #1731: model names only, over the ONE [llm] endpoint.

Every tier resolves to the same URL; a tier table holds only ``model``.
There is no fallback leg: a failed call fails. No test here touches the
network: every call goes through a fake opener or a patched urlopen.
"""
import io
import json

import pytest

from print_handy import ai_routes, settings
from print_handy.bot import llm
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import visual_watch as vw


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")

URL = "http://models.test/v1/chat/completions"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
OTHER_URL = "http://backup.test/v1/chat/completions"


class Endpoint:
    """A fake chat endpoint: models listed in ``fail`` raise, every other model answers."""

    def __init__(self, answer="hello", fail=()):
        self.answer = answer
        self.fail = set(fail)
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.requests.append({"url": req.full_url, "model": body["model"], "body": body,
                              "auth": req.get_header("Authorization"), "timeout": timeout})
        if body["model"] in self.fail:
            raise OSError("timed out")
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": self.answer}}]}).encode())

    @property
    def models(self):
        return [request["model"] for request in self.requests]


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    for name in ("LLM_API_KEY",):
        monkeypatch.delenv(name, raising=False)


def cfg(**sections):
    base = {"ai": {"small": {}, "chat": {}, "heavy": {}},
            "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 20},
            "design": {}, "vision": {"roast_model": "roast-model"}}
    for name, values in sections.items():
        base[name] = values
    return base


def one_endpoint(**sections):
    sections.setdefault("llm", {"chat_url": URL, "model": "chat-model",
                                "api_key_env": "", "timeout_s": 7})
    return cfg(**sections)


MSG = [{"role": "user", "content": "hi"}]


def test_an_install_that_configures_nothing_has_no_route_and_sends_nothing():
    endpoint = Endpoint()
    for tier in ai_routes.TIERS:
        assert not ai_routes.configured(tier, cfg())
        with pytest.raises(ai_routes.AINotConfigured):
            ai_routes.call(tier, MSG, cfg=cfg(), opener=endpoint)
    assert endpoint.requests == []


def test_exactly_one_endpoint_resolves_for_every_tier():
    """The #1731 end state: whatever the tier, the URL is llm.chat_url."""
    config = one_endpoint()
    for tier in ai_routes.TIERS:
        assert ai_routes.endpoint_url(tier, config) == URL
        assert ai_routes.configured(tier, config)


def test_the_one_endpoint_serves_every_tier_with_its_model_name(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")  # the small tier insists on a key for a hosted endpoint
    config = one_endpoint(
        llm={"chat_url": URL, "model": "chat-model", "api_key_env": "LLM_API_KEY", "timeout_s": 7},
        design={"model": "design-model"})
    endpoint = Endpoint()
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert (answer, served) == ("hello", "chat-model")
    assert endpoint.requests[0]["url"] == URL and endpoint.requests[0]["timeout"] == 7
    # heavy: design.model is the model-name override; the URL is the same.
    ai_routes.call("heavy", MSG, cfg=config, opener=endpoint)
    assert (endpoint.requests[1]["url"], endpoint.models[1]) == (URL, "design-model")
    # small: the same endpoint and the llm model by default.
    ai_routes.call("small", MSG, cfg=config, opener=endpoint)
    assert (endpoint.requests[2]["url"], endpoint.models[2]) == (URL, "chat-model")
    assert "roast-model" not in endpoint.models


def test_a_tier_model_name_wins_and_changes_nothing_else():
    config = one_endpoint(ai={"small": {}, "heavy": {}, "chat": {"model": "better-model"}})
    endpoint = Endpoint()
    _answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "better-model" and endpoint.requests[0]["url"] == URL
    assert ai_routes.model_for("chat", config) == "better-model"
    assert ai_routes.model_for("small", config) == "chat-model"


def test_a_failed_call_is_not_retried_and_the_error_surfaces():
    config = one_endpoint()
    endpoint = Endpoint(fail={"chat-model"})
    with pytest.raises(OSError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["chat-model"]  # exactly one attempt, no fallback


def test_an_empty_answer_counts_as_a_failure():
    config = one_endpoint()
    endpoint = Endpoint(answer="")
    with pytest.raises(ValueError, match="empty answer"):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["chat-model"]


def test_the_small_tier_refuses_a_hosted_endpoint_without_a_key_and_sends_nothing():
    config = cfg(llm={"chat_url": "https://api.example.com/v1/chat", "model": "m",
                      "api_key_env": "LLM_API_KEY", "timeout_s": 20})
    endpoint = Endpoint()
    assert not ai_routes.ready("small", config)
    with pytest.raises(ai_routes.AIKeyMissing):
        ai_routes.call("small", MSG, cfg=config, opener=endpoint)
    assert endpoint.requests == []


def test_the_chat_tier_still_sends_without_a_key_to_a_hosted_endpoint():
    config = cfg(llm={"chat_url": "https://api.example.com/v1/chat", "model": "m",
                      "api_key_env": "LLM_API_KEY", "timeout_s": 20})
    endpoint = Endpoint()
    ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.requests[0]["auth"] is None


def test_the_key_goes_to_the_one_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "the-secret")
    config = one_endpoint(llm={"chat_url": URL, "model": "chat-model",
                               "api_key_env": "LLM_API_KEY", "timeout_s": 20})
    endpoint = Endpoint()
    ai_routes.call("small", MSG, cfg=config, opener=endpoint)
    assert endpoint.requests[0]["auth"] == "Bearer the-secret"


def test_images_ride_on_the_last_user_message():
    config = cfg(llm={"chat_url": LOCAL_URL, "model": "m", "api_key_env": "", "timeout_s": 20})
    endpoint = Endpoint()
    ai_routes.call("small", [{"role": "user", "content": "look"}], images=[b"\xff\xd8"],
                   cfg=config, opener=endpoint)
    content = endpoint.requests[0]["body"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_describe_names_the_one_endpoint():
    config = one_endpoint()
    assert ai_routes.describe("chat", config) == f"chat-model at {URL}"
    assert ai_routes.describe("heavy", cfg()) == ""


def test_a_caller_timeout_beats_the_endpoint_default():
    config = one_endpoint()
    endpoint = Endpoint()
    ai_routes.call("chat", MSG, cfg=config, opener=endpoint, timeout=60)
    ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert [r["timeout"] for r in endpoint.requests] == [60, 7]


# --- the callers ride the one route --------------------------------------------------------------

def patch_urlopen(monkeypatch, endpoint):
    monkeypatch.setattr("urllib.request.urlopen", endpoint)


def test_the_chat_lane_uses_the_one_endpoint_and_a_failure_raises():
    config = one_endpoint()
    endpoint = Endpoint(answer=json.dumps({"reply": "Press stop.", "command": "/print-stop"}))
    assert llm.configured(config)
    # #1524 tranche 6: the recorded legacy "command" is ignored - ask
    # returns the reply and no proposal, so nothing can be dispatched.
    assert llm.ask("kill it", "help text", cfg=config, opener=endpoint) == ("Press stop.", None)
    assert endpoint.requests[0]["url"] == URL and endpoint.models == ["chat-model"]
    failing = Endpoint(fail={"chat-model"})
    with pytest.raises(OSError):
        llm.ask("kill it", "help text", cfg=config, opener=failing)
    assert failing.models == ["chat-model"]  # no fallback retry


def test_llm_is_unconfigured_until_the_endpoint_exists():
    assert not llm.configured(cfg())
    assert llm.configured(one_endpoint())


def test_the_roast_never_goes_through_the_endpoint(monkeypatch, tmp_path):
    """Roasts are local since #1002: a fully configured endpoint is never
    called for one."""
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-key")
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    endpoint = Endpoint(answer='"A fine stand."')
    patch_urlopen(monkeypatch, endpoint)
    assert mel.generate_roast("Stand")
    assert endpoint.models == [] and endpoint.requests == []


def test_the_vision_check_and_label_read_use_the_small_tier_on_the_one_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")  # the fake endpoint is hosted; small insists on its key
    config = vw._config.cfg()
    endpoint = Endpoint(answer='{"verdict": "ok", "reason": "fine"}')
    patch_urlopen(monkeypatch, endpoint)
    assert vw.vision_ready()
    assert vw.visual_verdict(b"\xff\xd8", "p", context="") == ("ok", "fine")
    # Label reading shares the route.
    assert vw.visual_extract(b"\xff\xd8", "p", context="") == {"verdict": "ok", "reason": "fine"}
    urls = {request["url"] for request in endpoint.requests}
    assert urls == {config["llm"]["chat_url"]}


# --- settings ------------------------------------------------------------------------------------

def test_the_shipped_defaults_leave_every_tier_empty():
    assert settings._read(settings.HOME)["ai"] == {"allowed_users": "", "edit_requests_per_hour": 20,
                                                   "small": {}, "chat": {}, "heavy": {}}


def test_ai_tier_settings_hold_model_names_only():
    data = {}
    settings._validate_ai(data)
    assert data["ai"] == {"allowed_users": "", "edit_requests_per_hour": 20,
                          "small": {}, "chat": {}, "heavy": {}}
    good = {"ai": {"small": {"model": "m"}}}
    settings._validate_ai(good)
    assert good["ai"]["small"] == {"model": "m"}
    for bad in ({"model": "m", "shade": "red"},          # unknown keys stay refused
                {"model": "m", "chat_url": URL}):        # endpoint keys are not tier keys
        with pytest.raises(settings.SettingsError):
            settings._validate_ai({"ai": {"heavy": bad}})
    with pytest.raises(settings.SettingsError):
        settings._validate_ai({"ai": {"chat": "http://x"}})


def test_a_legacy_config_loads_with_every_removed_key_inert(tmp_path):
    """#1731: a config carrying any removed key still loads; the values do
    nothing — in particular the retired vision URL must not configure the
    small tier, and the retired [agent] section must not come back."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        "llm:\n  chat_url: ''\n  model: ''\n"
        "agent:\n  chat_url: http://agent.test/v1/chat/completions\n  model: agent-model\n"
        "design:\n  chat_url: http://design.test/v1/chat/completions\n  api_key_env: LLM_API_KEY\n"
        "services:\n  vision_chat_url: http://vision-legacy.test/v1/chat/completions\n"
        "vision:\n  model: legacy-vision-model\n  ai_enabled: true\n"
        "ai:\n  small:\n    chat_url: http://tier.test/v1/chat/completions\n"
        "    model: tier-model\n    fallback_model: tier-fallback\n"
        "    api_key_env: LLM_API_KEY\n    timeout_s: 5\n",
        encoding="utf-8")
    data = settings.load(local=legacy, environ={})
    assert "agent" not in data
    assert "chat_url" not in data["design"] and "api_key_env" not in data["design"]
    assert "vision_chat_url" not in data["services"]
    assert "model" not in data["vision"]
    assert data["ai"]["small"] == {"model": "tier-model"}
    # Nothing resolves: the retired URLs are inert and [llm] is empty.
    for tier in ai_routes.TIERS:
        assert not ai_routes.configured(tier, data)
        assert ai_routes.endpoint_url(tier, data) == ""


def test_a_legacy_env_override_for_a_removed_key_is_ignored():
    data = settings.load(local=None, environ={
        "PRINT_HANDY__AGENT__CHAT_URL": "http://agent.test/v1/chat/completions",
        "PRINT_HANDY__SERVICES__VISION_CHAT_URL": "http://vision-legacy.test/v1/chat/completions",
        "PRINT_HANDY__VISION__MODEL": "legacy-model",
        "PRINT_HANDY__DESIGN__CHAT_URL": "http://design.test/v1/chat/completions"})
    assert "agent" not in data
    assert "vision_chat_url" not in data["services"]
    for tier in ai_routes.TIERS:
        assert not ai_routes.configured(tier, data)
