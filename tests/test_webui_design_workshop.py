"""The designs page workshop (#1449): design from the browser.

The page runs no design pipeline of its own: its forms submit through
the browser session's web chat view, so a design started here runs the
chat design dispatch (BotCore.process, /print-design) itself. These
tests cover the page's own surface (availability states, the workshop
rendering, the POST routes) and then drive one whole design through
the production entry points with the design pipeline doubled the way
the chat design tests double it: a scripted model behind the engine
factory seam, a fake OpenSCAD runner, and a recorded prepared-intake
hand-off.
"""
import asyncio
import copy
import http.client
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import settings
from print_handy.bot import web
from print_handy.core import design, prepared_intake
from print_handy.core.geometry_layout import write_binary_stl
from print_handy.runtime import _config
from print_handy.webui import designs as designs_module
from print_handy.webui import server as webui_server

GOOD = ("plate_z = 3;\n$fn = 48;\ndifference() {\n"
        "  translate([-20, -10, 0]) cube([40, 20, plate_z]);\n"
        "  for (x = [-15, 15]) translate([x, 0, -1]) cylinder(h = plate_z + 2, d = 4);\n}\n")
THICKER = GOOD.replace("plate_z = 3", "plate_z = 5")


# ------------------------------------------------------------ availability

def test_availability_states():
    off = {"design": {"enabled": False}}
    ready, reason = designs_module.design_availability(off)
    assert not ready and "design.enabled is false" in reason

    enabled_no_model = {"design": {"enabled": True, "model": ""},
                        "llm": {"chat_url": "", "model": ""}}
    ready, reason = designs_module.design_availability(enabled_no_model)
    assert not ready and "no design model is configured" in reason

    auto_no_model = {"design": {}, "llm": {"chat_url": "", "model": ""}}
    ready, reason = designs_module.design_availability(auto_no_model)
    assert not ready and "not set up yet" in reason

    configured = {"design": {"enabled": True, "model": "fake"},
                  "llm": {"chat_url": "http://x.test/v1", "model": "fake"}}
    assert designs_module.design_availability(configured) == (True, "")

    inherited = {"design": {}, "llm": {"chat_url": "http://x.test/v1", "model": "fake"}}
    assert designs_module.design_availability(inherited) == (True, "")


def test_owner_key_and_start_message():
    assert designs_module.owner_for_scope("abc") == "webui:abc:webui"
    assert designs_module.owner_for_scope("") == "webui::webui"
    assert designs_module.start_message("a box") == "/print-design a box"


# ------------------------------------------------------------ rendering

class FakeView:
    """The scoped chat view's shape, without a running service."""

    def __init__(self, scope="s1", *, busy=False, prompt=None, entries=()):
        self.scope = scope
        self._busy = busy
        self._prompt = prompt
        self._entries = list(entries)

    def busy(self):
        return self._busy

    def open_prompt(self):
        return self._prompt

    def entries(self, since=0):
        return list(self._entries)


READY_CFG = {"design": {"enabled": True, "model": "fake"},
             "llm": {"chat_url": "http://x.test/v1", "model": "fake"},
             "paths": {"cache": "/nonexistent-cache"}}


def _render(workshop, rows=()):
    return designs_module.render_designs_page(
        list(rows), workshop=workshop, form_token="tok123")


def test_no_form_when_design_is_off_or_unconfigured(tmp_path):
    cfg = {"design": {"enabled": False}, "paths": {"cache": tmp_path / "cache"}}
    page = _render(designs_module.workshop_model(cfg, FakeView()))
    assert "Design is turned off" in page
    assert 'action="/designs"' not in page

    cfg = {"design": {}, "llm": {"chat_url": "", "model": ""},
           "paths": {"cache": tmp_path / "cache"}}
    page = _render(designs_module.workshop_model(cfg, FakeView()))
    assert "not set up yet" in page
    assert 'action="/designs"' not in page


def test_no_form_when_web_chat_is_off():
    page = _render(designs_module.workshop_model(READY_CFG, None))
    assert "web chat is turned off" in page
    assert 'action="/designs"' not in page


def test_one_message_box_when_ready(tmp_path):
    cfg = dict(READY_CFG, paths={"cache": tmp_path / "cache"})
    model = designs_module.workshop_model(cfg, FakeView())
    assert model["phase"] == "start"
    page = _render(model)
    assert page.count('<form method="post" action="/designs" class="design-composer">') == 1
    assert 'name="_csrf" value="tok123"' in page
    assert "Describe a part" in page
    # #1636: no start/change form pair and no session table.
    assert 'name="mode"' not in page and "Describe the change" not in page
    assert "<th>Turns</th><td>" not in page and "Your current design" not in page
    assert designs_module.composer_message(model, "a box") == "/print-design a box"


def _make_session(tmp_path, owner, description="a 40x20 plate with two holes"):
    """One real session record on disk, written by the engine itself."""
    engine = design.DesignEngine(tmp_path / "cache", lambda messages: GOOD,
                                 cfg={"printer": {"bed_mm": [220, 220],
                                                  "max_print_height_mm": 250}},
                                 runner=_runner)
    session = engine.start_session(owner, "webui", description)
    asyncio.run(engine.run_turn(session, description))
    return session


def _box(dx, dy, dz, ox=0.0):
    v = [(ox, 0, 0), (ox + dx, 0, 0), (ox + dx, dy, 0), (ox, dy, 0),
         (ox, 0, dz), (ox + dx, 0, dz), (ox + dx, dy, dz), (ox, dy, dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _runner(cmd, **kwargs):
    """Fake openscad: one 40x20x3 box wherever the -o path says."""
    write_binary_stl(_box(40, 20, 3), Path(cmd[cmd.index("-o") + 1]))
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_workshop_panel_shows_the_live_session_and_its_question(tmp_path):
    cfg = dict(READY_CFG, paths={"cache": tmp_path / "cache"})
    session = _make_session(tmp_path, designs_module.owner_for_scope("s1"))
    prompt = {"clarify_id": "cid-1", "question": "What next for this design?",
              "choices": list(design.CHOICES)}
    view = FakeView("s1", prompt=prompt, entries=[
        {"seq": 1, "kind": "message", "content": "Made a plate, 40 x 20 x 3 mm."}])
    model = designs_module.workshop_model(cfg, view)
    assert model["session"]["id"] == session["id"]
    assert model["phase"] == "reply"  # the open question takes typed answers
    page = _render(model)
    # The thread: no conversation was saved for this engine-made session,
    # so its history (the request and the turn's preview) stands in, then
    # the live reply; the question is inline with its own buttons.
    assert "replies are not shown" in page
    assert f'src="/designs/{session["id"]}/preview?turn=1"' in page
    assert "Made a plate, 40 x 20 x 3 mm." in page
    assert '<form method="post" action="/designs/choice" class="inline">' in page
    for label in design.CHOICES:
        assert f">{label}</button>" in page
    assert page.count('class="design-composer"') == 1
    # Another scope never sees this session.
    other = designs_module.workshop_model(cfg, FakeView("someone-else"))
    assert other["session"] is None


def test_a_rendered_design_with_no_question_takes_the_next_message_as_its_change(tmp_path):
    # A restart closes the in-memory question; the design is still on
    # screen, so the box sends the next message as its change.
    cfg = dict(READY_CFG, paths={"cache": tmp_path / "cache"})
    _make_session(tmp_path, designs_module.owner_for_scope("s1"))
    model = designs_module.workshop_model(cfg, FakeView("s1"))
    assert model["lost"] and model["phase"] == "reply"
    assert designs_module.composer_message(model, "make it taller") == "make it taller"
    page = _render(model)
    assert "the buttons are gone" in page
    assert 'name="mode"' not in page and "Describe the change" not in page


# ------------------------------------------------------------ server routes

def load_settings(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        f"paths:\n  state_home: {tmp}/hermes\n"
        "webui:\n  enabled: true\n  port: 8765\n" + extra)
    return settings.load(local=local, environ={})


@pytest.fixture()
def running(tmp_path):
    srv = webui_server.make_server(load_settings(tmp_path), environ={}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    response = conn.getresponse()
    payload = response.read().decode("utf-8", "replace")
    conn.close()
    return response.status, payload


def chat_cookie(port, path="/designs"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 200
    header = response.getheader("Set-Cookie") or ""
    assert header.startswith(webui_server.CHAT_COOKIE + "=")
    return {"Cookie": header.split(";")[0]}, header.split(";")[0].split("=", 1)[1]


def test_server_page_without_design_config_shows_the_reason(running):
    _srv, port = running
    status, page = call(port, "GET", "/designs")
    assert status == 200
    assert "not set up yet" in page
    assert 'class="design-composer"' not in page


def test_server_post_without_form_token_is_refused(running):
    _srv, port = running
    body = urllib.parse.urlencode({"text": "a box"})
    status, _page = call(port, "POST", "/designs", body=body)
    assert status == 403


@pytest.fixture()
def running_configured(tmp_path, monkeypatch):
    extra = ("llm:\n  chat_url: http://127.0.0.1:9/v1\n  model: fake-model\n"
             "design:\n  enabled: true\n")
    data = load_settings(tmp_path, extra)
    data["paths"]["cache"] = tmp_path / "cache"  # never the machine's own cache
    # The bot inside web chat reads the runtime settings: the same ones.
    monkeypatch.setattr(_config, "_override", data)
    srv = webui_server.make_server(data, environ={}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_server_start_form_posts_the_command_to_web_chat(running_configured):
    srv, port = running_configured
    browser, secret = chat_cookie(port)
    status, page = call(port, "GET", "/designs", headers=browser)
    assert status == 200 and 'class="design-composer"' in page
    body = urllib.parse.urlencode({"_csrf": srv.form_token, "text": "a small storage box"})
    status, _page = call(port, "POST", "/designs", body=body, headers=browser)
    assert status == 303
    # The submission is in this browser's design thread (its own scope,
    # not the Chat page's), shown as typed; it went to the bot as the
    # /print-design command (designs.start_message), which is what starts
    # a session for the thread's owner key.
    service = webui_server.chat_service_for(srv)
    scope = web.design_scope(web.scope_for(secret))
    view = service.view(scope, shared=False)
    deadline = time.time() + 10
    seen = []
    while time.time() < deadline:
        seen = [e.get("content") or "" for e in view.entries()
                if e.get("kind") == "operator"]
        if seen:
            break
        time.sleep(0.05)
    assert seen == ["a small storage box"]
    assert not [e for e in service.view(web.scope_for(secret)).entries()
                if e.get("kind") == "operator"]
    root = designs_module.designs_root(srv.settings_data)
    session = _wait_for(lambda: design.newest_session_for_owner(
        root, designs_module.owner_for_scope(scope)), "the started session")
    assert session["description"] == "a small storage box"
    service.close()


# ------------------------------------------------------------ the whole flow

def _scripted(replies):
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


def _wait_for(predicate, what, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture()
def flow_cfg(tmp_path, monkeypatch):
    cfg = copy.deepcopy(_config.cfg())
    cfg["bot"]["mode"] = "standalone"
    cfg["paths"]["cache"] = tmp_path / "cache"
    cfg["paths"]["state_home"] = tmp_path
    cfg["printer"] = dict(cfg.get("printer") or {},
                          bed_mm=[220, 220], max_print_height_mm=250)
    cfg["llm"] = {"chat_url": "http://design.test/v1", "model": "fake",
                  "api_key_env": "LLM_API_KEY", "timeout_s": 20}
    cfg["design"] = {"enabled": True, "model": "fake", "timeout_s": 30,
                     "repair_attempts": 1}
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


def test_design_from_the_browser_start_iterate_and_hand_off(flow_cfg, monkeypatch):
    """Start, iterate, Slice it: the page's submissions, the real dispatch.

    The engine factory seam is doubled with a scripted model and a fake
    OpenSCAD runner (the chat design tests' doubling); prepared intake
    is recorded at its own entry point, so what is proven is the wiring:
    the page's start message runs a real design turn, the follow-up
    runs a second turn of the same session, and Slice it hands the
    session's STL to prepared_intake.prepare_link.
    """
    from print_handy.bot.handlers import design as design_handler

    complete = _scripted([GOOD, THICKER])

    def fake_engine(cfg, _complete):
        return design.DesignEngine(cfg["paths"]["cache"], complete, cfg=cfg,
                                   runner=_runner)

    monkeypatch.setattr(design_handler, "_design_engine", fake_engine)

    handed = []

    async def fake_prepare(event, adapter, state, root, **kwargs):
        handed.append(event)
        return {"action": "skip", "reason": "recorded by the test"}

    monkeypatch.setattr(prepared_intake, "prepare_link", fake_prepare)
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "load", lambda **kwargs: {})

    service = web.WebChatService(flow_cfg)
    try:
        scope = "workshop-test"
        view = service.view(scope)
        root = design.sessions_root(flow_cfg["paths"]["cache"])
        owner = designs_module.owner_for_scope(scope)

        # Start, the way the page's start form submits it.
        assert view.submit(designs_module.start_message(
            "a 40x20 plate with two holes")) is True
        prompt = _wait_for(lambda: service.open_prompt(scope),
                           "the What next question")
        assert prompt["question"].startswith("What next for this design?")
        assert prompt["choices"] == list(design.CHOICES)
        session = design.find_session_for_owner(root, owner)
        assert session is not None and session["turns"] == 1
        assert design.session_file(root, session["id"], "preview") is not None
        assert design.session_file(root, session["id"], "stl") is not None

        # Change it, then the follow-up, the way the page submits it.
        label = service.answer(prompt["clarify_id"], 1, scope)
        assert label == design.CHANGE_LABEL
        _wait_for(lambda: [e for e in service.entries(scope=scope)
                           if e.get("kind") == "message"
                           and "Say the change" in (e.get("content") or "")],
                  "the say-the-change reply")
        assert view.submit("make it 5 mm taller") is True
        prompt = _wait_for(lambda: service.open_prompt(scope),
                           "the What next question after the revision")
        session = design.find_session_for_owner(root, owner)
        assert session is not None and session["turns"] == 2

        # Slice it: the hand-off into prepared intake.
        label = service.answer(prompt["clarify_id"], 0, scope)
        assert label == design.SLICE_LABEL
        _wait_for(lambda: handed, "the intake hand-off")
        [event] = handed
        assert event.media_urls and event.media_urls[0].endswith(".stl")
        done = design.load_session(root, session["id"])
        assert done["status"] == "handed_off"
        assert complete.calls  # the model seam really was the pipeline's
    finally:
        service.close()
