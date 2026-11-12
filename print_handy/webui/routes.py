"""The Web UI route table: every route and its one declared policy (#1319).

The server (``server._Handler``) answers a request only through
:func:`resolve` and the policy middleware (``_Handler._policy``); a
route handler never checks a credential itself. Each route declares
exactly one policy:

``public``
    No credential. The health probe, the login form and its POST,
    logout (it ends only the caller's own session), the ``/setup``
    redirect, and the read-only refusal for unsupported methods.
``session``
    The read rule: access is open, or the ``X-WebUI-Token`` header
    matches, or a live login session. A browser navigation without it
    goes to ``/login``; anything else gets the JSON 401 (429 while the
    address is locked out).
``session+csrf``
    The form-write rule. While access is open, the per-process form
    token from the page. On a claimed install, the header, or a live
    session AND the form token bound to that session (a route marked
    ``process_token`` also accepts the per-process token beside the
    session). The form is read before the check, so a malformed body
    keeps its 413/415/400 answer; a refusal is the themed 403 page with
    a Back link to ``back``. A multipart upload is gated on the read
    rule before its body is read and checked again once its fields are
    parsed.
``header``
    The JSON API write rule: access is open, or the header matches. A
    login session never authorizes these, and they take JSON bodies
    only, so a cross-site HTML form cannot reach them.
``link-token``
    A no-login upload link: the token in the address is the whole
    credential and opens that form only. Checked before the body is
    read and again after it is received.

Routes are tried in table order and the first match wins. A pattern is
an exact path, or a path with one ``*`` that matches any text (slashes
included) between its prefix and suffix; the ``*`` text, URL-decoded
when ``decode``, is the handler's argument. Every method ends with a
catch-all row, so no request reaches a handler undeclared.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

PUBLIC = "public"
SESSION = "session"
SESSION_CSRF = "session+csrf"
HEADER = "header"
LINK_TOKEN = "link-token"
POLICIES = (PUBLIC, SESSION, SESSION_CSRF, HEADER, LINK_TOKEN)

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class Route:
    """One declared route.

    ``handler`` names the ``_Handler`` method that answers it. It is
    called with the ``*`` capture when the pattern has one, then the
    request path when ``pass_path``, then the parsed form for a
    ``session+csrf`` route (None for a multipart upload).

    ``back``: the Back link of a refused ``session+csrf`` form;
    ``{arg}`` is the URL-quoted capture.
    ``prechecks``: run the access re-sync, the broken-login-file 503
    and the host (DNS-rebinding) check first. Only the read-only
    refusal for the other methods skips them, as it always has.
    ``host_check``: the host check; off only for the liveness probe.
    ``claim_lock``: authorize and answer inside the claim lock with the
    access re-read, so two racing first claims cannot both win.
    ``process_token``: also accept the per-process form token beside a
    live session on a claimed install (see the module docstring).
    ``multipart``: a multipart body is gated before it is read.
    """

    method: str
    pattern: str
    policy: str
    handler: str
    back: str = "/"
    pass_path: bool = False
    decode: bool = True
    prechecks: bool = True
    host_check: bool = True
    claim_lock: bool = False
    process_token: bool = False
    multipart: bool = False

    def match(self, path) -> Tuple[bool, Optional[str]]:
        """(matched, capture): capture is None for an exact pattern."""
        if "*" not in self.pattern:
            return path == self.pattern, None
        prefix, suffix = self.pattern.split("*", 1)
        if not (path.startswith(prefix) and path.endswith(suffix)):
            return False, None
        return True, path[len(prefix):len(path) - len(suffix)]


def _get(pattern, policy, handler, **kw):
    return Route("GET", pattern, policy, handler, **kw)


def _post(pattern, policy, handler, **kw):
    return Route("POST", pattern, policy, handler, **kw)


ROUTES = (
    # -- GET ---------------------------------------------------------------
    # Unauthenticated by design: the container healthcheck probes it, and
    # liveness must not depend on the token or the Host header.
    _get("/healthz", PUBLIC, "_healthz", host_check=False),
    _get("/setup", PUBLIC, "_setup_redirect"),
    _get("/login", PUBLIC, "_login_page"),
    _get("/u/*", LINK_TOKEN, "_token_upload_page"),
    _get("/theme", SESSION, "_theme_route"),
    _get("/walkthrough", SESSION, "_walkthrough_page"),
    _get("/", SESSION, "_status_page"),
    _get("/api/status", SESSION, "_api_status"),
    _get("/data", SESSION, "_data_route", pass_path=True),
    _get("/wrapped", SESSION, "_data_route", pass_path=True),
    _get("/api/data", SESSION, "_data_route", pass_path=True),
    _get("/api/wrapped", SESSION, "_data_route", pass_path=True),
    _get("/wrapped/video-file", SESSION, "_wrapped_video_file_route"),
    _get("/api/wrapped/video-status", SESSION, "_wrapped_video_status_route"),
    _get("/readyz", SESSION, "_readyz"),
    # /status, /spools, /camera/snapshot and /screen/snapshot per printer.
    _get("/api/printers/*", SESSION, "_printer_status_route", decode=False),
    _get("/spools", SESSION, "_spools_index"),
    _get("/printers", SESSION, "_printers_page"),
    _get("/printers/*/screen", SESSION, "_screen_page"),
    _get("/spools/*", SESSION, "_spools_page"),
    _get("/config", SESSION, "_config_page"),
    _get("/chat", SESSION, "_chat_page"),
    _get("/chat/state", SESSION, "_chat_state"),
    _get("/upload", SESSION, "_upload_page"),
    _get("/sources", SESSION, "_sources_page"),
    _get("/designs", SESSION, "_designs_page"),
    _get("/designs/*", SESSION, "_design_route", decode=False),
    _get("/projects", SESSION, "_projects_route", pass_path=True),
    _get("/api/projects", SESSION, "_projects_route", pass_path=True),
    _get("/layers", SESSION, "_layers_route"),
    _get("/layers/*", SESSION, "_layers_route", decode=False),
    # Anything else: the themed 404 for a browser, JSON otherwise, behind
    # the read rule like every page.
    _get("*", SESSION, "_not_found", decode=False),
    # -- POST --------------------------------------------------------------
    _post("/u/*", LINK_TOKEN, "_token_upload_submit"),
    _post("/login", PUBLIC, "_login_submit"),
    _post("/chat", SESSION_CSRF, "_chat_post", back="/chat"),
    _post("/chat/choice", SESSION_CSRF, "_chat_choice_post", back="/chat"),
    _post("/upload", SESSION_CSRF, "_upload_post", back="/upload", multipart=True),
    # Ends only the caller's own session: the worst a cross-origin post
    # can do is sign the operator out.
    _post("/logout", PUBLIC, "_logout"),
    _post("/walkthrough", SESSION_CSRF, "_walkthrough_post", back="/walkthrough",
          claim_lock=True),
    # The Wrapped page embeds the per-process form token, so on a claimed
    # install these accept it beside a live session (as before #1319).
    _post("/wrapped/video", SESSION_CSRF, "_wrapped_video_post", back="/wrapped",
          process_token=True),
    _post("/wrapped/share", SESSION_CSRF, "_wrapped_share_post", back="/wrapped",
          process_token=True),
    _post("/config/preview", SESSION_CSRF, "_config_preview", back="/config"),
    _post("/config", SESSION_CSRF, "_config_save", back="/config"),
    _post("/debug", SESSION_CSRF, "_debug_post", back="/"),
    _post("/printers", SESSION_CSRF, "_printers_post", back="/printers"),
    _post("/spools/*", SESSION_CSRF, "_spools_page_post", back="/spools/{arg}"),
    # Printer controls and spool actions: JSON, the header only.
    _post("/api/printers/*", HEADER, "_printer_post_route", decode=False),
    _post("*", PUBLIC, "_read_only", decode=False),
    # -- every other method: the read-only refusal, as before ---------------
    *(Route(method, "*", PUBLIC, "_read_only", decode=False, prechecks=False)
      for method in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")),
)


def resolve(method, path):
    """(route, capture) for a request: the first row that matches.

    Every method in METHODS ends with a catch-all row, so this never
    returns None for them."""
    for route in ROUTES:
        if route.method != method:
            continue
        matched, capture = route.match(path)
        if matched:
            return route, capture
    return None, None
