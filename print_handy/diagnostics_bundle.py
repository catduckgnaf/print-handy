"""The diagnostics bundle (#390, Stage 3): one file an operator can attach to a bug report.

    python3 -m print_handy.diagnostics_bundle [--output PATH] [--log-lines N]

Assembles, in one Markdown document:

* Print Handy / Python / platform versions and the container image digest
  when the environment records one (``PRINT_HANDY_IMAGE_DIGEST``);
* a **redacted config summary** — settings are summarised, never dumped:
  any key whose name suggests a secret is replaced by the name of the
  environment variable that holds it (env-name indirection only), and
  every assembled line passes through ``print_handy.logs.redact``;
* the printer roster (id, model, connection — no hosts, no credentials);
* the doctor's output;
* the debug session's captured log window when one exists (#478),
  otherwise a bounded tail of the configured log file.

The assembled bundle passes through ``print_handy.sanitize_logs`` — the
export sanitizer layered on ``logs.redact`` — so internal IPs/hostnames,
platform user/channel IDs (as consistent pseudonyms), local paths,
emails, MACs, serials and URL query strings are removed as well as
secrets, and the bundle carries the counts-only sanitization report.

There is no telemetry: the bundle is written only where the operator asks
(this module's CLI, or ``/print-handy bundle`` in the bot, which writes it
into the state folder and says where). Nothing is uploaded anywhere.
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import sys
from pathlib import Path

from . import __version__, logs, sanitize_logs, settings

DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 2000
BUNDLE_NAME = "diagnostics-bundle.md"
_SECRET_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey", "access_code", "credential")
_DIGEST_ENVS = ("PRINT_HANDY_IMAGE_DIGEST", "IMAGE_DIGEST")


def _is_secret_key(key: str) -> bool:
    return any(part in key.lower() for part in _SECRET_KEY_PARTS)


def _summarise(value, key: str = ""):
    """A redacted, summary-shaped rendering of one settings value.

    Secret-shaped keys never render their value: ``*_env`` keys render the
    environment variable's NAME (that is the indirection, and the name is
    not a secret) plus whether it is set; any other secret-shaped value is
    replaced outright. Long strings are truncated; nested tables summarise
    key-by-key; lists render as counts plus scalar items.
    """
    if _is_secret_key(key):
        if key.endswith("_env") and isinstance(value, str) and value:
            return f"${value} ({'set' if os.environ.get(value) else 'not set'})"
        return "*** (redacted)"
    if isinstance(value, dict):
        return {k: _summarise(v, k) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_summarise(v) for v in value]
    if isinstance(value, str) and len(value) > 80:
        # Redact BEFORE truncating: a URL credential whose @ sits past the
        # cut would otherwise survive as a bare password fragment, because
        # the redaction pass in config_summary can no longer see the @.
        return logs.redact(value)[:77] + "..."
    return value


def config_summary(data) -> str:
    """The redacted config summary section: section names + summarised values."""
    lines = []
    for section in sorted(data):
        if section == "printers":
            continue  # the roster gets its own section, without hosts
        value = data[section]
        if isinstance(value, dict):
            lines.append(f"- [{section}]")
            for key in sorted(value):
                lines.append(f"  - {key}: {_summarise(value[key], key)}")
        else:
            lines.append(f"- {section}: {_summarise(value, section)}")
    return logs.redact("\n".join(lines))


def roster_summary(data) -> str:
    lines = []
    for entry in settings.printers(data):
        lines.append(f"- {entry.get('id')}: model={entry.get('model')}")
    return "\n".join(lines) or "- (no printers configured)"


def log_tail(data, lines: int = DEFAULT_LOG_LINES) -> str:
    """The bounded, redacted tail of the configured log file, or why there is none."""
    path = str((data.get("logging") or {}).get("file") or "").strip()
    if not path:
        return ("(no [logging] file is configured; attach the container or systemd log "
                "for the same time window instead)")
    target = Path(path).expanduser()
    try:
        text = target.read_text(errors="replace")
    except OSError as exc:
        return f"(log file {target} could not be read: {exc})"
    count = max(0, min(lines, MAX_LOG_LINES))
    if count == 0:
        # [-0:] is the whole file, so a zero request needs its own answer.
        return "(no log lines included: the requested line count is 0)"
    tail = text.splitlines()[-count:]
    return "\n".join(logs.redact(line) for line in tail) or "(log file is empty)"


def doctor_output() -> str:
    from . import doctor
    out = io.StringIO()
    try:
        doctor.run(out)
    except Exception as exc:  # the bundle must survive a broken doctor
        return f"(doctor did not complete: {type(exc).__name__}: {exc})"
    return out.getvalue().strip() or "(doctor produced no output)"


def _known_ids(data):
    """Platform IDs the config itself names, for consistent pseudonyms."""
    users, channels = [], []
    for section in ("discord", "telegram", "matrix"):
        cfg = data.get(section) or {}
        raw_users = cfg.get("allowed_user_ids")
        if isinstance(raw_users, (list, tuple)):
            users.extend(str(v) for v in raw_users)
        elif raw_users:
            users.extend(str(raw_users).replace(",", " ").split())
        users.extend(str(cfg.get("approval_user_id") or "").replace(",", " ").split())
        if cfg.get("bot_user_id"):
            users.append(str(cfg["bot_user_id"]))
        for key in ("chat_id", "room_id"):
            if cfg.get(key):
                channels.append(str(cfg[key]))
    return users, channels


def _load_settings():
    """(data, error): the current settings, or ({}, the failure) when they do not load.

    A bundle exists to report a broken setup, so a settings failure is
    bundle content, not a reason to produce no bundle at all.
    """
    try:
        return settings.current(), None
    except Exception as exc:
        return {}, exc


def build_bundle(data=None, *, log_lines: int = DEFAULT_LOG_LINES,
                 include_doctor: bool = True) -> str:
    """The whole bundle as one Markdown string. Never raises on a section."""
    load_error = None
    if data is None:
        data, load_error = _load_settings()
    return _assemble(data, log_lines=log_lines, include_doctor=include_doctor,
                     load_error=load_error)


def _assemble(data, *, log_lines: int, include_doctor: bool, load_error) -> str:
    digest = next((os.environ[name] for name in _DIGEST_ENVS if os.environ.get(name)),
                  "not recorded in this environment (standalone install or unset)")
    if load_error is not None:
        failure = f"{type(load_error).__name__}: {load_error}"
        config_text = f"(configuration could not be loaded: {failure})"
        roster_text = "- (unavailable: the configuration did not load)"
        log_title = "Recent log tail"
        log_text = f"(no log tail is available: the configuration did not load: {failure})"
    else:
        config_text = config_summary(data)
        roster_text = roster_summary(data)
        from . import debug_mode
        window = debug_mode.session_window(data)
        if window is not None:
            log_title = "Debug session log (sanitized; see docs/DIAGNOSTICS.md)"
            log_text = window
        else:
            shown = max(0, min(log_lines, MAX_LOG_LINES))
            log_title = f"Recent log tail (last {shown} lines, sanitized)"
            log_text = log_tail(data, log_lines)
    parts = [
        "# Print Handy diagnostics bundle",
        "",
        f"- Print Handy: {__version__}",
        f"- Python: {platform.python_version()}",
        f"- Platform: {platform.platform()}",
        f"- Image digest: {digest}",
        "",
        "## Sanitization",
        "",
        "{REPORT}",
        "",
        "## Configuration summary (redacted)",
        "",
        config_text,
        "",
        "## Printers",
        "",
        roster_text,
        "",
        "## Doctor",
        "",
        "```text",
        doctor_output() if include_doctor else "(skipped)",
        "```",
        "",
        f"## {log_title}",
        "",
        "```text",
        log_text,
        "```",
        "",
    ]
    users, channels = _known_ids(data)
    sanitizer = sanitize_logs.Sanitizer(known_user_ids=users, known_channel_ids=channels)
    clean = sanitizer.sanitize("\n".join(parts))
    return clean.replace("{REPORT}", sanitizer.report())


def write_bundle(path=None, data=None, *, log_lines: int = DEFAULT_LOG_LINES) -> Path:
    """Write the bundle (default: ``<state>/diagnostics-bundle.md``); returns the path."""
    load_error = None
    if data is None:
        data, load_error = _load_settings()
    if path:
        target = Path(path)
    else:
        try:
            target = settings.state_dir(data) / BUNDLE_NAME
        except Exception:
            # The state folder comes from the settings that just failed to
            # load; the bundle still has to land somewhere writable.
            target = Path(BUNDLE_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_assemble(data, log_lines=log_lines, include_doctor=True,
                                load_error=load_error))
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a redacted Print Handy diagnostics bundle.")
    parser.add_argument("--output", help=f"where to write it (default: <state>/{BUNDLE_NAME})")
    parser.add_argument("--log-lines", type=int, default=DEFAULT_LOG_LINES,
                        help=f"log tail length (default {DEFAULT_LOG_LINES}, max {MAX_LOG_LINES})")
    args = parser.parse_args(argv)
    target = write_bundle(args.output, log_lines=args.log_lines)
    print(f"Diagnostics bundle written to {target}. Review it before attaching; "
          "it is redacted, but it describes your setup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
