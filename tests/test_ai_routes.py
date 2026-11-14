"""The optional AI tiers (#818): one routing module for the roast, the vision check, label reading and chat.

No test here touches the network: every call goes through a fake opener or a patched urlopen.
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
    for name in ("TIER_KEY", "BACKUP_KEY", "LLM_API_KEY", "CLIPROXY_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def cfg(**sections):
    base = {"ai": {"small": {}, "chat": {}, "heavy": {}}, "llm": {"chat_url": "", "model": "", "api_key_env": "",
                                                                     "timeout_s": 20},
            "design": {}, "services": {"vision_chat_url": ""},
            "vision": {"model": "check-model", "roast_model": "roast-model"}}
    for name, values in sections.items():
        base[name] = values
    return base


MSG = [{"role": "user", "content": "hi"}]


def test_an_install_that_configures_nothing_has_no_route_and_sends_nothing():
    endpoint = Endpoint()
    for tier in ai_routes.TIERS:
        assert not ai_routes.configured(tier, cfg())
        with pytest.raises(ai_routes.AINotConfigured):
            ai_routes.call(tier, MSG, cfg=cfg(), opener=endpoint)
    assert endpoint.requests == []


def test_default_routes_map_the_existing_settings(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "vision-key")
    config = cfg(llm={"chat_url": URL, "model": "chat-model", "api_key_env": "", "timeout_s": 7},
                 services={"vision_chat_url": OTHER_URL + "/"},
                 design={"chat_url": "", "model": "design-model", "api_key_env": "", "timeout_s": 99})
    endpoint = Endpoint()
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert (answer, served) == ("hello", "chat-model")
    assert endpoint.requests[0]["url"] == URL and endpoint.requests[0]["timeout"] == 7
    ai_routes.call("heavy", MSG, cfg=config, opener=endpoint)  # [design] over [llm], url inherited
    assert (endpoint.requests[1]["url"], endpoint.models[1], endpoint.requests[1]["timeout"]) == \
        (URL, "design-model", 99)
    # small: the vision endpoint, the check model by default and the roast model for a roast.
    ai_routes.call("small", MSG, cfg=config, opener=endpoint, compat=None, purpose="vision")
    ai_routes.call("small", MSG, cfg=config, opener=endpoint, purpose="roast")
    assert endpoint.models[2:] == ["check-model", "check-model"]
    assert "roast-model" not in endpoint.models
    assert endpoint.requests[2]["url"] == OTHER_URL


def test_a_tier_setting_wins_one_field_at_a_time():
    config = cfg(llm={"chat_url": URL, "model": "chat-model", "api_key_env": "", "timeout_s": 20},
                 ai={"small": {}, "heavy": {}, "chat": {"model": "better-model"}})
    endpoint = Endpoint()
    _answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "better-model" and endpoint.requests[0]["url"] == URL


def test_a_failed_primary_retries_once_on_the_fallback_model_and_reports_who_served():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big", "fallback_model": "small"}})
    endpoint = Endpoint(fail={"big"})
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert (answer, served) == ("hello", "small")
    assert endpoint.models == ["big", "small"]
    assert {request["url"] for request in endpoint.requests} == {URL}


def test_a_primary_that_works_never_touches_the_fallback():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big", "fallback_model": "small"}})
    endpoint = Endpoint()
    assert ai_routes.call("chat", MSG, cfg=config, opener=endpoint)[1] == "big"
    assert endpoint.models == ["big"]


def test_when_both_fail_there_is_exactly_one_retry_and_the_error_surfaces():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big", "fallback_model": "small"}})
    endpoint = Endpoint(fail={"big", "small"})
    with pytest.raises(OSError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big", "small"]


def test_without_a_fallback_a_failure_is_not_retried():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big"}})
    endpoint = Endpoint(fail={"big"})
    with pytest.raises(OSError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big"]


def test_an_empty_answer_counts_as_a_failure():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big", "fallback_model": "small"}})

    class Blank(Endpoint):
        def __call__(self, req, timeout=None):
            self.answer = "" if json.loads(req.data)["model"] == "big" else "ok"
            return super().__call__(req, timeout)

    endpoint = Blank()
    assert ai_routes.call("chat", MSG, cfg=config, opener=endpoint) == ("ok", "small")


def test_the_primary_key_never_goes_to_a_different_fallback_host(monkeypatch):
    monkeypatch.setenv("TIER_KEY", "primary-secret")
    monkeypatch.setenv("BACKUP_KEY", "backup-secret")
    tier = {"chat_url": URL, "model": "big", "api_key_env": "TIER_KEY",
            "fallback_url": OTHER_URL, "fallback_model": "small"}
    endpoint = Endpoint(fail={"big"})
    ai_routes.call("chat", MSG, cfg=cfg(ai={"small": {}, "heavy": {}, "chat": tier}), opener=endpoint)
    assert [r["auth"] for r in endpoint.requests] == ["Bearer primary-secret", None]
    tier["fallback_api_key_env"] = "BACKUP_KEY"
    endpoint = Endpoint(fail={"big"})
    ai_routes.call("chat", MSG, cfg=cfg(ai={"small": {}, "heavy": {}, "chat": tier}), opener=endpoint)
    assert [r["auth"] for r in endpoint.requests] == ["Bearer primary-secret", "Bearer backup-secret"]
    assert endpoint.requests[1]["url"] == OTHER_URL


def test_a_named_fallback_key_wins_even_on_the_primary_endpoint(monkeypatch):
    # The fallback model on the same endpoint can run under its own key (#818 review).
    monkeypatch.setenv("TIER_KEY", "primary-secret")
    monkeypatch.setenv("BACKUP_KEY", "backup-secret")
    tier = {"chat_url": URL, "model": "big", "api_key_env": "TIER_KEY",
            "fallback_model": "small", "fallback_api_key_env": "BACKUP_KEY"}
    endpoint = Endpoint(fail={"big"})
    ai_routes.call("chat", MSG, cfg=cfg(ai={"small": {}, "heavy": {}, "chat": tier}), opener=endpoint)
    assert [r["auth"] for r in endpoint.requests] == ["Bearer primary-secret", "Bearer backup-secret"]
    assert endpoint.requests[1]["url"] == URL


def test_the_small_tier_refuses_a_hosted_endpoint_without_a_key_and_sends_nothing():
    config = cfg(ai={"chat": {}, "heavy": {}, "small": {"chat_url": "https://api.example.com/v1/chat",
                                                         "model": "m", "api_key_env": "TIER_KEY"}})
    endpoint = Endpoint()
    assert not ai_routes.ready("small", config)
    with pytest.raises(ai_routes.AIKeyMissing):
        ai_routes.call("small", MSG, cfg=config, opener=endpoint)
    assert endpoint.requests == []


def test_the_chat_tier_still_sends_without_a_key_to_a_hosted_endpoint():
    config = cfg(llm={"chat_url": "https://api.example.com/v1/chat", "model": "m", "api_key_env": "LLM_API_KEY",
                      "timeout_s": 20})
    endpoint = Endpoint()
    ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.requests[0]["auth"] is None


def test_images_ride_on_the_last_user_message():
    config = cfg(ai={"chat": {}, "heavy": {}, "small": {"chat_url": LOCAL_URL, "model": "m"}})
    endpoint = Endpoint()
    ai_routes.call("small", [{"role": "user", "content": "look"}], images=[b"\xff\xd8"], cfg=config, opener=endpoint)
    content = endpoint.requests[0]["body"]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_describe_names_the_primary_and_the_fallback():
    config = cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "big", "fallback_model": "small"}})
    assert ai_routes.describe("chat", config) == f"big at {URL}, fallback small"
    assert ai_routes.describe("heavy", config) == ""


# --- the callers are moved onto the routes -------------------------------------------------------

def patch_urlopen(monkeypatch, endpoint):
    monkeypatch.setattr("urllib.request.urlopen", endpoint)


def test_the_chat_fallback_uses_the_chat_tier_fallback_model():
    config = cfg(llm={"chat_url": URL, "model": "chat-model", "api_key_env": "", "timeout_s": 20},
                 ai={"small": {}, "heavy": {}, "chat": {"fallback_model": "backup-model"}})
    endpoint = Endpoint(answer=json.dumps({"reply": "Press stop.", "command": "/print-stop"}), fail={"chat-model"})
    assert llm.configured(config)
    assert llm.ask("kill it", "help text", cfg=config, opener=endpoint) == ("Press stop.", "/print-stop")
    assert endpoint.models == ["chat-model", "backup-model"]


def test_llm_is_unconfigured_until_a_chat_route_exists():
    assert not llm.configured(cfg())
    assert llm.configured(cfg(ai={"small": {}, "heavy": {}, "chat": {"chat_url": URL, "model": "m"}}))


def test_the_roast_never_goes_through_the_small_tier(monkeypatch, tmp_path):
    """Roasts are local since #1002: a fully configured small tier (with a
    fallback) is never called for one."""
    config = mel._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {},
                                       "small": {"chat_url": LOCAL_URL, "model": "roast-a", "fallback_model": "roast-b"}})
    monkeypatch.setitem(config["services"], "vision_chat_url", "")
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "test-key")
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.setattr(mel, "ROAST_STATE", tmp_path / "roast.json")
    endpoint = Endpoint(answer='"A fine stand."')
    patch_urlopen(monkeypatch, endpoint)
    assert mel.generate_roast("Stand")
    assert endpoint.models == [] and endpoint.requests == []


def test_the_vision_check_uses_an_ai_small_endpoint_when_no_vision_url_is_set(monkeypatch):
    config = vw._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {},
                                       "small": {"chat_url": LOCAL_URL, "model": "eyes", "fallback_model": "eyes2"}})
    monkeypatch.setitem(config["services"], "vision_chat_url", "")
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    endpoint = Endpoint(answer='{"verdict": "ok", "reason": "fine"}', fail={"eyes"})
    patch_urlopen(monkeypatch, endpoint)
    assert vw.vision_ready(lambda: "")
    verdict = vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: "", model="m",
                                api_key=lambda: "", context="")
    assert verdict == ("ok", "fine")
    assert endpoint.models == ["eyes", "eyes2"]
    # Label reading shares the route.
    assert vw.visual_extract(b"\xff\xd8", "p", chat_url=lambda: "", model="m",
                             api_key=lambda: "", context="") == {"verdict": "ok", "reason": "fine"}


def test_the_old_vision_settings_still_drive_the_check(monkeypatch):
    config = vw._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {}, "small": {}})
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    endpoint = Endpoint(answer='{"verdict": "spaghetti", "reason": "tangle"}')
    patch_urlopen(monkeypatch, endpoint)
    verdict = vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: URL, model="legacy-model",
                                api_key=lambda: "k", context="")
    assert verdict == ("spaghetti", "tangle")
    assert endpoint.models == ["legacy-model"] and endpoint.requests[0]["auth"] == "Bearer k"


# --- settings ------------------------------------------------------------------------------------

def test_the_shipped_defaults_leave_every_tier_empty():
    assert settings._read(settings.HOME)["ai"] == {"allowed_users": [], "edit_requests_per_hour": 20,
                                                "small": {}, "chat": {}, "heavy": {}}


def test_ai_tier_settings_are_validated():
    data = {}
    settings._validate_ai(data)
    assert data["ai"] == {"allowed_users": [], "edit_requests_per_hour": 20,
                                                "small": {}, "chat": {}, "heavy": {}}
    good = {"ai": {"small": {"chat_url": URL, "model": "m", "fallback_model": "n", "fallback_url": OTHER_URL,
                             "api_key_env": "TIER_KEY", "fallback_api_key_env": "BACKUP_KEY", "timeout_s": 5}}}
    settings._validate_ai(good)
    for bad in ({"chat_url": URL},  # no model
                {"chat_url": "ftp://x", "model": "m"},
                {"model": "m", "api_key_env": "not a name"},
                {"model": "m", "fallback_url": "models.test"},
                {"model": "m", "timeout_s": 0},
                {"model": "m", "shade": "red"}):
        with pytest.raises(settings.SettingsError):
            settings._validate_ai({"ai": {"heavy": bad}})
    with pytest.raises(settings.SettingsError):
        settings._validate_ai({"ai": {"chat": "http://x"}})


def test_a_tier_that_only_supplies_a_key_makes_the_inherited_endpoint_ready(monkeypatch):
    monkeypatch.setenv("TIER_KEY", "tier-secret")
    config = vw._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {}, "small": {"api_key_env": "TIER_KEY"}})
    monkeypatch.setitem(config["services"], "vision_chat_url", "https://vision.example.com/v1/chat")
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    assert vw.vision_ready(lambda: "")
    endpoint = Endpoint(answer='{"verdict": "ok", "reason": "fine"}')
    patch_urlopen(monkeypatch, endpoint)
    assert vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: "https://vision.example.com/v1/chat",
                             model="m", api_key=lambda: "", context="") == ("ok", "fine")
    assert endpoint.requests[0]["auth"] == "Bearer tier-secret"
    monkeypatch.delenv("TIER_KEY")
    assert not vw.vision_ready(lambda: "")
    assert vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: "https://vision.example.com/v1/chat",
                             model="m", api_key=lambda: "", context="") == ("uncertain", "vision key is not configured")


def test_a_configured_tier_timeout_beats_the_caller_default_and_the_default_applies_otherwise(monkeypatch):
    endpoint = Endpoint()
    config = cfg(ai={"chat": {}, "heavy": {}, "small": {"chat_url": LOCAL_URL, "model": "m", "timeout_s": 9}})
    ai_routes.call("small", MSG, cfg=config, opener=endpoint, compat=ai_routes.Compat(timeout_s=4))
    config["ai"]["small"].pop("timeout_s")
    ai_routes.call("small", MSG, cfg=config, opener=endpoint, compat=ai_routes.Compat(timeout_s=4))
    assert [r["timeout"] for r in endpoint.requests] == [9, 4]


def test_the_vision_call_passes_its_legacy_timeout_as_default(monkeypatch):
    config = mel._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {}, "small": {"chat_url": LOCAL_URL, "model": "m",
                                                                            "timeout_s": 15}})
    monkeypatch.setattr(mel, "_profile_env_value", lambda name: "k")
    monkeypatch.setattr(mel, "log", lambda line: None); monkeypatch.setattr(mel, "warn", lambda line: None)
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    endpoint = Endpoint(answer='{"verdict": "ok", "reason": "x"}')
    patch_urlopen(monkeypatch, endpoint)
    # The roast's old 4 s small-tier default is gone with the roast's model
    # call (#1002); the vision check keeps its own.
    vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: "", model="m", api_key=lambda: "", context="")
    assert [r["timeout"] for r in endpoint.requests] == [15]
    config["ai"]["small"].pop("timeout_s")
    vw.visual_verdict(b"\xff\xd8", "p", chat_url=lambda: "", model="m", api_key=lambda: "", context="")
    assert [r["timeout"] for r in endpoint.requests[1:]] == [60]
