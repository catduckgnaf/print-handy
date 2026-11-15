"""#1450: the Web UI workflow page runs the link/upload intake on the page.

Both entries (a pasted link, an uploaded file, plus an inbox file
started from the Upload page) go through the real server routes and
the real WebChatService/BotCore routing, with prepared_intake doubled
the way tests/bot/test_web_chat.py doubles it: the double asks its
questions through the real prompt machinery and writes the receipt the
real intake writes (bound to the session's thread, naming the sliced
file), so the page's buttons, its reattachment, and its download are
exercised end to end. The intake's own internals (and the real
bed-clear record) are the pipeline suites' subject.
"""
from __future__ import annotations

import http.client
import json
import re
import threading
import time
import urllib.parse
from pathlib import Path
from unittest import mock

import pytest

from print_handy import settings
from print_handy.webui import server as webui_server
from print_handy.webui import workflow

GCODE = "G28\nG1 X10 Y10 E1\n"
START_LABEL = "Start this exact plate? (bed is clear)"
MENU = [START_LABEL, "Send the files here", "Done, don't print"]


def load_settings(tmp, extra=""):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        f"paths:\n  state_home: {tmp}/hermes\n"
        "webui:\n  enabled: true\n  port: 8765\n" + extra)
    return settings.load(local=local, environ={})


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """A running server plus the doubled intake, on tmp state."""
    from print_handy.core import prepared_intake, printer_state
    from print_handy.runtime import _config

    data = load_settings(tmp_path)
    monkeypatch.setattr(_config, "_override", data)
    seen = {"answers": [], "starts": [], "events": []}

    async def fake_prepare(event, adapter_arg, state, root, reuse=None, **kwargs):
        from print_handy.bot import runtime
        from print_handy.core import link_intake
        source = event.source
        key = runtime.session_key(source)
        seen["events"].append(event)
        root = Path(root)
        iid = f"wf-{event.message_id}"
        color = await link_intake.ask(adapter_arg, source, key,
                                      "Which color?", ["Black", "White"])
        seen["answers"].append(color)
        folder = root / iid
        folder.mkdir(parents=True, exist_ok=True)
        gcode = folder / "plate_1.gcode"
        gcode.write_text(GCODE, encoding="utf-8")
        receipt = {"intake_id": iid, "url": "test", "status": "SLICED",
                   "sliced": True, "uploaded": False, "started": False,
                   "chat_id": source.chat_id, "thread_id": source.thread_id,
                   "platform": "webui", "gcodes": [str(gcode)],
                   "deliveries": []}
        (root / f"{iid}.json").write_text(json.dumps(receipt), encoding="utf-8")
        pick = await link_intake.ask(adapter_arg, source, key,
                                     "Sliced. What now?", MENU)
        seen["answers"].append(pick)
        if pick == 0:
            receipt["bed_clear_approval"] = {
                "job_json": str(folder / "JOB.json"), "gcode": str(gcode),
                "via": "button"}
            receipt["status"] = "START_REQUESTED"
            seen["starts"].append(str(gcode))
        elif pick == 1:
            receipt["deliveries"] = [{"method": "discord", "complete": True}]
            receipt["status"] = "SLICED_NOT_STARTED"
        else:
            receipt["status"] = "SLICED_NOT_STARTED"
        (root / f"{iid}.json").write_text(json.dumps(receipt), encoding="utf-8")
        await adapter_arg.send(chat_id=source.chat_id, content="All done.")
        return {"action": "skip", "reason": "test intake"}

    srv = webui_server.make_server(data, environ={}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    with mock.patch.object(prepared_intake, "prepare_link", fake_prepare), \
            mock.patch.object(printer_state, "load", return_value={}):
        yield srv, srv.server_address[1], seen, data
    srv.shutdown()


def call(port, method, path, body=None, headers=None, content_type=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = content_type or "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    response = conn.getresponse()
    payload = response.read()
    status, out_headers = response.status, dict(response.getheaders())
    conn.close()
    return status, out_headers, payload


def browser(port):
    """A browser session: the cookie the first workflow page issues."""
    status, headers, _page = call(port, "GET", "/workflow")
    assert status == 200
    cookie = headers.get("Set-Cookie") or ""
    assert cookie.startswith(webui_server.CHAT_COOKIE + "=")
    return {"Cookie": cookie.split(";")[0]}


def wait_page(port, headers, needle, timeout=15.0):
    deadline = time.time() + timeout
    page = ""
    while time.time() < deadline:
        status, _h, payload = call(port, "GET", "/workflow", headers=headers)
        page = payload.decode("utf-8", "replace")
        if needle in page:
            return page
        time.sleep(0.05)
    raise AssertionError(f"{needle!r} never appeared; page tail: {page[-800:]}")


def answer_current_question(port, srv, headers, index):
    """Press one button of the question the workflow page is showing."""
    page = wait_page(port, headers, 'action="/workflow/choice"')
    match = re.search(r'name="clarify_id" value="([^"]+)"', page)
    assert match, page[-800:]
    body = urllib.parse.urlencode({"_csrf": srv.form_token,
                                   "clarify_id": match.group(1), "index": index})
    status, _h, _p = call(port, "POST", "/workflow/choice", body=body,
                          headers=headers)
    assert status in (200, 303)


def multipart(token, filename, content):
    boundary = "----workflowtestboundary"
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="_csrf"\r\n\r\n{token}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + content + tail, f"multipart/form-data; boundary={boundary}"


def drive_to_menu(port, srv, headers):
    """Answer the color question; land on the final download-or-print menu."""
    wait_page(port, headers, "Which color?")
    answer_current_question(port, srv, headers, 1)
    wait_page(port, headers, "Sliced. What now?")
    page = wait_page(port, headers, "Send the files here")
    assert "Done, don&#x27;t print" in page or "Done, don't print" in page
    return page


# -- the two endings, one per entry -------------------------------------------


def test_link_entry_download_ending(harness):
    srv, port, seen, data = harness
    me = browser(port)
    status, _h, page = call(port, "GET", "/sources", headers=me)
    assert 'action="/workflow/link"' in page.decode()
    body = urllib.parse.urlencode(
        {"_csrf": srv.form_token,
         "url": "https://www.printables.com/model/123456-workflow-widget"})
    status, _h, _p = call(port, "POST", "/workflow/link", body=body, headers=me)
    assert status == 303
    drive_to_menu(port, srv, me)
    answer_current_question(port, srv, me, 1)  # Send the files here
    page = wait_page(port, me, "Your sliced file")
    match = re.search(r'href="(/workflow/download/[^"]+)"', page)
    assert match, page[-800:]
    status, headers, payload = call(port, "GET", match.group(1), headers=me)
    assert status == 200 and payload == GCODE.encode()
    assert headers["Content-Disposition"] == 'attachment; filename="plate_1.gcode"'
    [receipt] = workflow._receipts(data)
    assert receipt["deliveries"] and "bed_clear_approval" not in receipt
    assert seen["starts"] == []


def test_upload_entry_send_to_printer_ending(harness):
    srv, port, seen, data = harness
    me = browser(port)
    body, ctype = multipart(srv.form_token, "widget.stl", b"solid widget\nendsolid widget\n")
    status, _h, _p = call(port, "POST", "/workflow/upload", body=body,
                          headers=me, content_type=ctype)
    assert status in (200, 303)
    drive_to_menu(port, srv, me)
    answer_current_question(port, srv, me, 0)  # Start: the bed-clear button
    wait_page(port, me, "bed-clear approval is recorded")
    [receipt] = workflow._receipts(data)
    assert receipt["bed_clear_approval"]["via"] == "button"
    assert seen["starts"] == receipt["gcodes"]
    # The intake event carried the uploaded file, like a file dropped in chat.
    [event] = seen["events"]
    assert [Path(p).name for p in event.media_urls] == ["widget.stl"]


def test_inbox_file_starts_from_the_upload_page(harness, tmp_path):
    srv, port, seen, data = harness
    me = browser(port)
    body, ctype = multipart(srv.form_token, "inboxed.stl", b"solid x\nendsolid x\n")
    status, _h, _p = call(port, "POST", "/upload", body=body, headers=me,
                          content_type=ctype)
    assert status == 200
    status, _h, page = call(port, "GET", "/upload", headers=me)
    assert 'action="/workflow/start"' in page.decode()
    body = urllib.parse.urlencode({"_csrf": srv.form_token, "name": "inboxed.stl"})
    status, _h, _p = call(port, "POST", "/workflow/start", body=body, headers=me)
    assert status == 303
    drive_to_menu(port, srv, me)
    answer_current_question(port, srv, me, 2)  # Done, don't print
    page = wait_page(port, me, "Your sliced file")
    assert "/workflow/download/" in page
    [event] = seen["events"]
    assert [Path(p).name for p in event.media_urls] == ["inboxed.stl"]


# -- reattachment and guards ----------------------------------------------------


def test_leaving_and_returning_reattaches(harness):
    srv, port, seen, data = harness
    me = browser(port)
    body = urllib.parse.urlencode(
        {"_csrf": srv.form_token,
         "url": "https://www.printables.com/model/123456-workflow-widget"})
    call(port, "POST", "/workflow/link", body=body, headers=me)
    first = wait_page(port, me, "Which color?")
    # A fresh page load shows the same open question, not a restart.
    again = wait_page(port, me, "Which color?")
    assert 'action="/workflow/choice"' in again
    assert first.count("Which color?") >= 1 and again.count("Which color?") >= 1
    # A second start while one is running is refused, with the reason.
    status, _h, page = call(port, "POST", "/workflow/link", body=body, headers=me)
    assert status == 200 and "already running" in page.decode()
    assert len(seen["events"]) == 1
    # Finish, leave, return: the downloads are still there.
    drive_to_menu(port, srv, me)
    answer_current_question(port, srv, me, 1)
    wait_page(port, me, "Your sliced file")
    page = wait_page(port, me, "Your sliced file")
    assert "/workflow/download/" in page


def test_download_belongs_to_the_session_that_sliced(harness):
    srv, port, seen, data = harness
    me = browser(port)
    body = urllib.parse.urlencode(
        {"_csrf": srv.form_token,
         "url": "https://www.printables.com/model/123456-workflow-widget"})
    call(port, "POST", "/workflow/link", body=body, headers=me)
    drive_to_menu(port, srv, me)
    answer_current_question(port, srv, me, 1)
    page = wait_page(port, me, "Your sliced file")
    url = re.search(r'href="(/workflow/download/[^"]+)"', page).group(1)
    other = browser(port)  # a second browser: a different session scope
    status, _h, _p = call(port, "GET", url, headers=other)
    assert status == 404
    status, _h, _p = call(port, "GET", "/workflow/download/nope/0", headers=me)
    assert status == 404
    status, _h, _p = call(port, "GET", url + "9", headers=me)
    assert status == 404


def test_workflow_state_poll_shape(harness):
    srv, port, seen, data = harness
    me = browser(port)
    status, _h, payload = call(port, "GET", "/workflow/state", headers=me)
    assert status == 200
    state = json.loads(payload)
    assert set(state) == {"seq", "pending"}


# -- the receipt binding, directly ----------------------------------------------


def _write_receipt(tmp_path, scope, iid="abc", gcode=None):
    root = workflow.intake_root(load_settings(tmp_path))
    root.mkdir(parents=True, exist_ok=True)
    receipt = {"intake_id": iid, "platform": "webui", "thread_id": scope,
               "status": "SLICED_NOT_STARTED",
               "gcodes": [str(gcode)] if gcode else []}
    (root / f"{iid}.json").write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def test_receipt_lookup_is_scope_bound(tmp_path):
    data = load_settings(tmp_path)
    job = tmp_path / "job"
    job.mkdir()
    gcode = job / "plate_1.gcode"
    gcode.write_text(GCODE, encoding="utf-8")
    _write_receipt(tmp_path, "scope-a", gcode=gcode)
    assert workflow.latest_receipt(data, "scope-a")["intake_id"] == "abc"
    assert workflow.latest_receipt(data, "scope-b") is None
    assert workflow.download_target(data, "scope-a", "abc", "0") == gcode
    assert workflow.download_target(data, "scope-b", "abc", "0") is None
    assert workflow.download_target(data, "scope-a", "abc", "1") is None
    assert workflow.download_target(data, "scope-a", "abc", "../0") is None
    assert workflow.content_type(gcode).startswith("text/plain")


def test_sources_page_form_follows_the_chat_switch(tmp_path):
    from print_handy.webui import sources
    page = sources.render_sources_page(form_token="tok", chat_enabled=True)
    assert 'action="/workflow/link"' in page and 'name="_csrf" value="tok"' in page
    page = sources.render_sources_page(form_token="tok", chat_enabled=False)
    assert 'action="/workflow/link"' not in page
    assert "Web chat is not enabled" in page


def test_disabled_chat_disables_the_workflow(tmp_path, monkeypatch):
    from print_handy.runtime import _config
    data = load_settings(tmp_path, extra="  chat_enabled: false\n")
    monkeypatch.setattr(_config, "_override", data)
    srv = webui_server.make_server(data, environ={}, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        status, _h, page = call(port, "GET", "/workflow")
        assert status == 200 and "Web chat is turned off" in page.decode()
        body = urllib.parse.urlencode(
            {"_csrf": srv.form_token,
             "url": "https://www.printables.com/model/123456-x"})
        status, _h, page = call(port, "POST", "/workflow/link", body=body)
        assert status == 200 and "Web chat is turned off" in page.decode()
    finally:
        srv.shutdown()


def test_download_target_serves_only_plate_files(tmp_path):
    """A receipt naming something that is not a plate file resolves nothing
    (review of #1464: the receipt was trusted for the path)."""
    from print_handy.webui import workflow

    secret = tmp_path / "settings.toml"
    secret.write_text("token = 1")
    plate = tmp_path / "plate_1.gcode"
    plate.write_text("G28")
    receipt = {"intake_id": "abc", "gcodes": [str(secret), str(plate)]}
    with mock.patch.object(workflow, "receipt_for", return_value=receipt):
        assert workflow.download_target({}, "scope", "abc", "0") is None
        assert workflow.download_target({}, "scope", "abc", "1") == plate.resolve()
        assert workflow.download_target({}, "scope", "abc", "²") is None
