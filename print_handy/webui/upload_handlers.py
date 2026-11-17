"""Upload handlers for the Web UI (#1603 item 3).

Moved out of ``webui/server.py``: the Upload page, the multipart
receive shared by the Upload/Workflow/API routes, the header-token
API upload and the no-login link upload page/submit. ``_Handler``
keeps thin delegates with the route-table names (and
``_credential_fingerprint`` / ``_link_valid`` helpers other code
uses); size, quota and intake rules are the upload module's own and
are unchanged.
"""
from __future__ import annotations

import html
import json
import logging

from . import data as data_module
from . import shell
from . import upload as upload_module

log = logging.getLogger(__name__)

INVALID_LINK_MESSAGE = "This upload link is not valid. Ask for a fresh one."


def upload_page(handler):
    handler._send(200, handler._upload_page_html(), "text/html; charset=utf-8")

def upload_page_html(handler, notice=None, error=None):
    return upload_module.render_upload_page(
        handler.server.settings_data, form_token=handler._form_token(),
        notice=notice, error=error, theme=handler._theme(),
        nav=data_module.nav_for(handler.server.settings_data),
        fingerprint=handler._credential_fingerprint())

def receive_upload(handler, handle):
    """Receive one multipart upload body and hand it to ``handle``.

    The body is streamed to a spool file in the inbox, never read
    into memory whole (upload.parse_multipart_stream). Before a
    byte is read: at most MAX_CONCURRENT_UPLOADS uploads are
    received at once (503 with Retry-After otherwise), the
    Content-Length must fit the size cap (413), and the inbox
    quota must have room for it (507 with the plain reason).
    ``handle(fields, files)`` gets the parsed text fields and
    ``[(filename, spool path)]``; whatever spool file it does not
    move into the inbox is removed afterwards. Returns None, or
    ``(code, reason)`` when the body was refused. A refused body is
    not read; the connection closes after the answer, with a
    bounded drain of the unread rest first (_refuse_body), so a
    browser shows the answer rather than a reset."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        handler.close_connection = True  # no length to drain by
        return 400, "bad Content-Length"
    content_type = handler.headers.get("Content-Type") or ""
    if not content_type.startswith("multipart/form-data"):
        handler._refuse_body(length)
        return 415, "uploads must be multipart/form-data"
    if length < 0 or length > (upload_module.MAX_UPLOAD_BYTES
                               + upload_module.MULTIPART_OVERHEAD_BYTES):
        handler._refuse_body(length)
        return 413, "upload too large"
    data = handler.server.settings_data
    # The whole Content-Length counts toward the pre-read check: the
    # multipart framing is usually far smaller than its allowance, so
    # subtracting the allowance would let a file that cannot fit be
    # received before the save-time check refused it.
    upload_module.prune_stale_spool(data)
    full = upload_module.quota_refusal(data, max(0, length))
    if full is not None:
        handler._refuse_body(length)
        return 507, full
    if not upload_module.UPLOAD_SLOTS.try_acquire():
        handler._refuse_body(length)
        return 503, upload_module.BUSY_MESSAGE
    files = []
    from .server import _CountingReader
    body = _CountingReader(handler.rfile)
    try:
        try:
            fields, files = upload_module.parse_multipart_stream(
                body, length, content_type, upload_module.spool_dir(data))
        except upload_module.UploadError as exc:
            handler._refuse_body(length - body.count)
            return exc.status, str(exc)
        except OSError:
            from .server import _request_id
            rid = _request_id()
            log.exception("webui: could not receive an upload (request id %s)", rid)
            handler._refuse_body(length - body.count)
            return 500, ("the upload could not be received; nothing was saved "
                         f"(request id {rid})")
        fields = {name: [value] for name, value in fields.items()}
        if handler._policy_recheck(fields):
            handle(fields, files)
        return None
    finally:
        upload_module.discard_spooled(files)
        upload_module.UPLOAD_SLOTS.release()

def upload_post(handler, form):
    """POST /upload: a multipart file (``form`` is None; the policy
    gated it before the body and checks its form token once the
    fields are parsed) or a link and inbox action form."""
    if form is None:
        def handle(fields, files):
            if not files:
                handler._html_refusal(400, "no file was attached", "/upload")
                return
            saved, reason = upload_module.save_upload(
                handler.server.settings_data, files[0][0], files[0][1])
            if saved is None:
                handler._html_refusal(507 if reason.startswith(upload_module.INBOX_FULL)
                                   else 400, reason, "/upload")
                return
            handler._send(200, handler._upload_page_html(
                notice=f"{saved.name} is in the inbox. Pick it up in chat "
                       "to slice and print it."), "text/html; charset=utf-8")

        refused = handler._receive_upload(handle)
        if refused:
            headers = [("Retry-After", "30")] if refused[0] == 503 else []
            handler._send(refused[0], shell.page(
                "error", f'<p class="error">{html.escape(refused[1])}</p>'
                         '<p><a href="/upload">Back</a></p>',
                path="/upload", theme=handler._theme()),
                "text/html; charset=utf-8", headers=headers)
        return
    action = (form.get("action") or [""])[0]
    notice = error_text = None
    data = handler.server.settings_data
    if action == "create_link":
        token = upload_module.create_link(data, handler._credential_fingerprint())
        days = upload_module.LINK_TTL_S // 86400
        notice = (f"Upload link created: /u/{token} — copy it now; "
                  "only a digest of it is kept, so it cannot be shown again. "
                  f"It works for {days} days, or until you revoke it or the "
                  "Web UI login changes.")
    elif action == "revoke_link":
        if upload_module.revoke_link(data, (form.get("digest") or [""])[0]):
            notice = "Upload link revoked."
        else:
            error_text = "that upload link is already gone"
    elif action == "delete_file":
        name = (form.get("name") or [""])[0]
        if upload_module.delete_inbox_file(data, name):
            notice = f"{name} was deleted from the inbox."
        else:
            error_text = "that file is no longer in the inbox"
    else:
        error_text = "unknown upload action"
    handler._send(200, handler._upload_page_html(notice=notice, error=error_text),
               "text/html; charset=utf-8")

def api_upload_post(handler):
    """POST /api/upload (#1548): the header-token upload.

    Policy ``header`` (routes.py): the access-token header only
    (or no token configured); a login session never authorizes
    it, and there is no form token to check — the header is the
    whole credential, which is the point for the Home Assistant
    integration. The receive is the upload page's own
    (_receive_upload: streamed to a spool file, the shared size
    budget, the STL intake limit and the inbox quota checked
    before a byte lands, the same filename cleaning in
    save_upload), so the file lands in the same inbox under the
    same rules and stays an inbox file, never a print. Answers
    are the API JSON shape: ``{ok: true, filename}`` on success,
    ``{ok: false, reason}`` with the refusal's own code (400 for
    a refused file, 413 too large, 503 busy, 507 inbox full).
    """

    def handle(fields, files):
        if not files:
            handler._json(400, {"ok": False, "reason": "no file was attached"})
            return
        saved, reason = upload_module.save_upload(
            handler.server.settings_data, files[0][0], files[0][1])
        if saved is None:
            handler._json(507 if reason.startswith(upload_module.INBOX_FULL)
                       else 400, {"ok": False, "reason": reason})
            return
        handler._json(200, {"ok": True, "filename": saved.name})

    refused = handler._receive_upload(handle)
    if refused:
        code, reason = refused
        payload = {"ok": False, "reason": reason}
        if code == 503:
            handler._send(code, json.dumps(payload, indent=1, sort_keys=True),
                       "application/json", headers=[("Retry-After", "30")])
        else:
            handler._json(code, payload)

def token_upload_page(handler, _token):
    # The link-token policy (routes.py) already checked the token.
    handler._send(200, upload_module.render_token_page(),
               "text/html; charset=utf-8")

def token_upload_submit(handler, token):
    # The link-token policy (routes.py) checked the token before the
    # body and checks it again once the body is received; the
    # guarantee is in save_upload_for_link, which makes the final
    # validity decision and the write one atomic step against
    # revocation.
    invalid = INVALID_LINK_MESSAGE

    def handle(fields, files):
        if not files:
            handler._send(400, upload_module.render_token_page(
                error="No file was attached."), "text/html; charset=utf-8")
            return
        saved, reason = upload_module.save_upload_for_link(
            handler.server.settings_data, token, files[0][0], files[0][1],
            handler._credential_fingerprint())
        if saved is None and reason is None:
            # Revoked after the re-check above but before the write;
            # the atomic check-and-save refused the delivery.
            handler._send(404, upload_module.render_token_page(error=invalid),
                       "text/html; charset=utf-8")
            return
        if saved is None:
            handler._send(507 if reason.startswith(upload_module.INBOX_FULL) else 400,
                       upload_module.render_token_page(error=reason),
                       "text/html; charset=utf-8")
            return
        handler._send(200, upload_module.render_token_page(
            notice=f"{saved.name} is in the inbox. It will be picked up "
                   "and confirmed there before anything prints."),
            "text/html; charset=utf-8")

    refused = handler._receive_upload(handle)
    if refused:
        headers = [("Retry-After", "30")] if refused[0] == 503 else []
        handler._send(refused[0], upload_module.render_token_page(error=refused[1]),
                   "text/html; charset=utf-8", headers=headers)

