"""Camera snapshot URLs that carry credentials (``http://user:pass@cam/snap``).

urllib does not send the userinfo of a URL: it hands ``user:pass@cam`` to
the connection as the host, so a camera that needs its credentials always
failed, and the failure text (a DNS error, or "nonnumeric port: 'pass@cam'")
could carry the password into the listener log, where the URL redaction
pattern does not recognize it any more. Every HTTP camera fetch builds its
request here instead: the userinfo is removed from the URL and sent as an
HTTP Basic ``Authorization`` header, so the password never reaches urllib's
host handling, its error messages, or a log line. ``live_stream.redact_url``
stays the way to show such a URL to a person.
"""
from __future__ import annotations

import base64
import urllib.request
from urllib.parse import unquote, urlsplit, urlunsplit


def split_credentials(url):
    """``(url without userinfo, Basic Authorization header value or None)``.

    A URL without credentials comes back unchanged with None. Percent-encoded
    user names and passwords (``p%40ss`` for ``p@ss``) are decoded first,
    the way a browser sends them.
    """
    url = str(url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, None
    if "@" not in parts.netloc:
        return url, None
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    clean = urlunsplit((parts.scheme, hostport, parts.path, parts.query, parts.fragment))
    token = base64.b64encode(f"{unquote(user)}:{unquote(password)}".encode()).decode()
    return clean, f"Basic {token}"


def request(url, headers=None):
    """A ``urllib.request.Request`` for a camera URL, its credentials sent as Basic auth.

    The header is unredirected: a camera that redirects elsewhere does not
    get the credentials forwarded to the new location.
    """
    clean, auth = split_credentials(url)
    req = urllib.request.Request(clean, headers=dict(headers or {}))
    if auth:
        req.add_unredirected_header("Authorization", auth)
    return req
