"""Web chat and workflow handlers for the Web UI (#1603 item 3).

Moved out of ``webui/server.py``: the Chat page/state/posts and the
Workflow page/state/posts/download route, including this browser's
chat-cookie scoping. ``_Handler`` keeps thin delegates with the
route-table names (and ``_chat_service``, which delegates to
``server.chat_service_for``); behavior and output are unchanged.
"""
from __future__ import annotations

import html
import logging
import re
import secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from . import chat as chat_module
from . import data as data_module
from . import shell
from . import upload as upload_module
from . import workflow as workflow_module
from .sessions import REMEMBER_TTL_S

CHAT_COOKIE = "webui_chat"
_CHAT_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{32,64}")
TOKEN_HEADER = "X-WebUI-Token"

log = logging.getLogger(__name__)


def web_chat_enabled(handler):
    """[webui] chat_enabled, the chat page's own switch (PR #1106).

    Read from the server's settings mapping on every request, so
    the page a request gets always matches the saved configuration.
    """
    from ..bot.web import chat_enabled
    return chat_enabled(handler.server.settings_data)

def chat_page(handler, notice=None, error=None):
    if not handler._web_chat_enabled():
        # Off is a page, not an error: it says the state and the way
        # back on, and the chat service is never built behind it.
        handler._send(200, chat_module.render_chat_disabled_page(
            theme=handler._theme(),
            nav=data_module.nav_for(handler.server.settings_data)),
            "text/html; charset=utf-8")
        return
    ft = handler._form_token()
    view = handler._chat_view()
    prefill = (parse_qs(urlparse(handler.path).query).get("text") or [""])[0]
    handler._send(200, chat_module.render_chat_page(
        view, form_token=ft, notice=notice, error=error,
        theme=handler._theme(), prefill=prefill,
        nav=data_module.nav_for(handler.server.settings_data)),
        "text/html; charset=utf-8", headers=handler._chat_cookie_headers())

def cookie_value(handler, name):
    raw = handler.headers.get("Cookie") or ""
    if not raw:
        return ""
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # a malformed Cookie header carries nothing
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel is not None else ""

def chat_view(handler, issue=True):
    """This browser's view of the web chat: its own transcript only.

    Web chat is one conversation per Web UI session (#1145), never a
    shared outbox. The scope is the login session when the browser
    has one; on an install with no login it is the browser's own
    chat cookie, issued here on first use (``issue``). A request
    with neither (and ``issue`` off, as for the state poll) gets a
    fresh scope that holds nothing."""
    from ..bot.web import scope_for
    secret = handler._valid_session_id()
    if not secret and handler.server.access.required and handler.headers.get(TOKEN_HEADER):
        # A header client (no cookies) signed in with the access
        # token: one conversation for the token holder. The request
        # was already authorized by that header to get here.
        secret = "\x00access-token-client"
    if not secret:
        secret = handler._cookie_value(CHAT_COOKIE)
        if not _CHAT_COOKIE_VALUE.fullmatch(secret or ""):
            secret = secrets.token_urlsafe(32)
            if issue:
                handler._new_chat_cookie = secret
    return handler._chat_service().view(scope_for(secret))

def chat_cookie_headers(handler):
    """The Set-Cookie header for a chat cookie issued on this request."""
    value = getattr(handler, "_new_chat_cookie", None)
    if not value:
        return []
    flags = "; Secure" if handler._cookie_secure() else ""
    return [("Set-Cookie", f"{CHAT_COOKIE}={value}; HttpOnly; SameSite=Strict; "
                           f"Path=/; Max-Age={REMEMBER_TTL_S}{flags}")]

def chat_post(handler, form):
    if not handler._web_chat_enabled():
        handler._chat_page()  # the disabled page, with the explanation
        return
    text = (form.get("text") or [""])[0].strip()
    if not text:
        handler._redirect("/chat")
        return
    # The Ask AI checkbox is read here, on the authenticated POST,
    # and passed as a server-side flag — never parsed out of the
    # message text (#1524).
    ask_ai = (form.get("ask_ai") or [""])[0] in ("1", "true", "on")
    if not handler._chat_view().submit(text, ask_ai=ask_ai):
        handler._chat_page(error="Too many messages in the last minute; "
                              "give the bot a moment to catch up.")
        return
    handler._redirect("/chat", headers=handler._chat_cookie_headers())

def chat_choice_post(handler, form):
    if not handler._web_chat_enabled():
        handler._chat_page()  # the disabled page, with the explanation
        return
    clarify_id = (form.get("clarify_id") or [""])[0]
    try:
        index = int((form.get("index") or ["-1"])[0])
    except ValueError:
        index = -1
    if clarify_id and index >= 0:
        # A press can fail two ways, and neither may look like
        # success: the answer wait can time out (which used to
        # surface as a raw 500 from the outer POST guard), and the
        # press can resolve nothing — the question already closed,
        # or the press was refused — which used to redirect in
        # silence. Both come back as the page with an explanation.
        try:
            label = handler._chat_view().answer(clarify_id, index)
        except Exception:
            handler._chat_page(error="That answer didn't reach the bot "
                                  "in time. The question is still "
                                  "open — try again.")
            return
        if label is None:
            handler._chat_page(error="That answer wasn't accepted — "
                                  "the question may have closed, or "
                                  "the answer belongs to another step.")
            return
    handler._redirect("/chat")

def chat_state(handler):
    handler._json(200, chat_module.chat_state(
        handler._chat_view(issue=False) if handler._web_chat_enabled() else None))

def workflow_page(handler, notice=None, error=None):
    if not handler._web_chat_enabled():
        # The workflow runs on the web chat service, which is never
        # built while the switch is off: a page that says so, like
        # the chat page's disabled state.
        handler._send(200, workflow_module.render_workflow_disabled_page(
            theme=handler._theme(),
            nav=data_module.nav_for(handler.server.settings_data)),
            "text/html; charset=utf-8")
        return
    view = handler._chat_view()
    receipt = workflow_module.latest_receipt(handler.server.settings_data,
                                             view.scope)
    handler._send(200, workflow_module.render_workflow_page(
        view, receipt, form_token=handler._form_token(),
        notice=notice, error=error, theme=handler._theme(),
        nav=data_module.nav_for(handler.server.settings_data)),
        "text/html; charset=utf-8", headers=handler._chat_cookie_headers())

def workflow_state(handler):
    handler._json(200, chat_module.chat_state(
        handler._chat_view(issue=False) if handler._web_chat_enabled() else None))

def workflow_busy(handler, view):
    """True while this session's workflow is mid-flight: a turn
    running or a question open. A second start would queue a second
    intake behind it in the same conversation, so the start routes
    refuse and point at the running one."""
    return bool(view.busy() or view.open_prompt())

def workflow_link_post(handler, form):
    if not handler._web_chat_enabled():
        handler._workflow_page()
        return
    url = (form.get("url") or [""])[0].strip()
    if not url:
        handler._redirect("/workflow")
        return
    view = handler._chat_view()
    if handler._workflow_busy(view):
        handler._workflow_page(error="A workflow is already running. "
                                  "Finish it, or wait for it to end, "
                                  "before starting another.")
        return
    if not view.submit(url):
        handler._workflow_page(error="Too many messages in the last "
                                  "minute; give the bot a moment to "
                                  "catch up.")
        return
    handler._redirect("/workflow", headers=handler._chat_cookie_headers())

def workflow_choice_post(handler, form):
    if not handler._web_chat_enabled():
        handler._workflow_page()
        return
    clarify_id = (form.get("clarify_id") or [""])[0]
    try:
        index = int((form.get("index") or ["-1"])[0])
    except ValueError:
        index = -1
    if clarify_id and index >= 0:
        # The chat page's press rules, verbatim: a timed-out or
        # refused press comes back as the page with an explanation,
        # never a silent redirect that looks like success.
        try:
            label = handler._chat_view().answer(clarify_id, index)
        except Exception:
            log.warning("workflow page answer for %s did not reach the bot",
                        clarify_id, exc_info=True)
            handler._workflow_page(error="That answer didn't reach the "
                                      "bot in time. The question is "
                                      "still open, try again.")
            return
        if label is None:
            handler._workflow_page(error="That answer wasn't accepted, "
                                      "the question may have closed, "
                                      "or the answer belongs to "
                                      "another step.")
            return
    handler._redirect("/workflow")

def workflow_start_post(handler, form):
    """POST /workflow/start: begin the workflow for one inbox file.

    The name is checked against the inbox listing the Upload page
    shows (upload.list_inbox), so only a file that is really in the
    inbox can be named; the file stays in the inbox and is submitted
    to the intake as the upload it is."""
    if not handler._web_chat_enabled():
        handler._workflow_page()
        return
    name = (form.get("name") or [""])[0]
    data = handler.server.settings_data
    if name not in {row[0] for row in upload_module.list_inbox(data)}:
        handler._workflow_page(error="That file is no longer in the "
                                  "inbox, so no workflow was started.")
        return
    view = handler._chat_view()
    if handler._workflow_busy(view):
        handler._workflow_page(error="A workflow is already running. "
                                  "Finish it, or wait for it to end, "
                                  "before starting another.")
        return
    path = upload_module.inbox_dir(data) / name
    if not view.submit("", media_urls=[str(path)]):
        handler._workflow_page(error="Too many messages in the last "
                                  "minute; give the bot a moment to "
                                  "catch up.")
        return
    handler._redirect("/workflow", headers=handler._chat_cookie_headers())

def workflow_upload_post(handler, form):
    """POST /workflow/upload: receive a file and start its workflow.

    The receive is the Upload page's own (_receive_upload: streamed
    to a spool file, size and quota checked before a byte lands);
    the save is save_upload into the inbox; then the saved file is
    submitted to the intake at once, which is the difference from
    the Upload page: there the file waits for a chat pickup."""
    if not handler._web_chat_enabled():
        handler._workflow_page()
        return
    view = handler._chat_view()
    if handler._workflow_busy(view):
        handler._workflow_page(error="A workflow is already running. "
                                  "Finish it, or wait for it to end, "
                                  "before starting another.")
        return

    def handle(fields, files):
        if not files:
            handler._html_refusal(400, "no file was attached", "/workflow")
            return
        saved, reason = upload_module.save_upload(
            handler.server.settings_data, files[0][0], files[0][1])
        if saved is None:
            handler._html_refusal(507 if reason.startswith(upload_module.INBOX_FULL)
                               else 400, reason, "/workflow")
            return
        if not view.submit("", media_urls=[str(saved)]):
            handler._workflow_page(
                notice=f"{saved.name} is in the inbox, but the "
                       "workflow could not be started (too many "
                       "messages in the last minute). Start it from "
                       "the Upload page in a moment.")
            return
        handler._redirect("/workflow",
                       headers=handler._chat_cookie_headers())

    refused = handler._receive_upload(handle)
    if refused:
        headers = [("Retry-After", "30")] if refused[0] == 503 else []
        handler._send(refused[0], shell.page(
            "error", f'<p class="error">{html.escape(refused[1])}</p>'
                     '<p><a href="/workflow">Back</a></p>',
            path="/workflow", theme=handler._theme()),
            "text/html; charset=utf-8", headers=headers)

def workflow_download_route(handler, rest):
    """GET /workflow/download/<intake id>/<n>: one sliced file.

    ``rest`` is exactly an intake id and a plate position. The file
    is resolved by print_handy.webui.workflow from this session's
    own receipt: only a file that receipt names can be served, and
    an intake id from another session resolves nothing here."""
    segments = [segment for segment in rest.split("/") if segment != ""]
    path = None
    if len(segments) == 2 and handler._web_chat_enabled():
        view = handler._chat_view(issue=False)
        path = workflow_module.download_target(
            handler.server.settings_data, view.scope, segments[0], segments[1])
    if path is None:
        handler._json(404, {"ok": False, "reason": "no such sliced file"})
        return
    handler._send_file(path, workflow_module.content_type(path),
                    headers=[("Content-Disposition",
                              f'attachment; filename="{path.name}"'),
                             ("Cache-Control", "no-store")])

