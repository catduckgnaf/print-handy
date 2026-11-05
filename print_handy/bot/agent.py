"""The stray-chat agent lane for the standalone bot (#524 stage 3).

Free text the handlers did not take can be answered by an operator-configured
agent endpoint — any OpenAI-compatible chat completions URL, for example a
Hermes agent behind its API. The lane is deliberately unlike ``bot/llm.py``:

* No bot system prompt, no JSON contract and **no command channel**. The
  endpoint has its own persona and answers in its own voice; its reply is
  posted as-is and can never name a command into execution. ``llm.ask``'s
  whitelist is safe only because the bot controls that system prompt and the
  model is stateless; a remote agent's free text must not gain printer
  authority by being parsed for commands.
* The bot keeps no conversation history. It sends the user's text once, as a
  single user message, keyed by the live session key in the
  ``X-Print-Handy-Session`` header; the endpoint may key its own
  conversation on that value, or ignore it and treat every call as one-shot.
* One attempt, bounded by ``[agent] timeout_s`` — no retries, so a request
  the agent acts on is never double-submitted.

#902 adds the identity layer on top (``bot/agent_identity.py``): when
``[agent.users]``/``[agent.identities]`` are configured, :func:`ask_as`
sends the message to the *caller's* identity endpoint with that
identity's key, carrying a per-user conversation id (idle-reset locally)
instead of the raw session key, and users with no identity get no call
at all. Unconfigured installs keep the behavior above exactly.
"""
from __future__ import annotations

import json
import os
import urllib.request

from ..hermes import _config

SESSION_HEADER = "X-Print-Handy-Session"


def configured(cfg=None):
    agent = (cfg or _config.cfg()).get("agent") or {}
    if agent.get("chat_url"):
        return True
    # The identity layer (#902): named endpoints count as configured even
    # when the legacy single URL is empty.
    return any(str(values.get("chat_url") or "")
               for values in (agent.get("identities") or {}).values()
               if isinstance(values, dict))


def ask_as(identity, text, conversation_id, *, opener=None):
    """Ask one identity's endpoint, as that identity (#902).

    The request carries the identity's own key (read from its named env
    var at call time, never logged), the stable conversation id as both
    the OpenAI ``user`` field and the session header, and no bot system
    prompt — the same no-command-channel contract as :func:`ask`.
    Raises on transport errors, an unparseable response or an empty
    reply; the caller applies the lane's failure behavior.
    """
    key = identity.key()
    headers = {"Content-Type": "application/json", SESSION_HEADER: str(conversation_id)}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"messages": [{"role": "user", "content": str(text)}], "stream": False,
               "user": str(conversation_id)}
    if identity.model:
        payload["model"] = identity.model
    body = json.dumps(payload).encode()
    req = urllib.request.Request(identity.chat_url, data=body, headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=float(identity.timeout_s or 120)) as resp:
        data = json.loads(resp.read())
    reply = str(data["choices"][0]["message"]["content"] or "").strip()
    if not reply:
        raise ValueError("the agent endpoint returned an empty reply")
    return reply


def ask(text, session_key, *, cfg=None, opener=None):
    """The agent endpoint's reply text, posted as-is by the caller.

    Raises on transport errors, an unparseable response or an empty reply;
    the caller falls back to the honest agent-unavailable line.
    """
    agent = (cfg or _config.cfg())["agent"]
    key = os.environ.get(str(agent.get("api_key_env") or "")) or ""
    headers = {"Content-Type": "application/json", SESSION_HEADER: str(session_key)}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"messages": [{"role": "user", "content": str(text)}], "stream": False}
    if agent.get("model"):
        payload["model"] = agent["model"]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(agent["chat_url"], data=body, headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=float(agent.get("timeout_s") or 120)) as resp:
        data = json.loads(resp.read())
    reply = str(data["choices"][0]["message"]["content"] or "").strip()
    if not reply:
        raise ValueError("the agent endpoint returned an empty reply")
    return reply
