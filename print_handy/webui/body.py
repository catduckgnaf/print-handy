"""Request body reading for the Web UI (#1590 item 8).

Moved out of ``webui/server.py``'s ``_Handler``: the route table
(``webui/routes.py``) is the seam, and a handler now asks this module
to read and parse its body instead of reaching into ``rfile`` itself.
The rules are exactly the ones the handler enforced inline:

* a form body must be urlencoded, carry a sane Content-Length and fit
  64 KiB; every refusal is reported as ``(code, reason)`` and the caller
  drains the unread body (``_refuse_unread_body``) so the bytes are
  never parsed as the next request;
* a JSON body must be ``application/json``, fit the route's limit and
  decode to an object; the wording of each refusal is the route's own
  (``noun`` and the trailing sentence), because a control POST and a
  spool POST have always answered with different text.

Sessions are the other half of item 8: they live in
``webui/sessions.py`` (#1603 item 3), next to the login, logout and
session-cookie handlers.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

FORM_LIMIT_BYTES = 65536


def content_length(headers) -> int:
    """The request's Content-Length, or -1 when it is not a number."""
    try:
        return int(headers.get("Content-Length") or 0)
    except ValueError:
        return -1


def read_form(handler):
    """(form, None) or (None, (code, reason)) for a urlencoded POST body.

    ``handler`` is the request handler: its headers and ``rfile`` are
    read, and a refusal calls its ``_refuse_unread_body`` before
    returning, exactly as the inline ``_Handler._read_form`` did.
    """
    content_type = handler.headers.get("Content-Type") or ""
    if content_type and not content_type.startswith("application/x-www-form-urlencoded"):
        handler._refuse_unread_body()
        return None, (415, "POST bodies must be application/x-www-form-urlencoded")
    length = content_length(handler.headers)
    if length < 0:
        handler._refuse_unread_body()
        return None, (400, "bad Content-Length")
    if length > FORM_LIMIT_BYTES:
        handler._refuse_unread_body()
        return None, (413, "form body too large")
    raw = handler.rfile.read(length) if length else b""
    return parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True), None


def read_json(handler, *, limit, noun, tail):
    """(body, None) or (None, (code, reason)) for a JSON POST body.

    ``limit`` is the route's size cap in bytes, ``noun`` its name for
    the request ("control" / "spool") and ``tail`` the sentence its
    refusals end with ("Nothing sent." / "Nothing changed."). An
    oversized or malformed length refuses the unread body through the
    handler before answering, as the inline route code did.
    """
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    length = content_length(handler.headers)
    if length < 0 or length > limit:
        handler._refuse_unread_body()
        return None, (400, f"bad or oversized body. {tail}")
    raw = handler.rfile.read(length) if length else b""
    if content_type != "application/json":
        return None, (415, f"{noun} POSTs must be application/json. {tail}")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return None, (400, f"body is not valid JSON. {tail}")
    if not isinstance(body, dict):
        return None, (400, f"body must be a JSON object. {tail}")
    return body, None
