"""First-run walkthrough and access-sync handlers (#1603 item 3).

Moved out of ``webui/server.py``: the Walkthrough page/post and its
login-change guard, plus the per-request access re-sync that applies
a login saved by another process. ``_Handler`` keeps thin delegates
with the same names (``_sync_access`` is called by dispatch/policy,
the rest by the route table); behavior and output are unchanged.
"""
from __future__ import annotations

import logging

from .. import settings
from . import access as access_module
from . import pages
from . import walkthrough as walkthrough_module
from .sessions import _access_stamp

log = logging.getLogger(__name__)


def sync_access(handler):
    """Apply a login another process saved (chat setup, the CLI wizard).

    The bot and the Web UI are separate processes, so a login set from
    chat is only visible here as a change to the saved file. Old
    sessions and form tokens must not outlive the credential they
    were issued under."""
    server = handler.server
    if not hasattr(server, "access_stamp"):
        return
    # A broken login file is re-read on every request: fixing its
    # ownership or mode changes no mtime, and the fix must apply
    # without a restart.
    broken = getattr(server.access, "broken", False)
    if not broken and _access_stamp(server.settings_data) == server.access_stamp:
        return
    with server.access_sync_lock:
        stamp = _access_stamp(server.settings_data)
        if stamp == server.access_stamp and not server.access.broken:
            return
        server.access_stamp = stamp
        new_access = access_module.resolve(
            server.settings_data, server.bind_host, server.webui_environ)
        if new_access.credential_fingerprint() == \
                server.access.credential_fingerprint():
            # The stamp is only the file's mtime: a rewrite carrying
            # the credential this server already enforces (a byte-for-
            # byte re-save, or a login this process resolved from the
            # file itself) changed no rule, so nothing issued under it
            # is stale — sessions and the form token stay valid.
            server.access = new_access
            return
        server.access = new_access
        server.sessions.clear()
        handler._rotate_form_token()
        handler._revoke_remembered_sessions()
        handler._revoke_upload_links()

def walkthrough_data(handler):
    """The current effective settings for a walkthrough render or
    POST: reloaded rather than the server's startup snapshot, so a
    step that just saved a new printer model renders the new model's
    fields when the operator comes Back to it — field visibility is
    model-driven, and the startup data still names the old model."""
    return settings.load(environ=handler.server.webui_environ)

def walkthrough_page(handler):
    """GET /walkthrough: the current step, or / when this install
    has no walkthrough pending (finished or skipped). Every install
    owes it until then, however its access was resolved. An unclaimed
    install is the exception: the status page's "No login is set"
    banner links here, so a closed walkthrough reopens at Login
    instead of bouncing the banner's link back to the status page."""
    data = handler._walkthrough_data()
    if not walkthrough_module.pending(data):
        if not handler.server.access.unclaimed:
            handler._redirect("/")
            return
        walkthrough_module.reopen_at_login(data)
    handler._send(200, walkthrough_module.render_page(
        data, handler._field_values(),
        form_token=handler._form_token(), theme=handler._theme(),
        environ=handler.server.webui_environ,
        loopback=_is_loopback(handler.server.bind_host)), "text/html; charset=utf-8")

def walkthrough_post(handler, form):
    """POST /walkthrough: one step's action, authorized exactly like
    a config POST. The policy (routes.py, ``claim_lock``) authorizes
    it inside the claim lock and holds the lock until this returns,
    so two browsers racing to claim an unclaimed install cannot both
    win."""
    if handler._login_change_refused(form):
        return
    handler._walkthrough_apply(form)

def login_change_refused(handler, form):
    """True (the step re-rendered with the reason) when a claimed
    install's login would change without the current credential.

    Changing or removing the password needs the password (or the
    access token) in force now, typed into the Login step: a signed-
    in session that was left open, or a stolen page, is not enough to
    lock the owner out. Wrong entries count toward the login form's
    per-address backoff. An open install has no current credential,
    so nothing is asked there (the unclaimed design)."""
    access = handler.server.access
    data = handler._walkthrough_data()
    if not access.required or not walkthrough_module.login_change_requested(data, form):
        return False
    address = handler._client_ip()
    throttle = handler.server.login_throttle
    wait = throttle.retry_after(address)
    current = (form.get(walkthrough_module.CURRENT_PASSWORD_FIELD) or [""])[0]
    if not wait and current and access.matches(current):
        throttle.record_success(address)
        return False
    if wait:
        message = f"Too many failed attempts. Try again in {wait} seconds."
    elif current:
        throttle.record_failure(address)
        message = "The current password was not accepted, so the login was not changed."
    else:
        message = "Enter the current password to change or remove the login."
    handler._send(403, walkthrough_module.render_page(
        data, handler._field_values(), form_token=handler._form_token(), theme=handler._theme(),
        environ=handler.server.webui_environ,
        submitted={"username": (form.get(pages.SETUP_USER_FIELD) or [""])[0]},
        general_error=message, step_index=walkthrough_module._LOGIN_INDEX,
        loopback=_is_loopback(handler.server.bind_host)), "text/html; charset=utf-8")
    return True

def walkthrough_apply(handler, form):
    kind, payload = walkthrough_module.apply_post(
        handler._walkthrough_data(), handler.server.webui_environ, form,
        handler._field_values(), form_token=handler._form_token(),
        theme=handler._theme(), loopback=_is_loopback(handler.server.bind_host))
    if kind == "access":
        # The Login step saved a new rule: apply it now, and keep the
        # person who chose it signed in on this browser. A password
        # saved under require_auth = false turned that setting back on
        # in the settings file; tightening applies at once (loosening
        # still waits for a restart), so the password is enforced now.
        webui = handler.server.settings_data.get("webui")
        if isinstance(webui, dict) and not webui.get("require_auth", True) and \
                handler._walkthrough_data()["webui"].get("require_auth", True):
            webui["require_auth"] = True
        handler.server.access = access_module.resolve(
            handler.server.settings_data, handler.server.bind_host, handler.server.webui_environ)
        handler.server.access_stamp = _access_stamp(handler.server.settings_data)
        # Every session issued under the old rule ends — an intruder's
        # live session must not survive the owner changing the
        # password. The browser that made the change gets a fresh one.
        handler.server.sessions.clear()
        handler._rotate_form_token()
        handler._revoke_remembered_sessions()
        handler._revoke_upload_links()
        handler._redirect(payload, headers=[handler._start_session()]
                       if handler.server.access.required else [])
    elif kind == "redirect":
        handler._redirect(payload)
    else:
        handler._send(200, payload, "text/html; charset=utf-8")


def _is_loopback(host):
    from .server import is_loopback
    return is_loopback(host)
