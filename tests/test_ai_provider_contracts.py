"""AI/vision provider recorded contracts (deep-dive item 13).

Recorded provider response shapes live under
``tests/fixtures/provider_responses/`` and are authored from what the
parsers in this repo actually consume:

* ``print_handy.ai_routes._post`` reads
  ``data["choices"][0]["message"]["content"]`` from an OpenAI-compatible
  chat-completions body, and treats an empty answer as a failure.
* ``print_handy.bot.llm._parse`` extracts ``{"reply": ..., "command": ...}``
  JSON from that content and allowlists the command.
* ``print_handy.runtime.visual_watch`` parses the vision content as JSON
  with ``verdict`` / ``reason`` fields, fails closed to
  ``("uncertain", ...)`` for anything else, and never raises.
* ``print_handy.core.ai_fix._parse_reply`` pulls the first JSON object out
  of the heavy tier's reply and refuses (``FixError``) when there is none.

Every recorded shape is replayed through those real seams with a fake
transport (the same opener / ``urllib.request.urlopen`` seams
``tests/test_ai_routes.py`` uses). The contract under test: a provider
failure degrades to the named refusal or fallback the code defines --
a fallback model, ``("uncertain", ...)``, ``None``, a ``FixError``, or
one of the bot runtime's AI terminal texts -- never an unhandled
exception escaping the caller-facing wrappers, and never a half-applied
print action. Nothing here touches the network.
"""
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from print_handy import ai_routes
from print_handy.bot import llm, runtime
from print_handy.core import ai_fix
from print_handy.runtime import visual_watch as vw

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provider_responses"
URL = "http://models.test/v1/chat/completions"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
MSG = [{"role": "user", "content": "hi"}]
JPEG = b"\xff\xd8fake-jpeg"


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


def fixture_text(name):
    return (FIXTURES / name).read_text()


def cfg_with(tier, **tier_values):
    base = {"ai": {"small": {}, "chat": {}, "heavy": {}},
            "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 20},
            "design": {}, "services": {"vision_chat_url": ""},
            "vision": {"model": "check-model", "roast_model": "roast-model"}}
    base["ai"][tier] = tier_values
    return base


class RecordedEndpoint:
    """A fake transport that replays recorded bodies, keyed by model name.

    ``bodies`` maps a model name to a fixture file name (replayed as a
    200 response) or to ``(status, fixture)`` for an HTTP error in the
    provider's documented error shape, or to an exception instance to
    raise (timeout / transport failure at the seam).
    """

    def __init__(self, bodies):
        self.bodies = bodies
        self.requests = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        model = body["model"]
        self.requests.append({"url": req.full_url, "model": model, "timeout": timeout})
        action = self.bodies[model]
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, tuple):
            status, name = action
            raise urllib.error.HTTPError(req.full_url, status, f"recorded HTTP {status}",
                                         hdrs=None, fp=io.BytesIO(fixture_bytes(name)))
        return io.BytesIO(fixture_bytes(action))

    @property
    def models(self):
        return [request["model"] for request in self.requests]


@pytest.fixture(autouse=True)
def _no_custom_vision_endpoint(monkeypatch):
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.delenv("PRINT_HANDY_BOUNDS_IMPL", raising=False)


# ---------------------------------------------------------------- ai_routes


def test_recorded_chat_success_replays_through_the_route():
    config = cfg_with("chat", chat_url=URL, model="big")
    endpoint = RecordedEndpoint({"big": "openai_chat_success.json"})
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "big"
    # The chat caller parses the recorded content into reply + command.
    assert llm._parse(answer) == ("Press stop to halt the print.", "/print-stop")
    assert llm.allowed_command("/print-stop") == "/print-stop"


def test_recorded_malformed_body_falls_back_then_raises_the_parse_error():
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": "provider_malformed_body.txt",
                                 "small": "provider_malformed_body.txt"})
    with pytest.raises(json.JSONDecodeError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    # Exactly one retry: primary, then the fallback, never a third attempt.
    assert endpoint.models == ["big", "small"]


def test_recorded_malformed_body_on_the_primary_is_healed_by_the_fallback():
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": "provider_malformed_body.txt",
                                 "small": "openai_chat_success.json"})
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "small"
    assert "Press stop" in answer


@pytest.mark.parametrize("status,fixture", [
    (401, "openai_error_401.json"),
    (429, "openai_error_429.json"),
    (500, "openai_error_500.json"),
])
def test_recorded_http_error_bodies_raise_as_http_errors_with_their_status(status, fixture):
    # The recorded bodies are the providers' documented error shapes.
    recorded = json.loads(fixture_text(fixture))
    assert recorded["error"]["message"] and recorded["error"]["type"]
    config = cfg_with("chat", chat_url=URL, model="big")
    endpoint = RecordedEndpoint({"big": (status, fixture)})
    with pytest.raises(urllib.error.HTTPError) as caught:
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert caught.value.code == status
    assert endpoint.models == ["big"], "no fallback configured: no retry"


@pytest.mark.parametrize("status,fixture", [
    (401, "openai_error_401.json"),
    (429, "openai_error_429.json"),
    (500, "openai_error_500.json"),
])
def test_recorded_http_errors_fall_back_once_and_then_surface(status, fixture):
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": (status, fixture), "small": (status, fixture)})
    with pytest.raises(urllib.error.HTTPError) as caught:
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert caught.value.code == status
    assert endpoint.models == ["big", "small"]


def test_recorded_timeout_at_the_transport_seam_degrades_like_any_failure():
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": TimeoutError("timed out"),
                                 "small": "openai_chat_success.json"})
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert (served, "Press stop" in answer) == ("small", True)
    # With no fallback the timeout itself surfaces -- the route never
    # invents an answer.
    endpoint = RecordedEndpoint({"big": urllib.error.URLError("timed out")})
    with pytest.raises(urllib.error.URLError):
        ai_routes.call("chat", MSG, cfg=cfg_with("chat", chat_url=URL, model="big"),
                       opener=endpoint)


def test_recorded_empty_content_counts_as_a_failure_and_falls_back():
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": "openai_empty_content.json",
                                 "small": "openai_chat_success.json"})
    _answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "small"


@pytest.mark.parametrize("fixture", [
    "openai_wrong_shape_no_choices.json",
    "openai_wrong_shape_empty_choices.json",
    "openai_wrong_shape_no_content.json",
])
def test_recorded_wrong_shape_success_bodies_never_reach_the_caller(fixture):
    config = cfg_with("chat", chat_url=URL, model="big", fallback_model="small")
    endpoint = RecordedEndpoint({"big": fixture, "small": fixture})
    with pytest.raises((KeyError, IndexError, TypeError, ValueError)):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big", "small"]


def test_recorded_refusal_content_is_plain_text_with_no_command():
    # A provider refusal is content, not a transport failure: the chat
    # parser returns it as the reply text and names no command, so no
    # printer action can ever be dispatched from it.
    config = cfg_with("chat", chat_url=URL, model="big")
    endpoint = RecordedEndpoint({"big": "openai_refusal_content.json"})
    reply, command = llm.ask("do something", "help text", cfg=config, opener=endpoint)
    assert reply == "I can't help with that request."
    assert command is None


# ---------------------------------------------------------------- vision route


def _vision(monkeypatch, endpoint):
    monkeypatch.setattr("urllib.request.urlopen", endpoint)
    return vw.visual_verdict(JPEG, "judge this frame", chat_url=lambda: LOCAL_URL,
                             model="eyes", api_key=lambda: "", context="")


def _vision_config(monkeypatch):
    config = vw._config.cfg()
    monkeypatch.setitem(config, "ai", {"chat": {}, "heavy": {},
                                       "small": {"chat_url": LOCAL_URL, "model": "eyes"}})
    monkeypatch.setitem(config["services"], "vision_chat_url", "")
    return config


def test_recorded_vision_ok_and_spaghetti_verdicts_replay(monkeypatch):
    _vision_config(monkeypatch)
    endpoint = RecordedEndpoint({"eyes": "openai_vision_ok.json"})
    assert _vision(monkeypatch, endpoint) == ("ok", "the print looks fine")
    endpoint = RecordedEndpoint({"eyes": "openai_vision_spaghetti.json"})
    assert _vision(monkeypatch, endpoint) == ("spaghetti", "filament tangled on the bed")


def test_recorded_vision_unknown_verdict_fails_closed_to_uncertain(monkeypatch):
    _vision_config(monkeypatch)
    endpoint = RecordedEndpoint({"eyes": "openai_vision_unknown_verdict.json"})
    verdict, _detail = _vision(monkeypatch, endpoint)
    assert verdict == "uncertain", "an unrecognized verdict alerts; it never reads as safe"


def test_recorded_vision_missing_reason_defaults_to_an_empty_detail(monkeypatch):
    _vision_config(monkeypatch)
    endpoint = RecordedEndpoint({"eyes": "openai_vision_missing_reason.json"})
    assert _vision(monkeypatch, endpoint) == ("ok", "")


@pytest.mark.parametrize("action", [
    "provider_malformed_body.txt",
    (401, "openai_error_401.json"),
    (429, "openai_error_429.json"),
    (500, "openai_error_500.json"),
    TimeoutError("timed out"),
    "openai_wrong_shape_empty_choices.json",
])
def test_recorded_vision_failures_degrade_to_uncertain_never_raise(monkeypatch, action):
    _vision_config(monkeypatch)
    endpoint = RecordedEndpoint({"eyes": action})
    assert _vision(monkeypatch, endpoint) == ("uncertain", "the vision check could not be completed")


def test_recorded_vision_extract_returns_none_on_failure_and_data_on_success(monkeypatch):
    _vision_config(monkeypatch)
    endpoint = RecordedEndpoint({"eyes": (500, "openai_error_500.json")})
    monkeypatch.setattr("urllib.request.urlopen", endpoint)
    assert vw.visual_extract(JPEG, "read the label", chat_url=lambda: LOCAL_URL,
                             model="eyes", api_key=lambda: "", context="") is None
    endpoint = RecordedEndpoint({"eyes": "openai_vision_ok.json"})
    monkeypatch.setattr("urllib.request.urlopen", endpoint)
    assert vw.visual_extract(JPEG, "read the label", chat_url=lambda: LOCAL_URL,
                             model="eyes", api_key=lambda: "",
                             context="") == {"verdict": "ok", "reason": "the print looks fine"}


def test_recorded_custom_vision_endpoint_shape_replays(monkeypatch):
    # PRINT_VISION_URL endpoints take the simple {image, prompt} shape and
    # answer with the verdict JSON directly (docs/SETTINGS.md contract).
    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.test/check")
    seen = {}

    def opener(req, timeout=None):
        seen["body"] = json.loads(req.data)
        return io.BytesIO(fixture_bytes("vision_custom_endpoint_ok.json"))

    monkeypatch.setattr("urllib.request.urlopen", opener)
    verdict = vw.visual_verdict(JPEG, "judge", chat_url=lambda: "", model="m",
                                api_key=lambda: "", context="")
    assert verdict == ("ok", "custom endpoint: the print looks fine")
    assert seen["body"]["prompt"] == "judge" and seen["body"]["image"]


# ---------------------------------------------------------------- heavy / AI fix


def test_recorded_fix_proposal_shape_parses_and_an_empty_proposal_is_refused():
    recorded = fixture_text("ai_fix_proposal.json")
    raw = ai_fix._parse_reply(recorded)
    assert raw["plans"] == []
    # An empty proposal is not a fix: the validator refuses it by name.
    with pytest.raises(ai_fix.FixError):
        ai_fix.validate_proposal(raw)


def test_recorded_non_json_heavy_reply_is_a_named_fix_refusal():
    refusal_text = json.loads(fixture_text("openai_refusal_content.json"))[
        "choices"][0]["message"]["content"]
    with pytest.raises(ai_fix.FixError, match="not a JSON fix proposal"):
        ai_fix._parse_reply(refusal_text)
    with pytest.raises(ai_fix.FixError, match="not a JSON fix proposal"):
        ai_fix._parse_reply(fixture_text("provider_malformed_body.txt"))


# ------------------------------------------------- ask-ai fallback (runtime)


def test_explicit_ask_ai_invocation_is_stripped_before_any_lane():
    assert runtime._ai_invocation("ask ai: how do I stop a print?") == "how do I stop a print?"
    assert runtime._ai_invocation("hey ai, what spools do I have?") == "what spools do I have?"
    assert runtime._ai_invocation("ai: status please") == "status please"
    assert runtime._ai_invocation("how do I stop a print?") is None
    assert runtime._ai_invocation("ask the ai") is None


def test_ask_ai_terminal_texts_are_named_refusals_not_the_command_list():
    not_enabled = runtime.ai_not_enabled_text()
    assert "AI chat isn't switched on" in not_enabled and "/print-help" in not_enabled
    no_answer = runtime.ai_no_answer_text()
    assert "didn't answer" in no_answer and "/print-help" in no_answer
    unavailable = runtime.agent_unavailable_text()
    assert "didn't answer" in unavailable and "/print-help" in unavailable


def test_fuzzy_miss_plus_no_llm_is_exactly_the_ask_ai_refusal_setup():
    # The runtime's ask-ai fallback fires when fuzzy captures nothing and
    # no lane answers: pin both halves of that precondition here.
    assert runtime.command_suggestion("zzqqx blorp") is None
    assert runtime.command_suggestion_candidates("zzqqx blorp") == []
    empty = {"ai": {"small": {}, "chat": {}, "heavy": {}},
             "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 20},
             "design": {}, "services": {"vision_chat_url": ""},
             "vision": {"model": "", "roast_model": ""}}
    assert not llm.configured(empty), "no chat route: the AI lane cannot answer"
