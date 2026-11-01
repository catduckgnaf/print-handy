"""Model-site credentials (#462, #464): where each one lives and how to connect it.

Every model site that needs a credential is listed in ``LOGIN_SITES``:
MakerWorld, Thingiverse and Cults3D use a token or key the operator creates
on the site; Creality Cloud, Thangs and Nexprint use the login token the
operator's signed-in browser already holds (a cookie value); MyMiniFactory
uses its documented OAuth sign-in. None of these can be logged in to from a
script (login pages sit behind robot checks, and Print Handy never stores a
password or solves a captcha), so "connecting an account" always means the
operator hands Print Handy a value from their own account: saved in a token
file on the Print Handy machine (or the site's environment variable), or
pasted into the chat flow (see prepared_intake), which says up front what
that risks, checks the value's shape, writes it with owner-only permissions
(``save_token``) and tries to delete the message. The value is never logged,
echoed, or put in a receipt or an error text. The chat flow shows the whole
walkthrough in its first message and retries the download once the value is
in place.

``SiteLoginNeeded`` is what every provider raises when a request needs the
credential and it is missing, empty, expired or refused; the intake turns it
into the "Connect account" (or "Renew login") offer instead of a dead end.
``source_report`` is the ``/print-source`` answer: every site, what it
needs, and whether that is in place, without ever showing a credential.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

DOCS = "https://github.com/catduckgnaf/print-handy/blob/main/docs/PROJECT-REFERENCE.md"


@dataclass(frozen=True)
class LoginSite:
    site: str
    label: str
    folder: str
    sign_in_url: str
    where: str
    doc_section: str
    token_file: str = "token"
    env: str = ""
    # Site-specific walkthrough steps; empty means the browser-cookie steps.
    steps: tuple[str, ...] = ()
    intro: str = ""
    # Cookie names that hold the value when the operator pastes a cookie
    # string (``name=value; name2=value2``); one name, or for Creality the
    # user id then the token.
    cookies: tuple[str, ...] = ()
    # Plain-words shape of the value, for the "doesn't look like" re-ask.
    shape: str = "a single token with no spaces"

    @property
    def env_name(self):
        """The environment variable that can hold the credential instead of the file."""
        return self.env or f"{self.folder.upper()}_TOKEN"

    @property
    def docs_url(self):
        anchor = "".join(c for c in self.doc_section.lower().replace(" ", "-")
                         if c.isalnum() or c == "-")
        return f"{DOCS}#{anchor}"


MMF_AUTHORIZE = ("https://auth.myminifactory.com/web/authorize?client_id={client}"
                 "&redirect_uri={redirect}&response_type=token&state=print-handy")

LOGIN_SITES = {
    "makerworld": LoginSite(
        "makerworld", "MakerWorld", "bambu", "https://makerworld.com",
        "Cookies for https://makerworld.com, the cookie named `token`",
        "MakerWorld account token", env="MAKERWORLD_TOKEN", cookies=("token",),
        shape="the long value of the `token` cookie"),
    "thingiverse": LoginSite(
        "thingiverse", "Thingiverse", "thingiverse", "https://www.thingiverse.com",
        "the App Token on your Thingiverse app's page",
        "Thingiverse App Token", env="THINGIVERSE_TOKEN",
        shape="the App Token, 32 letters and digits",
        intro="Thingiverse links use Thingiverse's official API with a free App Token from "
              "your own account.",
        steps=("Sign in at https://www.thingiverse.com, then open "
               "https://www.thingiverse.com/developers and choose **Create an App**, then **Web App**.",
               "Fill in the form: App Name `Print Handy`; any description; App URL and Callback "
               "URL `https://github.com/catduckgnaf/print-handy`. Agree and create it (it can stay "
               "private and unpublished).",
               "On the app's page, copy the **App Token** (32 letters and digits). Not the Client "
               "Secret.")),
    "cults3d": LoginSite(
        "cults3d", "Cults3D", "cults3d", "https://cults3d.com",
        "your Cults API key page", "Cults3D API key", env="CULTS3D_TOKEN",
        shape="`nickname:key`, your Cults nickname, a colon, then the API key",
        intro="Cults3D links use Cults' official API with an API key from your own account. "
              "(Cults' API shows a model's listing only, never its files.)",
        steps=("Sign in at https://cults3d.com and open https://cults3d.com/en/api/keys.",
               "Create a key and copy it.",
               "Write it as `nickname:key`: your Cults nickname exactly as on your profile, "
               "capitals included, a colon, then the key.")),
    "crealitycloud": LoginSite(
        "crealitycloud", "Creality Cloud", "crealitycloud", "https://www.crealitycloud.com",
        "Cookies for https://www.crealitycloud.com, TWO cookies: `model_user_id` (a number) and "
        "`model_token`. Join them as `model_user_id:model_token`, for example "
        "`1234567890:4f2a…` (pasting both as `model_user_id=...; model_token=...` works too)",
        "Creality Cloud login token", cookies=("model_user_id", "model_token"),
        shape="`model_user_id:model_token`, the number from the `model_user_id` cookie, a colon, "
              "then the `model_token` value"),
    "thangs": LoginSite(
        "thangs", "Thangs", "thangs", "https://thangs.com",
        "Cookies for https://thangs.com, the cookie named `accessToken`",
        "Thangs login token", cookies=("accessToken",),
        shape="the long value of the `accessToken` cookie"),
    "nexprint": LoginSite(
        "nexprint", "Nexprint", "nexprint", "https://www.nexprint.com",
        "Cookies for https://www.nexprint.com, the cookie named `auth_token` "
        "(or Local Storage, `ACCOUNT_INFO`, its `access_token` value)",
        "Nexprint login token", cookies=("auth_token",),
        shape="the long value of the `auth_token` cookie"),
    # MyMiniFactory's documented OAuth sign-in (#464): the operator registers
    # their own app once and signs in through MyMiniFactory's own page; the
    # access token it returns in the redirect URL is the saved login. Its
    # folder already holds the inventory API key as `token`, so the sign-in
    # lives beside it as `oauth_token`.
    "myminifactory": LoginSite(
        "myminifactory", "MyMiniFactory", "myminifactory", "https://www.myminifactory.com",
        "the address bar after signing in (the `access_token=` value, or paste the whole URL)",
        "MyMiniFactory sign-in", token_file="oauth_token", env="MYMINIFACTORY_OAUTH_TOKEN",
        intro="MyMiniFactory downloads go through its own sign-in (OAuth).",
        shape="the whole address from the address bar after approving (it holds `access_token=`), "
              "or just the token",
        steps=("One time only: sign in at https://www.myminifactory.com, open Account Settings, "
               "then **Developers**, and register an app: Name `Print Handy`; Client key any unique "
               "slug, for example `print_handy`; Url and Redirect uri any page you control, for "
               "example `https://github.com/catduckgnaf/print-handy`. Save.",
               "Open this link in that browser, with your client key and redirect uri filled in "
               "(the redirect uri URL-encoded):\n"
               f"`{MMF_AUTHORIZE.format(client='<client key>', redirect='<redirect uri>')}`",
               "Approve. The browser lands on your redirect page; copy the **whole address** from "
               "the address bar (it holds `#access_token=...`). The sign-in lasts about a week; "
               "renewing it repeats only this step and the one before.")),
}


class SiteLoginNeeded(RuntimeError):
    """A request needs the operator's credential for ``site``.

    ``reason`` is ``missing``, ``empty``, ``expired`` or ``refused``. The
    message is the plain, complete remedy, so callers that do not offer the
    chat flow (design, the CLI) can show it unchanged.
    """

    def __init__(self, site, reason, message):
        super().__init__(message)
        self.site = site
        self.reason = reason


def token_path(site) -> Path:
    acc = LOGIN_SITES[site]
    return Path.home()/".config"/acc.folder/acc.token_file


def display_path(site) -> str:
    """``site``'s token file as chat shows it: home-relative, never a container path (#712)."""
    acc = LOGIN_SITES[site]
    return f"~/.config/{acc.folder}/{acc.token_file}"


def chat_safe(text) -> str:
    """``text`` with Print Handy's home folder shown as ``~`` (#712).

    In Docker the home folder is ``/data``. Chat never shows container-internal
    paths, so an absolute token path inside a curated message reads home-relative.
    """
    text = str(text)
    home = str(Path.home()).rstrip("/\\")
    if home:
        text = text.replace(home + "/", "~/").replace(home + "\\", "~/")
    return text


def short_cause(exc) -> str:
    """A one-line cause for a receipt's ``error`` field, never the walkthrough (#712)."""
    acc = LOGIN_SITES.get(getattr(exc, "site", None))
    label = acc.label if acc else str(getattr(exc, "site", None) or "site")
    return f"{label} login {getattr(exc, 'reason', None) or 'needed'}"


def _jwt_expiry(token):
    """The ``exp`` claim of a JWT-shaped token, or None when it has none."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return exp if isinstance(exp, (int, float)) else None


def refused(site, detail=""):
    """The SiteLoginNeeded for a credential the site turned down."""
    acc = LOGIN_SITES[site]
    lead = f"{acc.label} refused the login token{detail}"
    return SiteLoginNeeded(site, "refused", (
        f"{lead}, which usually means the saved token ({token_path(site)} or the "
        f"{acc.env_name} environment variable) is invalid or expired. Sign in at "
        f"{acc.sign_in_url} again and save the new value in that file (see "
        f"docs/PROJECT-REFERENCE.md, {acc.doc_section}), then send the link again."))


def _cookie_pairs(text):
    """``{name: value}`` from a pasted cookie string, or {} when it is not one."""
    body = text.strip()
    if body[:7].casefold() == "cookie:":
        body = body[7:]
    pairs = {}
    for part in body.split(";"):
        if not part.strip():
            continue
        name, sep, value = part.strip().partition("=")
        if not sep or not name.strip() or any(c.isspace() for c in name.strip()):
            return {}
        pairs[name.strip()] = value.strip().strip("\"'")
    return pairs


def _normalize(raw, site=None):
    """The bare token from what an operator is likely to paste.

    Accepts the cookie value as-is, wrapped in quotes, with a leading
    ``Bearer ``, a MyMiniFactory sign-in address (``#access_token=...``),
    (Nexprint) the whole ``ACCOUNT_INFO`` JSON object from local storage, and,
    for a ``site`` whose value lives in cookies, a pasted cookie string
    (``model_user_id=123; model_token=abc``, in either order, optionally after
    ``Cookie:``). Creality's two cookies become ``model_user_id:model_token``.
    """
    token = raw.strip()
    acc = LOGIN_SITES.get(site)
    if acc is not None and acc.cookies and "=" in token and not token.startswith("{"):
        pairs = _cookie_pairs(token)
        wanted = [pairs.get(name) for name in acc.cookies]
        if wanted and all(wanted):
            return ":".join(wanted)
    if "access_token=" in token and not token.startswith("{"):
        from urllib.parse import parse_qs, urlsplit
        part = urlsplit(token).fragment or urlsplit(token).query or token
        found = parse_qs(part).get("access_token")
        if found:
            token = found[0]
    if token.startswith("{"):
        try:
            info = json.loads(token)
        except ValueError:
            info = None
        if isinstance(info, dict):
            token = str(info.get("access_token") or info.get("accessToken") or "").strip()
    token = token.strip("\"'")
    if token[:7].casefold() == "bearer ":
        token = token[7:].strip()
    return token.strip("\"'")


def stored(site, *, environ=None):
    """(value, where) for ``site``'s saved credential, or (None, None) when none is saved.

    The file wins when it exists (the chat connect flow writes it, so a
    freshly saved value always takes effect); otherwise the environment
    variable. ``value`` is normalized and may be empty (an empty file).
    """
    acc = LOGIN_SITES[site]
    path = token_path(site)
    try:
        return _normalize(path.read_text(encoding="utf-8"), site), str(path)
    except FileNotFoundError:
        env = os.environ if environ is None else environ
        value = _normalize(env.get(acc.env_name) or "", site)
        return (value, f"the {acc.env_name} environment variable") if value else (None, None)


def read_token(site, *, now=None, environ=None):
    """The login token for ``site``; SiteLoginNeeded when unusable.

    A JWT whose ``exp`` has passed is refused here, before any request, so
    the operator hears "expired" rather than a site refusal.
    """
    acc = LOGIN_SITES[site]
    path = token_path(site)
    token, where = stored(site, environ=environ)
    if token is None:
        raise SiteLoginNeeded(site, "missing", (
            f"{acc.label} downloads need a login token in {path} (or the "
            f"{acc.env_name} environment variable); sign in at "
            f"{acc.sign_in_url} in a browser, open the developer tools, copy the "
            f"value from {acc.where}, and store it as the only content of that "
            f"file; see docs/PROJECT-REFERENCE.md ({acc.doc_section})"))
    if not token:
        raise SiteLoginNeeded(site, "empty", f"{acc.label} token empty: {path} has no token in it")
    exp = _jwt_expiry(token)
    if exp is not None and exp <= (time.time() if now is None else now):
        raise SiteLoginNeeded(site, "expired", (
            f"The {acc.label} login token in {where} has expired. Sign in at "
            f"{acc.sign_in_url} again and save the new value in {path} "
            f"(see docs/PROJECT-REFERENCE.md, {acc.doc_section})."))
    return token


_PASTE_PATTERNS = {
    "thingiverse": r"[A-Za-z0-9]{20,64}",
    "cults3d": r"[^:\s]+:[A-Za-z0-9_\-]{16,}",
    "crealitycloud": r"\d{4,}:[^\s:]{16,}",
    "thangs": r"(?=.{40,})[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",  # a JWT (its `exp` is documented)
}


def _looks_like_address(token):
    """True for a link or file path: ordinary messages, never a bare token."""
    return "://" in token or "/" in token or "\\" in token or token.casefold().startswith("www.")


def accept_pasted(site, raw, *, now=None):
    """``(token, "")`` for a value pasted in chat that looks like ``site``'s credential.

    Otherwise ``("", reason)``. The check is cheap and local: the right shape
    for the site (never a link or path), and a JWT that has not already expired. The reason never
    contains any of the pasted text, so it is safe to post and to log.
    """
    acc = LOGIN_SITES[site]
    token = _normalize(str(raw or ""), site)
    pattern = _PASTE_PATTERNS.get(site)
    # A MyMiniFactory sign-in address was already reduced to its token by _normalize.
    ok = (0 < len(token) <= 4096 and not any(c.isspace() for c in token)
          and not _looks_like_address(token)
          and (re.fullmatch(pattern, token) if pattern else len(token) >= 16))
    if not ok:
        return "", (f"That doesn't look like a {acc.label} token (I expect {acc.shape}). "
                    "Nothing was saved. Paste it again, or reply `cancel`.")
    exp = _jwt_expiry(token)
    if exp is not None and exp <= (time.time() if now is None else now):
        return "", (f"That {acc.label} token has already expired. Sign in again, copy a fresh one "
                    "and paste that, or reply `cancel`.")
    return token, ""


def save_token(site, token):
    """Write ``token`` as the only content of ``site``'s token file, atomically.

    The folder is created 0700 and the file 0600 (owner only; the chmod calls
    are no-ops on Windows). The write goes to a sibling temp file that
    replaces the target, so a reader never sees half a token. Returns the
    path. OSError messages carry the path only, never the value.
    """
    path = token_path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


RISK_NOTE = ("Pasting is quickest, but know where it goes: Print Handy stores the token on the "
             "machine it runs on, and anyone who can read this channel could see your message "
             "until it is deleted. I'll delete it right away if I can, but the chat service may "
             "keep a copy. If that bothers you, use the file instead.")


def connect_steps(site, *, renew=False, lead=""):
    """The complete chat walkthrough for connecting ``site``, in one message.

    ``lead`` is the one-line reason it is needed. Then the numbered steps,
    the two ways to hand the value over (paste it in chat, or save it on the
    Print Handy machine), a plain note on what pasting risks, and the docs link.
    """
    acc = LOGIN_SITES[site]
    path = display_path(site)
    intro = acc.intro or (
        f"Print Handy can't sign in for you ({acc.label}'s login page has a robot check, and "
        "passwords are never stored), so it uses the login token your browser already has.")
    steps = list(acc.steps) or [
        f"Sign in at {acc.sign_in_url} in a browser.",
        "Open the developer tools (F12), then Application (Chrome/Edge) or Storage (Firefox).",
        f"Copy the value from {acc.where}."]
    lines = ([lead.strip(), ""] if lead.strip() else []) + [intro, ""]
    lines += [f"{n}. {text}" for n, text in enumerate(steps, 1)]
    lines.append(f"{len(steps) + 1}. **Paste it here as your next message**, or save it on the "
                 f"Print Handy machine as the only content of `{path}` in Print Handy's home "
                 "folder and press **Saved**. I'll carry on from there.")
    lines += ["", RISK_NOTE]
    lines += ["", f"Save command, `.env` option and details: {acc.docs_url}"]
    return "\n".join(lines)


# /print-source: every site Print Handy knows, in the order people meet them.
# kind: "login" (needs a LOGIN_SITES credential to download), "open" (no
# credential), "listing" (listing only; credential named when one is used),
# "none" (not supported).
SOURCES = (
    ("makerworld", "MakerWorld", "login", ""),
    ("printables", "Printables", "open", "no login needed"),
    ("thingiverse", "Thingiverse", "login", ""),
    ("myminifactory", "MyMiniFactory", "login", ""),
    ("crealitycloud", "Creality Cloud", "login", ""),
    ("thangs", "Thangs", "login", ""),
    ("nexprint", "Nexprint", "login", ""),
    ("cults3d", "Cults3D", "listing", "listing only — Cults' API never shares a model's files"),
    ("makeronline", "MakerOnline", "listing",
     "listing only — its downloads need a browser security check Print Handy won't fake"),
    ("grabcad", "GrabCAD", "none", "not supported — its firewall blocks apps and its terms forbid them"),
)


def credential_state(site, *, now=None, environ=None):
    """'ready', 'missing', 'empty' or 'expired' for ``site``'s saved credential."""
    value, _ = stored(site, environ=environ)
    if value is None:
        return "missing"
    if not value:
        return "empty"
    exp = _jwt_expiry(value)
    if exp is not None and exp <= (time.time() if now is None else now):
        return "expired"
    return "ready"


def source_report(*, now=None, environ=None):
    """The /print-source answer: one line per site. Never shows a credential."""
    lines = ["**Model sources** — post a link from any of these:"]
    for site, label, kind, note in SOURCES:
        if kind == "open":
            lines.append(f"- ✅ **{label}**: ready ({note})")
            continue
        if kind == "none":
            lines.append(f"- ⛔ **{label}**: {note}")
            continue
        if site not in LOGIN_SITES:
            lines.append(f"- ℹ️ **{label}**: {note}")
            continue
        state = credential_state(site, now=now, environ=environ)
        acc = LOGIN_SITES[site]
        if kind == "listing":
            have = "key saved" if state == "ready" else "needs your API key"
            lines.append(f"- ℹ️ **{label}**: {note} ({have})")
        elif state == "ready":
            _, where = stored(site, environ=environ)
            source = "variable" if where and where.startswith("the ") else "token file"
            lines.append(f"- ✅ **{label}**: connected ({source})")
        elif state == "expired":
            lines.append(f"- ⚠️ **{label}**: login expired — post a {label} link and I'll walk "
                         "you through renewing it")
        else:
            lines.append(f"- ❌ **{label}**: not connected — post a {label} link and I'll walk "
                         f"you through connecting it, or see {acc.docs_url}")
    return "\n".join(lines)
