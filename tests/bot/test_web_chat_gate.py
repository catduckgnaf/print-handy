"""Web chat's own switch: [webui] chat_enabled (PR #1106 review).

``chat_enabled`` (default true) turns the whole web chat surface off:
the service processes no message and no button press. It is the
page's own switch; ``chat_llm_enabled`` only governs the stray-chat
model lane and is a separate helper.

There is deliberately no claim-based lock on this surface: an install
with no login set up prints and approves from web chat exactly like a
claimed one (catduck's ruling on PR #1106, reversing an earlier
direction the same day). The link-flow test below runs against a cfg
with no saved password, no saved leave-open choice and no access
token in the environment, and must reach the print confirmation.
"""
import time
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.bot import web


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


# -- the two helpers --------------------------------------------------------

def test_chat_enabled_is_the_page_switch_not_the_llm_gate(cfg):
    assert web.chat_enabled(cfg) is True
    assert web.chat_llm_enabled(cfg) is False  # chat AI is off by default (#1376)
    cfg["webui"]["chat_llm_enabled"] = True
    assert web.chat_llm_enabled(cfg) is True
    cfg["webui"]["chat_enabled"] = False
    assert web.chat_enabled(cfg) is False
    assert web.chat_llm_enabled(cfg) is True  # the LLM lane is separate
    cfg["webui"]["chat_llm_enabled"] = False
    assert web.chat_llm_enabled(cfg) is False
    assert web.chat_enabled(cfg) is False


def test_chat_enabled_setting_validates(tmp_path):
    local = Path(tmp_path) / "local.yaml"
    local.write_text("webui:\n  chat_enabled: false\n")
    data = settings.load(local=local, environ={})
    assert data["webui"]["chat_enabled"] is False
    local.write_text("webui:\n  chat_enabled: 'yes'\n")
    with pytest.raises(settings.SettingsError):
        settings.load(local=local, environ={})


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
    defaults they are handed, explicitly — chat_enabled true and
    chat_llm_enabled false (#1376: AI is for designing models) — in all
    three shipped files (home.toml was retired in #1322).
    """
    for name in ("home.yaml", "docker.yaml", "docker.toml"):
        data = settings._read(settings.ROOT / "config" / name)
        assert data["webui"]["chat_enabled"] is True, name
        assert data["webui"]["chat_llm_enabled"] is False, name


# -- chat_enabled=false: the service processes nothing ----------------------

def test_disabled_service_processes_nothing(cfg):
    cfg["webui"]["chat_enabled"] = False
    service = web.WebChatService(cfg)
    assert service.submit("help") is False
    assert service.entries() == []
    assert service.answer("anything", 0) is None
    assert service.entries() == []


# -- no login, no lock: an unclaimed install prints --------------------------

def test_no_login_install_reaches_the_confirm_prompt(cfg, monkeypatch):
    """No saved password, no leave-open, no token: the flow still runs.

    This cfg carries no claim of any kind, and web chat must treat it
    exactly like a claimed install (catduck's ruling on PR #1106):
    the link reaches the handlers, the questions are asked, and the
    print confirmation can be answered by button press.
    """
    from unittest import mock

    from print_handy.bot import runtime
    from print_handy.core import link_intake, prepared_intake, printer_state
    from print_handy.runtime import _config

    monkeypatch.setattr(_config, "_override", cfg)
    monkeypatch.delenv("WEBUI_ACCESS_TOKEN", raising=False)
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
        assert service.answer(prompt["clarify_id"], 0) == "Print it"
        wait_for(service, "message", "Started.")
    assert seen["answers"] == (1, 0)


def _wait_prompt(service, question, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        prompt = service.open_prompt()
        if prompt and prompt["question"].startswith(question):
            return prompt
        time.sleep(0.02)
    raise AssertionError(f"no open prompt {question!r}; "
                         f"entries: {service.entries()}")
