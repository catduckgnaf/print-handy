"""The one place Print Handy talks to an optional AI model (#818).

Three tiers, each with its own endpoint settings under ``[ai.<tier>]``:

* ``small``: the camera and image check, label reading.
* ``chat``: questions the scripted handlers cannot answer.
* ``heavy``: model-file editing and repair, deep image work. The repair
  feature (#978, ``core/ai_fix.py``) calls it: "Fix this for my printer"
  proposals are proposed on this tier, with its fallback.

A tier setting has ``chat_url``, ``model``, ``fallback_model``, ``fallback_url``,
``api_key_env``, ``fallback_api_key_env`` and ``timeout_s``. Nothing ships
filled in. An install that configures no ``[ai]`` value keeps the settings it
already had, which are each tier's default route: ``services.vision_chat_url``
with the ``vision.*`` models for ``small``, ``llm.*`` for ``chat`` and
``design.*`` (inheriting ``llm.*``) for ``heavy``. A tier value, when set, wins
over that default one field at a time.

``call`` is the whole interface. The primary request that fails or times out is
retried exactly once on the fallback (``fallback_model``, on ``fallback_url``
when given, else on the same endpoint). The fallback never receives the
primary's key unless it is the same endpoint. The answer comes back with the
model that served it, so a caller can record which one that was. Callers keep
interpreting the answer text; this module only moves it.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import credential_binding
from .runtime import _config

logger = logging.getLogger(__name__)

TIERS = ("small", "chat", "heavy")
TIER_KEYS = ("chat_url", "model", "fallback_model", "fallback_url",
             "api_key_env", "fallback_api_key_env", "timeout_s")

DEFAULT_TIMEOUT_S = 30.0


class AIRouteError(Exception):
    """Base class for route problems raised before or instead of a model answer."""


class AINotConfigured(AIRouteError, RuntimeError):
    """The tier has no endpoint or no model."""


class AIKeyMissing(AIRouteError):
    """The endpoint is hosted and its API key is not available. Nothing was sent."""


@dataclass(frozen=True)
class Compat:
    """A tier's default route, supplied by a caller that owns its own setting lookups."""
    chat_url: str = ""
    model: str = ""
    key: str = ""
    timeout_s: float = 0.0
    # The setting whose host the key is bound to (#1192, credential_binding).
    slot: str = ""


@dataclass(frozen=True)
class _Leg:
    url: str
    model: str
    key: str
    keyless_ok: bool
    slot: str = ""


@dataclass(frozen=True)
class _Route:
    tier: str
    primary: _Leg
    fallback: _Leg | None
    timeout_s: float


def keyless_ok(url) -> bool:
    """True when the endpoint is on this machine or the local network.

    A local OpenAI-compatible server (Ollama, LM Studio, llama.cpp, a Docker
    service name like ``http://ollama:11434``) usually takes no key. A hosted
    endpoint still needs one. Decided from the URL alone (no DNS): loopback,
    private and link-local IPs, ``localhost``, a single-label host name, and
    ``.local`` / ``.lan`` / ``.home.arpa`` names.
    """
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (host == "localhost" or "." not in host
                or host.endswith((".local", ".lan", ".home.arpa", ".localhost")))
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _secret(name) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    try:
        return _config.profile_env_value(name) or ""
    except Exception:
        return os.environ.get(name) or ""


def _tier_settings(tier, cfg) -> dict[str, Any]:
    if tier not in TIERS:
        raise ValueError(f"unknown AI tier {tier!r}; expected one of {', '.join(TIERS)}")
    values = ((cfg or {}).get("ai") or {}).get(tier) or {}
    return values if isinstance(values, dict) else {}


def _default_route(tier, cfg, purpose) -> Compat:
    """The route the pre-[ai] settings already describe for this tier."""
    if tier == "chat":
        llm = cfg.get("llm") or {}
        return Compat(str(llm.get("chat_url") or ""), str(llm.get("model") or ""),
                      _secret(llm.get("api_key_env")), float(llm.get("timeout_s") or 0),
                      slot="llm.chat_url")
    if tier == "heavy":
        llm, design = cfg.get("llm") or {}, cfg.get("design") or {}
        return Compat(str(design.get("chat_url") or llm.get("chat_url") or ""),
                      str(design.get("model") or llm.get("model") or ""),
                      _secret(design.get("api_key_env") or llm.get("api_key_env")),
                      float(design.get("timeout_s") or 0),
                      slot="design.chat_url" if design.get("chat_url") else "llm.chat_url")
    vision = cfg.get("vision") or {}
    model = vision.get("model")
    url = str((cfg.get("services") or {}).get("vision_chat_url") or "").rstrip("/")
    return Compat(url, str(model or ""), _secret("CLIPROXY_API_KEY"), slot="services.vision_chat_url")


def _resolve(tier, cfg, compat, purpose) -> _Route | None:
    cfg = cfg if cfg is not None else _config.cfg()
    mine = _tier_settings(tier, cfg)
    base = compat or _default_route(tier, cfg, purpose)
    url = str(mine.get("chat_url") or "").strip() or base.chat_url
    model = str(mine.get("model") or "").strip() or base.model
    if not url or not model:
        return None
    env_name = str(mine.get("api_key_env") or "").strip()
    if mine.get("chat_url") or env_name:
        key = _secret(env_name)
    else:
        key = base.key
    slot = (f"ai.{tier}.chat_url" if str(mine.get("chat_url") or "").strip()
            else base.slot or f"ai.{tier}.chat_url")
    primary = _Leg(url, model, key, keyless_ok(url), slot)
    fallback = None
    fb_model = str(mine.get("fallback_model") or "").strip()
    fb_url = str(mine.get("fallback_url") or "").strip()
    if fb_model or fb_url:
        fb_url = fb_url or url
        # An explicitly named fallback key always wins; otherwise the same endpoint reuses the
        # primary's key, and a different host never gets it.
        fb_env = str(mine.get("fallback_api_key_env") or "").strip()
        fb_key = _secret(fb_env) if fb_env else (key if fb_url == url else "")
        fallback = _Leg(fb_url, fb_model or model, fb_key, keyless_ok(fb_url),
                        slot if fb_url == url else f"ai.{tier}.fallback_url")
    timeout = float(mine.get("timeout_s") or 0) or base.timeout_s or DEFAULT_TIMEOUT_S
    return _Route(tier, primary, fallback, timeout)


def configured(tier, cfg=None, *, compat=None, purpose="") -> bool:
    """Whether the tier has an endpoint and a model, from [ai.<tier>] or its default settings."""
    return _resolve(tier, cfg, compat, purpose) is not None


def explicit(tier, cfg=None) -> bool:
    """Whether [ai.<tier>] names its own endpoint (the default settings are not consulted)."""
    cfg = cfg if cfg is not None else _config.cfg()
    return bool(str(_tier_settings(tier, cfg).get("chat_url") or "").strip())


def overrides_route(tier, cfg=None) -> bool:
    """Whether [ai.<tier>] changes where or with which key the tier's default route is called.

    True for a chat_url of its own, and also for an api_key_env alone: that
    supplies the key for the endpoint the tier inherits.
    """
    cfg = cfg if cfg is not None else _config.cfg()
    mine = _tier_settings(tier, cfg)
    return bool(str(mine.get("chat_url") or "").strip() or str(mine.get("api_key_env") or "").strip())


def ready(tier, cfg=None, *, compat=None, purpose="") -> bool:
    """Whether a call would be sent: the tier is configured and its endpoint has its key or needs none."""
    route = _resolve(tier, cfg, compat, purpose)
    return bool(route and _key_ok(tier, route.primary))


def _key_ok(tier, leg) -> bool:
    """Whether the leg may be sent. Only the small tier insists on a key for a hosted endpoint.

    Its calls feed alerts, and an unauthenticated hosted endpoint would fail
    every check and post a false one. The chat and heavy tiers have always sent
    without a key when none was set, and still do.
    """
    return tier != "small" or bool(leg.key) or leg.keyless_ok


def describe(tier, cfg=None, *, purpose="") -> str:
    """"model at url", plus the fallback model when one is set; empty when the tier is not configured."""
    route = _resolve(tier, cfg, None, purpose)
    if route is None:
        return ""
    text = f"{route.primary.model} at {route.primary.url}"
    if route.fallback:
        where = "" if route.fallback.url == route.primary.url else f" at {route.fallback.url}"
        text += f", fallback {route.fallback.model}{where}"
    return text


def _post(leg, messages, images, max_tokens, temperature, timeout, opener):
    messages = [dict(message) for message in messages]
    if images:
        parts = [{"type": "image_url",
                  "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}
                 for image in images]
        last = messages[-1]
        text = last.get("content")
        last["content"] = ([{"type": "text", "text": text}] if isinstance(text, str) else list(text or [])) + parts
    headers = {"Content-Type": "application/json"}
    key = credential_binding.key_for(leg.slot, leg.url, leg.key)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": leg.model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    request = urllib.request.Request(leg.url, data=body, headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        data = json.loads(response.read())
    answer = str(data["choices"][0]["message"]["content"] or "").strip()
    if not answer:
        raise ValueError("the model returned an empty answer")
    return answer


def call(tier, messages, *, images=(), compat=None, purpose="", max_tokens=200,
         temperature=0.2, timeout=None, cfg=None, opener=None):
    """Send ``messages`` (plus optional JPEG ``images``) to the tier: ``(answer, served_by)``.

    ``served_by`` is the model name that produced the answer, the fallback's
    when the primary failed. ``compat`` replaces the tier's default route for
    callers that resolve their own settings; ``purpose`` picks the
    default model within a tier. Raises AINotConfigured or AIKeyMissing before
    anything is sent, and otherwise the last transport or parse error when
    every leg failed. Only a failed primary is retried, once, on the fallback.
    """
    route = _resolve(tier, cfg, compat, purpose)
    if route is None:
        raise AINotConfigured(f"the {tier} AI tier has no endpoint and model "
                              f"(set [ai.{tier}] chat_url and model, or the settings it falls back to)")
    if not _key_ok(tier, route.primary):
        raise AIKeyMissing(f"the {tier} AI tier's API key is not configured")
    wait = float(timeout) if timeout else route.timeout_s
    error = None
    for index, leg in enumerate((route.primary, route.fallback)):
        if leg is None:
            continue
        if index and not _key_ok(tier, leg):
            logger.warning("ai %s fallback skipped: no API key for %s", tier, leg.model)
            break
        try:
            answer = _post(leg, messages, images, max_tokens, temperature, wait, opener)
        except Exception as exc:
            error = exc
            logger.warning("ai %s call to %s failed: %s: %s", tier, leg.model, type(exc).__name__, exc)
            continue
        logger.info("ai %s served by %s%s", tier, leg.model, " (fallback)" if index else "")
        return answer, leg.model
    raise error
