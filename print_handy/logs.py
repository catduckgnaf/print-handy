"""Shared logging setup for the Print Handy processes (#150).

``configure()`` is called once by each process entrypoint (the standalone bot and the printer listener).
It is never called on import, so Print Handy running as a plugin inside another program (the Hermes
gateway) leaves that program's logging alone. Modules just use ``logging.getLogger(__name__)``.

What it gives every log line:

* a timestamp with time zone, a severity and the logger name (the component), as text or one JSON object per line
* optional bound context such as the intake id (``bind``), appended to every line while it is bound
* redaction of Discord, Telegram and Matrix tokens, bearer tokens, ``token=``/``password=`` style values, URL credentials and the
  value of any ``*_TOKEN``/``*_KEY``/``*_SECRET``/``*_PASSWORD`` environment variable, in the message,
  the arguments and the traceback, for every handler

``stage()`` wraps a multi-step operation: it logs the start, the elapsed time on success, and the elapsed time
plus the traceback on failure, then re-raises.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
FORMATS = ("text", "json")
FILE_MAX_BYTES = 5 * 1024 * 1024
FILE_BACKUPS = 3
REDACTED = "***"

_HANDLER_MARK = "_print_handy_handler"
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("print_handy_log_context", default={})

_SECRET_ENV_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")
_PATTERNS = (
    # Matrix access token: the syt_... form a homeserver issues. Fires
    # bare, in an Authorization header and after ?access_token= alike.
    (re.compile(r"\bsyt_[A-Za-z0-9_\-]{10,}(?:[.=][A-Za-z0-9_\-]+)*"), REDACTED),
    # Telegram bot token: bot id, colon, secret. Also fires inside an
    # api.telegram.org/bot<token>/ URL, where the token is in the path.
    (re.compile(r"\d{5,16}:[A-Za-z0-9_-]{30,40}\b"), REDACTED),
    # Discord bot token: base64 user id, timestamp, hmac.
    (re.compile(r"[MNO][A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}"), REDACTED),
    (re.compile(r"(?i)\b(bearer|bot|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # token=abc, "password": "abc", api_key: abc, Authorization: abc (keeps the key, hides the value).
    (re.compile(r"(?i)((?:[\w-]*(?:token|secret|passw(?:or)?d|api[_-]?key|apikey|access[_-]?key|authorization|signature))"
                r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}]+)"), r"\1" + REDACTED),
    # https://user:pass@host
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
)


def redact(text: str) -> str:
    """Hide credentials in a string. Safe to call on any text; unmatched text is returned unchanged."""
    if not text:
        return text
    for name, value in os.environ.items():
        if len(value) >= 8 and name.upper().endswith(_SECRET_ENV_SUFFIXES):
            text = text.replace(value, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def bind(**fields) -> contextvars.Token:
    """Add fields (for example ``intake=iid``) to every line logged from this task until ``unbind``."""
    return _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})


def unbind(token: contextvars.Token) -> None:
    _context.reset(token)


def bound_context() -> dict:
    """A copy of the fields currently bound to every log line."""
    return dict(_context.get())


def clear_context() -> None:
    """Drop every bound field. For hosts that run Print Handy in-process
    (and for tests): a listener launched in its own process never needs it,
    because its ``printer_id`` binding is process-lifetime by design."""
    _context.set({})


def _pairs(fields: dict) -> str:
    return " ".join(f"{key}={_value(value)}" for key, value in fields.items())


def _value(value) -> str:
    text = str(value)
    return json.dumps(text) if (not text or re.search(r"[\s=\"]", text)) else text


class _Base(logging.Formatter):
    def timestamp(self, record) -> str:
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")

    def fields(self, record) -> dict:
        return {**_context.get(), **(getattr(record, "ctx", None) or {})}

    def exception_text(self, record) -> str:
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        return record.exc_text or ""


class TextFormatter(_Base):
    """``2026-10-24T08:01:02.123-04:00 ERROR print_handy.core.orca_adapter: message key=value`` plus a traceback."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{self.timestamp(record)} {record.levelname} {record.name}: {record.getMessage()}"
        fields = self.fields(record)
        if fields:
            line += " " + _pairs(fields)
        trace = self.exception_text(record)
        if trace:
            line += "\n" + trace
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return redact(line)


def _redact_value(value):
    """Redact credentials inside one payload value, before serialization.

    Redacting the already-serialized JSON cannot work: escaping rewrites the
    quotes the credential patterns anchor on, so a secret inside a quoted
    value survives, and a replacement can eat an escape character and leave
    the line invalid JSON. Redact each string value while it is still a
    plain string, then serialize.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class JsonFormatter(_Base):
    """One JSON object per line, for log shippers: ts, level, logger, message, any bound fields, exception."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.timestamp(record), "level": record.levelname, "logger": record.name,
                   "message": record.getMessage()}
        payload.update({k: v for k, v in self.fields(record).items() if k not in payload})
        trace = self.exception_text(record)
        if trace:
            payload["exception"] = trace
        return json.dumps(_redact_value(payload), default=str, ensure_ascii=False)


def _formatter(kind: str) -> logging.Formatter:
    return JsonFormatter() if kind == "json" else TextFormatter()


def _logging_settings() -> dict:
    try:
        from . import settings
        return dict(settings.current().get("logging") or {})
    except Exception as exc:  # a broken config must not stop logging; the config check reports it
        print(f"logging: using defaults, settings did not load: {redact(str(exc))}", file=sys.stderr)
        return {}


def configure(cfg: dict | None = None, *, stream=None) -> logging.Logger:
    """Install the Print Handy handlers on the root logger. Calling it again replaces them.

    ``cfg`` is the ``[logging]`` settings section (level, format, file); None reads it from the settings.
    Output goes to ``stream`` (default stderr, which systemd and Docker capture) and, when ``file`` is set,
    to a rotating file as well.
    """
    cfg = _logging_settings() if cfg is None else cfg
    level = str(cfg.get("level") or "INFO").upper()
    kind = str(cfg.get("format") or "text").lower()
    if level not in LEVELS:
        level = "INFO"
    if kind not in FORMATS:
        kind = "text"
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    path = str(cfg.get("file") or "").strip()
    if path:
        target = Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                target, maxBytes=FILE_MAX_BYTES, backupCount=FILE_BACKUPS, encoding="utf-8"))
        except OSError as exc:
            print(f"logging: cannot write {target}, logging to stderr only: {exc}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(_formatter(kind))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(getattr(logging, level))
    # Library debug output (gateway heartbeats, socket frames) buries ours. Keep it at INFO in debug mode.
    for noisy in ("discord", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.INFO if level == "DEBUG" else logging.NOTSET)
    # Per-subsystem verbosity (#390 stage 4): [logging] levels maps a logger
    # name (for example "print_handy.core.orca_adapter") to its own level,
    # applied after the defaults above so it always wins for that subtree.
    for name, sub_level in sorted((cfg.get("levels") or {}).items()):
        sub_level = str(sub_level).upper()
        if sub_level in LEVELS:
            logging.getLogger(str(name)).setLevel(getattr(logging, sub_level))
    # Debug log mode (#478): a session started by any surface is shared
    # state in the state folder; applying it here means every process that
    # configures logging (bot, listeners, CLI) joins the capture without a
    # config edit or a restart of the others. Best-effort by design: a
    # broken state file must never stop logging.
    try:
        from . import debug_mode
        debug_mode.apply()
    except Exception:
        pass
    return root


@contextlib.contextmanager
def stage(logger: logging.Logger, name: str, **fields):
    """Log ``name`` starting, then its elapsed time, or its elapsed time and traceback if it raises.

    Works around ``await`` too. Cancellation (``BaseException`` such as a Stop press) is logged as
    interrupted without a traceback, and an ``Exception`` is logged with one. Both are re-raised.
    """
    started = time.monotonic()
    extra = {"ctx": fields} if fields else None
    logger.info("%s started", name, extra=extra)
    try:
        yield
    except Exception as exc:
        logger.error("%s failed after %.1fs: %s: %s", name, time.monotonic() - started,
                     type(exc).__name__, exc, exc_info=True, extra=extra)
        raise
    except BaseException as exc:
        logger.warning("%s interrupted after %.1fs (%s)", name, time.monotonic() - started,
                       type(exc).__name__, extra=extra)
        raise
    logger.info("%s done in %.1fs", name, time.monotonic() - started, extra=extra)
