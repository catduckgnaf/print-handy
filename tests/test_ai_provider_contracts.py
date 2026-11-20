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
failure degrades to the named refusal the code defines --
``("uncertain", ...)``, ``None``, a ``FixError``, or one of the bot
runtime's AI terminal texts -- never an unhandled exception escaping the
caller-facing wrappers, and never a half-applied print action. Since
#1731 there is one endpoint and no fallback leg: a failed call fails
after exactly one attempt. Nothing here touches the network.
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


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "provider_responses"
URL = "http://models.test/v1/chat/completions"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
MSG = [{"role": "user", "content": "hi"}]
JPEG = b"\xff\xd8fake-jpeg"


def fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


def fixture_text(name):
    return (FIXTURES / name).read_text()


def endpoint_cfg(model="big", url=URL):
    """A config whose one endpoint answers under ``model``."""
    return {"ai": {"small": {}, "chat": {}, "heavy": {}},
            "llm": {"chat_url": url, "model": model, "api_key_env": "", "timeout_s": 20},
            "design": {}, "vision": {"roast_model": "roast-model"}}


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
def _no_bounds_impl(monkeypatch):
    monkeypatch.delenv("PRINT_HANDY_BOUNDS_IMPL", raising=False)


# ---------------------------------------------------------------- ai_routes


def test_recorded_chat_success_replays_through_the_route():
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": "openai_chat_success.json"})
    answer, served = ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert served == "big"
    assert endpoint.requests[0]["url"] == URL
    # The chat caller parses the recorded content into a reply plus typed
    # tool/proposal fields; the recorded legacy "command" string is
    # ignored - model output is never command text (#1524 tranche 6).
    assert llm._parse(answer) == ("Press stop to halt the print.", None, None)
    assert llm.allowed_command("/print-stop") == "/print-stop"


def test_recorded_malformed_body_raises_the_parse_error_after_one_attempt():
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": "provider_malformed_body.txt"})
    with pytest.raises(json.JSONDecodeError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big"], "no fallback leg since #1731: exactly one attempt"


@pytest.mark.parametrize("status,fixture", [
    (401, "openai_error_401.json"),
    (429, "openai_error_429.json"),
    (500, "openai_error_500.json"),
])
def test_recorded_http_error_bodies_raise_as_http_errors_with_their_status(status, fixture):
    # The recorded bodies are the providers' documented error shapes.
    recorded = json.loads(fixture_text(fixture))
    assert recorded["error"]["message"] and recorded["error"]["type"]
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": (status, fixture)})
    with pytest.raises(urllib.error.HTTPError) as caught:
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert caught.value.code == status
    assert endpoint.models == ["big"], "exactly one attempt"


def test_recorded_timeout_at_the_transport_seam_surfaces():
    # The timeout itself surfaces -- the route never invents an answer
    # and never retries on another model.
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": urllib.error.URLError("timed out")})
    with pytest.raises(urllib.error.URLError):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big"]


def test_recorded_empty_content_counts_as_a_failure():
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": "openai_empty_content.json"})
    with pytest.raises(ValueError, match="empty answer"):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big"]


@pytest.mark.parametrize("fixture", [
    "openai_wrong_shape_no_choices.json",
    "openai_wrong_shape_empty_choices.json",
    "openai_wrong_shape_no_content.json",
])
def test_recorded_wrong_shape_success_bodies_never_reach_the_caller(fixture):
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": fixture})
    with pytest.raises((KeyError, IndexError, TypeError, ValueError)):
        ai_routes.call("chat", MSG, cfg=config, opener=endpoint)
    assert endpoint.models == ["big"]


def test_recorded_refusal_content_is_plain_text_with_no_command():
    # A provider refusal is content, not a transport failure: the chat
    # parser returns it as the reply text and names no command, so no
    # printer action can ever be dispatched from it.
    config = endpoint_cfg()
    endpoint = RecordedEndpoint({"big": "openai_refusal_content.json"})
    reply, command = llm.ask("do something", "help text", cfg=config, opener=endpoint)
    assert reply == "I can't help with that request."
    assert command is None


# ---------------------------------------------------------------- vision route


def _vision(monkeypatch, endpoint):
    monkeypatch.setattr("urllib.request.urlopen", endpoint)
    return vw.visual_verdict(JPEG, "judge this frame", context="")


def _vision_config(monkeypatch):
    config = vw._config.cfg()
    monkeypatch.setitem(config, "llm", {"chat_url": LOCAL_URL, "model": "eyes",
                                        "api_key_env": "", "timeout_s": 20})
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
    assert vw.visual_extract(JPEG, "read the label", context="") is None
    endpoint = RecordedEndpoint({"eyes": "openai_vision_ok.json"})
    monkeypatch.setattr("urllib.request.urlopen", endpoint)
    assert vw.visual_extract(JPEG, "read the label",
                             context="") == {"verdict": "ok", "reason": "the print looks fine"}


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


def test_fuzzy_miss_plus_no_llm_is_exactly_the_ask_ai_refusal_setup():
    # The runtime's ask-ai fallback fires when fuzzy captures nothing and
    # no lane answers: pin both halves of that precondition here.
    assert runtime.command_suggestion("zzqqx blorp") is None
    assert runtime.command_suggestion_candidates("zzqqx blorp") == []
    empty = {"ai": {"small": {}, "chat": {}, "heavy": {}},
             "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 20},
             "design": {}, "vision": {"roast_model": ""}}
    assert not llm.configured(empty), "no endpoint: the AI lane cannot answer"
