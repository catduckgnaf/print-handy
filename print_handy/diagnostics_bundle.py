"""The diagnostics bundle (#390, Stage 3): one file an operator can attach to a bug report.

    python3 -m print_handy.diagnostics_bundle [--output PATH] [--log-lines N]
                                              [--printer-logs]

Assembles, in one Markdown document:

* Print Handy / Python / platform versions and the container image digest
  when the environment records one (``PRINT_HANDY_IMAGE_DIGEST``);
* a **runtime status** section (#1020): the image tag and commit SHA when
  the environment records them (``PRINT_HANDY_IMAGE_TAG`` /
  ``PRINT_HANDY_IMAGE_SHA``), each printer's listener heartbeat age and
  loop lag, the supervisor's persisted restart counts, and the
  capture-directory names with their lengths (the #1020 hang was a
  capture name that outgrew its limit);
* a **redacted config summary** — settings are summarised, never dumped:
  any key whose name suggests a secret is replaced by the name of the
  environment variable that holds it (env-name indirection only), and
  every assembled line passes through ``print_handy.logs.redact``;
* the printer roster (id, model, connection — no hosts, no credentials);
* a **crashes** section (#1404): the per-role crash records
  (:mod:`print_handy.crash_report`) — an unhandled exception in any
  Print Handy process lands there as a redacted structured record, so
  the bundle can carry the crash itself, not just its log shadow;
* the doctor's output;
* the debug session's captured log window when one exists (#478),
  otherwise a bounded tail of the configured log file, plus each
  printer's watchdog stack dump when one exists;
* with ``--printer-logs`` (#1020), the printer-side logs for Moonraker
  printers (console history, Klipper log, ZMod console and screen logs
  when the printer serves them), with each source's clock labeled.

The assembled bundle passes through ``print_handy.sanitize_logs`` — the
export sanitizer layered on ``logs.redact`` — so internal IPs/hostnames,
platform user/channel IDs (as consistent pseudonyms), local paths,
emails, MACs, serials and URL query strings are removed as well as
secrets, and the bundle carries the counts-only sanitization report.

There is no telemetry: the bundle is produced only where the operator
asks (this module's CLI, or ``/print-handy bundle`` in the bot). The CLI
writes a file; the chat command also uploads that file in the chat.
"""
from __future__ import annotations

import argparse
import io
import os
import platform
import sys
import time
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
    """The bounded, redacted tail of the configured log file, or why there is none.

    With no ``[logging]`` file configured, the debug capture file is the
    fallback (#1404): it exists on exactly the installs the old answer
    left with nothing (Docker/stderr, where the mode in #478 writes its
    capture whether or not a log file is set). With neither, the answer
    names both remaining routes instead of only the container log.
    """
    count = max(0, min(lines, MAX_LOG_LINES))
    path = str((data.get("logging") or {}).get("file") or "").strip()
    if not path:
        from . import debug_mode

        capture_text = ""
        try:
            capture = debug_mode.capture_path(data)
            if capture.is_file():
                capture_text = capture.read_text(errors="replace")
        except Exception:
            capture_text = ""
        if capture_text.strip():
            if count == 0:
                return "(no log lines included: the requested line count is 0)"
            tail = capture_text.splitlines()[-count:]
            return ("(no [logging] file is configured; the lines below are from the "
                    "debug capture file instead)\n"
                    + "\n".join(logs.redact(line) for line in tail))
        return ("(no [logging] file is configured; attach the container or systemd log "
                "for the same time window instead. A second route: run a debug session "
                "(`/print-handy debug on`, reproduce the problem, `/print-handy debug off`) "
                "and then build the bundle again, or set a [logging] file)")
    target = logs.current_file(path)  # daily files: the newest is the one being written
    try:
        text = target.read_text(errors="replace")
    except OSError as exc:
        return f"(log file {target} could not be read: {exc})"
    if count == 0:
        # [-0:] is the whole file, so a zero request needs its own answer.
        return "(no log lines included: the requested line count is 0)"
    tail = text.splitlines()[-count:]
    return "\n".join(logs.redact(line) for line in tail) or "(log file is empty)"


def _image_identity() -> str:
    """Image tag + commit SHA as the environment records them (#1020 C1).

    The published images carry these as OCI labels, which a process
    inside the container cannot read back, so the publish, develop and
    nightly workflows also stamp them into the image environment as
    build args (#1332); an operator can still override them. Absent
    values (a local build) are stated, never invented.
    """
    tag = os.environ.get("PRINT_HANDY_IMAGE_TAG", "").strip()
    sha = os.environ.get("PRINT_HANDY_IMAGE_SHA", "").strip()
    return (f"tag {tag or 'not recorded in this environment'}, "
            f"commit {sha or 'not recorded in this environment'}")


def runtime_section(data, *, now=None) -> str:
    """Per-printer liveness as files on disk record it (#1020 C1).

    Heartbeat age and loop lag come from each listener's heartbeat file,
    restart counts from the supervisor's persisted record, and the
    capture-directory listing names the directories the timelapse
    capture created, with their name lengths.
    """
    import json as _json

    from . import healthcheck, supervisor_state

    now = time.time() if now is None else now
    lines = [f"- Image: {_image_identity()}"]
    records = supervisor_state.read_all(data)
    cache = Path(str((data.get("paths") or {}).get("cache") or "")).expanduser()
    capture_root = cache / "timelapse" if str(cache) else None
    for entry in settings.printers(data):
        printer_id = entry.get("id")
        beat_text = "no heartbeat file"
        record = None
        try:
            record = _json.loads(healthcheck.heartbeat_path(data, printer_id).read_text())
        except FileNotFoundError:
            record = None  # no heartbeat file yet: the default text above
        except Exception as exc:
            # A heartbeat that exists but cannot be read or parsed is a
            # different fact from no heartbeat at all (#1404): the old
            # bare swallow reported both as "no heartbeat file".
            record = None
            beat_text = f"heartbeat file unreadable ({type(exc).__name__})"
        if record is not None:
            try:
                age = now - float(record["time"])
                beat_text = f"heartbeat age {age:.0f}s"
                lag = record.get("loop_lag_ms")
                if isinstance(lag, (int, float)):
                    beat_text += f", loop lag {lag / 1000:.1f}s"
                # The camera watch's coverage as the listener reported it (#1320).
                from .runtime import camera_watch
                watch_text = camera_watch.coverage_text(record.get("camera_watch"))
                if watch_text:
                    beat_text += f", camera watch {watch_text}"
            except Exception as exc:
                beat_text = f"heartbeat file unreadable ({type(exc).__name__})"
        sup = records.get(printer_id)
        if sup is None:
            sup_text = "supervisor restarts not recorded"
        else:
            sup_text = (f"supervisor restarts {sup.get('restarts')} of budget "
                        f"{sup.get('budget')} in {sup.get('window_s')}s")
            if sup.get("last_exit_status") is not None:
                sup_text += f", last exit status {sup.get('last_exit_status')}"
            if sup.get("exhausted"):
                sup_text += " — BUDGET EXHAUSTED, listener left down"
        lines.append(f"- {printer_id}: {beat_text}; {sup_text}")
    if capture_root is not None and capture_root.is_dir():
        names = sorted(p.name for p in capture_root.iterdir() if p.is_dir())
        if names:
            lines.append("- Capture directories (name, length in characters):")
            lines += [f"  - {name} ({len(name)})" for name in names[:50]]
            if len(names) > 50:
                lines.append(f"  - … and {len(names) - 50} more")
        else:
            lines.append("- Capture directories: none")
    else:
        lines.append("- Capture directories: no capture root on disk")
    return "\n".join(lines)


def process_logs_section(data, *, log_lines: int = DEFAULT_LOG_LINES) -> str:
    """The watchdog stack dumps, per printer (#1020 C1).

    Bot and listener share the configured log file (its tail is the
    bundle's log section); the supervisor writes no log file — its
    persisted record is the Runtime status section, and its messages go
    to the container log. A hung listener's own artifact is its stack
    dump, tailed here when one exists.
    """
    from . import healthcheck

    parts = []
    for entry in settings.printers(data):
        printer_id = entry.get("id")
        try:
            dump = healthcheck.stack_dump_path(data, printer_id)
            text = dump.read_text(errors="replace") if dump.is_file() else ""
        except Exception:
            text = ""
        if text:
            tail = "\n".join(text.splitlines()[-log_lines:])
            parts.append(f"### Listener stack dump for {printer_id}\n\n```text\n{tail}\n```")
    if not parts:
        return "(no listener stack dumps on disk)"
    return "\n\n".join(parts)


def _fetch_bytes(url, timeout=10):
    """(bytes) for a Moonraker GET, or None on any failure. Never raises."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _tail_lines(raw: bytes, count: int) -> list[str]:
    text = raw[-262_144:].decode("utf-8", "replace")
    return text.splitlines()[-count:]


def printer_logs_section(data, *, log_lines: int = DEFAULT_LOG_LINES, fetch=None) -> str:
    """Printer-side logs for Moonraker printers (#1020 C2).

    Sources, per printer: the console history from Moonraker's
    gcode_store (epoch-anchored, rendered in UTC), and the log files the
    printer serves under its logs root — klippy.log, a ZMod console log
    and a screen log when present. Each file's lines are shown as the
    printer wrote them, under a header naming that source's clock: the
    ZMod console clock is the printer's own and ran about 10 hours
    behind Moonraker time in the #1020 incident, so events are correlated
    through the epoch-anchored console history, never by comparing the
    console file's timestamps with Moonraker's.
    """
    import json as _json
    from datetime import datetime, timezone

    fetch = _fetch_bytes if fetch is None else fetch
    count = max(0, min(log_lines, MAX_LOG_LINES))
    parts = []
    for entry in settings.printers(data):
        printer_id = entry.get("id")
        base = str(entry.get("moonraker_url") or "").rstrip("/")
        if not base:
            parts.append(f"### {printer_id}\n\n(not a Moonraker printer; no printer-side logs pulled)")
            continue
        sections = [f"### {printer_id}"]
        raw = fetch(f"{base}/server/gcode_store?count={max(count, 1)}")
        entries = []
        if raw:
            try:
                store = (_json.loads(raw).get("result") or {}).get("gcode_store") or []
                entries = [e for e in store if isinstance(e, dict)]
            except ValueError:
                entries = []
        if entries:
            lines = []
            for item in entries[-count:]:
                stamp = item.get("time")
                when = (datetime.fromtimestamp(stamp, timezone.utc).strftime("%H:%M:%S")
                        if isinstance(stamp, (int, float)) else "--:--:--")
                lines.append(f"{when} UTC {item.get('message', '')}")
            sections.append("Console history (Moonraker gcode_store; timestamps are "
                            "epoch-anchored and shown in UTC):\n\n```text\n"
                            + "\n".join(lines) + "\n```")
        else:
            sections.append("Console history: unavailable from Moonraker.")
        listing_raw = fetch(f"{base}/server/files/directory?path=logs")
        names = []
        if listing_raw:
            try:
                result = _json.loads(listing_raw).get("result") or {}
                names = [str(item.get("path") or item.get("name") or "")
                         for item in result.get("dirs_files", result.get("files", []))
                         if isinstance(item, dict)]
            except ValueError:
                names = []
        wanted = []
        for name in names:
            low = name.lower()
            if low == "klippy.log":
                wanted.append((name, "Klipper log (klippy.log; lines as written, "
                                      "its clock is the printer's)"))
            elif "console" in low and low.endswith(".log"):
                wanted.append((name, "ZMod console log (its timestamps are the console's own "
                                     "clock, which ran about 10 hours behind Moonraker time "
                                     "in the #1020 incident; correlate via the console history above)"))
            elif ("helix" in low or "screen" in low) and low.endswith(".log"):
                wanted.append((name, "Screen log (lines as written, printer clock)"))
        for name, label in wanted[:4]:
            raw_file = fetch(f"{base}/server/files/logs/{name}")
            if raw_file is None:
                sections.append(f"{label}: unavailable.")
            else:
                sections.append(f"{label}:\n\n```text\n"
                                + "\n".join(_tail_lines(raw_file, count)) + "\n```")
        if not wanted:
            sections.append("Log files: the printer serves no klippy, console or screen log "
                            "under its logs root.")
        parts.append("\n\n".join(sections))
    return "\n\n".join(parts) or "(no printers configured)"


def doctor_output() -> str:
    from . import doctor
    out = io.StringIO()
    try:
        doctor.run(out)
    except Exception as exc:  # the bundle must survive a broken doctor
        return f"(doctor did not complete: {type(exc).__name__}: {exc})"
    return out.getvalue().strip() or "(doctor produced no output)"


def _keep_roots(data):
    """The app's own data and install directories (#1020), as
    (root, label) pairs: paths under these keep their layout in an
    export, relabeled relative to the label; every other local path is
    removed."""
    labels = {"state_home": "app-data", "cache": "app-cache",
              "archive": "app-archive", "records_vault": "app-records"}
    roots = []
    for key, label in labels.items():
        value = ((data or {}).get("paths") or {}).get(key)
        if value:
            roots.append((str(Path(str(value)).expanduser()), label))
    roots.append((str(Path(__file__).resolve().parent.parent), "app-install"))
    return roots


def _sanitizer(data=None):
    """The export sanitizer for these settings (#1020): known platform
    IDs, the public_text.denylist machine names, and the app's own
    path roots. Shared by the bundle and the debug-mode export so both
    apply one standard."""
    if not data:
        data, _error = _load_settings()
    users, channels = _known_ids(data or {})
    try:
        names = settings.public_text_denylist(data)
    except Exception:
        names = []
    return sanitize_logs.Sanitizer(known_user_ids=users, known_channel_ids=channels,
                                   machine_names=names, keep_path_roots=_keep_roots(data or {}))


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
                 include_doctor: bool = True, include_printer_logs: bool = False) -> str:
    """The whole bundle as one Markdown string. Never raises on a section."""
    load_error = None
    if data is None:
        data, load_error = _load_settings()
    return _assemble(data, log_lines=log_lines, include_doctor=include_doctor,
                     load_error=load_error, include_printer_logs=include_printer_logs)


def _section(fn, *args, **kwargs) -> str:
    """One bundle section, degrading to a note instead of raising."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return f"(unavailable: {type(exc).__name__}: {exc})"


def _assemble(data, *, log_lines: int, include_doctor: bool, load_error,
              include_printer_logs: bool = False) -> str:
    digest = next((os.environ[name] for name in _DIGEST_ENVS if os.environ.get(name)),
                  "not recorded in this environment (standalone install or unset)")
    from . import crash_report, debug_mode

    if load_error is not None:
        failure = f"{type(load_error).__name__}: {load_error}"
        config_text = f"(configuration could not be loaded: {failure})"
        roster_text = "- (unavailable: the configuration did not load)"
        log_title = "Recent log tail"
        log_text = f"(no log tail is available: the configuration did not load: {failure})"
    else:
        config_text = config_summary(data)
        roster_text = roster_summary(data)
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
        f"- Debug mode: {_section(debug_mode.status_text, data)}",
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
        "## Runtime status",
        "",
        _section(runtime_section, data) if load_error is None else "(unavailable: the configuration did not load)",
        "",
        "## Crashes",
        "",
        _section(crash_report.section, data),
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
        "## Listener stack dumps",
        "",
        _section(process_logs_section, data, log_lines=log_lines) if load_error is None else "(unavailable: the configuration did not load)",
        "",
    ]
    if include_printer_logs:
        parts += [
            "## Printer logs",
            "",
            _section(printer_logs_section, data, log_lines=log_lines) if load_error is None else "(unavailable: the configuration did not load)",
            "",
        ]
    sanitizer = _sanitizer(data)
    clean = sanitizer.sanitize("\n".join(parts))
    return clean.replace("{REPORT}", sanitizer.report())


def write_bundle(path=None, data=None, *, log_lines: int = DEFAULT_LOG_LINES,
                 include_printer_logs: bool = False) -> Path:
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
                                load_error=load_error,
                                include_printer_logs=include_printer_logs))
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Assemble a redacted Print Handy diagnostics bundle.")
    parser.add_argument("--output", help=f"where to write it (default: <state>/{BUNDLE_NAME})")
    parser.add_argument("--log-lines", type=int, default=DEFAULT_LOG_LINES,
                        help=f"log tail length (default {DEFAULT_LOG_LINES}, max {MAX_LOG_LINES})")
    parser.add_argument("--printer-logs", action="store_true",
                        help="also pull the printer-side logs (console history, Klipper, "
                             "ZMod console and screen logs) for Moonraker printers")
    args = parser.parse_args(argv)
    target = write_bundle(args.output, log_lines=args.log_lines,
                          include_printer_logs=args.printer_logs)
    print(f"Diagnostics bundle written to {target}. Review it before attaching; "
          "it is redacted, but it describes your setup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
