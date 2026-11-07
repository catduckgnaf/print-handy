"""The Wrapped cloud speech client (#981 step 4): text in, audio out.

This is a small dedicated client beside :mod:`print_handy.ai_routes`,
not a fourth ``ai_routes`` tier, on purpose. The ``ai_routes`` tiers
are chat-shaped: they POST ``messages`` and parse a text answer out of
``choices[0].message.content``, with fallback-model retry semantics
for a text answer. A speech call is the opposite shape — a short text
``input`` in, a binary audio body out, no choices to parse and no
meaningful text fallback — so forcing it through that interface would
bend both. What it does share with ``ai_routes`` is the secret
convention: the key is named by an ``api_key_env`` setting and read
through the same profile-environment secret path, never stored as a
setting value.

Configuration is the ``[ai.speech]`` table (``chat_url``, ``model``,
``api_key_env``, ``timeout_s``), validated like a tier table by
``settings._validate_ai``. When the table names no ``chat_url``, the
``chat`` tier's endpoint is reused: a trailing ``/chat/completions``
is stripped and ``/audio/speech`` is appended. The model defaults to
``tts-1`` (the widely supported OpenAI-compatible speech model name)
rather than the chat tier's model, because a chat model cannot speak.
Nothing is sent when no endpoint is configured, and a hosted endpoint
with no key available refuses before anything is sent — the same
discipline as the ``ai_routes`` small tier.
"""
from __future__ import annotations

import json
import urllib.request

from . import ai_routes

DEFAULT_MODEL = "tts-1"
DEFAULT_VOICE = "alloy"
DEFAULT_TIMEOUT_S = 30.0


class SpeechNotConfigured(RuntimeError):
    """No speech endpoint is configured. Nothing was sent."""


class SpeechKeyMissing(RuntimeError):
    """The speech endpoint is hosted and its key is unavailable. Nothing was sent."""


def _speech_table(cfg):
    values = ((cfg or {}).get("ai") or {}).get("speech") or {}
    return values if isinstance(values, dict) else {}


def speech_url(cfg=None):
    """The POST /audio/speech URL for ``cfg``, or "" when unconfigured."""
    cfg = cfg if cfg is not None else {}
    table = _speech_table(cfg)
    url = str(table.get("chat_url") or "").strip().rstrip("/")
    if not url:
        route = ai_routes._resolve("chat", cfg, None, "")
        if route is None:
            return ""
        url = route.primary.url.rstrip("/")
    if url.endswith("/audio/speech"):
        return url
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url + "/audio/speech"


def _resolve_speech(cfg):
    """(url, model, key, timeout_s) for one speech call."""
    table = _speech_table(cfg or {})
    url = speech_url(cfg)
    if not url:
        raise SpeechNotConfigured(
            "no speech endpoint is configured (set [ai.speech] chat_url, "
            "or configure the chat AI tier to reuse its endpoint)")
    model = str(table.get("model") or "").strip()
    key = ""
    env_name = str(table.get("api_key_env") or "").strip()
    if env_name:
        key = ai_routes._secret(env_name)
    if not model or not key:
        route = ai_routes._resolve("chat", cfg or {}, None, "")
        if route is not None:
            # The chat tier's key goes only to the chat tier's own
            # endpoint (the ai_routes rule: a different host never gets
            # it). A speech URL on another host needs its own
            # [ai.speech] api_key_env, set or not.
            same_endpoint = (route.primary.url.rstrip("/").removesuffix("/chat/completions")
                             == url.removesuffix("/audio/speech"))
            if not key and same_endpoint:
                key = route.primary.key
    model = model or DEFAULT_MODEL
    # The endpoint's own host decides whether a key is required, using
    # the same local-network rule as ai_routes.
    base = url.removesuffix("/audio/speech")
    if not key and not ai_routes.keyless_ok(base):
        raise SpeechKeyMissing(
            "the speech endpoint is hosted and its API key is not "
            "configured (set [ai.speech] api_key_env)")
    timeout = float(table.get("timeout_s") or 0) or DEFAULT_TIMEOUT_S
    return url, model, key, timeout


def synthesize_speech(text, *, cfg=None, opener=None, voice=DEFAULT_VOICE):
    """Speak ``text`` and return the audio bytes (MP3 by default).

    ``opener`` is the injectable transport seam (the
    ``urllib.request.urlopen`` shape), so tests never touch the
    network. Raises :class:`SpeechNotConfigured` or
    :class:`SpeechKeyMissing` before anything is sent.
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("nothing to speak: the text is empty")
    url, model, key, timeout = _resolve_speech(cfg)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": model, "input": text, "voice": voice,
                       "response_format": "mp3"}).encode()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        audio = response.read()
    if not audio:
        raise ValueError("the speech endpoint returned no audio")
    return audio


__all__ = ["DEFAULT_MODEL", "DEFAULT_VOICE", "SpeechKeyMissing",
           "SpeechNotConfigured", "speech_url", "synthesize_speech"]
