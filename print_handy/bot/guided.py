"""Guided setup and read-only debugging for ``/print-handy`` (#162).

Two flows, both driven by plain Discord messages:

- ``/print-handy setup`` walks a fresh install through small validated steps. Progress is a
  checklist persisted as JSON in the state folder, so cancelling, a timeout, or a bot restart
  resumes where the person left off. Nothing is written to the real configuration until the
  person explicitly replies ``apply`` at the review step; ``manual`` returns precise steps
  instead. The Discord bot token is never collected in chat: the token step only explains
  where to store it (the environment variable named by ``[discord] token_env``, in the
  profile/data folder ``.env`` with mode 600) and records that it was stored.
- ``/print-handy debugging`` runs read-only diagnostics: configuration validity, bot mode and
  Discord login readiness, channel/user permission prerequisites, service connectivity (GET
  probes only), required executables and versions, and state/log locations. It never calls a
  printer operation, never edits configuration, and never restarts anything. Every line of
  output passes through :func:`redact`, which removes secret values, private filesystem
  paths, and long numeric identifiers.

- ``/print-handy add-printer`` (#537) onboards another printer onto an already-configured
  install, on the same checklist machinery: model, then the display name the shared-channel
  picker shows, then the connection fields of that model's domain, then a live connection
  probe (a step type of its own — :class:`ProbeStep` — that runs the per-domain proof from
  ``core.connection_probe`` and loops back to the field that failed). Secrets are never
  collected in chat: a step names the environment variable to store the value in and the
  person replies when it is stored, exactly like the token step. Nothing is written until
  ``apply`` at review; the write itself is the roster writer (which converts a legacy
  single-printer config), followed by the printer's first hardware record. The flow never
  restarts anything — it says plainly that a restart brings the new printer live.
  A passed test also runs the read-only thermal probe (#741, ``core.thermal_probe``);
  facts it finds that the model record does not already carry become one more step,
  where the person confirms or declines each, and only confirmed ones are written to
  the printer's hardware record at apply.

Printer operations and print authorization are deliberately out of scope for these flows.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__, printers, settings

logger = logging.getLogger(__name__)

STORE_NAME = "guided-setup.json"
# A saved checklist survives this long. After it, a plain reply is not consumed as an answer;
# ``/print-handy setup`` resumes the saved answers and says the session had timed out.
SESSION_TIMEOUT_S = 24 * 3600

HELP = """**Print Handy setup and debugging** — neither flow prints, moves, or authorizes a printer.
- `/print-handy setup`: guided setup in small steps. Your checklist is saved, so you can stop and resume later. At the review step, reply `add` to set up more printers — each gets its own channel and state folder.
- `/print-handy add-printer`: guided onboarding of another printer onto this install — name, connection, a live test of the printer, then your approval. Secrets are never collected in chat. `/print-handy add-printer restart` discards a saved onboarding checklist and starts over.
- `/print-handy debugging`: read-only checks (configuration, bot login, permissions, connectivity, executables, state and logs). Secrets and private paths are redacted.
- `/print-handy bundle`: write a redacted diagnostics bundle (versions, configuration summary, doctor output, recent log tail) into the state folder, ready to attach to a bug report. Nothing is uploaded.
- `/print-handy debug on [minutes]` / `off` / `status`: debug log mode (#478) — temporary DEBUG capture (30 minutes by default, then it turns itself off). Turning it off delivers the sanitized log export for the session here in chat, with its sanitization report. Logging only; printing is unaffected.
- `/print-handy status`: show the saved setup checklist.
- `/print-handy cancel`: pause setup; the saved checklist is kept for resuming.
While setup is waiting for an answer, reply with the answer as a plain message."""


# ---------------------------------------------------------------------- redaction

_TOKEN_SHAPE = re.compile(r"\b[MN][A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b")
_SECRET_PAIR = re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?code)\b\s*[:=]\s*\S+")
_LONG_ID = re.compile(r"\b\d{17,20}\b")
_ABS_PATH = re.compile(r"(?<![:/\w])/(?:[\w.~-]+/)+[\w.~-]*")


def redact(text, *, secrets=(), roots=()):
    """Remove secret values, private paths, and long numeric identifiers from ``text``."""
    out = str(text or "")
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 4:
            out = out.replace(secret, "[redacted]")
    for root in roots:
        root = str(root or "")
        if len(root) > 1:
            out = out.replace(root, "<local path>")
    out = _TOKEN_SHAPE.sub("[redacted]", out)
    out = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}=[redacted]", out)
    out = _LONG_ID.sub(lambda m: "…" + m.group(0)[-2:], out)
    out = _ABS_PATH.sub("<local path>", out)
    return out


def _env_secrets(environ):
    env = os.environ if environ is None else environ
    return [v for k, v in env.items()
            if any(word in k.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY")) and v]


# ---------------------------------------------------------------------- setup steps

def _validate_model(value, _answers):
    key = value.strip()
    if key not in printers.MODELS:
        raise ValueError(f"unknown model key {key!r}; reply with one of: {', '.join(sorted(printers.MODELS))}")
    return key


def _one_id(value, label):
    from ..setup_wizard import _ids
    error = _ids(value)
    if error:
        raise ValueError(error)
    parts = [p for p in re.split(r"[,\s]+", value.strip()) if p]
    if len(parts) != 1:
        raise ValueError(f"reply with exactly one numeric {label} ID")
    return parts[0]


def _validate_chat_id(value, _answers):
    return _one_id(value, "channel")


def _validate_user_ids(value, _answers):
    from ..setup_wizard import _ids
    if value.strip().casefold() in ("everyone", "anyone", "all"):
        return ""  # channel mode (#386): the print channel is the access boundary
    error = _ids(value)
    if error:
        raise ValueError(error)
    if not value.strip():
        raise ValueError("reply with user IDs, or `everyone` to let anyone in the print channel use the bot")
    return value.strip()


def _connection_of(model_key):
    model = printers.MODELS.get(model_key or "")
    return model.vendor.connection if model is not None else "moonraker"


def _printer_url_prompt(model_key):
    """What the checklist should ask for, based on the model already chosen."""
    conn = _connection_of(model_key)
    if conn == "octoprint":
        return ("Reply with the printer's URL. For OctoPrint that is the OctoPrint "
                "address, including an OctoPi that is not the printer (for example "
                "http://octopi.local). Reply `skip` to leave it unset.")
    if conn in ("bambulab_lan", "prusalink", "flashforge_stock", "elegoo_sdcp"):
        return ("Reply with the printer's hostname or IP. Not a full URL, and no port "
                "(for example printer.local or 192.168.1.50). Reply `skip` to leave it unset.")
    if conn == "moonraker":
        return ("Reply with the printer's URL. For Klipper this is Moonraker "
                "(for example http://printer.local:7125), including a Klipper host "
                "that is not the printer itself. Reply `skip` to leave it unset.")
    return "This model has no printer address. Reply `skip`."


def _address_placement(model_key, address):
    """Where a non-empty Printer URL is written: (section, key), or None."""
    if not address:
        return None
    conn = _connection_of(model_key)
    if conn == "moonraker":
        return ("services", "moonraker_url")
    if conn == "octoprint":
        return ("services", "octoprint_url")
    if conn == "bambulab_lan":
        return ("bambu", "lan_host")
    if conn == "prusalink":
        return ("prusa", "link_host")
    if conn == "flashforge_stock":
        return ("flashforge_stock", "host")
    if conn == "elegoo_sdcp":
        return ("elegoo_sdcp", "host")
    return None


def _validate_service_url(value, answers):
    text = value.strip()
    if text.lower() in ("", "skip", "none"):
        return ""
    conn = _connection_of(answers.get("model"))
    if conn in ("bambulab_lan", "prusalink", "flashforge_stock", "elegoo_sdcp"):
        from ..setup_wizard import _host
        error = _host(text)
        if error:
            raise ValueError(f"Printer URL {error}, or reply `skip`")
        return text
    if conn not in ("moonraker", "octoprint"):
        raise ValueError("this printer model has no printer address; reply `skip`")
    from ..setup_wizard import _url
    error = _url(text)
    if error:
        raise ValueError(f"Printer URL {error}, or reply `skip`")
    return text


def _validate_spoolman_url(value, _answers):
    text = value.strip()
    if text.lower() in ("", "skip", "none"):
        return ""
    # Spoolman is printer-independent (unlike Moonraker above): every model can use it,
    # so there is no model-connection refusal here.
    from ..setup_wizard import _url
    error = _url(text)
    if error:
        raise ValueError(f"Spoolman URL {error}, or reply `skip`")
    return text


PLATFORMS = ("discord", "telegram", "matrix")

PLATFORM_TOKEN_INFO = {
    "discord": ("DISCORD_BOT_TOKEN", "Create the bot in the Discord developer portal"),
    "telegram": ("TELEGRAM_BOT_TOKEN", "Create the bot by talking to @BotFather in Telegram"),
    "matrix": ("MATRIX_ACCESS_TOKEN", "Sign in as the bot's Matrix user and copy its access token"),
}


def _validate_platform(value, _answers):
    text = value.strip().casefold()
    by_number = {"1": "discord", "2": "telegram", "3": "matrix"}
    if text in by_number:
        return by_number[text]
    if text in PLATFORMS:
        return text
    raise ValueError("reply `discord`, `telegram`, or `matrix` (or 1, 2, or 3)")


def _validate_chat_id_platform(value, answers):
    platform = (answers or {}).get("platform") or "discord"
    if platform == "matrix":
        text = value.strip()
        if not text.startswith("!") or ":" not in text:
            raise ValueError("reply with the Matrix room ID, which looks like "
                             "`!abc123:example.org` (Matrix Settings > Advanced in most clients)")
        return text
    if platform == "telegram":
        # Telegram group and channel IDs are negative numbers, which
        # the Discord-shaped ID check rejects; validate the digits and
        # keep the sign.
        text = value.strip()
        digits = text[1:] if text.startswith("-") else text
        _one_id(digits, "channel")
        return text
    return _one_id(value, "channel")


def _validate_user_ids_platform(value, answers):
    platform = (answers or {}).get("platform") or "discord"
    if platform == "matrix":
        if value.strip().casefold() in ("everyone", "anyone", "all"):
            return ""
        parts = [part.strip() for part in value.split(",") if part.strip()]
        if not parts or any(not part.startswith("@") or ":" not in part for part in parts):
            raise ValueError("reply with Matrix user IDs like `@you:example.org`, separated "
                             "by commas, or `everyone` for anyone in the room")
        return ", ".join(parts)
    return _validate_user_ids(value, answers)


def _validate_homeserver_url(value, _answers):
    from ..setup_wizard import _url
    error = _url(value.strip())
    if error:
        raise ValueError(f"Homeserver URL {error}")
    return value.strip()


def _validate_camera_url(value, _answers):
    text = value.strip()
    if text.lower() in ("", "skip", "none"):
        return ""
    from ..setup_wizard import _url
    error = _url(text)
    if error:
        raise ValueError(f"Camera snapshot URL {error}, or reply `skip`")
    return text


def _validate_timelapse(value, _answers):
    text = value.strip().casefold()
    if text in ("on", "yes"):
        return "on"
    if text in ("off", "no"):
        return "off"
    if text in ("skip", ""):
        return ""
    raise ValueError("reply `on`, `off`, or `skip` to keep the current timelapse setting")


def _validate_webui_login(value, _answers):
    text = value.strip().casefold()
    if text in ("password", "open", "clear", "skip"):
        return text
    raise ValueError("reply `password` to set a Web UI login, `open` to leave the Web UI "
                     "open, `clear` to remove a saved login, or `skip` to leave it as it is")


def _validate_webui_username(value, _answers):
    from ..webui import access as _access
    try:
        return _access.clean_username(value)
    except _access.AccessError as exc:
        raise ValueError(str(exc)) from exc


def _validate_secret_step(value, _answers):
    # Password and confirmation answers are intercepted in handle_text
    # and never reach this validator through the session store; the
    # marker is all a session may ever hold for them.
    return True


def _token_prompt(platform, token_env):
    _default_env, how = PLATFORM_TOKEN_INFO.get(platform or "discord",
                                                PLATFORM_TOKEN_INFO["discord"])
    return (f"{how} and store its token as the environment variable ${token_env} in the "
            f"profile/data folder's `.env` file (file mode 600). Never paste the token in "
            f"chat; Print Handy only ever reads ${token_env}. Reply `stored` when it is "
            f"saved, or `skip` to do it later.")


def setup_token_env(cfg, session, source_platform):
    """The token variable the setup token step should name.

    Setup can configure a different chat app from the one it is running
    in, so the session's chosen platform decides; the platform the chat
    arrived on only applies until a choice has been made. ``cfg`` may be
    None when the settings do not load.
    """
    answers = (session or {}).get("answers") or {}
    platform = answers.get("platform") or source_platform or "discord"
    env_key = "access_token_env" if platform == "matrix" else "token_env"
    try:
        configured = cfg[platform].get(env_key)
    except (AttributeError, KeyError, TypeError):
        configured = None
    default, _how = PLATFORM_TOKEN_INFO.get(platform, PLATFORM_TOKEN_INFO["discord"])
    return str(configured or default)


def _no_delete_login_prompt(step_key):
    """A Web UI login step's wording for a chat that cannot delete messages."""
    if step_key == "webui_login":
        return ("The Web UI is Print Handy's browser page. Reply `password` to set its login "
                "now (this chat cannot delete messages, so the password you send would stay "
                "visible in the chat history; the Web UI walkthrough's Login step avoids "
                "that), `open` to leave it open on your network, `clear` to remove a login "
                "saved earlier, or `skip` to leave it as it is.")
    if step_key == "webui_password":
        from . import webui_access
        return ("The new Web UI password needs at least 8 characters; a few unrelated words "
                "work well. " + webui_access.capture_notice(False)
                + " Spaces at the very start or end are trimmed in chat, so leave them off.")
    return ("Reply with the same password once more to confirm it. This chat cannot delete "
            "messages, so this one stays visible in the chat history too; the login is "
            "saved as soon as it matches.")


def _chat_id_prompt(platform):
    if platform == "telegram":
        return ("Reply with the print chat's numeric ID. In Telegram, add the bot to the "
                "chat, send a message, and read the chat ID from the bot's updates or a "
                "chat-ID helper bot (group IDs are negative numbers).")
    if platform == "matrix":
        return ("Reply with the print room's ID, which looks like `!abc123:example.org`. "
                "Most Matrix clients show it under the room's Settings > Advanced.")
    return ("Reply with the print channel's numeric ID. In Discord: User Settings > "
            "Advanced > Developer Mode on, then right-click the channel > Copy Channel ID.")


def _user_ids_prompt(platform):
    if platform == "telegram":
        return ("Reply with the numeric Telegram user IDs allowed to use Print Handy, "
                "separated by commas (a chat-ID helper bot can show yours) — or reply "
                "`everyone` to let anyone who can post in the print chat use it.")
    if platform == "matrix":
        return ("Reply with the Matrix user IDs allowed to use Print Handy, like "
                "`@you:example.org`, separated by commas — or reply `everyone` to let "
                "anyone in the print room use it.")
    return ("Reply with the numeric Discord user IDs allowed to use Print Handy, separated by "
            "commas (right-click a user > Copy User ID, with Developer Mode on) — or reply "
            "`everyone` to let anyone who can post in the print channel use it.")


def _validate_token_ack(value, _answers):
    text = value.strip().lower()
    if text == "stored":
        return True
    if text == "skip":
        return False
    raise ValueError("reply `stored` once the token is saved in that environment variable, or `skip` to "
                     "do it later. Never paste the token here: it is not collected in chat.")


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    prompt: str
    validate: object  # callable(value, answers) -> stored value, raising ValueError
    # callable(answers) -> bool: whether the step belongs to this session given the
    # answers so far; None means always. The flow reads it from the table (#1323).
    applies: object = None


@dataclass(frozen=True)
class ProbeOutcome:
    """The result of one probe-step run: what to tell the person, and — on
    failure — which earlier step `back` returns to (the field that failed).

    ``extra`` carries read-only facts the review step shows (Moonraker
    nozzle, bed, and Z). It is never written into the roster by itself.
    """
    ok: bool
    text: str
    back_key: str | None = None
    extra: dict | None = None


@dataclass(frozen=True)
class ProbeStep:
    """A step that runs an action instead of collecting an answer (#537).

    A connection test is not an answer: it takes seconds, is domain-specific,
    and its failures must loop back to the field that caused them rather than
    abort the flow — none of which the answer-shaped :class:`Step` expresses.
    ``run`` takes the answers collected so far and returns a
    :class:`ProbeOutcome`. The step is answered by the flow's probe protocol
    (``test`` / ``retry`` / ``back`` / ``offline``), implemented by the
    onboarding handler below; any future flow can reuse the type the same way.
    """
    key: str
    label: str
    prompt: str
    run: object  # callable(answers) -> ProbeOutcome


def _on_matrix(answers):
    return answers.get("platform") == "matrix"


def _password_login(answers):
    return answers.get("webui_login") == "password"


def steps(token_env="DISCORD_BOT_TOKEN"):
    model_list = ", ".join(sorted(printers.MODELS))
    return (
        Step("platform", "Chat platform",
             "Which chat app should Print Handy talk in? Reply `discord`, `telegram`, or "
             "`matrix` (or 1, 2, or 3). The rest of setup asks only that app's questions.",
             _validate_platform),
        Step("model", "Printer model",
             f"Which printer? Reply with its model key, one of: {model_list}.", _validate_model),
        Step("chat_id", "Print channel",
             "Reply with the print channel's numeric ID.", _validate_chat_id_platform),
        Step("user_ids", "Allowed users",
             "Reply with the user IDs allowed to use Print Handy.", _validate_user_ids_platform),
        Step("homeserver_url", "Matrix homeserver",
             "Reply with your Matrix homeserver URL (for example https://matrix.example.org).",
             _validate_homeserver_url, applies=_on_matrix),
        Step("moonraker_url", "Printer URL (optional)",
             "Reply with the printer's URL, or `skip`. The question names the right "
             "kind of address for the model you already picked.", _validate_service_url),
        Step("spoolman_url", "Spoolman URL (optional)",
             "If you run Spoolman for filament inventory, reply with its URL (for example "
             "http://spoolman.local:7912; the Spoolman included in the Docker stack is "
             "http://spoolman:8000). Otherwise reply `skip`.", _validate_spoolman_url),
        Step("camera_snapshot_url", "Camera snapshot URL (optional)",
             "If the printer has a camera, reply with its snapshot URL and finished prints "
             "can be watched and recorded. Otherwise reply `skip`.", _validate_camera_url),
        Step("timelapse", "Timelapse (optional)",
             "Reply `on` to record a timelapse of finished prints, `off` for none, or "
             "`skip` to keep the current setting.", _validate_timelapse),
        Step("token_stored", "Bot token stored securely",
             _token_prompt("discord", token_env), _validate_token_ack),
        Step("webui_login", "Web UI login (optional)",
             "The Web UI is Print Handy's browser page. Reply `password` to set its login "
             "now (the password message is deleted right after you send it and is never "
             "saved in this checklist), `open` to leave it open on your network, `clear` "
             "to remove a login saved earlier, or `skip` to leave it as it is.",
             _validate_webui_login),
        Step("webui_username", "Web UI username",
             "Reply with the username for the Web UI login.", _validate_webui_username,
             applies=_password_login),
        Step("webui_password", "Web UI password",
             "Reply with the new Web UI password (at least 8 characters; a few unrelated "
             "words work well). It is deleted from chat as soon as it is captured and is "
             "never saved in this checklist or repeated back. Spaces at the very start "
             "or end are trimmed in chat, so leave them off.", _validate_secret_step,
             applies=_password_login),
        Step("webui_confirm", "Confirm Web UI password",
             "Reply with the same password once more to confirm it. It is deleted from "
             "chat as soon as it is captured; the login is saved as soon as it matches.",
             _validate_secret_step, applies=_password_login),
    )


def _step_applies(step_key, answers):
    """Whether a step belongs to this session, given the answers so far: the step's
    ``applies`` in the table; a step without one, or a key not in it, always applies."""
    answers = answers or {}
    step = next((s for s in steps() if s.key == step_key), None)
    return step is None or step.applies is None or step.applies(answers)


def _applicable_steps(answers, token_env="DISCORD_BOT_TOKEN"):
    return [step for step in steps(token_env) if _step_applies(step.key, answers)]


# Password answers live only in this in-memory map, keyed by session
# key, between the password step and its confirmation. They are never
# written to the session store, never logged, and never echoed. Each
# value is (password, captured_at), so an abandoned one can be dropped.
_pending_webui_passwords = {}


def _purge_stale_webui_passwords(now):
    """Forget every captured password older than the session timeout."""
    for key, (_password, captured_at) in list(_pending_webui_passwords.items()):
        if now - captured_at > SESSION_TIMEOUT_S:
            _pending_webui_passwords.pop(key, None)


STEP_LABELS = {s.key: s.label for s in steps()}


# ---------------------------------------------------------------------- add-a-printer loop (#337)

def _taken_ids(answers):
    ids = {p["id"] for p in answers.get("printers") or [] if isinstance(p, dict) and p.get("id")}
    if answers.get("printer_id"):
        ids.add(answers["printer_id"])
    return ids


def _validate_first_id(value, answers):
    from ..setup_wizard import _printer_id_error
    error = _printer_id_error(value.strip(), _taken_ids(answers))
    if error:
        raise ValueError(error)
    return value.strip()


def _validate_new_id(value, answers):
    from ..setup_wizard import _printer_id_error
    error = _printer_id_error(value.strip(), _taken_ids(answers))
    if error:
        raise ValueError(error)
    return value.strip()


def _validate_new_chat_id(value, _answers):
    # An added printer always needs its OWN channel in this flow: the main
    # channel is the first printer's fallback claim, and two entries claiming
    # one channel are refused at load (design §2.3).
    return _one_id(value.strip(), "channel")


def _add_step_defs():
    model_list = ", ".join(sorted(printers.MODELS))
    return {
        "first_id": Step("first_id", "First printer's id",
                         "Your first printer needs an id too — it names its state folder and "
                         "binds its channel. Reply with a short slug (lowercase letters, digits, "
                         "- or _), for example `garage-a1`.", _validate_first_id),
        "id": Step("id", "Printer id",
                   "Give the new printer an id (a short slug, for example `workshop-printer`). "
                   "It names its state folder and binds its channel.", _validate_new_id),
        "model": Step("model", "Printer model",
                      f"Which printer? Reply with its model key, one of: {model_list}.",
                      _validate_model),
        "chat_id": Step("chat_id", "Printer channel",
                        "Reply with the new printer's own channel ID. It cannot share the main "
                        "print channel — that one is the first printer's, and approvals in a "
                        "channel can only start that channel's printer.", _validate_new_chat_id),
        "moonraker_url": Step("moonraker_url", "Printer URL (optional)",
                              "Reply with the printer's URL, or `skip`.",
                              _validate_service_url),
    }


def _add_keys(session):
    """The step keys of one add-a-printer cycle, frozen when the cycle starts.

    The first cycle also asks the first printer's id (the base flow never needed
    one); freezing the list keeps the step index stable as answers land."""
    keys = ["id", "model", "chat_id", "moonraker_url"]
    if "printer_id" not in (session.get("answers") or {}):
        keys.insert(0, "first_id")
    return keys


def _add_prompt_text(session, note=""):
    keys = session["add_keys"]
    step = _add_step_defs()[keys[session["add_step"]]]
    prompt = step.prompt
    if step.key == "moonraker_url":
        draft = session.get("draft") or {}
        prompt = _printer_url_prompt(draft.get("model") or (session.get("answers") or {}).get("model"))
    head = f"**Add a printer ({session['add_step'] + 1} of {len(keys)}): {step.label}**\n{prompt}"
    return (note + "\n" if note else "") + head + "\n\n" + checklist_text(session) + \
        "\n\nReply with the answer, or `/print-handy cancel` to pause (your checklist is saved)."


# ---------------------------------------------------------------------- printer onboarding (#537)
#
# A second flow on the same checklist machinery: add one printer to an
# already-configured install. The step list is not static — it is derived from
# the answers after every answer (the model's vendor decides the domain, the
# domain decides the fields, a gate step prunes the rest), and the flow always
# points at the first unanswered step. Secrets follow the house rule: a step
# names the environment variable to store the value in, and the person replies
# when it is stored; values are never collected, stored in the session, or
# echoed. The connection is proven by a ProbeStep before review, and nothing
# is written until `apply` — the roster writer plus the first hardware record.

def _bambu_env_name(printer_id):
    """The per-printer LAN access code variable (setup_wizard.py: the same
    derivation the wizard writes into the roster entry)."""
    from ..setup_wizard import secret_env_form
    return "BAMBU_LAN_PASSWORD_" + secret_env_form(printer_id)


def _prusa_env_name(printer_id):
    """The per-printer PrusaLink API key variable (same derivation rule)."""
    from ..setup_wizard import secret_env_form
    return "PRUSA_LINK_API_KEY_" + secret_env_form(printer_id)


def _derive_printer_id(name, taken_ids):
    """The roster id for a chosen display name: the settings loader's slug
    rule, uniquified with a numeric suffix (ids are append-stable keys).
    A candidate whose secret-name form collides with a taken id's
    (garage-a1 vs garage_a1, P2-138) is skipped like an exact clash."""
    from ..setup_wizard import secret_env_form
    taken_forms = {secret_env_form(taken) for taken in taken_ids}
    base = settings._printer_id_slug(name)
    candidate, suffix = base, 2
    while candidate in taken_ids or secret_env_form(candidate) in taken_forms:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _validate_ob_name(value, answers):
    label = printers.MODELS[answers["model"]].label
    name = label if value.strip().casefold() == "default" else value.strip()
    if not name:
        raise ValueError("reply with a name for this printer, or `default` for "
                    f"{label!r}")
    if len(name) > 48 or name.startswith("/") or "\n" in name:
        raise ValueError("keep the name short (at most 48 characters) and free of "
                    "newlines")
    taken = {str(n).casefold() for n in answers["__existing"]["names"]}
    if name.casefold() in taken:
        raise ValueError(f"one of your printers is already called {name!r} — in a "
                    "shared channel the name is how prints are told apart, so "
                    "reply with a different name")
    return name


def _validate_ob_host(value, answers):
    """A plain host, or blank/`skip` for an offline entry (stored as '')."""
    host = value.strip()
    if not host or host.casefold() == "skip":
        return ""
    from .. import setup_wizard
    error = setup_wizard._host(host)
    if error:
        raise ValueError(error)
    return host


def _validate_ob_lan_host(value, answers):
    host = value.strip()
    from .. import setup_wizard
    error = setup_wizard._host(host)
    if error:
        raise ValueError(error)
    return host


def _validate_ob_lan_serial(value, answers):
    serial = value.strip()
    from .. import setup_wizard
    error = setup_wizard._serial(serial)
    if error:
        raise ValueError(error)
    return serial


def _validate_yes_no(value, answers):
    low = value.strip().casefold()
    if low in ("yes", "y"):
        return True
    if low in ("no", "n"):
        return False
    raise ValueError("reply `yes` or `no`")


def _validate_secret_stored(value, answers):
    """The token step's posture: `stored` asserts the value is in the .env
    under the named variable; `skip` records that it is not (which, for a
    required secret, moves the printer to the unverified path)."""
    low = value.strip().casefold()
    if low == "stored":
        return True
    if low == "skip":
        return False
    raise ValueError("reply `stored` once the value is in the .env under that name "
                "(never paste the value here), or `skip`")


def _validate_trust_ack(value, answers):
    if value.strip().casefold() in ("ok", "yes", "continue"):
        return True
    raise ValueError("reply `ok` to continue on a trusted network, or "
                "`/print-handy cancel` to stop")


def _onboard_keys(answers):
    """The step list implied by the answers so far. The probe step appears
    only when the connection is complete enough to test (fields present and,
    where the domain has one, its secret confirmed stored); otherwise the
    flow goes straight to review and says the entry is unverified."""
    keys = ["model", "name"]
    model_key = answers.get("model")
    if not model_key:
        return keys
    conn = printers.MODELS[model_key].vendor.connection
    if conn == "moonraker":
        keys.append("moonraker_url")
        if answers.get("moonraker_url"):
            if model_key == "flashforge.ad5x":
                keys.append("zmod_secrets")
            keys.append("probe")
    elif conn == "bambulab_lan":
        keys.append("lan_gate")
        if answers.get("lan_gate"):
            keys += ["lan_host", "lan_serial", "lan_secret"]
            if answers.get("lan_secret"):
                keys.append("probe")
    elif conn == "prusalink":
        keys.append("link_host")
        if answers.get("link_host"):
            keys.append("prusa_secret")
            if answers.get("prusa_secret"):
                keys.append("probe")
    elif conn == "elegoo_sdcp":
        keys.append("host")
        if answers.get("host"):
            keys += ["sdcp_trust", "probe"]
    elif conn == "octoprint":
        keys.append("octoprint_url")
        if answers.get("octoprint_url"):
            keys.append("octo_secret")
            if answers.get("octo_secret"):
                keys.append("probe")
    elif conn == "flashforge_stock":
        keys.append("host")
        if answers.get("host"):
            keys.append("stock_secrets")
            if answers.get("stock_secrets"):
                keys.append("probe")
    if "probe" in keys and _thermal_proposals(answers):
        keys.append("thermal")
    return keys


def _thermal_proposals(answers):
    """The thermal facts a passed connection test proposed (#741), or []."""
    probe = answers.get("probe") or {}
    thermal = probe.get("thermal") if probe.get("ok") else None
    return list((thermal or {}).get("proposals") or [])


def _validate_thermal(value, answers):
    """`all`, `none`, or the numbers of the proposals to confirm. Returns the confirmed proposals."""
    items = _thermal_proposals(answers)
    low = value.strip().casefold()
    if low in ("all", "yes", "y"):
        return items
    if low in ("none", "no", "n", "skip"):
        return []
    picked = [p for p in re.split(r"[,\s]+", low) if p]
    if not picked or not all(p.isdigit() and 1 <= int(p) <= len(items) for p in picked):
        raise ValueError(f"reply `all`, `none`, or numbers from 1 to {len(items)} separated by commas")
    return [items[i - 1] for i in sorted({int(p) for p in picked})]


_STEP_LABELS_OB = {
    "model": "Printer model", "name": "Display name",
    "moonraker_url": "Printer URL", "zmod_secrets": "Stock side-channel secrets",
    "lan_gate": "LAN connection", "lan_host": "Printer URL",
    "lan_serial": "Printer serial", "lan_secret": "LAN access code (stored, never pasted)",
    "link_host": "Printer URL", "prusa_secret": "API key (stored, never pasted)",
    "host": "Printer URL", "sdcp_trust": "Network trust",
    "octoprint_url": "Printer URL", "octo_secret": "API key (stored, never pasted)",
    "stock_secrets": "Serial + check code (stored, never pasted)",
    "probe": "Live connection test",
    "thermal": "Thermal facts from the printer",
}


def _onboard_step_defs(session, probe_runner=None):
    """The step definitions for the session's domain, prompts interpolated
    with the derived id / secret variable names, rebuilt per render."""
    answers = session["ob_answers"]
    existing = session["ob_existing"]
    pid = answers.get("printer_id") or "printer"
    model_key = answers.get("model")
    label = printers.MODELS[model_key].label if model_key else ""
    bambu_env = _bambu_env_name(pid)
    prusa_env = _prusa_env_name(pid)
    env_hint = (
        "Store it in the profile/data folder's `.env` file (file mode 600), "
        "the same place this bot's own secrets live.")
    model_list = ", ".join(sorted(printers.MODELS))
    defs = {
        "model": Step("model", _STEP_LABELS_OB["model"],
                      f"Which printer? Reply with its model key, one of: {model_list}.",
                      _validate_model),
        "name": Step("name", _STEP_LABELS_OB["name"],
                     "What should this printer be called? The name is what the "
                     "which-printer picker and every announcement in this shared "
                     "channel show, and it must differ from your other printers' "
                     f"names. Reply with a name, or `default` for {label!r}.",
                     _validate_ob_name),
        "moonraker_url": Step("moonraker_url", _STEP_LABELS_OB["moonraker_url"],
                              "Reply with the printer's URL. For Klipper this is Moonraker "
                              "(for example http://printer.local:7125), including a host that "
                              "is not the printer itself. To add the printer offline "
                              "instead — slicing only, nothing tested — reply `skip`.",
                              _validate_service_url),
        "zmod_secrets": Step(
            "zmod_secrets", _STEP_LABELS_OB["zmod_secrets"],
            "Optional: the AD5X's stock side channel (IFS occupancy, the "
            "printer's own start button) uses two secrets, `$AD5X_SERIAL` and "
            "`$AD5X_CHECK_CODE` — this printer's stock-firmware serial number "
            "and check code. They are the same pair this AD5X uses on the "
            "stock Flashforge LAN connection, and one pair serves the whole "
            "install; the other ZMod printers (AD5M, Creator) have no stock "
            "side channel and never use them. " + env_hint +
            " Reply `stored` if they are saved (the "
            "main Moonraker connection does not need them), or `skip`.",
            _validate_secret_stored),
        "lan_gate": Step(
            "lan_gate", _STEP_LABELS_OB["lan_gate"],
            "Connect this Bambu Lab printer over your LAN? It must be in LAN "
            "Mode — in LAN Mode the Bambu Handy app cannot reach it. Reply "
            "`yes` to set up the live connection, or `no` to add the printer "
            "as an offline entry (slicing only; nothing is tested).",
            _validate_yes_no),
        "lan_host": Step("lan_host", _STEP_LABELS_OB["lan_host"],
                         "Reply with the printer's IP address or hostname on "
                         "your LAN (plain host — no http://, no port).",
                         _validate_ob_lan_host),
        "lan_serial": Step("lan_serial", _STEP_LABELS_OB["lan_serial"],
                           "Reply with the printer's serial number (on the "
                           "printer: Settings > Device, or the WLAN page).",
                           _validate_ob_lan_serial),
        "lan_secret": Step(
            "lan_secret", _STEP_LABELS_OB["lan_secret"],
            "The printer's LAN access code is a secret and is never collected "
            f"here. Store it as `${bambu_env}` — that per-printer name keeps "
            "it separate from your other Bambu printers. " + env_hint + " "
            "Reply `stored` when it is saved (the connection test uses it "
            "next), or `skip` to save the printer unverified instead.",
            _validate_secret_stored),
        "link_host": Step(
            "link_host", _STEP_LABELS_OB["link_host"],
            "Reply with the printer's IP address or hostname (plain host, no "
            "http:// and no port) — or reply `skip` to add it as an offline "
            "printer.", _validate_ob_host),
        "prusa_secret": Step(
            "prusa_secret", _STEP_LABELS_OB["prusa_secret"],
            "The PrusaLink API key is a secret and is never collected here. "
            f"Store it as `${prusa_env}` — that per-printer name keeps it "
            "separate from your other Prusa printers. " + env_hint + " Reply "
            "`stored` when it is saved (the connection test uses it next), or "
            "`skip` to save the printer unverified instead.",
            _validate_secret_stored),
        "host": Step("host", _STEP_LABELS_OB["host"],
                     "Reply with the printer's IP address or hostname (plain "
                     "host, no http:// and no port) — or reply `skip` to add "
                     "it as an offline printer.", _validate_ob_host),
        "sdcp_trust": Step(
            "sdcp_trust", _STEP_LABELS_OB["sdcp_trust"],
            "One caution before the test: SDCP has no password or key at all "
            "— anyone who can reach the printer on the network can talk to "
            "it. Only continue on a network you trust. Reply `ok` to continue.",
            _validate_trust_ack),
        "octoprint_url": Step(
            "octoprint_url", _STEP_LABELS_OB["octoprint_url"],
            "Reply with the printer's URL. For OctoPrint that is the OctoPrint "
            "address, including an OctoPi that is not the printer (for example "
            "http://octopi.local). Reply `skip` to add the printer offline.",
            lambda value, answers: ("" if value.strip().casefold() in ("", "skip")
                                    else _url_or_err(value))),
        "octo_secret": Step(
            "octo_secret", _STEP_LABELS_OB["octo_secret"],
            "OctoPrint's API key is a secret and is never collected here. "
            "Store it as `$OCTOPRINT_API_KEY`. " + env_hint +
            (" Note: that is one shared name — the OctoPrint printer you "
             "already have uses it too, so both printers must accept the same "
             "key; a second OctoPrint printer cannot hold a different one "
             "today." if "octoprint" in existing["connections"] else "") +
            " Reply `stored` when it is saved (the connection test uses it "
            "next), or `skip` to save the printer unverified instead.",
            _validate_secret_stored),
        "stock_secrets": Step(
            "stock_secrets", _STEP_LABELS_OB["stock_secrets"],
            "The stock Flashforge API needs two secrets: the printer serial "
            "(`$AD5X_SERIAL`) and its check code (`$AD5X_CHECK_CODE`). They "
            "are never collected here. " + env_hint +
            (" Note: those are shared names — the stock printer you already "
             "have uses them too, so both printers must share one serial and "
             "check code today." if "flashforge_stock" in existing["connections"]
             else "") +
            " Reply `stored` when both are saved (the connection test uses "
            "them next), or `skip` to save the printer unverified instead.",
            _validate_secret_stored),
        "probe": ProbeStep(
            "probe", _STEP_LABELS_OB["probe"],
            "Everything is collected — now the printer is proven live. Reply "
            "`test` to run the connection test against the real printer (it "
            "can take a few seconds). If it fails, the reply says why: "
            "`retry` tests again, `back` returns to the setting that failed, "
            "and `offline` saves the printer unverified instead.",
            probe_runner or _default_onboard_probe),
    }
    proposals = _thermal_proposals(answers)
    if proposals:
        from ..core.thermal_probe import proposal_lines
        defs["thermal"] = Step(
            "thermal", _STEP_LABELS_OB["thermal"],
            f"The connected printer was read (read-only) for the thermal facts the {label} "
            "model record does not settle. Each line below is a proposal from what this "
            "printer itself reports:\n" + "\n".join(proposal_lines(proposals)) +
            "\nReply `all` to record every one for this printer, the numbers to record "
            "only some (for example `1,3`), or `none` to record nothing. Confirmed facts are "
            "saved for this printer only, as operator-confirmed; the model record never changes.",
            _validate_thermal)
    return defs


def _url_or_err(value):
    from .. import setup_wizard
    error = setup_wizard._url(value.strip())
    if error:
        raise ValueError(error)
    return value.strip()


def _onboard_checklist_text(session):
    answers = session["ob_answers"]
    keys = session.get("ob_keys") or _onboard_keys(answers)
    current = session.get("ob_step", 0)
    in_review = session["status"] == "onboard_review"
    lines = []
    for index, key in enumerate(keys):
        label = _STEP_LABELS_OB[key]
        if in_review or key in answers:
            mark = "☑"
        elif index == current:
            mark = "▶"
        else:
            mark = "☐"
        lines.append(f"{mark} {index + 1}. {label}")
    lines.append(f"{'▶' if in_review else '☐'} {len(keys) + 1}. "
                 "Review and apply")
    return "**Checklist**\n" + "\n".join(lines)


def _onboard_prompt_text(session, note=""):
    keys = session.get("ob_keys") or _onboard_keys(session["ob_answers"])
    index = session["ob_step"]
    step = _onboard_step_defs(session)[keys[index]]
    text = f"**Add a printer ({index + 1} of {len(keys)}): {step.label}**\n{step.prompt}"
    if isinstance(step, ProbeStep) and session.get("ob_probe_error"):
        text = (f"**The connection test did not pass.** "
                f"{session['ob_probe_error']}\n\n{text}")
    if note:
        text = f"{note}\n\n{text}"
    return (f"{text}\n\n{_onboard_checklist_text(session)}\n\n"
            "Reply with the answer, or `/print-handy cancel` to pause "
            "(your checklist is saved).")


def _onboard_entry(answers):
    """The roster entry the answers describe: id/model/name always; the
    domain's connection keys only when a connection was actually set up."""
    entry = {"id": answers["printer_id"], "model": answers["model"],
             "name": answers["name"]}
    conn = printers.MODELS[answers["model"]].vendor.connection
    if conn == "moonraker" and answers.get("moonraker_url"):
        entry["moonraker_url"] = answers["moonraker_url"]
    elif conn == "bambulab_lan" and answers.get("lan_gate"):
        entry.update(lan_host=answers["lan_host"],
                     lan_serial=answers["lan_serial"],
                     lan_access_code_env=_bambu_env_name(answers["printer_id"]))
    elif conn == "prusalink" and answers.get("link_host"):
        entry.update(link_host=answers["link_host"],
                     api_key_env=_prusa_env_name(answers["printer_id"]))
    elif conn in ("elegoo_sdcp", "flashforge_stock") and answers.get("host"):
        entry["host"] = answers["host"]
    elif conn == "octoprint" and answers.get("octoprint_url"):
        entry["octoprint_url"] = answers["octoprint_url"]
    return entry


def _default_onboard_probe(answers):
    """Resolve the domain's secrets by name (never values in the session) and
    run the per-domain proof from core.connection_probe."""
    from ..core import connection_probe
    from ..runtime import _config

    conn = printers.MODELS[answers["model"]].vendor.connection
    pid = answers.get("printer_id") or ""
    secrets = {}
    if conn == "moonraker":
        secrets["moonraker_api_key"] = _config.profile_env_value("MOONRAKER_API_KEY")
    elif conn == "bambulab_lan":
        secrets["access_code"] = _config.profile_env_value(_bambu_env_name(pid))
    elif conn == "prusalink":
        secrets["api_key"] = _config.profile_env_value(_prusa_env_name(pid))
    elif conn == "octoprint":
        secrets["api_key"] = _config.profile_env_value("OCTOPRINT_API_KEY")
    elif conn == "flashforge_stock":
        secrets["serial"] = _config.profile_env_value("AD5X_SERIAL")
        secrets["check_code"] = _config.profile_env_value("AD5X_CHECK_CODE")
    result = connection_probe.probe(conn, _onboard_entry(answers), secrets)
    if result.ok:
        extra = {key: result.detail[key] for key in ("geometry", "geometry_error")
                 if result.detail.get(key)}
        extra["thermal"] = _onboard_thermal(conn, answers, secrets)
        return ProbeOutcome(True, result.summary, extra=extra)
    secret_step = {"bambulab_lan": "lan_secret", "prusalink": "prusa_secret",
                   "octoprint": "octo_secret",
                   "flashforge_stock": "stock_secrets"}.get(conn)
    if result.category in ("auth", "missing_secret") and secret_step:
        return ProbeOutcome(False, result.error, secret_step)
    first_step = {"moonraker": "moonraker_url", "bambulab_lan": "lan_host",
                  "prusalink": "link_host", "elegoo_sdcp": "host",
                  "octoprint": "octoprint_url",
                  "flashforge_stock": "host"}.get(conn)
    return ProbeOutcome(False, result.error, first_step)


def _onboard_thermal(conn, answers, secrets):
    """Run the read-only thermal probe (#741) once the connection is proven.
    Returns {"proposals": [...], "note": str}; a failure is a note, never an error."""
    from ..core import thermal_probe
    from ..knowledge import thermal_entry_for_model
    try:
        entry = thermal_entry_for_model(answers["model"])
        result = thermal_probe.probe(conn, _onboard_entry(answers), secrets)
        found = thermal_probe.proposals(result, entry.thermal) if entry is not None else []
        return {"proposals": found, "note": result.note}
    except Exception:
        return {"proposals": [], "note": "the thermal probe did not run; nothing was proposed."}


def _rollback_roster_write(result):
    """Undo a just-committed roster append whose follow-up steps failed
    (P2-218): restore the backup ``add_printer`` kept next to the settings
    file, so the file is exactly what it was before the attempt. A rollback
    failure is logged, never hidden — the original failure is the one the
    operator sees, and the log names the state the file was left in."""
    from ..setup_wizard import _atomic_write
    try:
        text = Path(result["backup"]).read_text(encoding="utf-8")
        _atomic_write(Path(result["path"]), text, 0o644)
    except Exception:
        logger.warning("could not roll back the roster write in %s after a failed "
                       "onboarding apply; the new entry may still be in the file",
                       result["path"], exc_info=True)


def _apply_onboarding(answers):
    """The onboarding apply: roster write (converting a legacy config on the
    way), then the new printer's first hardware record. Raises on failure;
    the handler keeps the session at review so nothing is lost. A failure
    after the append rolls the append back (P2-218): the review's 'nothing
    was written' stays true, and the retry does not die on the duplicate
    id its own first attempt left behind."""
    from .. import roster_write
    from ..core import printer_state

    entry = _onboard_entry(answers)
    result = roster_write.add_printer(entry)
    try:
        view = settings.view_for(entry["id"], settings.load())
        seeded = printer_state.seed_hardware_record(entry["id"], view)
        from ..core import thermal_probe
        confirmed = thermal_probe.record_confirmed(entry["id"], entry["model"], answers.get("thermal"))
    except Exception:
        _rollback_roster_write(result)
        raise
    converted = (" Your existing single-printer settings were converted into "
                 "the roster's first entry on the way."
                 if result["converted"] else "")
    record = (" Its first hardware record was created."
              if seeded else
              " A hardware record already existed for it and was kept.")
    if confirmed:
        record += (f" {confirmed} thermal fact{'s' if confirmed != 1 else ''} confirmed from the "
                   "printer were recorded for it (operator-confirmed).")
    return (f"**{entry['name']}** (`{entry['id']}`) was added to "
            f"{result['path'].name}; the previous file is kept as "
            f"{result['backup'].name}.{converted}{record} "
            "**Restart Print Handy to bring it live** — restart the container, "
            "or the bot and listener services on a standalone install. Nothing "
            "was restarted for you. Until the restart the new printer has no "
            "listener and health checks will report it missing; that is "
            "expected, not a failure. After the restart it serves this channel "
            f"under the name {entry['name']}.")


def _onboard_secret_line(answers):
    conn = printers.MODELS[answers["model"]].vendor.connection
    pid = answers["printer_id"]
    if conn == "bambulab_lan" and answers.get("lan_gate"):
        state = "stored" if answers.get("lan_secret") else "not stored — unverified"
        return f"- Secret: LAN access code in `${_bambu_env_name(pid)}` ({state})"
    if conn == "prusalink" and answers.get("link_host"):
        state = "stored" if answers.get("prusa_secret") else "not stored — unverified"
        return f"- Secret: API key in `${_prusa_env_name(pid)}` ({state})"
    if conn == "octoprint" and answers.get("octoprint_url"):
        state = "stored" if answers.get("octo_secret") else "not stored — unverified"
        return f"- Secret: API key in `$OCTOPRINT_API_KEY` ({state})"
    if conn == "flashforge_stock" and answers.get("host"):
        state = "stored" if answers.get("stock_secrets") else "not stored — unverified"
        return f"- Secrets: `$AD5X_SERIAL` + `$AD5X_CHECK_CODE` ({state})"
    if conn == "moonraker" and answers.get("moonraker_url"):
        side = ("stock side channel stored" if answers.get("zmod_secrets")
                else "stock side channel off")
        return f"- Secrets: none needed for Moonraker itself; {side}"
    return None


def _onboard_geometry_lines(answers, model):
    """Review lines for a Moonraker geometry read (#623).

    A failed read is named. A mismatch warns and offers the generic Klipper
    profile. Neither line changes the model the operator picked, and nothing
    here is written — apply is still a separate reply.
    """
    probe = answers.get("probe") or {}
    if not probe.get("ok"):
        return []
    geometry = probe.get("geometry")
    error = probe.get("geometry_error")
    if not geometry and not error:
        return []
    from ..core.printer_geometry import warning_for
    if error or not isinstance(geometry, dict):
        detail = error or "the printer config did not include nozzle, bed, or Z"
        return ["- Printer reported: could not read nozzle, bed, or Z "
                f"({detail}). Adding the printer continues."]
    bits = []
    if geometry.get("nozzle_mm") is not None:
        bits.append(f"nozzle {float(geometry['nozzle_mm']):g} mm")
    bed = geometry.get("bed_mm")
    if isinstance(bed, (list, tuple)) and len(bed) == 2:
        bits.append(f"bed {float(bed[0]):g} x {float(bed[1]):g} mm")
    if geometry.get("max_z_mm") is not None:
        bits.append(f"max Z {float(geometry['max_z_mm']):g} mm")
    lines = ["- Printer reported: " + ", ".join(bits)
             + ". These numbers are what the printer reported, not a tuned profile."]
    warning = warning_for(model, geometry)
    if warning:
        lines.append(f"- Geometry: {warning}")
    return lines


def _onboard_thermal_lines(answers):
    """Review lines for the thermal probe (#741): what was confirmed, or why
    nothing could be probed, and how many thermal facts stay open questions."""
    probe = answers.get("probe") or {}
    thermal = probe.get("thermal") if probe.get("ok") else None
    if not isinstance(thermal, dict):
        return []
    lines = []
    proposals = thermal.get("proposals") or []
    confirmed = answers.get("thermal") or []
    if proposals:
        names = ", ".join(f"{p['field']} {p['value']}" for p in confirmed) or "none"
        lines.append(f"- Thermal facts from the printer: {len(confirmed)} of {len(proposals)} "
                     f"confirmed ({names}); saved for this printer only, as operator-confirmed")
    elif not thermal.get("note"):
        lines.append("- Thermal probe: the printer reports nothing the model record does not already say")
    if thermal.get("note"):
        lines.append(f"- Thermal probe: {thermal['note']}")
    try:
        from ..knowledge import thermal_entry_for_model, thermal_setup_questions
        entry = thermal_entry_for_model(answers["model"])
        install = {p["field"]: {"value": p["value"]} for p in confirmed}
        questions = thermal_setup_questions(entry, install) if entry is not None else []
    except Exception:
        questions = []
    if questions:
        lines.append("- Thermal facts still unknown for this printer (not assumed): "
                     + "; ".join(q["question"] for q in questions))
    return lines


def _onboard_review_text(session, note=""):
    answers = session["ob_answers"]
    model = printers.MODELS[answers["model"]]
    conn = model.vendor.connection
    entry = _onboard_entry(answers)
    target = (entry.get("moonraker_url") or entry.get("octoprint_url")
              or entry.get("lan_host") or entry.get("link_host")
              or entry.get("host"))
    lines = ["**Ready to add this printer.** Nothing has been written yet.",
             f"- Name: **{answers['name']}** — the name the which-printer "
             "picker and announcements show",
             f"- Id: `{answers['printer_id']}` (settings key and state folder)",
             f"- Model: {model.label} (`{answers['model']}`)",
             f"- Connection: {target or 'none — an offline entry (slicing only; no listener serves it)'}"]
    secret_line = _onboard_secret_line(answers)
    if secret_line:
        lines.append(secret_line)
    probe = answers.get("probe")
    if probe and probe.get("ok"):
        lines.append(f"- Live test: **passed** — {probe['summary']}")
    elif probe:
        lines.append("- Live test: **not passed** "
                     f"({probe.get('reason') or 'the test was skipped'}) — the "
                     "printer is saved with its connection settings but "
                     "unverified; once the cause is fixed it connects at the "
                     "next restart.")
    elif target:
        lines.append("- Live test: not run — the entry is saved unverified "
                     "(its secret step was skipped).")
    lines.extend(_onboard_geometry_lines(answers, model))
    lines.extend(_onboard_thermal_lines(answers))
    lines += ["",
              _onboard_checklist_text(session),
              "",
              "Reply `apply` to write the roster entry into the settings file "
              "(a timestamped backup is kept; a legacy single-printer file is "
              "converted into the roster, and the rewrite does not preserve "
              "comments) and create the printer's first hardware record. "
              "Applying never restarts anything — you restart Print Handy "
              "afterwards to bring the printer live. Reply `manual` for the "
              "exact roster YAML to add by hand instead, reply `back` or a "
              "different model key to change the model, or `/print-handy "
              "cancel` to pause."]
    if note:
        lines.insert(0, note)
    return "\n".join(lines)


def _onboard_manual_text(session):
    answers = session["ob_answers"]
    entry = _onboard_entry(answers)
    import yaml

    snippet = yaml.safe_dump({"printers": [entry]}, sort_keys=False,
                             default_flow_style=False).rstrip()
    return ("Onboarding paused — nothing was written. To add the printer by "
            "hand, put this entry in the `printers:` list of your settings "
            "file (config/local.yaml; on a legacy single-printer file, first "
            "convert it as docs/SETTINGS.md describes), store any secret under "
            "the environment variable named in the entry — never in the file — "
            "and restart Print Handy:\n\n```yaml\n"
            f"{snippet}\n```\n\n"
            "Run `/print-handy add-printer` any time to redo the walkthrough.")


def _onboard_advance(session):
    """Recompute the step list from the answers and point at the first
    unanswered step; when every step is answered, move to review."""
    keys = _onboard_keys(session["ob_answers"])
    session["ob_keys"] = keys
    for index, key in enumerate(keys):
        if key not in session["ob_answers"]:
            session["ob_step"] = index
            session["status"] = "onboarding"
            return
    session["status"] = "onboard_review"


def _onboard_answer(session, raw, *, store, now, probe_runner=None):
    answers = session["ob_answers"]
    keys = session.get("ob_keys") or _onboard_keys(answers)
    key = keys[session["ob_step"]]
    step = _onboard_step_defs(session, probe_runner)[key]
    if isinstance(step, ProbeStep):
        return _onboard_probe_answer(session, raw, step, store=store, now=now)
    context = dict(answers)
    context["__existing"] = session["ob_existing"]
    try:
        value = step.validate(raw, context)
    except ValueError as exc:
        return (f"That answer does not work: {exc}\n\n"
                f"{_onboard_prompt_text(session)}")
    answers[key] = value
    if key == "name":
        answers["printer_id"] = _derive_printer_id(
            value, session["ob_existing"]["ids"])
    session["updated_at"] = now
    _onboard_advance(session)
    store.save(session)
    note = f"Saved: {step.label}."
    if session["status"] == "onboard_review":
        return _onboard_review_text(session, note=note)
    return _onboard_prompt_text(session, note)


def _onboard_probe_answer(session, raw, step, *, store, now):
    answers = session["ob_answers"]
    low = raw.strip().casefold()
    if low in ("test", "retry"):
        outcome = step.run(answers)
        session["updated_at"] = now
        if outcome.ok:
            recorded = {"ok": True, "summary": outcome.text}
            extra = outcome.extra or {}
            if extra.get("geometry"):
                recorded["geometry"] = extra["geometry"]
            if extra.get("geometry_error"):
                recorded["geometry_error"] = extra["geometry_error"]
            if isinstance(extra.get("thermal"), dict):
                recorded["thermal"] = extra["thermal"]
            answers["probe"] = recorded
            session.pop("ob_probe_error", None)
            session.pop("ob_back_key", None)
            _onboard_advance(session)
            store.save(session)
            note = f"Saved: {step.label}."
            if session["status"] == "onboard_review":
                return _onboard_review_text(session, note=note)
            return _onboard_prompt_text(session, note)
        session["ob_probe_error"] = outcome.text
        session["ob_back_key"] = outcome.back_key
        store.save(session)
        back = (f", `back` to change the step that failed"
                if outcome.back_key else "")
        return (f"**The connection test did not pass.** {outcome.text}\n\n"
                f"Fix the printer or the setting, then reply `retry` to test "
                f"again{back}, or `offline` to save this printer unverified. "
                "`/print-handy cancel` pauses; nothing has been written.")
    if low == "back":
        back_key = session.get("ob_back_key")
        if not back_key:
            return ("There is no failed test to go back from yet — reply "
                    "`test` to run it, or `offline` to save without "
                    f"testing.\n\n{_onboard_prompt_text(session)}")
        keys = _onboard_keys(answers)
        if back_key in keys:
            for later in keys[keys.index(back_key):]:
                answers.pop(later, None)
        answers.pop("probe", None)
        session.pop("ob_probe_error", None)
        session.pop("ob_back_key", None)
        session["updated_at"] = now
        _onboard_advance(session)
        store.save(session)
        return _onboard_prompt_text(
            session, "Back to the step that failed — correct it and the test "
                     "will run again at the end.")
    if low == "offline":
        answers["probe"] = {
            "ok": False, "offline": True,
            "reason": session.get("ob_probe_error") or "the test was skipped",
        }
        session.pop("ob_probe_error", None)
        session.pop("ob_back_key", None)
        session["updated_at"] = now
        _onboard_advance(session)
        store.save(session)
        note = f"Saved: {step.label}."
        if session["status"] == "onboard_review":
            return _onboard_review_text(session, note=note)
        return _onboard_prompt_text(session, note)
    return ("Reply `test` to run the connection test, `back` after a failure "
            "to change the setting that failed, or `offline` to save without "
            f"testing.\n\n{_onboard_prompt_text(session)}")


def _onboard_review_answer(session, raw, *, store, now, onboard_apply=None):
    low = raw.strip().casefold()
    if low == "apply":
        apply_fn = onboard_apply or _apply_onboarding
        try:
            detail = apply_fn(session["ob_answers"])
        except Exception as exc:  # the roster writer refused: stay at review
            return (f"**The printer was not added.** {exc}\nNothing was "
                    "written and your answers are kept. Reply `apply` to try "
                    "again once the cause is fixed, `manual` for the YAML to "
                    "add by hand, or `/print-handy cancel` to pause.")
        session["status"] = "complete"
        session["updated_at"] = now
        store.save(session)
        return f"**Printer onboarding complete.** {detail}"
    if low == "manual":
        session["status"] = "cancelled"
        session["updated_at"] = now
        store.save(session)
        return _onboard_manual_text(session)
    if low == "back":
        # Back to the model step. Every later answer depends on the model, so
        # the walkthrough restarts there — the geometry warning's "go back
        # and pick Generic Klipper" advice needs this path to exist (P2-256).
        session["ob_answers"] = {}
        session.pop("ob_probe_error", None)
        session.pop("ob_back_key", None)
        session["updated_at"] = now
        _onboard_advance(session)
        store.save(session)
        return _onboard_prompt_text(
            session, "Back to the first step — pick the model again and the "
                     "walkthrough redoes the rest from there.")
    model_key = raw.strip()
    if model_key in printers.MODELS:
        answers = session["ob_answers"]
        if model_key == answers.get("model"):
            return ("That is the model already picked.\n\n"
                    f"{_onboard_review_text(session)}")
        # Switch models: keep only the name and id — every other answer was
        # collected for the old model's connection and steps (P2-256).
        kept = {k: answers[k] for k in ("name", "printer_id") if k in answers}
        kept["model"] = model_key
        session["ob_answers"] = kept
        session.pop("ob_probe_error", None)
        session.pop("ob_back_key", None)
        session["updated_at"] = now
        _onboard_advance(session)
        store.save(session)
        return _onboard_prompt_text(
            session, f"Model changed to {printers.MODELS[model_key].label} "
                     f"(`{model_key}`). The connection steps are redone for "
                     "the new model; your name and id are kept.")
    return ("Reply `apply` to add the printer, `manual` for the YAML to add "
            "by hand, `back` or a different model key to change the model, "
            "or `/print-handy cancel` to pause.\n\n"
            f"{_onboard_review_text(session)}")


# ---------------------------------------------------------------------- session store

class SetupStore:
    """All guided-setup sessions in one JSON file (mode 600: it holds Discord user IDs)."""

    def __init__(self, path):
        self.path = Path(path)

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        session = self._load().get(key)
        return session if isinstance(session, dict) else None

    def all_sessions(self):
        return [s for s in self._load().values() if isinstance(s, dict)]

    def save(self, session):
        data = self._load()
        data[session["key"]] = session
        self.path.parent.mkdir(parents=True, exist_ok=True)
        from ..setup_wizard import _atomic_write
        _atomic_write(self.path, json.dumps(data, indent=1), 0o600)

    def clear(self, key):
        data = self._load()
        if key in data:
            del data[key]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from ..setup_wizard import _atomic_write
            _atomic_write(self.path, json.dumps(data, indent=1), 0o600)


def default_store(cfg=None):
    data = cfg if cfg is not None else settings.current()
    return SetupStore(settings.state_dir(data) / STORE_NAME)


def _record_shared_completion(store, surface="chat"):
    """Write the shared setup-completion record beside the session store.

    Best effort: the shared record (print_handy.setup_state) must never
    break a flow that has already completed on its own terms.
    """
    try:
        from .. import setup_state
        setup_state.record_completion_in_state_dir(store.path.parent, surface)
    except Exception:
        logger.exception("shared setup-completion record could not be written")


def shared_completion(store):
    """The shared completion record visible from this store, or {}."""
    try:
        from .. import setup_state
        return setup_state.load_completion_in_state_dir(store.path.parent)
    except Exception:
        return {}


def _new_session(key, now):
    return {"key": key, "status": "in_progress", "answers": {}, "step": 0,
            "created_at": now, "updated_at": now}


def _expired(session, now):
    return (session["status"] in ("in_progress", "awaiting_consent", "adding_printer",
                                   "onboarding", "onboard_review")
            and now - float(session.get("updated_at") or 0) > SESSION_TIMEOUT_S)


SETUP_ONLY_REFUSAL = ("**Setup-only mode.** This install is not configured yet, so the bot "
                      "answers only the guided setup conversation. Run `/print-handy setup` "
                      "to set it up; every other command becomes available once setup is "
                      "applied and Print Handy has restarted.")


def active_setup_owner(store, now, exclude_key=None):
    """The session key owning an in-progress guided setup, or None.

    The Q5 binding rule: a setup conversation binds at conversation
    start and is session-scoped — while one setup session is active
    (not expired), no other key may start or join one. An expired
    session releases the bind, so an abandoned flow frees the install.
    Onboarding sessions do not hold the setup bind.
    """
    for session in store.all_sessions():
        if session.get("key") == exclude_key or session.get("flow") == "onboard":
            continue
        if session.get("status") in ("in_progress", "awaiting_consent", "adding_printer") \
                and not _expired(session, now):
            return session.get("key")
        # A finished setup keeps its bind: setup-only mode lasts until the
        # restart, and nobody else may rewrite what its owner just applied.
        if session.get("status") == "complete" \
                and now - float(session.get("updated_at") or 0) <= SESSION_TIMEOUT_S:
            return session.get("key")
    return None


def checklist_text(session):
    answers = session.get("answers") or {}
    flow = _applicable_steps(answers)
    current = None
    if session["status"] == "in_progress":
        full = steps()
        if 0 <= session.get("step", 0) < len(full):
            current = full[session["step"]].key
    lines = []
    for step in flow:
        if step.key in answers:
            mark = "☑"
        elif step.key == current:
            mark = "▶"
        else:
            mark = "☐"
        lines.append(f"{mark} {step.label}")
    for extra in answers.get("printers") or []:
        if isinstance(extra, dict) and extra.get("id"):
            lines.append(f"☑ Added printer {extra['id']} ({extra.get('model', '?')})")
    if session["status"] == "awaiting_consent":
        lines.append("▶ Review and choose how to apply")
    return "\n".join(lines)


def _prompt_text(session, note="", token_env="DISCORD_BOT_TOKEN", can_delete_secrets=True):
    flow = steps(token_env)
    step = flow[session["step"]]
    answers = session.get("answers") or {}
    platform = answers.get("platform") or "discord"
    if step.key == "moonraker_url":
        prompt = _printer_url_prompt(answers.get("model"))
    elif step.key == "chat_id":
        prompt = _chat_id_prompt(platform)
    elif step.key == "user_ids":
        prompt = _user_ids_prompt(platform)
    elif step.key == "token_stored":
        prompt = _token_prompt(platform, token_env)
    elif not can_delete_secrets and step.key in ("webui_login", "webui_password", "webui_confirm"):
        prompt = _no_delete_login_prompt(step.key)
    else:
        prompt = step.prompt
    applicable = _applicable_steps(answers, token_env)
    position = next((i + 1 for i, s in enumerate(applicable) if s.key == step.key),
                    session["step"] + 1)
    head = f"**Setup step {position} of {len(applicable)}: {step.label}**\n{prompt}"
    return (note + "\n" if note else "") + head + "\n\n" + checklist_text(session) + \
        "\n\nReply with the answer, or `/print-handy cancel` to pause (your checklist is saved)."


def _review_text(session):
    answers = session["answers"]
    platform = answers.get("platform") or "discord"
    lines = ["**Setup is ready to review.** Nothing has been written yet, except a Web UI "
             "password you set above — that one is saved as soon as it is confirmed.",
             f"- Chat platform: `{platform}`",
             f"- Printer model: `{answers.get('model')}`",
             "- Bot mode: `standalone`",
             "- Print channel and access: the user IDs you entered (not repeated here), "
             "or everyone in the channel if you chose `everyone`",
             f"- Printer URL: {answers.get('moonraker_url') or 'not set'}",
             f"- Spoolman URL: {answers.get('spoolman_url') or 'not set'}",
             f"- Camera snapshot URL: {answers.get('camera_snapshot_url') or 'not set'}",
             f"- Timelapse: {answers.get('timelapse') or 'kept as it is'}",
             f"- Bot token: {'stored in its environment variable' if answers.get('token_stored') else 'not stored yet — the bot cannot log in until it is'}",
             _login_review_line(answers)]
    roster = answers.get("printers") or []
    if roster:
        lines.append(f"- First printer id: `{answers.get('printer_id')}`")
        for extra in roster:
            lines.append(f"- Added printer: `{extra['id']}` — model `{extra.get('model')}`, "
                         f"channel {'its own' if extra.get('chat_id') else 'the main print channel'}"
                         + (f", Printer URL {extra['moonraker_url']}" if extra.get('moonraker_url') else ""))
    lines += ["", checklist_text(session), "",
              "Reply `apply` to write these settings to config/local.yaml (values already there "
              "always win; the token is never written). Reply `add` to add another printer — it "
              "gets its own channel and state folder. Reply `manual` for precise steps to do it "
              "yourself instead, or `/print-handy cancel` to leave setup unfinished and resume later."]
    return "\n".join(lines)


def _manual_text(session):
    answers = session["answers"]
    roster = answers.get("printers") or []
    lines = ["**Manual setup steps** — nothing was changed for you.",
             "1. In the Print Handy folder, create or edit `config/local.yaml` with:"]
    if roster:
        lines.append("   - a `printers:` list (TOML spelling: `[[printers]]`), one entry per printer:")
        first = f"     - `id` = `{answers.get('printer_id')}`, `model` = `{answers.get('model')}`"
        placed = _address_placement(answers.get("model"), answers.get("moonraker_url"))
        if placed:
            first += f", `{placed[1]}` = the Printer URL you entered above"
        lines.append(first)
        for extra in roster:
            entry = f"     - `id` = `{extra['id']}`, `model` = `{extra.get('model')}`"
            if extra.get("chat_id"):
                entry += ", `chat_id` = the channel ID you entered above"
            extra_placed = _address_placement(extra.get("model"), extra.get("moonraker_url"))
            if extra_placed:
                entry += f", `{extra_placed[1]}` = the Printer URL you entered above"
            lines.append(entry)
        lines.append("   - entry values can be overridden later with PRINT_HANDY__PRINTERS__<ID>__<KEY>; "
                     "the list itself is file-only (docs/SETTINGS.md, 'Printers list')")
    else:
        lines.append(f"   - `[printer] model` = `{answers.get('model')}`")
    _section, _chat = _chat_section(answers)
    lines += ["   - `[bot] mode` = `standalone` and `[bot] platform` = "
              f"`{answers.get('platform') or 'discord'}`",
              f"   - `[{_section}]` chat/room and `allowed_user_ids` = what you entered above "
              "(blank `allowed_user_ids` = everyone in the channel)"]
    if answers.get("moonraker_url") and not roster:
        placed = _address_placement(answers.get("model"), answers.get("moonraker_url"))
        if placed:
            lines.append(f"   - `[{placed[0]}] {placed[1]}` = the Printer URL you entered above")
    if answers.get("spoolman_url"):
        lines.append("   - `[services] spoolman_url` = the URL you entered above")
    if answers.get("camera_snapshot_url"):
        lines.append("   - `[services] camera_snapshot_url` = the URL you entered above, "
                     "with `[services] camera_source` = `auto`")
    if answers.get("timelapse") == "on":
        lines.append("   - `[timelapse] mode` = `rendered` and `capture_enabled` = true")
    elif answers.get("timelapse") == "off":
        lines.append("   - `[timelapse] mode` = `off` and `capture_enabled` = false")
    _penv, _phow = PLATFORM_TOKEN_INFO.get(answers.get("platform") or "discord",
                                           PLATFORM_TOKEN_INFO["discord"])
    lines += [f"2. Store the bot token in its environment variable (for this platform, "
              f"`{_penv}` unless the settings name another) in the profile/data folder `.env` "
              "(file mode 600). Do not put the token in the YAML file or in chat.",
              "3. Validate offline: `python3 -m print_handy.bot check` (standalone) or "
              "`python3 -m print_handy.doctor`.",
              "4. Run `/print-handy debugging` here to re-check. Your saved checklist stays "
              "available with `/print-handy status` until you finish or restart setup."]
    return "\n".join(lines)


def _chat_section(answers):
    """(section name, section dict) for the chosen platform's chat settings."""
    platform = answers.get("platform") or "discord"
    if platform == "matrix":
        section = {"room_id": answers["chat_id"],
                   "allowed_user_ids": answers["user_ids"]}
        if answers.get("homeserver_url"):
            section["homeserver_url"] = answers["homeserver_url"]
        return "matrix", section
    return platform, {"chat_id": answers["chat_id"],
                      "allowed_user_ids": answers["user_ids"]}


def _stamp_camera_and_timelapse(config, answers):
    """The camera/timelapse keys, written exactly as the Web UI Camera step writes them."""
    if answers.get("camera_snapshot_url"):
        config.setdefault("services", {})["camera_snapshot_url"] = answers["camera_snapshot_url"]
        config["services"].setdefault("camera_source", "auto")
    if answers.get("timelapse") == "on":
        config["timelapse"] = {"mode": "rendered", "capture_enabled": True}
    elif answers.get("timelapse") == "off":
        config["timelapse"] = {"mode": "off", "capture_enabled": False}


def _login_review_line(answers):
    choice = answers.get("webui_login")
    if choice == "password":
        return (f"- Web UI login: set for the username you entered "
                f"(`{answers.get('webui_username') or 'saved'}`); the password itself was "
                "saved when you confirmed it and is never shown here")
    if choice == "open":
        return "- Web UI login: left open by your saved choice when this setup is applied"
    if choice == "clear":
        return ("- Web UI login: the saved login/open choice is removed when this setup "
                "is applied (a token set through the Docker variable still applies)")
    return "- Web UI login: left as it is"


def _stamp_address(entry, model_key, address):
    placed = _address_placement(model_key, address)
    if placed is not None:
        entry[placed[1]] = address


def _roster_config(answers):
    """The [[printers]] config for a roster apply (#337): the first printer plus
    every added one, with Spoolman kept as the one shared inventory service."""
    first = {"id": answers["printer_id"], "model": answers["model"]}
    _stamp_address(first, answers["model"], answers.get("moonraker_url"))
    entries = [first]
    for extra in answers["printers"]:
        entry = {"id": extra["id"], "model": extra["model"]}
        if extra.get("chat_id"):
            entry["chat_id"] = extra["chat_id"]
        _stamp_address(entry, extra.get("model"), extra.get("moonraker_url"))
        entries.append(entry)
    section, chat = _chat_section(answers)
    config = {"printers": entries,
              "bot": {"mode": "standalone", "platform": answers.get("platform") or "discord"},
              section: chat}
    if answers.get("spoolman_url"):
        config.setdefault("services", {})["spoolman_url"] = answers["spoolman_url"]
    _stamp_camera_and_timelapse(config, answers)
    return config


def _refuse_unsafe_roster_target(config_path):
    """A roster apply never merges into a file that already describes printers.

    The wizard merge is existing-keys-win, so a roster merged into a file with a
    legacy [printer] shape (or an existing roster) would be silently dropped or
    clash; and a file the wizard cannot round-trip is never rewritten at all.
    Both cases raise with the file left byte-for-byte unchanged."""
    from .. import setup_wizard
    path = Path(config_path)
    if not path.exists():
        return
    parsed = setup_wizard._load_for_rewrite(path.read_text(encoding="utf-8"))
    if parsed is None:
        raise RuntimeError("the existing config/local.yaml cannot be safely rewritten (it does "
                           "not parse and re-emit faithfully), so it was left unchanged. Fix it "
                           "by hand or move it aside, then reply `apply` again.")
    printer = parsed.get("printer") if isinstance(parsed.get("printer"), dict) else {}
    services = parsed.get("services") if isinstance(parsed.get("services"), dict) else {}
    legacy = ("printers" in parsed or bool(printer.get("model") or printer.get("name"))
              or any(services.get(k) for k in ("moonraker_url", "octoprint_url", "camera_snapshot_url"))
              or any(isinstance(parsed.get(section), dict)
                     and any(parsed[section].get(k) for k in ("lan_host", "link_host", "host"))
                     for section in ("bambu", "prusa", "elegoo_sdcp", "flashforge_stock")))
    if legacy:
        raise RuntimeError("the existing config/local.yaml already describes a printer, so guided "
                           "setup will not merge a roster into it; the file was left unchanged. "
                           "Edit it by hand (docs/SETTINGS.md, 'Printers list') or move it aside, "
                           "then reply `apply` again.")


def _apply_answers(answers, config_path):
    """Write the collected settings after the explicit `apply` consent.

    Only config/local.yaml is touched, through the setup wizard's merge (existing keys win).
    No secret is written: the token step collected none. Installing software, restarting
    services, and changing network exposure are never done by this flow. When printers were
    added, the write is the [[printers]] roster form instead, and it refuses (leaving the
    file untouched) when the existing file already describes printers or cannot be
    round-tripped by the wizard (#337).
    """
    from .. import setup_wizard

    class _Consent:
        """The Discord `apply` reply is the consent merge_config asks for."""
        def __init__(self):
            self.notes = []

        def say(self, text=""):
            self.notes.append(str(text))

        def confirm(self, _key, _prompt, default=True):
            return True

    roster = bool(answers.get("printers"))
    if roster:
        _refuse_unsafe_roster_target(config_path)
        config = _roster_config(answers)
    else:
        section, chat = _chat_section(answers)
        config = {"printer": {"model": answers["model"]},
                  "bot": {"mode": "standalone",
                          "platform": answers.get("platform") or "discord"},
                  section: chat}
        placed = _address_placement(answers.get("model"), answers.get("moonraker_url"))
        if placed:
            config.setdefault(placed[0], {})[placed[1]] = answers["moonraker_url"]
        if answers.get("spoolman_url"):
            config.setdefault("services", {})["spoolman_url"] = answers["spoolman_url"]
        _stamp_camera_and_timelapse(config, answers)
    writer = _Consent()
    if not setup_wizard.merge_config(Path(config_path), config, writer):
        detail = writer.notes[-1] if writer.notes else "the wizard merge declined to write"
        raise RuntimeError("the settings were not written: " + detail)
    lead = (f"Settings written to config/local.yaml as a [[printers]] roster "
            f"({len(answers['printers']) + 1} printers; any values already there were kept). "
            if roster else
            "Settings written to config/local.yaml (any values already there were kept). ")
    return (lead + "The bot token was not written anywhere by this flow. Nothing was installed, "
            "restarted, or exposed on the network. Next: `python3 -m print_handy.bot check`, "
            "then `/print-handy debugging`.")


def _start_onboarding(key, store, now, config_loader, restarted=False):
    """Begin a fresh onboarding checklist and return its first prompt.

    The current settings must load and the roster must be writable before
    anyone is walked through the steps; both refusals name the fix.
    ``restarted`` says a saved checklist was just discarded (P2-219)."""
    loader = config_loader or (lambda: settings.load())
    try:
        cfg = loader()
    except Exception as exc:
        return ("**Printer onboarding cannot start** — the current settings do not "
                f"load: {exc}\nFix the configuration first (`/print-handy debugging` "
                "shows the checks), then run `/print-handy add-printer` again.")
    from .. import roster_write
    refusal = roster_write.write_refusal()
    if refusal:
        return f"**Printer onboarding cannot start** — {refusal}"
    entries = cfg.get("printers") or []
    session = {"key": key, "flow": "onboard", "status": "onboarding",
               "ob_answers": {}, "ob_step": 0, "ob_keys": ["model", "name"],
               "ob_existing": {
                   "ids": [entry["id"] for entry in entries],
                   "names": [str(entry["name"]) for entry in entries],
                   "connections": [printers.MODELS[entry["model"]].vendor.connection
                                   for entry in entries]},
               "created_at": now, "updated_at": now}
    store.save(session)
    note = ("**Add a printer.** One question at a time; nothing is written until you "
            "approve it at the end, and the printer is tested live before it is saved. "
            "Secrets are never collected here — you store each value in the .env and "
            "reply when it is stored.")
    if restarted:
        note = "**Onboarding restarted — the saved checklist was discarded.**\n\n" + note
    return _onboard_prompt_text(session, note)


# ---------------------------------------------------------------------- the flow

def _access_data(access_data):
    """The settings mapping the Web UI access writers act on, or None."""
    if access_data is not None:
        return access_data
    try:
        return settings.current()
    except Exception:
        return None


def _apply_login_choice(session, access_data, access_environ):
    """The review-time Web UI login actions: leave-open and clear-claim.

    A set password is already saved by then (it is written at its
    confirmation step, because the value may not be stored); these two
    carry no secret, so they wait for the same `apply` consent as the
    rest of setup. Returns a sentence for the completion reply, or "".
    """
    choice = (session.get("answers") or {}).get("webui_login")
    if choice not in ("open", "clear"):
        return ""
    data = _access_data(access_data)
    if data is None:
        return (" The Web UI login choice could not be applied because this "
                "install's settings do not load; set it from the Web UI instead.")
    from . import webui_access
    result = (webui_access.leave_open(data, True, environ=access_environ)
              if choice == "open" else
              webui_access.clear_claim(data, True, environ=access_environ))
    return " " + result.message


# Setup-only mode admits strangers, so checking who owns setup and claiming it
# has to be one step: without this, two people starting at once could both win.
_SETUP_ONLY_LOCK = threading.Lock()


def handle_text(key, text, **kwargs):
    """One message in the guided flows; see :func:`_handle_text`."""
    if kwargs.get("setup_only"):
        with _SETUP_ONLY_LOCK:
            return _handle_text(key, text, **kwargs)
    return _handle_text(key, text, **kwargs)


def _handle_text(key, text, *, store, now=None, token_env="DISCORD_BOT_TOKEN",
                apply_config=None, report=None, bundle=None, debug=None,
                probe_runner=None, onboard_apply=None, config_loader=None,
                setup_only=False, access_data=None, access_environ=None,
                can_delete_secrets=True):
    """One message in the guided flows. Returns the reply text, or None when not ours.

    ``key`` identifies one person's session in one channel/thread. ``report`` is a zero-arg
    callable returning the debugging report; ``bundle`` is a zero-arg callable that writes
    the #390 diagnostics bundle and returns the reply saying where it went;
    ``debug`` is a one-arg callable taking the text after ``debug`` (``on``/``off``/``status``
    plus any duration) and returning the #478 debug-mode reply; ``apply_config`` writes the
    consented settings. The onboarding kwargs are the same injection seam for #537:
    ``probe_runner`` runs the live connection test, ``onboard_apply`` performs the
    review-approved roster write, and ``config_loader`` reads the current settings the new
    printer joins. ``access_data`` and ``access_environ`` are the settings and environment
    the Web UI login steps act on. ``can_delete_secrets`` says whether this chat can delete
    the password messages; when it cannot, the login prompts say so before asking.
    """
    now = time.time() if now is None else now
    raw = str(text or "").strip()
    low = raw.casefold()
    session = store.get(key)
    _purge_stale_webui_passwords(now)
    if session is not None and _expired(session, now):
        _pending_webui_passwords.pop(key, None)

    def _prompt(current, note=""):
        return _prompt_text(current, note, token_env=token_env,
                            can_delete_secrets=can_delete_secrets)

    if low.startswith("/print-handy"):
        rest = low[len("/print-handy"):].strip()
        if setup_only and rest not in ("", "help", "setup", "setup restart", "status", "cancel"):
            return SETUP_ONLY_REFUSAL
        if setup_only and rest in ("setup", "setup restart"):
            owner = active_setup_owner(store, now, exclude_key=key)
            if owner is not None:
                return ("A setup conversation is already in progress with another user. "
                        "Only one setup runs at a time; it becomes available again when "
                        "that conversation finishes, is paused, or times out.")
        if rest in ("", "help"):
            return HELP
        if rest == "debugging":
            if report is None:
                return "Debugging is not available in this runtime."
            return report()
        if rest == "debug" or rest.startswith("debug "):
            if debug is None:
                return ("Debug mode is not available in this runtime. On the host, run "
                        "`python3 -m print_handy.debug_mode on|off|status` instead.")
            return debug(rest[len("debug"):].strip())
        if rest == "bundle":
            if bundle is None:
                return ("Diagnostics bundles are not available in this runtime. On the host, run "
                        "`python3 -m print_handy.diagnostics_bundle` instead.")
            return bundle()
        if rest == "status":
            if session is None:
                return (f"Print Handy {__version__}\n"
                        "No guided setup has been started here. `/print-handy setup` begins one.")
            state = {"in_progress": "in progress", "adding_printer": "adding another printer",
                     "awaiting_consent": "waiting for your review choice",
                     "onboarding": "adding a printer (guided onboarding)",
                     "onboard_review": "adding a printer — waiting for your review choice",
                     "cancelled": "paused (cancelled)", "complete": "complete"}[session["status"]]
            checklist = (_onboard_checklist_text(session) if session.get("flow") == "onboard"
                         else checklist_text(session))
            return f"Print Handy {__version__}\n**Guided setup: {state}**\n" + checklist
        if rest == "cancel":
            _pending_webui_passwords.pop(key, None)
            if session is None or session["status"] == "complete":
                return "No guided setup is in progress. Nothing was changed."
            session["status"] = "cancelled"
            session["updated_at"] = now
            store.save(session)
            if session.get("flow") == "onboard":
                return ("Printer onboarding paused. Your answers are saved in the checklist "
                        "and nothing was written to the configuration.\n"
                        + _onboard_checklist_text(session) +
                        "\nResume any time with `/print-handy add-printer`, or discard the "
                        "checklist and start over with `/print-handy add-printer restart`.")
            login_note = ""
            if session["answers"].get("webui_confirm"):
                login_note = ("\nThe Web UI login you confirmed stays saved: cancelling setup "
                              "does not remove it. To remove it, run `/print-handy setup "
                              "restart` and choose `clear` at the Web UI login step.")
            return ("Setup paused. Your answers are saved in the checklist and nothing was "
                    "written to the configuration.\n" + checklist_text(session) +
                    "\nResume any time with `/print-handy setup`." + login_note)
        if rest == "add-printer restart":
            # Discard a saved onboarding checklist and start over (P2-219):
            # resuming is the default everywhere else, so without this an
            # accepted model or name could never be corrected, and a
            # checklist left by a hand-added printer trapped its owner in
            # a review whose apply must fail on the existing id.
            if (session is not None and session.get("flow") != "onboard"
                    and session["status"] in ("in_progress", "awaiting_consent",
                                               "adding_printer")):
                return ("A guided setup is in progress here. Finish it, or "
                        "`/print-handy cancel` to pause it, then run "
                        "`/print-handy add-printer`.")
            discarded = (session is not None and session.get("flow") == "onboard"
                         and session["status"] != "complete")
            if discarded:
                store.clear(key)
            return _start_onboarding(key, store, now, config_loader,
                                     restarted=discarded)
        if rest == "add-printer":
            if (session is not None and session.get("flow") == "onboard"
                    and session["status"] in ("onboarding", "onboard_review", "cancelled")):
                note = ""
                if session["status"] == "cancelled":
                    note = "**Welcome back — resuming printer onboarding.**"
                    _onboard_advance(session)
                elif _expired(session, now):
                    note = ("**Your previous session timed out; your saved answers were "
                            "kept.** Resuming:")
                session["updated_at"] = now
                store.save(session)
                if session["status"] == "onboard_review":
                    return (note + "\n" if note else "") + _onboard_review_text(session)
                return _onboard_prompt_text(session, note)
            if (session is not None and session.get("flow") != "onboard"
                    and session["status"] in ("in_progress", "awaiting_consent",
                                              "adding_printer")):
                return ("A guided setup is in progress here. Finish it, or "
                        "`/print-handy cancel` to pause it, then run "
                        "`/print-handy add-printer`.")
            return _start_onboarding(key, store, now, config_loader)
        if rest in ("setup", "setup restart"):
            if (session is not None and session.get("flow") == "onboard"
                    and session["status"] != "complete"):
                return ("A printer onboarding is in progress here — `/print-handy add-printer` "
                        "resumes it, `/print-handy cancel` pauses it, or "
                        "`/print-handy add-printer restart` discards it and starts over. "
                        "Guided setup can start once it finishes.")
            if rest == "setup" and session is None:
                prior = shared_completion(store)
                if prior.get("done") and not prior.get("skipped"):
                    from .. import setup_state as _setup_state
                    return (_setup_state.describe(prior) +
                            " Run `/print-handy setup restart` to run guided setup again.")
            if rest == "setup restart" or session is None or session["status"] == "complete":
                _pending_webui_passwords.pop(key, None)
                session = _new_session(key, now)
                store.save(session)
                return _prompt(session, "**Guided setup started.** One question at a time; "
                                        "nothing is written until you approve it at the end.")
            note = ""
            if session["status"] == "cancelled":
                note = "**Welcome back — resuming your saved checklist.**"
            elif _expired(session, now):
                note = "**Your previous session timed out; your saved answers were kept.** Resuming:"
            if session.get("add_keys") is not None and session.get("draft") is not None:
                # paused mid add-a-printer cycle: resume the cycle, not the base flow
                session["status"] = "adding_printer"
                session["updated_at"] = now
                store.save(session)
                return _add_prompt_text(session, note)
            session["status"] = "in_progress" if session["status"] != "awaiting_consent" else "awaiting_consent"
            session["updated_at"] = now
            store.save(session)
            if session["status"] == "awaiting_consent":
                return (note + "\n" if note else "") + _review_text(session)
            return _prompt(session, note)
        return ("Use `/print-handy setup`, `/print-handy add-printer`, `/print-handy debugging`, "
                "`/print-handy bundle`, `/print-handy debug on|off|status`, `/print-handy status`, "
                "or `/print-handy cancel`.")

    if session is None or session["status"] not in ("in_progress", "awaiting_consent",
                                                     "adding_printer", "onboarding",
                                                     "onboard_review"):
        if setup_only:
            if active_setup_owner(store, now, exclude_key=key) is not None:
                return ("A setup conversation is already in progress with another user. "
                        "Only one setup runs at a time; it becomes available again when "
                        "that conversation finishes, is paused, or times out.")
            return ("This install is not set up yet. Run `/print-handy setup` to begin "
                    "guided setup — it is the one conversation available until setup "
                    "is applied.")
        return None
    if setup_only and active_setup_owner(store, now, exclude_key=key) is not None:
        return ("A setup conversation is already in progress with another user. "
                "Only one setup runs at a time; it becomes available again when "
                "that conversation finishes, is paused, or times out.")
    if raw.startswith("/"):
        return None  # another command; setup stays open for a later plain reply
    if _expired(session, now):
        if session.get("flow") == "onboard":
            return ("This onboarding session timed out, so that reply was not used as an "
                    "answer. Your saved answers were kept — run `/print-handy add-printer` "
                    "to resume the checklist.")
        return ("This setup session timed out, so that reply was not used as an answer. "
                "Your saved answers were kept — run `/print-handy setup` to resume the checklist.")

    if session.get("flow") == "onboard":
        if session["status"] == "onboard_review":
            return _onboard_review_answer(session, raw, store=store, now=now,
                                          onboard_apply=onboard_apply)
        return _onboard_answer(session, raw, store=store, now=now,
                               probe_runner=probe_runner)

    if session["status"] == "awaiting_consent":
        if low == "apply":
            apply_config = apply_config or (lambda answers: _apply_answers(answers, settings.LOCAL))
            try:
                detail = apply_config(session["answers"])
            except Exception as exc:  # a failed write must not lose the checklist
                # The reply below names the log as where the detail lives
                # (P2-180): put it there, redacted, before returning —
                # actionable refusals (an existing config that must be
                # edited or moved aside) were discarded silently before
                # this module logged anything.
                logger.warning("guided setup apply failed; the checklist was kept: %s",
                               redact(str(exc)))
                return ("The settings could not be saved, so nothing was changed. "
                        "This looks like a problem on the Print Handy side, not with your answers — "
                        "the detail is in the log, and `/print-handy bundle` gathers it for a report.\n"
                        "Your checklist is unchanged. Reply `apply` to retry, `manual` for "
                        "steps, or `/print-handy cancel` to pause.")
            session["status"] = "complete"
            session["updated_at"] = now
            store.save(session)
            detail += _apply_login_choice(session, access_data, access_environ)
            _record_shared_completion(store)
            if setup_only:
                return ("**Setup complete.** " + detail +
                        "\n\nRestart Print Handy to bring the new settings live — "
                        "setup-only mode ends at that restart.")
            return "**Setup complete.** " + detail
        if low == "manual":
            session["status"] = "cancelled"  # finished with the flow, checklist kept for reference
            session["updated_at"] = now
            store.save(session)
            return _manual_text(session)
        if low == "add":
            session["status"] = "adding_printer"
            session["draft"] = {}
            session["add_keys"] = _add_keys(session)
            session["add_step"] = 0
            session["updated_at"] = now
            store.save(session)
            return _add_prompt_text(session, "**Adding another printer.** Same idea: a few "
                                    "questions, and nothing is written until you reply `apply`.")
        return "Reply `apply` to write the settings, `add` to add another printer, `manual` for precise manual steps, or `/print-handy cancel` to pause."

    if session["status"] == "adding_printer":
        defs = _add_step_defs()
        key = session["add_keys"][session["add_step"]]
        step = defs[key]
        context = dict(session["answers"])
        if key == "moonraker_url" and session["draft"].get("model"):
            context["model"] = session["draft"]["model"]  # validate against the new printer's model
        try:
            value = step.validate(raw, context)
        except ValueError as exc:
            return f"That answer does not work: {exc}\n\n" + _add_prompt_text(session)
        if key == "first_id":
            session["answers"]["printer_id"] = value
        else:
            session["draft"][key] = value
        session["add_step"] += 1
        session["updated_at"] = now
        if session["add_step"] >= len(session["add_keys"]):
            draft = session["draft"]
            session["answers"].setdefault("printers", []).append(
                {"id": draft["id"], "model": draft["model"],
                 "chat_id": draft.get("chat_id", ""), "moonraker_url": draft.get("moonraker_url", "")})
            session["status"] = "awaiting_consent"
            for scratch in ("draft", "add_keys", "add_step"):
                session.pop(scratch, None)
            store.save(session)
            return (f"Saved: printer {draft['id']}.\n\n" + _review_text(session))
        store.save(session)
        return _add_prompt_text(session, f"Saved: {step.label}.")

    flow = steps(token_env)
    step = flow[session["step"]]

    def _advance():
        session["step"] += 1
        while session["step"] < len(flow) and not _step_applies(flow[session["step"]].key,
                                                                session["answers"]):
            session["step"] += 1
        session["updated_at"] = now
        if session["step"] >= len(flow):
            session["status"] = "awaiting_consent"
            store.save(session)
            return _review_text(session)
        store.save(session)
        return _prompt(session, f"Saved: {step.label}.")

    if step.key == "webui_password":
        # In memory only: the value never enters the session, the store,
        # or any reply. Validation happens at confirmation, through the
        # Web UI's own writer (bot/webui_access.py).
        if not raw:
            return "That answer does not work: reply with the password.\n\n" + _prompt(session)
        _pending_webui_passwords[key] = (raw, now)
        session["answers"]["webui_password"] = True
        return _advance()
    if step.key == "webui_confirm":
        pending_entry = _pending_webui_passwords.pop(key, None)
        if pending_entry is None:
            session["step"] = next(i for i, s in enumerate(flow) if s.key == "webui_login")
            session["answers"].pop("webui_password", None)
            store.save(session)
            return ("The password from the previous step is gone (setup was paused or timed "
                    "out, or the bot restarted, before it was confirmed), so nothing was "
                    "saved. Start the login step again.\n\n" + _prompt(session))
        pending = pending_entry[0]
        from . import webui_access
        data = _access_data(access_data)
        if data is None:
            session["step"] = next(i for i, s in enumerate(flow)
                                   if s.key == "webui_password")
            store.save(session)
            return ("The Web UI login could not be saved because this install's settings "
                    "do not load. Fix that first, then reply with the password again.\n\n"
                    + _prompt(session))
        result = webui_access.set_password(
            data, session["answers"].get("webui_username"), pending, raw,
            environ=access_environ)
        if not result.ok:
            session["step"] = next(i for i, s in enumerate(flow)
                                   if s.key == "webui_password")
            session["answers"].pop("webui_password", None)
            store.save(session)
            return (f"That password does not work: {result.message}\n\n" + _prompt(session))
        session["answers"]["webui_confirm"] = True
        return _advance()
    try:
        value = step.validate(raw, session["answers"])
    except ValueError as exc:
        return f"That answer does not work: {exc}\n\n" + _prompt(session)
    session["answers"][step.key] = value
    return _advance()


# ---------------------------------------------------------------------- debugging

@dataclass
class Finding:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str
    action: str = ""


_MARKS = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "–"}


def _default_probe(url):
    """A read-only GET. Returns the HTTP status; raises on any failure."""
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status


def _default_login_probe(token):
    from . import rest
    return rest.whoami(token)


def _default_version_runner(command):
    # Run from a scratch dir: OrcaSlicer writes 00000.log into its cwd and aborts if that is
    # not writable (the Docker image's /app is not writable by the container user).
    with tempfile.TemporaryDirectory(prefix="print-handy-version-") as scratch:
        run = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=15, cwd=scratch)
        out = (run.stdout or run.stderr or "").strip()
        if run.returncode != 0 or not out:
            run = subprocess.run([*command, "--help"], capture_output=True, text=True, timeout=15, cwd=scratch)
            out = (run.stdout or run.stderr or "").strip()
    return out.splitlines()[0] if out else ""


def _default_token_getter(cfg):
    from ..runtime import _config
    return _config.profile_env_value(str(cfg["discord"].get("token_env") or "DISCORD_BOT_TOKEN"))


def run_diagnostics(*, loader=None, token_getter=None, probe=None, login_probe=None,
                    which=shutil.which, version_runner=None, environ=None):
    """Read-only checks. Every external contact is a GET probe or a `--version` run."""
    loader = loader or (lambda: settings.load(environ={}))
    probe = probe or _default_probe
    version_runner = version_runner or _default_version_runner
    findings: list[Finding] = []
    secrets = _env_secrets(environ)
    roots = [Path.home()]

    def clean(text):
        return redact(text, secrets=secrets, roots=roots)

    cfg = None
    try:
        cfg = loader()
    except Exception as exc:
        findings.append(Finding("configuration", "fail", clean(f"settings do not load: {exc}"),
                                "Fix the named setting in config/local.yaml (see "
                                "docs/CONFIGURATION-YAML.md), then run debugging again."))
    if cfg is not None:
        for section in ("paths",):
            for value in (cfg.get(section) or {}).values():
                if isinstance(value, (str, Path)):
                    roots.append(str(value))
        printer = cfg.get("printer") or {}
        findings.append(Finding("configuration", "ok",
                                clean(f"settings load and validate; printer model {printer.get('model')}, "
                                      f"bed {printer.get('bed_mm')}")))

        # Token presence and a read-only login probe.
        token = ""
        try:
            token = (token_getter or _default_token_getter)(cfg) or ""
        except Exception:
            token = ""
        if token:
            secrets.append(token)
        token_env = str((cfg.get("discord") or {}).get("token_env") or "DISCORD_BOT_TOKEN")
        if not token:
            findings.append(Finding("bot mode and login", "fail",
                                    f"mode is standalone but ${token_env} is empty",
                                    f"Store the bot token in ${token_env} in the profile/data "
                                    "folder .env (mode 600); never paste it in chat."))
        else:
            try:
                me = (login_probe or _default_login_probe)(token)
                findings.append(Finding("bot mode and login", "ok",
                                        clean(f"standalone; Discord accepted the token "
                                              f"(bot user {me.get('username')})")))
            except Exception as exc:
                findings.append(Finding("bot mode and login", "warn",
                                        clean(f"standalone; ${token_env} is set but the read-only "
                                              "login check did not pass"),
                                        "Check network/DNS to discord.com and that the token was "
                                        "not reset in the developer portal."))

        # Permission prerequisites: what configuration can prove offline. Channel overrides
        # themselves are only visible to the logged-in bot, so they stay a manual next step.
        from .. import setup_wizard
        discord_cfg = cfg.get("discord") or {}
        chat = str(discord_cfg.get("chat_id") or "")
        try:
            users = settings.user_ids(discord_cfg.get("allowed_user_ids"))
        except Exception:
            users = []
        missing = []
        if not chat.isdigit():
            missing.append("discord.chat_id is not a numeric channel ID")
        needed = ", ".join(setup_wizard.discord_permissions())
        if missing:
            findings.append(Finding("permissions", "fail", clean("; ".join(missing)),
                                    f"The bot needs these channel permissions: {needed}. "
                                    "Set the IDs in config/local.yaml and check channel overrides."))
        else:
            # No allow list is a valid mode (#386): everyone in the channel.
            access = f"{len(users)} allowed user(s)" if users else "channel access (everyone in the print channel)"
            findings.append(Finding("permissions", "ok",
                                    clean(f"print channel set; access: {access}; "
                                          f"required channel permissions: {needed}"),
                                    ""))

        # Service connectivity and health: GET probes only, never a printer operation.
        from .. import doctor
        services = cfg.get("services") or {}
        probe_paths = dict(doctor.SERVICE_PATHS)
        if services.get("octoprint_url"):
            probe_paths["octoprint_url"] = "/api/version"
        for key, path in probe_paths.items():
            url = str(services.get(key) or "")
            label = key.replace("_url", "")
            if not url:
                findings.append(Finding(f"service: {label}", "skip", "not configured", ""))
                continue
            try:
                status = probe(url.rstrip("/") + path)
                findings.append(Finding(f"service: {label}", "ok",
                                        f"answered HTTP {status} on its read-only health path"))
            except Exception as exc:
                findings.append(Finding(f"service: {label}", "warn",
                                        clean("the health check did not answer"),
                                        f"Check the {label} service is running and reachable at "
                                        "the configured URL; debugging did not try to restart it."))

        # Executables and versions.
        findings.append(Finding("executable: python", "ok",
                                f"Python {sys.version_info.major}.{sys.version_info.minor}."
                                f"{sys.version_info.micro}"))
        orca = (cfg.get("orca") or {}).get("command") or []
        if orca:
            exe = orca[0]
            found = which(exe) or (str(exe) if Path(str(exe)).is_file() else None)
            if not found:
                findings.append(Finding("executable: slicer", "fail",
                                        clean(f"{exe} is not on PATH"),
                                        "Install OrcaSlicer or fix [orca] command in the settings."))
            else:
                try:
                    version = version_runner(list(orca))
                    findings.append(Finding("executable: slicer", "ok",
                                            clean(f"{exe} runs: {version}" if version else f"{exe} runs")))
                except Exception as exc:
                    findings.append(Finding("executable: slicer", "warn",
                                            clean(f"{exe} was found but did not run cleanly: {exc}"),
                                            "Run the slicer command in a terminal and fix what it reports."))
        ffmpeg = str((cfg.get("paths") or {}).get("ffmpeg") or "")
        if ffmpeg:
            found = which(ffmpeg) or (ffmpeg if Path(ffmpeg).is_file() else None)
            findings.append(Finding("executable: ffmpeg", "ok" if found else "warn",
                                    "found" if found else "not found; timelapse/video features "
                                    "that need it will fail",
                                    "" if found else "Install ffmpeg or fix [paths] ffmpeg."))

        # State and log locations: existence/access only. Exact paths stay out of the report.
        state = settings.state_dir(cfg)
        if not state.is_dir():
            findings.append(Finding("state and logs", "warn", "the state folder does not exist yet",
                                    "It is created when the bot or a check first runs: "
                                    "`python3 -m print_handy.bot check`."))
        elif not os.access(state, os.W_OK):
            findings.append(Finding("state and logs", "fail", "the state folder is not writable",
                                    "Fix ownership/permissions on the profile/data folder; "
                                    "the bot must save state and its heartbeat there."))
        else:
            log_file = str((cfg.get("logging") or {}).get("file") or "")
            detail = "state folder exists and is writable"
            if log_file:
                parent = Path(log_file).expanduser()
                parent = parent if parent.is_absolute() else state / parent
                detail += "; a log file is configured" + ("" if parent.parent.is_dir()
                                                          else " but its folder is missing")
            findings.append(Finding("state and logs", "ok", detail))
            try:
                from . import health
                heartbeat = health.read()
                if heartbeat:
                    fresh, why = health.evaluate(heartbeat, stale_s=int(cfg["bot"]["health_stale_s"]))
                    findings.append(Finding("bot heartbeat", "ok" if fresh else "warn",
                                            clean(f"standalone heartbeat: {why}"),
                                            "" if fresh else "Start the standalone bot."))
                else:
                    findings.append(Finding("bot heartbeat", "skip",
                                            "no standalone heartbeat found (the bot is not running)", ""))
            except Exception:
                pass
    return findings


def format_report(findings, *, secrets=(), roots=()):
    roots = [Path.home(), *roots]
    lines = ["**Print Handy debugging** — read-only: no settings, services, or printer state were changed."]
    for finding in findings:
        line = f"{_MARKS.get(finding.status, '?')} {finding.name}: " + \
            redact(finding.detail, secrets=secrets, roots=roots)
        lines.append(line)
        if finding.action:
            lines.append("  → Next: " + redact(finding.action, secrets=secrets, roots=roots))
    failed = sum(1 for f in findings if f.status == "fail")
    warned = sum(1 for f in findings if f.status == "warn")
    lines.append(f"Summary: {failed} failed, {warned} warning(s), "
                 f"{sum(1 for f in findings if f.status == 'ok')} ok. "
                 "Anything that would edit configuration, install software, restart a service, "
                 "or change network exposure needs your explicit go-ahead first; otherwise the "
                 "Next steps above are the manual path.")
    return "\n".join(lines)


def debugging_report(**kwargs):
    """The full redacted report text for ``/print-handy debugging``."""
    secrets = list(kwargs.pop("secrets", ()) or ())
    return format_report(run_diagnostics(**kwargs), secrets=secrets)
