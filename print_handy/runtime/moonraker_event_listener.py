#!/usr/bin/env python3
"""Event driven replacement for the old 5 minute Moonraker cron job. No AI agent involved.

Holds a websocket subscription to Moonraker print_stats. On every print state change
(and on every (re)connect) it runs the existing announcer logic in
moonraker_discord_announcements.main(), captures what it would have printed, and
posts that to #3d-printer through the bot's own Discord REST sender
(bot token only, no LLM, no Hermes CLI).

A light local tick (default 10 min) re-runs the same logic so the 2 hour post print
quiet reset and the 4 AM ET backup reset fire without any external scheduler. The
tick makes no model calls and posts nothing unless a transition occurred.
"""

import asyncio
import concurrent.futures
import contextlib
import functools
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import websockets

from .. import __version__, ai_routes, camera_url, debug_mode, go2rtc, healthcheck, logs, printers, settings
from ..core import delivery, filament_color, launch_state, printer_clock, spool_canon, spoolman_client
from . import _config
from . import moonraker_discord_announcements as announcer
from . import discord_print_summary as print_summary
from . import listener_common as _listener_common
from . import starter_pings
from . import start_claims
from . import plan_timelapse
from . import timelapse_capture
from . import timelapse_encode
from . import print_control
from . import light_control
from . import visual_watch as _visual_watch
from . import camera_watch as _camera_watch
from . import local_vision as _local_vision
from . import lane_data_sync
from . import print_markers
from . import print_records
from . import vertical_short
INTERRUPTED_HINT = print_control.INTERRUPTED_HINT
try:
    from . import maintenance as maintenance_state
except ImportError:
    maintenance_state = None

_LISTENER = _config.cfg()["listener"]
WS_URL = str(_config.cfg()["services"]["moonraker_ws_url"])
DISCORD_TARGET = _listener_common.DISCORD_TARGET


class _ModuleSeams:
    """This listener's own globals, read live, as the namespace listener_common resolves seams through.

    ``sys.modules[__name__]`` is not this module when the launcher runs it
    with ``runpy.run_module(..., run_name="__main__")`` (listeners.py):
    runpy executes the code in a fresh namespace and leaves
    ``sys.modules["__main__"]`` as the launcher, so every seam lookup
    failed with "module '__main__' has no attribute ..." in the 2.3.0
    container. Reading ``globals()`` works under both an import and
    runpy, and still sees a test's monkeypatch of a module attribute.
    """

    __slots__ = ("_globals",)

    def __init__(self, namespace):
        self._globals = namespace

    def __getattr__(self, name):
        try:
            return self._globals[name]
        except KeyError:
            raise AttributeError(name) from None


_SELF = _ModuleSeams(globals())  # the lookup namespace listener_common resolves this listener's seams through
TICK_SECONDS = float(_LISTENER["tick_s"])
LOG = announcer.PROFILE_HOME / "logs" / "moonraker_event_listener.log"
TERMINAL = {"cancelled": "Print cancelled", "error": "Print stopped with an error"}


logger = logging.getLogger("print_handy.runtime.moonraker_event_listener")  # fixed: runpy runs this as __main__
_PROBLEM = re.compile(r"\b(failed|failure|error|lost|refused|could not|cannot|unreachable|timed out)\b", re.I)


_log_day = {}


def _log_file():
    """Today's legacy listener log file, ``<LOG stem>-YYYY-MM-DD.log``.

    The file used to grow for ever. It now follows the shared log files'
    rule (logs.DailyFileHandler): one dated file per day beside LOG, never
    renamed (several listener processes can share the folder), and dated
    files older than logs.FILE_RETENTION_DAYS are pruned once a day.
    """
    day = datetime.now().strftime("%Y-%m-%d")
    path = LOG.with_name(f"{LOG.stem}-{day}{LOG.suffix or '.log'}")
    if _log_day.get(LOG) != day:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        logs.prune(LOG, logs.FILE_RETENTION_DAYS, today=day)
        _log_day[LOG] = day
    return path


def log(line):
    """Record one listener event: the legacy log file, and the shared logger (stderr, journald, docker logs).

    A line that reports a failure is logged as a warning, and the traceback of the exception being handled
    when it is called, if any, is kept. Credentials are redacted in both places.
    """
    line = logs.redact(str(line))
    with _log_file().open("a") as fh:
        fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {line}\n")
    logger.log(logging.WARNING if _PROBLEM.search(line) else logging.INFO, "%s", line,
               exc_info=sys.exc_info() if sys.exc_info()[0] else None)


def _platform_name():
    """The standalone bot's chat platform ([bot] platform, #164); 'discord' unless configured."""
    return _listener_common._platform_name()


def _telegram_standalone():
    return _listener_common._telegram_standalone(ns=_SELF)


def _telegram_token():
    """The Telegram bot token, from the variable [telegram] token_env names."""
    return _listener_common._telegram_token(ns=_SELF)


def _telegram_chat_id():
    return _listener_common._telegram_chat_id()


def _matrix_standalone():
    return _listener_common._matrix_standalone(ns=_SELF)


def _buttonless_standalone():
    """The platform name when the bot's posts have no Discord buttons, else None (listener_common)."""
    return _listener_common._buttonless_standalone(ns=_SELF)


def _matrix_token():
    """The Matrix access token, from the variable [matrix] access_token_env names."""
    return _listener_common._matrix_token(ns=_SELF)


def _matrix_homeserver():
    return _listener_common._matrix_homeserver()


def _matrix_room_id():
    return _listener_common._matrix_room_id()


def post(message, *, mention_user_id=None, ping_kind=None, ping_job_key=None, ping_episode=None):
    """Deliver physical print lifecycle text to the print chat only, with no agent (listener_common.post).

    What the chat got goes to web chat too (bot/web_feed.py).
    """
    return _listener_common.post(message, mention_user_id=mention_user_id, ping_kind=ping_kind,
                                 ping_job_key=ping_job_key, ping_episode=ping_episode, ns=_SELF)



# Web chat delivery (bot/web_feed.py) lives in listener_common with the post
# path; these are the long-standing names every listener calls.
_WEB_ALERT_POSTED = _listener_common._WEB_ALERT_POSTED
_web_scoped = _listener_common._web_scoped


def _web_publish(message, alert=None):
    """Hand one post to web chat's feed (listener_common._web_publish). Never raises."""
    return _listener_common._web_publish(message, alert=alert, ns=_SELF)


def _web_alert(message, kind, filename, *, connection=None, printer_id=None, identity=None):
    """One alert to web chat with its buttons (listener_common._web_alert). Never raises."""
    return _listener_common._web_alert(message, kind, filename, connection=connection,
                                       printer_id=printer_id, identity=identity, ns=_SELF)


def _post_rest(message, mention_user_id=None):
    """The configured platform's REST sender with its bot token (listener_common._post_rest)."""
    return _listener_common._post_rest(message, mention_user_id=mention_user_id, ns=_SELF)


def _post_rest_discord(message, mention_user_id=None):
    """Discord delivery through the bot's own REST sender (listener_common._post_rest_discord)."""
    return _listener_common._post_rest_discord(message, mention_user_id=mention_user_id, ns=_SELF)


@_web_scoped
def post_alert(message, kind, filename, episode=None):
    """Post with Resume/Cancel buttons; fall back to plain text if Discord refuses.

    On Telegram the alert carries its buttons as an inline keyboard
    (#1291): the same recorded alert, and the press returns through the
    Telegram adapter's callback polling. On Matrix the alert carries
    its actions as reactions (#1291): the same recorded alert, the
    alert text names one reaction per action, the bot seeds them on
    its own message, and a reaction returns through the Matrix
    adapter's /sync ingest. Web chat gets the alert with the same
    actions as buttons on every install (_web_alert).
    """
    from .. import listeners as _web_listeners
    _web_alert(message, kind, filename, printer_id=_web_listeners.instance_printer_id())
    # An alert that needs a person pings the print's starter (#1095): the
    # classifier decides from the alert kind and the job behind the
    # filename — once per episode, so a re-posted or refreshed alert the
    # caller sends again asks the classifier again and gets nobody. The
    # decision is made once here and threaded to whichever transport
    # delivers, so the button post and its plain-text fallback can never
    # ping twice for the same alert.
    if episode is None:
        # A pause's episode is its cause, so a different problem on the
        # same print pings again: the pause call sites pass theirs
        # explicitly (they classified the pause already); the cause is
        # read back out of the alert text for any caller that does not.
        # Every other kind's episode is the kind itself.
        episode = f"paused:{classify_pause(message)}" if kind == "paused" else kind
    mention = starter_pings.ping_for(kind, job_key=filename, episode=episode) or None
    delivered = None
    if _telegram_standalone():
        try:
            from . import print_alert_buttons as pab
            kwargs = {}
            from .. import listeners as _listeners
            # The alert's buttons act on this listener's printer, as on Discord.
            if _listeners.instance_printer_id():
                kwargs["printer_id"] = _listeners.instance_printer_id()
            if pab.post_telegram_with_buttons(_telegram_token(), _telegram_chat_id(),
                                              message, kind, filename, **kwargs):
                log(f"post telegram buttons kind={kind} {message.splitlines()[0][:120]!r}")
                return True
            log("telegram button post failed; falling back to plain text")
        except Exception as exc:
            log(f"telegram button post error: {exc!r}")
        delivered = post(message, **({"mention_user_id": mention} if mention else {}))
    elif _matrix_standalone():
        try:
            from . import print_alert_buttons as pab
            kwargs = {"mention_user_id": mention} if mention else {}
            from .. import listeners as _listeners
            # The alert's reactions act on this listener's printer, as on Discord.
            if _listeners.instance_printer_id():
                kwargs["printer_id"] = _listeners.instance_printer_id()
            if pab.post_matrix_with_reactions(_matrix_homeserver(), _matrix_token(),
                                              _matrix_room_id(), message, kind, filename, **kwargs):
                log(f"post matrix reactions kind={kind} {message.splitlines()[0][:120]!r}")
                return True
            log("matrix reaction post failed; falling back to plain text")
        except Exception as exc:
            log(f"matrix reaction post error: {exc!r}")
        delivered = post(message, **({"mention_user_id": mention} if mention else {}))
    elif _buttonless_standalone():
        delivered = post(message, **({"mention_user_id": mention} if mention else {}))
    else:
        try:
            from . import print_alert_buttons as pab
            kwargs = {"mention_user_id": mention} if mention else {}
            from .. import listeners as _listeners
            # The alert's buttons act on this listener's printer, not the default one (a
            # listener with no instance is the default printer's; the alert records that).
            if _listeners.instance_printer_id():
                kwargs["printer_id"] = _listeners.instance_printer_id()
            if pab.post_with_buttons(_bot_token(), announcer.DISCORD_CHAT_ID,
                                     message, kind, filename, **kwargs):
                log(f"post buttons kind={kind} {message.splitlines()[0][:120]!r}")
                return True
            log("button post failed; falling back to plain text")
        except Exception as exc:
            log(f"button post error: {exc!r}")
        delivered = post(message, **({"mention_user_id": mention} if mention else {}))
    if not delivered and mention:
        # Every delivery attempt failed, so the recorded ping never
        # landed: release it, and a re-send of this alert can ping.
        starter_pings.release_ping(kind, job_key=filename, episode=episode)
    return delivered


def post_plate_picker(plan_id):
    """Post the interactive remaining-plate picker; name the command only when it cannot be delivered."""
    if _buttonless_standalone():
        try:
            # Register the pending offer here too (P2-246): the only
            # remember() call used to sit in the Discord branch below,
            # so on buttonless platforms (Telegram/Matrix) the plan was
            # never registered and the empty-bed next-plate offer could
            # never become due.
            from . import next_plate_offer
            next_plate_offer.remember(plan_id)
        except Exception as exc:
            log(f"next-plate offer registration failed plan={plan_id}: {exc!r}")
        return post(f"To start the next plate, type `/print-next {plan_id}`.")
    try:
        from . import print_alert_buttons as pab
        from . import print_next
        from .print_records import remaining_plate_choices
        plan = print_next.load_plan(plan_id)
        parts = remaining_plate_choices(plan)
        if not parts:
            return False
        from . import next_plate_offer
        next_plate_offer.remember(plan_id)
        options = [{"label": print_next.plate_choice_label(part), "value": str(part["id"])} for part in parts]
        if pab.post_plate_picker(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 "Which plate is next? Clear the bed, then choose a plate.", plan_id, options):
            log(f"post plate picker plan={plan_id} plates={len(options)}")
            return True
        log(f"plate picker post failed plan={plan_id}; falling back to text")
    except Exception as exc:
        log(f"plate picker post error plan={plan_id}: {exc!r}")
    return post(f"To start the next plate, type `/print-next {plan_id}`.")


def post_bed_clear_offer(choice):
    """Post the empty-bed offer with the existing confirm button. Starts nothing.

    Buttonless platforms return False so the caller posts the text, which
    names `/print-next` and does not start a plate.
    """
    if _buttonless_standalone():
        return False
    plan_id = str((choice or {}).get("plan_id") or "")
    part_id = str((choice or {}).get("part_id") or "")
    text = str((choice or {}).get("text") or "")
    if not plan_id or not part_id or not text:
        return False
    try:
        from . import print_alert_buttons as pab
        if pab.post_plate_confirm(_bot_token(), announcer.DISCORD_CHAT_ID, text, plan_id, part_id):
            log(f"post next-plate offer plan={plan_id} part={part_id}")
            # Web chat gets the offer's text, which names `/print-next`:
            # typed there, it runs the same confirmed next-plate flow.
            _web_publish(text)
            return True
        log(f"next-plate offer button post failed plan={plan_id}; falling back to text")
    except Exception as exc:
        log(f"next-plate offer button post error plan={plan_id}: {exc!r}")
    return False


def post_insertion_confirmation(slot):
    """Offer an answer-only IFS insertion UI, then retain the command-text fallback."""
    if _buttonless_standalone():
        return post(insertion_prompt(slot))
    try:
        from . import print_alert_buttons as pab
        text = insertion_confirmation_text(slot)
        if pab.post_insertion_confirmation(_bot_token(), announcer.DISCORD_CHAT_ID,
                                           text, slot):
            log(f"post insertion confirmation slot={slot}")
            return True
        log(f"insertion confirmation button post failed slot={slot}; falling back to text")
    except Exception as exc:
        log(f"insertion confirmation button post error slot={slot}: {exc!r}")
    return post(insertion_prompt(slot))


def post_spool_question(record, nag=False):
    """Post once or refresh a paused-spool question without stacking nag messages."""
    if _buttonless_standalone():
        if nag:  # no Discord message to refresh; never stack a second question
            log(f"spool question nag skipped on {_buttonless_standalone()}: no message refresh")
            return False
        return post(spool_question_text(record))
    try:
        from . import print_alert_buttons as pab
        previous = str(record.get("discord_message_id") or "") if nag else ""
        if nag and not previous:
            log("spool question nag skipped: no Discord message id to refresh")
            return False
        message_id = pab.post_spool_question(_bot_token(), announcer.DISCORD_CHAT_ID,
                                             spool_question_text(record, nag=nag, buttons=True),
                                             record, previous or None)
        if message_id:
            record["discord_message_id"] = message_id
            atomic_write_json(RUNOUT_PAUSE_STATE, record)
            log(f"spool question {'refreshed' if previous else 'posted'} message={message_id}")
            return True
        log("spool question component post failed")
    except Exception as exc:
        log(f"spool question component post error: {exc!r}")
    if nag:
        log("spool question nag left unchanged to avoid stacking messages")
        return False
    log("spool question component post failed; falling back to text")
    return post(spool_question_text(record))


def shutdown_alert_text(filename, state_message=None):
    from . import print_alert_buttons as pab
    if state_message is None:
        state_message = pab.shutdown_message()
    return ("**Printer firmware stopped.**\n" + print_summary.format_event("error", filename)
            + "\n**Why:** " + pab.explain(state_message)
            + "\nAn emergency stop can't be resumed. Only tap the button after the bed and nozzle are clear: "
              "it restarts the printer's controller once (no motion or heat), then ends the stopped job "
              "for good. Or start over with /print-again.")


def capture_stop(filename, trigger):
    """Read and store why the print stopped, before anything restarts the printer. Never raises."""
    try:
        from . import stop_capture
        return stop_capture.capture(filename, trigger, log=log).get("stop_reason") or ""
    except Exception as exc:
        log(f"stop capture failed ({trigger}): {exc!r}")
        return ""


def post_cancel_report(filename, reason=None, *, operator_cancel=False):
    """Full Klipper report on a cancel nobody asked for; title plus roast for the bot's own.

    ``operator_cancel`` is True only for a Moonraker "cancelled" transition (not a
    print that dropped to standby at the printer); with the bot's own cancel
    marker fresh, the operator asked for it and the short post is enough (#1373).
    """
    if reason is None:
        reason = capture_stop(filename, "cancel")
    mention = starter_pings.ping_for("cancelled", job_key=filename) or None
    try:
        from . import klipper_report
        by_bot = klipper_report.cancelled_by_bot()
    except Exception:
        by_bot = False
    if by_bot and operator_cancel:
        # The operator asked for this cancel and just confirmed it (#1373): the
        # title and the roast say it; temperatures, Z height and console
        # lines are for a stop nobody asked for.
        return post(_with_status_roast(f"Print cancelled: {_cancel_title(filename)}", filename,
                                       "cancelled", f"cancelled:{filename}"),
                    **({"mention_user_id": mention} if mention else {}))
    try:
        from . import klipper_report
        try:
            report_text = klipper_report.report(filename, reason)
        except TypeError:
            report_text = klipper_report.report(filename)
        text = print_summary.format_event("cancelled", filename) + "\n" + report_text
    except Exception as exc:
        log(f"klipper report failed: {exc!r}")
        text = print_summary.format_event("cancelled", filename)
    return post(_with_status_roast(text, filename, "cancelled", f"cancelled:{filename}"),
                **({"mention_user_id": mention} if mention else {}))


def _cancel_title(filename):
    """The job's own title for a cancel post, else the file's label. Never raises."""
    try:
        job, _plate = print_summary.find_job(filename)
        if (job or {}).get("title"):
            return job["title"]
    except Exception:
        pass
    return print_summary.safe_label(filename) if filename else "the print"


SCREEN_CANCEL_NATIVE = {"cancel", "cancelled"}
SCREEN_CANCEL_BUSY = {"printing", "paused", "pausing", "error", "busy"}


def native_screen_cancel(prev, now):
    """Stock firmware left a job (printing/paused/error) for cancel or ready."""
    return prev in SCREEN_CANCEL_BUSY and now in SCREEN_CANCEL_NATIVE | {"ready"}


def screen_cancel_reboot(filename, *, status_reader=None, rebooter=None, clearer=None, poster=None):
    """House rule: a cancel from the printer screen force restarts Klipper. Never while printing."""
    from print_handy import printer_control
    from . import print_alert_buttons as pab
    control = printer_control.for_printer()
    read = status_reader or print_control.status
    say = poster or post
    st = read()
    if st.get("state") == "printing":
        log("screen cancel reboot skipped: printer is printing again")
        return False, "skipped"
    label = print_summary.safe_label(filename) if filename else "the print"
    if st.get("klippy") == "shutdown":
        # The cancel report was just posted; one result line after the restart is enough.
        ok, msg = (clearer or control.clear_shutdown)()
    else:
        say(f"Screen cancel on {label}. Force restarting the printer now, about two minutes.")
        ok, msg = (rebooter or control.force_reboot)()
    log(f"screen cancel reboot ok={ok}: {msg}")
    if ok and clearer is None and rebooter is None and not _buttonless_standalone():  # real run only; tests inject fakes
        with contextlib.suppress(Exception):
            pab.retire_alerts(_bot_token(), filename, "Cleared after the screen cancel; nothing to do.")
    say(msg)
    return ok, msg


def post_shutdown_alert(filename):
    reason = capture_stop(filename, "shutdown")
    return post_alert(shutdown_alert_text(filename, reason or None), "shutdown", filename)


def _profile_env_value(name):
    """Read one profile env value. The listener unit loads no EnvironmentFile."""
    return _listener_common._profile_env_value(name)


def _bot_token():
    """The Discord bot token, from the variable [discord] token_env names (DISCORD_BOT_TOKEN by default)."""
    return _listener_common._bot_token(ns=_SELF)


def roast_enabled(cfg=None):
    """The roast switch: [vision] roast_enabled, on by default.

    Roasts come from the local corpus (core/roast.py, #1002) and need no
    endpoint, so the switch alone decides; it is the operator's way to turn
    them off without losing the camera watchdog.
    """
    vision = (cfg or _config.cfg()).get("vision") or {}
    return bool(vision.get("roast_enabled", True))


def roast_custom_bank(cfg=None):
    """The operator's custom roast bank path: [vision] roast_custom_bank.

    Empty (the default) means the shipped corpus alone. A path names a
    local JSON file of house roast lines (#1002 Phase 4); the engine
    validates and merges it per draw, and a file that cannot be used
    degrades to no custom roasts with the problem named in the log.
    """
    vision = (cfg or _config.cfg()).get("vision") or {}
    return str(vision.get("roast_custom_bank") or "").strip()


ROAST_STATE = _config.state_file("PRINT_HANDY_ROAST_STATE", "roast_state.json", per_instance=True)
_ROAST_FACTS = ("duration_s", "material", "color", "grams", "printer", "copies",
                "print_number", "times", "finish_hour", "last_title",
                "prev_fails", "fail_streak", "color_streak", "top_color")


def generate_roast(title, summary="", facts=None, key=None, status="completed"):
    """One short roast line for a print, from the local corpus. Best-effort.

    No LLM at all (#1002): no model call, no model fallback, no endpoint.
    ``facts`` are what is known about this print (duration, material,
    color, grams, printer, times printed, finish hour); a template that
    needs an unknown fact is never drawn. ``key`` makes the choice stick to
    one print, so a retried delivery repeats the same line instead of
    drawing a new one. ``summary`` is accepted for older callers and unused.
    """
    title = str(title or "").strip()
    if not title:
        return ""
    if not roast_enabled():
        log("roast skipped: vision.roast_enabled is false")
        return ""
    try:
        from ..core import roast as roast_engine
        known = {k: v for k, v in dict(facts or {}).items() if k in _ROAST_FACTS}
        ctx = roast_engine.build_context(title, **known)
        return roast_engine.roast(ctx, status=status, key=key, state_path=ROAST_STATE,
                                  custom_path=roast_custom_bank() or None)
    except Exception as exc:
        log(f"roast skipped: {exc!r}")
        return ""


def _with_status_roast(text, filename, status, key, facts=None):
    """A terminal post with its status-bank roast line appended, or unchanged.

    #1017 batch 2: the failure and cancel posts draw from the same local
    corpus as the completion post — the failed bank for an error, the
    cancelled bank for a cancel — under the same switch and the same
    best-effort contract (no line, post unchanged). The confirmed-pasta
    alert uses the same helper with the spaghetti bank. ``key`` is
    per-print, so a reposted notice repeats its line instead of drawing
    a second roast for the same print. No LLM anywhere in this path.
    """
    try:
        # The job's own facts (#1002 Phase 2): title, material, color and the
        # print-history slots (prev_fails, fail_streak, ...) that unlock the
        # reprint-failed and fail-streak pools. Caller facts (the measured
        # duration) win; a cancelled print's sliced grams were never used.
        title, known = print_summary.safe_label(filename), {}
        try:
            job, plate = print_summary.find_job(filename)
            title = (job or {}).get("title") or title
            known = dict(announcer.roast_facts(job, plate, {}) or {})
        except Exception as exc:
            log(f"{status} roast facts unavailable: {exc!r}")
        if status == "cancelled":
            known.pop("grams", None)
        known.update(facts or {})
        line = generate_roast(title, facts=known, key=key, status=status)
    except Exception as exc:
        log(f"{status} roast skipped: {exc!r}")
        return text
    return f"{text}\n\n{line}" if line else text


def _objects_line(filename, archive=None, context_path=None):
    """The "Objects still printing: …" line for a job's plate G-code, or "".

    One helper for both start announcements (P2-244): the fresh start
    post and the claimed start's warmup-done edit must name the same
    objects. Best-effort — any lookup failure is simply no line.
    """
    try:
        from ..core.exclude_object import objects_line_for_path
        _job, plate = print_summary.find_job(filename, archive=archive, context_path=context_path)
        return objects_line_for_path((plate or {}).get("gcode")) or ""
    except Exception:
        return ""


def start_message(filename, archive=None, context_path=None):
    """Format the canonical start message. The roast lives on the completion post."""
    message = print_summary.format_event("started", filename, archive=archive, context_path=context_path)
    line = _objects_line(filename, archive=archive, context_path=context_path)
    if line:
        message += "\n" + line
    return message


def post_exclude_objects(filename, archive=None, context_path=None):
    """Offer one-object cancel when the G-code names objects. Starts nothing.

    No exclude-object metadata posts nothing. Discord gets a menu that only
    asks. Telegram and Matrix get the typed `/exclude` line. A failed menu
    post falls back to that line.
    """
    try:
        from ..core.exclude_object import object_names
        _job, plate = print_summary.find_job(filename, archive=archive, context_path=context_path)
        path = (plate or {}).get("gcode") if isinstance(plate, dict) else None
        text = ""
        if path:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        names = object_names(text)
        if not names:
            return False
        from . import exclude_chat
        content = exclude_chat.surface_text(names)
        if not _buttonless_standalone():
            from . import print_alert_buttons as pab
            printer_id = ""
            try:
                from print_handy import listeners as _listeners
                printer_id = _listeners.instance_printer_id() or ""
            except Exception:
                printer_id = ""
            aid = pab.record("exclude-pick", filename or "", names=names, printer_id=printer_id)
            if exclude_chat.post_picker(_bot_token(), announcer.DISCORD_CHAT_ID, content, aid, names):
                log(f"post exclude menu file={filename!r} objects={len(names)}")
                return True
            log(f"exclude menu post failed file={filename!r}; falling back to text")
        return post(content)
    except Exception as exc:
        log(f"exclude object post error file={filename!r}: {exc!r}")
        return False


def _live_auto_on_print_start():
    """Auto-start the live view on a print start (#311 stage 4), fail-soft.

    When [live_stream] auto_start_on_print is on and the feature is
    configured, the shared session record (print_handy.live_stream) is
    started and the Activity pointer is posted as its own line, like
    the no-mesh alert. This listener is the process that sees every
    Moonraker print start — claimed by the intake or started at the
    printer — so it is where the trigger lives; the bot and the
    Activity server read the same record. Anything wrong here (live
    view off, unconfigured, a torn record) is a silent no-op: a viewer
    feature never disturbs a print announcement (#235 posture).
    """
    try:
        from print_handy import live_stream

        cfg = _config.cfg()
        section = cfg.get("live_stream") or {}
        path = live_stream.session_file_path(cfg)
        posted = []

        def update(session):
            text = live_stream.auto_start(session, section, now=time.time())
            if text is None:
                return False
            posted.append(text)
            return True

        # One serialized transaction (P2-143): the record is shared
        # with the bot's /print-live and the Activity server's
        # presence writes, so the read-modify-write must not
        # interleave with theirs. The pointer posts after the lock is
        # released — posting never holds it.
        live_stream.transact_session_file(path, update)
        if posted:
            post(posted[0])
    except Exception:
        pass


def _live_auto_on_print_end():
    """End an auto-started live session when its print ends (#311 stage 4).

    Only a session the auto-start trigger itself opened is ended here;
    a session a user started with /print-live is theirs to stop (the
    empty timeout still applies to it). Fail-soft, like the start hook.
    """
    try:
        from print_handy import live_stream

        cfg = _config.cfg()
        path = live_stream.session_file_path(cfg)
        if not path.exists():
            return

        def update(session):
            if session.is_live and session.started_by == live_stream.AUTO_STARTED_BY:
                session.stop(now=time.time())
                return True
            return False

        # One serialized transaction (P2-143), like the start hook:
        # this stop must land after any in-flight presence update, not
        # be overwritten by its stale live snapshot.
        live_stream.transact_session_file(path, update)
    except Exception:
        pass


def paused_message(filename, runout=False, slot=None, spool_id=None):
    """One alert when a print pauses. Never pauses, resumes, or cancels anything itself."""
    where = ""
    try:
        from . import print_stop
        where = print_stop.where_text(print_stop.status())
    except Exception as exc:
        log(f"paused status read failed: {exc!r}")
    if runout:
        which = filament_color.slot_display(slot) if slot in (1, 2, 3, 4) else "The active slot"
        into = filament_color.slot_display(slot) if slot in (1, 2, 3, 4) else "the active slot"
        spool = f" (spool #{spool_id})" if spool_id else ""
        # The spool question is only opened when a slot is known (open_question), so
        # promise it only then; otherwise this alert and the follow-up notice contradict.
        asked = slot in (1, 2, 3, 4)
        # Name the answers the question's buttons offer (spool_question_components):
        # with a known spool, a runout also offers the new-roll and fix-feed choices.
        choices = ("New roll, same filament; Same spool, fix feed or jam; Existing spool; or New spool"
                   if spool_id else "Existing spool or New spool")
        extra = (f"Filament runout: {which}{spool} ran out of filament.\n"
                 "To finish this print:\n"
                 f"1. Load the replacement filament into {into} at the printer.\n"
                 + (f"2. Answer the spool question below ({choices}) so the replacement "
                    "spool is the one registered for that slot — filament usage stays paused for every "
                    "spool until you answer.\n3. " if asked else "2. ")
                 + "Tap Resume print below (or type /print-resume) once the filament is loaded. "
                 "The print resumes only when you press it; nothing resumes on its own.")
    else:
        extra = (f"Paused at {where}. " if where else "") + "Fix it at the printer, then tap Resume or Cancel below (or type /print-resume or /print-stop)."
    return print_summary.format_event("paused", filename, extra=extra)


# A runout pause that comes back within this long of its resume, on the same
# slot, is the same runout: the new filament has not reached the toolhead
# yet. It is not a new episode (seen live: eight pause/resume rounds in 26
# minutes, each a full three-step post, and a spool question re-opened for a
# spool created four minutes earlier).
RUNOUT_REPAUSE_WINDOW_S = 600


def runout_repause_message(filename, slot=None, count=1):
    """The one rewritten alert for a print that paused again right after a runout resume."""
    where = filament_color.slot_display(slot) if slot in (1, 2, 3, 4) else "the slot"
    times = "once more" if count == 1 else f"{count} times"
    extra = (f"Paused again: the filament has not reached the toolhead yet. The print paused "
             f"{times} within minutes of resuming after the runout on {where}. Feed the filament "
             "at the printer until it comes out of the nozzle, then tap Resume print below (or "
             "type /print-resume). The spool question from the runout is unchanged: nothing new "
             "to answer.")
    return print_summary.format_event("paused", filename, extra=extra, emit=False)


def recovery_slot(filename):
    """The slot and spool this job was using, from the hardware map. No printer call."""
    try:
        state = filament_color.adopt_renamed_slots_key(json.loads(_hardware_file().read_text()))
    except (OSError, ValueError):
        return None, None
    slots = state.get("filament_slots") or {}
    active = state.get("active_slot")
    slot = active if active in (1, 2, 3, 4) else None
    record = slots.get(str(slot)) if slot else None
    return slot, (record or {}).get("spoolman_id")


def launch_mapped_slots(filename):
    """The slots this job was launched on, from its `.launch.json` checkpoint
    command (T<tool>=<slot> pairs). No printer call; any failure reads as no
    record, and the caller falls back to its other evidence."""
    try:
        from ..core import model_job
        artifact = Path(model_job.resolve_print_target(filename))
        data = json.loads(artifact.with_suffix(".launch.json").read_text())
        command = str((data or {}).get("command") or "")
    except (Exception, SystemExit):  # resolve_print_target refuses with SystemExit
        return set()
    return {int(slot) for slot in re.findall(r"T\d+=(\d+)", command)}


def attribute_runout_slot(newly_emptied, empty, mapped, fallback):
    """Name the slot that ran out when presence alone is ambiguous (#838).

    The tracker already separates slots that became empty after this print
    started (`emptied`, confirmed loaded→empty transitions since the print
    began) from slots that merely read empty — a slot empty at print start
    is in neither set. What classify_runout lacked was using that: it took
    the presence list wholesale and, with two or more empty, fell back to
    the job's recorded active slot — during a CHANGE_ZCOLOR, the tool that
    was just unloaded. So: attribute by newly emptied slots intersected
    with the job's mapped slots from the launch record; when that yields
    nothing, presence (likewise narrowed by the mapping) decides only when
    exactly one candidate remains; otherwise the recorded fallback stands.
    """
    newly = sorted(set(newly_emptied) & set(mapped)) if mapped else sorted(set(newly_emptied))
    if len(newly) == 1:
        return newly[0]
    if not newly:
        present = sorted(set(empty) & set(mapped)) if mapped else sorted(set(empty))
        if len(present) == 1:
            return present[0]
    if fallback is None and len(mapped or ()) == 1:
        # No tracker history (a restart mid-print forgets which slots were
        # loaded) and no saved active slot: a job launched on exactly one
        # slot can only have run out of that slot.
        return next(iter(mapped))
    return fallback


def list_spools():
    """Read Spoolman inventory. A failure returns nothing rather than inventing a spool."""
    base = _config.service_url("spoolman_url")
    if not base:
        log("spool inventory read skipped: services.spoolman_url is not configured")
        return []
    payload = spoolman_client.list_spools(base_url=base, timeout=8)
    if payload is None:
        log("spool inventory read failed: Spoolman did not answer")
        return []
    return payload


def spool_matches_recovery(existing_material, candidate_material, existing_color, candidate_color,
                           existing_hex=None, candidate_hex=None):
    """PLA and PLA+ can match; the color must match by name ("Black PLA" == "Black") or by eye
    (hexes within SAME_DELTA_E: 000000, 161616 and 1A1A1A are one black). Finishes never cross."""
    def normalized(value):
        return " ".join(str(value or "").casefold().split())

    if spool_canon.swap_group(existing_material) is None or spool_canon.swap_group(candidate_material) is None:
        # A material off the menu only matches its exact self.
        if normalized(existing_material) != normalized(candidate_material):
            return False
        return bool(normalized(existing_color)) and normalized(existing_color) == normalized(candidate_color)
    if not normalized(existing_color):
        return False
    return spool_canon.changeover_compatible(existing_material, existing_color, existing_hex,
                                             candidate_material, candidate_color, candidate_hex)






classify_pause = _listener_common.classify_pause
pause_is_jam = _listener_common.pause_is_jam


def _span_hms(seconds):
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def jam_message(filename, facts):
    """The IFS jam pause post (#1020 E1/E2): measured facts, safe actions only.

    ``facts`` carries duration_s, layer, slot, next_cmd_delay,
    detection_length and head_sensor (True/False/None). Unknown values
    are stated as not reported rather than invented. The actions are
    inspect, then resume or cancel — the listener never resumes a jam
    on its own, and this post carries the ordinary paused alert's
    Resume/Cancel buttons.
    """
    slot = facts.get("slot")
    slot_text = (filament_color.slot_display(slot) if slot in (1, 2, 3, 4)
                 else "the active slot (no slot could be identified)")
    duration = _span_hms(facts.get("duration_s"))
    layer = facts.get("layer")
    delay = facts.get("next_cmd_delay")
    detection = facts.get("detection_length")
    head_sensor = facts.get("head_sensor")
    lines = [
        f"**Filament jam detected (IFS)** on {slot_text} while printing "
        f"{clean(filename or 'this print')}. The printer paused itself; nothing has resumed it.",
        "",
        "What the printer reports:",
        f"- Print duration at pause: {duration or 'not reported'}",
        f"- Layer: {layer if isinstance(layer, int) else 'not reported'}",
        f"- Slot: {slot_text}",
        f"- zmod_ifs next_cmd_delay: {delay if delay is not None else 'not reported'}",
        f"- zmod_ifs detection_length: {detection if detection is not None else 'not reported'}",
        f"- Head switch sensor: {'present' if head_sensor is True else 'not present' if head_sensor is False else 'not reported'}",
        "",
        "What to do:",
        "1. Inspect the filament path at the printer — check for a real drag or tangle "
        "on that slot before assuming a false trigger (see the jam procedure in "
        "docs/DIAGNOSTICS.md).",
        "2. Once the path is clear, tap Resume print below (or type /print-resume); "
        "to give up on this print, tap Cancel (or type /print-stop).",
    ]
    return "\n".join(lines)


def persist_runout_pause(filename, slot, prior_spool_id, path=None):
    """Remember the depleted slot until the operator answers. No printer action."""
    record = {
        "filename": filename or "",
        "slot": slot if slot in (1, 2, 3, 4) else None,
        "prior_spool_id": prior_spool_id,
        "cause": "runout",
        "recorded_at": time.time(),
    }
    atomic_write_json(Path(path or RUNOUT_PAUSE_STATE), record)
    return record


def load_runout_pause(path=None, max_age_s=6 * 60 * 60):
    record = read_json_object(Path(path or RUNOUT_PAUSE_STATE))
    recorded = record.get("recorded_at")
    if record.get("cause") not in ("runout", "swap") or not isinstance(recorded, (int, float)):
        return None
    # Suspended usage never expires: nobody is being charged until a human answers.
    if not record.get("usage_suspended") and time.time() - recorded > max_age_s:
        return None
    return record


def pause_is_runout(message, slot, slot_presence, head_has_filament):
    """Runout = the alert says so, the paused job's slot reads empty, or the head sensor is empty."""
    if classify_pause(message) == "runout":
        return True
    if slot in (1, 2, 3, 4) and isinstance(slot_presence, dict) and slot_presence.get(slot) is False:
        return True
    return head_has_filament is False


FILAMENT_SENSOR_PREFIXES = ("filament_switch_sensor ", "filament_motion_sensor ")


def filament_sensor_names(objects):
    """Filament sensor object names from a Moonraker objects/list payload.

    Names come from the printer's own object list, never a hardcoded sensor
    name: any Klipper filament switch or motion sensor qualifies.
    """
    if not isinstance(objects, (list, tuple)):
        return []
    return [name for name in objects
            if isinstance(name, str) and name.startswith(FILAMENT_SENSOR_PREFIXES)]


def _sensor_suffix_tokens(name):
    """The name tokens after a filament sensor's object prefix.

    "filament_switch_sensor extruder1" -> {"extruder1"}; the tokens are
    how a sensor is associated with an extruder — a whole-token match,
    so "extruder1" never associates with the "extruder" tool.
    """
    suffix = name
    for prefix in FILAMENT_SENSOR_PREFIXES:
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            break
    return {token for token in re.split(r"[^0-9A-Za-z]+", suffix) if token}


def head_sensor_from_status(status, single_extruder=False):
    """True/False when an enabled filament sensor at the head answers, else None.

    Fail-safe (#442): a sensor that is absent, disabled, or reporting a
    non-boolean value yields no evidence — an unreadable sensor must not
    turn every pause into a runout.

    The evidence is restricted to the ACTIVE filament path (P2-171): on
    a multi-extruder printer the idle tool's sensor reads empty
    normally, so when the toolhead names the active extruder and some
    sensors associate with it (the extruder name is a whole token of
    the sensor's suffix), only those sensors decide — among them, any
    empty reading is decisive, as before. When no association can be
    determined, agreeing sensors decide together, and conflicting
    readings are unknown (None), never a runout on a guess.

    ``single_extruder`` (the printer lists no extruder1, extruder2, ...):
    every sensor is on the one filament path, so any empty one — head or
    entry — is runout evidence even when the sensor names carry no
    "extruder" token (2.2.5 review).
    """
    if not isinstance(status, dict):
        return None
    readings = []
    for name, sensor in status.items():
        if not (isinstance(name, str) and name.startswith(FILAMENT_SENSOR_PREFIXES)):
            continue
        if not isinstance(sensor, dict) or sensor.get("enabled") is not True:
            continue
        detected = sensor.get("filament_detected")
        if isinstance(detected, bool):
            readings.append((name, detected))
    if not readings:
        return None
    if single_extruder:
        return False if any(detected is False for _name, detected in readings) else True
    toolhead = status.get("toolhead")
    active = toolhead.get("extruder") if isinstance(toolhead, dict) else None
    if isinstance(active, str) and active:
        associated = [detected for name, detected in readings
                      if active in _sensor_suffix_tokens(name)]
        if associated:
            return False if any(detected is False for detected in associated) else True
    values = {detected for _name, detected in readings}
    if len(values) == 1:
        return values.pop()
    return None


def read_head_sensor():
    """Discover the printer's filament sensors and read them via Moonraker. None when unreadable.

    Read-only: two GETs (objects/list, then an objects/query for exactly the
    sensor objects the printer lists). Any failure returns None so the pause
    classification falls back to the message and slot evidence alone.
    """
    try:
        listing = print_control._call("/printer/objects/list")
        objects = ((listing.get("result") or {}).get("objects")
                   if isinstance(listing, dict) else None)
        names = filament_sensor_names(objects)
        if not names:
            return None
        # The toolhead rides along: which extruder is active decides
        # whose sensor counts as runout evidence (P2-171).
        query = "&".join(urllib.parse.quote(name, safe="") for name in [*names, "toolhead"])
        data = print_control._call(f"/printer/objects/query?{query}")
        status = ((data.get("result") or {}).get("status")
                  if isinstance(data, dict) else None)
        single = not any(isinstance(o, str) and re.fullmatch(r"extruder\d+", o) for o in objects)
        return head_sensor_from_status(status, single_extruder=single)
    except Exception as exc:
        log(f"head filament sensor read failed: {exc!r}")
        return None


def open_spool_question(filename, slot, prior_spool_id, cause, suspended_spool, path=None):
    """Persist an unanswered spool question. Usage is already suspended by the caller."""
    record = {
        "filename": filename or "",
        "slot": slot if slot in (1, 2, 3, 4) else None,
        "prior_spool_id": prior_spool_id,
        "cause": cause,
        "usage_suspended": True,
        "suspended_spool_id": suspended_spool,
        "recorded_at": time.time(),
        "last_nag_at": time.time(),
        "nags": 0,
    }
    atomic_write_json(Path(path or RUNOUT_PAUSE_STATE), record)
    return record


def spool_question_text(record, nag=False, *, buttons=False):
    """The paused spool question's text.

    Two shapes (#1063). With ``buttons`` (the Discord component post) the
    text only names the buttons: no slash-command instructions and no
    typed format recipe in the routine guided flow — the shelf is the
    Existing spool picker and a new spool is the New spool walkthrough.
    Without buttons (buttonless platforms, and the plain-text fallback
    when a component post fails) typed replies are the only route, so
    the text names the reply words instead; it still never points at a
    slash command.
    """
    slot = record.get("slot")
    where = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the active IFS slot"
    prior = record.get("prior_spool_id")
    runout = record.get("cause") == "runout"
    why = "ran out" if runout else "was unloaded and reloaded mid-print"
    head = (f"**Still waiting (reminder {record.get('nags', 0)})**: which spool is in {where}?" if nag
            else f"**Spool check:** the filament in {where} {why}.")
    paused = ("\nFilament usage is **paused for every spool** until you answer, "
              "so neither the old nor the new spool is charged.")
    if buttons:
        if runout and prior:
            options = (
                f"\n• **New roll, same filament** — a fresh roll of the same filament: a new "
                f"spool is created and #{prior} is marked depleted\n"
                f"• **Same spool — fix feed or jam** — the same physical spool #{prior} stays "
                f"in the slot and you clear its stuck feed or jam at the printer: nothing new "
                f"is created, #{prior} is not marked depleted, and its remaining weight is "
                f"unchanged\n"
                "• **Existing spool** — pick a different spool from the shelf\n"
                "• **New spool** — add a different new spool\n"
                "• **Cancel print** — end the print and close this question")
        elif record.get("cause") == "swap" and prior:
            options = (
                f"\n• **Same spool** — spool #{prior} went back in\n"
                "• **Existing spool** — pick a different spool from the shelf\n"
                "• **New spool** — add a different new spool\n"
                "• **Cancel print** — end the print and close this question")
        else:
            options = (
                "\n• **Existing spool** — pick a spool from the shelf\n"
                "• **New spool** — add a new spool\n"
                "• **Cancel print** — end the print and close this question")
        return (f"{head}{paused}\nTap one:{options}\n"
                "Answering only settles the spool records: it never resumes the print and "
                "does not mean the feed path is clear. A runout signal alone does not prove "
                "the roll is empty — if the filament is stuck rather than gone, use the "
                "same-spool fix above. Print Handy cannot load or unload filament while the "
                "print is paused, so a stuck feed is fixed at the printer; cancelling is "
                "the one choice that acts on the printer.")
    same = ("\n• `same` if you put spool #%s back" % prior) if record.get("cause") == "swap" and prior else ""
    refeed = (f"\n• `refeed` or `fix feed` if the same physical spool #{prior} stays in the "
              f"slot and you clear its stuck feed or jam at the printer — no new spool is "
              f"created, #{prior} is not marked depleted, and its remaining weight is unchanged"
              f"\n• `same filament` if you loaded a fresh roll of the same filament — "
              f"a new spool is created and #{prior} is marked depleted") if runout and prior else ""
    return (f"{head}{paused}\nReply with:\n"
            f"• a spool number from Spoolman, e.g. `12`{same}{refeed}\n"
            f"• or a new spool: `brand, material, color, grams` e.g. `Sunlu, PLA+, White, 1000`\n"
            f"• or `cancel the print` (or the **Cancel print** button) to end the print and close this question\n"
            "Answering never touches the printer and never resumes the print; "
            "cancelling is the one reply that does.")


def clear_runout_pause(path=None):
    target = Path(path or RUNOUT_PAUSE_STATE)
    if target.exists():
        target.unlink()


# Once the print a spool question was opened for is over, the question
# still waits for an answer, but a reminder every SPOOL_NAG_SECONDS for days
# is noise (one open question was reminded 619 times over four days).
# Reminders slow to this interval instead; the count keeps going up.
SPOOL_NAG_ENDED_SECONDS = 6 * 60 * 60


def spool_question_print_is_current(record, state, filename, total_duration=None, now=None):
    """Whether the print an open spool question was asked for is still the printer's job.

    True only while the printer is printing or paused on the question's
    file and, when the printer reports how long the job has run, that job
    started before the question opened: a later reprint of the same file is
    a different print. An unknown run time on the same file counts as the
    same print, the conservative reading.
    """
    if state not in ("printing", "paused") or not record:
        return False
    if not filename or not launch_state.same_remote(filename, record.get("filename") or ""):
        return False
    recorded = record.get("recorded_at")
    if isinstance(total_duration, (int, float)) and isinstance(recorded, (int, float)):
        started = (time.time() if now is None else now) - float(total_duration)
        return started <= float(recorded) + 60
    return True


def refeed_settles_question(record, bound_spool, present):
    """The #1063 refeed rule, applied once the question's print is over.

    An open runout question whose slot is still bound to the very spool
    that ran out, with that slot reading loaded, is the refeed answer: the
    same spool is back in the slot. A different spool, an unknown binding,
    or a slot that reads empty or could not be read never settles it.
    """
    if not record or record.get("cause") != "runout" or not record.get("usage_suspended"):
        return False
    prior = record.get("prior_spool_id")
    if record.get("slot") not in (1, 2, 3, 4) or not isinstance(prior, int) or isinstance(prior, bool):
        return False
    return bound_spool == prior and present is True


def refeed_settled_text(record):
    """The one line posted when the refeed rule closes a question on its own."""
    slot, prior = record.get("slot"), record.get("prior_spool_id")
    return (f"**Spool check closed:** spool #{prior}, the spool that ran out, is back in "
            f"IFS slot {slot}, so the open question is answered as a refeed (same spool, feed "
            f"fix): no new spool was created, #{prior} was not marked depleted, and filament "
            f"usage is no longer paused.")


def open_question_start_notice(record):
    """The one notice a new print gets while an older spool question still pauses usage."""
    slot = record.get("slot")
    where = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "an IFS slot"
    about = clean(record.get("filename") or "an earlier print")
    return (f"**Spool question still open:** the question about {where} from {about} has not "
            "been answered, so filament usage is paused until it is answered and this print "
            "may not be charged to any spool. The print was not held; answer the question "
            "to settle the spool records.")


def compatible_offers(spools, material, color, color_hex=None):
    """Offer same color/finish PLA or PLA+ spools. Never pick one."""
    offers = []
    for item in spools or []:
        filament = item.get("filament") or {}
        if item.get("archived") is True:
            continue
        if spool_matches_recovery(material, filament.get("material") or item.get("material"),
                                  color, filament.get("name") or item.get("color") or item.get("name"),
                                  color_hex, filament.get("color_hex")):
            offers.append({"id": item.get("id"), "remaining_weight": item.get("remaining_weight"),
                           "name": filament.get("name") or item.get("color") or item.get("name"),
                           "material": filament.get("material") or item.get("material")})
    return offers


EXISTING_CHOICE = "existing spool"
NEW_CHOICE = "new spool"


def _grams(value):
    text = str(value or "").strip()
    # Discord replies commonly end in punctuation; accept a grams suffix but not stray prose.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:g)?\s*[.!?]*", text, re.I)
    if not match:
        return None
    try:
        grams = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if grams <= 0:
        return None
    return round(grams, 2)


CANCEL_PRINT_REPLIES = frozenset({
    "cancel", "cancel print", "cancel the print", "cancel it",
    "stop", "stop print", "stop the print",
})


def parse_cancel_print_reply(text):
    """True only for an unambiguous typed instruction to cancel the paused print.

    Consulted while a spool question is pending, before the spool-answer
    parser rejects the reply. Anything softer — a question about cancelling,
    a sentence that merely mentions cancel, extra words — is not an
    instruction and keeps falling through to that rejection, exactly as
    before. The reply gate is the same one every answer passes through
    (an authorized operator in the print channel), so a typed cancel carries
    the same authority as the Cancel print button.
    """
    folded = " ".join(str(text or "").split()).casefold().strip(" .!")
    return folded in CANCEL_PRINT_REPLIES


def parse_runout_reply(text):
    """Turn one Discord reply into an existing-spool or new-spool choice. No writes."""
    raw = " ".join(str(text or "").split())
    folded = raw.casefold()
    if not raw:
        return {"status": "need_choice"}
    if folded in {"refeed", "refed", "retry feed", "retry the feed",
                  "fix feed", "fix the feed", "fix jam", "fix feed or jam", "jam", "jammed"}:
        return {"status": "refeed"}
    if folded in {"same", "same spool", "same filament"}:
        return {"status": "same"}
    if folded in {"a", "existing", EXISTING_CHOICE}:
        return {"status": "need_spool_number"}
    if folded in {"b", "new", NEW_CHOICE}:
        return {"status": "need_new_fields"}
    spool_number = None
    if folded.startswith("#") and folded[1:].isdigit():
        spool_number = int(folded[1:])
    elif folded.isdigit():
        spool_number = int(folded)
    if spool_number:
        return {"status": "existing", "spool_id": spool_number}
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) < 3:
        return {"status": "unparsed", "text": raw}
    brand = parts[0] or None
    material, color = parts[1], parts[2]
    grams = _grams(parts[3]) if len(parts) > 3 else None
    lot = parts[4] if len(parts) > 4 else None
    if not material or not color or grams is None:
        return {"status": "unparsed", "text": raw}
    return {"status": "new", "brand": brand, "material": material, "color": color,
            "grams": grams, "lot": lot}


def apply_runout_choice(state, context, choice, *, client, filaments=None):
    """Bind the paused slot. Inventory writes only. Never calls the printer.

    client(method, path, body) stands in for Spoolman. filaments is a list of
    existing filament records used to avoid a duplicate type.
    """
    slot = context.get("slot")
    if slot not in (1, 2, 3, 4):
        raise ValueError("runout pause has no IFS slot")
    prior_id = context.get("prior_spool_id")
    if choice.get("status") == "refeed":
        # The same physical spool is back in the slot (#1063): it is
        # already bound there, so there is nothing to write — no new
        # record, no bind, and the spool is NOT marked depleted. The
        # caller resumes usage on it and closes the question.
        if prior_id is None:
            raise ValueError("refeed needs the spool that was in the slot")
        return {"action": "refeed", "slot": slot, "spool_id": int(prior_id),
                "prior_spool_id": prior_id, "printer_commands": []}
    if choice.get("status") == "existing":
        spool_id = int(choice["spool_id"])
        if prior_id is not None and spool_id == int(prior_id):
            raise ValueError(f"spool #{spool_id} is the depleted spool; pick another or say new")
        record = client("GET", f"/api/v1/spool/{spool_id}", None)
        if record.get("archived") is True:
            raise ValueError(f"spool #{spool_id} is archived")
        filament = record.get("filament") or {}
        client("PATCH", f"/api/v1/spool/{spool_id}", {"location": f"IFS Slot {slot}"})
        if prior_id and int(prior_id) != spool_id:
            client("PATCH", f"/api/v1/spool/{int(prior_id)}",
                   {"archived": True, "remaining_weight": 0, "location": "depleted"})
        return {"action": "existing", "slot": slot, "spool_id": spool_id,
                "material": filament.get("material"), "color": filament.get("name"),
                "prior_spool_id": prior_id, "printer_commands": []}
    if choice.get("status") != "new":
        raise ValueError("choice is not an existing or new spool")
    grams = _grams(choice.get("grams"))
    material = str(choice.get("material") or "").strip()
    color = str(choice.get("color") or "").strip()
    if grams is None or not material or not color:
        raise ValueError("new spool needs material, color/finish, and full starting grams")
    try:
        canon = spool_canon.canonical_spec({"material": material, "color": color, "brand": choice.get("brand")})
    except spool_canon.SpecError as exc:
        raise ValueError(str(exc)) from None
    material, color, brand = canon["material"], canon["color"], canon["brand"]
    match = spool_canon.find_filament(filaments or [], canon)
    if match is None:
        match = client("POST", "/api/v1/filament", {
            "name": color, "material": material, "vendor_name": brand,
            "diameter": 1.75, "density": 1.24, "weight": grams,
        })
    created = client("POST", "/api/v1/spool", {
        "filament_id": match["id"], "initial_weight": grams, "remaining_weight": grams,
        "used_weight": 0, "location": f"IFS Slot {slot}", "lot_nr": choice.get("lot"),
        "comment": RUNOUT_SPOOL_COMMENT,
    })
    if prior_id:
        client("PATCH", f"/api/v1/spool/{int(prior_id)}",
               {"archived": True, "remaining_weight": 0, "location": "depleted"})
    return {"action": "new", "slot": slot, "spool_id": created.get("id"),
            "filament_id": match.get("id"), "material": material, "color": color,
            "starting_grams": grams, "prior_spool_id": prior_id, "printer_commands": []}


def manual_resume_tracking_message(filename, slot=None, compatible_spools=None):
    """Prompt inventory reconciliation after a manual resume; never control the printer."""
    slot_text = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the affected IFS slot"
    choices = []
    for item in compatible_spools or []:
        if isinstance(item, dict) and item.get("id") is not None:
            # Offered spools are named in the one picker shape (#792 part 2).
            choices.append(print_summary.spool_picker_label(
                item.get("name"), item.get("material"), item["id"], item.get("remaining_weight")))
    offer = (" Compatible same-color PLA/PLA+ spool found: " + ", ".join(choices) + ". You may choose its number; I will not select it automatically." if choices else "")
    return (
        f"Filament runout recovery for {print_summary.safe_label(filename)}, {slot_text}. "
        "Choose how to continue tracking: A) existing Spoolman spool, or B) new spool."
        + offer +
        " If existing, reply with its spool number or confirm the offered number. I will update assignment/tracking only. "
        "If new, load it and resume manually at the printer first. Once the live printer state shows the job resumed, "
        "send a clear label photo (preferred), or give optional brand, material, color/finish, and full starting weight in grams. "
        "I will then create a distinct spool at that full starting weight, mark the replaced exhausted spool depleted when confirmed, "
        "map this IFS slot to the new spool, and reconcile tracking sources. Similar-color records remain untouched. "
        "Inventory only: I will never start, resume, pause, heat, or move the printer."
    )


def _completion_roast_line(message, completion_message):
    """The one roast line for a pending completion, or "" when there is none.

    Best-effort: no recorded title, a disabled switch or a generation
    failure returns "" and the post goes out exactly as it was. The line
    is keyed to the print, so asking again for the same delivery returns
    the same line without drawing a second roast.
    """
    try:
        state = announcer.load_state()
        title, summary = announcer.pending_roast_subject(state, completion_message)
        if not title or not str(message).startswith(completion_message):
            return ""
        facts, key = announcer.pending_roast_facts(state, completion_message)
        return generate_roast(title, summary, facts=facts, key=key) or ""
    except Exception as exc:
        log(f"completion roast skipped: {exc!r}")
        return ""


def _with_completion_roast(message, completion_message):
    """The completion post with its one roast, placed under the completion text.

    Used when the post carries no attachment: the roast goes before any
    suffix lines (a timelapse-not-attached note stays last), which keeps
    MEDIA:-free posts a single message. A post that does carry a timelapse
    is handled by _post_completion instead — there the roast must land
    under the video, and no in-message order can do that.
    """
    roast = _completion_roast_line(message, completion_message)
    if not roast:
        return message
    return f"{completion_message}\n\n{roast}{message[len(completion_message):]}"


def _acknowledge_completion(completion_message):
    """Clear a delivered completion's pending notice and its roast subject."""
    state = announcer.load_state()
    if announcer.pending_completion_message(state) == completion_message:
        announcer.clear_pending_roast_subject(state)
        announcer.clear_pending_completion_message(state, completion_message)
        announcer.clear_pending_timelapse_job_key(state)
        announcer.clear_pending_timelapse_routing(state)
        state["announced_completion"] = True
        announcer.save_state(state)


def _post_completion(message, completion_message, job_key=None):
    """Acknowledge only a matching pending notice after transport success.

    A post carrying a timelapse (a MEDIA: attachment) delivers its roast
    as a separate follow-up post, so the roast lands UNDER the video in
    the chat: on Discord a message's text always renders above its
    attachments, and the Telegram and Matrix senders likewise post text
    before files, so a roast inside the timelapse's own message — wherever
    it sits in the string — always appears above the video. The follow-up
    is best-effort and never un-delivers the completion: the notice is
    acknowledged on the timelapse post's success alone, and a refused
    timelapse post posts no roast at all, leaving it pending for the
    caller's text-only fallback post to carry inline.
    """
    # The completion post pings the starter (#1029, kept by #1095): the
    # classifier decides from the kind and the job — the caller's job key
    # when it has one, else the announcer state's current job. The roast
    # follow-up post below deliberately gets no ping decision: a roast
    # never pings.
    if not job_key:
        try:
            job_key = announcer.load_state().get("job_key")
        except Exception:
            job_key = None
    mention = starter_pings.ping_for("complete", job_key=job_key) or None
    mention_kwargs = {"mention_user_id": mention} if mention else {}
    if re.search(r"MEDIA:\S+", str(message)):
        roast = _completion_roast_line(message, completion_message)
        delivered = post(message, **mention_kwargs)
        if not delivered:
            return delivered
        _acknowledge_completion(completion_message)
        if roast:
            try:
                if not post(roast):
                    log("completion roast follow-up post failed; the timelapse post stands")
            except Exception as exc:
                log(f"completion roast follow-up post failed: {exc!r}; the timelapse post stands")
        return delivered
    delivered = post(_with_completion_roast(message, completion_message), **mention_kwargs)
    if delivered:
        _acknowledge_completion(completion_message)
    return delivered


def _persist_timelapse_routing(plan_id, tl_plan):
    """Record a pending completion's multipart routing beside its job key.

    The pending job key alone cannot replay a multipart completion after
    a restart: the retry needs the plan marker and the plate picker the
    original pass parsed from the announcer's one-shot markers (P2-146).
    """
    try:
        state = announcer.load_state()
        announcer.set_pending_timelapse_routing(state, plan_id, tl_plan)
        announcer.save_state(state)
    except Exception as exc:
        log(f"could not persist timelapse routing: {exc!r}")


def _run_announcer_main(reason):
    """Run one announcer pass and return its stdout ("" when the pass failed)."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            announcer.main()
    except Exception as exc:  # never let one bad pass kill the listener
        log(f"announcer error ({reason}): {exc!r}")
        return ""
    return out.getvalue()


def _announcer_markers(output):
    """One announcer pass's output, split into (marker, plan_id, tl_plan, message)."""
    lines = output.splitlines()
    marker = next((line for line in lines if line.startswith("TIMELAPSE_POSTPROCESS:")), None)
    picker = next((line for line in lines if line.startswith(announcer.PLATE_PICKER_MARKER)), None)
    plan_id = picker.split(":", 1)[1].strip() if picker else None
    tl_line = next((line for line in lines if line.startswith(plan_timelapse.PLAN_MARKER)), None)
    message = "\n".join(line for line in lines
                        if not line.startswith(("TIMELAPSE_POSTPROCESS:", announcer.PLATE_PICKER_MARKER,
                                                plan_timelapse.PLAN_MARKER))).strip()
    tl_plan = None
    if tl_line:
        try:
            tl_id, tl_stage, tl_order = tl_line[len(plan_timelapse.PLAN_MARKER):].strip().split(":")
            tl_plan = (tl_id, tl_stage, int(tl_order))
        except ValueError:
            log(f"ignoring malformed timelapse plan marker {tl_line!r}")
    return marker, plan_id, tl_plan, message


def _process_announcer_markers(output):
    """Act on the one-shot markers of a pass whose text is handled elsewhere (P2-38).

    A pending-completion pass runs the announcer to advance its state; the
    pending delivery owns this pass's text, so the announcer's message is
    NOT posted here. Its markers are one-shot, though: when the same file
    was printed again while the old completion sat undelivered, the pass
    consumes the new print's TIMELAPSE_POSTPROCESS / plan / picker
    markers, and discarding them would skip that print's timelapse and
    picker permanently. Process them against the text the announcer
    printed for them — which the pass also stored as the pending
    completion, so _post_completion acknowledges exactly that delivery.
    """
    marker, plan_id, tl_plan, message = _announcer_markers(output)
    if not marker and not tl_plan and not plan_id:
        return
    if not message:
        message = announcer.pending_completion_message(announcer.load_state()) or ""
    if tl_plan and tl_plan[1] == "partial":
        # Plan stopped midway: post what exists so far, labeled partial. No picker.
        threading.Thread(target=_plan_timelapse_post, args=(None, message, tl_plan), daemon=True,
                         name="timelapse-partial").start()
        return
    if not marker:
        if tl_plan and tl_plan[1] == "final":
            _plan_timelapse_post(None, message, tl_plan)
        if plan_id:
            post_plate_picker(plan_id)
        return
    job_key = announcer._timelapse_job_key(marker.split(":", 1)[1].strip())
    if not job_key:
        log(f"timelapse postprocess has no matching active job for {marker!r}")
        if plan_id:
            post_plate_picker(plan_id)
        return
    _persist_timelapse_routing(plan_id, tl_plan)
    _start_postprocess(job_key, message, plan_id, tl_plan, "timelapse-postprocess")


def run_announcer(reason):
    """Run announcer once; wait to make completion and its video one Discord message."""
    pending = announcer.pending_completion_message(announcer.load_state())
    if pending:
        tl_key = announcer.pending_timelapse_job_key(announcer.load_state())
        if tl_key:
            # The completion's video leg was armed but never delivered — the
            # listener restarted (or the postprocess died) during the render
            # wait. Re-drive the retrieval for the persisted job instead of
            # posting the pending text bare and silently losing the video
            # (#373). Claim the key first so a later pass cannot re-drive it
            # twice; the postprocess's own fallbacks post text (with the
            # reason) if the video still cannot be attached.
            # A render for this key still running in this process is the
            # original postprocess, not a restart: leave it to post once.
            if _claim_postprocess(tl_key):
                # The persisted key stays until the postprocess's delivery
                # is acknowledged (_post_completion clears it): clearing it
                # here would lose the video leg entirely if this process
                # died again mid-retrieval (P2-145). The in-memory claim
                # above is what keeps one process from re-driving it twice.
                # The persisted routing replays the original multipart
                # flow instead of a bare single-plate post (P2-146).
                plan_id, tl_plan = announcer.pending_timelapse_routing(announcer.load_state())
                _start_postprocess(tl_key, pending, plan_id, tl_plan,
                                   "timelapse-postprocess-retry", claimed=True)
            else:
                log(f"timelapse postprocess for {tl_key!r} still running; not re-driving it")
            _process_announcer_markers(_run_announcer_main(reason))
            return
        delivered = _post_completion(pending, pending)
        if not delivered or announcer.pending_completion_message(announcer.load_state()):
            # Avoid a second identical delivery attempt from main() in this pass.
            _process_announcer_markers(_run_announcer_main(reason))
            return
    marker, plan_id, tl_plan, message = _announcer_markers(_run_announcer_main(reason))
    if not message:
        return
    if tl_plan and tl_plan[1] == "partial":
        # Plan stopped midway: post what exists so far, labeled partial. No picker.
        threading.Thread(target=_plan_timelapse_post, args=(None, message, tl_plan), daemon=True,
                         name="timelapse-partial").start()
        return
    if not marker:
        if tl_plan and tl_plan[1] == "final":
            _plan_timelapse_post(None, message, tl_plan)
        else:
            _post_completion(message, message)
        if plan_id:
            post_plate_picker(plan_id)
        return
    job_key = announcer._timelapse_job_key(marker.split(":", 1)[1].strip())
    if not job_key:
        log(f"timelapse postprocess has no matching active job for {marker!r}")
        if tl_plan and tl_plan[1] == "final":
            _plan_timelapse_post(None, message, tl_plan)
        else:
            # The marker fired, so a video was expected: say in the post why
            # there isn't one instead of letting it vanish silently (#373).
            _post_completion(f"{message}\nTimelapse not attached: the video marker "
                             "didn't match the active job (see the listener log).", message)
        if plan_id:
            post_plate_picker(plan_id)
        return
    # Rendering can take minutes; leave the websocket loop alone and post once when done.
    _persist_timelapse_routing(plan_id, tl_plan)
    _start_postprocess(job_key, message, plan_id, tl_plan, "timelapse-postprocess")


def _short_meta(job_key):
    """Overlay metadata for a vertical short, from what the listener already knows (#317).

    Read from where the data actually lives (P2-112): a normal
    record_start context nests the material under active_spool.material
    and carries no duration at all; the elapsed time is the completed
    record's print_duration_seconds.
    """
    context = {}
    try:
        context = print_records.load_context(job_key) if job_key else {}
    except Exception:
        context = {}
    printer_name = ""
    try:
        printer_name = printers.current().display_name
    except Exception:
        printer_name = ""
    spool = context.get("active_spool")
    material = (context.get("filament_material") or context.get("material")
                or (spool.get("material") if isinstance(spool, dict) else None))
    elapsed = context.get("print_duration") or context.get("elapsed_s")
    if not elapsed and job_key:
        try:
            elapsed = print_records.latest_completed_record(job_key).get("print_duration_seconds")
        except Exception:
            elapsed = None
    return {
        "title": context.get("title") or clean(job_key or "print"),
        "printer": printer_name,
        "material": material,
        "elapsed_s": elapsed,
        "outcome": "Done",
    }


def _vertical_short_attachment(video, job_key):
    """Build the #317 vertical short for a sized timelapse; None when off, failed, or oversize.

    Warn-only by contract: any failure logs and returns None, so the
    completion post always carries the timelapse itself. The short is
    clamped to the configured platform's effective upload limit
    (_media_upload_limit, #314 — the same cap the timelapse shrink
    uses); a short that cannot fit is dropped, never the timelapse.
    """
    try:
        cfg = _config.cfg()
        timelapse_cfg = cfg.get("timelapse") or {}
        if not timelapse_cfg.get("vertical_short"):
            return None
        short = vertical_short.build_short(
            video, meta=_short_meta(job_key),
            mode=str(timelapse_cfg.get("vertical_short_mode") or "pad"),
            overlay=bool(timelapse_cfg.get("vertical_short_overlay", True)),
            ffmpeg=FFMPEG_BIN)
        limit = _media_upload_limit()
        if Path(str(short)).stat().st_size > limit:
            short = platform_sized_video(short, max_bytes=limit)
        if Path(str(short)).stat().st_size > limit:
            log(f"vertical short for {job_key!r} is still over the {limit}-byte platform limit; dropping it")
            return None
        return Path(str(short))
    except Exception as exc:
        log(f"vertical short for {job_key!r} failed; posting timelapse only: {exc!r}")
        return None


def _captured_timelapse(job_key):
    """The in-container captured timelapse for a job, or None (#246 stage 3).

    Stage 3's half of the delivery fallback order: a finished capture
    that assembles under the platform cap wins; anything else — capture
    off, no capture for this job, every encoder failed — returns None
    and the caller uses the printer-rendered path exactly as before.
    Never raises.
    """
    try:
        return timelapse_encode.captured_video(
            job_key, data=_config.cfg(), max_bytes=_media_upload_limit(), log=log)
    except Exception as exc:
        log(f"captured timelapse lookup failed for {job_key!r}: {exc!r}")
        return None


def _dark_capture(job_key):
    """The #476 camera-dark capture outcome for a job (a manifest dict), or None.

    The manifest of a finished in-container capture that kept no frames
    because every frame was completely dark. Gated on capture_active
    exactly like _captured_timelapse's lookup: with capture off there is
    no dark outcome to name, and the delivery thread does no extra work.
    Never raises.
    """
    try:
        data = _config.cfg()
        if not go2rtc.capture_active(data):
            return None
        return timelapse_encode.dark_capture(job_key, data=data)
    except Exception as exc:
        log(f"dark capture lookup failed for {job_key!r}: {exc!r}")
        return None


def _capture_outcome_text():
    """Why the in-container capture path has no video, in one clause (#540).

    Consulted only when the captured-video lookup found nothing and the
    dark outcome did not apply: either capture is disabled, or it is
    enabled but produced no video for the job — no finished capture, or an
    assembly no encoder could complete (the capture and encode layers log
    their own specifics, like a dead source, when they happen). The
    printer-rendered archive states are reported separately and never in
    these terms. [timelapse] mode is named when it is the reason (2.2.2:
    mode=off disables capture whatever capture_enabled says).
    """
    try:
        data = _config.cfg()
        mode = go2rtc.timelapse_mode(data)
        if mode == "off":
            return "timelapse mode is off (timelapse.mode is off)"
        if mode == "container":
            return "in-container capture produced no video for this job"
        if not go2rtc.capture_enabled(data):
            return "in-container capture is disabled (timelapse.capture_enabled is off)"
    except Exception:
        pass
    return "in-container capture produced no video for this job"


# Job keys whose completion video leg is running in this process. The
# complete -> standby transition triggers a second announcer pass while the
# first render is still running; without this, that pass took the pending
# video key for a listener restart, started a second render of the same
# frames, and posted "Print complete" twice (one copy without the video).
_POSTPROCESS_LOCK = threading.Lock()
_POSTPROCESS_ACTIVE = set()


def _claim_postprocess(job_key):
    """Reserve the video leg for job_key; False when one is already running."""
    if not job_key:
        return True
    with _POSTPROCESS_LOCK:
        if job_key in _POSTPROCESS_ACTIVE:
            return False
        _POSTPROCESS_ACTIVE.add(job_key)
        return True


def _release_postprocess(job_key):
    with _POSTPROCESS_LOCK:
        _POSTPROCESS_ACTIVE.discard(job_key)


def _start_postprocess(job_key, completion_message, plan_id, tl_plan, name, claimed=False):
    if not claimed and not _claim_postprocess(job_key):
        log(f"timelapse postprocess already running for {job_key!r}; not starting a second one")
        return False
    threading.Thread(target=_timelapse_postprocess, args=(job_key, completion_message, plan_id, tl_plan),
                     daemon=True, name=name).start()
    return True


def _timelapse_postprocess(job_key, completion_message, plan_id=None, tl_plan=None):
    try:
        if tl_plan:
            _plan_timelapse_post(job_key, completion_message, tl_plan)
        else:
            _timelapse_post(job_key, completion_message)
    finally:
        # The picker follows the completion/timelapse post, whatever happened to the video.
        if plan_id:
            post_plate_picker(plan_id)
        _release_postprocess(job_key)


def _plan_timelapse_post(job_key, completion_message, tl_plan):
    """A plate of a multi-plate plan: keep its clip, post video only once, after the last plate or on cancel.

    Non-final plates post the completion text alone (the caller adds the picker). The final plate,
    or a stopped plan ("partial"), posts one video of every stored clip in plate order.
    """
    plan, stage, order = tl_plan
    clip = None
    if job_key and stage in ("more", "final"):
        try:
            captured = _captured_timelapse(job_key)
            if captured is not None:
                clip = plan_timelapse.add_clip(plan, order, captured)
            elif not go2rtc.rendered_active(_config.cfg()):
                # [timelapse] mode closes the printer-rendered path for
                # plan plates too (2.2.2): no archive lookup happens, and
                # the plate simply contributes no clip.
                dark = _dark_capture(job_key)
                if dark:
                    log(f"timelapse capture for {job_key!r} kept no frames: all "
                        f"{dark.get('dark_frames')} frame(s) were completely dark (#476)")
                else:
                    log(f"plan timelapse clip for {job_key!r} unavailable: timelapse mode is "
                        f"{go2rtc.timelapse_mode(_config.cfg())}; the printer-rendered "
                        "path is off in this mode")
            else:
                outcome = announcer.archive_completed_timelapse(job_key)
                if outcome.state == announcer.ARCHIVED and outcome.path:
                    clip = Path(str(announcer.materialize_archived_timelapse(outcome.path)))
                    plan_timelapse.add_clip(plan, order, clip)
                else:
                    dark = _dark_capture(job_key)
                    if dark:
                        log(f"timelapse capture for {job_key!r} kept no frames: all "
                            f"{dark.get('dark_frames')} frame(s) were completely dark (#476)")
                    elif outcome.state == announcer.ARCHIVE_OFF:
                        log(f"plan timelapse clip for {job_key!r} unavailable: printer-rendered "
                            "archive is off (timelapse.archive_host / archive_dir not configured), "
                            "lookup skipped")
                    elif outcome.state == announcer.ARCHIVE_FAILED:
                        log(f"plan timelapse clip for {job_key!r} unavailable: printer-rendered "
                            f"archive failed: {outcome.detail}")
                    else:
                        log(f"timelapse not rendered for {job_key!r} within the wait")
        except Exception as exc:
            log(f"plan timelapse clip for {job_key!r} unavailable: {exc!r}")
            clip = None
    if stage == "more":
        _post_completion(completion_message, completion_message, job_key=job_key)
        with contextlib.suppress(Exception):
            plan_timelapse.prune()  # abandoned plans never reach a final post
        return
    stored = plan_timelapse.clips(plan)
    expected = plan_timelapse.expected_clips(plan)
    # Partial when any plate's clip is missing, not only the final plate's.
    partial = stage == "partial" or clip is None or (expected is not None and len(stored) < expected)
    try:
        if not stored:
            _post_completion(completion_message, completion_message, job_key=job_key)
            return
        note = f"{completion_message}\nPartial timelapse: {len(stored)} plate(s) so far." if partial else completion_message
        try:
            combined = plan_timelapse.combine(plan)
        except Exception as exc:
            log(f"timelapse combine failed for plan {plan!r}: {exc!r}")
            _post_completion(completion_message, completion_message, job_key=job_key)
            return
        try:
            announcer.archive_local_timelapse(plan, combined)
        except Exception as exc:
            log(f"combined timelapse archive failed for plan {plan!r}: {exc!r}")
        video = combined
        try:
            video = platform_sized_video(combined)
        except Exception as exc:
            log(f"combined timelapse shrink failed for plan {plan!r}: {exc!r}")
        message = f"{note}\nMEDIA:{video}"
        short = _vertical_short_attachment(video, job_key or plan)
        if short is not None:
            message += f"\nMEDIA:{short}"
        if not _post_completion(message, completion_message, job_key=job_key):
            log(f"combined timelapse attach failed for plan {plan!r}; posting text only")
            _post_completion(f"{note}\n{_timelapse_fallback_text(message, combined=True)}", completion_message, job_key=job_key)
    finally:
        plan_timelapse.clear(plan)
        with contextlib.suppress(Exception):
            plan_timelapse.prune()


def _timelapse_post(job_key, completion_message):
    try:
        captured = _captured_timelapse(job_key)
        if captured is not None:
            # The captured video is already assembled under the platform
            # cap by timelapse_encode; it skips the archive + shrink the
            # printer-rendered path needs.
            completion_message = timelapse_encode.caption_with_number(completion_message, job_key)
            message = f"{completion_message}\nMEDIA:{captured}"
            short = _vertical_short_attachment(captured, job_key)
            if short is not None:
                message += f"\nMEDIA:{short}"
            if not _post_completion(message, completion_message, job_key=job_key):
                log(f"captured timelapse attach failed for {job_key!r}; posting text only")
                _post_completion(f"{completion_message}\n{_timelapse_fallback_text(message, captured.name)}",
                                 completion_message, job_key=job_key)
            return
        if not go2rtc.rendered_active(_config.cfg()):
            # [timelapse] mode closes the printer-rendered path (2.2.2):
            # "off" disables timelapses outright — the bare completion
            # post is the configured behavior, like the archive-off case
            # below — and "container" chose the in-container capture,
            # whose absence is named rather than waited out on a render
            # the mode says not to use. A dark capture still names
            # itself first, as below.
            dark = _dark_capture(job_key)
            if dark:
                _post_completion(
                    f"{completion_message}\n{timelapse_capture.dark_outcome_text(dark.get('dark_frames') or 0)}",
                    completion_message, job_key=job_key)
                return
            if go2rtc.timelapse_mode(_config.cfg()) == "off":
                log(f"no timelapse for {job_key!r}: timelapse mode is off")
                _post_completion(completion_message, completion_message, job_key=job_key)
            else:
                log(f"no timelapse for {job_key!r}: {_capture_outcome_text()}; "
                    "printer-rendered path is off in container mode")
                _post_completion(f"{completion_message}\nTimelapse not attached: "
                                 f"{_capture_outcome_text()}.", completion_message, job_key=job_key)
            return
        outcome = announcer.archive_completed_timelapse(job_key)
        archived = outcome.path if outcome.state == announcer.ARCHIVED else None
        if not archived:
            dark = _dark_capture(job_key)
            if dark:
                # #476: the in-container capture ran and every frame was
                # dark — name that outcome in the completion post (the
                # #383 principle) instead of the generic not-rendered line.
                _post_completion(
                    f"{completion_message}\n{timelapse_capture.dark_outcome_text(dark.get('dark_frames') or 0)}",
                    completion_message, job_key=job_key)
                return
            # Name each path's actual state (#540): only ARCHIVE_NOT_RENDERED
            # means a bounded wait elapsed, so only it may be described as a
            # render timeout. With the archive off the bare text post is the
            # configured behaviour and stays quiet in the channel; the log
            # still records both states.
            capture_text = _capture_outcome_text()
            if outcome.state == announcer.ARCHIVE_OFF:
                log(f"no timelapse for {job_key!r}: {capture_text}; printer-rendered archive "
                    "is off (timelapse.archive_host / archive_dir not configured), lookup skipped")
                _post_completion(completion_message, completion_message, job_key=job_key)
            elif outcome.state == announcer.ARCHIVE_FAILED:
                log(f"no timelapse for {job_key!r}: {capture_text}; printer-rendered archive "
                    f"failed: {outcome.detail}")
                _post_completion(f"{completion_message}\nTimelapse not attached: the printer's "
                                 "rendered video was found, but archiving it failed "
                                 "(see the listener log).", completion_message, job_key=job_key)
            else:
                log(f"timelapse not rendered for {job_key!r} within the wait"
                    + (f" ({outcome.detail})" if outcome.detail else ""))
                _post_completion(f"{completion_message}\nTimelapse not attached: the printer's "
                                 "rendered video didn't arrive within the wait "
                                 "(see the listener log).", completion_message, job_key=job_key)
            return
        local_video = announcer.materialize_archived_timelapse(archived)
        try:
            local_video = platform_sized_video(local_video)
        except Exception as exc:  # oversize source still gets a try; the text fallback covers a refusal
            log(f"timelapse shrink failed for {job_key!r}: {exc!r}")
        message = f"{completion_message}\nMEDIA:{local_video}"
        short = _vertical_short_attachment(local_video, job_key)
        if short is not None:
            message += f"\nMEDIA:{short}"
        if not _post_completion(message, completion_message, job_key=job_key):
            # Never lose the completion notice because the platform refused the attachment.
            log(f"timelapse attach failed for {job_key!r}; posting text only")
            _post_completion(f"{completion_message}\n{_timelapse_fallback_text(message, Path(str(archived)).name)}", completion_message, job_key=job_key)
    except Exception as exc:
        log(f"timelapse postprocess failed for {job_key!r}: {exc!r}")
        _post_completion(completion_message, completion_message, job_key=job_key)


# paths.ffmpeg: a bare name is looked up on PATH, a path is used as is.
FFMPEG_BIN = (shutil.which(_config.ffmpeg_bin()) or _config.ffmpeg_bin()) if "/" not in _config.ffmpeg_bin() else _config.ffmpeg_bin()


def _media_upload_limit():
    """The timelapse size cap in bytes for the configured platform (#314).

    The one resolution every listener video goes through: bot-core
    delivery's rule (core.delivery.upload_limit — the configured
    platform's [platform] upload_max_bytes, that platform's settings
    default, [discord] as the fallback), tightened for a standalone
    Matrix install by the homeserver's own m.upload.size when the
    server reports one — the same runtime clamp the bot's Matrix
    adapter applies. Discord's resolution is exactly what it was, so
    Discord sizing is unchanged. A failed clamp lookup degrades to the
    configured limit; it never blocks a completion post.
    """
    limit = delivery.upload_limit(_config.cfg())
    if _matrix_standalone():
        try:
            from print_handy.bot import matrix_rest
            server_limit = matrix_rest.media_config(_matrix_homeserver(), _matrix_token())
        except Exception:
            server_limit = None
        if server_limit:
            limit = min(limit, int(server_limit))
    return limit


def _timelapse_too_big_text(archived_name=None, *, combined=False):
    """Why a timelapse is missing from a completion post, in the platform's terms (#314).

    Discord's lines are the ones it has always posted. On Matrix the
    sentence names the real cause — the server's upload limit — so a
    household whose server cannot take the video is told the timelapse
    was recorded but cannot be delivered, instead of it silently never
    arriving. A failed attachment is never reported as delivered.
    """
    platform = _platform_name()
    if platform == "matrix":
        subject = "Combined timelapse" if combined else "Timelapse"
        return f"{subject} was recorded but is too large for this Matrix server's upload limit."
    if platform == "telegram":
        subject = "Combined timelapse" if combined else "Timelapse"
        return f"{subject} was recorded but is too large for Telegram's upload limit."
    if combined:
        return "Combined timelapse too big for Discord."
    return f"Timelapse archived but too big for Discord: {archived_name}"


def _timelapse_unposted_text(archived_name=None, *, combined=False):
    """Why a timelapse is missing when its post failed for a non-size reason.

    The cause is not known (transport, auth, network), so the line says
    only what is true: the video exists — archived under the same name —
    but could not be posted. It never names a cause that was not measured.
    """
    platform = _platform_name()
    if platform in ("matrix", "telegram"):
        subject = "Combined timelapse" if combined else "Timelapse"
        return f"{subject} was recorded but could not be posted."
    if combined:
        return "Combined timelapse could not be posted."
    return f"Timelapse archived but could not be posted: {archived_name}"


def _message_media_oversize(message):
    """True when a MEDIA: attachment in the message exceeds the upload limit."""
    limit = _media_upload_limit()
    for match in re.finditer(r"MEDIA:(\S+)", str(message)):
        try:
            if Path(match.group(1)).stat().st_size > limit:
                return True
        except OSError:
            continue
    return False


def _timelapse_fallback_text(message, archived_name=None, *, combined=False):
    """The honest fallback line after a failed timelapse post (#524).

    "Too big" is claimed only when an attached video actually exceeds the
    platform's upload limit; every other failure gets the generic line.
    """
    if _message_media_oversize(message):
        return _timelapse_too_big_text(archived_name, combined=combined)
    return _timelapse_unposted_text(archived_name, combined=combined)


def _video_duration(path):
    probe = str(Path(FFMPEG_BIN).with_name("ffprobe"))
    out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    return float(out)


def _timelapse_encoder():
    """The [timelapse] encoder choice for the sizing shrink (#314): "cpu" unless configured.

    One of vertical_short.ENCODERS; anything unexpected resolves to
    "cpu" here as well as in settings validation, so a bad value can
    cost hardware acceleration but never an encode.
    """
    try:
        encoder = str((_config.cfg().get("timelapse") or {}).get("encoder") or "cpu").strip().lower()
    except Exception:
        return "cpu"
    return encoder if encoder in vertical_short.ENCODERS else "cpu"


def _encode_sized(source, partial, kbps, encoder):
    """One ffmpeg encode of source toward partial at kbps, with the seam's codec for encoder.

    ffmpeg is the compressor — there is no other shrink mechanism. The
    codec choice comes from vertical_short's encoder seam (#246/#311
    names); the bitrate budget that makes the output fit is the sizing
    strategy and stays here.
    """
    subprocess.run([FFMPEG_BIN, "-y", "-v", "error", *vertical_short.encoder_input_args(encoder),
                    "-i", str(source), "-an",
                    "-vf", vertical_short.encoder_filter(encoder, "scale='min(1280,iw)':-2"),
                    *vertical_short.encoder_args(encoder),
                    "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
                    "-movflags", "+faststart", str(partial)],
                   capture_output=True, timeout=600, check=True)


def platform_sized_video(path, max_bytes=None):
    """Return a copy of path re-encoded under the platform upload cap, or path if it already fits.

    The cap defaults to _media_upload_limit() (#314): the configured
    platform's effective upload limit, not always Discord's. The
    two-attempt bitrate strategy is unchanged from the Discord-only
    sizing this generalises. The encode is ffmpeg's, with the
    [timelapse] encoder choice (CPU libx264 by default; VAAPI/QSV/NVENC
    when configured), and any hardware-encode failure retries the same
    attempt with the CPU encoder — it never fails the shrink, let alone
    the completion post.
    """
    max_bytes = max_bytes or _media_upload_limit()
    source = Path(str(path))
    if source.stat().st_size <= max_bytes:
        return source
    target = source.with_name(f"{source.stem}.sized.mp4")
    if target.is_file() and 0 < target.stat().st_size <= max_bytes:
        return target
    duration = max(1.0, _video_duration(source))
    # 90% of the budget for video; timelapses carry no audio.
    kbps = max(200, int(max_bytes * 8 * 0.90 / duration / 1000))
    encoder = _timelapse_encoder()
    for attempt_kbps in (kbps, int(kbps * 0.7)):
        partial = timelapse_encode.unique_partial(target)
        used = encoder
        try:
            try:
                _encode_sized(source, partial, attempt_kbps, encoder)
            except Exception:
                if encoder == "cpu":
                    raise
                log(f"timelapse {encoder} encode failed for {source.name}; retrying with the CPU encoder")
                used = "cpu"
                _encode_sized(source, partial, attempt_kbps, "cpu")
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        if partial.stat().st_size <= max_bytes:
            os.replace(partial, target)
            log(f"timelapse shrunk {source.stat().st_size} -> {target.stat().st_size} bytes at {attempt_kbps}k ({used})")
            return target
        partial.unlink(missing_ok=True)
    raise RuntimeError(f"could not shrink {source.name} under {max_bytes} bytes")



NOTIFICATION_STATE = _config.state_file("PRINT_HANDY_NOTIFICATION_STATE", "lifecycle_notifications.json", per_instance=True)


def _notification_identity(stats):
    """Stable per-job identity; prefer Moonraker history id, then filename plus start time."""
    job_id = stats.get("job_id") or stats.get("history_job_id")
    if job_id is not None:
        return f"job:{job_id}"
    filename = stats.get("filename")
    started = stats.get("print_start_time") or stats.get("start_time")
    if filename and started is not None:
        return f"file:{filename}|start:{started}"
    return None


def _claim_notification(identity, phase, path=None):
    """Persist an at-most-once lifecycle notification claim, atomically."""
    if not identity:
        return True
    target = Path(path or NOTIFICATION_STATE)
    state = read_json_object(target)
    key = f"{identity}|{phase}"
    if key in state.get("sent", []):
        return False
    sent = list(state.get("sent", []))
    sent.append(key)
    state["sent"] = sent[-500:]
    atomic_write_json(target, state)
    return True

def _record_transition_markers(old, new_state, filename, stats):
    """Record #317 event markers for one print state transition.

    print_markers.record() never raises for storage problems, so marker
    recording can never break the listener or a post. When the #299
    per-domain listener contract lands, its normalised events should call
    print_markers.record() the same way; for now the Moonraker transitions
    below are the only producer.
    """
    if not filename:
        return
    elapsed = stats.get("print_duration") if isinstance(stats, dict) else None
    try:
        elapsed = float(elapsed) if elapsed is not None else None
    except (TypeError, ValueError):
        elapsed = None
    active = old in ("printing", "paused")
    if new_state == "printing" and old == "paused":
        print_markers.record(filename, print_markers.RESUME, elapsed_s=elapsed)
    elif new_state == "printing" and not active:
        print_markers.record(filename, print_markers.START, elapsed_s=elapsed)
    elif new_state == "paused" and old == "printing":
        print_markers.record(filename, print_markers.PAUSE, elapsed_s=elapsed)
    elif active and new_state == "cancelled":
        print_markers.record(filename, print_markers.CANCEL, elapsed_s=elapsed)
        print_markers.record(filename, print_markers.COMPLETE, elapsed_s=elapsed, outcome="cancelled")
    elif active and new_state == "error":
        print_markers.record(filename, print_markers.ERROR, elapsed_s=elapsed)
        print_markers.record(filename, print_markers.COMPLETE, elapsed_s=elapsed, outcome="failed")
    elif active and new_state == "complete":
        print_markers.record(filename, print_markers.COMPLETE, elapsed_s=elapsed, outcome="success")


def clean(filename):
    return print_summary.safe_label(filename, "print")


# Start claims stay shared on purpose: print-now writes them and every
# listener reads them, keyed by the content-hashed remote file name —
# a cross-process channel, not one listener's operational state (P1-82).
START_CLAIMS = start_claims.START_CLAIMS
START_CLAIM_TTL_S = start_claims.START_CLAIM_TTL_S
NATIVE_POLL_SECONDS = float(_LISTENER["ifs_presence_poll_s"])
# After a failed clock set (no DATE_SET macro, refused, still off) the poll waits this long
# before trying again, so a printer without the macro is not sent it every minute.
CLOCK_SYNC_BACKOFF_S = 1800.0
# How often the listener's poll re-checks the printer's clock for a sync. Connect and Klipper
# ready (a printer reboot) still sync at once; this only catches a printer that was busy then.
CLOCK_SYNC_INTERVAL_S = 600.0
IFS_EDGE_STATE = _config.state_file("IFS_EDGE_STATE", "ifs_presence_edges.json", per_instance=True)
IFS_PENDING_STATE = _config.state_file("IFS_PENDING_STATE", "ifs_pending_prompts.json", per_instance=True)
RUNOUT_PAUSE_STATE = _config.state_file("RUNOUT_PAUSE_STATE", "runout_pause.json", per_instance=True)
# The last stock-screen error alerted, so a listener restart does not post the same error again.
NATIVE_ERROR_STATE = _config.state_file("NATIVE_ERROR_STATE", "native_error_alerted.json", per_instance=True)
SPOOLMAN_KNOWN_STATE = _config.state_file("SPOOLMAN_KNOWN_STATE", "spoolman_known_spools.json", per_instance=True)
LANE_DATA_STATE = _config.state_file("LANE_DATA_STATE", "lane_data_watch.json", per_instance=True)
RUNOUT_SPOOL_COMMENT = "runout replacement; starting weight supplied by operator"
# An undelivered new-spool prompt is retried this often, and given up after a day.
NEW_SPOOL_RETRY_S = 300.0
NEW_SPOOL_GIVE_UP_S = 24 * 3600.0
HARDWARE_STATE = _config.state_file("PRINTER_HARDWARE_STATE", "printer_hardware.json")


def _hardware_file():
    """The record the slicer reads: the default printer's own file once it exists."""
    if os.environ.get("PRINTER_HARDWARE_STATE"):
        return HARDWARE_STATE
    from .spool_sync import default_hardware_file
    return default_hardware_file(HARDWARE_STATE)


def read_json_object(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.replace(temporary, path)


def insertion_prompt(slot):
    return (f"Filament loaded in IFS slot {slot}. If it is the same spool, ignore this. "
            f"If it is a different one: `/spool change {slot} <spool#>` for a spool already in Spoolman, "
            f"or `/spool new brand, material, color, grams` then `/spool change {slot} <new#>`. "
            "`/spool list` shows the shelf. Nothing has been changed yet.")


def insertion_confirmation_text(slot):
    """Short human confirmation prompt for a newly detected idle IFS insertion."""
    return (f"**IFS slot {slot} has filament.** Is this the same spool already mapped to that slot? "
            "Tap Same spool, Existing spool, or New spool. No inventory or printer change happens until you answer.")


def spool_display_label(spool):
    """The one picker label shape (#792 part 2): 'Matte White · PLA+ · #21 · 1000 g left'.

    Color, material, spool number and grams left, with unknowns said in
    place — the same label the spool pickers show, so the new-spool
    prompt, its slot question and its load confirmation all name the
    exact same spool the same way.
    """
    filament = (spool or {}).get("filament") or {}
    return print_summary.spool_picker_label(
        filament.get("name"), filament.get("material"),
        (spool or {}).get("id"), (spool or {}).get("remaining_weight"))


def new_spool_prompt_text(spool):
    """The one-time Discord prompt for a spool created outside Print Handy (#483)."""
    return (f"**New spool in Spoolman: {spool_display_label(spool)}.**\n"
            "It was added outside Print Handy, so no slot uses it yet. **Load into a slot** walks "
            "you through loading it; **Leave on the shelf** keeps it unassigned; **Dismiss** just "
            "closes this. You are only ever asked once about a spool.")


def new_spool_plain_text(spool):
    """The buttonless wording (Telegram/Matrix): the same facts, answered with /spool."""
    return (f"New spool in Spoolman: {spool_display_label(spool)}. It was added outside Print Handy, "
            f"so no slot uses it yet. Load it with `/spool change <slot> {(spool or {}).get('id')}`, "
            "or leave it on the shelf — you are only ever asked once about a spool.")


def post_new_spool_prompt(spool):
    """Post the one-time new-spool prompt; fall back to plain text if Discord refuses buttons."""
    try:
        from ..notify import emit_queued
        emit_queued("spool_added", new_spool_plain_text(spool).splitlines()[0])
    except Exception:
        pass
    if _buttonless_standalone():
        return post(new_spool_plain_text(spool))
    try:
        from . import print_alert_buttons as pab
        if pab.post_new_spool(_bot_token(), announcer.DISCORD_CHAT_ID,
                              new_spool_prompt_text(spool), int(spool["id"]), spool_display_label(spool)):
            log(f"post new spool prompt spool={spool.get('id')}")
            return True
        log(f"new spool button post failed spool={spool.get('id')}; falling back to text")
    except Exception as exc:
        log(f"new spool button post error: {exc!r}")
    return post(new_spool_plain_text(spool))


def created_by_print_handy(spool):
    """True for a spool Print Handy made itself: /spool new, the insertion modal or a runout replacement."""
    from . import spool_sync
    comment = str((spool or {}).get("comment") or "").strip()
    # A purchase spool (#801) appends "; purchased: <url>" to the created
    # comment; it is still Print Handy-made, so the head decides.
    return comment in (spool_sync.CREATED_SPOOL_COMMENT, RUNOUT_SPOOL_COMMENT) \
        or comment.startswith(spool_sync.CREATED_SPOOL_COMMENT + ";")


def _pending_entries(raw):
    """{spool id: {"since", "tried"}} from the persisted pending map, skipping anything malformed."""
    entries = {}
    if not isinstance(raw, dict):
        return entries
    for key, value in raw.items():
        try:
            entries[int(key)] = {"since": float(value["since"]), "tried": float(value["tried"])}
        except (TypeError, ValueError, KeyError):
            continue
    return entries


class SpoolmanNewSpoolWatcher:
    """Diff Spoolman inventory against a persisted known-set; prompt once per new spool (#483).

    The Spoolman-side twin of FilamentInsertionMonitor: that one watches IFS
    slot presence at the printer, this one watches Spoolman itself, so a
    spool created in Spoolman's UI (or by another tool) gets its Discord
    prompt too. The first successful poll seeds the known-set silently —
    existing inventory is history, not news. Spools Print Handy created
    itself (recognised by the comment it stamps on them) are recorded
    silently, as are new spools that arrive already archived (a history
    import, not a new physical spool); deleted IDs drop out of the set.

    A new spool waits in "pending" until the caller reports the prompt
    delivered, so a chat outage does not lose it: a pending prompt is
    retried every NEW_SPOOL_RETRY_S, across restarts, and given up after
    NEW_SPOOL_GIVE_UP_S. Pending spools that are archived or deleted in the
    meantime are dropped. Fail-soft, per the #387 posture: when Spoolman
    does not answer, list_spools returns None and the poll reports nothing
    and changes nothing.
    """

    def __init__(self, state_path=SPOOLMAN_KNOWN_STATE, lister=None, clock=time.time):
        self.state_path = Path(state_path)
        self.lister = lister
        self.clock = clock

    def poll(self):
        """Spool records to prompt for now: new ones, plus undelivered ones due for a retry."""
        lister = self.lister
        if lister is None:
            # Archived spools included: Spoolman hides them by default, and a spool
            # that left the list on archiving would be "new" again when unarchived.
            from print_handy.core import spoolman_client
            lister = lambda: spoolman_client.list_spools(allow_archived=True)  # noqa: E731
        rows = lister()
        if not isinstance(rows, list):
            return []
        current = {}
        for row in rows:
            sid = row.get("id") if isinstance(row, dict) else None
            if isinstance(sid, int) and not isinstance(sid, bool):
                current[sid] = row
        state = read_json_object(self.state_path)
        known = state.get("known")
        if not state.get("seeded") or not isinstance(known, list):
            atomic_write_json(self.state_path, {"seeded": True, "known": sorted(current), "pending": {}})
            return []
        known_ids = {item for item in known if isinstance(item, int) and not isinstance(item, bool)}
        pending = _pending_entries(state.get("pending"))
        now = self.clock()
        for sid in sorted(current):
            row = current[sid]
            if sid not in known_ids and not row.get("archived") and not created_by_print_handy(row):
                pending[sid] = {"since": now, "tried": 0.0}
        due = []
        for sid in sorted(pending):
            row, entry = current.get(sid), pending[sid]
            if row is None or row.get("archived"):
                del pending[sid]  # deleted or archived since: nothing left to ask about
            elif now - entry["since"] >= NEW_SPOOL_GIVE_UP_S:
                log(f"spoolman watcher gave up on the undelivered prompt for spool #{sid}")
                del pending[sid]
            elif now - entry["tried"] >= NEW_SPOOL_RETRY_S:
                entry["tried"] = now
                due.append(row)
        updated = {"seeded": True, "known": sorted(current),
                   "pending": {str(sid): entry for sid, entry in sorted(pending.items())}}
        if updated != state:
            atomic_write_json(self.state_path, updated)
        return due

    def delivered(self, spool_id):
        """Clear a spool's pending prompt once it has reached the chat."""
        state = read_json_object(self.state_path)
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.pop(str(int(spool_id)), None) is not None:
            atomic_write_json(self.state_path, state)


def post_lane_event(event):
    """Post one HelixScreen lane announcement (#791) to the printer channel."""
    text = lane_data_sync.lane_event_text(event)
    if not text:
        return False
    return post(text)


def feed_screen_runout_load(slot, spool_id, context, *, client=None, mr=None, state_path=None):
    """Feed a screen-side load during a paused runout into the existing recovery (#791).

    The same steps a chat answer to the spool question takes (the plugin's
    _answer_spool_question existing-spool path): bind the slot with the
    runout disposition so the depleted spool is marked depleted by the bind
    itself, resume filament usage on the new spool, and clear the open
    question so its reminders stop. The physical load at the screen is the
    operator's answer; nothing here moves the printer.

    One exception (#1063): when the screen shows the very spool that ran
    out back in the slot, that is a refeed, not a replacement — the same
    physical spool, re-fed after a feed failure. Usage resumes on it and
    the question closes; nothing is bound, created or depleted. (Before
    #1063 this returned False and the question could never be answered
    from the screen for that spool.)
    """
    from . import spool_sync
    client = client or spool_sync.spoolman
    mr = mr or spool_sync.moonraker
    cause = (context or {}).get("cause")
    prior = (context or {}).get("prior_spool_id")
    if cause == "runout" and prior is not None and int(prior) == int(spool_id):
        spool_sync.resume_usage(int(spool_id), mr=mr)
        clear_runout_pause()
        return True
    spool_sync.bind_slot(int(slot), int(spool_id), client=client, mr=mr, state_path=state_path,
                         prior_disposition="depleted" if cause == "runout" else "shelf",
                         note="Bound from HelixScreen during a paused runout")
    spool_sync.resume_usage(int(spool_id), mr=mr)
    clear_runout_pause()
    return True


def _log_usage_entry(filename, entry):
    """One log line for a spool usage charge or check, when there was one."""
    if not entry:
        return
    if entry.get("kind") == "single_slot":
        log(f"spool usage check file={filename!r} spool=#{entry.get('spool_id')} "
            f"status={entry.get('status')} expected={entry.get('expected_g')}g "
            f"observed={entry.get('observed_g')}g reason={entry.get('reason')!r}")
        return
    log(f"multi-slot usage file={filename!r} status={entry.get('status')} "
        + " ".join(f"slot{row.get('slot')}:#{row.get('spool_id')}={row.get('grams')}g/{row.get('status')}"
                   for row in entry.get("slots") or []))


class FilamentInsertionMonitor:
    """Stable native slot presence detector. Sends text only, never edits inventory."""
    # After a reboot or reconnect ZMod can report placeholder presence for a while
    # (2026-10-30: four false insertion prompts 20 s after klippy ready). Readings that
    # disagree with the persisted edges are held for this long before being adopted.
    SETTLE_SECONDS = 120

    def __init__(self, edge_path=IFS_EDGE_STATE, pending_path=IFS_PENDING_STATE):
        self.edge_path = Path(edge_path)
        self.pending_path = Path(pending_path)
        self.warmup_pending = False
        self.warm_until = None
        self.reset_baseline()

    def reset_baseline(self, *, warmup=False):
        """Forget in-memory arming; the persisted edges stay the physical reference.

        ``warmup`` marks a Klipper/Moonraker reconnect: ZMod can report one all-empty
        placeholder sample after it first reads the real slots, which looked like four
        quick pull-and-reinserts (2026-11-02: four prompts 30 s after a restart). For
        SETTLE_SECONDS after re-arming, any reading that disagrees with the persisted
        edges goes through the silent settle path instead of prompting."""
        if warmup:
            self.warmup_pending = True
            self.warm_until = None
        self.baselined = False
        self.last_sample = {}
        self.stable_counts = {}
        self.transition_from = {}
        self.settling_since = None

    def _start_warmup(self):
        if self.warmup_pending:
            self.warmup_pending = False
            self.warm_until = time.monotonic() + self.SETTLE_SECONDS

    def _armed(self, current):
        self._start_warmup()
        self.last_sample = current
        self.stable_counts = {slot: 2 for slot in current}
        self.transition_from = {}
        self.settling_since = None
        self.baselined = True

    def observe(self, slots, *, idle, connected=True):
        """Return newly confirmed slots; startup/reconnect samples only establish baseline."""
        if not connected or not idle or not isinstance(slots, dict):
            self.reset_baseline()
            return []
        wanted = {str(slot): value for slot, value in slots.items() if str(slot) in {"1", "2", "3", "4"}}
        if any(not isinstance(value, bool) for value in wanted.values()):
            # An unknown slot reading is not a physical state: never arm or emit on it.
            self.reset_baseline()
            return []
        current = wanted
        edges = read_json_object(self.edge_path)
        if self.settling_since is not None:
            # Rebaseline after reset: a reboot is not a user action, so readings returning
            # to the persisted edges prompt nothing, and a disagreement is adopted silently
            # only once it has held for SETTLE_SECONDS.
            if all(edges.get(slot) == present for slot, present in current.items()):
                self._armed(current)
            elif (current == self.last_sample
                    and time.monotonic() - self.settling_since >= self.SETTLE_SECONDS):
                edges.update(current)
                atomic_write_json(self.edge_path, edges)
                self._armed(current)
            else:
                if current != self.last_sample:
                    # The readings changed shape mid-settle: restart the
                    # window. It measures how long THIS reading has held,
                    # not how long any disagreement has existed — adopting
                    # a two-second-old reading on the first reading's
                    # clock silently rewrote the edges and later produced
                    # a false insertion prompt when the readings settled
                    # back (P2-271).
                    self.settling_since = time.monotonic()
                self.last_sample = current
            return []
        if not self.baselined:
            known = {slot: present for slot, present in current.items() if slot in edges}
            if any(edges[slot] != present for slot, present in known.items()):
                self.settling_since = time.monotonic()
                self.last_sample = current
                self.baselined = True
                return []
            self.last_sample = current
            self.stable_counts = {slot: 1 for slot in current}
            if any(slot not in edges for slot in current):
                edges.update(current)
                atomic_write_json(self.edge_path, edges)
            self._start_warmup()
            self.baselined = True
            return []
        if (self.warm_until is not None and time.monotonic() < self.warm_until
                and any(slot in edges and edges[slot] != present for slot, present in current.items())):
            self.settling_since = time.monotonic()
            self.last_sample = current
            return []
        stable = []
        pending = read_json_object(self.pending_path)
        changed = False
        for slot, present in current.items():
            prior = self.last_sample.get(slot)
            self.stable_counts[slot] = self.stable_counts.get(slot, 0) + 1 if prior == present else 1
            if prior != present:
                self.transition_from[slot] = prior
            # A quick pull/reinsert can produce only one absent sample: edges remains True,
            # but the physical change is still worth an inventory-only confirmation prompt.
            if (self.transition_from.get(slot) is False and present is True and edges.get(slot) is True):
                stable.append(int(slot))
                pending[slot] = {"event": f"slot-{slot}-present", "status": "awaiting_user_dispatch"}
                self.transition_from[slot] = None
            elif self.stable_counts[slot] >= 2 and edges.get(slot) != present:
                edges[slot] = present
                changed = True
                if self.transition_from.get(slot) is False and present is True:
                    stable.append(int(slot))
                    pending[slot] = {"event": f"slot-{slot}-present", "status": "awaiting_user_dispatch"}
                    self.transition_from[slot] = None
        self.last_sample = current
        if changed:
            atomic_write_json(self.edge_path, edges)
        if stable:
            atomic_write_json(self.pending_path, pending)
        return stable

    def prune_pending(self, slots, open_prompt_slots):
        """Drop pending prompts that were answered or expired, or whose slot no longer reads loaded."""
        pending = read_json_object(self.pending_path)
        present = _slot_bools(slots)
        kept = {}
        for slot, entry in pending.items():
            sid = int(slot) if str(slot).isdigit() else None
            if sid in open_prompt_slots and present.get(sid) is not False:
                kept[slot] = entry
        if kept != pending:
            atomic_write_json(self.pending_path, kept)


# Start claims and the abandoned-start rule live in start_claims (#1320), shared with
# the family listeners; these wrappers read this module's START_CLAIMS and
# START_CLAIM_TTL_S, so the listener's long-standing names keep working.
def start_claim_time(filename, now=None):
    """When print-now claimed this job's start announcement, or None when unclaimed/expired."""
    return start_claims.start_claim_time(filename, now, path=START_CLAIMS, ttl_s=START_CLAIM_TTL_S)


def start_claimed(filename, now=None):
    """True when model_job.py print-now already owns this job's start announcement."""
    return start_claim_time(filename, now) is not None


def claimed_start_filenames(now=None):
    """The unexpired claimed start files, as print-now recorded them (the uploaded names)."""
    return start_claims.claimed_start_filenames(now, path=START_CLAIMS, ttl_s=START_CLAIM_TTL_S)


def release_start_claim(filename):
    """Drop this file's start claim (the listener's counterpart of model_job's release)."""
    return start_claims.release_start_claim(filename, path=START_CLAIMS)


ABANDONED_START_COLD_S = start_claims.ABANDONED_START_COLD_S
ABANDONED_START_MESSAGE = ("The printer stopped preparing this job without printing. "
                           "Check the IFS slot / HelixScreen.")
AbandonedStartWatch = start_claims.AbandonedStartWatch


def announce_start_abandoned(filename):
    """A claimed start never became a print: edit its one start card, never post another.

    Mirrors announce_warmup_done: the recorded start message id is consumed, so the edit
    happens at most once, and with no recorded id the start post is left as is.
    """
    message_id = None
    channel_id = None
    try:
        from . import print_next
        message_id, channel_id = print_next.take_start_message_target(filename)
    except Exception as exc:
        log(f"start message lookup failed file={filename!r}: {exc!r}")
    if not message_id:
        log(f"no recorded start message; abandoned start not edited file={filename!r}")
        return False
    text = print_summary.format_event("started", filename)
    line = _objects_line(filename)
    if line:
        text += "\n" + line
    text += "\n" + ABANDONED_START_MESSAGE
    try:
        from . import print_alert_buttons as pab
        if pab.edit_message(_bot_token(), channel_id or announcer.DISCORD_CHAT_ID,
                            message_id, text):
            log(f"start message edited (abandoned) id={message_id} file={filename!r}")
            return True
    except Exception as exc:
        log(f"start message edit error id={message_id}: {exc!r}")
    log(f"start message edit failed id={message_id}; leaving the start post as is")
    return False


def announce_warmup_done(filename):
    """A claimed start already has its one "Print started" post: edit it, never post another.

    The intake posted its start message and recorded its id; this edit adds
    the warmup-complete line, keeping the canonical start details. The
    recorded id is consumed, so a replayed transition cannot edit twice.
    With no recorded id, or when Discord refuses the edit, the start post is
    left as is — no second post. The roast belongs to the completion post.
    """
    text = print_summary.format_event("started", filename)
    line = _objects_line(filename)
    if line:
        text += "\n" + line
    from ..core import status_voice
    # print_monitor: the one in-print milestone post that exists. The key is
    # per print and milestone, so a replayed edit keeps the same line.
    text += "\n" + status_voice.voiced("Warmup complete, now printing.", "print_monitor",
                                       event_key=f"{filename}:warmup_done")
    message_id = None
    channel_id = None
    try:
        from . import print_next
        message_id, channel_id = print_next.take_start_message_target(filename)
    except Exception as exc:
        log(f"start message lookup failed file={filename!r}: {exc!r}")
    if not message_id:
        # The intake already posted the one "Print started" message; a second line after it is noise.
        # Expected, not a fault (#1282): only a Discord intake whose start was still warming
        # records its card for this edit. Other print-now starts (web UI, reprints, a start
        # already printing when print-now returned) posted their final start text already.
        log(f"no warmup edit recorded for this claimed start (its start post is final); "
            f"warmup done silently file={filename!r}")
        return False
    if message_id:
        try:
            from . import print_alert_buttons as pab
            # Edit where the start post actually lives (P2-11): the intake may
            # have originated in another channel or a thread. Rows recorded
            # before destinations were kept fall back to the configured one.
            if pab.edit_message(_bot_token(), channel_id or announcer.DISCORD_CHAT_ID,
                                message_id, text):
                log(f"start message edited id={message_id} file={filename!r}")
                return True
        except Exception as exc:
            log(f"start message edit error id={message_id}: {exc!r}")
        log(f"start message edit failed id={message_id}; leaving the start post as is")
    return False


SUBSCRIBE = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "printer.objects.subscribe",
    "params": {"objects": {"print_stats": ["state", "filename", "message", "info"],
                             "gcode_move": ["gcode_position"], "toolhead": ["position"]}},
})

# ZMod START_PRINT logs `Printing with bed mesh: <profile>`. A blank profile means no mesh is
# applied; that is what made the 2026-10-03 first layers print too high.
NO_MESH_ALERT = ("**Heads up: this print started with NO bed mesh.** The first layer will likely be too high or "
                 "uneven, like the cancelled one on Oct 3. Watch it, and cancel if it is not sticking. Fix: run "
                 "full bed leveling on the printer screen before the next print.")


def no_mesh_line(line):
    """True when a gcode response line reports a print starting with no bed mesh profile."""
    marker = "Printing with bed mesh:"
    if not isinstance(line, str) or marker not in line:
        return False
    return line.split(marker, 1)[1].strip() == ""


SPOOL_NAG_SECONDS = float(_LISTENER["spool_nag_s"])


def _camera_url():
    return _config.require(_config.service_url("camera_snapshot_url"), "services.camera_snapshot_url")


def _vision_chat_url():
    return _listener_common._vision_chat_url()


# The visual-watch implementation is shared with the Bambu camera watch (#294):
# these names are the shared module's, re-exported so existing references work.
VISUAL_VERDICTS = _visual_watch.VISUAL_VERDICTS
VISUAL_ALERT_VERDICTS = _visual_watch.VISUAL_ALERT_VERDICTS
VISUAL_HEALTHY_VERDICTS = _visual_watch.VISUAL_HEALTHY_VERDICTS
VISUAL_SENSITIVITY = _visual_watch.VISUAL_SENSITIVITY
_visual_level = _visual_watch._visual_level
_visual_profile = _visual_watch._visual_profile
_frame_changed = _visual_watch._frame_changed
_strip_json_fence = _visual_watch._strip_json_fence
VisualPrintWatch = _visual_watch.VisualPrintWatch
_VISION_BASE = _config.service_url("vision_chat_url")
_VISION_MODEL = _listener_common._VISION_MODEL


def _vision_context() -> str:
    """The verdict context for a Moonraker frame, built from the serving printer.

    The Bambu and family contexts are built from their printer's label for
    the same reason (bambu_watch.vision_context): a frame judged against
    another printer's description is a wrong prompt — this context used to
    name the Flashforge AD5X whatever printer the listener served. The
    name is the serving printer's configured name or model label
    (printers.current().display_name, the same source the completion
    summary uses). No enclosure or plate claim is made: the listener does
    not know them for an arbitrary Moonraker printer
    (family_control._idle_context takes the same care).
    """
    label = ""
    try:
        label = printers.current().display_name
    except Exception:
        label = ""
    label = str(label or "").strip() or "3D printer"
    return (
        f"Printer: {label}, build plate. "
        "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
        "ok means plastic is on the bed and printing normally. "
        "air means the bed is empty while a print should be there. "
        "spaghetti means a tangled failed extrusion. "
        "detached means a printed part has come off the plate. "
        "empty_idle means the printer is idle and the bed is empty. "
        "uncertain means the frame is not clear enough to judge."
    )


# One camera still: at most SNAPSHOT_MAX_BYTES, read within SNAPSHOT_BUDGET_S
# in total. The per-read timeout alone let a slow-drip camera (a byte every
# few seconds) hold a watch worker for ever.
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024
SNAPSHOT_BUDGET_S = 15.0


def _fetch_snapshot(url, *, budget_s=None, clock=time.monotonic):
    budget_s = SNAPSHOT_BUDGET_S if budget_s is None else budget_s
    deadline = clock() + budget_s
    # Credentials in the URL travel as a Basic header, never as part of the
    # host urllib resolves (and names in its errors).
    request = camera_url.request(url)
    chunks, size = [], 0
    with urllib.request.urlopen(request, timeout=8) as response:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > SNAPSHOT_MAX_BYTES:
                break
            if clock() > deadline:
                raise TimeoutError(f"camera snapshot took longer than {budget_s:.0f}s")
    image = b"".join(chunks)
    if len(image) > SNAPSHOT_MAX_BYTES or not image.startswith(bytes((0xff, 0xd8))):
        raise ValueError("camera response is not a bounded JPEG snapshot")
    return image


def _camera_snapshot_bytes():
    """One watchdog still, from the go2rtc fan-out whenever it is running.

    The #311 one-upstream rule: while timelapse capture or live viewing
    has go2rtc running, it holds the single camera pull, so the watchdog
    reads its still from go2rtc's loopback frame endpoint and never
    opens a second direct connection. When no fan-out feature is on,
    go2rtc is not running and the direct snapshot URL is the sole pull,
    exactly as before. A failed fan-out fetch falls back to the direct
    URL once — sequentially, never in parallel — so a broken stream can
    never degrade the watchdog (#311's fail-soft rule).

    The [services] camera_source pin narrows the choice (2.2.2; before,
    nothing read it): "none" raises — camera images are off, and every
    caller here already degrades a snapshot failure to a logged skip;
    "moonraker" uses the direct URL only; "go2rtc" uses the fan-out
    only and names it when it is not running; "bambu" names the
    mismatch — this listener serves Moonraker printers, which have no
    Bambu camera source.
    """
    pin = go2rtc.camera_source(_config.cfg())
    if pin == "none":
        raise ValueError("camera images are off (services.camera_source is none)")
    if pin == "bambu":
        raise ValueError("services.camera_source is bambu, but this printer's camera "
                         "is the Moonraker webcam; no Bambu camera source exists here")
    direct = _camera_url()
    if pin == "moonraker":
        return _fetch_snapshot(direct)
    if pin == "go2rtc":
        fanout = go2rtc.fanout_frame_url(_config.cfg())
        if not fanout:
            raise ValueError("services.camera_source is go2rtc, but the go2rtc fan-out "
                             "is not running (it needs timelapse capture or live "
                             "viewing on, the bundled binary and a resolvable source)")
        return _fetch_snapshot(fanout)
    try:
        fanout = go2rtc.fanout_frame_url(_config.cfg())
    except Exception:
        fanout = None
    if fanout:
        try:
            frame = _fetch_snapshot(fanout)
            if not _visual_watch.frame_dark(frame):
                return frame
            # #476: a black JPEG is a *successful* fetch, so the #424
            # failure fallback never fired on it — go2rtc serves black
            # frames before the stream produces real ones. A completely
            # dark fan-out frame falls back exactly like a failed fetch.
            log("visual watch: go2rtc fan-out snapshot is completely dark; "
                "falling back to the direct camera snapshot")
        except Exception:
            log("visual watch: go2rtc fan-out snapshot failed; "
                "falling back to the direct camera snapshot")
    return _fetch_snapshot(direct)


# The camera-watch timer beat (#913): fine enough for the 60 s empty-bed
# re-check and the 120 s early-layer interval, cheap when nothing is due.
VISUAL_TIMER_S = 15

# Camera and verdict I/O runs on its own small pool, never the default one:
# a hung camera or verdict endpoint can then only tie up these workers, and
# the listener's other thread work (announcer, posts, the heartbeat's
# debug poll) keeps its pool. Each whole visual check has an overall
# deadline; a check that overruns is abandoned, logged and counted, so the
# single-flight timer is free for the next beat.
WATCH_IO_WORKERS = 4
VISUAL_CHECK_DEADLINE_S = _camera_watch.CHECK_DEADLINE_S
_WATCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=WATCH_IO_WORKERS, thread_name_prefix="camera-watch")


async def _watch_io(fn, *args, **kwargs):
    """Run one blocking camera/verdict call on the camera-watch pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WATCH_EXECUTOR, functools.partial(fn, *args, **kwargs))


def _visual_verdict(image, prompt, streak=None, local_fallback=False, idle=False):
    """The shared verdict call (visual_watch.visual_verdict) with this listener's values.

    ``streak`` is the print watch's per-print local-detector escalation
    state (#455); only visual_check passes it — the auto light-off idle
    checks judge single frames, so they leave it None. ``local_fallback``
    is true inside a risky print's early-layer window (#913): a failed
    endpoint then falls back to the local detector when its model exists.
    ``idle`` marks the idle plate checks (auto light-off, next-plate
    offer): only they may be answered by the Stage 1 plate-reference
    producer — the in-print watch never passes it, so the in-print empty
    recheck stays endpoint-only.
    """
    return _visual_watch.visual_verdict(
        image, prompt, chat_url=_vision_chat_url, model=_VISION_MODEL,
        api_key=lambda: _profile_env_value("CLIPROXY_API_KEY"), context=_vision_context(),
        local_streak=streak, local_fallback=local_fallback, idle=idle)


def _record_watchdog_event(filename, verdict, *, streak=None, action="none",
                           recorder=None):
    """Record one print-watch verdict in the #981 events file.

    Best-effort beside the watch loop: the recorder
    (``watchdog_events.record_for_file``) never raises by contract, and
    this wrapper swallows even a broken injected recorder, so event
    recording can never break a watch check or a print.
    """
    try:
        if recorder is None:
            from . import watchdog_events
            recorder = watchdog_events.record_for_file
        return recorder(filename=filename, verdict=verdict, streak=streak,
                        action=action)
    except Exception as exc:
        log(f"watchdog event record failed file={filename!r}: {exc!r}")
        return False


def _auto_pause_early(filename, identity, printer_id=None):
    """Send PAUSE through the alert pause path (#837) for the early-layer auto-pause (#913).

    The same code the camera alert's Pause button runs: status readback,
    a same-file check, one /printer/print/pause, then a paused readback.
    It never cancels. Returns the pause path's reply text.

    The pause is bound to this listener's printer, as the alert record is
    (print_alert_buttons._visual_extra): without it a multi-printer roster
    refused the pause as an alert of unknown printer.
    """
    from . import print_alert_buttons as pab
    printer_id = printer_id or _watch_printer_id()
    alert = {"kind": "visual", "filename": filename, "identity": identity}
    if printer_id:
        alert["printer_id"] = str(printer_id)
    return pab.handle_click("pause", alert)


def _camera_missing_reason():
    """Why this Moonraker printer has no camera to watch with, in plain words; "" when it has one.

    Configuration only, no network: no snapshot URL and no go2rtc fan-out,
    or a [services] camera_source pin that rules the webcam out. A camera
    that is configured but fails is not "missing": its failed checks alert
    (throttled). Unreadable settings read as "" so the watch keeps its
    old behaviour.
    """
    try:
        data = _config.cfg()
        pin = go2rtc.camera_source(data)
        if pin == "none":
            return "camera images are off (services.camera_source is none)"
        if pin == "bambu":
            return ("services.camera_source is bambu, but this printer's camera is the "
                    "Moonraker webcam")
        if pin == "go2rtc" or _config.service_url("camera_snapshot_url"):
            return ""
        try:
            fanout = go2rtc.fanout_frame_url(data)
        except Exception:
            fanout = None
        return "" if fanout else "no camera snapshot URL is configured (services.camera_snapshot_url)"
    except Exception:
        return ""


def _watch_now():
    """The camera watch's wall clock (a seam: tests replace it)."""
    return time.time()


def _watch_printer_id():
    """This listener's roster printer, for the auto-pause evidence; None when unknown."""
    try:
        from .. import listeners as _listeners
        return _listeners.instance_printer_id()
    except Exception:
        return None


def _watch_coverage_now():
    """This printer's camera-watch coverage for the heartbeat (camera_watch.listener_coverage)."""
    return _camera_watch.listener_coverage(
        "moonraker", _config.cfg(), lambda: _profile_env_value("CLIPROXY_API_KEY"),
        camera_reason=_camera_missing_reason())


# One listener per process: its coverage, refreshed at most every
# camera_watch.COVERAGE_REFRESH_S from the heartbeat writer thread.
_WATCH_COVERAGE = _camera_watch.CoverageReporter(_watch_coverage_now)


def _vision_ready():
    """Whether the camera watch has a vision endpoint to judge frames with (visual_watch.vision_ready)."""
    return _listener_common._vision_ready(ns=_SELF)


# -- #516: auto light-off ---------------------------------------------------
# The pure policy (light_control.AutoLightPolicy) decides; this listener
# owns the I/O: the Moonraker object list for light discovery (the #442
# precedent — names come from the printer, never hardcoded), the existing
# _camera_snapshot_bytes + _visual_verdict path for idle plate checks,
# and print_control's G-code script endpoint for the switch itself.
# Switching happens in Moonraker, so Home Assistant's Moonraker
# integration sees the same light state — Print Handy never talks to HA
# for this. Every entry point below is fail-soft: an exception is
# caught, logged, and can never break announcements or the watchdog.

_AUTO_LIGHT_PROMPT = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"
_AUTO_LIGHT_IDLE_STATES = ("complete", "cancelled", "error", "standby")


def _light_object_names():
    """Moonraker object names for light discovery; None when the list is unreadable."""
    try:
        listing = print_control._call("/printer/objects/list")
    except Exception as exc:
        log(f"auto light: Moonraker object list failed: {exc!r}")
        return None
    objects = ((listing.get("result") or {}).get("objects")
               if isinstance(listing, dict) else None)
    return objects if isinstance(objects, list) else None


def _send_light_script(script):
    """Send one light G-code script to Moonraker. Raises on failure; callers log."""
    print_control._send("/printer/gcode/script", method="POST",
                        data={"script": script}, timeout=15)


_DARK_WORD = re.compile(r"\b(?:dark|black)\b", re.IGNORECASE)
_DARK_SUBJECT = re.compile(r"\b(?:camera|feed|image|frame|picture|view|scene|see|visible|light|lighting)\b",
                           re.IGNORECASE)


def _verdict_says_dark(verdict, detail):
    """True when an uncertain verdict blames a dark camera feed (#476), not the print."""
    detail = str(detail or "")
    return verdict == "uncertain" and bool(_DARK_WORD.search(detail) and _DARK_SUBJECT.search(detail))


def _visual_layer(stats):
    info = stats.get("info") if isinstance(stats, dict) else None
    if not isinstance(info, dict):
        return None
    value = info.get("current_layer") or info.get("layer")
    return value if isinstance(value, int) else None


def _print_elapsed(stats):
    """Printing time so far (print_stats.print_duration), or None when unknown.

    Klipper counts print_duration only from the first extrusion, so heat-up
    and pauses are excluded: the clock the layerless camera watch runs on.
    """
    value = stats.get("print_duration") if isinstance(stats, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _layerless_notice_text(filename, periodic_s, early):
    """The once-per-print notice that the camera watch runs on a timer (no layer info)."""
    minutes = max(1, round(periodic_s / 60))
    cadence = f"about every {minutes} minutes"
    if early:
        cadence += ", more often during the early part of this risky print"
    return _with_printer_name(
        f"Camera watch: {clean(filename)} reports no layer numbers (it was sliced without "
        "layer info), so I'm checking the camera on a timer instead, "
        f"{cadence}. Stall detection needs layer numbers and is off for this print.")


def _with_printer_name(text):
    """Append the serving printer's name on a multi-printer roster (#536).

    The watchdog texts are built here, not via format_event, so they take
    the same trailing tag explicitly; a one-printer install is unchanged.
    """
    name = print_summary.announcement_printer_name()
    return f"{text}\nPrinter: {name}" if name else text


@_web_scoped
def _visual_alert(filename, image, reason, identity=None, ping=True):
    # #317: a watchdog alert is blooper material; record() never raises.
    print_markers.record(filename, print_markers.WATCHDOG_ALERT, detail=reason)
    try:
        from ..notify import emit_queued
        emit_queued("watchdog_offer", reason)
    except Exception:
        pass
    # The shared frame folder keeps only the newest frames (visual_watch.prune_frames).
    path = _visual_watch.write_frame(filename, image)
    text = _with_printer_name(f"Print camera check: {clean(filename)}\n{reason}")
    _web_alert(text + f"\nMEDIA:{path}", "visual", filename, identity=identity)
    # A watchdog alert asking for a decision pings the starter (#1095),
    # once per episode: the decision is made once here and threaded to
    # whichever transport delivers, button post or plain fallback.
    # ``ping`` False (a weak, below-threshold detection) posts for a look
    # without asking the classifier, so it never spends the episode's ping.
    mention = (starter_pings.ping_for("watchdog", job_key=filename, episode="watchdog") or None
               if ping else None)
    if _buttonless_standalone():
        delivered = post(text + f"\n{path}\nMEDIA:{path}",
                         **({"mention_user_id": mention} if mention else {}))
        if mention and not delivered:
            starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
        return path
    try:
        from . import print_alert_buttons as pab
        kwargs = {"mention_user_id": mention} if mention else {}
        if pab.post_visual_alert(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 text, filename, path, identity=identity, **kwargs):
            log(f"post visual alert {clean(filename)!r}")
            return path
        log("visual button post failed; falling back to text")
    except Exception as exc:
        log(f"visual button post error: {exc!r}")
    delivered = post(text + f"\n{path}\nMEDIA:{path}",
                     **({"mention_user_id": mention} if mention else {}))
    if mention and not delivered:
        # Every delivery attempt failed, so the recorded ping never
        # landed: release it, and a later alert can ping (post_alert's
        # rule, #1095).
        starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
    return path


@_web_scoped
def _visual_alert_text(filename, reason, identity=None):
    """A watchdog alert with no frame to attach: the snapshot itself failed.

    The watchdog fired but has no picture to show — the camera is
    unavailable, malformed, or unconfigured. Same bookkeeping as
    _visual_alert (the #317 marker and the notify emit); delivery is
    text with the visual alert's buttons on Discord, plain text on
    buttonless standalone platforms.
    """
    print_markers.record(filename, print_markers.WATCHDOG_ALERT, detail=reason)
    try:
        from ..notify import emit_queued
        emit_queued("watchdog_offer", reason)
    except Exception:
        pass
    text = _with_printer_name(f"Print camera check: {clean(filename)}\n{reason}")
    _web_alert(text, "visual", filename, identity=identity)
    mention = starter_pings.ping_for("watchdog", job_key=filename, episode="watchdog") or None
    if _buttonless_standalone():
        delivered = post(text, **({"mention_user_id": mention} if mention else {}))
        if mention and not delivered:
            starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")
        return
    try:
        from . import print_alert_buttons as pab
        kwargs = {"mention_user_id": mention} if mention else {}
        if pab.post_visual_text_alert(_bot_token(), announcer.DISCORD_CHAT_ID,
                                      text, filename, identity=identity, **kwargs):
            log(f"post visual text alert {clean(filename)!r}")
            return
        log("visual text alert button post failed; falling back to text")
    except Exception as exc:
        log(f"visual text alert post error: {exc!r}")
    delivered = post(text, **({"mention_user_id": mention} if mention else {}))
    if mention and not delivered:
        # Every delivery attempt failed: release the recorded ping, as
        # post_alert does, so a later alert can ping (#1095).
        starter_pings.release_ping("watchdog", job_key=filename, episode="watchdog")


@_web_scoped
def _visual_dark_notice(filename, identity=None):
    """The once-per-print dark-camera notice (#476): a camera problem, not a print alert.

    Plain text on buttonless standalone platforms; on Discord the same text
    with only the "Stop asking for this print" button, via the alert-button
    transport. No image is attached — the frame is black.
    """
    text = _with_printer_name(
        f"Camera check: {clean(filename)} — the camera image is completely dark, "
        "so I can't check the print. Check the camera or its light. "
        "Monitoring continues; nothing was sent to the printer.")
    _web_alert(text, "visual-dark", filename, identity=identity)
    if _buttonless_standalone():
        post(text)
        return
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_dark_notice(_bot_token(), announcer.DISCORD_CHAT_ID,
                                       text, filename, identity=identity):
            log(f"post visual dark notice {clean(filename)!r}")
            return
        log("visual dark button post failed; falling back to text")
    except Exception as exc:
        log(f"visual dark notice post error: {exc!r}")
    post(text)


def _slot_bools(slots):
    out = {}
    for key, value in (slots or {}).items():
        try:
            slot = int(key)
        except (TypeError, ValueError):
            continue
        if slot in (1, 2, 3, 4) and isinstance(value, bool):
            out[slot] = value
    return out


class FailoverChannelWatch:
    """Notices Zmod Infinity switching the feeding IFS channel mid-print (debounced).

    Infinity mode loads "a similar rod" from another slot when one runs dry, with
    no pause. Moonraker keeps charging the spool it was pointed at, so usage lands
    on the empty spool. A channel change only counts once seen on two polls in a row.
    """

    def __init__(self, stable_samples=2):
        self.stable_samples = stable_samples
        self.reset()

    def reset(self):
        self.channel = None
        self.candidate = None
        self.count = 0

    def observe(self, channel):
        """Returns (old, new) once a different channel is stable, else None."""
        if channel is None:
            return None
        if self.channel is None:
            self.channel = channel
            return None
        if channel == self.channel:
            self.candidate, self.count = None, 0
            return None
        if channel == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = channel, 1
        if self.count < self.stable_samples:
            return None
        old, self.channel = self.channel, channel
        self.candidate, self.count = None, 0
        return old, channel

    def retry(self, change):
        """Undo a reported change whose usage update failed, so a later poll reports it again.

        The change is only consumed once usage is reconciled (review of #878):
        a transient Moonraker error must not leave the empty spool charged.
        """
        old, new = change
        if self.channel == new:
            self.channel, self.candidate, self.count = old, None, 0


def failover_usage_target(old_slot, new_slot, charged_spool, mapped):
    """The spool to point usage at after a failover, or None to leave usage alone.

    Only a single-spool job moves: Moonraker must be charging exactly the old
    slot's spool. A multi-slot job charges no spool, and channel changes there are
    ordinary tool changes. The new slot needs its own mapped spool.
    """
    old_spool, new_spool = mapped(old_slot), mapped(new_slot)
    if charged_spool is None or old_spool is None or new_spool is None:
        return None
    if int(charged_spool) != int(old_spool) or int(new_spool) == int(old_spool):
        return None
    return int(new_spool)


class MidPrintSlotTracker:
    """Debounced per-slot presence during a print. Reports emptied and refilled slots."""

    def __init__(self, stable_samples=2):
        self.stable_samples = stable_samples
        self.reset()

    def reset(self):
        self.confirmed = {}
        self.candidate = {}
        self.counts = {}
        self.emptied = set()
        self.seen_loaded = set()
        self.runout_asked = set()

    def runout_question(self, slot):
        """A runout question covers this slot: refilling it answers that question, it is not a swap."""
        if slot:
            self.runout_asked.add(slot)

    def observe(self, slots):
        events = []
        for slot, present in _slot_bools(slots).items():
            if present:
                self.seen_loaded.add(slot)
            if self.candidate.get(slot) == present:
                self.counts[slot] = self.counts.get(slot, 0) + 1
            else:
                self.candidate[slot], self.counts[slot] = present, 1
            if self.counts[slot] < self.stable_samples or self.confirmed.get(slot) == present:
                continue
            before = self.confirmed.get(slot)
            self.confirmed[slot] = present
            if before is True and present is False:
                self.emptied.add(slot)
                events.append(("emptied", slot))
            elif before is False and present is True and slot in self.emptied:
                self.emptied.discard(slot)
                events.append(("reloaded" if slot in self.runout_asked else "refilled", slot))
                self.runout_asked.discard(slot)
        return events

    def empty_slots(self, slots=None):
        # Only a slot seen loaded this print can have just run out; a never-loaded slot is not a runout.
        live = {s for s, p in _slot_bools(slots).items()
                if p is False and s in self.seen_loaded and self.confirmed.get(s) is not False}
        return sorted(self.emptied | live)


def _mapped_spool(slot):
    try:
        state = filament_color.adopt_renamed_slots_key(json.loads(_hardware_file().read_text()))
    except (OSError, ValueError):
        return None
    return ((state.get("filament_slots") or {}).get(str(slot)) or {}).get("spoolman_id")


def _maintenance_history_totals():
    """The printer's lifetime print total in HOURS.

    /server/history/totals reports total_time/print_time in seconds;
    maintenance records and thresholds are in hours, so convert here —
    raw seconds read as thousands of hours and retrigger reminders
    immediately after every maintenance run.
    """
    payload = print_control._call('/server/history/totals')
    result = payload.get('result') or {}
    return float(result.get('total_time') or result.get('print_time') or 0.0) / 3600.0


async def maintenance_reminder_tick(listener):
    """Post due maintenance only when an explicit idle state is known."""
    from . import maintenance
    try:
        st = await asyncio.to_thread(print_control.status)
        if st.get('state') != 'standby':
            return
        hours = await asyncio.to_thread(_maintenance_history_totals)
        state = maintenance.load()
        message = maintenance.reminder_text(state, hours, idle=True)
        if message:
            signature = message.partition('(')[0]
            if state.get('last_reminder') != signature:
                if await asyncio.to_thread(post, message):
                    state['last_reminder'] = signature
                    maintenance.save(state)
    except Exception as exc:
        log(f"maintenance reminder check failed: {exc!r}")


async def maintenance_cancel_reminder():
    try:
        st = await asyncio.to_thread(print_control.status)
        if st.get('state') == 'standby':
            await asyncio.to_thread(post, 'A print was cancelled. When the printer is idle, check the filament path and consider a maintenance quick check.')
    except Exception as exc:
        log(f"cancel maintenance reminder failed: {exc!r}")


NATIVE_ERROR_HINTS = {
    "E0102": "the filament changer could not feed that slot to the head (tangle, chewed or bent tip, or tube not seated)",
}


def native_error_key(detail):
    """(code, slot) when the stock screen reports an error, else None. The screen can halt a
    print while Klipper still says printing, so Moonraker alone never notices."""
    code = str((detail or {}).get("errorCode") or "").strip()
    if not code and (detail or {}).get("status") != "error":
        return None
    slot = ((detail or {}).get("matlStationInfo") or {}).get("currentLoadSlot")
    return (code or "unknown", slot)


def native_error_text(filename, key):
    code, slot = key
    where = f" on {filament_color.slot_display(slot)}" if slot else ""
    hint = NATIVE_ERROR_HINTS.get(code, "see the printer screen for details")
    link = f"https://wiki.flashforge.com/en/ad5x/error_code_list_ad5x" if code.startswith("E") else ""
    return (f"**Print halted by the printer: {code}{where}.**\n" + print_summary.format_event("error", filename)
            + f"\n**Why:** {hint}.\nKlipper still shows printing, so nothing else will alert. Fix it, then press "
              "retry on the touchscreen." + (f"\n{link}" if link else ""))


def load_native_error_alerted():
    """The stock-screen error last alerted, persisted so a restart does not post it again."""
    key = read_json_object(NATIVE_ERROR_STATE).get("key")
    return tuple(key) if isinstance(key, list) and len(key) == 2 else None


def save_native_error_alerted(key):
    try:
        atomic_write_json(NATIVE_ERROR_STATE, {"key": list(key) if key else None})
    except OSError as exc:
        log(f"native error state not saved: {exc!r}")


def zmod_display_off(status):
    """ZMod save_variables display_off (0 stock screen, 1 screenless), or None when unknown."""
    variables = ((status or {}).get("save_variables") or {}).get("variables")
    mode = variables.get("display_off") if isinstance(variables, dict) else None
    return mode if type(mode) is int and mode in (0, 1) else None


def zmod_active_channel(status):
    """The IFS channel Klipper reports as feeding (zmod_ifs.Chan, else zmod_color.channel), or None.

    2026-11-04: a screenless AD5X ran slot 3 dry with no launch record and no saved active slot,
    and every slot still read loaded (the tail was in the tube), so the runout named no slot.
    Klipper reported Chan 3 the whole time. Only 1..4 count; 0 means nothing is loaded.
    """
    if not isinstance(status, dict):
        return None
    for obj, key in (("zmod_ifs", "Chan"), ("zmod_color", "channel")):
        value = (status.get(obj) or {}).get(key) if isinstance(status.get(obj), dict) else None
        if type(value) is int and 1 <= value <= 4:
            return value
    return None


def zmod_slot_presence(status):
    """{slot: bool | None} from Klipper's zmod_color (screenless presence); see
    printers.flashforge.zmod.stock.read_zmod_color_slots, which mapped launch shares."""
    from ..printers.flashforge.zmod.stock import read_zmod_color_slots
    return read_zmod_color_slots(status)


class Listener:
    def __init__(self):
        self.state = None
        self.filename = ""
        # #1020 A3: the heartbeat is written by a dedicated thread, not by
        # the event loop, and carries loop_lag_ms measured from the loop's
        # own tick — a blocked loop then shows as a growing lag in a fresh
        # heartbeat instead of silently stopping the beats.
        self._loop_tick = time.time()
        self._heartbeat_thread = None
        self.pause_message = ""
        self.ifs_monitor = FilamentInsertionMonitor()
        self.spoolman_watcher = SpoolmanNewSpoolWatcher()
        self.lane_watcher = lane_data_sync.LaneDataWatcher(
            LANE_DATA_STATE,
            runout_context_fn=load_runout_pause,
            runout_feed_fn=lambda slot, spool_id, context:
                feed_screen_runout_load(slot, spool_id, context),
            presence_fn=self.lane_presence)
        self.slot_tracker = MidPrintSlotTracker()
        self.ifs_channel = None
        self.failover_watch = FailoverChannelWatch()
        self.visual_watch = VisualPrintWatch()
        # #455: this printer's local-detector temporal escalation state;
        # reset wherever visual_watch is (new file, fresh start, print end).
        self.local_streak = _local_vision.FailureStreak()
        self.last_stats = {}
        self.last_visual_task = None
        self.visual_frames = {}
        self.last_slots = {}
        self.last_slots_at = None  # monotonic time of the last successful slot read
        self.last_native_poll = 0.0
        self.native_poll_inflight = False
        self.prev_native = None
        self.screen_reboot_task = None
        self.background_tasks = set()  # strong refs: an unreferenced task can be collected mid-run
        self.maintenance_last_check = 0.0
        self.clock_watch = printer_clock.PrinterClockWatch(
            lambda: printer_clock.read_clock_skew(_config.moonraker_url(), api_key=announcer.API_KEY))
        self.abandoned_watch = AbandonedStartWatch()  # #836: claimed starts that never print
        self.last_clock_check = 0.0
        self.clock_sync_retry_at = 0.0
        self.last_clock_sync = 0.0
        self.last_pause_was_runout = False
        self.last_pause_episode = None  # the starter-pings episode of the live pause's alert
        self.pause_context = None
        # The last runout of this print: its slot, its alert message (Discord
        # channel and message ids, when known), when it was resumed, and how
        # often it paused again since. None outside a runout.
        self.runout_episode = None
        self.native_error_alerted = load_native_error_alerted()
        self.display_off = None  # live ZMod display mode from the last presence poll; None until read
        self.presence_failure = None  # last presence poll failure, logged once until it changes
        self.capture_session = None  # #246 stage 2: active timelapse_capture session, if any
        self._capture_generation = 0  # bumped per capture begin/end; a stale begin installs nothing (#1020)
        self._capture_task = None  # in-flight scheduled capture fetch (P2-133)
        self.shutdown_alerted = None  # filename whose firmware-stop alert fired for THIS attempt (P2-57)
        self.print_identity = None  # this print's identity, fixed at its fresh start
        self._auto_light = None  # #516: AutoLightPolicy, built lazily from [vision]
        self._light_idle_task = None  # in-flight scheduled idle light check (P2-200)
        self.light_target = None  # #516: resolved Moonraker light object, cached
        self.light_target_resolved = False
        self.light_unresolved_logged = False

    def note_slot_presence(self, slots):
        """Remember a successful slot presence read and when it was taken."""
        self.last_slots = slots
        self.last_slots_at = time.monotonic()

    def lane_presence(self):
        """This tick's slot presence for the lane_data watcher (#852), or None.

        poll_native_presence reads the slots just before poll_lane_data on
        the same tick, so a read younger than one poll interval is current;
        anything older means this tick's read failed (or never ran) and the
        watcher must treat presence as unknown, not reuse a stale answer.
        """
        if self.last_slots_at is None or time.monotonic() - self.last_slots_at >= NATIVE_POLL_SECONDS:
            return None
        return self.last_slots

    async def read_slots(self):
        """(stock /detail or None, {slot: present}). Screenless ZMod has no stock API; Klipper has the slots."""
        from print_handy.core import printer_state
        from print_handy.core import model_job
        reply = await asyncio.to_thread(print_control._call, "/printer/objects/query?zmod_color&zmod_ifs&save_variables")
        status = (reply.get("result") or {}).get("status") or {}
        self.ifs_channel = zmod_active_channel(status)
        self.display_off = zmod_display_off(status)
        if self.display_off == 1:
            return None, zmod_slot_presence(status)
        detail = await asyncio.to_thread(model_job.read_stock_detail, printer_state.load())
        return detail, model_job.read_stock_filament_slots_from_detail(detail)

    async def open_question(self, slot, cause):
        """Stop charging every spool, remember the question, and ask once. Nag loop repeats it."""
        if slot is None:
            # The evidence names no IFS slot (a slotless printer, or a
            # runout no slot read could place). A persisted slot question
            # could never be answered with a spool choice here —
            # apply_runout_choice refuses a slotless record outright —
            # while suspending usage would stop charging EVERY spool
            # until that unanswerable question is answered (P2-170).
            # Post one plain notice and leave usage running instead.
            log(f"spool question skipped: cause={cause} names no slot; usage left running")
            await asyncio.to_thread(
                post,
                f"**Filament check:** the filament for {clean(self.filename or 'this print')} "
                f"{'ran out' if cause == 'runout' else 'was unloaded and reloaded mid-print'}, "
                "but no IFS slot could be identified, so there is no spool "
                "question to answer and filament usage has **not** been paused. If you "
                "loaded a different spool, point usage at it with `/spool` so the "
                "amounts stay right.")
            return None
        if load_runout_pause() is not None:
            return None
        from . import spool_sync
        try:
            suspended = await asyncio.to_thread(spool_sync.suspend_usage)
            note = ""
        except Exception as exc:
            suspended, note = None, f"\n⚠️ Could not pause filament usage ({exc}); the old spool may keep being charged."
            log(f"usage suspend failed: {exc!r}")
        record = open_spool_question(self.filename, slot, _mapped_spool(slot) if slot else None,
                                     cause, suspended)
        log(f"spool question opened: cause={cause} slot={slot} suspended_spool={suspended}")
        if cause == "runout":
            self.slot_tracker.runout_question(slot)
        await asyncio.to_thread(post_spool_question, record)
        return record

    async def poll_printer_clock(self):
        """Post once when a rebooted printer's clock is still wrong, then once when it syncs (#807).

        The watch itself is read-only and warn-only: it never sets the
        clock and never blocks anything. Fail-soft like the spool poll
        beside it — an unreadable clock posts nothing and changes
        nothing, and the caller logs and continues on any error.
        """
        messages = await asyncio.to_thread(self.clock_watch.poll)
        for message in messages:
            log(f"printer clock watch: {message.splitlines()[0][:160]}")
            if await asyncio.to_thread(post, message):
                await asyncio.to_thread(self.clock_watch.delivered, message)
            else:
                log("printer clock watch message not delivered; the watch will offer it again")

    async def poll_abandoned_starts(self):
        """Report a claimed start that never became a print (#836).

        A start the printer accepted can still be abandoned during preparation
        (heat, IFS load fault, back to standby with cold heaters): the start
        card would otherwise say "Print started, warming up" forever. For each
        claimed file, read print_stats and the heater targets; the watch fires
        once after ABANDONED_START_COLD_S of standby/error with both targets 0,
        and then the one start card is edited and the claim released. Each
        outstanding claim is watched with its own cold window (#848), and
        windows for claims that are no longer outstanding are retired first.
        Fail-soft like the polls beside it: an unreadable printer changes
        nothing.
        """
        claims = claimed_start_filenames()
        self.abandoned_watch.retain(claims)
        for filename in claims:
            try:
                reply = await asyncio.to_thread(
                    print_control._call, "/printer/objects/query?print_stats&extruder&heater_bed")
                status = (reply.get("result") or {}).get("status") or {}
            except Exception as exc:
                log(f"abandoned start poll failed file={filename!r}: {exc!r}")
                continue
            stats = status.get("print_stats") or {}
            reported = stats.get("filename") or ""
            if reported and not launch_state.same_remote(reported, filename):
                continue  # the printer is reporting another file; this claim is not its story
            def target(name):
                value = (status.get(name) or {}).get("target")
                return float(value) if isinstance(value, (int, float)) else None
            if self.abandoned_watch.observe(filename, stats.get("state"),
                                            target("extruder"), target("heater_bed"),
                                            claimed_at=start_claim_time(filename)):
                log(f"claimed start never became a print file={filename!r}")
                await asyncio.to_thread(announce_start_abandoned, filename)
                await asyncio.to_thread(release_start_claim, filename)
                self.abandoned_watch.retire(filename)
                await asyncio.to_thread(self._release_abandoned_lease, filename)

    def _release_abandoned_lease(self, filename):
        """The printer gave up on this start: free the printer for the next one (#848 review)."""
        from .. import listeners as _listeners
        try:
            row = launch_state.release_abandoned(_listeners.instance_printer_id(), filename)
            if row:
                log(f"launch lease released: the printer abandoned {filename!r} without printing")
        except Exception as exc:
            log(f"abandoned launch lease release failed file={filename!r}: {exc!r}")

    async def poll_spoolman_new_spools(self):
        """Prompt once per spool created in Spoolman outside Print Handy (#483).

        Runs on the native-presence poll's cadence. Fail-soft: the watcher
        reports nothing when Spoolman does not answer, and the caller logs
        and continues on any error — this poll never stalls the loop.
        """
        due = await asyncio.to_thread(self.spoolman_watcher.poll)
        for spool in due:
            if await asyncio.to_thread(post_new_spool_prompt, spool):
                await asyncio.to_thread(self.spoolman_watcher.delivered, spool["id"])
                log(f"spoolman watcher prompted for new spool #{spool.get('id')}")
            else:
                log(f"spoolman watcher could not deliver the prompt for spool #{spool.get('id')}; will retry")

    async def poll_lane_data(self):
        """Announce and converge HelixScreen-initiated spool changes (#791).

        Runs on the native-presence poll's cadence, beside the #483
        Spoolman watcher — no new connection: the watcher reads the
        Moonraker ``lane_data`` namespace through this printer's existing
        Moonraker client. Fail-soft like the watcher beside it: an
        unreachable or absent namespace reads as unknown and changes
        nothing, and any error is logged by the caller, never raised.
        """
        from . import spool_sync
        from .. import listeners as _listeners
        instance_id = _listeners.instance_printer_id()
        if instance_id:
            self.lane_watcher.mr = spool_sync.moonraker_for(instance_id)
            self.lane_watcher.printer_id = instance_id
        due = await asyncio.to_thread(self.lane_watcher.poll)
        for event in due:
            if await asyncio.to_thread(post_lane_event, event):
                await asyncio.to_thread(self.lane_watcher.delivered, event["key"])
                log(f"lane_data watcher announced {event.get('kind')} for slot {event.get('slot')}")
            else:
                log(f"lane_data watcher could not deliver the {event.get('kind')} announcement "
                    f"for slot {event.get('slot')}; will retry")

    async def nag_if_due(self):
        record = load_runout_pause()
        if not record or not record.get("usage_suspended"):
            return
        # The paused print itself keeps the short reminder. Once the print the
        # question was opened for is over, reminders back off (2026-11-12).
        interval = SPOOL_NAG_SECONDS
        stats = getattr(self, "last_stats", None) or {}
        if record.get("print_over") or not spool_question_print_is_current(
                record, getattr(self, "state", None), getattr(self, "filename", ""),
                stats.get("total_duration")):
            interval = max(SPOOL_NAG_SECONDS, SPOOL_NAG_ENDED_SECONDS)
        if time.time() - float(record.get("last_nag_at") or 0) < interval:
            return
        record["nags"] = int(record.get("nags") or 0) + 1
        record["last_nag_at"] = time.time()
        atomic_write_json(RUNOUT_PAUSE_STATE, record)
        await asyncio.to_thread(post_spool_question, record, True)

    async def _slot_present_now(self, slot):
        """True/False for whether an IFS slot reads loaded now; None when it cannot be read.

        This tick's presence poll answers when it is current; otherwise one
        slot read is taken (native-screen printers only). Fail-soft.
        """
        slots = self.lane_presence()
        if slots is None:
            if not printers.current().has_native_screen:
                return None
            try:
                _detail, slots = await self.read_slots()
            except Exception as exc:
                log(f"spool question slot read failed: {exc!r}")
                return None
            self.note_slot_presence(slots)
        value = (slots or {}).get(slot) if isinstance(slots, dict) else None
        return value if isinstance(value, bool) else None

    async def settle_spool_question(self, *, state, filename, total_duration=None,
                                    new_print=False, printing=False, start_notice=False, mr=None):
        """Settle an open spool question whose print is over (2026-11-12).

        Runs on a fresh print start and after each spool reconcile. The
        print the question was opened for is left alone: during it the
        question behaves exactly as before. Once that print is over:

        * the #1063 refeed rule closes it when the slot is still bound to
          the very spool that ran out and reads loaded: usage resumes on
          that spool through the same refeed path a screen load takes and
          one short line says so. While another print is running, its
          launch owns Moonraker's usage pointer, so the question is only
          closed and the pointer is left as the launch set it;
        * otherwise the question stays open, is marked as outliving its
          print (reminders back off), and with ``start_notice`` the new
          print gets one notice that usage is paused. Warn only: nothing
          here holds or stops a print.

        Returns "settled", "open", or None when there was nothing to do.
        """
        record = load_runout_pause()
        if not record or not record.get("usage_suspended"):
            return None
        if not new_print and spool_question_print_is_current(record, state, filename, total_duration):
            return None
        slot = record.get("slot")
        bound = _mapped_spool(slot) if slot in (1, 2, 3, 4) else None
        if refeed_settles_question(record, bound, True) and refeed_settles_question(
                record, bound, await self._slot_present_now(slot)):
            try:
                if printing:
                    clear_runout_pause()
                else:
                    await asyncio.to_thread(
                        functools.partial(feed_screen_runout_load, slot, bound, record,
                                          **({"mr": mr} if mr is not None else {})))
            except Exception as exc:
                log(f"spool question refeed settle failed: {exc!r}")
            else:
                log(f"spool question closed as a refeed: slot {slot} is back on spool #{bound}")
                await asyncio.to_thread(post, refeed_settled_text(record))
                return "settled"
        if not record.get("print_over"):
            record["print_over"] = True
            atomic_write_json(RUNOUT_PAUSE_STATE, record)
        if start_notice:
            await asyncio.to_thread(post, open_question_start_notice(record))
        return "open"

    async def poll_native_presence(self):
        """Read authenticated stock detail while idle and send only a one-way prompt."""
        if not printers.current().has_native_screen:
            return  # no vendor status API, no IFS presence to watch
        if self.native_poll_inflight:
            return
        self.native_poll_inflight = True
        failure = None
        # This tick's presence is unknown until its read succeeds: a failed read must never
        # leave the previous tick's answer looking current to the lane watcher (#852).
        self.last_slots_at = None
        try:
            detail, slots = await self.read_slots()
            self.note_slot_presence(slots)
            # Screenless ZMod (detail None): the stock firmware is not running, so there is no
            # stock status to watch for screen cancels or screen errors; slot presence still is.
            native = detail.get("status") if detail is not None else None
            if native_screen_cancel(self.prev_native, native):
                # Klipper shut down mid print, so Moonraker never sees the screen cancel. The stock firmware does.
                if (await asyncio.to_thread(print_control.status)).get("klippy") == "shutdown":
                    reason = await asyncio.to_thread(capture_stop, self.filename, "cancel")
                    await asyncio.to_thread(post_cancel_report, self.filename, reason)
                    self.start_screen_cancel_reboot(f"native {self.prev_native}->{native} during emergency stop")
            self.prev_native = native
            if self.state in ("printing", "paused"):
                key = native_error_key(detail) if detail is not None else None
                if key and key != self.native_error_alerted:
                    self.native_error_alerted = key
                    save_native_error_alerted(key)
                    log(f"native screen error {key} file={self.filename!r}")
                    episode = f"native-error:{key[0]}:{key[1]}"
                    mention = starter_pings.ping_for(
                        "alert", job_key=self.filename, episode=episode) or None
                    delivered = await asyncio.to_thread(
                        post, native_error_text(self.filename, key),
                        **({"mention_user_id": mention} if mention else {}))
                    if not delivered and mention:
                        # The post failed, so the recorded ping never landed:
                        # release it, as post_alert does, so the next post of
                        # this error can still ping the starter.
                        starter_pings.release_ping("alert", job_key=self.filename, episode=episode)
                elif not key and detail is not None and self.native_error_alerted is not None:
                    self.native_error_alerted = None
                    save_native_error_alerted(None)
                if self.state == "printing":
                    await self.follow_failover(self.failover_watch.observe(self.ifs_channel))
                for event, slot in self.slot_tracker.observe(slots):
                    log(f"mid-print slot {slot} {event}")
                    if event == "refilled":
                        # Unloaded and reloaded mid print: unknown spool. A slot whose runout
                        # question was asked reports "reloaded" instead: that question covers it.
                        await self.open_question(slot, "swap")
                    elif (event == "emptied" and self.state == "paused"
                            and not self.last_pause_was_runout):
                        # The slot emptied while paused and the pause was not
                        # classified as a runout: upgrade it instead of
                        # dropping the event at a log line (#442).
                        await self.upgrade_paused_runout(slot)
            # Only exact idle state qualifies. Unknown/preparing/printing all suppress prompts.
            # Screenless, Klipper's standby is the only idle signal there is.
            idle = (detail is None or detail.get("status") == "ready") and self.state == "standby"
            found = self.ifs_monitor.observe(slots, idle=idle, connected=True)
            for slot in found:
                await asyncio.to_thread(post_insertion_confirmation, slot)
                log(f"native filament presence transition prompted for slot {slot}")
            from . import print_alert_buttons as pab
            self.ifs_monitor.prune_pending(slots, await asyncio.to_thread(pab.open_insertion_slots))
        except Exception as exc:
            failure = f"{exc!r}"
            self.ifs_monitor.observe({}, idle=False, connected=False)
        finally:
            self.native_poll_inflight = False
        # One line per distinct failure (and one on recovery), logged outside the handler so no
        # traceback rides along: this poll runs every NATIVE_POLL_SECONDS.
        if failure and failure != self.presence_failure:
            log(f"native filament presence poll failed: {failure}")
        elif not failure and self.presence_failure:
            log("native filament presence poll recovered")
        self.presence_failure = failure

    def failover_retry(self, change):
        watch = getattr(self, "failover_watch", None)
        if watch is not None:
            watch.retry(change)

    @property
    def failover_failures_posted(self):
        posted = self.__dict__.get("_failover_failures_posted")
        if posted is None:
            posted = self.__dict__["_failover_failures_posted"] = set()
        return posted

    async def follow_failover(self, change):
        """Point usage at the replacement spool when Infinity switched channels on a one-spool job."""
        if not change:
            return
        old, new = change
        from . import spool_sync
        try:
            charged = await asyncio.to_thread(spool_sync.active_usage_spool)
        except Exception as exc:
            log(f"failover: active spool read failed, retrying on a later poll: {exc!r}")
            self.failover_retry(change)
            return
        target = failover_usage_target(old, new, charged, _mapped_spool)
        log(f"failover: IFS channel {old}->{new} charged={charged} target={target}")
        if target is None:
            return
        try:
            await asyncio.to_thread(spool_sync.resume_usage, target)
        except Exception as exc:
            log(f"failover: usage repoint failed, retrying on a later poll: {exc!r}")
            self.failover_retry(change)
            if change in self.failover_failures_posted:
                return
            self.failover_failures_posted.add(change)
            await asyncio.to_thread(post, (
                f"**Filament failover:** the printer switched from {filament_color.slot_display(old)} "
                f"to {filament_color.slot_display(new)} "
                f"for {clean(self.filename or 'this print')}, but filament usage could not be moved to "
                f"spool #{target} ({exc}). Spool #{charged} may keep being charged."))
            return
        await asyncio.to_thread(post, (
            f"**Filament failover:** {filament_color.slot_display(old)} ran out and the printer switched "
            f"to {filament_color.slot_display(new)} on its own. "
            f"Filament usage now goes to spool #{target} (was #{charged})."))

    async def classify_runout(self):
        """Pause cause from the alert text, the head sensor, and a fresh slot read.

        Returns (is_runout, slot). Klipper's macro-driven runout pause leaves
        print_stats.message empty (#442), so the message alone is not enough:
        the printer's own filament sensor (discovered from its Moonraker
        object list, any printer) and the IFS slot presence (native-screen
        printers) are read as well. A sensor or slot read that fails or is
        absent contributes no evidence — the pause stays generic.
        """
        empty = []
        present = {}
        slot = None
        if printers.current().has_native_screen:
            slots = self.last_slots
            try:
                _detail, slots = await self.read_slots()
                self.note_slot_presence(slots)
                self.slot_tracker.observe(slots)
            except Exception as exc:
                log(f"pause slot read failed: {exc!r}")
            empty = self.slot_tracker.empty_slots(slots)
            present = _slot_bools(slots)
            slot = attribute_runout_slot(self.slot_tracker.emptied, empty,
                                         launch_mapped_slots(self.filename),
                                         recovery_slot(self.filename)[0])
            if slot is None:
                # Nothing else named it: the channel Klipper says is feeding is the one that ran dry.
                slot = self.ifs_channel
        head_has_filament = await asyncio.to_thread(read_head_sensor)
        if slot is None and head_has_filament is False:
            # The head sensor names no slot; the job's own slot is the one that fed it.
            slot = recovery_slot(self.filename)[0]
        is_runout = pause_is_runout(self.pause_message, slot, present, head_has_filament) or bool(empty)
        return is_runout, slot

    def _jam_facts(self):
        """Measured facts for the jam post (#1020 E2). Read-only, best-effort:
        any source that fails to read contributes 'not reported'."""
        facts = {"duration_s": self.last_stats.get("print_duration"),
                 "layer": _visual_layer(self.last_stats),
                 "slot": self.ifs_channel or recovery_slot(self.filename)[0],
                 "next_cmd_delay": None, "detection_length": None,
                 "head_sensor": None}
        try:
            reply = print_control._call("/printer/objects/query?zmod_ifs")
            ifs = ((reply.get("result") or {}).get("status") or {}).get("zmod_ifs") or {}
            if isinstance(ifs, dict):
                facts["next_cmd_delay"] = ifs.get("next_cmd_delay")
                facts["detection_length"] = ifs.get("detection_length")
        except Exception as exc:
            log(f"jam facts zmod_ifs read failed: {exc!r}")
        try:
            listing = print_control._call("/printer/objects/list")
            objects = ((listing.get("result") or {}).get("objects")
                       if isinstance(listing, dict) else None)
            names = filament_sensor_names(objects)
            facts["head_sensor"] = any(
                name.startswith("filament_switch_sensor ") for name in names)
        except Exception as exc:
            log(f"jam facts sensor read failed: {exc!r}")
        return facts

    def _slot_agreement(self):
        """The four slot sources at print start (#1020 D3). Read-only."""
        from . import lane_data_sync, spool_sync

        job_slot, spool_id = recovery_slot(self.filename)
        if job_slot is None:
            mapped = sorted(launch_mapped_slots(self.filename))
            job_slot = mapped[0] if len(mapped) == 1 else None
        ffm_channel = ifs_chan = None
        try:
            reply = print_control._call("/printer/objects/query?FFMInfo&zmod_ifs")
            status = (reply.get("result") or {}).get("status") or {}
            ffm = (status.get("FFMInfo") or {}).get("channel")
            ifs = (status.get("zmod_ifs") or {}).get("Chan")
            ffm_channel = ffm if type(ffm) is int and 1 <= ffm <= 4 else None
            ifs_chan = ifs if type(ifs) is int and 1 <= ifs <= 4 else None
        except Exception as exc:
            log(f"slot self-check object read failed: {exc!r}")
        helix_slot = None
        try:
            lanes = lane_data_sync.parse_lane_data(spool_sync.read_lane_data())
            helix_slot = lane_data_sync.helix_seated_slot(lanes, spool_id)
        except Exception as exc:
            log(f"slot self-check lane read failed: {exc!r}")
        return lane_data_sync.slot_agreement_warning(
            job_slot=job_slot, ffm_channel=ffm_channel, ifs_chan=ifs_chan,
            helix_slot=helix_slot, filename=clean(self.filename or "this print"))

    async def _slot_self_check(self):
        """Post the slot mismatch warning once at print start (#1020 D3).

        Best-effort and read-only: a failed read posts nothing, and the
        warning fires only when the sources actually disagree.
        """
        try:
            warning = await asyncio.to_thread(self._slot_agreement)
            if warning:
                await asyncio.to_thread(post, warning)
        except Exception as exc:
            log(f"slot self-check failed: {exc!r}")

    def _runout_repause(self, slot):
        """True when this runout pause is the last runout pausing again on the same slot."""
        episode = self.runout_episode
        if not episode or not episode.get("resumed_at"):
            return False
        if slot is not None and episode.get("slot") is not None and slot != episode["slot"]:
            return False
        return time.time() - float(episode["resumed_at"]) <= RUNOUT_REPAUSE_WINDOW_S

    def _alert_message(self, kind):
        """(channel_id, message_id) of this job's newest Discord alert of ``kind``, or None."""
        if _buttonless_standalone() or _telegram_standalone() or _matrix_standalone():
            return None
        try:
            from . import print_alert_buttons as pab
            return pab.latest_alert_message(kind, self.filename)
        except Exception as exc:
            log(f"alert message lookup failed file={self.filename!r}: {exc!r}")
            return None

    def _post_runout_repause(self):
        """Rewrite the runout's alert in place; post one short alert when that is not possible.

        Runs in a worker thread. The rewritten message gets fresh Resume
        and Cancel buttons (the old ones were usually spent by the resume).
        """
        episode = self.runout_episode
        episode["repauses"] = int(episode.get("repauses") or 0) + 1
        episode["resumed_at"] = None
        text = runout_repause_message(self.filename, episode.get("slot"), episode["repauses"])
        log(f"runout pause again ({episode['repauses']}) on slot {episode.get('slot')}: "
            f"filament not at the toolhead yet; no new spool question: {self.filename!r}")
        target = episode.get("message")
        if target:
            try:
                from . import print_alert_buttons as pab
                from .. import listeners as _listeners
                if pab.rearm_alert(_bot_token(), target[0], target[1], text, "paused", self.filename,
                                   printer_id=_listeners.instance_printer_id() or None):
                    return True
            except Exception as exc:
                log(f"runout alert rewrite failed file={self.filename!r}: {exc!r}")
        delivered = post_alert(text, "paused", self.filename, episode="paused:runout")
        episode["message"] = self._alert_message("paused") or target
        return delivered

    async def upgrade_paused_runout(self, slot):
        """A paused print's slot emptied after the pause was posted as generic (#442).

        The pause alert already went out without a cause; do not leave the
        runout at a log line. Re-post as a runout alert (with the same
        Resume/Cancel buttons a paused alert carries) and open the spool
        question, exactly as if the pause had been classified runout at once.
        """
        self.last_pause_was_runout = True
        spool_id = _mapped_spool(slot) if slot else None
        # The upgrade re-posts the SAME pause with its cause filled in:
        # it carries the original pause's ping episode, so the re-post
        # does not ping a second time for the one problem (#1095).
        await asyncio.to_thread(post_alert, paused_message(self.filename, runout=True,
                                                           slot=slot, spool_id=spool_id),
                                "paused", self.filename,
                                episode=self.last_pause_episode or "paused:other")
        self.pause_context = await self.open_question(slot, "runout")
        self.runout_episode = {"slot": slot, "resumed_at": None, "repauses": 0,
                               "message": self._alert_message("paused")}

    def _visual_identity(self):
        """This print's identity for watchdog mutes (#476): the notification identity.

        The identity recorded at the print's fresh start, which the
        lifecycle claims and the capture key use too. Recomputing it later
        would flip it once the start claim expires (start_claim_ttl_s), and
        a mute would stop matching mid-print. Before any start is seen it
        falls back to the computed identity, which can be None; the mute
        store's fallback key covers that.
        """
        return self.print_identity or _notification_identity(
            {"start_time": start_claim_time(self.filename), **self.last_stats, "filename": self.filename})

    async def _dark_frame_check(self, reason, layer):
        """Handle one completely dark frame (#476). True when the frame was dark.

        A dark frame is a camera fact: it is logged, the vision endpoint is
        never consulted, the frame never becomes a stall baseline, and it
        never posts as a print alert. The operator gets at most one
        dark-camera notice per print — and none at all when this print's
        watchdog posts are muted.
        """
        log(f"visual check file={self.filename!r} reason={reason} layer={layer} verdict=dark detail='camera image completely dark'")
        if self.visual_watch.dark_noticed:
            return True
        self.visual_watch.dark_noticed = True
        from . import print_alert_buttons as pab
        if await asyncio.to_thread(pab.visual_muted, self.filename, self._visual_identity()):
            log(f"visual dark notice suppressed for muted print {self.filename!r}")
            return True
        await asyncio.to_thread(_visual_dark_notice, self.filename, self._visual_identity())
        return True

    async def visual_check(self, reason, layer):
        """One camera check under an overall deadline (VISUAL_CHECK_DEADLINE_S).

        A camera or verdict endpoint that never answers used to hang the
        check for ever, and the single-flight timer then skipped every beat:
        the watch died with no log. An overrun is abandoned, logged and
        counted in ``visual_timeouts``. Never raises.
        """
        filename = self.filename
        identity = self._visual_identity()
        watch = self.visual_watch
        try:
            await asyncio.wait_for(self._visual_check_once(reason, layer), VISUAL_CHECK_DEADLINE_S)
        except asyncio.TimeoutError:
            self.visual_timeouts = getattr(self, "visual_timeouts", 0) + 1
            log(f"visual check timed out file={self.filename!r} reason={reason} after "
                f"{VISUAL_CHECK_DEADLINE_S:.0f}s (timeouts so far: {self.visual_timeouts}); "
                "the camera or the vision endpoint did not answer")
            # Do not send an old print's timeout to a replacement print.
            if self.filename != filename or self._visual_identity() != identity or self.visual_watch is not watch:
                return
            try:
                from . import print_alert_buttons as pab
                # One failure episode with the camera and verdict failures
                # (visual_watch.FailureEpisode): the first alerts, repeats
                # wait out the periodic interval.
                if not watch.failure_alert_due(_watch_now()):
                    log(f"visual timeout alert suppressed for {filename!r}: "
                        "already alerted within the periodic interval")
                    return
                if await asyncio.to_thread(pab.visual_muted, filename, identity):
                    return
                await asyncio.to_thread(
                    _visual_alert_text, filename, _visual_watch.TIMEOUT_ALERT_TEXT, identity=identity)
            except Exception as alert_exc:
                log(f"visual timeout alert failed file={filename!r}: {alert_exc!r}")

    async def _note_watch_failure(self, filename, identity):
        """Count one failed check for this print. True when its alert should go out now.

        Muted prints still count (so their episode ends normally), but never alert.
        """
        if not self.visual_watch.failure_alert_due(_watch_now()):
            log(f"visual failure alert suppressed for {filename!r}: "
                "already alerted within the periodic interval")
            return False
        from . import print_alert_buttons as pab
        if await asyncio.to_thread(pab.visual_muted, filename, identity):
            log(f"visual alert suppressed for muted print {filename!r}")
            return False
        return True

    async def _note_watch_working(self, filename, identity):
        """A check judged a frame: end any failure episode, and say so once if it had alerted."""
        if not self.visual_watch.failures.recovered():
            return
        from . import print_alert_buttons as pab
        if await asyncio.to_thread(pab.visual_muted, filename, identity):
            return
        log(f"camera watch working again for {filename!r}")
        await asyncio.to_thread(post, _with_printer_name(
            _visual_watch.recovery_text(clean(filename))))

    async def _visual_check_once(self, reason, layer):
        """Capture and classify a print frame; notify humans only, never actuate.

        A configured vision endpoint that returns uncertain, or any snapshot or
        vision failure, alerts. Missing plastic, spaghetti, and a detached part
        alert. Layers that advanced with an unchanged frame are promoted to a
        stall here, because the scheduler cannot see the camera before capture.
        A stall alerts only when the vision verdict is not ok/safe: an ok frame
        never raises an operator alert, at any [visual_watch] sensitivity. A
        verdict that says the feed is dark gets the dark-camera notice, not an
        alert, and its stillness is not a stall.
        """
        # Bind this check to the print that produced the frame (Codex, #920):
        # capture its filename and identity before any await, and discard the
        # result if a different print has taken over by the time it lands —
        # a stale verdict must never count toward the new print's streak,
        # alert as the new print, or send its PAUSE.
        filename = self.filename
        identity = None
        def stale():
            return self.filename != filename or (identity is not None and self._visual_identity() != identity)
        image = None
        try:
            identity = self._visual_identity()
            image = await _watch_io(_camera_snapshot_bytes)
            captured_at = _watch_now()
            if stale():
                log(f"visual check discarded file={filename!r} reason={reason}: the print changed during capture")
                return
            if await _watch_io(_visual_watch.frame_dark, image):
                await self._dark_frame_check(reason, layer)
                return
            prior = self.visual_frames.get(filename)
            changed = None if prior is None else await _watch_io(_frame_changed, prior, image)
            # The shared per-check rules (camera_watch, #1320): stall
            # promotion, then the baseline frame; a layer-progress probe
            # that is not a stall adopts the frame without moving the
            # periodic clock and asks no verdict.
            reason, stall = _camera_watch.promote_stall(self.visual_watch, reason, changed, layer)
            self.visual_frames[filename] = image
            if _camera_watch.keep_frame(self.visual_watch, reason, layer, image, time.time()):
                log(f"visual check file={filename!r} reason={reason} layer={layer} changed={changed} verdict=skipped")
                return
            prompt = _camera_watch.WATCH_PROMPT
            early = self.visual_watch.in_early_window(layer)
            try:
                result = await _watch_io(
                    _visual_verdict, image, prompt, self.local_streak, local_fallback=early)
            except TypeError:
                result = await _watch_io(
                    _visual_verdict, image, prompt, self.local_streak)
            verdict, detail = result
            # The local detector's structured result (local_vision.Verdict);
            # None for an endpoint answer. The weak-detection rules decide
            # on it, never on the detail text.
            detection = _local_vision.detection_of(result)
            if stale():
                log(f"visual check discarded file={filename!r} reason={reason} verdict={verdict}: "
                    "the print changed while the verdict was in flight")
                return
            if verdict == "empty_idle" and self.state == "printing":
                # #913: print_stats says printing, so an empty bed is not a
                # healthy answer. Re-check within 60 s; a second empty-bed
                # verdict on that re-check alerts (camera_watch.is_suspicious).
                if reason == "empty_idle_recheck":
                    detail = (detail or "") + " (empty bed again while the printer reports printing)"
                else:
                    self.visual_watch.note_empty_idle(time.time())
                    log(f"visual check file={filename!r} empty_idle while printing; re-check in "
                        f"{_visual_watch.EMPTY_IDLE_RECHECK_S}s")
            printer_id = _watch_printer_id()
            signature = (await _watch_io(_visual_watch.frame_signature, image)
                         if verdict in _visual_watch.AUTO_PAUSE_VERDICTS else None)
            streak = self.visual_watch.note_verdict(
                verdict, layer, printer_id=printer_id, print_identity=identity,
                signature=signature, frame_age_s=_watch_now() - captured_at, now=_watch_now())
            frame_counted = self.visual_watch.last_counted
            if verdict in _visual_watch.AUTO_PAUSE_VERDICTS and early and not frame_counted:
                log(f"visual check file={filename!r} reason={reason}: suspicious frame not counted "
                    f"toward auto-pause: {self.visual_watch.last_evidence}")
            # Moonraker's recorded rules (camera_watch.FAMILY_RULES): a
            # healthy vision verdict outranks the pixel diff at every
            # sensitivity, and an empty bed while printing alerts on its
            # re-check.
            suspicious = _camera_watch.is_suspicious(
                verdict, reason=reason, changed=changed, profile=self.visual_watch.profile,
                rules=_camera_watch.FAMILY_RULES["moonraker"], printing=self.state == "printing",
                detection=detection)
            log(f"visual check file={filename!r} reason={reason} layer={layer} changed={changed} verdict={verdict} detail={detail!r}")
            if _verdict_says_dark(verdict, detail):
                # A dark feed frame_dark's thresholds let through: a camera
                # fact, not a print alert. Its stillness is no stall, so
                # neither the stall nor the dark baseline is kept.
                if stall:
                    self.visual_watch.alerted_stall_layer = None
                if prior is None:
                    self.visual_frames.pop(filename, None)
                else:
                    self.visual_frames[filename] = prior
                await self._dark_frame_check(reason, layer)
                return
            # A verdict call that could not judge the frame is a watch
            # failure (one episode with failed snapshots and timeouts, one
            # throttle); any real judgement ends the episode.
            watch_failed = _camera_watch.watch_failed(verdict, detail)
            if not watch_failed:
                await self._note_watch_working(filename, identity)
            # #981: what the watch did with this verdict, for the
            # always-on event recording after the alert block below.
            event_action = "none"
            event_discarded = False
            pause_now = False
            if not suspicious and _camera_watch.weak_noted(verdict, detection, reason=reason,
                                                           changed=changed):
                # A weak local detection that has not persisted: logged
                # above and recorded, never posted (camera_watch.weak_noted).
                log(f"visual check file={filename!r} reason={reason}: weak local detection "
                    "noted, not posted")
                event_action = "noted"
            if suspicious and image is not None:
                from . import print_alert_buttons as pab
                if await asyncio.to_thread(pab.visual_muted, filename, identity):
                    log(f"visual alert suppressed for muted print {filename!r}")
                    event_action = "suppressed"
                elif watch_failed and not self.visual_watch.failure_alert_due(_watch_now()):
                    log(f"visual failure alert suppressed for {filename!r}: "
                        "already alerted within the periodic interval")
                    event_action = "suppressed"
                elif stale():
                    log(f"visual alert discarded file={filename!r} reason={reason}: the print changed before the alert")
                    event_discarded = True
                else:
                    # decide_auto_pause reserves the once-per-print pause
                    # before yielding (Codex, #920): overlapping checks must
                    # not both observe auto_paused == False while an alert
                    # is in flight and each send their own PAUSE.
                    pause_now, pause_why = _camera_watch.decide_auto_pause(
                        self.visual_watch,
                        enabled=_visual_watch.watch_settings(_config.cfg())["auto_pause_early"],
                        printer_id=printer_id, identity=identity, state=self.state,
                        verdict=verdict, frame_age_s=_watch_now() - captured_at, early=early,
                        frame_counted=frame_counted)
                    if streak and not pause_now:
                        log(f"auto-pause not sent file={filename!r}: {pause_why}")
                    text = _camera_watch.alert_text(reason, layer, verdict, detail,
                                                    pause_why=pause_why if pause_now else "",
                                                    detection=detection)
                    if verdict == "spaghetti":
                        # The confirmed-pasta post (#1017 batch 2): a
                        # spaghetti verdict itself — never a mere risk
                        # flag like uncertain or air — carries a line from
                        # the spaghetti bank, keyed to this print so a
                        # repeat alert repeats the line.
                        text = _with_status_roast(text, filename, "spaghetti",
                                                  f"spaghetti:{identity or filename}")
                    await asyncio.to_thread(_visual_alert, filename, image, text,
                                            identity=identity,
                                            **({} if _camera_watch.alert_pings(
                                                reason, verdict, detection) else {"ping": False}))
                    if pause_now and (stale() or self.state != "printing"):
                        # The alert took time; the printer must still be
                        # printing this print when the PAUSE goes out.
                        log(f"auto-pause not sent file={filename!r}: the printer is {self.state} "
                            "or the print changed while the alert was posting")
                        pause_now = False
                        await asyncio.to_thread(post, _with_printer_name(
                            f"Auto-pause: {clean(filename)}\nNot sent: the printer is no longer "
                            "printing this print."))
                    event_action = "auto_pause" if pause_now else "alert"
                    if pause_now:
                        # #913: the only automatic printer action, opt-in, once per print
                        # (reserved above, before the alert await).
                        log(f"auto-pause early layers file={filename!r} layer={layer} streak={streak}: sending PAUSE")
                        reply = await asyncio.to_thread(_auto_pause_early, filename, identity)
                        log(f"auto-pause early layers file={filename!r}: {reply}")
                        await asyncio.to_thread(post, _with_printer_name(
                            f"Auto-pause: {clean(filename)}\n{reply}"))
            if not event_discarded:
                # #981: always-on watchdog event recording — best-effort
                # and append-only (hermes/watchdog_events carries the
                # rule); a recording failure never reaches this loop.
                await asyncio.to_thread(
                    _record_watchdog_event, filename, verdict,
                    streak=streak, action=event_action)
        except Exception as exc:
            log(f"visual check failed file={filename!r} reason={reason}: {exc!r}")
            if image is not None:
                try:
                    if await self._note_watch_failure(filename, identity):
                        await asyncio.to_thread(
                            _visual_alert, filename, image,
                            _camera_watch.vision_failed_text(reason, layer),
                            identity=identity)
                except Exception as alert_exc:
                    log(f"visual alert failed file={filename!r}: {alert_exc!r}")
            else:
                # The snapshot itself failed, so there is no image to
                # attach — but the operator still gets an alert: a dead
                # camera must not silently leave the print unmonitored.
                # Throttled like the Bambu watch: a dead camera never moves
                # the snapshot baseline, so probes keep coming; the alert
                # goes out at most once per periodic interval per print
                # (visual_watch.FailureEpisode, shared with timeouts and
                # verdict failures).
                try:
                    if await self._note_watch_failure(filename, identity):
                        await asyncio.to_thread(
                            _visual_alert_text, filename,
                            _camera_watch.camera_failed_text(reason, layer),
                            identity=identity)
                except Exception as alert_exc:
                    log(f"visual alert failed file={filename!r}: {alert_exc!r}")

    async def on_status(self, print_stats, reason):
        self.last_stats.update(print_stats)
        new_state = print_stats.get("state", self.state)
        if "filename" in print_stats:
            filename = print_stats.get("filename")
            if filename and filename != self.filename:
                self.filename = filename
                self.visual_watch.reset()
                self.local_streak.reset()
                # Only the current print's baseline frame is kept: the old
                # pop of the new name left one frame per past print behind.
                self.visual_frames.clear()
        if "message" in print_stats:
            self.pause_message = print_stats.get("message") or ""
        if new_state is None or new_state == self.state:
            return
        old = self.state
        identity = _notification_identity({"start_time": start_claim_time(self.filename),
                                           **self.last_stats, "filename": self.filename})
        fresh_start = new_state == "printing" and old != "paused"
        if fresh_start:
            # One identity per print, fixed here and reused by the watchdog
            # mute, the capture key and the terminal claim: recomputing it
            # later flips once the start claim expires (start_claim_ttl_s).
            # An unclaimed start gets its own so a mute outlives midnight.
            self.print_identity = identity or f"file:{self.filename}|seen:{time.time()}"
            identity = self.print_identity
            # When this print started, as seen live (None after a reconnect
            # mid-print): the end charge's window when history is unavailable.
            self.print_seen_started_at = time.time() if old is not None else None
            # The firmware-stop alert dedups per print attempt, not per
            # filename for the listener's lifetime: a reprint of the same
            # file must alert again if it stops too (P2-57).
            self.shutdown_alerted = None
        else:
            identity = self.print_identity or identity
        # A job that ends into standby (printer-side stop, job removed, Klipper
        # or printer restart during a long pause) never passes through a
        # TERMINAL state, but the job is just as over: #370. Treat it as a
        # terminal transition so it gets the same single claimed notice.
        gone = old in ("printing", "paused") and new_state == "standby"
        phase = "start" if fresh_start else ("terminal" if old in ("printing", "paused") and (new_state in TERMINAL or gone) else None)
        duplicate_start = False
        if phase and not _claim_notification(identity, phase):
            log(f"suppressed duplicate {phase} notice identity={identity!r}")
            if phase != "start":
                self.state = new_state
                return
            # A listener restart mid-print re-sees the claimed start: only
            # the start post is a duplicate, the print still needs its
            # capture, light and watchdog.
            duplicate_start = True
        self.state = new_state
        log(f"state {old} -> {new_state} file={self.filename!r} ({reason})")
        _record_transition_markers(old, new_state, self.filename, self.last_stats)
        if new_state == "printing":
            await asyncio.to_thread(self._settle_launch_lease)
        if fresh_start:
            # A fresh start (or a print already running when the listener
            # (re)connected): open the #246 stage 2 capture session,
            # replacing any session a missed end left open. A resume keeps
            # the session the pause left open.
            # Off the event loop with a deadline (#1020): this touches disk
            # and the camera, and a stall here once froze the heartbeat and
            # every alert. A capture that cannot start in time is skipped.
            await self._begin_capture(identity, resume=old is None)
            # #913: pick this print's watch schedule before the first check.
            self.visual_watch.reset()
            self.local_streak.reset()
            self._apply_watch_mode()
            self._note_watch_not_running()
            # #516: auto light-on at a fresh print start (a resume is not
            # a start; the light never went off during a pause).
            await self.light_print_started()
            # #1020 D3: the print-start slot self-check — one warning
            # post, only when the slot sources disagree after conversion.
            await self._slot_self_check()
        if old is None and new_state == "paused":
            # The listener (re)connected to a paused print: continue the
            # capture the earlier process left open, so the resume that
            # follows (not a fresh start) still has frames to add to.
            await self._begin_capture(self.print_identity or identity, resume=True)
        elif old is None and new_state in ("complete", "cancelled", "error"):
            # The print ended while the listener was down: close its
            # left-open capture before the announcer looks for it.
            await asyncio.to_thread(self._close_left_open_capture, new_state)
            # It was never charged either: the live end transition did not
            # happen, and the announcer's missed-finish check sees the same
            # job key, so neither charges it (#1362). Charge it here from
            # history, before the announcer builds the finish post; the
            # ledger makes it a no-op when it was charged already.
            await self._charge_missed_end()
        if new_state == "printing" and _config.cfg()["visual_watch"]["enabled"]:
            layer = _visual_layer(self.last_stats)
            reason_due = self.visual_watch.due(layer, time.time(), snapshot_changed=None)
            if reason_due and self._watch_ready():
                self.last_visual_task = asyncio.create_task(self.visual_check(reason_due, layer))
        if old is not None and old != "printing" and new_state == "printing":
            # Claimed starts already have the intake's one post; unclaimed starts (printer screen,
            # other tools) get exactly one canonical post here.
            if old == "paused":
                log(f"resume, no start post: {self.filename!r}")
                if self.last_pause_was_runout and self.runout_episode is not None:
                    self.runout_episode["resumed_at"] = time.time()
                self.last_pause_was_runout = False
                self.last_pause_episode = None
                # The pause is over: its starter-ping episodes stop
                # suppressing, so a later pause of the same print is a
                # new problem that can ping (#1095).
                starter_pings.clear_pause_episodes(job_key=self.filename)
            else:
                self.runout_episode = None
                self.slot_tracker.reset()
                self.failover_watch.reset()
                self.visual_watch.reset()
                self.local_streak.reset()
                self._apply_watch_mode()
                if duplicate_start:
                    log(f"start already announced; no second start post: {self.filename!r}")
                elif start_claimed(self.filename):
                    log(f"start claimed by print-now; updating its message, no second start post: {self.filename!r}")
                    await asyncio.to_thread(announce_warmup_done, self.filename)
                    await asyncio.to_thread(post_exclude_objects, self.filename)
                else:
                    await asyncio.to_thread(post, start_message(self.filename))
                    await asyncio.to_thread(post_exclude_objects, self.filename)
                if not duplicate_start:
                    # A new print while an older spool question is open: settle it as a
                    # refeed when the same spool is back, else one warn-only notice.
                    try:
                        await self.settle_spool_question(
                            state=new_state, filename=self.filename, new_print=True,
                            printing=True, start_notice=True)
                    except Exception as exc:
                        log(f"spool question check at print start failed: {exc!r}")
                # The live view's auto-start trigger (#311 stage 4):
                # fail-soft, and a no-op unless auto_start_on_print is on.
                await asyncio.to_thread(_live_auto_on_print_start)
        elif old == "printing" and new_state == "paused":
            if pause_is_jam(self.pause_message):
                # #1020 E1: an IFS jam gets its own post with the measured
                # facts and only safe actions; it is never auto-resumed
                # and never folded into the runout question flow.
                self.last_pause_was_runout = False
                self.last_pause_episode = "paused:jam"
                self.pause_context = None
                facts = await asyncio.to_thread(self._jam_facts)
                await asyncio.to_thread(post_alert, jam_message(self.filename, facts),
                                        "paused", self.filename, episode="paused:jam")
            else:
                self.last_pause_was_runout, slot = await self.classify_runout()
                if self.last_pause_was_runout and self._runout_repause(slot):
                    # The same runout pausing again: rewrite its alert, never
                    # a new episode, a new usage suspend or a new question.
                    self.last_pause_episode = "paused:runout"
                    await asyncio.to_thread(self._post_runout_repause)
                elif self.last_pause_was_runout:
                    self.last_pause_episode = "paused:runout"
                    spool_id = _mapped_spool(slot) if slot else None
                    await asyncio.to_thread(post_alert, paused_message(self.filename, runout=True,
                                                                       slot=slot, spool_id=spool_id),
                                            "paused", self.filename, episode="paused:runout")
                    self.pause_context = await self.open_question(slot, "runout")
                    self.runout_episode = {"slot": slot, "resumed_at": None, "repauses": 0,
                                           "message": self._alert_message("paused")}
                else:
                    self.last_pause_episode = "paused:other"
                    self.pause_context = None
                    await asyncio.to_thread(post_alert, paused_message(self.filename),
                                            "paused", self.filename, episode="paused:other")
        elif old in ("printing", "paused") and (new_state in TERMINAL or gone):
            self.last_pause_was_runout = False
            # Ended into standby gets the cancelled notice: the print stopped
            # at the printer without completing, which is what the operator
            # needs to hear (#370). The Bambu listener maps the same
            # transition to "cancelled" ("a stop returns the printer to idle").
            effective = "cancelled" if gone else new_state
            if effective == "cancelled":
                await maintenance_cancel_reminder()
            # Completion, records, snapshot, and session reset stay in the announcer.
            # The announcer also owns the sole completion Discord post.
            if effective == "cancelled":
                # One post: post_cancel_report leads with the cancelled summary and
                # falls back to it alone, so a separate ack read as a duplicate.
                # Finish stop capture before starting the screen-cancel reboot so Moonraker
                # state and log tails are read before Klipper restarts.
                reason = await asyncio.to_thread(capture_stop, self.filename, "cancel")
                task = asyncio.create_task(asyncio.to_thread(
                    post_cancel_report, self.filename, reason,
                    operator_cancel=(new_state == "cancelled" and not gone)))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
                from . import klipper_report
                if new_state == "cancelled" and not klipper_report.cancelled_by_bot():
                    self.start_screen_cancel_reboot("moonraker cancelled")
            elif new_state != "complete":
                if new_state == "error":
                    await asyncio.to_thread(capture_stop, self.filename, "error")
                    facts = {}
                    duration = announcer.elapsed_print_seconds(self.last_stats)
                    if duration is not None:
                        facts["duration_s"] = duration
                    mention = starter_pings.ping_for("error", job_key=self.filename) or None
                    await asyncio.to_thread(
                        post, _with_status_roast(
                            print_summary.format_event(new_state, self.filename),
                            self.filename, "failed",
                            f"failed:{identity or self.filename}", facts),
                        **({"mention_user_id": mention} if mention else {}))
                else:
                    await asyncio.to_thread(post, print_summary.format_event(new_state, self.filename))
        ended = new_state in ("complete", "cancelled", "error", "standby") and old in ("printing", "paused")
        if ended:
            # The print an open spool question was asked for is over: the
            # question stays open, but its reminders back off from here.
            try:
                record = load_runout_pause()
                if (record and record.get("usage_suspended") and not record.get("print_over")
                        and spool_question_print_is_current(record, old, self.filename)):
                    record["print_over"] = True
                    atomic_write_json(RUNOUT_PAUSE_STATE, record)
            except Exception as exc:
                log(f"spool question print-over mark failed: {exc!r}")
            # A multi-slot print charged no spool while it ran: charge each
            # slot's spool now, before the announcer builds the finish post
            # that names the charge.
            await self._charge_multislot_usage(new_state)
            # Close the capture before the announcer: its timelapse
            # postprocess only finds a capture whose manifest is no longer
            # "capturing", and would otherwise attach the previous job's.
            self._capture_end(new_state)
        await asyncio.to_thread(run_announcer, f"{old}->{new_state}")
        if ended:
            await self._print_ended(new_state)

    async def _charge_multislot_usage(self, new_state):
        """Charge a finished multi-slot print's usage to each slot's spool. Fail-soft.

        Moonraker charged nothing during the print (the launch cleared its
        active spool), so the per-tool grams go to the spool each slot held
        at launch. A stopped print is scaled by the filament it used. An
        open spool question that pauses usage charges nothing. The ledger
        in multislot_usage makes a second end event or a restart a no-op.
        The print context is charged only when it is this print's: its
        start inside the history entry's window (#1362), as the missed-end
        charge requires, so an earlier launch's context is never charged.
        """
        from . import multislot_usage
        filename = self.filename
        try:
            context = print_records.load_context(filename) if filename else {}
            plan = multislot_usage.slot_plan(context)
            if plan is None and not context.get("spool_usage_baseline"):
                return
            history = await asyncio.to_thread(self._history_entry, filename)
            # The print's own window: its history entry, else (a Moonraker
            # without [history]) the start this listener saw live.
            job_start = ((history or {}).get("start_time")
                         or self.__dict__.get("print_seen_started_at"))
            job_end = (history or {}).get("end_time") or time.time()
            if not multislot_usage.context_in_window(context, job_start, job_end):
                log(f"spool usage not checked file={filename!r}: the print context is not this "
                    f"print's (context started {context.get('started_at')!r}, history "
                    f"{'unavailable' if history is None else history.get('start_time')})")
                return
            finished = history if (history or {}).get("status") not in (None, "in_progress") else None
            if plan is None:
                await self._guard_single_slot_usage(context, new_state, finished)
                return
            outcome = "complete" if new_state == "complete" else "stopped"
            actual = planned = None
            if outcome != "complete":
                actual = (self.last_stats or {}).get("filament_used")
                if not isinstance(actual, (int, float)) or isinstance(actual, bool) or actual <= 0:
                    # A job that dropped to standby resets print_stats: ask history.
                    actual = (finished or {}).get("filament_used")
                if not context.get("planned_filament_mm"):
                    try:
                        metadata = await asyncio.to_thread(announcer.get_file_metadata, filename)
                        planned = (metadata or {}).get("filament_total")
                    except Exception:
                        planned = None
            record = load_runout_pause()
            suspended = bool(record and record.get("usage_suspended"))
            entry = await asyncio.to_thread(
                multislot_usage.charge_finished_print, context, outcome,
                actual_mm=actual, planned_mm=planned, suspended=suspended,
                base_url=_config.service_url("spoolman_url") or None)
        except Exception as exc:
            log(f"multi-slot usage charge failed file={filename!r}: {exc!r}")
            return
        _log_usage_entry(filename, entry)

    async def _charge_missed_end(self):
        """Charge a print that ended while the listener was down and is still the printer's job.

        The listener reconnects to a complete/cancelled/error job: no live
        end transition ran, and the announcer's missed-finish check sees
        the same job key, so neither path charged it (#1362). The history
        entry is the print's own record: multislot_usage.charge_history_job
        checks the context is this print's and charges through the same
        ledger, so a print already charged is a no-op. Fail-soft.
        """
        from . import multislot_usage
        filename = self.filename
        try:
            context = print_records.load_context(filename) if filename else {}
            if multislot_usage.slot_plan(context) is None and not context.get("spool_usage_baseline"):
                return
            history = await asyncio.to_thread(self._history_entry, filename)
            if history is None or history.get("status") in (None, "in_progress"):
                log(f"missed end not charged file={filename!r}: no finished history entry")
                return
            active = await asyncio.to_thread(self._active_spool_id)
            entry = await asyncio.to_thread(
                multislot_usage.charge_history_job, history, context, active_spool_id=active,
                base_url=_config.service_url("spoolman_url") or None)
        except Exception as exc:
            log(f"missed end usage charge failed file={filename!r}: {exc!r}")
            return
        _log_usage_entry(filename, entry)

    @staticmethod
    def _history_entry(filename):
        """Moonraker's newest history entry for filename (any status), or None when unknown."""
        if not filename:
            return None
        try:
            jobs = ((announcer.get_json("/server/history/list?limit=20&order=desc") or {})
                    .get("result") or {}).get("jobs") or []
        except Exception:
            return None
        for job in jobs:
            if isinstance(job, dict) and job.get("filename") == filename and job.get("start_time"):
                return job
        return None

    @staticmethod
    def _active_spool_id():
        """Moonraker's active spool id now, or None when there is none or it cannot be read."""
        try:
            result = (announcer.get_json("/server/spoolman/spool_id") or {}).get("result")
        except Exception:
            return None
        return result.get("spool_id") if isinstance(result, dict) else None

    async def _guard_single_slot_usage(self, context, new_state, history=None):
        """Undo a runaway Moonraker charge on a finished single-slot print (#1374).

        Moonraker charged the active spool while the print ran. After a
        Moonraker restart its first charge can be everything Klipper has
        extruded since boot; multislot_usage.guard_single_slot compares the
        spool's growth since launch with what this print extruded and
        writes the used weight back when it is far over. Moonraker's active
        spool at the end goes with it: a spool no longer active is left alone.
        """
        from . import multislot_usage
        filename = self.filename
        if not (context or {}).get("spool_usage_baseline"):
            return
        actual = (self.last_stats or {}).get("filament_used")
        if not isinstance(actual, (int, float)) or isinstance(actual, bool) or actual <= 0:
            actual = (history or {}).get("filament_used")
        record = load_runout_pause()
        active = await asyncio.to_thread(self._active_spool_id)
        entry = await asyncio.to_thread(
            multislot_usage.guard_single_slot, context,
            "complete" if new_state == "complete" else "stopped", actual_mm=actual,
            active_spool_id=active, suspended=bool(record and record.get("usage_suspended")),
            base_url=_config.service_url("spoolman_url") or None)
        _log_usage_entry(filename, entry)

    async def _print_ended(self, new_state):
        """End-of-print cleanup, for a terminal transition or a Klipper shutdown."""
        self.visual_watch.reset()
        self.local_streak.reset()
        self.visual_frames.pop(self.filename, None)
        self._capture_end(new_state)
        self.print_identity = None
        self.runout_episode = None
        # #516: the print is over; open the auto light-off idle window.
        await self.light_print_finished()
        # An auto-started live session ends with its print (#311 stage 4).
        await asyncio.to_thread(_live_auto_on_print_end)
        await self._retire_paused_prompt(new_state)
        await self.after_print(new_state)

    async def _retire_paused_prompt(self, new_state):
        """Retire this job's paused prompt now that the job has ended (#370).

        The prompt's Resume/Cancel buttons act on a live paused job. Left
        live, a later tap is the first anyone hears that the job ended —
        the operator discovers it is gone only by pressing Cancel. Discord
        only: buttonless platforms never recorded a prompt. Never raises
        into the status flow.
        """
        if _buttonless_standalone():
            return
        headline = {
            "complete": "Print complete.",
            "cancelled": "Print cancelled.",
            "error": "Print stopped with an error.",
            "standby": "Print ended at the printer.",
        }.get(new_state, "Print ended.")
        try:
            from . import print_alert_buttons as pab
            await asyncio.to_thread(
                pab.retire_alerts, _bot_token(), self.filename,
                "This paused prompt is closed; its buttons no longer act on a live job.",
                ("paused",), headline=headline)
        except Exception as exc:
            log(f"retire paused prompt failed file={self.filename!r}: {exc!r}")

    def start_screen_cancel_reboot(self, why):
        """Fire once per cancel, in the background, and only when the bot did not cancel it."""
        from . import klipper_report
        if not printers.current().has_native_screen or self.display_off == 1:
            # The screen-cancel firmware restart is a stock-screen workaround; screenless ZMod
            # (display_off=1) has no stock firmware running, and its cancels are plain Klipper ones.
            return False
        if klipper_report.cancelled_by_bot():
            log(f"cancel was the bot's ({why}); no auto restart")
            return False
        if self.screen_reboot_task is not None and not self.screen_reboot_task.done():
            log(f"screen cancel restart already running ({why})")
            return False
        log(f"screen cancel detected ({why}); auto restart")
        self.screen_reboot_task = asyncio.create_task(asyncio.to_thread(screen_cancel_reboot, self.filename))
        return True

    async def after_print(self, new_state):
        """Screen updates deferred during the print, then the next queued copy prompt."""
        self.slot_tracker.reset()
        self.failover_watch.reset()
        try:
            from . import spool_sync
            # A listener instance (#336) flushes only its own printer's
            # deferred-screen queue (#335 keyed the queues per printer, so
            # an update can never reach another printer's screen); with no
            # instance identity (direct/Hermes runs) the legacy queue is
            # flushed exactly as before.
            from .. import listeners as _listeners
            instance_id = _listeners.instance_printer_id()
            pending = spool_sync._pending_path(None, instance_id) if instance_id else None
            flushed = await asyncio.to_thread(
                spool_sync.flush_pending_screen, pending_path=pending)
            if flushed:
                log(f"deferred screen sync applied to slots {flushed}")
        except Exception as exc:
            log(f"deferred screen sync failed: {exc!r}")
        if new_state == "complete":
            from . import print_control
            # Reprint queues are per printer (P2-135): a listener instance
            # consults only its own printer's queue; with no instance
            # identity the legacy queue is read exactly as before.
            instance_id = None
            try:
                from .. import listeners as _listeners
                instance_id = _listeners.instance_printer_id()
            except Exception:
                instance_id = None
            prompt = print_control.next_copy_prompt(self.filename, printer_id=instance_id)
            if prompt:
                await asyncio.to_thread(post, prompt)

    # -- #516: auto light-off ----------------------------------------------
    # A side feature like capture below: every hook fails soft, and with
    # vision.auto_light_off_minutes at its default of 0 the policy is
    # disabled and nothing here changes any existing behaviour. The
    # feature gate is the policy's minutes AND a vision endpoint
    # (_vision_ready) AND a resolved light target; the first two are
    # re-checked at each use, the target is resolved lazily once.

    def _auto_light_policy(self):
        if self._auto_light is None:
            vision = _config.cfg().get("vision") or {}
            self._auto_light = light_control.AutoLightPolicy(
                vision.get("auto_light_off_minutes", 0))
        return self._auto_light

    async def _light_target_object(self):
        """The resolved light object name, or None when the feature cannot act.

        The operator's vision.light_object wins; otherwise the printer's
        object list is asked (once, cached) and auto-discovery resolves
        only an exactly-one candidate. An unusable configured name, or
        zero/several candidates, logs once and stays unresolved; an
        unreadable object list is transient and retried next time.
        """
        if self.light_target_resolved:
            return self.light_target
        vision = _config.cfg().get("vision") or {}
        configured = str(vision.get("light_object") or "").strip()
        if configured:
            try:
                target, _candidates = light_control.resolve_light_object(configured, [])
            except ValueError as exc:
                if not self.light_unresolved_logged:
                    self.light_unresolved_logged = True
                    log(f"auto light: vision.light_object is unusable ({exc}); "
                        f"the feature stays off until it names one light object")
                return None
            self.light_target, self.light_target_resolved = target, True
            return target
        objects = await asyncio.to_thread(_light_object_names)
        if objects is None:
            return None
        target, candidates = light_control.resolve_light_object("", objects)
        if target:
            self.light_target, self.light_target_resolved = target, True
            log(f"auto light: using the printer's only light object {target!r}")
            return target
        if not self.light_unresolved_logged:
            self.light_unresolved_logged = True
            if candidates:
                log(f"auto light: the printer lists several light objects {candidates}; "
                    f"set vision.light_object to choose one — the feature stays off until then")
            else:
                log("auto light: the printer lists no light object "
                    "(led/neopixel/output_pin); the feature stays off")
        return None

    async def light_print_started(self):
        """Print-start transition: light on, from state only (never vision)."""
        try:
            policy = self._auto_light_policy()
            if not policy.enabled:
                return
            if policy.print_started(time.time()) != "turn_on":
                return
            if not self._watch_ready():
                return
            target = await self._light_target_object()
            if not target:
                return
            on_script, _off = light_control.light_scripts(target)
            await asyncio.to_thread(_send_light_script, on_script)
            log(f"auto light: print started; light on ({target})")
        except Exception as exc:
            # Best-effort: a failed light command logs once per start and
            # never blocks or fails the print flow.
            log(f"auto light: light-on at print start failed: {exc!r}")

    async def light_print_finished(self):
        """Terminal print-end transition: open the idle window."""
        try:
            self._auto_light_policy().print_finished(time.time())
        except Exception as exc:
            log(f"auto light: opening the idle window failed: {exc!r}")

    async def light_idle_tick(self):
        """One idle plate check when the policy says one is due. Never raises.

        Runs on the native-presence poll cadence; the policy's own check
        interval (300s) is the real cadence. The frame is judged through
        the same snapshot + verdict path as the watchdog. A completely
        dark frame is fed as "uncertain" (#476's rule: an unjudgeable
        frame is uncertain), which resets the empty streak — darkness
        never turns the light off.
        """
        try:
            policy = self._auto_light_policy()
            if not policy.enabled or self.state not in _AUTO_LIGHT_IDLE_STATES:
                return
            now = time.time()
            if not policy.should_check(now):
                return
            if not self._watch_ready():
                policy.note_check(now)
                return
            target = await self._light_target_object()
            if not target:
                policy.note_check(now)
                return
            image = await _watch_io(_camera_snapshot_bytes)
            if await _watch_io(_visual_watch.frame_dark, image):
                verdict, detail = "uncertain", "camera frame completely dark"
            else:
                verdict, detail = await asyncio.wait_for(_watch_io(
                    _visual_verdict, image, _AUTO_LIGHT_PROMPT, idle=True), VISUAL_CHECK_DEADLINE_S)
            action = policy.note_verdict(verdict, time.time())
            log(f"auto light idle check verdict={verdict} detail={detail!r}")
            if action == "turn_off":
                if self.state not in _AUTO_LIGHT_IDLE_STATES:
                    # A new print started while the snapshot or the vision
                    # request was in flight: the verdict describes the
                    # plate as it was, not as it is. Revalidate before
                    # issuing OFF — never switch the light off under a
                    # running print (P2-200).
                    log(f"auto light: idle verdict went stale (printer is "
                        f"{self.state}); light stays on")
                    return
                _on, off_script = light_control.light_scripts(target)
                await asyncio.to_thread(_send_light_script, off_script)
                log(f"auto light: plate seen empty for "
                    f"{policy.minutes} minutes; light off ({target})")
        except Exception as exc:
            log(f"auto light idle check failed: {exc!r}")
            with contextlib.suppress(Exception):
                # A check that could not observe the plate (the camera is
                # down, say) is an uncertain observation, like a failed
                # vision verdict: it resets the empty streak instead of
                # merely advancing the cadence, so one empty verdict after
                # the camera recovers cannot satisfy the sustained-empty
                # requirement on a pre-outage streak (P2-201).
                self._auto_light_policy().note_verdict("uncertain", time.time())

    def _schedule_light_idle_tick(self):
        """Run the idle light check as a task, off the receive loop (P2-200).

        Awaited inline, the check's snapshot + vision round trip stalled
        status processing, so a print starting mid-check could not update
        self.state before the OFF decision. As a task the loop keeps
        receiving; light_idle_tick revalidates the state before it sends.
        Single-flight, like the capture fetch.
        """
        task = self._light_idle_task
        if task is not None and not task.done():
            return
        self._light_idle_task = asyncio.create_task(self.light_idle_tick())

    def _schedule_next_plate_offer(self):
        """Run the next-plate bed look as a task, off the receive loop.

        Its snapshot and vision round trip can take about a minute; awaited
        inline it held every status event behind it. Single-flight, like
        the idle light check; the look revalidates the printer state before
        it acts, so a print that started meanwhile voids it.
        """
        task = getattr(self, "_next_plate_task", None)
        if task is not None and not task.done():
            return
        self._next_plate_task = asyncio.create_task(self._next_plate_offer_task())

    async def _next_plate_offer_task(self):
        try:
            await self.offer_next_plate_if_clear()
        except Exception as exc:
            log(f"next plate offer failed: {exc!r}")

    async def offer_next_plate_if_clear(self):
        """One empty-bed look for a queued plate. Posts an offer. Starts nothing.

        Uses the same snapshot and verdict words as the auto-light check (#516).
        No camera, a dark frame, or no queued plate: no offer and no invented bed.
        A print that starts (or a file change) while the look is in flight
        voids it: the frame shows the bed as it was, not as it is.
        """
        from . import next_plate_offer
        if self.state not in _AUTO_LIGHT_IDLE_STATES:
            return
        filename = self.filename

        def voided():
            if self.state in _AUTO_LIGHT_IDLE_STATES and self.filename == filename:
                return False
            log(f"next plate bed check voided: the printer is {self.state} now")
            return True
        if not next_plate_offer.due():
            return
        if not self._watch_ready():
            next_plate_offer.observe(None)
            return
        image = await _watch_io(_camera_snapshot_bytes)
        if not image or await _watch_io(_visual_watch.frame_dark, image):
            next_plate_offer.observe(None)
            return
        verdict, detail = await asyncio.wait_for(_watch_io(
            _visual_verdict, image, _AUTO_LIGHT_PROMPT, idle=True), VISUAL_CHECK_DEADLINE_S)
        if verdict == "uncertain":
            # Stage 1 (#455): the calibrated plate reference may certify
            # the empty bed the offer waits for (see light_idle_tick).
            from . import local_empty as _local_empty
            local = await _watch_io(_local_empty.idle_verdict, image)
            if local is not None:
                verdict, detail = local
        if voided():
            return
        log(f"next plate bed check verdict={verdict} detail={detail!r}")
        choice = next_plate_offer.observe(verdict)
        if not isinstance(choice, dict):
            return
        if choice.get("action") == "offer" and await asyncio.to_thread(post_bed_clear_offer, choice):
            return
        text = choice.get("text") or ""
        if text:
            await asyncio.to_thread(post, text)


    # -- #246 stage 2: in-container timelapse capture ---------------------
    # Capture is a side feature: every hook fails soft and, with
    # timelapse.capture_enabled off (the default), begin() returns None
    # and nothing here changes any existing behaviour.

    def _left_open_capture(self):
        """The capture an earlier listener process left open for this print, or None.

        The printer's own run time for the job (total_duration) dates the
        job's start, so a capture left open by an earlier print of the same
        file is never continued. Never raises.
        """
        started = None
        try:
            total = float((self.last_stats or {}).get("total_duration"))
            if total >= 0:
                started = time.time() - total
        except (TypeError, ValueError):
            started = None
        return timelapse_capture.open_capture(
            self.filename, data=_config.cfg(), job_started_at=started)

    def _capture_begin(self, job_key, resume=False):
        """Open a capture session for the job that just started; return it. Never raises.

        Runs in a worker thread (#1020): it touches no listener state, so a
        thread that outlives its deadline cannot install anything. The
        caller (:meth:`_begin_capture`) installs the returned session on
        the event loop only if the start is still the current one, and
        closes it otherwise. With ``resume`` (the listener just connected
        to a print already running), the capture an earlier process left
        open for this print is continued instead of starting a second one.
        """
        try:
            if resume:
                directory = self._left_open_capture()
                if directory is not None:
                    session = timelapse_capture.resume(directory, data=_config.cfg(), log=log)
                    if session is not None:
                        return session
            return timelapse_capture.begin(
                job_key, self.filename, data=_config.cfg(), log=log)
        except Exception as exc:  # begin() already fails soft; belt and braces
            log(f"timelapse capture begin failed file={self.filename!r}: {exc!r}")
            return None

    async def _begin_capture(self, job_key, resume=False):
        """Install the capture session for a fresh start, with a deadline (#1020).

        Cancelling the awaited thread call does not stop the thread, so a
        start that times out abandons its request: the late result is
        closed by a completion callback instead of being installed over
        whatever the listener has moved on to (a print that ended, or a
        newer print's own session).
        """
        self._capture_end("superseded")  # a session a missed end left open
        self._capture_generation += 1
        generation = self._capture_generation
        worker = asyncio.ensure_future(asyncio.to_thread(self._capture_begin, job_key, resume))
        try:
            session = await asyncio.wait_for(asyncio.shield(worker), CAPTURE_BEGIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            worker.add_done_callback(self._dispose_late_capture)
            log(f"timelapse capture begin timed out after {CAPTURE_BEGIN_TIMEOUT_S}s; this print runs without a capture")
            return
        except asyncio.CancelledError:
            worker.add_done_callback(self._dispose_late_capture)
            raise
        if generation != self._capture_generation or self.state not in ("printing", "paused"):
            self._close_capture_session(session)
            return
        self.capture_session = session

    def _close_left_open_capture(self, state):
        """Finish the capture a dead listener left open for a print that ended meanwhile.

        Its manifest still says "capturing", which the completion's
        timelapse lookup never uses; stamping it with the end state lets
        the completion post carry the frames taken before the restart.
        Runs in a worker thread. Never raises.
        """
        try:
            directory = self._left_open_capture()
            if directory is None:
                return
            session = timelapse_capture.resume(directory, data=_config.cfg(), log=log)
            if session is not None:
                session.stop(state)
        except Exception as exc:
            log(f"timelapse capture close after restart failed file={self.filename!r}: {exc!r}")

    def _close_capture_session(self, session):
        """Close a session that was opened but never installed. Never raises."""
        if session is None:
            return
        try:
            session.stop("superseded")
        except Exception as exc:  # stop() already fails soft; belt and braces
            log(f"timelapse capture close of an uninstalled session failed file={self.filename!r}: {exc!r}")

    def _dispose_late_capture(self, worker):
        """Close a capture session whose start was abandoned (a timeout).

        A done callback on the event loop: the session is never installed,
        whatever the listener has moved on to since.
        """
        if worker.cancelled():
            return
        if worker.exception() is not None:
            return  # _capture_begin never raises; a failure opened nothing
        self._close_capture_session(worker.result())

    def _capture_end(self, state):
        """Close the active capture session, if any. Never raises."""
        self._capture_generation += 1  # a begin still in flight for this print is now stale
        session, self.capture_session = self.capture_session, None
        if session is not None:
            try:
                session.stop(state)
            except Exception as exc:  # stop() already fails soft; belt and braces
                log(f"timelapse capture end failed file={self.filename!r}: {exc!r}")

    async def _capture_due(self, layer):
        """Store a frame when the active session's trigger is due.

        The due check is pure; only an actual capture (an HTTP fetch)
        leaves the event loop. Never raises into the status flow.
        """
        session = self.capture_session
        if session is None or self.state != "printing":
            return
        try:
            if session.due(layer=layer):
                await asyncio.to_thread(session.tick, layer)
        except Exception as exc:
            log(f"timelapse capture tick failed file={self.filename!r}: {exc!r}")

    def _schedule_capture(self, layer):
        """Run one due capture as a task, off the websocket receive loop (P2-133).

        Awaiting the fetch inline held the receive loop for up to the
        fetch timeout per attempt, delaying pause/runout handling,
        firmware-shutdown alerts and completion processing behind a slow
        camera. Single-flight: a fetch still running when the next status
        lands is the capture in progress — statuses never queue fetches
        behind it, and the due check inside the task decides if a frame
        is owed at all.
        """
        task = self._capture_task
        if task is not None and not task.done():
            return
        self._capture_task = asyncio.create_task(self._capture_due(layer))

    def _capture_timer_tick(self):
        """One interval-capture beat: ask for a frame when a timed trigger is live.

        Status events only carry the layer signal (print_stats changes once
        per layer), so an interval or both trigger needs its own beat (#906).
        Scheduling is single-flight and runs off the receive loop. Inert while
        paused or idle, without a session, and for the layer trigger.
        """
        session = self.capture_session
        if session is None or self.state != "printing":
            return False
        if getattr(session.trigger, "name", "interval") not in ("interval", "both"):
            return False
        self._schedule_capture(_visual_layer(self.last_stats))
        return True

    def _apply_watch_mode(self):
        """Set the camera watch schedule for the current print from its start context (#913).

        A risky print (visual_watch.risky_job: a slicer finding, a print
        anyway override, a supports decision, or the risky marker) gets the
        early-layer mode; every other print keeps the normal schedule.
        """
        try:
            cfg = _visual_watch.watch_settings(_config.cfg())
            self.visual_watch.early_layers = cfg["early_layers"]
            self.visual_watch.early_interval_s = cfg["early_interval_s"]
            context = print_records.load_context(self.filename) if self.filename else {}
            # On the generic launch path record_start writes this context
            # only after the start is confirmed, so it can land after the
            # first printing event: remember whether a context existed, so
            # the timer tick can retry the lookup until one does (Codex, #920).
            self._watch_context_seen = bool(context)
            why = _visual_watch.risky_job(context)
        except Exception as exc:
            log(f"camera watch mode lookup failed file={self.filename!r}: {exc!r}")
            self._watch_context_seen = False
            why = ""
        mode = _visual_watch.WATCH_MODE_EARLY if why else _visual_watch.WATCH_MODE_NORMAL
        self.visual_watch.set_mode(mode)
        if why:
            log(f"camera watch mode=early_layer file={self.filename!r} ({why}): every layer for "
                f"{self.visual_watch.early_layers} layers, at least every {self.visual_watch.early_interval_s}s")
        return mode

    def _visual_timer_tick(self, now=None):
        """One camera-watch beat, independent of layer events (#913).

        Moonraker sends print_stats only when a field changes, so a long
        layer produced no event and no periodic check (V12: 49 minutes).
        Single-flight: a check still running skips this beat.
        """
        if not _config.cfg()["visual_watch"]["enabled"]:
            return None
        self._post_detector_notices()
        if self.state != "printing":
            return None
        task = self.last_visual_task
        if task is not None and not task.done():
            return None
        if (self.visual_watch.mode != _visual_watch.WATCH_MODE_EARLY
                and not getattr(self, "_watch_context_seen", False)):
            # The start context had not landed when the mode was first
            # picked; retry until it exists, so a risky print upgrades to
            # the early-layer schedule instead of staying on normal cadence.
            self._apply_watch_mode()
        layer = _visual_layer(self.last_stats)
        now = time.time() if now is None else now
        reason = self.visual_watch.tick(layer, now, elapsed_s=_print_elapsed(self.last_stats))
        ready = None
        if layer is None:
            ready = self._watch_ready()
            if ready:
                self._note_layerless_watch(now)
        else:
            self.visual_watch.layer_unknown_since = None
        if reason and (self._watch_ready() if ready is None else ready):
            self.last_visual_task = asyncio.create_task(self.visual_check(reason, layer))
            return reason
        return None

    def _post_detector_notices(self):
        """Post the local detector's pending failure and recovery notices (local_vision.take_notices).

        Only while the watch judges with the detector: a detector the watch
        does not use has nothing to tell the operator. Never raises.
        """
        try:
            if not _local_vision.watch_uses_detector():
                return
            for text in _local_vision.take_notices():
                log(f"camera watch: {text}")
                task = asyncio.create_task(asyncio.to_thread(post, _with_printer_name(text)))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
        except Exception as exc:
            log(f"local detector notice failed: {exc!r}")

    def _watch_ready(self):
        """The listener's camera-check gate: a camera exists and a frame can be judged.

        With no camera configured the watch is off for this printer: no
        check is scheduled, so a missing camera never posts an alert per
        cadence (the print-start notice names it instead).
        """
        if _camera_missing_reason():
            return False
        return _vision_ready()

    def _watch_unavailable_reason(self):
        """Why the camera watch cannot run for this printer, in plain words; "" when it can."""
        camera = _camera_missing_reason()
        if camera:
            return camera
        if _vision_ready():
            return ""
        try:
            reason = _visual_watch.watch_unavailable_reason(
                lambda: _profile_env_value("CLIPROXY_API_KEY"))
        except Exception as exc:
            reason = f"the vision route could not be checked ({exc})"
        return reason or "no vision route is ready"

    def _note_watch_not_running(self):
        """At a print start: one chat notice when the watch is switched on but cannot run.

        A watch that silently does nothing reads as a watched print; the
        operator hears once per print why it is not watched. Never raises.
        """
        try:
            if not _config.cfg()["visual_watch"]["enabled"]:
                return None
            reason = self._watch_unavailable_reason()
            if not reason:
                return None
            log(f"camera watch not running for {self.filename!r}: {reason}")
            text = _with_printer_name(
                f"Camera watch is on but not running for {clean(self.filename)}: {reason}. "
                "This print is not being watched; nothing was sent to the printer.")
            task = asyncio.create_task(asyncio.to_thread(post, text))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            return reason
        except Exception as exc:
            log(f"camera watch status notice failed: {exc!r}")
            return None

    def _note_layerless_watch(self, now=None):
        """Post, once per print, that the watch runs on a timer: the print reports no layers.

        Only once printing has really started (the timed checks began,
        which waits for printing time) and the layer has stayed unknown for
        LAYERLESS_NOTICE_GRACE_S since, and never for a print that reports
        its total layer count: that print has layer info, its first layer
        marker is just late (start G-code, purge).
        """
        watch = self.visual_watch
        if watch.layerless_noticed or not watch.layerless:
            return
        now = time.time() if now is None else now
        if watch.layer_unknown_since is None:
            watch.layer_unknown_since = now
        if now - watch.layer_unknown_since < _visual_watch.LAYERLESS_NOTICE_GRACE_S:
            return
        info = (self.last_stats or {}).get("info")
        total = info.get("total_layer") if isinstance(info, dict) else None
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            return
        watch.layerless_noticed = True
        text = _layerless_notice_text(self.filename, watch.profile["periodic_s"],
                                      watch.mode == _visual_watch.WATCH_MODE_EARLY)
        log(f"camera watch file={self.filename!r}: no layer numbers reported; checking on a timer")
        task = asyncio.create_task(asyncio.to_thread(post, text))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _visual_timer(self, sleep=None):
        """Run camera-watch beats every VISUAL_TIMER_S until cancelled (disconnect)."""
        sleep = sleep or asyncio.sleep
        while True:
            await sleep(VISUAL_TIMER_S)
            try:
                self._visual_timer_tick()
            except Exception as exc:
                log(f"camera watch timer failed: {exc!r}")

    async def _capture_timer(self, sleep=None):
        """Run capture beats every capture_interval_s until cancelled (disconnect)."""
        sleep = sleep or asyncio.sleep
        while True:
            try:
                interval = timelapse_capture.capture_settings(_config.cfg())["capture_interval_s"]
            except Exception:
                interval = timelapse_capture.DEFAULTS["capture_interval_s"]
            await sleep(max(0.5, interval))
            try:
                self._capture_timer_tick()
            except Exception as exc:
                log(f"timelapse capture timer failed: {exc!r}")

    def _settle_launch_lease(self):
        """The printer runs this file: clear a launch lease left uncertain for it (lost start response)."""
        from .. import listeners as _listeners
        try:
            row = launch_state.settle_observed(_listeners.instance_printer_id(), self.filename)
            if row:
                log(f"launch lease settled: the printer reports {self.filename!r}, so the earlier "
                    f"{row['phase']} start reached it")
        except Exception as exc:
            log(f"launch lease settle failed: {exc!r}")

    @staticmethod
    def _clock_adapter():
        """This listener's printer adapter: its roster entry's, else the configured printer's (#1147)."""
        from .. import listeners as _listeners
        pid = _listeners.instance_printer_id()
        if pid:
            return printers.get(settings.by_id(pid)["model"], settings.view_for(pid))
        return printers.current()

    def _sync_printer_clock(self, why):
        """Set the printer's clock from this host's when it is off and the printer is idle.

        Runs at connect, at klippy ready and every CLOCK_SYNC_INTERVAL_S from the poll, so a
        printer that was busy then, or whose NTP never answered, is corrected once idle (2026-11-03).
        A failed set backs off CLOCK_SYNC_BACKOFF_S; a busy printer is simply tried again.
        """
        if time.monotonic() < self.clock_sync_retry_at:
            return
        try:
            res = launch_state.sync_printer_clock(_config.cfg()["services"]["moonraker_url"],
                                                  settings.current()["discord"]["timezone"], log=log,
                                                  adapter=self._clock_adapter())
            if res.startswith("failed"):
                self.clock_sync_retry_at = time.monotonic() + CLOCK_SYNC_BACKOFF_S
            if res not in ("ok", "off", "skipped:printer not idle", launch_state.NOT_ZMOD):
                log(f"printer clock sync ({why}): {res}")
        except Exception as exc:
            self.clock_sync_retry_at = time.monotonic() + CLOCK_SYNC_BACKOFF_S
            log(f"printer clock sync ({why}) failed: {exc!r}")

    async def session(self):
        self.ifs_monitor.reset_baseline(warmup=True)
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
            self.moonraker_connected = True
            await ws.send(SUBSCRIBE)
            await asyncio.to_thread(self._sync_printer_clock, "connect")
            last_tick = time.monotonic()
            capture_timer = asyncio.create_task(self._capture_timer())
            visual_timer = asyncio.create_task(self._visual_timer())
            try:
                await self._receive_loop(ws, last_tick)
            finally:
                capture_timer.cancel()
                visual_timer.cancel()

    async def _receive_loop(self, ws, last_tick):
        if True:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    raw = None
                if raw:
                    msg = json.loads(raw)
                    method = msg.get("method")
                    if msg.get("id") == 1:
                        if "error" in msg:  # klippy not ready yet; notify_klippy_ready resubscribes
                            log(f"subscribe refused: {msg['error']}")
                        else:
                            status = ((msg.get("result") or {}).get("status") or {}).get("print_stats") or {}
                            await self.on_status(status, "subscribe")
                            self._schedule_capture(_visual_layer(self.last_stats))
                    elif method == "notify_status_update":
                        ps = (msg.get("params") or [{}])[0].get("print_stats")
                        if ps:
                            await self.on_status(ps, "event")
                            self._schedule_capture(_visual_layer(self.last_stats))
                            if self.state == "printing" and _config.cfg()["visual_watch"]["enabled"]:
                                layer = _visual_layer(self.last_stats)
                                reason_due = self.visual_watch.due(layer, time.time())
                                if reason_due and self._watch_ready():
                                    self.last_visual_task = asyncio.create_task(self.visual_check(reason_due, layer))
                    elif method == "notify_gcode_response":
                        for line in msg.get("params") or []:
                            if no_mesh_line(line):
                                log(f"no bed mesh at print start: {self.filename!r}")
                                await asyncio.to_thread(post, f"{NO_MESH_ALERT}\nPrint: {print_summary.safe_label(self.filename)}")
                    elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
                        self.ifs_monitor.reset_baseline(warmup=True)
                        log(f"{method} while state={self.state}")
                        if self.state in ("printing", "paused"):
                            if getattr(self, "shutdown_alerted", None) != self.filename:
                                self.shutdown_alerted = self.filename  # one firmware-stopped alert per job
                                await asyncio.to_thread(post_shutdown_alert, self.filename)
                            else:
                                log("firmware stop already alerted for this job; not posting again")
                            self.state = "error"
                            # Klipper comes back as error -> standby, which
                            # is not a terminal transition: clean up here.
                            await self._print_ended("error")
                    elif method == "notify_klippy_ready":
                        self.ifs_monitor.reset_baseline(warmup=True)
                        log("klippy ready; resubscribing")
                        await asyncio.to_thread(self._sync_printer_clock, "klippy ready")
                        await ws.send(SUBSCRIBE)
                if time.monotonic() - self.last_native_poll >= NATIVE_POLL_SECONDS:
                    self.last_native_poll = time.monotonic()
                    await self.poll_native_presence()
                    try:
                        await self.poll_abandoned_starts()
                    except Exception as exc:
                        log(f"abandoned start poll failed: {exc!r}")
                    try:
                        await self.poll_spoolman_new_spools()
                    except Exception as exc:
                        log(f"spoolman new-spool poll failed: {exc!r}")
                    if time.monotonic() - self.last_clock_check >= printer_clock.CHECK_INTERVAL_S:
                        self.last_clock_check = time.monotonic()
                        # Fix the clock first, so the watch below warns only about a clock
                        # Print Handy could not set (busy printer, no DATE_SET, refused).
                        if time.monotonic() - self.last_clock_sync >= CLOCK_SYNC_INTERVAL_S:
                            self.last_clock_sync = time.monotonic()
                            await asyncio.to_thread(self._sync_printer_clock, "poll")
                        try:
                            await self.poll_printer_clock()
                        except Exception as exc:
                            log(f"printer clock poll failed: {exc!r}")
                    try:
                        await self.poll_lane_data()
                    except Exception as exc:
                        log(f"lane_data poll failed: {exc!r}")
                    try:
                        await self.nag_if_due()
                    except Exception as exc:
                        log(f"spool nag failed: {exc!r}")
                    try:
                        self._schedule_light_idle_tick()
                    except Exception as exc:
                        log(f"auto light idle tick failed: {exc!r}")
                    try:
                        self._schedule_next_plate_offer()
                    except Exception as exc:
                        log(f"next plate offer failed: {exc!r}")
                if time.monotonic() - self.maintenance_last_check >= 3600:
                    self.maintenance_last_check = time.monotonic()
                    await maintenance_reminder_tick(self)
                if time.monotonic() - last_tick >= TICK_SECONDS:
                    last_tick = time.monotonic()
                    await asyncio.to_thread(run_announcer, "tick")

    def _beat_once(self):
        """One heartbeat write, from the writer thread (#1020 A3)."""
        tick = getattr(self, "_loop_tick", None)
        lag_ms = int(max(0.0, time.time() - tick) * 1000) if tick else 0
        details = {}
        coverage = _WATCH_COVERAGE.current()
        if coverage is not None:
            details["camera_watch"] = coverage  # #1320: active, or off and why
        healthcheck.beat(moonraker_connected=self.moonraker_connected,
                         state=self.state, loop_lag_ms=lag_ms, **details)

    def _heartbeat_writer(self):
        """The dedicated heartbeat thread: beats even while the loop is blocked."""
        while True:
            try:
                self._beat_once()
            except Exception as exc:
                log(f"heartbeat write failed: {exc!r}")
            time.sleep(healthcheck.HEARTBEAT_S)

    def _start_heartbeat_writer(self):
        if getattr(self, "_heartbeat_thread", None) is None:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_writer, name="listener-heartbeat", daemon=True)
            self._heartbeat_thread.start()

    async def heartbeat(self):
        """Adapted from contributor Docker commit 3bb0984; no printer probe.

        The loop-side companion of the writer thread: it only stamps the
        loop tick (the writer turns the gap into loop_lag_ms) and polls
        the shared debug-mode state on the same cadence.
        """
        poll = None
        while True:
            # Stamped directly, never behind an executor: with the default
            # pool busy, an awaited to_thread here stopped the stamps, the
            # writer reported a growing loop lag, and the supervisor killed
            # a healthy listener.
            self._loop_tick = time.time()
            if poll is None or poll.done():
                # Debug log mode (#478) is shared state this listener
                # observes on its heartbeat tick, like every process;
                # single-flight and never awaited.
                poll = asyncio.ensure_future(asyncio.to_thread(debug_mode.poll))
                poll.add_done_callback(lambda task: task.cancelled() or task.exception())
            await asyncio.sleep(healthcheck.HEARTBEAT_S)

    async def backfill_slot_colors(self):
        """Give every loaded slot a color code before the session starts.

        Best effort and side-effect free on the printer: reads Spoolman and
        the printer's zmod_color report, fills only blank (or previously
        guessed) color codes in this printer's hardware record, never
        overwrites a valid one. Runs at startup and on each reconnect.
        """
        try:
            from . import spool_sync
            from .. import listeners as _listeners
            instance_id = _listeners.instance_printer_id()
            mr = spool_sync.moonraker_for(instance_id) if instance_id else spool_sync.moonraker
            changes = await asyncio.to_thread(spool_sync.backfill_slot_colors, mr=mr, printer_id=instance_id)
            for change in changes:
                note = (" (printer screen differs; /spool sync updates it when the printer is idle)"
                        if change.get("screen") == "needs_sync" else "")
                log(f"slot {change['slot']} color code filled: #{change['hex']} from {change['source']}{note}")
        except Exception as exc:
            log(f"slot color backfill skipped: {exc!r}")

    async def reconcile_spools(self):
        """Rebuild this printer's slot cache from Spoolman's ph_slot assignments (#798).

        Best effort, like the color backfill beside it: runs at startup and
        on each reconnect, migrates the local cache into Spoolman on the
        first run, names conflicts in the log instead of picking silently,
        and never fails the session — a Spoolman that does not answer leaves
        the local record untouched and is logged as such.
        """
        try:
            from . import spool_sync
            from .. import listeners as _listeners
            instance_id = _listeners.instance_printer_id()
            mr = spool_sync.moonraker_for(instance_id) if instance_id else spool_sync.moonraker
            result = await asyncio.to_thread(spool_sync.reconcile, instance_id, mr=mr)
            for line in spool_sync.reconcile_text(result).splitlines():
                log(f"spool reconcile: {line}")
            if result.get("ok"):
                await self._retry_pending_spool_sync()
                await self._settle_spool_question_after_reconcile(mr)
        except Exception as exc:
            log(f"spool reconcile skipped: {exc!r}")

    async def _settle_spool_question_after_reconcile(self, mr):
        """After a reconcile, settle an open spool question whose print is over.

        The printer's job comes from a FRESH print_stats read (the caches
        are empty or stale here, as for the pending sync retry below); an
        unreadable printer leaves the question untouched, since it could
        still be the paused print itself.
        """
        try:
            stats = await self._fresh_print_stats()
            if stats is None:
                return
            state = stats.get("state")
            await self.settle_spool_question(
                state=state, filename=stats.get("filename") or "",
                total_duration=stats.get("total_duration"),
                printing=state in ("printing", "paused"), mr=mr)
        except Exception as exc:
            log(f"spool question check after reconcile skipped: {exc!r}")

    async def _fresh_print_stats(self):
        """This printer's print_stats read now (never the caches), or None when unreadable."""
        from .. import listeners as _listeners
        try:
            instance_id = _listeners.instance_printer_id()
        except Exception:
            instance_id = None
        if instance_id:
            # Per-printer Moonraker, resolved at call time like
            # reconcile_spools' own client (#335), not the base URL
            # captured when print_control was imported.
            from . import spool_sync
            reply = await asyncio.to_thread(
                spool_sync.moonraker_for(instance_id), "GET",
                "/printer/objects/query?print_stats")
        else:
            reply = await asyncio.to_thread(
                print_control._call, "/printer/objects/query?print_stats")
        if not isinstance(reply, dict):
            return None
        stats = ((reply.get("result") or {}).get("status") or {}).get("print_stats")
        return stats if isinstance(stats, dict) else None

    async def _retry_pending_spool_sync(self):
        """A Spoolman probe just succeeded: retry the running job's pending
        active-spool sync (#838). At launch, a Spoolman outage records
        `spoolman_usage_pending` on the job (#387) and nothing read it back,
        so Moonraker charged the previous print's spool for the whole run.
        Fail-soft: any error leaves the record and never breaks reconcile.

        The job is identified from a FRESH print_stats read, never from
        self.last_stats/self.filename: forever() runs reconcile before
        session() populates those caches, so at startup they are empty
        (the retry would always skip a running print) and on reconnect
        they can name the PREVIOUS print (the retry would restore that
        job's spool pointer over a print started while disconnected).
        Only a fresh state of printing/paused with a filename is acted
        on, and this listener's printer identity rides along so the
        retry loads that printer's record (#847)."""
        try:
            # Multi-slot charges a Spoolman outage left pending at print end.
            from . import multislot_usage
            retried = await asyncio.to_thread(
                multislot_usage.retry_pending, base_url=_config.service_url("spoolman_url") or None)
            if retried:
                log(f"multi-slot usage retried for {retried}")
        except Exception as exc:
            log(f"multi-slot usage retry failed: {exc!r}")
        try:
            from ..core import model_job
            from .. import listeners as _listeners
            try:
                instance_id = _listeners.instance_printer_id()
            except Exception:
                instance_id = None
            stats = await self._fresh_print_stats() or {}
            filename = stats.get("filename") or ""
            if stats.get("state") not in ("printing", "paused") or not filename:
                return
            done = (await asyncio.to_thread(model_job.retry_pending_spool_sync, filename,
                                            printer_id=instance_id)
                    if instance_id else
                    await asyncio.to_thread(model_job.retry_pending_spool_sync, filename))
            if done:
                log(f"pending active-spool sync completed for {filename!r}")
        except Exception as exc:
            log(f"pending spool sync retry skipped: {exc!r}")

    async def forever(self):
        delay = RECONNECT_MIN_S
        self.moonraker_connected = False
        self._start_heartbeat_writer()
        beating = asyncio.create_task(self.heartbeat())
        try:
            while True:
                await self.backfill_slot_colors()
                await self.reconcile_spools()
                started = time.monotonic()
                lost = None
                try:
                    await self.session()
                except Exception as exc:
                    lost = exc
                finally:
                    self.moonraker_connected = False
                if time.monotonic() - started >= HEALTHY_SESSION_S:
                    # A session that held up was healthy: this drop is a
                    # fresh one, not the next step of an outage.
                    delay = RECONNECT_MIN_S
                if lost is not None:
                    log(f"connection lost: {lost!r}; retry in {delay}s")
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_S)
        finally:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)


#: Reconnect backoff: doubles per failed connection from RECONNECT_MIN_S up to
#: RECONNECT_MAX_S, and starts again at the minimum once a session stayed up
#: for HEALTHY_SESSION_S.
RECONNECT_MIN_S = 5
RECONNECT_MAX_S = 120
HEALTHY_SESSION_S = 60

#: Longest the capture start may take before the print runs without one.
CAPTURE_BEGIN_TIMEOUT_S = 20


def _install_stack_dump_handler():
    """Dump every thread's stack on SIGUSR1 (#1020 A4).

    The container supervisor sends SIGUSR1 to a listener whose heartbeat went
    stale, just before it restarts it, so the log names the line that hung.
    The shared installer (healthcheck) dumps to stderr and to the state
    file beside the heartbeat; it is also installed by the listeners
    entry point for the other domains, and this call keeps a direct
    ``python -m print_handy.runtime.moonraker_event_listener`` run covered.
    """
    healthcheck.install_stack_dump_handler()


def main():
    if not WS_URL:
        raise SystemExit("services.moonraker_url is not configured (or services.moonraker_ws_url). "
                         "Set it in config/local.yaml or config/local.toml; the listener has nothing to connect to.")
    route = "PRINT_VISION_URL" if os.environ.get("PRINT_VISION_URL") else (_VISION_BASE or "not configured")
    logs.configure()
    _install_stack_dump_handler()
    # The local detector's model is fetched now, in the background, so the
    # first print's first camera check never waits on a 202 MB download.
    _local_vision.prefetch_model()
    log(f"listener starting (Print Handy {__version__}); camera watchdog initialized route={route} model={_VISION_MODEL}")
    asyncio.run(Listener().forever())


if __name__ == "__main__":
    main()
