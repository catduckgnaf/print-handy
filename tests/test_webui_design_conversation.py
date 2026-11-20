"""The Designs page as a conversation (#1636).

One thread per design: your messages and the bot's replies in order,
each turn's preview at the turn that made it, the open question inline
with its buttons, one message box, and nothing else from the chat (no
print feed, no Chat or Workflow traffic). The thread is saved beside
its session, so it survives a reload and a restart and reopens from the
catalog; New design starts a fresh one and leaves the old session as it
was.

Everything is driven through the production entry points: the Web UI
server's routes, its web chat service and the real chat design
dispatch. The design pipeline is doubled the way the design workshop
tests double it: a scripted model behind the engine factory seam, a
fake OpenSCAD runner, and a recorded prepared-intake hand-off.
"""
import asyncio
import copy
import http.client
import json
import re
import threading
import urllib.parse

import pytest

from print_handy.bot import web
from print_handy.core import design, prepared_intake
from print_handy.runtime import _config
from print_handy.webui import designs as designs_module
from print_handy.webui import server as webui_server

from tests.test_webui_design_workshop import GOOD, THICKER, _runner, _scripted, _wait_for

DESCRIPTION = "a 40x20 plate with two holes"


# ------------------------------------------------------------ the harness

@pytest.fixture()
def flow_cfg(tmp_path, monkeypatch):
    cfg = copy.deepcopy(_config.cfg())
    cfg["bot"]["mode"] = "standalone"
    cfg["paths"]["cache"] = tmp_path / "cache"
    cfg["paths"]["state_home"] = tmp_path
    cfg["printer"] = dict(cfg.get("printer") or {},
                          bed_mm=[220, 220], max_print_height_mm=250)
    cfg["design"] = {"enabled": True, "chat_url": "http://design.test/v1",
                     "model": "fake", "timeout_s": 30, "repair_attempts": 1}
    cfg["webui"] = dict(cfg.get("webui") or {}, enabled=True, port=8765, chat_enabled=True)
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


@pytest.fixture()
def pipeline(monkeypatch):
    """The doubled design pipeline: (scripted model, recorded hand-offs)."""
    from print_handy.bot.handlers import design as design_handler
    from print_handy.core import printer_state

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
    monkeypatch.setattr(printer_state, "load", lambda **kwargs: {})
    return complete, handed


class Browser:
    """One browser on one Web UI server: its cookies, its requests."""

    def __init__(self, srv):
        self.srv = srv
        self.cookies = {}
        self.asked = set()  # clarify ids of the questions already waited for

    @property
    def port(self):
        return self.srv.server_address[1]

    def request(self, method, path, fields=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {}
        body = None
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if fields is not None:
            body = urllib.parse.urlencode({"_csrf": self.srv.form_token, **fields})
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        for name, value in response.getheaders():
            if name.lower() == "set-cookie":
                key, _, rest = value.partition("=")
                self.cookies[key] = rest.split(";")[0]
        conn.close()
        return response.status, payload

    def page(self, path="/designs"):
        status, payload = self.request("GET", path)
        assert status == 200, (path, status)
        return payload.decode("utf-8")

    def post(self, path, **fields):
        status, payload = self.request("POST", path, fields)
        assert status in (200, 303), (path, status, payload[:300])
        return status

    def say(self, text):
        return self.post("/designs", text=text)

    @property
    def scope(self):
        return web.scope_for(self.cookies[webui_server.CHAT_COOKIE])

    @property
    def thread_scope(self):
        return web.design_scope(self.scope, self.cookies.get("webui_design", ""))


def serve(cfg):
    srv = webui_server.make_server(cfg, environ={}, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(srv):
    service = getattr(srv, "chat_service", None)
    if service is not None:
        service.close()
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def server(flow_cfg):
    srv = serve(flow_cfg)
    yield srv
    stop(srv)


def service_of(srv):
    return webui_server.chat_service_for(srv)


def open_question(srv, browser, what="the What next question"):
    """The thread's next open question (never one this browser already saw)."""
    service = service_of(srv)

    def fresh():
        prompt = service.open_prompt(browser.thread_scope, shared=False)
        return prompt if prompt and prompt["clarify_id"] not in browser.asked else None

    prompt = _wait_for(fresh, what)
    browser.asked.add(prompt["clarify_id"])
    return prompt


def settled(srv, browser):
    """Wait until the thread's turn is done (a question open, or idle)."""
    service = service_of(srv)
    _wait_for(lambda: not service.busy(browser.thread_scope)
              or service.open_prompt(browser.thread_scope, shared=False), "the turn")


def current_session(cfg, browser):
    return design.newest_session_for_owner(
        design.sessions_root_for(cfg), designs_module.owner_for_scope(browser.thread_scope))


def thread_text(page):
    """The conversation's log, without the catalog below it."""
    start = page.index('<section class="design-workshop"')
    return page[start:page.index("</section>", start)]


def start_design(srv, browser):
    browser.page()  # the chat cookie, as a first visit issues it
    browser.say(DESCRIPTION)
    return open_question(srv, browser)


# ------------------------------------------------------------ start and iterate

def test_one_message_starts_a_design_and_the_next_iterates_it(server, flow_cfg, pipeline):
    complete, _handed = pipeline
    browser = Browser(server)
    prompt = start_design(server, browser)
    assert prompt["question"].startswith("What next for this design?")
    session = current_session(flow_cfg, browser)
    assert session["turns"] == 1 and session["description"] == DESCRIPTION

    page = thread_text(browser.page())
    # Your message (as typed, not the command the page wrapped it in),
    # then the bot's reply, then the turn's preview, then the question
    # with its own buttons.
    assert '<li class="chat-msg chat-op">' in page and DESCRIPTION in page
    assert "/print-design" not in page
    preview = f'src="/designs/{session["id"]}/preview?turn=1"'
    assert page.index(DESCRIPTION) < page.index("Design turn 1") < page.index(preview)
    # Once each: the saved thread and the live transcript never double up.
    assert page.count(f"<p>{DESCRIPTION}</p>") == 1
    assert page.count("Design turn 1") == 1 and page.count(preview) == 1
    for label in design.CHOICES:
        assert f">{label}</button>" in page
    assert 'action="/designs/choice"' in page
    assert page.count('class="design-composer"') == 1
    status, png = browser.request("GET", f"/designs/{session['id']}/preview?turn=1")
    assert status == 200 and png.startswith(b"\x89PNG")

    # A change typed while the question is open is the next turn.
    browser.say("give it a 6 mm lip")
    open_question(server, browser, "the question after the change")
    assert current_session(flow_cfg, browser)["turns"] == 2
    page = thread_text(browser.page())
    second = f'src="/designs/{session["id"]}/preview?turn=2"'
    assert (page.index(preview) < page.index("give it a 6 mm lip")
            < page.index("Design turn 2") < page.index(second))
    assert len(complete.calls) == 2


def test_a_button_answer_and_typed_answers_continue_the_design(server, flow_cfg, pipeline):
    _complete, handed = pipeline
    browser = Browser(server)
    prompt = start_design(server, browser)

    # Change it, by button: the design waits for the change.
    browser.post("/designs/choice", clarify_id=prompt["clarify_id"],
                 index=str(design.CHOICES.index(design.CHANGE_LABEL)))
    _wait_for(lambda: "Say the change" in thread_text(browser.page()), "the say-the-change reply")
    assert current_session(flow_cfg, browser)["status"] == "awaiting_revision"
    browser.say("give it a 6 mm lip")
    open_question(server, browser, "the question after the revision")
    assert current_session(flow_cfg, browser)["turns"] == 2

    # Slice it, typed: the hand-off into the normal intake.
    browser.say("slice it")
    _wait_for(lambda: handed, "the intake hand-off")
    assert handed[0].media_urls[0].endswith(".stl")
    assert current_session(flow_cfg, browser)["status"] == "handed_off"
    page = thread_text(browser.page())
    assert "slice it" in page and "Handing the design to the normal intake" in page


def test_a_typed_choice_number_answers_the_question(server, flow_cfg, pipeline):
    browser = Browser(server)
    start_design(server, browser)
    browser.say(str(design.CHOICES.index(design.CANCEL_LABEL) + 1))
    _wait_for(lambda: "Design cancelled" in thread_text(browser.page()), "the cancel reply")
    assert current_session(flow_cfg, browser)["status"] == "cancelled"


def test_a_clarifying_question_is_asked_in_the_thread_and_answered_there(
        server, flow_cfg, monkeypatch):
    """The one clarifying question a design may ask lands in the thread, and
    the next message in the same box is its answer (as in chat)."""
    from print_handy.bot.handlers import design as design_handler

    complete = _scripted(["QUESTION: How deep should the plate be?", GOOD])
    monkeypatch.setattr(design_handler, "_design_engine", lambda cfg, _c: design.DesignEngine(
        cfg["paths"]["cache"], complete, cfg=cfg, runner=_runner))
    browser = Browser(server)
    browser.page()
    browser.say("a plate with two holes")
    _wait_for(lambda: "How deep should the plate be?" in thread_text(browser.page()),
              "the clarifying question")
    settled(server, browser)
    assert current_session(flow_cfg, browser)["status"] == "awaiting_revision"
    assert "Describe a new part" not in thread_text(browser.page())  # the box answers
    browser.say("3 mm")
    open_question(server, browser, "the question after the answer")
    session = current_session(flow_cfg, browser)
    assert session["turns"] == 1 and session["question_answer"] == "3 mm"
    page = thread_text(browser.page())
    assert page.index("How deep should the plate be?") < page.index("<p>3 mm</p>") \
        < page.index("Design turn 1")


def test_design_routes_keep_suffix_before_query_and_reach_handlers(server, flow_cfg, pipeline):
    _complete, handed = pipeline
    browser = Browser(server)
    browser.page()
    browser.post("/chat/new?view=design")
    assert "Describe a part" in thread_text(browser.page("/chat?view=design"))
    browser.say(DESCRIPTION)
    prompt = open_question(server, browser)
    page = browser.page("/chat?view=design")
    assert 'action="/chat/new?view=design"' in page
    assert 'action="/chat/choice?view=design"' in page
    assert 'action="/chat/upload?view=design"' in page
    assert '"/chat/state?view=design"' in page
    browser.post("/chat/choice?view=design", clarify_id=prompt["clarify_id"],
                 index=str(design.CHOICES.index(design.SLICE_LABEL)))
    _wait_for(lambda: bool(handed), "the query-suffixed choice handler")
    assert current_session(flow_cfg, browser)["status"] == "handed_off"


def test_slice_it_by_button_hands_off(server, flow_cfg, pipeline):
    _complete, handed = pipeline
    browser = Browser(server)
    prompt = start_design(server, browser)
    browser.post("/designs/choice", clarify_id=prompt["clarify_id"],
                 index=str(design.CHOICES.index(design.SLICE_LABEL)))
    _wait_for(lambda: handed, "the intake hand-off")
    assert current_session(flow_cfg, browser)["status"] == "handed_off"


# ------------------------------------------------------------ only the design

def test_a_print_feed_post_never_appears_in_the_design_thread(server, flow_cfg, pipeline):
    """The screenshot regression: print-complete posts and their roasts in
    the same browser scope stay on the Chat page, out of the design."""
    browser = Browser(server)
    start_design(server, browser)
    service = service_of(server)
    feed = ("Print complete: Duck Bookends PAIR 22mm. "
            "Roast: that duck has seen things.")
    # A listener feed line is a shared post, the way feed_once sends it.
    asyncio.run_coroutine_threadsafe(
        service._in_scope("", lambda: service.adapter.send(web.CHAT_ID, feed)),
        service._loop).result(10)
    assert feed in browser.page("/chat")  # regular Chat remains separately reachable
    page = browser.page("/chat?view=design")
    assert "Duck Bookends" not in page and "Design turn 1" in page
    session = current_session(flow_cfg, browser)
    saved = json.dumps(design.load_thread(design.sessions_root_for(flow_cfg), session["id"]))
    assert "Duck Bookends" not in saved
    # Nor is a shared question (a listener alert) the design page's to
    # show or answer, though it is the newest one open.
    alert = "Print paused on the Garage printer. Resume?"
    service.adapter.registry.register("webalert-test", "listener-alert:webui:1",
                                      alert, ["Resume", "Cancel the print"])
    asyncio.run_coroutine_threadsafe(service._in_scope("", lambda: service.adapter.send_clarify(
        web.CHAT_ID, alert, ["Resume", "Cancel the print"], "webalert-test",
        "listener-alert:webui:1")), service._loop).result(10)
    assert "Print paused" in browser.page("/chat")
    page = browser.page("/chat?view=design")
    assert "Print paused" not in page and "What next for this design?" in page
    assert service.view(browser.thread_scope, shared=False).answer("webalert-test", 0) is None


def test_workflow_and_chat_no_longer_show_design_traffic(server, flow_cfg, pipeline):
    browser = Browser(server)
    start_design(server, browser)
    workflow = browser.page("/workflow")
    assert "Design turn 1" not in workflow and "What next for this design?" not in workflow
    assert "Start with a link" in workflow  # the start forms are not hidden
    assert "Current step" not in workflow
    chat = browser.page("/chat")
    assert "Design turn 1" not in chat and DESCRIPTION not in chat
    design_page = browser.page("/chat?view=design")
    assert "Design turn 1" in design_page and DESCRIPTION in design_page


# ------------------------------------------------------------ saved threads

def test_the_thread_survives_a_reload_and_a_restart(flow_cfg, pipeline):
    srv = serve(flow_cfg)
    browser = Browser(srv)
    try:
        start_design(srv, browser)
        session = current_session(flow_cfg, browser)
        first = thread_text(browser.page())
        assert thread_text(browser.page()) == first  # a reload changes nothing
    finally:
        stop(srv)

    # A new process: no live transcript, no open question. The thread
    # comes back from the session's saved conversation.
    srv = serve(flow_cfg)
    browser.srv = srv
    try:
        page = thread_text(browser.page())
        assert DESCRIPTION in page and "Design turn 1" in page
        assert f'src="/designs/{session["id"]}/preview?turn=1"' in page
        assert "the buttons are gone" in page
        assert 'action="/designs/choice"' not in page
        # The next message is the change, and the design carries on.
        browser.say("give it a 6 mm lip")
        open_question(srv, browser, "the question after the restart")
        assert current_session(flow_cfg, browser)["turns"] == 2
        page = thread_text(browser.page())
        assert page.index("Design turn 1") < page.index("give it a 6 mm lip") \
            < page.index("Design turn 2")
    finally:
        stop(srv)


def test_new_design_starts_fresh_and_the_old_session_reopens(server, flow_cfg, pipeline):
    browser = Browser(server)
    start_design(server, browser)
    old = current_session(flow_cfg, browser)
    browser.post("/designs/new")
    assert re.fullmatch(r"[0-9a-f]{12}", browser.cookies["webui_design"])
    page = thread_text(browser.page())
    assert "Design turn 1" not in page and "Describe a part" in page
    # Not deleted, not cancelled: the old session is as it was.
    assert design.load_session(design.sessions_root_for(flow_cfg), old["id"])["status"] == "active"
    assert old["id"] in browser.page()  # still in the catalog

    detail = browser.page(f"/designs/{old['id']}")
    assert DESCRIPTION in detail and "Design turn 1" in detail
    assert f"/designs/{old['id']}/preview?turn=1" in detail
    assert f'action="/designs/{old["id"]}/continue"' in detail
    assert 'href="/designs"' in detail and "<nav" in detail  # the shared nav

    # The new thread is its own design.
    browser.say("a 30 mm cube")
    open_question(server, browser, "the new design's question")
    new = current_session(flow_cfg, browser)
    assert new["id"] != old["id"] and new["description"] == "a 30 mm cube"

    # Continue the old one: it is this browser's thread again.
    browser.post(f"/designs/{old['id']}/continue")
    assert current_session(flow_cfg, browser)["id"] == old["id"]
    page = thread_text(browser.page())
    assert "Design turn 1" in page and "a 30 mm cube" not in page


def test_a_design_from_another_chat_opens_but_continues_there(server, flow_cfg, pipeline):
    root = design.sessions_root_for(flow_cfg)
    engine = design.DesignEngine(flow_cfg["paths"]["cache"], lambda m: GOOD, cfg=flow_cfg,
                                 runner=_runner)
    session = engine.start_session("123456:thread:42", "123456", "a hook for a bag")
    asyncio.run(engine.run_turn(session, "a hook for a bag"))
    browser = Browser(server)
    browser.page()
    detail = browser.page(f"/designs/{session['id']}")
    # No saved conversation (another platform's): its history stands in.
    assert "a hook for a bag" in detail and "replies are not shown" in detail
    assert f"/designs/{session['id']}/preview?turn=1" in detail
    assert "carries on there" in detail and "/continue" not in detail
    status, page = browser.request("POST", f"/designs/{session['id']}/continue", {})
    assert status == 200 and b"cannot be continued from this browser" in page
    assert design.load_session(root, session["id"])["owner_key"] == "123456:thread:42"


def test_a_turn_preview_resolves_only_through_the_session_record(server, flow_cfg, pipeline):
    browser = Browser(server)
    start_design(server, browser)
    sid = current_session(flow_cfg, browser)["id"]
    # A picture for a turn the record does not have (a stray file in the
    # folder) is not served: the record's turn count bounds the route.
    folder = design.sessions_root_for(flow_cfg) / sid
    (folder / "turn-2.png").write_bytes((folder / "turn-1.png").read_bytes())
    for query in ("turn=0", "turn=2", "turn=-1", "turn=abc", "turn=1/../../session.json",
                  "turn=%2e%2e"):
        status, _ = browser.request("GET", f"/designs/{sid}/preview?{query}")
        assert status == 404, query
    status, _ = browser.request("GET", "/designs/zzzzzzzzzzzz/preview?turn=1")
    assert status == 404


# ------------------------------------------------------------ a failed first turn

def test_a_failed_first_turn_says_so_and_offers_try_again(server, flow_cfg, monkeypatch):
    from print_handy.bot.handlers import design as design_handler
    from print_handy.core import printer_state

    calls = []

    def complete(messages):
        calls.append(messages)
        if len(calls) == 1:
            raise design.DesignError("the design model could not be reached")
        return GOOD

    monkeypatch.setattr(design_handler, "_design_engine", lambda cfg, _c: design.DesignEngine(
        cfg["paths"]["cache"], complete, cfg=cfg, runner=_runner))
    monkeypatch.setattr(printer_state, "load", lambda **kwargs: {})
    browser = Browser(server)
    browser.page()
    browser.say(DESCRIPTION)
    _wait_for(lambda: "did not work" in thread_text(browser.page()), "the failure reply")
    settled(server, browser)
    page = thread_text(browser.page())
    assert ("That design did not work: the design model could not be reached. "
            "Nothing was sliced or started.") in page
    assert "design change" not in page
    assert "Did not work" in page and ">Try again</button>" in page
    assert "Describe a new part" in page  # the box starts over, it is no change
    browser.post("/designs", retry="1")
    open_question(server, browser, "the question after Try again")
    session = current_session(flow_cfg, browser)
    assert session["turns"] == 1 and session["description"] == DESCRIPTION


# ------------------------------------------------------------ the off states

@pytest.mark.parametrize("change, reason", [
    ({"design": {"enabled": False}}, "Design is turned off"),
    ({"design": {"enabled": True, "chat_url": "", "model": ""},
      "llm": {"chat_url": "", "model": ""}}, "no design model is configured"),
    ({"webui": {"chat_enabled": False}}, "web chat is turned off"),
])
def test_off_states_show_the_reason_and_no_message_box(flow_cfg, change, reason):
    cfg = copy.deepcopy(flow_cfg)
    for key, value in change.items():
        cfg[key] = dict(cfg.get(key) or {}, **value)
    srv = serve(cfg)
    try:
        page = Browser(srv).page()
    finally:
        stop(srv)
    assert reason in page
    assert 'class="design-composer"' not in page and 'action="/designs/new"' not in page
