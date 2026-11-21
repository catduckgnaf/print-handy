"""Upload a part and edit it, from the Designs page and the Chat page (#1721).

The Web UI had no way to drop a file into a design: the engine already
seeded edit sessions from a dropped file in chat (Discord, Telegram,
Matrix), but the browser could only describe a part in text, and the
chat page accepted no attachments at all. Both pages now have an
upload form that saves the file through the upload inbox and submits
it as a /print-design message with the file riding it — the exact
event a file drop produces — so the same dispatch, engine, question
and intake hand-off run.

Everything is driven through the production entry points: the Web UI
server's routes, its web chat service and the real chat design
dispatch, with the design pipeline doubled the way the design
conversation tests double it (a scripted model behind the engine
factory seam, a fake OpenSCAD runner, a recorded intake hand-off).
"""
import copy
import http.client

import pytest

from print_handy.core import design, prepared_intake
from print_handy.core.geometry_layout import write_binary_stl
from print_handy.runtime import _config
from print_handy.webui import chat as chat_module
from print_handy.webui import designs as designs_module
from print_handy.webui import upload as upload_module

from tests.test_webui_design_conversation import Browser, serve, stop
from tests.test_webui_design_workshop import GOOD, _box, _runner, _scripted, _wait_for


# ------------------------------------------------------------ unit shape

def test_upload_message_and_shown():
    assert designs_module.upload_message("make it taller") == "/print-design make it taller"
    assert designs_module.upload_message("  make  it\ntaller ") == "/print-design make it taller"
    assert designs_module.upload_message("") == "/print-design"
    assert designs_module.upload_shown("make it taller", "part.stl") == "📎 part.stl — make it taller"
    assert designs_module.upload_shown("", "part.stl") == "📎 part.stl"


def test_save_upload_scad_only_with_the_design_extra(tmp_path):
    data = {"paths": {"state_home": tmp_path}}
    saved, reason = upload_module.save_upload(data, "part.scad", b"cube([1, 1, 1]);")
    assert saved is None and reason == "only .stl and .3mf files can be uploaded"

    saved, reason = upload_module.save_upload(
        data, "part.scad", b"cube([1, 1, 1]);",
        extra_exts=designs_module.EDIT_UPLOAD_EXTS)
    assert reason is None and saved is not None and saved.read_bytes() == b"cube([1, 1, 1]);"

    saved, reason = upload_module.save_upload(
        data, "notes.txt", b"hello", extra_exts=designs_module.EDIT_UPLOAD_EXTS)
    assert saved is None and reason == "only .stl, .3mf and .scad files can be uploaded here"


class _FakeChatView:
    def __init__(self):
        self._entries = []

    def open_prompt(self):
        return None

    def busy(self):
        return False

    def entries(self, since=0):
        return list(self._entries)


def test_chat_page_renders_the_upload_form():
    page = chat_module.render_chat_page(_FakeChatView(), form_token="<redacted>")
    assert 'action="/chat/upload"' in page
    assert 'enctype="multipart/form-data"' in page
    assert 'accept=".stl,.3mf,.scad"' in page
    assert 'name="change"' in page


def test_designs_page_renders_the_upload_form_when_ready(tmp_path):
    from tests.test_webui_design_workshop import FakeView, READY_CFG, _render
    cfg = dict(READY_CFG, paths={"cache": tmp_path / "cache"})
    page = _render(designs_module.workshop_model(cfg, FakeView()))
    assert 'action="/designs/upload"' in page
    assert 'enctype="multipart/form-data"' in page
    assert 'accept=".stl,.3mf,.scad"' in page


# ------------------------------------------------------------ the flow

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
    cfg["webui"] = dict(cfg.get("webui") or {}, enabled=True, port=8765, chat_enabled=True)
    monkeypatch.setattr(_config, "_override", cfg)
    return cfg


@pytest.fixture()
def pipeline(monkeypatch):
    from print_handy.bot.handlers import design as design_handler
    from print_handy.core import printer_state

    complete = _scripted([GOOD])

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


@pytest.fixture()
def server(flow_cfg):
    srv = serve(flow_cfg)
    yield srv
    stop(srv)


def _stl_bytes(tmp_path):
    target = tmp_path / "part.stl"
    write_binary_stl(_box(40, 20, 3), target)
    return target.read_bytes()


def _multipart(token, filename, content, change=None):
    boundary = "----uploadedittestboundary"
    parts = [f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="_csrf"\r\n\r\n{token}\r\n']
    if change is not None:
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="change"\r\n\r\n{change}\r\n')
    head = "".join(parts).encode() + (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def _upload(browser, path, filename, content, change=None):
    body, ctype = _multipart(browser.srv.form_token, filename, content, change)
    conn = http.client.HTTPConnection("127.0.0.1", browser.port, timeout=15)
    headers = {"Content-Type": ctype}
    if browser.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in browser.cookies.items())
    conn.request("POST", path, body=body, headers=headers)
    response = conn.getresponse()
    payload = response.read()
    for name, value in response.getheaders():
        if name.lower() == "set-cookie":
            key, _, rest = value.partition("=")
            browser.cookies[key] = rest.split(";")[0]
    conn.close()
    return response.status, payload


def _edit_sessions(cfg):
    root = design.sessions_root_for(cfg)
    return [s for s in design.list_sessions(root) if s.get("kind") == "edit"]


def _wait_edit_session(cfg, what="the edit session"):
    # start_edit_session saves the session before its edit mode and
    # source land, so wait for the finished record, not the first one.
    def ready():
        return [s for s in _edit_sessions(cfg)
                if s.get("edit_mode") and (s.get("source") or {}).get("name")] or None

    return _wait_for(ready, what, timeout=60.0)



def _wait_turns(cfg, session_id, turns=1):
    """Wait (generously: sibling test suites share this machine) for a turn."""
    root = design.sessions_root_for(cfg)
    return _wait_for(
        lambda: (design.load_session(root, session_id) or {}).get("turns") == turns
        or None, f"turn {turns}", timeout=90.0)

def test_designs_upload_stl_with_a_change_starts_an_edit(server, flow_cfg, pipeline, tmp_path):
    complete, _handed = pipeline
    browser = Browser(server)
    browser.page()  # the chat cookie, as a first visit issues it
    status, _payload = _upload(browser, "/designs/upload", "widget.stl",
                                _stl_bytes(tmp_path), change="make it thicker")
    assert status in (200, 303)
    [session] = _wait_edit_session(flow_cfg)
    assert session["edit_mode"] == "mesh"
    assert session["source"]["name"] == "widget.stl"
    assert session["description"] == "make it thicker"
    # The change ran as the first turn: one model call, one turn done,
    # and the What-next question is open in the design thread.
    _wait_turns(flow_cfg, session["id"])
    assert len(complete.calls) == 1
    page = browser.page()
    assert "widget.stl" in page and "make it thicker" in page


def test_designs_upload_scad_without_a_change_shows_the_part_as_is(server, flow_cfg, pipeline):
    complete, _handed = pipeline
    browser = Browser(server)
    browser.page()
    status, _payload = _upload(browser, "/designs/upload", "plate.scad",
                                GOOD.encode())
    assert status in (200, 303)
    [session] = _wait_edit_session(flow_cfg)
    assert session["edit_mode"] == "scad"
    assert session["source"]["name"] == "plate.scad"
    # No change asked for: the baseline turn renders the part as it is,
    # with no model call in between.
    _wait_turns(flow_cfg, session["id"])
    assert complete.calls == []


def test_chat_upload_starts_the_edit_in_the_chat(server, flow_cfg, pipeline, tmp_path):
    complete, _handed = pipeline
    browser = Browser(server)
    status, page = browser.request("GET", "/chat")
    assert status == 200 and 'action="/chat/upload"' in page.decode()
    status, _payload = _upload(browser, "/chat/upload", "bracket.stl",
                                _stl_bytes(tmp_path), change="double the height")
    assert status in (200, 303)
    [session] = _wait_edit_session(flow_cfg)
    assert session["edit_mode"] == "mesh"
    assert session["source"]["name"] == "bracket.stl"
    _wait_turns(flow_cfg, session["id"])
    assert len(complete.calls) == 1
    status, page = browser.request("GET", "/chat")
    assert "bracket.stl" in page.decode()


def test_upload_refuses_a_file_that_is_not_a_part(server, flow_cfg, pipeline):
    browser = Browser(server)
    browser.page()
    status, payload = _upload(browser, "/designs/upload", "notes.txt", b"hello")
    assert status == 400
    assert b"only .stl, .3mf and .scad files can be uploaded here" in payload
    assert _edit_sessions(flow_cfg) == []
