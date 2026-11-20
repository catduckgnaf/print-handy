"""The one place Print Handy talks to an optional AI model (#818, #1731).

There is exactly one model endpoint: the OpenAI-compatible endpoint in
``[llm]`` — ``chat_url``, ``model``, ``api_key_env`` and ``timeout_s``.
Three tiers name the purposes a call serves:

* ``small``: camera and image checks, label and purchase reads.
* ``chat``: Web Chat questions the scripted handlers cannot answer.
* ``heavy``: design and model-file editing and repair. The repair
  feature (#978, ``core/ai_fix.py``) calls it: "Fix this for my printer"
  proposals are proposed on this tier.

A tier is a purpose plus an optional model name: ``[ai.<tier>] model``
overrides the [llm] model for that tier's calls, and ``design.model``
overrides it for design's heavy calls. Nothing else is per-tier — the
endpoint, the key and the timeout come from [llm] alone, and there is
one credential-binding slot for the endpoint (``llm.chat_url``).

``call`` is the whole interface, and the only code in Print Handy that
posts to a model endpoint. A call that fails fails honestly: there is
no fallback retry. The answer comes back with the model that served it,
so a caller can record which one that was. Callers keep interpreting
the answer text; this module only moves it.
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
TIER_KEYS = ("model",)

DEFAULT_TIMEOUT_S = 30.0


class AIRouteError(Exception):
    """Base class for route problems raised before or instead of a model answer."""


class AINotConfigured(AIRouteError, RuntimeError):
    """The one endpoint has no URL or no model for this tier."""


class AIKeyMissing(AIRouteError):
    """The endpoint is hosted and its API key is not available. Nothing was sent."""


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
    leg: _Leg
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


def model_for(tier, cfg=None) -> str:
    """The model name a tier's calls use (see the resolution order below)."""
    cfg = cfg if cfg is not None else _config.cfg()
    return _model_for(tier, cfg)


def _model_for(tier, cfg) -> str:
    """The model name a tier's calls use: its own override, then the defaults.

    The heavy tier's default override is ``design.model`` (design is the
    heavy tier's main caller and names its stronger model there); every
    tier otherwise falls back to the [llm] model.
    """
    model = str(_tier_settings(tier, cfg).get("model") or "").strip()
    if model:
        return model
    if tier == "heavy":
        model = str((cfg.get("design") or {}).get("model") or "").strip()
        if model:
            return model
    return str((cfg.get("llm") or {}).get("model") or "").strip()


def _resolve(tier, cfg) -> _Route | None:
    cfg = cfg if cfg is not None else _config.cfg()
    llm = cfg.get("llm") or {}
    url = str(llm.get("chat_url") or "").strip()
    model = _model_for(tier, cfg)
    if not url or not model:
        return None
    timeout = float(llm.get("timeout_s") or 0) or DEFAULT_TIMEOUT_S
    leg = _Leg(url, model, _secret(llm.get("api_key_env")), keyless_ok(url), "llm.chat_url")
    return _Route(tier, leg, timeout)


def configured(tier, cfg=None) -> bool:
    """Whether the tier resolves: the one endpoint has a URL and the tier a model."""
    return _resolve(tier, cfg) is not None


def endpoint_url(tier, cfg=None) -> str:
    """The one endpoint's URL as the tier resolves it; empty when not configured."""
    route = _resolve(tier, cfg)
    return route.leg.url if route else ""


def ready(tier, cfg=None) -> bool:
    """Whether a call would be sent: the tier is configured and its endpoint has its key or needs none."""
    route = _resolve(tier, cfg)
    return bool(route and _key_ok(tier, route.leg))


def _key_ok(tier, leg) -> bool:
    """Whether the call may be sent. Only the small tier insists on a key for a hosted endpoint.

    Its calls feed alerts, and an unauthenticated hosted endpoint would fail
    every check and post a false one. The chat and heavy tiers have always sent
    without a key when none was set, and still do.
    """
    return tier != "small" or bool(leg.key) or leg.keyless_ok


def describe(tier, cfg=None) -> str:
    """"model at url" for the tier; empty when the tier is not configured."""
    route = _resolve(tier, cfg)
    if route is None:
        return ""
    return f"{route.leg.model} at {route.leg.url}"


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


def call(tier, messages, *, images=(), max_tokens=200,
         temperature=0.2, timeout=None, cfg=None, opener=None):
    """Send ``messages`` (plus optional JPEG ``images``) on the tier: ``(answer, served_by)``.

    ``served_by`` is the model name that produced the answer. Raises
    AINotConfigured or AIKeyMissing before anything is sent, and otherwise
    the transport or parse error: a failed call fails, it is not retried
    on another model or endpoint.
    """
    route = _resolve(tier, cfg)
    if route is None:
        raise AINotConfigured(f"the {tier} AI tier has no endpoint and model "
                              f"(set [llm] chat_url and model)")
    if not _key_ok(tier, route.leg):
        raise AIKeyMissing(f"the {tier} AI tier's API key is not configured")
    wait = float(timeout) if timeout else route.timeout_s
    try:
        answer = _post(route.leg, messages, images, max_tokens, temperature, wait, opener)
    except Exception as exc:
        logger.warning("ai %s call to %s failed: %s: %s", tier, route.leg.model, type(exc).__name__, exc)
        raise
    logger.info("ai %s served by %s", tier, route.leg.model)
    return answer, route.leg.model
