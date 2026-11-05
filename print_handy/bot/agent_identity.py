"""Per-user identity for the stray-chat agent lane (#902).

The Discord Assist model, applied to Print Handy's ``[agent]`` lane: the
bot answers a user's free text through an agent only when it knows which
identity to run the request as.

* a **mapped** user (``[agent.users]``) runs as their identity's endpoint,
  with that identity's own key env var — never a shared root key;
* an **allowlisted** user (``[agent.allowlist]``) runs as the configured
  **fallback identity**, meant to be a least-privilege profile;
* **everyone else gets no agent call at all** — they keep the ``[llm]``
  tier if configured, else the short not-understood reply. Scripted
  handlers, buttons and confirmation gates are untouched for everyone.

Fail closed: an identity whose endpoint or key is missing resolves to no
call, never to another identity. With no identity layer configured, the
legacy single ``[agent] chat_url`` keeps working exactly as before and is
treated as a fallback identity shared by every authorized channel user
(bot check names that as a deprecation). The table and the checks here
are self-contained on purpose, so the layer can be lifted out into a
shared service later (#902 scope).

This module also carries the lane's conversation shape (per-user,
per-channel conversation ids with an idle reset), its per-user rate
limit, the reply sanitizer (agent text can never ping anyone), the
auth-checked endpoint probe and the Hermes ``api_server`` capability
report used by bot check, the doctor and the Web UI.
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LEGACY_IDENTITY = "legacy"
DEFAULT_IDLE_TIMEOUT_S = 1800
DEFAULT_RATE_LIMIT_PER_MINUTE = 10

# Hermes toolsets that must never be reachable by a fallback (guest)
# identity: the profile would hand them to anyone in the channel (#902).
DANGEROUS_TOOLSETS = {
    "terminal": "terminal",
    "file": "file",
    "files": "file",
    "code": "code execution",
    "code_execution": "code execution",
    "cron": "cron",
    "delegation": "delegation",
    "delegate": "delegation",
    "kanban": "kanban",
}


@dataclass(frozen=True)
class Identity:
    """One named agent endpoint with its own key. ``key()`` reads the key
    from the named environment variable at call time; the value is never
    stored, logged or shown."""

    name: str
    chat_url: str
    api_key_env: str = ""
    model: str = ""
    timeout_s: float = 120

    def key(self, environ=None):
        if not self.api_key_env:
            return ""
        return (os.environ if environ is None else environ).get(self.api_key_env) or ""

    @property
    def host(self):
        return urlparse(self.chat_url).hostname or self.chat_url


def _agent_section(cfg):
    return (cfg or {}).get("agent") or {}


def identities(cfg):
    """All configured identities by name, including the synthetic legacy
    one when only the single shared endpoint is set."""
    agent = _agent_section(cfg)
    found = {}
    for name, values in (agent.get("identities") or {}).items():
        found[str(name)] = Identity(
            name=str(name),
            chat_url=str(values.get("chat_url") or ""),
            api_key_env=str(values.get("api_key_env") or ""),
            model=str(values.get("model") or ""),
            timeout_s=float(values.get("timeout_s") or 120))
    if agent.get("chat_url"):
        found.setdefault(LEGACY_IDENTITY, Identity(
            name=LEGACY_IDENTITY,
            chat_url=str(agent["chat_url"]),
            api_key_env=str(agent.get("api_key_env") or ""),
            model=str(agent.get("model") or ""),
            timeout_s=float(agent.get("timeout_s") or 120)))
    return found


def layer_active(cfg):
    """True when the operator configured any part of the identity layer
    (as opposed to only the legacy single endpoint, or nothing)."""
    agent = _agent_section(cfg)
    return bool(agent.get("identities") or agent.get("users")
                or agent.get("allowlist") or agent.get("fallback_identity"))


def lane_configured(cfg):
    """True when any agent endpoint exists at all (legacy or identities)."""
    return bool(identities(cfg))


def fallback_identity(cfg):
    """The identity allowlisted users run as: the named fallback, else the
    legacy shared endpoint when that is all that is configured."""
    agent = _agent_section(cfg)
    found = identities(cfg)
    named = str(agent.get("fallback_identity") or "").strip()
    if named and named in found:
        return found[named]
    if not layer_active(cfg):
        return found.get(LEGACY_IDENTITY)
    return found.get(LEGACY_IDENTITY) if not named else None


def _user_keys(platform, user_id):
    uid = str(user_id or "").strip()
    if not uid:
        return ()
    return (f"{platform}:{uid}", uid) if platform else (uid,)


def resolve(cfg, platform, user_id):
    """The identity a user's stray chat runs as, or None for no agent call.

    Mapped users get their identity; allowlisted users get the fallback;
    everyone else gets None once the identity layer is active. With no
    layer, the legacy shared endpoint is everyone's fallback (today's
    behavior); with nothing configured, None.
    """
    found = identities(cfg)
    if not found:
        return None
    if not layer_active(cfg):
        return found.get(LEGACY_IDENTITY)
    agent = _agent_section(cfg)
    users = agent.get("users") or {}
    for key in _user_keys(platform, user_id):
        name = users.get(key)
        if name and name in found:
            return found[name]
    allowlist = {str(entry).strip() for entry in agent.get("allowlist") or []}
    if any(key in allowlist for key in _user_keys(platform, user_id)):
        return fallback_identity(cfg)
    return None


def authorized_counts(cfg):
    """(mapped, allowlisted) user counts for bot check and the Web UI."""
    agent = _agent_section(cfg)
    return len(agent.get("users") or {}), len(agent.get("allowlist") or [])


def anyone_authorized(cfg):
    """True when at least one user can resolve to an identity."""
    if not lane_configured(cfg):
        return False
    if not layer_active(cfg):
        return True  # the legacy endpoint serves every authorized channel user
    mapped, allowlisted = authorized_counts(cfg)
    if mapped:
        return True
    return bool(allowlisted and fallback_identity(cfg) is not None)


def nobody_authorized(cfg):
    """The lane has endpoints but no user can ever reach one (#902's
    warning state: bot check and the Web UI must say so, loudly)."""
    return lane_configured(cfg) and not anyone_authorized(cfg)


class ConversationBook:
    """Per-(identity, platform, channel, user) conversation ids.

    The same id is carried to the endpoint (OpenAI ``user`` field and the
    session header) until the conversation has been idle for the
    configured timeout; the next message starts a fresh id, so the agent
    drops stale context on its own schedule too.
    """

    def __init__(self, idle_timeout_s=DEFAULT_IDLE_TIMEOUT_S, clock=None):
        self.idle_timeout_s = float(idle_timeout_s or DEFAULT_IDLE_TIMEOUT_S)
        self._clock = clock or time.monotonic
        self._sessions = {}

    def conversation_id(self, key):
        now = self._clock()
        entry = self._sessions.get(key)
        if entry is None or now - entry[1] > self.idle_timeout_s:
            entry = (uuid.uuid4().hex, now)
        else:
            entry = (entry[0], now)
        self._sessions[key] = entry
        return entry[0]


class RateLimiter:
    """A per-key sliding-window limit with a one-notice rule: the first
    excess message in a window earns one notice, the rest drop silently."""

    def __init__(self, limit=DEFAULT_RATE_LIMIT_PER_MINUTE, window_s=60.0, clock=None):
        self.limit = max(1, int(limit or DEFAULT_RATE_LIMIT_PER_MINUTE))
        self.window_s = float(window_s)
        self._clock = clock or time.monotonic
        self._hits = {}
        self._noticed = {}

    def check(self, key):
        """'ok', 'notice' (drop this one, tell the user) or 'drop'."""
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] > self.window_s:
            hits.popleft()
        if len(hits) < self.limit:
            hits.append(now)
            return "ok"
        if now - self._noticed.get(key, -self.window_s - 1) > self.window_s:
            self._noticed[key] = now
            return "notice"
        return "drop"


class LaneState:
    """The lane's process state (conversations + rate limits), shared by
    the standalone runtime and the Hermes-mode dispatch hook."""

    def __init__(self, cfg=None):
        agent = _agent_section(cfg)
        self.conversations = ConversationBook(agent.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S)
        self.limiter = RateLimiter(agent.get("rate_limit_per_minute") or DEFAULT_RATE_LIMIT_PER_MINUTE)

    def conversation_key(self, identity, platform, chat_id, user_id):
        return (identity.name, str(platform or ""), str(chat_id or ""), str(user_id or ""))


_LANE_STATES = {}


def lane_state(cfg):
    """The process-wide LaneState for this config object (the runtimes
    hold one config for the process lifetime)."""
    key = id(cfg)
    state = _LANE_STATES.get(key)
    if state is None:
        state = _LANE_STATES[key] = LaneState(cfg)
    return state


_MENTION = re.compile(r"<@[!&]?(\d+)>")
_EVERYONE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)


def sanitize_reply(text):
    """Neutralize every ping in an agent reply: ``@everyone``, ``@here``
    and user/role mentions get a zero-width space after the ``@``, so the
    text reads the same but can never ping anyone, on any adapter —
    including ones that ignore an allowed-mentions flag."""
    text = _EVERYONE.sub(lambda m: "@\u200b" + m.group(1), str(text or ""))
    return _MENTION.sub(lambda m: "<@\u200b" + ("&" if m.group(0).startswith("<@&") else "")
                         + m.group(1) + ">", text)


def is_dm(source):
    """Best-effort DM detection across platform source shapes: a source
    that marks itself a direct message in any of the fields the platform
    adapters use. Unmarked sources are channels (today's behavior)."""
    if getattr(source, "is_dm", None):
        return True
    if str(getattr(source, "channel_type", "") or "").casefold() in ("dm", "direct", "group_dm"):
        return True
    metadata = getattr(source, "metadata", None) or {}
    return isinstance(metadata, dict) and bool(metadata.get("is_dm"))


def log_served(identity, platform, chat_id, user_id):
    """Observability: which identity served a message — never its content."""
    logger.info("agent lane: identity %s served %s user %s in chat %s",
                identity.name, platform or "?", user_id, chat_id)


def models_url(identity):
    """The endpoint's model-list URL, derived from the chat URL — the
    auth-checked reachability probe target."""
    url = identity.chat_url
    if url.endswith("/chat/completions"):
        return url[: -len("/chat/completions")] + "/models"
    return url


def probe(identity, *, opener=None, timeout_s=3.0, environ=None):
    """Auth-checked reachability for one identity, for bot check.

    Returns (verdict, detail): verdict is "ok" (the endpoint answered and
    accepted the key), "unauthorized" (it answered but rejected the key)
    or "unreachable". The key is sent as a bearer header and never
    appears in the detail.
    """
    headers = {}
    key = identity.key(environ)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(models_url(identity), headers=headers, method="GET")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", None) or 200
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        return "unreachable", f"{identity.host}: {type(exc).__name__}"
    if status == 200:
        return "ok", f"{identity.host}: reachable, key accepted"
    if status in (401, 403):
        return "unauthorized", f"{identity.host}: reachable, but the key was rejected (HTTP {status})"
    return "unreachable", f"{identity.host}: answered HTTP {status}"


def hermes_profile(identity):
    """The Hermes profile name in an endpoint URL (``/p/<profile>/``), or
    None when the endpoint is not a Hermes profile URL."""
    parts = [part for part in urlparse(identity.chat_url).path.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part == "p":
            return parts[index + 1]
    return None


def _profile_config_paths(profile, cfg):
    candidates = []
    hermes_home = str(((cfg or {}).get("paths") or {}).get("hermes_home") or "").strip()
    if hermes_home:
        home = Path(hermes_home).expanduser()
        candidates.append(home.parent / profile / "config.yaml")
        if home.name == profile:
            candidates.append(home / "config.yaml")
    candidates.append(Path("~/.hermes/profiles").expanduser() / profile / "config.yaml")
    return candidates


def capability_report(identity, cfg=None):
    """What the identity's Hermes profile may do on its ``api_server``
    platform: (level, detail, dangerous).

    level is "warn" when a dangerous toolset is exposed (or no explicit
    allowlist exists — Hermes then defaults to every toolset), "ok" when
    an explicit allowlist names no dangerous toolset, and "unknown" when
    the endpoint is not a Hermes profile URL or its config cannot be
    read from here. ``dangerous`` is the sorted list of exposed
    dangerous capabilities (terminal, file, code execution, cron,
    delegation, kanban).
    """
    profile = hermes_profile(identity)
    if profile is None:
        return "unknown", f"{identity.name}: not a Hermes profile endpoint; capability not checked", []
    path = next((p for p in _profile_config_paths(profile, cfg) if p.is_file()), None)
    if path is None:
        return "unknown", (f"{identity.name}: Hermes profile '{profile}', but its config is not "
                           f"readable from here; verify platform_toolsets.api_server on the "
                           f"Hermes host"), []
    try:
        from ..hermes._config import _parse_yaml
        data = _parse_yaml(path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown", f"{identity.name}: Hermes profile '{profile}' config at {path} is unreadable", []
    toolsets = None
    if isinstance(data, dict):
        platforms = data.get("platform_toolsets")
        if isinstance(platforms, dict):
            toolsets = platforms.get("api_server")
    if toolsets is None:
        return "warn", (f"{identity.name}: Hermes profile '{profile}' has no explicit "
                        f"platform_toolsets.api_server allowlist — Hermes defaults it to every "
                        f"toolset, including terminal, file, code execution, cron, delegation "
                        f"and kanban"), sorted(set(DANGEROUS_TOOLSETS.values()))
    if isinstance(toolsets, str):
        toolsets = [toolsets]
    names = [str(item).strip().casefold() for item in toolsets if str(item).strip()]
    if any(name in ("all", "*", "everything") for name in names):
        return "warn", (f"{identity.name}: Hermes profile '{profile}' allows every toolset on "
                        f"api_server, including terminal, file, code execution, cron, delegation "
                        f"and kanban"), sorted(set(DANGEROUS_TOOLSETS.values()))
    dangerous = sorted({DANGEROUS_TOOLSETS[name] for name in names if name in DANGEROUS_TOOLSETS})
    if dangerous:
        return "warn", (f"{identity.name}: Hermes profile '{profile}' exposes "
                        f"{', '.join(dangerous)} on api_server"), dangerous
    return "ok", (f"{identity.name}: Hermes profile '{profile}' api_server toolsets are "
                  f"{', '.join(names) or 'none'} — nothing dangerous"), []


def status_summary(cfg, *, probe_fn=None):
    """(detail, warning) for the doctor / Web UI agent-lane row: lane
    state, mapped/allowlisted counts, the fallback identity, the
    nobody-authorized warning and the fallback capability warning.

    ``probe_fn`` (bot check passes the real one) adds per-identity
    reachability; the doctor surface stays offline and omits it.
    """
    if not lane_configured(cfg):
        return "not configured (set [agent] chat_url or [agent.identities] to enable the lane)", None
    found = identities(cfg)
    mapped, allowlisted = authorized_counts(cfg)
    fallback = fallback_identity(cfg)
    parts = []
    warnings = []
    if layer_active(cfg):
        parts.append(f"identity layer on: {mapped} mapped, {allowlisted} allowlisted, "
                     f"fallback {fallback.name if fallback else 'none'}")
    else:
        parts.append("legacy single endpoint (treated as the fallback identity for every "
                     "authorized channel user; add [agent.users] to map users to identities)")
    if nobody_authorized(cfg):
        warnings.append("nobody is authorized: the agent lane is enabled but no user maps to an "
                        "identity and no allowlisted user has a fallback identity — stray chat "
                        "gets no agent call")
    lines = []
    for identity in found.values():
        if probe_fn is not None:
            verdict, detail = probe_fn(identity)
            lines.append(f"{identity.name}: {verdict} ({detail})")
            if verdict != "ok":
                warnings.append(f"identity {identity.name} is not serving: {detail}")
        level, detail, _dangerous = capability_report(identity, cfg)
        guest_reachable = fallback is not None and identity.name == fallback.name
        if level == "warn" and guest_reachable:
            warnings.append(detail + " — and this profile is reachable by the fallback identity, "
                                     "so anyone allowlisted (or, on a legacy config, anyone in the "
                                     "channel) can reach those tools")
        elif level != "unknown":
            lines.append(detail)
    if lines:
        parts.append("; ".join(lines))
    return "; ".join(parts), "; ".join(warnings) if warnings else None
