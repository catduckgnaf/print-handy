"""Optional natural language for the standalone bot through any OpenAI-compatible chat endpoint.

The model only words a reply and may name one command from a fixed list. A named command runs
through the same handlers as a typed one, so every printer action still waits for its button.
Commands that change inventory or the printer screen without a button are never allowed here.
The model gets no tools, no files, no links and no printer access.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

from ..hermes import _config

# What the model may run. Each of these is read-only or asks for a button before acting.
ALLOWED = (
    re.compile(r"/print-help"),
    re.compile(r"/spool(?: list)?"),
    re.compile(r"/print-spools"),
    re.compile(r"/print-again list"),
    re.compile(r"/print-again(?! (?:stop|cancel|clear)\b)(?: [\w .#-]{1,60})?", re.I),
    re.compile(r"/print-next(?: [\w.-]{1,80}){0,2}"),
    re.compile(r"/print-(?:stop|resume|reboot|coldpull)"),
    # The guided nozzle change asks for its own confirmations before heating (#513).
    re.compile(r"/nozzle(?: change)?"),
    re.compile(r"/print-maintenance(?: (?:full|quick))?"),
    re.compile(r"/print-lookup(?: [\w .#/-]{1,80})?"),
    re.compile(r"/print-source"),
    re.compile(r"/print-troubleshoot(?: (?:first-layer|clog|heat-creep|bed-level|ringing))?"),
    re.compile(r"/print-macros"),
    # Firmware reporting and the manual guides are read-only; `update` asks
    # for its own confirmation but changes the printer, so it stays out (#561).
    re.compile(r"/print-firmware"),
    re.compile(r"/print-firmware guide (?:zmod|stock)"),
    # The calibration menu, records and Z-offset report are read-only; a
    # calibration run or an accept may never come from the model (#565).
    re.compile(r"/print-calibration"),
    re.compile(r"/print-calibration records"),
    re.compile(r"/print-calibration zoffset"),
    # Design asks for its own buttons before anything is sliced (#388 Stage 2);
    # /design is its short alias (#388 close-out).
    re.compile(r"/print-design(?: [\w .#/-]{1,160})?", re.I),
    re.compile(r"/design(?: [\w .#/-]{1,160})?", re.I),
)


def configured(cfg=None):
    llm = (cfg or _config.cfg()).get("llm") or {}
    return bool(llm.get("chat_url") and llm.get("model"))


def allowed_command(command):
    """The command when the model may run it, else None."""
    command = re.sub(r"\s+", " ", str(command or "").strip())
    if not command:
        return None
    name, _, rest = command.partition(" ")
    command = (name.casefold() + (" " + rest if rest else ""))
    return command if any(p.fullmatch(command) for p in ALLOWED) else None


def system_prompt(help_text):
    return (f"You are {_config.bot_name()}, the Discord helper for a 3D printer. You cannot see the printer, "
            "start, stop or change anything yourself. Every printer action is a command that asks the "
            "operator to press a button first. Answer in at most two short sentences. If one of these "
            "commands does what the person asks, name it in \"command\"; otherwise use null. Never invent "
            "commands, G-code or settings.\n\n" + help_text +
            '\n\nReply with JSON only: {"reply": "<text>", "command": "<command or null>"}')


def _parse(content):
    text = str(content or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return str(data.get("reply") or "").strip(), data.get("command")
        except ValueError:
            pass
    return text, None


def ask(text, help_text, *, cfg=None, opener=None):
    """(reply, command or None). Raises on transport errors; the caller falls back to the help text."""
    llm = (cfg or _config.cfg())["llm"]
    key = os.environ.get(str(llm.get("api_key_env") or "")) or ""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({
        "model": llm["model"],
        "messages": [{"role": "system", "content": system_prompt(help_text)},
                     {"role": "user", "content": str(text)[:2000]}],
        "max_tokens": 200,
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(llm["chat_url"], data=body, headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=float(llm.get("timeout_s") or 20)) as resp:
        data = json.loads(resp.read())
    reply, command = _parse(data["choices"][0]["message"]["content"])
    return reply[:1500], allowed_command(command)
