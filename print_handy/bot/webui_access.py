"""Chat-side Web UI access: the credential route from a chat platform.

The Web UI's access writers already exist and are the single store:
``print_handy.webui.access.save_password`` / ``save_open`` write
``webui_access.json`` in the state folder, and deleting that file is
the documented reset. This module is the bot-side core any chat
platform calls to perform the same three deliberate actions
(catduck's Q1 ruling: full equivalents from chat):

- **set a password** (:func:`set_password`),
- **choose leave-open** (:func:`leave_open`) — an explicit saved
  record (Q2), the "I mean it" choice that dismisses the claim banner,
- **clear an existing claim** (:func:`clear_claim`) — removes the
  saved claim only. It does not and cannot clear the environment
  token: the Docker operator stays authoritative (Q4). A saved
  password coexists with the token as a co-credential, and a saved
  "open" never overrides a token; this module's confirmation copy
  says so whenever a token is present.

Authorization is not this module's job and it invents none: callers
(the guided setup dispatcher) admit a user only through the existing
setup authorization — ``Gateway._is_user_authorized_for_source`` /
the guided setup gate — before any function here is reached.

Secrets discipline is the design center. A Web UI password has to
arrive as a chat message to be set from chat, so:

- the caller must delete the password message immediately after
  capture, using the same secret-message machinery the pasted-token
  flow uses (:func:`deletion_supported` tells the caller whether the
  platform adapter can; the flow must say so *before* accepting a
  value when it cannot — see :func:`pre_capture_notice`);
- the password value is passed straight to the writer and never
  returned, logged, or placed in a result message;
- callers must never write it to the guided session store
  (``guided-setup.json``) — the guided integration keeps it in memory
  only, and its tests assert the store never contains it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..webui import access as access_module

ACTIONS = ("set_password", "leave_open", "clear_claim")


@dataclass(frozen=True)
class AccessResult:
    """A platform-neutral outcome: what happened, and the copy to show.

    ``message`` is safe to post in chat: it never contains the password
    or any other secret value.
    """

    ok: bool
    action: str
    message: str
    env_token_present: bool = False


def env_token_present(data, environ=None):
    """True when the Docker/environment access token is set (Q4)."""
    environ = os.environ if environ is None else environ
    try:
        name = (data.get("webui") or {}).get("access_token_env") or "WEBUI_ACCESS_TOKEN"
    except AttributeError:
        name = "WEBUI_ACCESS_TOKEN"
    return bool(environ.get(name))


def _token_note(data, environ):
    if env_token_present(data, environ):
        return (" Note: a Web UI token is set through the Docker variable "
                "(environment), and that token keeps working — the Docker "
                "operator stays in charge of it.")
    return ""


def saved_claim(data):
    """The saved claim's kind: ``"password"``, ``"open"``, or None."""
    try:
        saved = json.loads(access_module.access_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(saved, dict) and saved.get("mode") in (access_module.PASSWORD,
                                                         access_module.OPEN):
        return saved["mode"]
    return None


def deletion_supported(adapter):
    """True when this platform adapter can delete a chat message.

    Every shipped platform adapter (Discord, Telegram, Matrix)
    overrides ``delete_message``; the base adapter deletes nothing and
    reports False. The check is the override, not a platform name, so a
    future adapter answers for itself.
    """
    if adapter is None:
        return False
    try:
        from .platform import BasePlatformAdapter
        return type(adapter).delete_message is not BasePlatformAdapter.delete_message
    except Exception:
        return hasattr(adapter, "delete_message")


def pre_capture_notice(adapter):
    """Copy the flow shows *before* accepting a password value."""
    if deletion_supported(adapter):
        return ("Send the password as your next message. It is deleted from "
                "chat as soon as it is captured, it is never saved in the "
                "setup checklist, and it is never repeated back here.")
    return ("This chat cannot delete messages, so a password you send here "
            "would stay visible in the chat history. If that is not "
            "acceptable, stop here and set the password from the Web UI "
            "walkthrough's Login step instead. To continue anyway, send "
            "the password as your next message; it is never saved in the "
            "setup checklist and never repeated back here.")


def set_password(data, username, password, confirm, environ=None):
    """Set the Web UI login from chat, with the Web UI's own validation.

    Same refusals as the Web UI: ``clean_username`` and
    ``weak_password_reason`` run inside ``access.save_password``, so a
    password the Web UI would refuse is refused here with the same
    reason. The value is never echoed in the result.
    """
    try:
        access_module.save_password(data, password, confirm, username=username)
    except access_module.AccessError as exc:
        return AccessResult(False, "set_password", str(exc),
                            env_token_present(data, environ))
    return AccessResult(
        True, "set_password",
        "Web UI login saved: sign in with that username and password."
        + _token_note(data, environ),
        env_token_present(data, environ))


def leave_open(data, confirmed, environ=None):
    """Record the explicit leave-open choice from chat (Q2)."""
    try:
        access_module.save_open(data, confirmed)
    except access_module.AccessError as exc:
        return AccessResult(False, "leave_open", str(exc),
                            env_token_present(data, environ))
    note = _token_note(data, environ)
    if env_token_present(data, environ):
        note += (" While that token is set it still applies: a saved "
                 "\"open\" choice does not override the Docker token.")
    return AccessResult(
        True, "leave_open",
        "Web UI left open by your saved choice: anyone who can reach it "
        "can use it, and the setup surfaces will stop asking." + note,
        env_token_present(data, environ))


def clear_claim(data, confirmed, environ=None):
    """Remove the saved claim (password or open record) from chat (Q1).

    Only ``webui_access.json`` is removed — the same reset as deleting
    the file by hand on the Web UI side. The environment token is not
    in that file and is never touched; the confirmation copy says
    plainly that a Docker-set token still applies if one is set.
    """
    if not confirmed:
        return AccessResult(
            False, "clear_claim",
            "Not cleared. Confirm the clear to remove the saved Web UI "
            "login/open choice.",
            env_token_present(data, environ))
    existing = saved_claim(data)
    try:
        access_module.access_path(data).unlink(missing_ok=True)
    except OSError as exc:
        return AccessResult(False, "clear_claim",
                            f"The saved claim could not be removed: {exc}",
                            env_token_present(data, environ))
    what = ("The saved Web UI claim was removed."
            if existing else
            "There was no saved Web UI claim to remove.")
    return AccessResult(
        True, "clear_claim",
        what + " This removes only the claim saved through the Web UI or "
        "chat; it cannot clear a token set through the Docker variable — "
        "if one is set, that token still applies."
        + ("" if env_token_present(data, environ) else
           " No environment token is set on this install."),
        env_token_present(data, environ))


__all__ = [
    "ACTIONS", "AccessResult", "clear_claim", "deletion_supported",
    "env_token_present", "leave_open", "pre_capture_notice", "saved_claim",
    "set_password",
]
