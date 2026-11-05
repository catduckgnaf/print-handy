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
import contextlib
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

from .. import __version__, ai_routes, debug_mode, go2rtc, healthcheck, logs, printers, settings
from ..core import delivery, filament_color, launch_state, printer_clock, spool_canon, spoolman_client
from . import _config
from . import moonraker_discord_announcements as announcer
from . import discord_print_summary as print_summary
from . import plan_timelapse
from . import timelapse_capture
from . import timelapse_encode
from . import print_control
from . import light_control
from . import visual_watch as _visual_watch
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
DISCORD_TARGET = f"discord:{announcer.DISCORD_CHAT_ID}"
TICK_SECONDS = float(_LISTENER["tick_s"])
LOG = announcer.PROFILE_HOME / "logs" / "moonraker_event_listener.log"
TERMINAL = {"cancelled": "Print cancelled", "error": "Print stopped with an error"}


logger = logging.getLogger(__name__)
_PROBLEM = re.compile(r"\b(failed|failure|error|lost|refused|could not|cannot|unreachable|timed out)\b", re.I)


def log(line):
    """Record one listener event: the legacy log file, and the shared logger (stderr, journald, docker logs).

    A line that reports a failure is logged as a warning, and the traceback of the exception being handled
    when it is called, if any, is kept. Credentials are redacted in both places.
    """
    line = logs.redact(str(line))
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {line}\n")
    logger.log(logging.WARNING if _PROBLEM.search(line) else logging.INFO, "%s", line,
               exc_info=sys.exc_info() if sys.exc_info()[0] else None)


def _platform_name():
    """The standalone bot's chat platform ([bot] platform, #164); 'discord' unless configured."""
    return str(_config.cfg()["bot"].get("platform") or "discord")


def _telegram_standalone():
    return _config.standalone() and _platform_name() == "telegram"


def _telegram_token():
    """The Telegram bot token, from the variable [telegram] token_env names."""
    return _profile_env_value(str(_config.cfg()["telegram"].get("token_env") or "TELEGRAM_BOT_TOKEN"))


def _telegram_chat_id():
    return str(_config.cfg()["telegram"].get("chat_id") or "")


def _matrix_standalone():
    return _config.standalone() and _platform_name() == "matrix"


def _buttonless_standalone():
    """The platform name when standalone posts have no Discord buttons, else None.

    Telegram (#164 stage 2) and Matrix (stage 3) have no Discord-shaped
    button posts: alerts go out as plain text and the operator answers
    with the typed commands the text names.
    """
    if _telegram_standalone():
        return "telegram"
    if _matrix_standalone():
        return "matrix"
    return None


def _matrix_token():
    """The Matrix access token, from the variable [matrix] access_token_env names."""
    return _profile_env_value(str(_config.cfg()["matrix"].get("access_token_env") or "MATRIX_ACCESS_TOKEN"))


def _matrix_homeserver():
    return str(_config.cfg()["matrix"].get("homeserver_url") or "")


def _matrix_room_id():
    return str(_config.cfg()["matrix"].get("room_id") or "")


def post(message):
    """Deliver physical print lifecycle text to the print chat only, with no agent."""
    if _config.standalone():
        return _post_rest(message)
    return _post_rest_discord(message)


def _post_rest(message):
    """Standalone mode: the configured platform's REST sender with its bot token.

    Works with no gateway connected. Discord is the original path; Telegram
    (#164 stage 2) posts text and MEDIA files through telegram_rest, Matrix
    (stage 3) through matrix_rest.
    """
    if _platform_name() == "telegram":
        from print_handy.bot import telegram_rest
        chat = _telegram_chat_id()
        if not chat:
            log("post skipped: telegram.chat_id is not configured")
            return False
        ok, detail = telegram_rest.send(_telegram_token(), chat, message)
        log(f"post rest target=telegram:{chat} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
            f"{'' if ok else ' ' + str(detail)[:200]}")
        return ok
    if _platform_name() == "matrix":
        from print_handy.bot import matrix_rest
        room = _matrix_room_id()
        if not room:
            log("post skipped: matrix.room_id is not configured")
            return False
        ok, detail = matrix_rest.send(_matrix_homeserver(), _matrix_token(), room, message)
        log(f"post rest target=matrix:{room} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
            f"{'' if ok else ' ' + str(detail)[:200]}")
        return ok
    return _post_rest_discord(message)


def _post_rest_discord(message):
    """Discord delivery through the bot's own REST sender with its bot token (#524).

    The one Discord post path in both modes: Hermes mode no longer shells
    out to the `hermes send` CLI. The token and chat id resolve exactly as
    the standalone path resolves them ([discord] token_env / chat_id, via
    the profile env). rest.send refuses honestly when either is missing —
    the failure is logged with its detail and False is returned; there is
    no silent fallback to another transport.
    """
    from print_handy.bot import rest
    ok, detail = rest.send(_bot_token(), announcer.DISCORD_CHAT_ID, message)
    log(f"post rest target={DISCORD_TARGET} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
        f"{'' if ok else ' ' + str(detail)[:200]}")
    return ok


def post_alert(message, kind, filename):
    """Post with Resume/Cancel buttons; fall back to plain text if Discord refuses.

    On Telegram and Matrix (#164 stages 2-3) the Discord-shaped button
    posts do not apply: the alert goes out as plain text and the operator
    answers with the typed commands the text names.
    """
    if _buttonless_standalone():
        return post(message)
    try:
        from . import print_alert_buttons as pab
        if pab.post_with_buttons(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 message, kind, filename):
            log(f"post buttons kind={kind} {message.splitlines()[0][:120]!r}")
            return True
        log("button post failed; falling back to plain text")
    except Exception as exc:
        log(f"button post error: {exc!r}")
    return post(message)


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
                                             spool_question_text(record, nag=nag), record, previous or None)
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


def post_cancel_report(filename):
    """Full Klipper report on any cancel; falls back to the short summary if the report fails."""
    try:
        from . import klipper_report
        text = print_summary.format_event("cancelled", filename) + "\n" + klipper_report.report(filename)
    except Exception as exc:
        log(f"klipper report failed: {exc!r}")
        text = print_summary.format_event("cancelled", filename)
    return post(text)


SCREEN_CANCEL_NATIVE = {"cancel", "cancelled"}
SCREEN_CANCEL_BUSY = {"printing", "paused", "pausing", "error", "busy"}


def native_screen_cancel(prev, now):
    """Stock firmware left a job (printing/paused/error) for cancel or ready."""
    return prev in SCREEN_CANCEL_BUSY and now in SCREEN_CANCEL_NATIVE | {"ready"}


def screen_cancel_reboot(filename, *, status_reader=None, rebooter=None, clearer=None, poster=None):
    """House rule: a cancel from the printer screen force restarts Klipper. Never while printing."""
    from . import print_alert_buttons as pab
    read = status_reader or print_control.status
    say = poster or post
    st = read()
    if st.get("state") == "printing":
        log("screen cancel reboot skipped: printer is printing again")
        return False, "skipped"
    label = print_summary.safe_label(filename) if filename else "the print"
    if st.get("klippy") == "shutdown":
        # The cancel report was just posted; one result line after the restart is enough.
        ok, msg = (clearer or pab.clear_shutdown)()
    else:
        say(f"Screen cancel on {label}. Force restarting the printer now, about two minutes.")
        ok, msg = (rebooter or print_control.force_reboot)()
    log(f"screen cancel reboot ok={ok}: {msg}")
    if ok and clearer is None and rebooter is None and not _buttonless_standalone():  # real run only; tests inject fakes
        with contextlib.suppress(Exception):
            pab.retire_alerts(_bot_token(), filename, "Cleared after the screen cancel; nothing to do.")
    say(msg)
    return ok, msg


def post_shutdown_alert(filename):
    return post_alert(shutdown_alert_text(filename), "shutdown", filename)


def _profile_env_value(name):
    """Read one profile env value. The listener unit loads no EnvironmentFile."""
    return _config.profile_env_value(name)


def _bot_token():
    """The Discord bot token, from the variable [discord] token_env names (DISCORD_BOT_TOKEN by default)."""
    return _profile_env_value(str(_config.cfg()["discord"].get("token_env") or "DISCORD_BOT_TOKEN"))


def roast_enabled(cfg=None):
    """The roast switch: [vision] roast_enabled, on by default.

    Roasts run whenever the switch is on AND an LLM/vision endpoint is
    available (generate_roast's own checks below); the switch is the
    operator's way to turn them off without losing the camera watchdog.
    """
    vision = (cfg or _config.cfg()).get("vision") or {}
    return bool(vision.get("roast_enabled", True))


def generate_roast(title, summary=""):
    """Generate a quick 1-sentence roast via Luna LLM for the bot persona. Best-effort."""
    title = str(title or "").strip()
    if not title:
        return ""
    if not roast_enabled():
        log("roast skipped: vision.roast_enabled is false")
        return ""
    # The small AI tier (#818): [ai.small] when set, else services.vision_chat_url
    # with the vision roast model and CLIPROXY_API_KEY, as before.
    compat = ai_routes.Compat(_config.service_url("vision_chat_url"),
                              str(_config.cfg()["vision"]["roast_model"]),
                              _profile_env_value("CLIPROXY_API_KEY"), timeout_s=4)
    if not ai_routes.configured("small", compat=compat):
        log("roast skipped: services.vision_chat_url is not configured")
        return ""
    if not ai_routes.ready("small", compat=compat):
        return ""
    prompt = f"Model: {title}"
    if summary:
        prompt += f" ({summary})"
    try:
        res, served_by = ai_routes.call(
            "small",
            [{"role": "system", "content": f"You are {_config.bot_name()}, a witty 3D printer assistant who gently roasts 3D prints. Give ONE short funny sentence roasting this model. Keep it under 20 words. No quotes, no hashtags, no introductory filler."},
             {"role": "user", "content": prompt}],
            compat=compat, purpose="roast", max_tokens=50, temperature=0.8)
        log(f"roast written by {served_by}")
        return res.strip("\"'").strip()
    except Exception:
        return ""


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
        which = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "The active slot"
        into = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the active slot"
        spool = f" (spool #{spool_id})" if spool_id else ""
        # The spool question is only opened when a slot is known (open_question), so
        # promise it only then; otherwise this alert and the follow-up notice contradict.
        asked = slot in (1, 2, 3, 4)
        extra = (f"Filament runout: {which}{spool} ran out of filament.\n"
                 "To finish this print:\n"
                 f"1. Load the replacement filament into {into} at the printer.\n"
                 + ("2. Answer the spool question below (Existing spool or New spool) so the replacement "
                    "spool is the one registered for that slot — filament usage stays paused for every "
                    "spool until you answer.\n3. " if asked else "2. ")
                 + "Tap Resume print below (or type /print-resume) once the filament is loaded. "
                 "The print resumes only when you press it; nothing resumes on its own.")
    else:
        extra = (f"Paused at {where}. " if where else "") + "Fix it at the printer, then tap Resume or Cancel below (or type /print-resume or /print-stop)."
    return print_summary.format_event("paused", filename, extra=extra)


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


def classify_pause(message, where=""):
    """A pause is a filament runout only when the alert text says so.

    Moonraker's print_stats payload has no pause cause. Ordinary pauses stay ordinary.
    """
    text = f"{message or ''} {where or ''}".casefold()
    return "runout" if "runout" in text or "filament sensor" in text else "other"


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


def spool_question_text(record, nag=False):
    slot = record.get("slot")
    where = f"IFS slot {slot}" if slot in (1, 2, 3, 4) else "the active IFS slot"
    prior = record.get("prior_spool_id")
    why = ("ran out" if record.get("cause") == "runout"
           else "was unloaded and reloaded mid-print")
    head = (f"**Still waiting (reminder {record.get('nags', 0)})**: which spool is in {where}?" if nag
            else f"**Spool check:** the filament in {where} {why}.")
    same = ("\n• `same` if you put spool #%s back" % prior) if record.get("cause") == "swap" and prior else ""
    return (f"{head}\nFilament usage is **paused for every spool** until you answer, "
            f"so neither the old nor the new spool is charged.\nReply with:\n"
            f"• a spool number from Spoolman, e.g. `12`{same}\n"
            f"• or a new spool: `brand, material, color, grams` e.g. `Sunlu, PLA+, White, 1000`\n"
            f"• or `cancel the print` (or the **Cancel print** button) to end the print and close this question\n"
            "`/spool list` shows what is on the shelf. Answering never touches the printer; "
            "cancelling is the one reply that does.")


def clear_runout_pause(path=None):
    target = Path(path or RUNOUT_PAUSE_STATE)
    if target.exists():
        target.unlink()


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


def _with_completion_roast(message, completion_message):
    """The completion post with its one roast, placed under the completion text.

    The roast rides the post that carries the timelapse, so it lands after
    the video is ready rather than on the start post. It goes before any
    MEDIA: attachment lines, which must stay on their own lines. Best-effort:
    no recorded title, a disabled switch or a model failure leaves the post
    exactly as it was.
    """
    try:
        title, summary = announcer.pending_roast_subject(announcer.load_state(), completion_message)
        if not title or not str(message).startswith(completion_message):
            return message
        roast = generate_roast(title, summary)
    except Exception as exc:
        log(f"completion roast skipped: {exc!r}")
        return message
    if not roast:
        return message
    return f"{completion_message}\n\n{roast}{message[len(completion_message):]}"


def _post_completion(message, completion_message):
    """Acknowledge only a matching pending notice after transport success."""
    delivered = post(_with_completion_roast(message, completion_message))
    if delivered:
        state = announcer.load_state()
        if announcer.pending_completion_message(state) == completion_message:
            announcer.clear_pending_roast_subject(state)
            announcer.clear_pending_completion_message(state, completion_message)
            announcer.clear_pending_timelapse_job_key(state)
            announcer.clear_pending_timelapse_routing(state)
            state["announced_completion"] = True
            announcer.save_state(state)
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
        _post_completion(completion_message, completion_message)
        with contextlib.suppress(Exception):
            plan_timelapse.prune()  # abandoned plans never reach a final post
        return
    stored = plan_timelapse.clips(plan)
    expected = plan_timelapse.expected_clips(plan)
    # Partial when any plate's clip is missing, not only the final plate's.
    partial = stage == "partial" or clip is None or (expected is not None and len(stored) < expected)
    try:
        if not stored:
            _post_completion(completion_message, completion_message)
            return
        note = f"{completion_message}\nPartial timelapse: {len(stored)} plate(s) so far." if partial else completion_message
        try:
            combined = plan_timelapse.combine(plan)
        except Exception as exc:
            log(f"timelapse combine failed for plan {plan!r}: {exc!r}")
            _post_completion(completion_message, completion_message)
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
        if not _post_completion(message, completion_message):
            log(f"combined timelapse attach failed for plan {plan!r}; posting text only")
            _post_completion(f"{note}\n{_timelapse_fallback_text(message, combined=True)}", completion_message)
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
            if not _post_completion(message, completion_message):
                log(f"captured timelapse attach failed for {job_key!r}; posting text only")
                _post_completion(f"{completion_message}\n{_timelapse_fallback_text(message, captured.name)}",
                                 completion_message)
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
                    completion_message)
                return
            if go2rtc.timelapse_mode(_config.cfg()) == "off":
                log(f"no timelapse for {job_key!r}: timelapse mode is off")
                _post_completion(completion_message, completion_message)
            else:
                log(f"no timelapse for {job_key!r}: {_capture_outcome_text()}; "
                    "printer-rendered path is off in container mode")
                _post_completion(f"{completion_message}\nTimelapse not attached: "
                                 f"{_capture_outcome_text()}.", completion_message)
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
                    completion_message)
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
                _post_completion(completion_message, completion_message)
            elif outcome.state == announcer.ARCHIVE_FAILED:
                log(f"no timelapse for {job_key!r}: {capture_text}; printer-rendered archive "
                    f"failed: {outcome.detail}")
                _post_completion(f"{completion_message}\nTimelapse not attached: the printer's "
                                 "rendered video was found, but archiving it failed "
                                 "(see the listener log).", completion_message)
            else:
                log(f"timelapse not rendered for {job_key!r} within the wait"
                    + (f" ({outcome.detail})" if outcome.detail else ""))
                _post_completion(f"{completion_message}\nTimelapse not attached: the printer's "
                                 "rendered video didn't arrive within the wait "
                                 "(see the listener log).", completion_message)
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
        if not _post_completion(message, completion_message):
            # Never lose the completion notice because the platform refused the attachment.
            log(f"timelapse attach failed for {job_key!r}; posting text only")
            _post_completion(f"{completion_message}\n{_timelapse_fallback_text(message, Path(str(archived)).name)}", completion_message)
    except Exception as exc:
        log(f"timelapse postprocess failed for {job_key!r}: {exc!r}")
        _post_completion(completion_message, completion_message)


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
    platform = _platform_name() if _config.standalone() else "discord"
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
    platform = _platform_name() if _config.standalone() else "discord"
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
START_CLAIMS = _config.state_file("PRINT_HANDY_START_CLAIMS", "print_handy_start_claims.json")
START_CLAIM_TTL_S = float(_LISTENER["start_claim_ttl_s"])
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
    operator's answer; nothing here moves the printer. Returns False when
    the screen spool is the one that just ran out — that spool cannot
    answer its own runout question, exactly as a chat reply cannot.
    """
    from . import spool_sync
    client = client or spool_sync.spoolman
    mr = mr or spool_sync.moonraker
    cause = (context or {}).get("cause")
    prior = (context or {}).get("prior_spool_id")
    if cause == "runout" and prior is not None and int(prior) == int(spool_id):
        return False
    spool_sync.bind_slot(int(slot), int(spool_id), client=client, mr=mr, state_path=state_path,
                         prior_disposition="depleted" if cause == "runout" else "shelf",
                         note="Bound from HelixScreen during a paused runout")
    spool_sync.resume_usage(int(spool_id), mr=mr)
    clear_runout_pause()
    return True


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


def start_claim_time(filename, now=None):
    """When print-now claimed this job's start announcement, or None when unclaimed/expired."""
    now = time.time() if now is None else now
    try:
        claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    # print-now claims the uploaded name (X.gcode.3mf); the printer reports the path it runs, which
    # for a ZMod 3MF is 3mf_print/X.gcode. Matching only the basename missed that and posted a
    # second "Print started" beside the intake's (2026-10-31, Print #64).
    from ..core.launch_state import same_remote
    name = str(filename or "")
    for key in (Path(name).name, *claims):
        ts = claims.get(key)
        if (isinstance(ts, (int, float)) and 0 <= now - ts < START_CLAIM_TTL_S
                and (key == Path(name).name or same_remote(name, key) or same_remote(Path(name).name, key))):
            return ts
    return None


def start_claimed(filename, now=None):
    """True when model_job.py print-now already owns this job's start announcement."""
    return start_claim_time(filename, now) is not None


def claimed_start_filenames(now=None):
    """The unexpired claimed start files, as print-now recorded them (the uploaded names)."""
    now = time.time() if now is None else now
    try:
        claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(claims, dict):
        return []
    return [name for name, ts in claims.items()
            if isinstance(ts, (int, float)) and 0 <= now - ts < START_CLAIM_TTL_S]


def release_start_claim(filename):
    """Drop this file's start claim (the listener's counterpart of model_job's release).

    Matching follows start_claim_time: print-now claims the uploaded name, the printer
    reports its own path for the same file."""
    try:
        claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(claims, dict):
        return
    from ..core.launch_state import same_remote
    name = str(filename or "")
    kept = {key: ts for key, ts in claims.items()
            if not (key == Path(name).name or same_remote(name, key) or same_remote(Path(name).name, key))}
    if len(kept) != len(claims):
        try:
            tmp = START_CLAIMS.with_suffix(".tmp")
            tmp.write_text(json.dumps(kept))
            os.replace(tmp, START_CLAIMS)
        except OSError:
            pass


# A claimed start that never became a print: print_stats reads standby/error with both heater
# targets at 0, sustained this long, before the listener calls it abandoned (#836). Shorter
# cold reads are just the gap between the start card and warmup.
ABANDONED_START_COLD_S = 60.0
ABANDONED_START_MESSAGE = ("The printer stopped preparing this job without printing. "
                           "Check the IFS slot / HelixScreen.")


class AbandonedStartWatch:
    """Tracks claimed starts that have not become prints, one cold window per claimed file.

    Pure observation in, one decision out: `observe` returns True exactly once per claim for
    a claimed file whose printer reads standby/error with both heater targets at 0 for
    ABANDONED_START_COLD_S. Each outstanding claim keeps its own window: the poll feeds every
    claim the same printer read (an empty reported filename matches them all), and one claim's
    reads must never reset another claim's window (#848 — with a single shared window, two
    outstanding claims alternately reset each other and neither was ever reported). Any
    heater target or an unreadable target restarts that claim's window only — neither is
    evidence a start was abandoned. A printing/paused read retires the claim's window
    outright: that start took, so its later idle reads are a finished print's story, not an
    abandoned start's. `retire` drops a claim the poll has released, and `retain` drops
    windows for claims that are no longer outstanding, so a later claim of the same file
    always starts a fresh window.
    """

    def __init__(self):
        self._claims = {}  # filename -> {"cold_since": float | None, "announced": bool}

    def observe(self, filename, state, extruder_target, bed_target, now=None):
        if not filename:
            return False
        now = time.time() if now is None else now
        if state in ("printing", "paused"):
            self._claims.pop(filename, None)  # this claim's start took: retire its window
            return False
        claim = self._claims.setdefault(filename, {"cold_since": None, "announced": False})
        cold = (state in ("standby", "error")
                and extruder_target is not None and bed_target is not None
                and float(extruder_target) == 0.0 and float(bed_target) == 0.0)
        if not cold:
            claim["cold_since"] = None
            return False
        if claim["cold_since"] is None:
            claim["cold_since"] = now
            return False
        if not claim["announced"] and now - claim["cold_since"] >= ABANDONED_START_COLD_S:
            claim["announced"] = True
            return True
        return False

    def retire(self, filename):
        """Drop one claim's window (it fired, or its claim was released another way)."""
        self._claims.pop(filename, None)

    def retain(self, filenames):
        """Drop the windows of files that are no longer outstanding claims."""
        outstanding = set(filenames)
        for filename in list(self._claims):
            if filename not in outstanding:
                del self._claims[filename]


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
    text += "\nWarmup complete, now printing."
    message_id = None
    channel_id = None
    try:
        from . import print_next
        message_id, channel_id = print_next.take_start_message_target(filename)
    except Exception as exc:
        log(f"start message lookup failed file={filename!r}: {exc!r}")
    if not message_id:
        # The intake already posted the one "Print started" message; a second line after it is noise.
        log(f"no recorded start message; warmup done silently file={filename!r}")
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
    return _config.require(_config.service_url("vision_chat_url"), "services.vision_chat_url")


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
_VISION_MODEL = str(_config.cfg()["vision"]["model"])
_VISION_CONTEXT = (
    "Printer: Flashforge AD5X, enclosed, smooth PEI plate. "
    "Reply with JSON only, no markdown: {\"verdict\":\"ok|air|spaghetti|detached|empty_idle|uncertain\",\"reason\":\"short\"}. "
    "ok means plastic is on the bed and printing normally. "
    "air means the bed is empty while a print should be there. "
    "spaghetti means a tangled failed extrusion. "
    "detached means a printed part has come off the plate. "
    "empty_idle means the printer is idle and the bed is empty. "
    "uncertain means the frame is not clear enough to judge."
)


_VISION_CONTEXT = "Printer: Flashforge AD5X, enclosed, smooth PEI plate. Reply JSON verdict ok|air|spaghetti|detached|empty_idle|uncertain. "


def _fetch_snapshot(url):
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=8) as response:
        image = response.read(8 * 1024 * 1024 + 1)
    if len(image) > 8 * 1024 * 1024 or not image.startswith(bytes((0xff, 0xd8))):
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




_VISION_CONTEXT = "Printer: Flashforge AD5X, enclosed, smooth PEI plate. Reply JSON verdict ok|air|spaghetti|detached|empty_idle|uncertain. "


# The camera-watch timer beat (#913): fine enough for the 60 s empty-bed
# re-check and the 120 s early-layer interval, cheap when nothing is due.
VISUAL_TIMER_S = 15


def _visual_verdict(image, prompt, streak=None, local_fallback=False):
    """The shared verdict call (visual_watch.visual_verdict) with this listener's values.

    ``streak`` is the print watch's per-print local-detector escalation
    state (#455); only visual_check passes it — the auto light-off idle
    checks judge single frames, so they leave it None. ``local_fallback``
    is true inside a risky print's early-layer window (#913): a failed
    endpoint then falls back to the local detector when its model exists.
    """
    return _visual_watch.visual_verdict(
        image, prompt, chat_url=_vision_chat_url, model=_VISION_MODEL,
        api_key=lambda: _profile_env_value("CLIPROXY_API_KEY"), context=_VISION_CONTEXT,
        local_streak=streak, local_fallback=local_fallback)


def _auto_pause_early(filename, identity):
    """Send PAUSE through the alert pause path (#837) for the early-layer auto-pause (#913).

    The same code the camera alert's Pause button runs: status readback,
    a same-file check, one /printer/print/pause, then a paused readback.
    It never cancels. Returns the pause path's reply text.
    """
    from . import print_alert_buttons as pab
    alert = {"kind": "visual", "filename": filename, "identity": identity}
    return pab.handle_click("pause", alert)


def _vision_ready():
    """Whether the camera watch has a vision endpoint to judge frames with (visual_watch.vision_ready)."""
    return _visual_watch.vision_ready(lambda: _profile_env_value("CLIPROXY_API_KEY"), log)


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


def _with_printer_name(text):
    """Append the serving printer's name on a multi-printer roster (#536).

    The watchdog texts are built here, not via format_event, so they take
    the same trailing tag explicitly; a one-printer install is unchanged.
    """
    name = print_summary.announcement_printer_name()
    return f"{text}\nPrinter: {name}" if name else text


def _visual_alert(filename, image, reason, identity=None):
    # #317: a watchdog alert is blooper material; record() never raises.
    print_markers.record(filename, print_markers.WATCHDOG_ALERT, detail=reason)
    try:
        from ..notify import emit_queued
        emit_queued("watchdog_offer", reason)
    except Exception:
        pass
    target = Path(os.environ.get("PRINT_WATCH_FRAME_DIR") or _config.cache_dir() / "print-watch")
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{int(time.time())}-{Path(filename or 'print').stem}.jpg"
    path.write_bytes(image)
    text = _with_printer_name(f"Print camera check: {clean(filename)}\n{reason}")
    if _buttonless_standalone():
        post(text + f"\n{path}\nMEDIA:{path}")
        return path
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_alert(_bot_token(), announcer.DISCORD_CHAT_ID,
                                 text, filename, path, identity=identity):
            log(f"post visual alert {clean(filename)!r}")
            return path
        log("visual button post failed; falling back to text")
    except Exception as exc:
        log(f"visual button post error: {exc!r}")
    post(text + f"\n{path}\nMEDIA:{path}")
    return path


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
    if _buttonless_standalone():
        post(text)
        return
    try:
        from . import print_alert_buttons as pab
        if pab.post_visual_text_alert(_bot_token(), announcer.DISCORD_CHAT_ID,
                                      text, filename, identity=identity):
            log(f"post visual text alert {clean(filename)!r}")
            return
        log("visual text alert button post failed; falling back to text")
    except Exception as exc:
        log(f"visual text alert post error: {exc!r}")
    post(text)


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
    where = f" (slot {slot})" if slot else ""
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
        self.pause_context = None
        self.native_error_alerted = load_native_error_alerted()
        self.display_off = None  # live ZMod display mode from the last presence poll; None until read
        self.presence_failure = None  # last presence poll failure, logged once until it changes
        self.capture_session = None  # #246 stage 2: active timelapse_capture session, if any
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
                                            target("extruder"), target("heater_bed")):
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
        if time.time() - float(record.get("last_nag_at") or 0) < SPOOL_NAG_SECONDS:
            return
        record["nags"] = int(record.get("nags") or 0) + 1
        record["last_nag_at"] = time.time()
        atomic_write_json(RUNOUT_PAUSE_STATE, record)
        await asyncio.to_thread(post_spool_question, record, True)

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
                    await asyncio.to_thread(post_cancel_report, self.filename)
                    self.start_screen_cancel_reboot(f"native {self.prev_native}->{native} during emergency stop")
            self.prev_native = native
            if self.state in ("printing", "paused"):
                key = native_error_key(detail) if detail is not None else None
                if key and key != self.native_error_alerted:
                    self.native_error_alerted = key
                    save_native_error_alerted(key)
                    log(f"native screen error {key} file={self.filename!r}")
                    await asyncio.to_thread(post, native_error_text(self.filename, key))
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
                f"**Filament failover:** the printer switched from IFS slot {old} to slot {new} "
                f"for {clean(self.filename or 'this print')}, but filament usage could not be moved to "
                f"spool #{target} ({exc}). Spool #{charged} may keep being charged."))
            return
        await asyncio.to_thread(post, (
            f"**Filament failover:** slot {old} ran out and the printer switched to slot {new} on its own. "
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

    async def upgrade_paused_runout(self, slot):
        """A paused print's slot emptied after the pause was posted as generic (#442).

        The pause alert already went out without a cause; do not leave the
        runout at a log line. Re-post as a runout alert (with the same
        Resume/Cancel buttons a paused alert carries) and open the spool
        question, exactly as if the pause had been classified runout at once.
        """
        self.last_pause_was_runout = True
        spool_id = _mapped_spool(slot) if slot else None
        await asyncio.to_thread(post_alert, paused_message(self.filename, runout=True,
                                                           slot=slot, spool_id=spool_id),
                                "paused", self.filename)
        self.pause_context = await self.open_question(slot, "runout")

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
        image = None
        try:
            image = await asyncio.to_thread(_camera_snapshot_bytes)
            if await asyncio.to_thread(_visual_watch.frame_dark, image):
                await self._dark_frame_check(reason, layer)
                return
            prior = self.visual_frames.get(self.filename)
            changed = None if prior is None else await asyncio.to_thread(_frame_changed, prior, image)
            snapshot_before = self.visual_watch.last_snapshot_layer
            stall = (reason in {"layer_progress", "periodic"} and changed is False
                     and isinstance(layer, int) and layer >= 5
                     and snapshot_before and layer - snapshot_before >= self.visual_watch.profile["stall_layers"]
                     and self.visual_watch.alerted_stall_layer is None)
            if stall:
                reason = "stalled_layers"
                self.visual_watch.alerted_stall_layer = layer
            # A layer-progress probe that is not a stall must not move the
            # periodic clock or replace the baseline frame.
            if reason != "layer_progress":
                self.visual_frames[self.filename] = image
                self.visual_watch.record(layer, time.time(), image)
            # A layer-progress probe that is not a stall adopts the new frame
            # as the baseline without moving the periodic clock.
            if reason == "layer_progress":
                self.visual_frames[self.filename] = image
                self.visual_watch.last_snapshot = image
                if isinstance(layer, int):
                    self.visual_watch.last_snapshot_layer = max(self.visual_watch.last_snapshot_layer, layer)
                    self.visual_watch.progress_seen_layer = layer
                self.visual_watch.last_snapshot_layer = max(self.visual_watch.last_snapshot_layer, layer or 0)
                self.visual_watch.progress_seen_layer = layer or 0
                log(f"visual check file={self.filename!r} reason={reason} layer={layer} changed={changed} verdict=skipped")
                return
            prompt = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"
            early = self.visual_watch.in_early_window(layer)
            verdict, detail = await asyncio.to_thread(_visual_verdict, image, prompt, self.local_streak, early)
            suspicious = verdict in {"air", "spaghetti", "detached", "uncertain", "no", "unsafe"}
            if verdict == "empty_idle" and self.state == "printing":
                # #913: print_stats says printing, so an empty bed is not a
                # healthy answer. Re-check within 60 s; a second empty-bed
                # verdict on that re-check alerts.
                if reason == "empty_idle_recheck":
                    suspicious = True
                    detail = (detail or "") + " (empty bed again while the printer reports printing)"
                else:
                    self.visual_watch.note_empty_idle(time.time())
                    log(f"visual check file={self.filename!r} empty_idle while printing; re-check in "
                        f"{_visual_watch.EMPTY_IDLE_RECHECK_S}s")
            streak = self.visual_watch.note_verdict(verdict, layer)
            if reason == "stalled_layers" and changed is False:
                # A healthy vision verdict outranks the pixel diff.
                suspicious = verdict not in VISUAL_HEALTHY_VERDICTS
            if verdict == "uncertain":
                suspicious = True
            log(f"visual check file={self.filename!r} reason={reason} layer={layer} changed={changed} verdict={verdict} detail={detail!r}")
            if _verdict_says_dark(verdict, detail):
                # A dark feed frame_dark's thresholds let through: a camera
                # fact, not a print alert. Its stillness is no stall, so
                # neither the stall nor the dark baseline is kept.
                if stall:
                    self.visual_watch.alerted_stall_layer = None
                if prior is None:
                    self.visual_frames.pop(self.filename, None)
                else:
                    self.visual_frames[self.filename] = prior
                await self._dark_frame_check(reason, layer)
                return
            if suspicious and image is not None:
                from . import print_alert_buttons as pab
                if await asyncio.to_thread(pab.visual_muted, self.filename, self._visual_identity()):
                    log(f"visual alert suppressed for muted print {self.filename!r}")
                else:
                    pause_now = (streak >= _visual_watch.AUTO_PAUSE_STREAK and not self.visual_watch.auto_paused
                                 and _visual_watch.watch_settings(_config.cfg())["auto_pause_early"])
                    text = f"{reason} at layer {layer or 'unknown'}: {verdict}; {detail or 'visual check needs operator review'}"
                    if pause_now:
                        text += (f"\nAuto-pause (visual_watch.auto_pause_early): {streak} suspicious checks in a row "
                                 "in the early layers, so I am pausing the print. It is not cancelled.")
                    await asyncio.to_thread(_visual_alert, self.filename, image, text,
                                            identity=self._visual_identity())
                    if pause_now:
                        # #913: the only automatic printer action, opt-in, once per print.
                        self.visual_watch.auto_paused = True
                        log(f"auto-pause early layers file={self.filename!r} layer={layer} streak={streak}: sending PAUSE")
                        reply = await asyncio.to_thread(_auto_pause_early, self.filename, self._visual_identity())
                        log(f"auto-pause early layers file={self.filename!r}: {reply}")
                        await asyncio.to_thread(post, _with_printer_name(
                            f"Auto-pause: {clean(self.filename)}\n{reply}"))
        except Exception as exc:
            log(f"visual check failed file={self.filename!r} reason={reason}: {exc!r}")
            if image is not None:
                try:
                    from . import print_alert_buttons as pab
                    if await asyncio.to_thread(pab.visual_muted, self.filename, self._visual_identity()):
                        log(f"visual alert suppressed for muted print {self.filename!r}")
                    else:
                        await asyncio.to_thread(
                            _visual_alert, self.filename, image,
                            f"{reason} at layer {layer or 'unknown'}: the vision check could not be completed, "
                            "so treat this as unreviewed and check the print on the printer screen",
                            identity=self._visual_identity())
                except Exception as alert_exc:
                    log(f"visual alert failed file={self.filename!r}: {alert_exc!r}")
            else:
                # The snapshot itself failed, so there is no image to
                # attach — but the operator still gets an alert: a dead
                # camera must not silently leave the print unmonitored.
                try:
                    from . import print_alert_buttons as pab
                    if await asyncio.to_thread(pab.visual_muted, self.filename, self._visual_identity()):
                        log(f"visual alert suppressed for muted print {self.filename!r}")
                    else:
                        await asyncio.to_thread(
                            _visual_alert_text, self.filename,
                            f"{reason} at layer {layer or 'unknown'}: the camera snapshot could not be captured, "
                            "so this print is unmonitored until the camera answers again; check the camera",
                            identity=self._visual_identity())
                except Exception as alert_exc:
                    log(f"visual alert failed file={self.filename!r}: {alert_exc!r}")

    async def on_status(self, print_stats, reason):
        self.last_stats.update(print_stats)
        new_state = print_stats.get("state", self.state)
        if "filename" in print_stats:
            filename = print_stats.get("filename")
            if filename and filename != self.filename:
                self.filename = filename
                self.visual_watch.reset()
                self.local_streak.reset()
                self.visual_frames.pop(filename, None)
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
            self._capture_begin(identity)
            # #913: pick this print's watch schedule before the first check.
            self.visual_watch.reset()
            self.local_streak.reset()
            self._apply_watch_mode()
            # #516: auto light-on at a fresh print start (a resume is not
            # a start; the light never went off during a pause).
            await self.light_print_started()
        if new_state == "printing" and _config.cfg()["visual_watch"]["enabled"]:
            layer = _visual_layer(self.last_stats)
            reason_due = self.visual_watch.due(layer, time.time(), snapshot_changed=None)
            if reason_due and _vision_ready():
                self.last_visual_task = asyncio.create_task(self.visual_check(reason_due, layer))
        if old is not None and old != "printing" and new_state == "printing":
            # Claimed starts already have the intake's one post; unclaimed starts (printer screen,
            # other tools) get exactly one canonical post here.
            if old == "paused":
                log(f"resume, no start post: {self.filename!r}")
                self.last_pause_was_runout = False
            else:
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
                # The live view's auto-start trigger (#311 stage 4):
                # fail-soft, and a no-op unless auto_start_on_print is on.
                await asyncio.to_thread(_live_auto_on_print_start)
        elif old == "printing" and new_state == "paused":
            self.last_pause_was_runout, slot = await self.classify_runout()
            if self.last_pause_was_runout:
                spool_id = _mapped_spool(slot) if slot else None
                await asyncio.to_thread(post_alert, paused_message(self.filename, runout=True,
                                                                   slot=slot, spool_id=spool_id),
                                        "paused", self.filename)
                self.pause_context = await self.open_question(slot, "runout")
            else:
                self.pause_context = None
                await asyncio.to_thread(post_alert, paused_message(self.filename), "paused", self.filename)
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
                task = asyncio.create_task(asyncio.to_thread(post_cancel_report, self.filename))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
                from . import klipper_report
                if new_state == "cancelled" and not klipper_report.cancelled_by_bot():
                    self.start_screen_cancel_reboot("moonraker cancelled")
            elif new_state != "complete":
                await asyncio.to_thread(post, print_summary.format_event(new_state, self.filename))
        ended = new_state in ("complete", "cancelled", "error", "standby") and old in ("printing", "paused")
        if ended:
            # Close the capture before the announcer: its timelapse
            # postprocess only finds a capture whose manifest is no longer
            # "capturing", and would otherwise attach the previous job's.
            self._capture_end(new_state)
        await asyncio.to_thread(run_announcer, f"{old}->{new_state}")
        if ended:
            await self._print_ended(new_state)

    async def _print_ended(self, new_state):
        """End-of-print cleanup, for a terminal transition or a Klipper shutdown."""
        self.visual_watch.reset()
        self.local_streak.reset()
        self.visual_frames.pop(self.filename, None)
        self._capture_end(new_state)
        self.print_identity = None
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
            if not _vision_ready():
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
            if not _vision_ready():
                policy.note_check(now)
                return
            target = await self._light_target_object()
            if not target:
                policy.note_check(now)
                return
            image = await asyncio.to_thread(_camera_snapshot_bytes)
            if await asyncio.to_thread(_visual_watch.frame_dark, image):
                verdict, detail = "uncertain", "camera frame completely dark"
            else:
                verdict, detail = await asyncio.to_thread(_visual_verdict, image, _AUTO_LIGHT_PROMPT)
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

    async def offer_next_plate_if_clear(self):
        """One empty-bed look for a queued plate. Posts an offer. Starts nothing.

        Uses the same snapshot and verdict words as the auto-light check (#516).
        No camera, a dark frame, or no queued plate: no offer and no invented bed.
        """
        from . import next_plate_offer
        if self.state not in _AUTO_LIGHT_IDLE_STATES:
            return
        if not next_plate_offer.due():
            return
        if not _vision_ready():
            next_plate_offer.observe(None)
            return
        image = await asyncio.to_thread(_camera_snapshot_bytes)
        if not image or await asyncio.to_thread(_visual_watch.frame_dark, image):
            next_plate_offer.observe(None)
            return
        verdict, detail = await asyncio.to_thread(_visual_verdict, image, _AUTO_LIGHT_PROMPT)
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

    def _capture_begin(self, job_key):
        """Open a capture session for the job that just started. Never raises."""
        self._capture_end("superseded")
        try:
            self.capture_session = timelapse_capture.begin(
                job_key, self.filename, data=_config.cfg(), log=log)
        except Exception as exc:  # begin() already fails soft; belt and braces
            self.capture_session = None
            log(f"timelapse capture begin failed file={self.filename!r}: {exc!r}")

    def _capture_end(self, state):
        """Close the active capture session, if any. Never raises."""
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
            why = _visual_watch.risky_job(context)
        except Exception as exc:
            log(f"camera watch mode lookup failed file={self.filename!r}: {exc!r}")
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
        if self.state != "printing" or not _config.cfg()["visual_watch"]["enabled"]:
            return None
        task = self.last_visual_task
        if task is not None and not task.done():
            return None
        layer = _visual_layer(self.last_stats)
        reason = self.visual_watch.tick(layer, time.time() if now is None else now)
        if reason and _vision_ready():
            self.last_visual_task = asyncio.create_task(self.visual_check(reason, layer))
            return reason
        return None

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
                                                  settings.current()["discord"]["timezone"], log=log)
            if res.startswith("failed"):
                self.clock_sync_retry_at = time.monotonic() + CLOCK_SYNC_BACKOFF_S
            if res not in ("ok", "off", "skipped:printer not idle"):
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
                                if reason_due and _vision_ready():
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
                        await self.offer_next_plate_if_clear()
                    except Exception as exc:
                        log(f"next plate offer failed: {exc!r}")
                if time.monotonic() - self.maintenance_last_check >= 3600:
                    self.maintenance_last_check = time.monotonic()
                    await maintenance_reminder_tick(self)
                if time.monotonic() - last_tick >= TICK_SECONDS:
                    last_tick = time.monotonic()
                    await asyncio.to_thread(run_announcer, "tick")

    async def heartbeat(self):
        """Adapted from contributor Docker commit 3bb0984; no printer probe."""
        while True:
            try:
                await asyncio.to_thread(healthcheck.beat,
                                        moonraker_connected=self.moonraker_connected,
                                        state=self.state)
            except Exception as exc:
                log(f"heartbeat write failed: {exc!r}")
            try:
                # Debug log mode (#478) is shared state this listener
                # observes on its heartbeat tick, like every process.
                await asyncio.to_thread(debug_mode.poll)
            except Exception:
                pass
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
        except Exception as exc:
            log(f"spool reconcile skipped: {exc!r}")

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
            from ..core import model_job
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
            stats = (((reply.get("result") or {}).get("status") or {}).get("print_stats")
                     if isinstance(reply, dict) else None) or {}
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
        delay = 5
        self.moonraker_connected = False
        beating = asyncio.create_task(self.heartbeat())
        try:
            while True:
                await self.backfill_slot_colors()
                await self.reconcile_spools()
                try:
                    await self.session()
                except Exception as exc:
                    log(f"connection lost: {exc!r}; retry in {delay}s")
                finally:
                    self.moonraker_connected = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
        finally:
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)


def main():
    if not WS_URL:
        raise SystemExit("services.moonraker_url is not configured (or services.moonraker_ws_url). "
                         "Set it in config/local.yaml or config/local.toml; the listener has nothing to connect to.")
    route = "PRINT_VISION_URL" if os.environ.get("PRINT_VISION_URL") else (_VISION_BASE or "not configured")
    logs.configure()
    log(f"listener starting (Print Handy {__version__}); camera watchdog initialized route={route} model={_VISION_MODEL}")
    asyncio.run(Listener().forever())


if __name__ == "__main__":
    main()
