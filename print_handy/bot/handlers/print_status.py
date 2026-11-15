"""The print status chat handler (#1475).

/print-status: how the current print is going, on demand — the listener
only posts when it decides to. Read-only: state, progress, time left,
layer, temperatures and a current camera frame, read the way every
listener family reads status (``print_stop.status`` routes Moonraker,
Bambu LAN and the family adapters; the family normalised status and one
Moonraker heater query fill in the temperatures it drops).

When a print is running, the answer offers Pause and Stop buttons (Resume
when paused). Those buttons follow the existing confirm rules: Pause is
the one deliberate tap the alert buttons already use, Stop asks its own
confirm question first, exactly like /print-stop.
"""
import logging
import asyncio, time, uuid

from .common import (
    _adapter_for, _command_matches, _is_bot_platform, _is_hosting_profile, _live_key_for, _pkw,
    _printer_label, _routed_form, _routed_printer_id, _undecided_printer_ids,
)

logger = logging.getLogger(__name__)

ACTIVE = ("printing", "paused")
KEEP_LABEL = "Keep printing"


def _fmt_duration(seconds):
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return None
    if s < 0:
        return None
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s"


def _time_left(st):
    """Seconds left: the family's own remaining figure, else estimated from progress."""
    remaining_min = st.get("remaining_min")
    if isinstance(remaining_min, (int, float)) and not isinstance(remaining_min, bool):
        return remaining_min * 60
    progress, duration = st.get("progress"), st.get("print_duration")
    if (isinstance(progress, (int, float)) and isinstance(duration, (int, float))
            and 0 < progress < 1 and duration > 0):
        return duration * (1 - progress) / progress
    return None


def _enrich(st, pid):
    """Fill temperatures / remaining time the reduced status dict drops. Fail-soft."""
    if st.get("hotend_c") is not None or st.get("bed_c") is not None:
        return st
    try:
        from print_handy.runtime import family_control
        adapter = family_control.adapter_for(pid)
        if family_control.connection_of(adapter) in family_control.FAMILY_CONNECTIONS:
            full = family_control.status_fn_for(adapter)()
            for key in ("hotend_c", "bed_c", "hotend_target_c", "bed_target_c", "remaining_min"):
                if full.get(key) is not None:
                    st[key] = full[key]
            return st
    except Exception:
        logger.debug("family status enrichment failed", exc_info=True)
    try:  # Moonraker: one heater query alongside the job query
        from print_handy.runtime import print_stop
        data = print_stop._send("/printer/objects/query?extruder=temperature,target"
                                "&heater_bed=temperature,target", **_pkw(pid))
        s = (data.get("result") or {}).get("status") or {}
        ext, bed = s.get("extruder") or {}, s.get("heater_bed") or {}
        st["hotend_c"], st["hotend_target_c"] = ext.get("temperature"), ext.get("target")
        st["bed_c"], st["bed_target_c"] = bed.get("temperature"), bed.get("target")
    except Exception:
        logger.debug("Moonraker heater read failed", exc_info=True)
    return st


def _camera_frame(st, pid):
    """A current camera frame path, or None. Best-effort, never fails the status."""
    if st.get("camera_image_path"):  # Bambu: the status read already grabbed one (#294)
        return st["camera_image_path"]
    try:
        import urllib.request

        from print_handy import settings
        from print_handy.runtime import _config
        url = None
        data = settings.current()
        if pid:
            try:
                url = settings.by_id(pid, data).get("camera_snapshot_url")
            except Exception:
                url = None
        url = url or (data.get("services") or {}).get("camera_snapshot_url")
        if not url:
            return None
        with urllib.request.urlopen(url, timeout=5) as resp:
            image = resp.read()
        if not image:
            return None
        folder = _config.state_dir() / "status-frames"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"frame-{int(time.time())}.jpg"
        path.write_bytes(image)
        return str(path)
    except Exception:
        logger.debug("status camera frame failed", exc_info=True)
        return None


def status_text(st, label):
    """The status answer: state, file, progress, time left, layer, temperatures."""
    state = str(st.get("state") or "unknown")
    lines = [f"**{label}** — {state}"]
    if st.get("filename"):
        lines.append(f"File: {st['filename']}")
    bits = []
    progress = st.get("progress")
    if isinstance(progress, (int, float)) and not isinstance(progress, bool):
        bits.append(f"{progress * 100:.0f}% done")
    left = _time_left(st)
    if left is not None and state in ACTIVE:
        bits.append(f"about {_fmt_duration(left)} left")
    elapsed = _fmt_duration(st.get("print_duration"))
    if elapsed and state in ACTIVE:
        bits.append(f"{elapsed} elapsed")
    if st.get("layer") and st.get("total_layer"):
        bits.append(f"layer {st['layer']}/{st['total_layer']}")
    if bits:
        lines.append(" · ".join(bits))
    temps = []
    for key, target_key, name in (("hotend_c", "hotend_target_c", "Nozzle"),
                                  ("bed_c", "bed_target_c", "Bed")):
        value = st.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target = st.get(target_key)
            temps.append(f"{name} {value:.0f}°C" + (f" / {target:.0f}°C" if target else ""))
    if temps:
        lines.append(" · ".join(temps))
    return "\n".join(lines)


def _pause(control, pid, filename):
    """One pause for the confirmed file, through the path its family uses.

    Every send goes through the printer_control service or an existing
    entry point (the alert buttons), never around them: Bambu pauses
    through the service, Moonraker through the camera alert's Pause path
    (which re-reads, sends once and reads back), and the other families
    have no pause surface anywhere in Print Handy, so they say so.
    """
    if control.is_bambu_lan():
        return control.pause(filename)[1]
    try:
        from print_handy.runtime import family_control
        adapter = family_control.adapter_for(pid)
        if family_control.connection_of(adapter) in family_control.FAMILY_CONNECTIONS:
            return "Pause is not available for this printer here. Nothing sent."
    except Exception:
        logger.debug("status pause family check failed", exc_info=True)
    from print_handy.runtime import print_alert_buttons as pab
    alert = {"kind": "visual", "filename": filename}
    if pid:
        alert["printer_id"] = pid
    return pab.handle_click("pause", alert)


def _resume(control, filename):
    """One resume for the confirmed paused file, through the service (as /print-resume)."""
    return control.resume(filename)[1]


async def _ask(adapter, source, metadata, live_key, question, choices):
    """One button question through the prompt registry (the print_stop pattern)."""
    from print_handy.core.prompts import gateway as _prompts
    from print_handy.core import prompts as _prompt_wait
    from print_handy.runtime import _config
    cm = _prompts()
    cid = uuid.uuid4().hex
    session_key = f'print-status:{source.chat_id}:{source.thread_id}'
    cm.register(cid, live_key or session_key, question, choices)
    try:
        sent = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                                          choices=choices, clarify_id=cid,
                                          session_key=session_key, metadata=metadata)
        if not getattr(sent, 'success', False):
            raise RuntimeError('button delivery failed')
        return await _prompt_wait.await_answer(
            cid, _config.safety_confirm_timeout_s() if _config else 600, registry=cm)
    finally:
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)


async def print_status_dispatch(event, gateway, **kwargs):
    """/print-status: the current print's state, progress, time left, layer, temps and camera."""
    source = getattr(event, 'source', None)
    text = str(getattr(event, 'text', '')).strip()
    if not _command_matches(text, '/print-status', True):
        return None
    if (source is None or not _is_bot_platform(source)
            or not _is_hosting_profile(getattr(source, 'profile', None))
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile_route_rejected', False)):
        return None
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'print status UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    wanted = text[len('/print-status'):].strip()
    if wanted:  # the slash form's optional printer option arrives as its roster id
        known = {entry.get('id') for entry in _roster()}
        if wanted in known:
            pid = _routed_form(wanted)
        else:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content=f'No printer named {wanted} here. Nothing was read.')
            return {'action': 'skip', 'reason': 'print status unknown printer'}
    from print_handy.runtime import print_stop
    try:
        st = await asyncio.to_thread(print_stop.status, **_pkw(pid))
    except Exception:
        logger.exception('/print-status could not read the printer')
        await adapter.send(chat_id=source.chat_id, metadata=metadata,
                           content='Could not read the printer right now. Nothing was changed.')
        return {'action': 'skip', 'reason': 'print status read failed'}
    st = await asyncio.to_thread(_enrich, st, pid)
    label = _label_for(source, gateway, pid)
    frame = await asyncio.to_thread(_camera_frame, st, pid)
    content = status_text(st, label)
    if frame:
        content += f"\nMEDIA:{frame}"
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)
    state = str(st.get('state') or 'unknown')
    if state not in ACTIVE:
        return {'action': 'skip', 'reason': 'print status shown'}
    filename = st.get('filename') or ''
    live_key = _live_key_for(source, gateway, _channel_pid(source, gateway, pid))
    if state == 'printing':
        choices = ['Pause', 'Stop', KEEP_LABEL]
        question = f'Pause or stop {filename or "this print"}?'
    else:
        choices = ['Resume', 'Stop', KEEP_LABEL]
        question = f'Resume or stop {filename or "this print"}?'
    answer = await _ask(adapter, source, metadata, live_key, question, choices)
    if answer in ('Pause', 'Resume'):
        from print_handy import printer_control
        control = printer_control.for_printer(pid)
        if answer == 'Pause':
            result = await asyncio.to_thread(_pause, control, pid, filename)
        else:
            result = await asyncio.to_thread(_resume, control, filename)
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=result)
    elif answer == 'Stop':
        # /print-stop's rule: the stop itself takes a confirm question.
        confirm = await _ask(adapter, source, metadata, live_key,
                             f'Stop {filename or "this print"}? It cannot be resumed from here.',
                             ['Stop the print', KEEP_LABEL])
        if confirm == 'Stop the print':
            from print_handy import printer_control
            _sent, result = await asyncio.to_thread(
                printer_control.for_printer(pid).cancel, filename)
            await adapter.send(chat_id=source.chat_id, metadata=metadata, content=result)
    return {'action': 'skip', 'reason': 'print status shown'}


def _roster():
    try:
        from print_handy import settings
        return settings.current().get('printers') or []
    except Exception:
        return []


def _channel_pid(source, gateway, pid):
    from .common import _channel_printer_id
    # The printer the command named wins (review of #1484); the channel's
    # own printer only fills in when none was named.
    return pid or _channel_printer_id(source, gateway)


def _label_for(source, gateway, pid):
    channel_pid = _channel_pid(source, gateway, pid)
    return _printer_label(channel_pid) if channel_pid else 'Printer'


# --- handler interface (#1323) ---
from ._gates import command_gate  # noqa: E402

match = command_gate('/print-status', True)
handle = print_status_dispatch
handle.dispatch_gate = match
