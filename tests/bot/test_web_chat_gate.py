"""Web chat's own switch and the unclaimed print lock (PR #1106 review).

Two gates live in the web chat service, nowhere else:

- ``[webui] chat_enabled`` (default true) turns the whole web chat
  surface off: the service processes no message and no button press.
  It is the page's own switch; ``chat_llm_enabled`` only governs the
  stray-chat model lane and is a separate helper.

- While the install is unclaimed — no saved password, no saved
  leave-open choice and no WEBUI_ACCESS_TOKEN in the environment (the
  print_handy.webui.access model) — web chat refuses to start a print
  (a model link, /print-again, /print-next, /print-design) and refuses
  to answer a print-approval prompt, posting one explanation instead.
  Everything else in web chat keeps working, and the shared adapter
  machinery other platforms use is untouched: the gate is the web
  service's submit/answer choke point, not BotCore and not the base
  adapter.
"""
import time
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.bot import web
from print_handy.core import prompts
from print_handy.webui import access


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


def refusals(service):
    return [e for e in service.entries()
            if "locked until this install is claimed" in (e.get("content") or "")]


@pytest.fixture
def unclaimed(cfg, tmp_path, monkeypatch):
    """A cfg whose install nobody has claimed, state in a temp folder."""
    monkeypatch.delenv("WEBUI_ACCESS_TOKEN", raising=False)
    cfg["paths"]["hermes_home"] = tmp_path / "hermes"
    assert web.install_claimed(cfg) is False
    return cfg


def claim_with_open(cfg):
    access.save_open(cfg, True)


# -- the two helpers --------------------------------------------------------

def test_chat_enabled_is_the_page_switch_not_the_llm_gate(cfg):
    assert web.chat_enabled(cfg) is True
    assert web.chat_llm_enabled(cfg) is True
    cfg["webui"]["chat_enabled"] = False
    assert web.chat_enabled(cfg) is False
    assert web.chat_llm_enabled(cfg) is True  # the LLM lane is separate
    cfg["webui"]["chat_llm_enabled"] = False
    assert web.chat_llm_enabled(cfg) is False
    assert web.chat_enabled(cfg) is False


def test_install_claimed_variants(unclaimed, cfg, monkeypatch):
    assert web.install_claimed(cfg) is False
    # A saved leave-open choice claims it.
    claim_with_open(cfg)
    assert web.install_claimed(cfg) is True


def test_install_claimed_by_saved_password(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_ACCESS_TOKEN", raising=False)
    cfg["paths"]["hermes_home"] = tmp_path / "hermes"
    assert web.install_claimed(cfg) is False
    access.save_password(cfg, "correct horse battery",
                         "correct horse battery", username="catduck")
    assert web.install_claimed(cfg) is True


def test_install_claimed_by_env_token(unclaimed, cfg, monkeypatch):
    monkeypatch.setenv("WEBUI_ACCESS_TOKEN", "sekrit")
    assert web.install_claimed(cfg) is True


def test_install_claimed_by_require_auth_opt_out(unclaimed, cfg):
    # require_auth=false is the operator's deliberate open choice in the
    # config file — the same class of decision as a saved leave-open.
    cfg["webui"]["require_auth"] = False
    assert web.install_claimed(cfg) is True


def test_chat_toggles_load_from_env_and_file_opt_out_stands(tmp_path):
    """The .env route works, and no layer invents a true over a file false.

    The Compose file carries no environment entry for either toggle
    (P2-88, pinned in tests/test_docker_release.py); the values reach
    the container through the .env env_file, i.e. the process
    environment the loader reads. Absent from the environment, a
    config file's explicit false stands.
    """
    local = Path(tmp_path) / "local.yaml"
    local.write_text("webui:\n  chat_enabled: false\n"
                     "  chat_llm_enabled: false\n")
    data = settings.load(local=local, environ={})
    assert data["webui"]["chat_enabled"] is False
    assert data["webui"]["chat_llm_enabled"] is False
    env = {"PRINT_HANDY__WEBUI__CHAT_ENABLED": "true",
           "PRINT_HANDY__WEBUI__CHAT_LLM_ENABLED": "true"}
    data = settings.load(local=local, environ=env)
    assert data["webui"]["chat_enabled"] is True
    assert data["webui"]["chat_llm_enabled"] is True


def test_shipped_configs_show_both_chat_toggles_explicitly():
    """The default config files name both toggles, not a loader comment.

    A reviewer (and an operator) should find chat_llm_enabled in the
    defaults they are handed, at the same explicit true as
    chat_enabled — in all four shipped files.
    """
    for name in ("home.yaml", "home.toml", "docker.yaml", "docker.toml"):
        data = settings._read(settings.ROOT / "config" / name)
        assert data["webui"]["chat_enabled"] is True, name
        assert data["webui"]["chat_llm_enabled"] is True, name


def test_chat_enabled_setting_validates(tmp_path):
    local = Path(tmp_path) / "local.yaml"
    local.write_text("webui:\n  chat_enabled: false\n")
    data = settings.load(local=local, environ={})
    assert data["webui"]["chat_enabled"] is False
    local.write_text("webui:\n  chat_enabled: 'yes'\n")
    with pytest.raises(settings.SettingsError):
        settings.load(local=local, environ={})


# -- chat_enabled=false: the service processes nothing ----------------------

def test_disabled_service_processes_nothing(cfg):
    cfg["webui"]["chat_enabled"] = False
    service = web.WebChatService(cfg)
    assert service.submit("help") is False
    assert service.entries() == []
    assert service.answer("anything", 0) is None
    assert service.entries() == []


# -- unclaimed: print starts are refused ------------------------------------

def test_unclaimed_model_link_is_refused_and_intake_never_runs(
        unclaimed, cfg, monkeypatch):
    from unittest import mock

    from print_handy.core import prepared_intake, printer_state
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    calls = []

    async def fake_prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return {"action": "skip", "reason": "test intake"}

    service = web.WebChatService(cfg)
    with mock.patch.object(prepared_intake, "prepare_link", fake_prepare), \
            mock.patch.object(printer_state, "load", return_value={}):
        assert service.submit(
            "https://www.printables.com/model/123456-web-chat-widget") is True
        entry = wait_for(service, "message", "locked until this install is claimed")
    assert "password" in entry["content"]
    assert calls == []  # intake never ran
    assert len(refusals(service)) == 1


@pytest.mark.parametrize("text", ["/print-again", "/print-next plan-1",
                                  "/print-design a small storage box",
                                  "/design a small storage box"])
def test_unclaimed_print_commands_are_refused_before_botcore(
        unclaimed, cfg, monkeypatch, text):
    service = web.WebChatService(cfg)
    seen = []

    async def spy(event, **kwargs):
        seen.append(event)
        return {"action": "skip", "reason": "spy"}

    monkeypatch.setattr(service.core, "process", spy)
    assert service.submit(text) is True
    entry = wait_for(service, "message", "locked until this install is claimed")
    assert "password" in entry["content"]
    assert seen == []  # BotCore never saw the message


def test_unclaimed_nonprint_chat_still_works(unclaimed, cfg, monkeypatch):
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    service = web.WebChatService(cfg)
    assert service.submit("help") is True
    entry = wait_for(service, "message")
    assert entry["content"]  # the bot's own help text
    assert service.submit("/print-source") is True
    entry = wait_for(service, "message", "Printables")
    assert "printables" in entry["content"].casefold()
    assert refusals(service) == []


# -- claimed: the same submissions proceed ----------------------------------

def test_claimed_model_link_reaches_the_confirm_prompt(
        unclaimed, cfg, monkeypatch):
    from unittest import mock

    from print_handy.bot import runtime
    from print_handy.core import link_intake, prepared_intake, printer_state
    from print_handy.hermes import _config

    monkeypatch.setattr(_config, "_override", cfg)
    claim_with_open(cfg)
    seen = {}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        source = event.source
        key = runtime.session_key(source)
        color = await link_intake.ask(adapter_arg, source, key,
                                      "Which color?", ["Black", "White"])
        start = await link_intake.ask(adapter_arg, source, key,
                                      "Start the print?", ["Print it"],
                                      approval=True)
        seen["answers"] = (color, start)
        await adapter_arg.send(chat_id=source.chat_id, content="Started.")
        return {"action": "skip", "reason": "test intake"}

    service = web.WebChatService(cfg)
    with mock.patch.object(prepared_intake, "prepare_link", fake_prepare), \
            mock.patch.object(printer_state, "load", return_value={}):
        assert service.submit(
            "https://www.printables.com/model/123456-web-chat-widget") is True
        prompt = _wait_prompt(service, "Which color?")
        service.answer(prompt["clarify_id"], 1)
        prompt = _wait_prompt(service, "Start the print?")
        service.answer(prompt["clarify_id"], 0)
        wait_for(service, "message", "Started.")
    assert seen["answers"] == (1, 0)
    assert refusals(service) == []


def test_claimed_by_env_token_print_command_proceeds(
        unclaimed, cfg, monkeypatch):
    monkeypatch.setenv("WEBUI_ACCESS_TOKEN", "sekrit")
    service = web.WebChatService(cfg)
    seen = []

    async def spy(event, **kwargs):
        seen.append(event)
        return {"action": "skip", "reason": "spy"}

    monkeypatch.setattr(service.core, "process", spy)
    assert service.submit("/print-design a small storage box") is True
    deadline = time.time() + 5
    while not seen and time.time() < deadline:
        time.sleep(0.02)
    assert len(seen) == 1
    assert refusals(service) == []


# -- unclaimed: print-approval answers are refused --------------------------

def _wait_prompt(service, question, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        prompt = service.open_prompt()
        if prompt and prompt["question"].startswith(question):
            return prompt
        time.sleep(0.02)
    raise AssertionError(f"no open prompt {question!r}; "
                         f"entries: {service.entries()}")


def _open_approval_prompt(service, cid="cid-gate-1"):
    prompts.register(cid, "webui:webui::webui", "Start the print?",
                     ["Print it", "Cancel"], approval=True)
    service.adapter._record_prompt(
        cid, {"choices": ["Print it", "Cancel"],
              "question": "Start the print?"})
    return cid


def test_unclaimed_approval_press_is_refused_but_cancel_is_allowed(
        unclaimed, cfg):
    service = web.WebChatService(cfg)
    cid = _open_approval_prompt(service)
    assert service.answer(cid, 0) is None  # "Print it" refused
    entry = wait_for(service, "message", "locked until this install is claimed")
    assert "password" in entry["content"]
    assert service.open_prompt() is not None  # the question is still open
    # Cancelling starts nothing, so it stays allowed while unclaimed.
    cid2 = _open_approval_prompt(service, "cid-gate-2")
    assert service.answer(cid2, 1) == "Cancel"


def test_claimed_approval_press_resolves(unclaimed, cfg):
    claim_with_open(cfg)
    service = web.WebChatService(cfg)
    cid = _open_approval_prompt(service)
    assert service.answer(cid, 0) == "Print it"
    assert refusals(service) == []


def test_unclaimed_typed_approval_answer_is_refused(unclaimed, cfg):
    service = web.WebChatService(cfg)
    _open_approval_prompt(service)
    assert service.submit("Print it") is True
    wait_for(service, "message", "locked until this install is claimed")
    assert service.open_prompt() is not None  # still waiting, unresolved


def test_gate_is_the_web_service_not_the_shared_adapter(unclaimed, cfg):
    """Other platforms answer approvals through the same base adapter.

    The lock lives in WebChatService.answer, so a direct adapter answer
    (the path every other platform's service takes) still resolves —
    proving the gate did not leak into shared machinery.
    """
    import asyncio

    service = web.WebChatService(cfg)
    cid = _open_approval_prompt(service)
    label = asyncio.run(service.adapter.answer_choice(
        cid, 0, user_id=web.OPERATOR_ID))
    assert label == "Print it"
