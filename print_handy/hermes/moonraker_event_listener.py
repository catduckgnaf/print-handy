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

from .. import __version__, debug_mode, go2rtc, healthcheck, logs, printers
from ..core import delivery, filament_color, spoolman_client
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
            + "\nAn emergency stop can't be resumed. Clear the bed, then tap Cancel print "
              "(it clears the stop and ends the job, no motion), or start over with /print-again.")


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
    key = _profile_env_value("CLIPROXY_API_KEY")
    chat_url = _config.service_url("vision_chat_url")
    if not chat_url:
        log("roast skipped: services.vision_chat_url is not configured")
        return ""
    if not key and not _visual_watch.keyless_ok(chat_url):
        return ""
    prompt = f"Model: {title}"
    if summary:
        prompt += f" ({summary})"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        chat_url,
        headers=headers,
        data=json.dumps({
            "model": str(_config.cfg()["vision"]["roast_model"]),
            "messages": [
                {"role": "system", "content": f"You are {_config.bot_name()}, a witty 3D printer assistant who gently roasts 3D prints. Give ONE short funny sentence roasting this model. Keep it under 20 words. No quotes, no hashtags, no introductory filler."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 50,
            "temperature": 0.8
        }).encode()
    )
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
            res = data["choices"][0]["message"]["content"].strip().strip("\"'")
            return res if res else ""
    except Exception:
        return ""


def start_message_with_roast(filename, archive=None, context_path=None):
    """Format the canonical start message and append a best-effort model roast."""
    message = print_summary.format_event("started", filename, archive=archive, context_path=context_path)
    try:
        from ..core.exclude_object import objects_line_for_path
        _job, plate = print_summary.find_job(filename, archive=archive, context_path=context_path)
        line = objects_line_for_path((plate or {}).get("gcode"))
        if line:
            message += "\n" + line
    except Exception:
        pass
    job, _plate = print_summary.find_job(filename, archive=archive)
    title = (job or {}).get("title") or Path(str(filename or "")).stem
    roast = generate_roast(title)
    return f"{message}\n\n{roast}" if roast else message


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
        session = live_stream.read_session_file(path)
        text = live_stream.auto_start(session, section, now=time.time())
        if text is None:
            return
        live_stream.write_session_file(session, path)
        post(text)
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
        session = live_stream.read_session_file(path)
        if session.is_live and session.started_by == live_stream.AUTO_STARTED_BY:
            session.stop(now=time.time())
            live_stream.write_session_file(session, path)
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
        extra = (f"Filament runout: {which}{spool} ran out of filament.\n"
                 "To finish this print:\n"
                 f"1. Load the replacement filament into {into} at the printer.\n"
                 "2. Answer the spool question below (Existing spool or New spool) so the replacement "
                 "spool is the one registered for that slot — filament usage stays paused for every "
                 "spool until you answer.\n"
                 "3. Tap Resume print below (or type /print-resume) once the filament is loaded. "
                 "The print resumes only when you press it; nothing resumes on its own.")
    else:
        extra = (f"Paused at {where}. " if where else "") + "Fix it at the printer, then tap Resume or Cancel below (or type /print-resume or /print-stop)."
    return print_summary.format_event("paused", filename, extra=extra)


def recovery_slot(filename):
    """The slot and spool this job was using, from the hardware map. No printer call."""
    try:
        state = filament_color.adopt_renamed_slots_key(json.loads(HARDWARE_STATE.read_text()))
    except (OSError, ValueError):
        return None, None
    slots = state.get("filament_slots") or {}
    active = state.get("active_slot")
    slot = active if active in (1, 2, 3, 4) else None
    record = slots.get(str(slot)) if slot else None
    return slot, (record or {}).get("spoolman_id")


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


def spool_matches_recovery(existing_material, candidate_material, existing_color, candidate_color):
    """PLA and PLA+ can match, but color/finish must match exactly."""
    def normalized(value):
        return " ".join(str(value or "").casefold().split())

    materials = {normalized(existing_material), normalized(candidate_material)}
    materials_compatible = materials <= {"pla", "pla+"}
    if not materials_compatible and normalized(existing_material) != normalized(candidate_material):
        return False
    return bool(normalized(existing_color)) and normalized(existing_color) == normalized(candidate_color)


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


def head_sensor_from_status(status):
    """True/False when an enabled filament sensor at the head answers, else None.

    Fail-safe (#442): a sensor that is absent, disabled, or reporting a
    non-boolean value yields None (unknown), never False — an unreadable
    sensor must not turn every pause into a runout. Any enabled sensor
    reporting no filament is decisive (False wins); otherwise one enabled
    sensor reporting filament means the head is loaded.
    """
    if not isinstance(status, dict):
        return None
    saw_loaded = False
    for name, sensor in status.items():
        if not (isinstance(name, str) and name.startswith(FILAMENT_SENSOR_PREFIXES)):
            continue
        if not isinstance(sensor, dict) or sensor.get("enabled") is not True:
            continue
        detected = sensor.get("filament_detected")
        if detected is False:
            return False
        if detected is True:
            saw_loaded = True
    return True if saw_loaded else None


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
        query = "&".join(urllib.parse.quote(name, safe="") for name in names)
        data = print_control._call(f"/printer/objects/query?{query}")
        status = ((data.get("result") or {}).get("status")
                  if isinstance(data, dict) else None)
        return head_sensor_from_status(status)
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


def compatible_offers(spools, material, color):
    """Offer same color/finish PLA or PLA+ spools. Never pick one."""
    offers = []
    for item in spools or []:
        filament = item.get("filament") or {}
        if item.get("archived") is True:
            continue
        if spool_matches_recovery(material, filament.get("material") or item.get("material"),
                                  color, filament.get("name") or item.get("color") or item.get("name")):
            offers.append({"id": item.get("id"), "remaining_weight": item.get("remaining_weight")})
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
    match = None
    for filament in filaments or []:
        if (str(filament.get("material") or "").casefold() == material.casefold()
                and str(filament.get("name") or "").casefold() == color.casefold()
                and str((filament.get("vendor") or {}).get("name") or "").casefold()
                == str(choice.get("brand") or "").casefold()):
            match = filament
            break
    if match is None:
        match = client("POST", "/api/v1/filament", {
            "name": color, "material": material, "vendor_name": choice.get("brand") or None,
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
            choices.append(f"#{item['id']} ({item.get('remaining_weight', 'unknown')} g remaining)")
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


def _post_completion(message, completion_message):
    """Acknowledge only a matching pending notice after transport success."""
    delivered = post(message)
    if delivered:
        state = announcer.load_state()
        if announcer.pending_completion_message(state) == completion_message:
            announcer.clear_pending_completion_message(state, completion_message)
            announcer.clear_pending_timelapse_job_key(state)
            state["announced_completion"] = True
            announcer.save_state(state)
    return delivered


def run_announcer(reason):
    """Run announcer once; wait to make completion and its video one Discord message."""
    out = io.StringIO()
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
                state = announcer.load_state()
                announcer.clear_pending_timelapse_job_key(state, tl_key)
                announcer.save_state(state)
                _start_postprocess(tl_key, pending, None, None, "timelapse-postprocess-retry", claimed=True)
            else:
                log(f"timelapse postprocess for {tl_key!r} still running; not re-driving it")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    announcer.main()
            except Exception as exc:  # never let one bad pass kill the listener
                log(f"announcer error ({reason}): {exc!r}")
            return
        delivered = _post_completion(pending, pending)
        if not delivered or announcer.pending_completion_message(announcer.load_state()):
            # Avoid a second identical delivery attempt from main() in this pass.
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    announcer.main()
            except Exception as exc:  # never let one bad pass kill the listener
                log(f"announcer error ({reason}): {exc!r}")
            return
    try:
        with contextlib.redirect_stdout(out):
            announcer.main()
    except Exception as exc:  # never let one bad pass kill the listener
        log(f"announcer error ({reason}): {exc!r}")
        return
    lines = out.getvalue().splitlines()
    marker = next((line for line in lines if line.startswith("TIMELAPSE_POSTPROCESS:")), None)
    picker = next((line for line in lines if line.startswith(announcer.PLATE_PICKER_MARKER)), None)
    plan_id = picker.split(":", 1)[1].strip() if picker else None
    tl_line = next((line for line in lines if line.startswith(plan_timelapse.PLAN_MARKER)), None)
    message = "\n".join(line for line in lines
                        if not line.startswith(("TIMELAPSE_POSTPROCESS:", announcer.PLATE_PICKER_MARKER,
                                                plan_timelapse.PLAN_MARKER))).strip()
    if not message:
        return
    tl_plan = None
    if tl_line:
        try:
            tl_id, tl_stage, tl_order = tl_line[len(plan_timelapse.PLAN_MARKER):].strip().split(":")
            tl_plan = (tl_id, tl_stage, int(tl_order))
        except ValueError:
            log(f"ignoring malformed timelapse plan marker {tl_line!r}")
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
    _start_postprocess(job_key, message, plan_id, tl_plan, "timelapse-postprocess")


def _short_meta(job_key):
    """Overlay metadata for a vertical short, from what the listener already knows (#317)."""
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
    return {
        "title": context.get("title") or clean(job_key or "print"),
        "printer": printer_name,
        "material": context.get("filament_material") or context.get("material"),
        "elapsed_s": context.get("print_duration") or context.get("elapsed_s"),
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
        partial = target.with_name(f".{target.name}.partial.mp4")
        used = encoder
        try:
            _encode_sized(source, partial, attempt_kbps, encoder)
        except Exception:
            if encoder == "cpu":
                raise
            log(f"timelapse {encoder} encode failed for {source.name}; retrying with the CPU encoder")
            used = "cpu"
            _encode_sized(source, partial, attempt_kbps, "cpu")
        if partial.stat().st_size <= max_bytes:
            os.replace(partial, target)
            log(f"timelapse shrunk {source.stat().st_size} -> {target.stat().st_size} bytes at {attempt_kbps}k ({used})")
            return target
        partial.unlink(missing_ok=True)
    raise RuntimeError(f"could not shrink {source.name} under {max_bytes} bytes")



NOTIFICATION_STATE = _config.state_file("PRINT_HANDY_NOTIFICATION_STATE", "lifecycle_notifications.json")


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


START_CLAIMS = _config.state_file("PRINT_HANDY_START_CLAIMS", "print_handy_start_claims.json")
START_CLAIM_TTL_S = float(_LISTENER["start_claim_ttl_s"])
NATIVE_POLL_SECONDS = float(_LISTENER["ifs_presence_poll_s"])
IFS_EDGE_STATE = _config.state_file("IFS_EDGE_STATE", "ifs_presence_edges.json")
IFS_PENDING_STATE = _config.state_file("IFS_PENDING_STATE", "ifs_pending_prompts.json")
RUNOUT_PAUSE_STATE = _config.state_file("RUNOUT_PAUSE_STATE", "runout_pause.json")
# The last stock-screen error alerted, so a listener restart does not post the same error again.
NATIVE_ERROR_STATE = _config.state_file("NATIVE_ERROR_STATE", "native_error_alerted.json")
SPOOLMAN_KNOWN_STATE = _config.state_file("SPOOLMAN_KNOWN_STATE", "spoolman_known_spools.json")
RUNOUT_SPOOL_COMMENT = "runout replacement; starting weight supplied by operator"
# An undelivered new-spool prompt is retried this often, and given up after a day.
NEW_SPOOL_RETRY_S = 300.0
NEW_SPOOL_GIVE_UP_S = 24 * 3600.0
HARDWARE_STATE = _config.state_file("PRINTER_HARDWARE_STATE", "printer_hardware.json")


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
    """'#21 Sunlu PLA+ Matte White' — number, brand, material, colour from the filament record."""
    filament = (spool or {}).get("filament") or {}
    brand = str((filament.get("vendor") or {}).get("name") or "").strip()
    parts = [brand, str(filament.get("material") or "").strip(),
             str(filament.get("name") or "").strip()]
    return f"#{(spool or {}).get('id')} " + (" ".join(part for part in parts if part) or "unknown filament")


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
        from ..notify import emit
        emit("spool_added", new_spool_plain_text(spool).splitlines()[0])
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
    return comment in (spool_sync.CREATED_SPOOL_COMMENT, RUNOUT_SPOOL_COMMENT)


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


class FilamentInsertionMonitor:
    """Stable native slot presence detector. Sends text only, never edits inventory."""
    # After a reboot or reconnect ZMod can report placeholder presence for a while
    # (2026-10-30: four false insertion prompts 20 s after klippy ready). Readings that
    # disagree with the persisted edges are held for this long before being adopted.
    SETTLE_SECONDS = 120

    def __init__(self, edge_path=IFS_EDGE_STATE, pending_path=IFS_PENDING_STATE):
        self.edge_path = Path(edge_path)
        self.pending_path = Path(pending_path)
        self.reset_baseline()

    def reset_baseline(self):
        """Forget in-memory arming; the persisted edges stay the physical reference."""
        self.baselined = False
        self.last_sample = {}
        self.stable_counts = {}
        self.transition_from = {}
        self.settling_since = None

    def _armed(self, current):
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
            self.baselined = True
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


def announce_warmup_done(filename):
    """A claimed start already has its one "Print started" post: edit it, never post another.

    This is the single roast point for a warming start (#374): the intake
    posted its start message without a roast and recorded its id, and this
    edit adds the warmup-complete line plus exactly one roast, keeping the
    canonical start details. The recorded id is consumed, so a replayed
    transition cannot add a second roast. With no recorded id, or when
    Discord refuses the edit, the start post is left as is — no second
    post and no roast is posted elsewhere.
    """
    text = print_summary.format_event("started", filename)
    text += "\nWarmup complete, now printing."
    message_id = None
    roast = ""
    try:
        from . import print_next
        message_id = print_next.take_start_message(filename)
    except Exception as exc:
        log(f"start message lookup failed file={filename!r}: {exc!r}")
    if not message_id:
        # The intake already posted the one "Print started" message; a second line after it is noise.
        log(f"no recorded start message; warmup done silently file={filename!r}")
        return False
    try:
        job, _plate = print_summary.find_job(filename)
        roast = generate_roast((job or {}).get("title") or Path(str(filename or "")).stem,
                               (job or {}).get("summary") or "")
    except Exception:
        roast = ""
    if roast:
        text += f"\n\n{roast}"
    if message_id:
        try:
            from . import print_alert_buttons as pab
            if pab.edit_message(_bot_token(), announcer.DISCORD_CHAT_ID,
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


def _visual_verdict(image, prompt, streak=None):
    """The shared verdict call (visual_watch.visual_verdict) with this listener's values.

    ``streak`` is the print watch's per-print local-detector escalation
    state (#455); only visual_check passes it — the auto light-off idle
    checks judge single frames, so they leave it None.
    """
    return _visual_watch.visual_verdict(
        image, prompt, chat_url=_vision_chat_url, model=_VISION_MODEL,
        api_key=lambda: _profile_env_value("CLIPROXY_API_KEY"), context=_VISION_CONTEXT,
        local_streak=streak)


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
        from ..notify import emit
        emit("watchdog_offer", reason)
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
        state = filament_color.adopt_renamed_slots_key(json.loads(HARDWARE_STATE.read_text()))
    except (OSError, ValueError):
        return None
    return ((state.get("filament_slots") or {}).get(str(slot)) or {}).get("spoolman_id")


def _maintenance_history_totals():
    payload = print_control._call('/server/history/totals')
    result = payload.get('result') or {}
    return float(result.get('total_time') or result.get('print_time') or 0.0)


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
        self.slot_tracker = MidPrintSlotTracker()
        self.visual_watch = VisualPrintWatch()
        # #455: this printer's local-detector temporal escalation state;
        # reset wherever visual_watch is (new file, fresh start, print end).
        self.local_streak = _local_vision.FailureStreak()
        self.last_stats = {}
        self.last_visual_task = None
        self.visual_frames = {}
        self.last_slots = {}
        self.last_native_poll = 0.0
        self.native_poll_inflight = False
        self.prev_native = None
        self.screen_reboot_task = None
        self.background_tasks = set()  # strong refs: an unreferenced task can be collected mid-run
        self.maintenance_last_check = 0.0
        self.last_pause_was_runout = False
        self.pause_context = None
        self.native_error_alerted = load_native_error_alerted()
        self.display_off = None  # live ZMod display mode from the last presence poll; None until read
        self.presence_failure = None  # last presence poll failure, logged once until it changes
        self.capture_session = None  # #246 stage 2: active timelapse_capture session, if any
        self.print_identity = None  # this print's identity, fixed at its fresh start
        self._auto_light = None  # #516: AutoLightPolicy, built lazily from [vision]
        self.light_target = None  # #516: resolved Moonraker light object, cached
        self.light_target_resolved = False
        self.light_unresolved_logged = False

    async def read_slots(self):
        """(stock /detail or None, {slot: present}). Screenless ZMod has no stock API; Klipper has the slots."""
        from print_handy.core import printer_state
        from print_handy.core import model_job
        reply = await asyncio.to_thread(print_control._call, "/printer/objects/query?zmod_color&save_variables")
        status = (reply.get("result") or {}).get("status") or {}
        self.display_off = zmod_display_off(status)
        if self.display_off == 1:
            return None, zmod_slot_presence(status)
        detail = await asyncio.to_thread(model_job.read_stock_detail, printer_state.load())
        return detail, model_job.read_stock_filament_slots_from_detail(detail)

    async def open_question(self, slot, cause):
        """Stop charging every spool, remember the question, and ask once. Nag loop repeats it."""
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
        try:
            detail, slots = await self.read_slots()
            self.last_slots = slots
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
                self.last_slots = slots
                self.slot_tracker.observe(slots)
            except Exception as exc:
                log(f"pause slot read failed: {exc!r}")
            empty = self.slot_tracker.empty_slots(slots)
            present = _slot_bools(slots)
            slot = empty[0] if len(empty) == 1 else recovery_slot(self.filename)[0]
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
            verdict, detail = await asyncio.to_thread(_visual_verdict, image, prompt, self.local_streak)
            suspicious = verdict in {"air", "spaghetti", "detached", "uncertain", "no", "unsafe"}
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
                    await asyncio.to_thread(_visual_alert, self.filename, image,
                                            f"{reason} at layer {layer or 'unknown'}: {verdict}; {detail or 'visual check needs operator review'}",
                                            identity=self._visual_identity())
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
        if fresh_start:
            # A fresh start (or a print already running when the listener
            # (re)connected): open the #246 stage 2 capture session,
            # replacing any session a missed end left open. A resume keeps
            # the session the pause left open.
            self._capture_begin(identity)
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
                self.visual_watch.reset()
                self.local_streak.reset()
                if duplicate_start:
                    log(f"start already announced; no second start post: {self.filename!r}")
                elif start_claimed(self.filename):
                    log(f"start claimed by print-now; updating its message, no second start post: {self.filename!r}")
                    await asyncio.to_thread(announce_warmup_done, self.filename)
                    await asyncio.to_thread(post_exclude_objects, self.filename)
                else:
                    await asyncio.to_thread(post, start_message_with_roast(self.filename))
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
            prompt = print_control.next_copy_prompt(self.filename)
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
                _on, off_script = light_control.light_scripts(target)
                await asyncio.to_thread(_send_light_script, off_script)
                log(f"auto light: plate seen empty for "
                    f"{policy.minutes} minutes; light off ({target})")
        except Exception as exc:
            log(f"auto light idle check failed: {exc!r}")
            with contextlib.suppress(Exception):
                self._auto_light_policy().note_check(time.time())

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

    async def session(self):
        self.ifs_monitor.reset_baseline()
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
            self.moonraker_connected = True
            await ws.send(SUBSCRIBE)
            last_tick = time.monotonic()
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
                            await self._capture_due(_visual_layer(self.last_stats))
                    elif method == "notify_status_update":
                        ps = (msg.get("params") or [{}])[0].get("print_stats")
                        if ps:
                            await self.on_status(ps, "event")
                            await self._capture_due(_visual_layer(self.last_stats))
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
                        self.ifs_monitor.reset_baseline()
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
                        self.ifs_monitor.reset_baseline()
                        log("klippy ready; resubscribing")
                        await ws.send(SUBSCRIBE)
                if time.monotonic() - self.last_native_poll >= NATIVE_POLL_SECONDS:
                    self.last_native_poll = time.monotonic()
                    await self.poll_native_presence()
                    try:
                        await self.poll_spoolman_new_spools()
                    except Exception as exc:
                        log(f"spoolman new-spool poll failed: {exc!r}")
                    try:
                        await self.nag_if_due()
                    except Exception as exc:
                        log(f"spool nag failed: {exc!r}")
                    try:
                        await self.light_idle_tick()
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

    async def forever(self):
        delay = 5
        self.moonraker_connected = False
        beating = asyncio.create_task(self.heartbeat())
        try:
            while True:
                await self.backfill_slot_colors()
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
