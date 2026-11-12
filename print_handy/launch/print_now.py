"""Starting a sliced job: print_now, the per-family start paths, start confirmation and
the start records.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import logging
import hashlib, json, os, re, subprocess, time, zipfile
import urllib.request
from pathlib import Path
from typing import NamedTuple

from .. import firmware_update, printers, settings
from ..core import (
    _external, filament_color, gcode_3mf, launch_state, printer_state, purge, spoolman_client,
    start_recorder, start_truth,
)
from ..gcode.artifact import _text_like_read_text, file_sha256
from ..gcode.verify import (
    _gcode_tools, _refuse_multicolor, _verify_routed, _with_gcode_temperatures,
    verify_gcode_offline,
)
from ..jobs.common import FILAMENT_SLOTS, MOONRAKERCTL, TIMEZONE, _moonrakerctl_env, _mr, request_json
from ..jobs.filament import (
    read_filament_telemetry, read_stock_detail, read_stock_filament_slots,
    read_stock_filament_slots_from_detail, resolve_filament_selection,
)
from ..jobs.store import (
    CONTEXT, RECORD_KEYS, _detail_tail, _job_adapter, _job_for_gcode, _job_record_for_gcode,
    _printer_key, _recorded_printer_id, _timing, decisions_digest, effective_color_decisions,
    gcode_estimate_s, human_dur, remote_name, resolve_print_target, write_job,
)
from .gates import (
    _BED_CLEAR, _YOLO, _claim_bed_clear, _gate, _native, _printer_phase, _refuse, _require_idle,
    _require_idle_before_mapped_launch, _require_launch_checkpoint_clear,
    _verify_filament_readiness, _waive, mesh_readiness, partial_job_refusal,
    require_current_artifact, require_mechanical_limits, screen_z_offset, start_print_settings,
    validate_native_start, validate_saved_mesh_start, yolo_active,
)
from ..printers.base import MultiColorUnsupported

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.core.model_job")


# Bambu Lab start confirmation (#271): the printer can take well over a minute after `project_file`
# to unpack the package and report it, so one snapshot is not evidence. Poll this often, for the
# [bambu] start_confirm_s window (default below). Both hooks are module attributes so tests can
# drive the window with a fake clock instead of sleeping.
BAMBU_START_CONFIRM_S = 90
BAMBU_START_POLL_S = 10
_confirm_clock = time.monotonic
_confirm_sleep = time.sleep


MAP_PREFIX = "; hermes_tool_slots = "


def _sync_active_spool(st, slot, printer_id=None):
    """Point Moonraker and the hardware record at the launching slot's spool.

    Runs only after every launch preflight has passed and before upload.
    Nozzle and bed temperatures are left alone; per-slot temperatures are
    Phase 2.

    Moonraker is a launch dependency: its active-spool read failing refuses
    the launch, and a readback that ANSWERS with a different spool refuses
    too (a live contradiction, nothing uploaded). Spoolman is not a launch
    dependency (#387): when Spoolman does not answer, or the Moonraker
    spool-pointer write itself fails, the launch proceeds on the slot's
    recorded data, the user gets the one-line notice, and the result
    carries `spoolman_pending` so the caller records the usage as pending
    in the job record for later reconciliation — it is never retried in a
    way that blocks the job. A live Spoolman answer that the spool is
    archived or a different material than the slot record still refuses:
    that is Spoolman data contradicting the operator's record, not an
    outage, and the refusal protects the physical gate (#235).
    """
    configured = st.get('filament_slots', {}).get(str(slot), {})
    sid = configured.get('spoolman_id')
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise RuntimeError(f'IFS slot {slot} has no mapped Spoolman spool; nothing uploaded or started')
    try:
        current = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        raise RuntimeError(f'Moonraker active spool read failed: {exc}; nothing uploaded or started') from exc
    if current == sid and st.get('spool', {}).get('spoolman_id') == sid:
        return {'spool_id': sid, 'changed': False}
    spool = spoolman_client.get_spool(sid, base_url=printer_state.spoolman_url(st))
    pending = spool is None
    if pending:
        spoolman_client.note_unavailable_once()
        filament = {}
    else:
        filament = spool.get('filament') or {}
        if spool.get('archived') or not printer_state.same_material(filament.get('material'), configured.get('material')):
            raise RuntimeError(f'Spoolman spool #{sid} is archived or its material differs from slot {slot}; nothing uploaded or started')
    try:
        _mr(st, '/server/spoolman/spool_id', data={'spool_id': sid})
        back = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        # The pointer write is Spoolman accounting routed through Moonraker;
        # its failure must not refuse the launch (#387). Usage may charge
        # the previous spool or none until reconciliation.
        logger.warning('active spool sync failed for spool #%s: %s', sid, exc)
        spoolman_client.note_unavailable_once()
        pending = True
    else:
        if back != sid:
            raise RuntimeError(f'Moonraker active spool readback {back!r} != #{sid}; nothing uploaded or started')
    recorded = st.get('spool') or {}
    # The reload/save stays on the printer `st` belongs to: with a roster
    # printer_id the legacy no-ID record is a DIFFERENT printer's (#847).
    with printer_state.hardware_lock(printer_id or None):
        fresh = printer_state.load(printer_id) if printer_id else printer_state.load()
        fresh['spool'] = {'spoolman_id': sid,
                          'name': filament.get('name') or configured.get('name'),
                          'material': filament.get('material') or configured.get('material'),
                          'diameter_mm': filament.get('diameter', recorded.get('diameter_mm', 1.75)),
                          'density': filament.get('density', recorded.get('density', 1.24))}
        # temps_c belonged to the previous spool: mark it so slicing resolves this one's (#692).
        fresh['temperature_resolution'] = {'source': 'spool changed by launch sync; resolved per slice'}
        if printer_id:
            printer_state.save(fresh, printer_id)
        else:
            printer_state.save(fresh)
    st['spool'] = dict(fresh['spool'])
    print(f'ACTIVE SPOOL SYNCED: #{current} -> #{sid} (slot {slot})', flush=True)
    result = {'spool_id': sid, 'previous': current, 'changed': True}
    if pending:
        result['spoolman_pending'] = True
    return result


def _clear_active_spool(st):
    """Clear Moonraker's active spool before a multi-slot launch (P1-24).

    A multi-slot job's usage cannot be apportioned to one spool, so no spool
    may be charged — but a pointer left over from a single-slot job or a
    manual selection would keep Moonraker deducting ALL extrusion from that
    stale spool while the launch claims deduction is disabled. Clear it with
    verified readback before upload, with _sync_active_spool's posture:
    Moonraker is a launch dependency (its read failing refuses, and a
    readback that still names a spool after the clear refuses too), while a
    failed clear WRITE is Spoolman accounting routed through Moonraker —
    the launch proceeds and the result carries `spoolman_pending` so the
    caller records it in the job record for later reconciliation (#387).
    """
    try:
        current = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        raise RuntimeError(f'Moonraker active spool read failed: {exc}; nothing uploaded or started') from exc
    if current is None:
        return {'spool_id': None, 'previous': None, 'changed': False}
    try:
        _mr(st, '/server/spoolman/spool_id', data={})
        back = _mr(st, '/server/spoolman/spool_id')['result'].get('spool_id')
    except Exception as exc:
        logger.warning('active spool clear failed for spool #%s: %s', current, exc)
        spoolman_client.note_unavailable_once()
        return {'spool_id': current, 'previous': current, 'changed': False, 'spoolman_pending': True}
    if back is not None:
        raise RuntimeError(f'Moonraker active spool readback {back!r} after clear; nothing uploaded or started')
    print(f'ACTIVE SPOOL CLEARED: #{current} -> none (multi-slot job charges no spool)', flush=True)
    return {'spool_id': None, 'previous': current, 'changed': True}


def _start_family_monitor(watch, adapter, remote, job):
    """Start a family's launched-print monitor for a just-started print.

    The monitor runs in a detached worker process (hermes.monitor_worker),
    never on a thread here: print-now is a short-lived subprocess, so an
    in-process daemon thread dies with the submission and the print loses
    its completion post and timelapse (P1-85, P1-98). The worker rebuilds
    the job printer's adapter there — this process's adapter only
    identifies the family, through the watch module it pairs with. The
    job's recorded printer_id rides along so the worker serves that
    printer and the monitor's pause alerts act on it (SAFE-4 / P1-101).
    """
    from print_handy.runtime import monitor_worker
    return monitor_worker.spawn(watch.__name__, remote, _recorded_printer_id(job))


def _per_tool_filament_settings(text):
    """The per-tool filament split for slicer_settings (#981 v2).

    ``filament_used_g_list`` (per-tool grams, the name
    core/spool_lifecycle.py already uses for the same header list)
    plus the aligned ``filament_color_list``, both from the G-code's
    own header via gcode_3mf.plate_metadata(). Records used to flatten
    a multi-color print to one color/grams total; carrying the split
    in slicer_settings lets future records answer per-color grams
    natively. A header that names no per-tool usage yields {} — the
    keys stay absent, exactly like every other sparse record field.
    """
    try:
        filaments = gcode_3mf.plate_metadata(text).get("filaments") or []
    except Exception:
        return {}
    rows = [row for row in filaments if isinstance(row, dict)]
    if not rows or any(not isinstance(row.get("used_g"), (int, float))
                       or isinstance(row.get("used_g"), bool) for row in rows):
        return {}
    out = {"filament_used_g_list": [float(row["used_g"]) for row in rows]}
    colors = [row.get("color") for row in rows]
    if all(isinstance(c, str) and c for c in colors):
        out["filament_color_list"] = colors
    return out


def record_start(gcode_path, moonraker_name=None, started_at=None, print_number=None):
    """Write the JSON start record plus the completion sidecar for a started print.

    print_number, when the caller minted one for this launch, is stamped into
    the record context so the completion record carries THIS launch's number,
    not the job's first-print number.
    """
    print_records = _external.load("print_records")
    st=printer_state.load(); g=Path(gcode_path).expanduser().resolve(); text=g.read_text(errors="ignore")
    def cfg(key):
        m=re.search(rf'^; {re.escape(key)} = (.+)$', text, re.M); return m.group(1).strip() if m else None
    job=_job_for_gcode(g)
    key=moonraker_name or g.name
    src=(job or {}).get("selected_source") or {}
    ctx={"job_key":key,"title":(job or {}).get("title") or g.stem,"source_url":(job or {}).get("url"),
         "source_platform":(job or {}).get("site"),"started_at":started_at or print_records.utc_now(),
         "hardware":st.get("printer", "Klipper") + ", "+printer_state.summary(st),
         "printer_profile":"Klipper base ini, resliced locally (site settings not imported)",
         "estimated_time":cfg("estimated printing time (normal mode)") or _bambu_total_estimated_time(text),"estimated_filament_g":cfg("total filament used [g]") or _filament_total_g(text),
         "filament_diameter_mm":st["spool"]["diameter_mm"],"filament_density":st["spool"]["density"],
         "slicer_settings":{**{k:cfg(k) for k in RECORD_KEYS if cfg(k) is not None},
                            **_per_tool_filament_settings(text),
                            "source_profile":src.get("title"),"source_file":(job or {}).get("downloaded_file"),"local_gcode":str(g)},
         "active_spool":{"id":st["spool"]["spoolman_id"],"material":st["spool"]["material"],"color":st["spool"]["name"]}}
    if isinstance(print_number, int) and not isinstance(print_number, bool) and print_number > 0:
        ctx["print_number"] = print_number
    plan = (job or {}).get('multipart_plan') or {}
    project = (job or {}).get('project') or {}
    project_id = (job or {}).get('project_id') or plan.get('project_id') or project.get('project_id')
    if project_id:
        ctx.update(project_id=project_id,
                   project_source_kind=(job or {}).get('project_source_kind') or plan.get('source_kind')
                   or project.get('source_kind'),
                   project_run_id=plan.get('project_run_id') or 'original',
                   project_plate_id='plate1', project_plate_suffix='A',
                   project_component_ids=[str(p['component_id']) for p in project.get('components') or []
                                          if isinstance(p, dict) and p.get('component_id')])
    for part in plan.get('parts') or []:
        if part.get('gcode_path') and Path(part['gcode_path']).resolve() == g:
            # Split plates advance the same plan that /print-next reads.
            ctx.update(multipart_plan_id=plan['plan_id'], part_id=part['id'], required_parts=plan['parts'])
            if project_id:
                ctx.update(project_plate_id=part.get('project_plate_id') or part['id'],
                           project_plate_suffix=part.get('project_plate_suffix') or 'A',
                           project_part_keys=part.get('component_keys') or [])
                # Source component IDs and slicer part keys are distinct.
                ctx.pop('project_component_ids', None)
            break
    if project_id:
        # One project number for every plate, reprint and re-slice of this
        # job: the first plate to start claims it in JOB.json with its own
        # print number (#988 mints one per launch); later ones read it back.
        try:
            job_json_path = _job_record_for_gcode(g)[1]
            claimed = print_records.claim_project_number(job_json_path, ctx.get('print_number'))
        except Exception as exc:
            print(f"WARNING: project number not claimed: {exc}")
            claimed = None
        if claimed:
            ctx['project_number'] = claimed
    mapped = None
    current = printers.current()
    if current.multi_slot:
        # Only a native adapter has an embedded mapping to lift; a multi_slot
        # adapter without the native contract (Bambu AMS, #671 — the stock
        # AD5X never had one either) records the plain start, as it did while
        # it was classified single.
        native = current.native_or_none
        if native is not None:
            mapped = native.embedded_mapping(text, st)
    if mapped is not None:
        ctx['tool_to_physical_slot'] = mapped
        ctx['active_spool']['accounting_only_not_job_color'] = True
        ctx['slicer_settings']['mapped_materials'] = {
            tool: st['filament_slots'][str(slot)] for tool, slot in mapped.items()}
        ctx['printer_profile'] = 'Reviewed Orca AD5X native multicolor'
    if (job or {}).get('delivery_format'):
        ctx['delivery_format'] = job['delivery_format']
    starter = (job or {}).get('started_by')
    if isinstance(starter, dict) and starter.get('user_id'):
        # The intake-stamped requester rides the start context into the
        # print record, so the completion sidecar lookup can still name the
        # starter after the job folder is gone.
        ctx['started_by'] = {'platform': str(starter.get('platform') or 'discord'),
                             'user_id': str(starter['user_id'])}
    if isinstance((job or {}).get('spoolman_usage_pending'), dict):
        # Launch-time accounting failure (#387): carry the pending marker and
        # its intended spool into the start record and the completion sidecar,
        # so the completion record is marked pending instead of silently
        # attributing the usage to whatever spool answers then (P2-155).
        ctx['spoolman_usage_pending'] = job['spoolman_usage_pending']
    # #913: risky prints get the early-layer camera watch; the listener reads
    # this back from the context sidecar and the record keeps it.
    try:
        why = _external.load("visual_watch").risky_job(job)
    except Exception:
        why = ""
    ctx['watch_mode'] = 'early_layer' if why else 'normal'
    if why:
        ctx['watch_risk'] = why
    ctx["record_path"]=print_records.write_start_record(ctx)
    record_path=ctx["record_path"]
    try: side=json.loads(CONTEXT.read_text())
    except (OSError, ValueError): side={}
    if not isinstance(side.get("jobs"),dict): side={"jobs":{}}
    side["jobs"][key]={**side["jobs"].get(key,{}),**ctx}
    tmp=CONTEXT.with_suffix(".tmp"); tmp.write_text(json.dumps(side,indent=2)+"\n"); os.replace(tmp,CONTEXT)
    print(f"PRINT RECORD: {ctx['record_path']}")
    return record_path


# Start announcement ownership. print_now claims the file before starting it so the
# Moonraker listener skips its generic "Print started" post for that same job.
# The family print paths claim the same way (#460): a claimed start belongs to
# the print path and its launched-print monitor; the family listener domains
# post starts only for prints nobody claimed (started outside Print Handy).
START_CLAIMS = Path(os.environ.get("PRINT_HANDY_START_CLAIMS", settings.state_dir()/"print_handy_start_claims.json"))
START_CLAIM_TTL_S = settings.current()["listener"]["start_claim_ttl_s"]
# Intake receipts (#970): the chat intake records each job it slices in
# <state>/model-link-intakes/<intake_id>.json. A CLI print-now start is
# out-of-band for that flow, so print_now reconciles the receipt itself
# (see _record_intake_receipt_started).
INTAKE_RECEIPTS = Path(os.environ.get("PRINT_HANDY_INTAKE_RECEIPTS", settings.state_dir()/"model-link-intakes"))


def _write_claims(claims):
    tmp = START_CLAIMS.with_suffix(".tmp"); tmp.write_text(json.dumps(claims)); os.replace(tmp, START_CLAIMS)


def _read_claims():
    try: claims = json.loads(START_CLAIMS.read_text())
    except (OSError, ValueError): return {}
    return claims if isinstance(claims, dict) else {}


def claim_start_announcement(remote, now=None):
    now = time.time() if now is None else now
    claims = {k: v for k, v in _read_claims().items()
              if isinstance(v, (int, float)) and now - v < START_CLAIM_TTL_S}
    claims[remote] = now
    _write_claims(claims)


def release_start_announcement(remote):
    claims = _read_claims()
    if claims.pop(remote, None) is not None:
        try: _write_claims(claims)
        except OSError: pass


Z_OFFSET_NOTICE = ("Z offset: no explicit START_PRINT Z_OFFSET; effective offset depends on the "
                   "installed macros and screen mode. Historical first-layer cause is not established "
                   "by this G-code. Do not inject or change an offset on this evidence alone.")


def z_offset_notice(text):
    """None when START_PRINT passes an explicit Z_OFFSET; otherwise the truthful notice.
    Informational only: with display_off=0 ZMod applies the native screen zProbeOffset, which is correct."""
    return None if "Z_OFFSET" in start_print_settings(text)["args"] else Z_OFFSET_NOTICE


def mesh_line(mesh):
    if not isinstance(mesh,dict) or not mesh.get("profile"): return "Bed mesh: unknown"
    when="unknown"
    if mesh.get("saved_at"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        when=datetime.fromtimestamp(mesh["saved_at"],ZoneInfo(TIMEZONE)).strftime("%-I:%M %p %a %b %-d")
    line=f"Bed mesh: {mesh['profile']} (config saved {when})"
    if mesh.get("stale"): line+=". Nozzle changed after this mesh; rerun full bed leveling"
    return line


def _assign_print_number(job_path, artifact=None):
    """Mint this launch's own permanent print number; None on any failure.

    Every launch mints a NEW number from the shared /print-again registry —
    a reprint of an already-numbered job is a new print, not the old print
    number again. The job's first mint stays its legacy registry number, so
    old numbers keep resolving to the same job.
    """
    if job_path is None:
        return None
    try:
        print_control = _external.load("print_control")
        ARCHIVE, mint_launch_number = print_control.ARCHIVE, print_control.mint_launch_number
        key = Path(job_path).resolve()
        if not key.is_relative_to(ARCHIVE.resolve()):
            return None  # scratch and test jobs are not prints
        return mint_launch_number(key, at=time.time(), artifact=artifact)
    except Exception as e:
        print(f"WARNING: print number not assigned: {e}")
        return None


def _maintenance_detail_line():
    try:
        maintenance = _external.load("maintenance")
        _call = _external.load("print_control")._call
        runs = maintenance.load().get("runs", [])
        hours = None
        try:
            data = _call("status")
            hours = data.get("print_hours") if isinstance(data, dict) else None
        except Exception: pass
        full = next((r for r in reversed(runs) if r.get("kind") == "full"), None)
        if full is None: return "- Maintenance: history unavailable"
        try:
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(full["at"])).days
        except Exception: age = "unknown"
        delta = "print hours unknown" if hours is None else f"{max(0,float(hours)-float(full.get('hours_total',0))):.1f} h"
        note = " (history totals unavailable)" if hours is None else ""
        return f"- Maintenance: last full clean {age} days / {delta} ago.{note}"
    except Exception: return "- Maintenance: history unavailable"


def job_details(out, *, hardware=None, selection=None, settings=None, record_path=None, z_notice=None, live=None,
                screen_z=None, mesh=None, printer=None, nozzle_mm=None, calibration=True):
    """Compact public summary; accounting, telemetry and paths stay in structured records."""
    def u(v): return v if v not in (None, "") else "unknown"
    args = (settings or {}).get("args") or {}
    bed = args.get("BED_TEMP") or args.get("BED"); hot = args.get("EXTRUDER_TEMP") or args.get("HOTEND")
    chan = (f"slot {selection.get('slot')}: {u(selection.get('color'))} {u(selection.get('material'))}"
            if isinstance(selection, dict) and selection.get("slot") is not None else None)
    grams = out.get("filament_g")
    # Historical records may include a timezone label; public details always assume New York.
    finish = re.sub(r"\s+(?:ET|EDT|EST)\b", "", str(u(out.get("finish_et"))))
    owner = (mesh or {}).get("offset_source") if isinstance(mesh, dict) else None
    source = owner or ("persisted" if isinstance(mesh, dict) else "screen")
    z = f"{screen_z:.3f} mm ({source})" if isinstance(screen_z, (int, float)) and not isinstance(screen_z, bool) else "unknown"
    return "\n".join([
        "**Job Details:**",
        *([f"- Print #{out['print_number']}"] if out.get("print_number") else []),
        f"- Model: {u(out.get('title'))}",
        f"- Estimated time: {u(out.get('print_time'))} printing + ~5 min warmup; finish {finish}",
        f"- Filament: {f'{grams} g' if grams is not None else 'unknown'}; {u(chan)}",
        f"- Printer: {u(printer)}",
        f"- Stock nozzle: {u(nozzle_mm)} mm; targets nozzle {u(hot)} C, bed {u(bed)} C",
        *([f"- Temperature material: {u((hardware or {}).get('spool', {}).get('material'))}; source: {u((hardware or {}).get('temperature_resolution', {}).get('source'))}"] if hardware else []),
        *([f"- Calibration: Z offset {z}; {mesh_line(mesh)}"] if calibration else []),
        _maintenance_detail_line(),
    ])


def _filament_total_g(text):
    """Total filament grams from the slicer summary comments, or None when absent.

    OrcaSlicer/Flash spell it "; total filament used [g] = X"; Bambu Studio's
    header block spells it "; total filament weight [g] : X" (#444, verified
    against a real 2.8.2.61 slice). Absent stays None (unknown), never zero.
    """
    m = (re.search(r"^; total filament used \[g\] = ([\d.]+)", text or "", re.M)
         or re.search(r"^;\s*total filament weight \[g\]\s*:\s*([\d.]+)", text or "", re.M))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _bambu_total_estimated_time(text):
    """The header-block "total estimated time" string, for Bambu files only (#444).

    Both BBL slicers write that header totals line with the same spelling, so
    this is gated on the BambuStudio identity line and OrcaSlicer files keep
    exactly the metadata they had before.
    """
    if not re.search(r"^; BambuStudio ", text or "", re.M):
        return None
    m = re.search(r"^;.*?total estimated time\s*:\s*([^;\n]+)", text or "", re.M)
    return m.group(1).strip() if m else None


def _zmod_version(st):
    """Installed ZMod version tuple from Moonraker's update status, or None (#564).

    Fail-soft by design: any transport or parse problem is "unknown", never an
    error — auto format resolution treats unknown as plain .gcode.
    """
    try:
        status = firmware_update.fetch_status(lambda _method, path: _mr(st, path))
    except Exception:
        return None
    return gcode_3mf.version_from_status(status)


def _note_delivery_format(job, job_path, note):
    """Persist the resolved delivery format on the job so record_start lifts it
    into the print record (#564). Best effort: a failed note never blocks a print."""
    if not isinstance(job, dict) or not job_path:
        return
    try:
        job["delivery_format"] = note
        write_job(Path(job_path).parent, job)
    except Exception as exc:
        logger.warning("could not record delivery format: %s", exc)


def _zmod_upload_artifact(adapter, st, g, sha, remote):
    """Resolve the Moonraker upload artifact for a Flashforge/ZMod print (#564).

    Returns (upload_path, upload_sha256, remote, note). The default — and every
    fallback — is the plain finalized .gcode exactly as before. When the
    printer's zmod_delivery_format resolves to 3mf, the finalized G-code is
    packaged as .gcode.3mf next to itself: the upload checksum becomes the
    PACKAGE's sha256 (Moonraker verifies it against the uploaded bytes), while
    the remote name keeps the G-code's stem+sha with a .3mf suffix so
    find_job/record binding is unchanged. All verification has already run on
    the .gcode by the time this is called; nothing here re-checks or alters it.
    Callers note the returned note on the job once the upload verifies.
    """
    setting = str(adapter.config.get("printer", {}).get("zmod_delivery_format") or "gcode").strip().casefold()
    if getattr(getattr(adapter, "model", None), "vendor_key", None) != "flashforge":
        return g, sha, remote, None
    version = _zmod_version(st) if setting == "auto" else None
    fmt, reason = gcode_3mf.resolve_format(setting, version)
    if fmt != "3mf":
        return g, sha, remote, None
    nozzle = (st.get("nozzle") or {}).get("diameter_mm")
    package = gcode_3mf.package(g, gcode_3mf.package_path_for(g),
                                nozzle_mm=float(nozzle) if isinstance(nozzle, (int, float)) else None)
    note = {"format": "3mf", "setting": setting, "reason": reason,
            "zmod_version": ".".join(map(str, version)) if version else None,
            "package_sha256": file_sha256(package)}
    packaged_remote = remote[:-len(".gcode")] + ".gcode.3mf" if remote.endswith(".gcode") else remote + ".3mf"
    print(f"DELIVERY FORMAT: .gcode.3mf ({reason})", flush=True)
    return package, note["package_sha256"], packaged_remote, note


def _announce_print_name(st, job, slots=None, gcode=None):
    """Best effort: publish "<title> (<colors>)" as Klipper display_status.message.

    The Moonraker Home Assistant integration already exposes that field as the
    "Current Display Message" sensor, so no listener or HA change is needed.
    Sent before the start command because a script queued during START_PRINT
    heating would block on the gcode mutex. Never blocks or fails a print.
    """
    title = job.get("title") if isinstance(job, dict) else None
    if not title:
        title = Path(str((job or {}).get("downloaded_file") or "")).stem if isinstance(job, dict) else ""
    name = re.sub(r"[^\w .,&+#@:/-]", "", str(title or "")).strip()[:50]
    if not name:
        return None
    colors = []
    for slot in slots or []:
        row = (st.get("filament_slots") or {}).get(str(slot)) or {}
        label = " ".join(w.capitalize() for w in str(row.get("color") or "").split())
        if label and label not in colors:
            colors.append(label)
    if not colors and isinstance(job, dict):
        sel = job.get("filament_selection") or {}
        if sel.get("color"):
            colors.append(" ".join(w.capitalize() for w in str(sel["color"]).split()))
    plates = (job.get("derived_plates") or []) if isinstance(job, dict) else []
    if gcode is not None and len(plates) > 1:
        paths = [str(r.get("gcode") or "") for r in plates]
        if str(gcode) in paths:
            name = f"{name} {paths.index(str(gcode)) + 1}/{len(plates)}"
    if colors:
        name = f"{name} ({' + '.join(colors)})"
    try:
        request_json(st["moonraker_url"] + "/printer/gcode/script",
                     data={"script": f'SET_DISPLAY_TEXT MSG="{name}"'}, timeout=5,
                     headers={"Content-Type": "application/json"})
        print("PRINT NAME PUBLISHED " + json.dumps({"name": name}), flush=True)
        return name
    except Exception as exc:
        print(f"WARNING: print name not published: {exc}", flush=True)
        return None


def job_card(job, remote, gcode_text=""):
    """Copies and spools for the Home Assistant print card: one JSON document, no secrets."""
    job = job if isinstance(job, dict) else {}
    dec = effective_color_decisions(job) or {}  # the slots a #1189 fallback artifact uses
    picks = [dec["selection"]] if isinstance(dec.get("selection"), dict) else list((dec.get("choices") or {}).values())
    if not picks and isinstance(job.get("filament_selection"), dict):
        picks = [job["filament_selection"]]
    spools, seen = [], set()
    for row in picks:
        if isinstance(row, dict) and row.get("slot") not in seen:
            seen.add(row.get("slot"))
            spools.append({k: row.get(k) for k in ("slot", "color", "material", "spoolman_id")})
    grams = _filament_total_g(gcode_text)
    return {"title": job.get("title") or Path(str(remote)).stem, "file": remote,
            "copies": int(job.get("copies") or 1), "spools": spools,
            "filament_g": round(grams, 1) if grams is not None else None,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def _publish_job_card(st, job, remote, gcode_text=""):
    """Best effort: Moonraker database hermes/current_job, read by Home Assistant. Never blocks a print."""
    try:
        card = job_card(job, remote, gcode_text)
        request_json(st["moonraker_url"] + "/server/database/item",
                     data={"namespace": "hermes", "key": "current_job", "value": card}, timeout=5,
                     headers={"Content-Type": "application/json"})
        return card
    except Exception as exc:
        print(f"WARNING: job card not published: {exc}", flush=True)
        return None


_readback_status = start_truth._readback_status


START_READBACK_QUERY = ("/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard"
                        "&idle_timeout&webhooks&toolhead")


def _start_baseline(st, *, native=True):
    """The printer's status immediately before the one start request: what the readback is compared with.

    Remote names are derived from the artifact hash, so a reprint reports the same file name the previous
    run left behind (state complete, the axes still homed). Only a change from this baseline is evidence
    of the new start. {} when unreadable: an unreadable baseline proves no change, so then only a
    printing state counts."""
    try:
        status = _readback_status(_mr(st, START_READBACK_QUERY + ("&zmod_ifs" if native else "")))
    except Exception:
        return {}
    return status if isinstance(status.get("print_stats"), dict) else {}


def _baseline_heaters_off(baseline):
    """_preparation_evidence(st) is False, from an already-read baseline: every heater target read 0."""
    try:
        return all(float((baseline.get(k) or {}).get("target")) == 0 for k in ("extruder", "heater_bed"))
    except (TypeError, ValueError, AttributeError):
        return False


def _baseline_heats(baseline):
    """True only when a readable baseline shows a heater target above 0."""
    for name in ("extruder", "heater_bed"):
        try:
            if float(((baseline or {}).get(name) or {}).get("target")) > 0:
                return True
        except (TypeError, ValueError, AttributeError):
            pass
    return False


def _preparation_evidence(st):
    """True when a heater target is set (native start preparation), False when all are 0,
    None when unreadable. Read-only; used only after exactly one submission."""
    try:
        q = _mr(st, "/printer/objects/query?extruder=target&heater_bed=target")["result"]["status"]
        targets = [float((q.get(k) or {}).get("target")) for k in ("extruder", "heater_bed")]
    except Exception:
        return None
    return any(v > 0 for v in targets)


def _wait_printing(st, remote, wait_s, prep_check=None, baseline=None):
    """Wait until print_stats is printing, or the start sequence settles without printing.

    prep_check (native mapped starts): heater targets set by the printer's own preparation prove
    acceptance only provisionally (#836). Once seen, keep watching: preparation that ends with
    the heaters cold and print_stats still standby was a start the printer abandoned (an IFS load
    fault, say), so return "abandoned" then instead of reporting the early heat as a started print.

    baseline (_start_baseline): print_stats as it was just before the request. An error or cancelled
    state that is still the previous job's (same state and file) is not this start ending, and a
    printer back cold on that unchanged leftover (a reprint's state complete, same file) abandoned
    the start just as one back at standby did."""
    base_ps = (baseline or {}).get("print_stats") if isinstance((baseline or {}).get("print_stats"), dict) else {}
    leftover = (base_ps.get("state"), base_ps.get("filename")) if base_ps else None
    t0=time.monotonic(); state=None; prep_seen=False
    while time.monotonic()-t0 < wait_s:
        state,fn,busy=_printer_phase(st)
        step = start_truth.moonraker_wait_step(state, fn, remote, leftover)
        if step: return step  # this upload printing, or this start ended in error/cancelled
        if prep_check and time.monotonic()-t0 > 2:
            try:
                prep = prep_check(st)
                if prep is True: prep_seen=True
                elif start_truth.moonraker_abandoned(prep, prep_seen, state, fn, leftover):
                    return start_truth.ABANDONED  # prep ended, never printed
            except Exception: pass
        if time.monotonic()-t0 > 20 and busy != "Printing": return state  # settled, not printing
        time.sleep(1)
    return "busy" if state == "printing" else state  # printing some other file is not this start


def live_snapshot(st):
    """Current temps and every IFS slot for Job Details. Read-only; unknown stays unknown."""
    snap={"nozzle_c":None,"bed_c":None,"slots":"unknown"}
    try:
        q=_mr(st,"/printer/objects/query?extruder&heater_bed")["result"]["status"]
        snap["nozzle_c"]=q.get("extruder",{}).get("temperature")
        snap["bed_c"]=q.get("heater_bed",{}).get("temperature")
    except Exception: pass
    if not _native(): return snap
    try:
        tel=read_filament_telemetry(st); palette=tel.get("configured_palette")
        presence={}
        try: presence=read_stock_filament_slots(st)
        except Exception: pass
        if isinstance(palette,list):
            snap["slots"]=[{"slot":p.get("ID"),"color":p.get("Color"),"material":p.get("Material"),
                            "loaded":presence.get(int(p["ID"])) if str(p.get("ID")).isdigit() else None}
                           for p in palette]
    except Exception: pass
    return snap


def _moonraker_started_files(st):
    """Moonraker's recent job history as (job_id, filename, start_epoch) triples, for settling
    an uncertain earlier start (launch_state.reached_printer) and for the pre-submission
    history baseline (launch_state.acquire). Unreadable history settles nothing."""
    try:
        jobs = _mr(st, '/server/history/list?limit=20&order=desc', timeout=10)['result']['jobs']
        # A printer with a wrong clock stamps its history with that clock, which never lines up with the
        # lease time. Shift its start times onto host time so the lease can still settle (the history
        # baseline, not the clock, is what proves a run is new).
        skew = launch_state.printer_clock_skew(st.get('moonraker_url'))
        shift = skew if skew is not None and abs(skew) >= launch_state.CLOCK_SKEW_WARN_S else 0
        now = time.time()

        def shifted(start):
            # Only a run stamped by the wrong clock itself may be shifted. A run from before a
            # clock reset carries a correct stamp; shifting it by the current skew would move it
            # into the future, where reached_printer's lower bound alone (the pre-submission
            # baseline can be empty when its read failed) would accept it as this lease's run.
            # A shifted time beyond now (plus the history slack) proves a different clock epoch:
            # keep the raw stamp, which is honestly dated and cannot confirm a later lease.
            if shift > 0 and start + shift > now + launch_state.HISTORY_SLACK_S:
                return start
            return start + shift

        return [(j.get('job_id'), j.get('filename'), shifted(j.get('start_time'))) for j in jobs
                if isinstance(j, dict) and j.get('filename') and isinstance(j.get('start_time'), (int, float))]
    except Exception:
        return []


def _read_start_readback(st, remote, *, native=True, baseline=None):
    """(readable, readback): one status read after a start request that did not answer cleanly.

    The readback is classified only when print_stats names this exact upload (never another job's);
    it is None when the printer shows a different file, and `readable` is False when it could not be read.
    `baseline` (_start_baseline, read just before the request) makes only a change count as evidence.
    """
    try:
        snap = _mr(st, START_READBACK_QUERY + ("&zmod_ifs" if native else ""))  # generic Klipper has no ZMod objects
        return True, start_truth.moonraker_readback(snap, remote, baseline)
    except Exception:
        return False, None


def _reconcile_lost_start(st, remote, *, heaters_were_off=True, native=True, baseline=None):
    """What the printer says about one start request whose response was lost or rejected.

    Shared by the mapped native path (#688) and the generic Moonraker path. Evidence, in order: the printer
    reports this exact file (launch_state.same_remote via the readback), then heater targets set from a
    baseline that proved every heater was off before the request, else nothing: pending. Heater targets
    alone never count when the baseline was not proven, since they may belong to unrelated work.

    Returns the outcome fields (phase, outcome, accepted, started, message, optional readback) plus `quiet`:
    True only when the printer was readable, does not report this file and has no heater target, which is
    what lets an HTTP 4xx answer be treated as "definitely not started".
    """
    readable, readback = _read_start_readback(st, remote, native=native, baseline=baseline)
    # Heater targets are read only when print_stats does not name this file; they count only when the
    # baseline proved every heater off before the request.
    heating = _preparation_evidence(st) if readback is None else None
    truth = start_truth.moonraker(readback, heating=heating, heating_counts=bool(heaters_were_off),
                                  readable=readable)
    return start_truth.lost_fields(truth)


def _start_rejection(exc):
    """The printer's own words when `exc` is a definitive refusal of the start request, else None.

    Only an HTTP 4xx answer qualifies: the request arrived, was read and was refused (Moonraker 400
    "Must home", 404 file not found). A timeout, a reset connection and every 5xx may have reached a
    printer that went on to start, so they stay uncertain. 408, 425 and 429 describe the transport,
    not the printer's decision, and stay uncertain too.
    """
    if not isinstance(exc, urllib.error.HTTPError) or not 400 <= exc.code < 500 or exc.code in (408, 425, 429):
        return None
    detail = ""
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else (error if isinstance(error, str) else "")
    except Exception:
        pass
    return f"HTTP {exc.code}: {' '.join(str(detail or exc.reason or 'rejected').split())}"[:300]


def _settle_lost_start(lease, st, remote, exc, *, base, heaters_were_off, first_command=True, native=True,
                       baseline=None):
    """Settle a start request that raised `exc`: returns (out, rejection).

    Anything but a clean 4xx on the first command leaves the lease uncertain (never released by guessing)
    and reconciles from the printer, like the mapped native path always did. A 4xx answer on a printer
    that shows nothing happening releases the lease and the start claim and returns (None, message):
    the caller ends as REFUSED, with the printer's words. A 4xx on a printer that shows activity is
    uncertain after all.
    """
    rejection = _start_rejection(exc) if first_command else None
    if rejection is None:
        lease.uncertain(f'start request response lost: {exc!r}')
    # Moonraker answers 400 when a macro errors partway, heaters left on: after a rejection only a
    # readback naming this exact file may say started; heater targets alone are never evidence.
    verdict = _reconcile_lost_start(st, remote, heaters_were_off=heaters_were_off and rejection is None,
                                    native=native, baseline=baseline)
    quiet = verdict.pop('quiet')
    if rejection is not None:
        if quiet:
            lease.release()  # the printer refused it and shows no sign of work: nothing started
            release_start_announcement(remote)
            return None, rejection
        lease.uncertain(f'start request rejected ({rejection}) but the printer shows activity: {exc!r}')
    out = {**base, **verdict}
    if not out['accepted']:
        lease.uncertain(out['message'])
        # Nobody has announced this start, so let the listener post the "Print started" card
        # when print_stats reaches this file.
        release_start_announcement(remote)
    return out, rejection


def _rejected_start_message(rejection):
    return (f"REFUSED: the printer rejected the start request ({rejection}). "
            "The file was uploaded but nothing started.")


def _bang_lines(payload):
    """Every `!!` line in a Moonraker reply payload, however deeply nested.

    Klipper reports a macro failure (unknown command, macro error) as a `!!` console line while
    Moonraker still answers HTTP 200, so a start receipt has to be read, not just its status."""
    lines = []
    if isinstance(payload, str):
        lines.extend(ln.strip() for ln in payload.splitlines() if ln.strip().startswith("!!"))
    elif isinstance(payload, dict):
        for value in payload.values():
            lines.extend(_bang_lines(value))
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            lines.extend(_bang_lines(value))
    return lines


def _gcode_store_entries(st):
    """The gcode store entries Moonraker serves, or None when the read proves nothing."""
    try:
        entries = _mr(st, "/server/gcode_store?count=100")["result"]["gcode_store"]
    except Exception:
        return None
    return entries if isinstance(entries, list) else None


def _store_entry_mark(entry):
    """A store entry's identity for watermark matching: its message and store timestamp."""
    if not isinstance(entry, dict):
        return None
    return (entry.get("message"), entry.get("time"))


def _gcode_store_watermark(st):
    """The store's state immediately before a submission: (entry count, newest entry's mark).

    Attribution by position, never by clock (#848): entries at or before the watermark belong
    to whatever happened before the submission, however close in time, and entries after it
    are the submission's, however skewed their timestamps are. None when the store cannot be
    read at capture time — then no store entry can be attributed to the submission at all."""
    entries = _gcode_store_entries(st)
    if entries is None:
        return None
    return (len(entries), _store_entry_mark(entries[-1]) if entries else None)


def _entries_after_watermark(entries, watermark):
    """The entries that entered the store after `watermark` was captured.

    The watermark's newest entry is located first (its last occurrence, in case the same line
    was logged twice), because a capped store window can slide between capture and readback;
    the count alone is the fallback when that entry is no longer served."""
    count, mark = watermark
    if mark is not None:
        for index in range(len(entries) - 1, -1, -1):
            if _store_entry_mark(entries[index]) == mark:
                return entries[index + 1:]
    return entries[count:] if len(entries) >= count else []


def _start_receipt_error(st, receipt, *, watermark):
    """The printer's `!!` refusal of the one start just sent, or None (#836).

    The POST receipt is inspected first; when it carries no `!!` line, the gcode store is read
    back, counting only entries after the watermark captured immediately before the POST
    (#848: a `!!` line logged before the submission belongs to an earlier problem, not this
    start, and comparing its printer timestamp with host time made attribution clock-skew
    sensitive). With no watermark, or an unreadable store, there is no evidence — never a
    refusal."""
    lines = _bang_lines(receipt)
    if lines:
        return lines[0][:300]
    if watermark is None:
        return None
    entries = _gcode_store_entries(st)
    if entries is None:
        return None
    for entry in _entries_after_watermark(entries, watermark):
        if not isinstance(entry, dict) or not isinstance(entry.get("message"), str):
            continue
        found = _bang_lines(entry["message"])
        if found:
            return found[0][:300]
    return None


def _start_visibly_running(st, remote, heaters_were_off):
    """Whether the printer shows this start under way: it reports this file printing or paused, or
    heater targets are set now that were proven off immediately before the POST. Unreadable: False."""
    try:
        state, fn, _busy = _printer_phase(st)
    except Exception:
        state = fn = None
    return start_truth.moonraker_running(state, fn, remote, heaters_were_off=heaters_were_off,
                                        heating=lambda: _preparation_evidence(st))


START_OUTCOME_MARK = "START READBACK "  # "MAPPED START READBACK " contains it, so one parser reads both
_OUTCOME_KEYS = ("phase", "outcome", "started", "accepted", "file", "method", "message")


def _emit_start_outcome(out, mark=START_OUTCOME_MARK, compact=True):
    """The one uniform outcome line every launch ends with: `START READBACK {json with phase}`.

    prepared_intake and print_next read the phase from it to tell a confirmed start from an unresolved one.
    """
    line = {k: out[k] for k in _OUTCOME_KEYS if k in out} if compact else dict(out)
    if isinstance(line.get('message'), str):
        line['message'] = line['message'][:200]
    print(mark + json.dumps(line, default=str), flush=True)


def _emit_mapped_outcome(out):
    """The mapped route's outcome: the bulky record on an earlier `MAPPED START DETAIL` line (the checkpoint
    file holds it too), then the compact `MAPPED START READBACK` line last. print_next keeps only the last
    1500 characters of output, so the line the intake parses must stay short."""
    print('MAPPED START DETAIL ' + json.dumps(out, default=str), flush=True)
    _emit_start_outcome(out, mark='MAPPED START READBACK ')


def _uncertain_after_submission(lease, remote, exc, *, method, phase=start_truth.PHASE_AWAITING, extra=None,
                                mark=START_OUTCOME_MARK):
    """A failure after the start request went out (a readback that raised, a Klippy restart, a 503).

    The printer may be starting, so nothing is retried: the lease becomes uncertain, the start-announcement
    claim is released (nobody announced this start; the listener posts it when the printer reaches the
    file, which is what the chat was told), and the run ends with a clean UNCERTAIN line and outcome.
    """
    reason = ' '.join(str(exc).split()) or type(exc).__name__
    try:
        lease.uncertain(f'readback after the start request failed: {reason}')
    except Exception as lease_exc:
        logger.warning('could not mark the lease uncertain: %s', lease_exc)
    release_start_announcement(remote)
    message = f'the start was sent, but reading the printer back failed ({reason}); do not resubmit'
    out = {**(extra or {}), 'started': False, 'accepted': False, 'file': remote, 'method': method,
           **start_truth.uncertain_fields(phase), 'message': message}
    print('UNCERTAIN: ' + message, flush=True)
    if extra is not None:
        print(mark.replace('READBACK', 'DETAIL') + json.dumps(out, default=str), flush=True)
    _emit_start_outcome(out, mark=mark)
    return out


def _submission_failed(lease, remote, label, exc):
    """Upload or start raised for a transport whose client collapses both into one step: uncertain,
    claim released, outcome line printed, never resubmitted. Raises."""
    lease.uncertain(f"{label} submission did not complete: {exc!r}")
    release_start_announcement(remote)
    _emit_start_outcome({'started': False, 'accepted': False, 'file': remote, **start_truth.uncertain_fields(),
                         'message': f'{label} submission did not complete; do not resubmit'})
    if isinstance(exc, RuntimeError):
        raise SystemExit(f"{label} submission failed or uncertain: {exc}. Do not resubmit without "
                         "checking printer status.") from exc
    raise exc


# Start confirmation by polling (#271 for Bambu, now every transport): one immediate readback after the
# start request is not evidence either way, since a printer takes seconds to minutes to report the file.
START_CONFIRM_DEFAULT_S = 45
START_CONFIRM_POLL_S = 3


def _start_confirm_window_s():
    """How long a non-Bambu start is polled (Bambu has [bambu] start_confirm_s)."""
    return float(START_CONFIRM_DEFAULT_S)


class StartPoll(NamedTuple):
    confirmed: bool    # the printer reports this file printing or preparing
    preparing: bool    # ... and is still in a transitional state (heating, calibrating)
    live: dict         # the last readable normalized status
    snapshots: int
    window_s: float


def _poll_start_confirmation(read_status, remote, *, window_s=None, poll_s=START_CONFIRM_POLL_S,
                             reports=None, job_path=None, stage="start_confirm",
                             evidence_of=None, on_snapshot=None, truth=None):
    """Poll `read_status()` (a normalized status dict) until the start truth confirms or `window_s` ends.

    `truth(live, remote)` is the transport's start-truth function (start_truth.polled by default, with
    the caller's baseline bound in); a legacy yes/no `reports(live, remote)` matcher is still accepted.

    Never raises for a bad read: an exception counts as a snapshot, the window keeps running and the last
    readable status is what is reported. Every snapshot is written to the job's TIMING.jsonl so an
    unconfirmed start leaves evidence of what the printer said. Both clock hooks are module attributes so
    tests drive the window without sleeping.
    """
    window_s = _start_confirm_window_s() if window_s is None else float(window_s)
    judge = truth or (start_truth.from_reports(reports) if reports is not None else start_truth.polled)
    started = _confirm_clock()
    deadline = started + window_s
    last_good, snapshots = {}, 0
    while True:
        snapshots += 1
        error = None
        try:
            live = read_status()
            if not isinstance(live, dict):
                raise RuntimeError("the printer returned no readable status")
            last_good = live
        except Exception as exc:
            live, error = {}, " ".join(str(exc).split()) or type(exc).__name__
        evidence = {"snapshot": snapshots, "elapsed_s": round(_confirm_clock() - started, 1),
                    **(evidence_of(live) if evidence_of else {"state": live.get("state"), "file": live.get("filename")})}
        if error:
            evidence["error"] = error
        if on_snapshot:
            on_snapshot(evidence)
        _timing(job_path, stage, **evidence)
        verdict = judge(live, remote)
        if verdict.confirmed:
            return StartPoll(True, verdict.verdict == start_truth.PREPARING, live, snapshots, window_s)
        remaining = deadline - _confirm_clock()
        if remaining <= 0:
            return StartPoll(False, False, last_good, snapshots, window_s)
        _confirm_sleep(min(poll_s, remaining))


def _settle_polled_start(lease, poll, remote, *, method, uncertain_detail=None, **extra):
    """The lease and the outcome dict for a polled start.

    Confirmed (printing, or accepted and still heating/calibrating) releases the lease: the busy printer
    refuses another start. Anything else leaves it uncertain and frees the start claim so the listener
    posts the start when the printer reaches the file. Nothing here ever resubmits.
    """
    phase, outcome, message = start_truth.polled_fields(poll.confirmed, poll.preparing)
    if poll.confirmed:
        lease.release()
    else:
        lease.uncertain(uncertain_detail or (
            f"submitted, awaiting confirmation; printer reported state {poll.live.get('state') or 'unknown'}, "
            f"file {poll.live.get('filename') or 'none'!r} on the last of {poll.snapshots} snapshot(s) "
            f"over {poll.window_s:g}s"))
        release_start_announcement(remote)
    return {"started": poll.confirmed, "accepted": True, "file": remote, "method": method, "phase": phase,
            "outcome": outcome, "live": poll.live, "message": message, **extra}


def _announce_polled_outcome(out):
    """The legacy human line plus the uniform outcome line, last so a 6-line tail always holds it."""
    print(("PRINT STARTED " if out["phase"] == start_truth.PHASE_EXECUTING else "START SUBMITTED; AWAITING CONFIRMATION ")
          + json.dumps(out, default=str))
    _emit_start_outcome(out)


def _write_checkpoint(checkpoint, data):
    try:
        checkpoint.write_text(json.dumps(data, indent=2) + '\n')
    except OSError as exc:
        logger.warning('could not write the launch checkpoint %s: %s', checkpoint, exc)


def color_mismatch_key(slot, shown_live, shown_recorded):
    """The one slot color difference a Print anyway acknowledgement names (#795)."""
    return f"{slot}:{shown_live}:{shown_recorded}"


def accepted_color_mismatches(job, gcode_sha256):
    """Slot color differences the operator chose to print anyway, for this exact G-code only (#795)."""
    accepted = (job or {}).get('color_mismatch_accepted') or {}
    if not gcode_sha256 or accepted.get('gcode_sha256') != gcode_sha256:
        return frozenset()
    return frozenset(str(key) for key in accepted.get('slots') or [])


def label_mismatch_key(slot, shown_label, shown_recorded):
    """The one slot material-label difference a Print anyway acknowledgement names (#839)."""
    return f"{slot}:{shown_label}:{shown_recorded}"


def accepted_label_mismatches(job, gcode_sha256):
    """Slot material-label differences the operator chose to print anyway, for this exact G-code only.

    The label analog of accepted_color_mismatches (#795): same binding —
    the exact G-code's SHA, consumed by the caller before the start POST.
    """
    accepted = (job or {}).get('label_mismatch_accepted') or {}
    if not gcode_sha256 or accepted.get('gcode_sha256') != gcode_sha256:
        return frozenset()
    return frozenset(str(key) for key in accepted.get('slots') or [])


def _check_mapped_slot_color(slot, tool, live_raw, expected, accepted=frozenset(), found=None):
    """The printer screen's slot color against the recorded one, before a mapped launch.

    The two are compared as colors, not strings (FFFFFF == FAFAFA, 0C6283FF == 0C6283),
    and only a clear difference between two real hexes refuses. A missing, unreadable
    or merely close value prints a WARNING and proceeds, and so does a difference from
    a record hex we guessed ourselves (``hex_source`` name/default): that is our own
    guess disagreeing with the screen, not the operator's evidence of a wrong spool.

    With ``found`` (a list), an unacknowledged clear difference is appended there as
    ``(slot, tool, shown_live, shown_recorded)`` instead of refusing, so the caller can
    gather every mismatched slot and ask once (#795).
    """
    live = filament_color.normalize_hex(live_raw)
    recorded = filament_color.first_hex(expected.get('hex'))
    shown_live = f"#{live}" if live else (str(live_raw).strip() if isinstance(live_raw, str) and live_raw.strip() else 'blank')
    shown_recorded = f"#{recorded}" if recorded else 'no color code'
    # A blend spool (Spoolman's multi_color_hexes, carried on the slot record)
    # has no single color: a screen set to ANY of its colors is the same spool,
    # not a mismatch (#838). Records written before the field existed simply
    # fall through to the representative hex below, exactly as before.
    if live and any(filament_color.compare_colors(live, hx) == 'same'
                    for hx in filament_color.all_hexes(expected.get('multi_color_hexes'))):
        return
    verdict = filament_color.compare_colors(live, recorded)
    if verdict == 'same':
        return
    if verdict == 'different' and expected.get('hex_source') not in filament_color.DERIVED_SOURCES:
        if color_mismatch_key(slot, shown_live, shown_recorded) in accepted:
            print(f"WARNING: printer slot {slot} color is {shown_live} but Spoolman has {shown_recorded} "
                  f"there (T{tool}); the operator chose Print anyway for this G-code.", flush=True)
            return
        if found is not None:
            found.append((slot, tool, shown_live, shown_recorded))
            return
        # Never a hard refusal (#795): the intake asks Print anyway / set the screen / Cancel.
        raise SystemExit(f"REFUSED: printer slot {slot} color is {shown_live} but Spoolman has "
                         f"{shown_recorded} there (T{tool}). Nothing uploaded or started.")
    reason = {'unknown': 'the colors could not be compared',
              'close': 'the colors are close but not identical',
              'different': 'the recorded color code was worked out from the spool name, not read from Spoolman'}[verdict]
    print(f"WARNING: printer slot {slot} shows {shown_live} but the record has {shown_recorded} "
          f"(T{tool}); {reason}, so the launch continues.", flush=True)


def print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint):
    """Native explicit mapping route; exactly one SET_ZCOLOR submission, no fallback."""
    adapter = _job_adapter(job)
    _gate("current artifact", require_current_artifact, job, g)
    display_off = mesh.get('display_off')
    if display_off not in (0, 1):
        raise SystemExit(f"REFUSED: ZMod reports an unknown screen mode (display_off={display_off!r}), "
                         "so Print Handy cannot tell where slot and idle state come from. "
                         "Nothing uploaded or started.")
    # Screenless (display_off=1: HelixScreen or no screen) the stock firmware is not
    # running and its /detail API refuses, so idle and slot presence come from Klipper
    # alone. ZMod's own print-file macro starts AD5X prints with SET_ZCOLOR in both
    # modes; [zmod_color] runs with display: False when screenless.
    screenless = display_off == 1
    text = g.read_text()
    _embedded = adapter.native.embedded_mapping
    _gate("spool since slicing", _embedded, text, st)  # refuses a spool swapped since slicing, before any printer I/O
    # A towerless G-code on a Nopoop printer would purge nowhere (#832).
    _gate("purge", purge.require_launch_safe, text, st)
    _gate("saved-mesh start", validate_saved_mesh_start, text, mesh)
    status_query = ('/printer/objects/query?webhooks&print_stats&extruder&heater_bed&zmod_color'
                    '&filament_switch_sensor%20head_switch_sensor')
    response = _mr(st, status_query)
    status = response['result']['status']
    # #1153 item 9: the first idle check reads the status just taken instead of querying the same
    # objects again, and the slot occupancy below reuses the /detail that check last read. Every
    # later idle check (the retry loop, the one inside the lease) still reads the printer fresh.
    primed = [status]
    last_native = []
    def read_moonraker():
        current = primed.pop() if primed else _mr(st, status_query)['result']['status']
        return {'state': current['webhooks']['state'], 'print': current['print_stats'],
                'extruder': current['extruder'], 'bed': current['heater_bed']}
    if screenless:
        require_idle_screenless = adapter.native.require_idle_screenless
        def require_idle():
            _require_idle_before_mapped_launch(lambda *_: require_idle_screenless(read_moonraker),
                                               read_moonraker, None)
    else:
        require_idle_settled = adapter.native.require_idle_settled
        def read_native():
            detail = read_stock_detail(st)
            last_native[:] = [detail]
            return detail
        def require_idle():
            _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native)
    require_idle()
    primed.clear()
    sensor = status.get('filament_switch_sensor head_switch_sensor', {})
    if sensor.get('enabled') is not True and not _waive(
            "head filament sensor", 'head filament sensor protection is disabled or unknown'):
        raise RuntimeError('head filament sensor protection is disabled or unknown')
    # Idle nozzle engagement is not feeder occupancy. Genuine native errors and
    # installed runout/jam protections stay intact; do not actuate sensor overrides.
    configured = status.get('zmod_color', {}).get('slots', [])
    if screenless:
        occupancy = adapter.native.read_zmod_color_slots(status)
    else:
        occupancy = read_stock_filament_slots_from_detail(last_native[-1] if last_native else read_native())
    normalize_mapping = adapter.native.normalize_mapping
    label_matches = adapter.native.screen_label_matches
    mapped = normalize_mapping(mapping, st)
    sha = file_sha256(g)  # hashed once: the acknowledgements, the lease and the checkpoint all use it
    accepted_colors = accepted_color_mismatches(job, sha)
    accepted_labels = accepted_label_mismatches(job, sha)
    color_found = []  # every unacknowledged slot color difference, refused together below (#795)
    found_segments = []  # color and label differences in slot order, for the one combined refusal
    for tool, slot in mapped.items():
        expected = st['filament_slots'][str(slot)]
        matches = [item for item in configured if str(item.get('ID')) == str(slot)]
        # The native slot label is coarser than Spoolman's material: family match
        # (PLA == PLA+), or the screen's own type for it (SILK for Silk PLA).
        if len(matches) != 1:
            _refuse("slot list", f"REFUSED: printer slot {slot} is missing from the printer's slot list (T{tool}). "
                                 "Nothing uploaded or started.")
            continue
        label = matches[0].get('Material')
        if not label_matches(label, expected['material']):
            # A stale or blank native label gets the #795 ask-once treatment
            # too (#839): collected here, refused once with the color
            # differences below, and waived only by a Print anyway recorded
            # for this exact G-code.
            shown_label = label or 'blank'
            if label_mismatch_key(slot, shown_label, expected['material']) in accepted_labels:
                print(f"WARNING: printer slot {slot} is labeled {shown_label} but Spoolman has "
                      f"{expected['material']} there (T{tool}); the operator chose Print anyway "
                      "for this G-code.", flush=True)
            else:
                found_segments.append(f"printer slot {slot} is labeled {shown_label} but Spoolman has "
                                      f"{expected['material']} there (T{tool})")
        before = len(color_found)
        _check_mapped_slot_color(slot, tool, matches[0].get('HEX'), expected, accepted_colors, color_found)
        for c_slot, c_tool, live, recorded in color_found[before:]:
            found_segments.append(f"printer slot {c_slot} color is {live} but Spoolman has "
                                  f"{recorded} there (T{c_tool})")
        if occupancy.get(slot) is not True:
            # Operator-confirmed loaded feeders supersede idle false occupancy,
            # never unknown telemetry or an actual native error/activity.
            if (occupancy.get(slot) is not False or expected.get('physical_loading_verified') is not True) \
                    and not _waive("slot occupancy", f'T{tool} slot {slot} occupancy is unknown or unconfirmed'):
                raise RuntimeError(f'T{tool} slot {slot} occupancy is unknown or unconfirmed')
    if found_segments:
        # One refusal naming every mismatched slot, so the intake asks once (#795).
        _refuse("slot color/label", 'REFUSED: ' + '; '.join(found_segments) + '. Nothing uploaded or started.')
    remote = remote_name(g, sha, job)
    upload_path, upload_sha, remote, delivery = _zmod_upload_artifact(adapter, st, g, sha, remote)
    lease = launch_state.acquire(_printer_key(st, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=(status.get('print_stats') or {}).get('filename'),
                                 started_files=lambda: _moonraker_started_files(st))
    checkpoint, checkpoint_data = g.with_suffix('.launch.json'), None
    try:
        # Again inside the lease: the first read may predate another job's start.
        require_idle()
        _claim_bed_clear(job_path, job, g, reprint, consume=not yolo_active())
        _require_launch_checkpoint_clear(checkpoint, reprint, remote=remote)
        slots_used = sorted(set(mapped.values()))
        _timing(job_path, 'upload_begin')
        uploaded = subprocess.run([str(MOONRAKERCTL), 'upload', str(upload_path), remote, upload_sha],
                                  text=True, capture_output=True, timeout=300, env=_moonrakerctl_env(st))
        if uploaded.returncode:
            raise RuntimeError('The file could not be uploaded to the printer, so nothing started. '
                               'Check the printer is on and reachable, then try again.'
                               + _detail_tail(uploaded.stderr or uploaded.stdout))
        upload_json = json.loads(uploaded.stdout)
        g.with_suffix('.upload.json').write_text(json.dumps(upload_json, indent=2) + '\n')
        result = upload_json.get('result', upload_json)
        item = result.get('item', {}) if isinstance(result, dict) else {}
        if item.get('root') != 'gcodes' or item.get('path') != remote:
            raise RuntimeError('uploaded target mismatch: ' + json.dumps(item))
        # Integrity: Moonraker checked the SHA256 we sent and refuses the upload (HTTP 400) on mismatch,
        # so the file is never downloaded back to hash it.
        _timing(job_path, 'upload_verified')
        if delivery: _note_delivery_format(job, job_path, delivery)
        command = (f'SET_ZCOLOR FILENAME={remote} SILENT=1 AUTO_ASSIGN=0 LEVELING=0 ' +
                   ' '.join(f'T{tool}={slot}' for tool, slot in sorted(mapping.items())))
        # One checkpoint per launch attempt: keyed by this lease's token, so a deliberate reprint
        # replaces the earlier attempt's record instead of colliding with it.
        # The audit trail names BOTH artifacts (#838): `file` is the uploaded remote (the
        # .gcode.3mf package when ZMod delivery packages it), so gcode_sha256 is the inner
        # G-code's hash and upload_sha256 the bytes Moonraker actually received. `sha256`
        # stays as the G-code hash for existing readers (resolve_print_target, /print-again).
        checkpoint_data = {'phase': 'submission_pending', 'command': command, 'sha256': sha,
                           'gcode_sha256': sha, 'upload_sha256': upload_sha,
                           'file': remote, 'artifact': str(g), 'submission_count': 1,
                           'accepted': False, 'started': False, 'lease_token': lease.token}
        if delivery:
            checkpoint_data['delivery_format'] = delivery
        checkpoint.write_text(json.dumps(checkpoint_data, indent=2) + '\n')
        # This path posts its own start card, so the listener must not post a second one
        # when native preparation (heating, leveling) finishes minutes later (2026-10-11 duplicate).
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        # #836: the idle guard proved every heater target was 0 before an upload that can take
        # minutes. Re-read the targets after the upload: a preheat tapped on HelixScreen during the
        # upload is not this job's preparation, and a printer someone else is heating must not be
        # started over. An unreadable read (None) proves nothing either way: the start still goes
        # out, but with no proven-off baseline, so heater targets after a lost POST response are
        # never credited to this job. This read comes BEFORE every side effect of the start (the
        # active spool, the display message, the Home Assistant job card), so a refusal here leaves
        # none of them behind; the status baseline read immediately before the POST re-checks the
        # targets for the lost-response evidence.
        pre_start_evidence = _preparation_evidence(st)
        heaters_were_off = pre_start_evidence is False
        if pre_start_evidence is True:
            raise SystemExit("REFUSED: the printer's heaters are already on before the start was sent, "
                             "so this start was not sent; a preheat or other work is heating the printer. "
                             "Check the printer or HelixScreen, then try again. Nothing started.")
        if len(slots_used) > 1:
            print('WARNING: MULTI-SLOT JOB: automatic Spoolman usage deduction disabled; aggregate usage cannot be safely apportioned to selected spools.', flush=True)
            cleared = _clear_active_spool(st)
            if cleared.get('spoolman_pending') and job_path is not None:
                # The clear could not be written: Moonraker may keep charging
                # the stale spool during this print. Record which spool that
                # is in the job record for later reconciliation; recording
                # must never block the launch (#387).
                job['spoolman_usage_pending'] = {
                    'spool_id': None, 'stale_spool_id': cleared.get('previous'), 'slots': slots_used,
                    'reason': 'Moonraker active spool could not be cleared before a multi-slot print; the stale spool may be charged',
                    'recorded_at': time.time()}
                try:
                    write_job(Path(job_path).parent, job)
                except Exception as exc:
                    logger.warning('could not record pending Spoolman usage: %s', exc)
        else:
            _routed = _recorded_printer_id(job)
            sync = (_sync_active_spool(st, slots_used[0], printer_id=_routed) if _routed
                    else _sync_active_spool(st, slots_used[0]))
            if sync.get('spoolman_pending') and job_path is not None:
                # Usage could not be pointed at this job's spool (#387):
                # record it as pending in the job record for later
                # reconciliation. Recording must never block the launch.
                job['spoolman_usage_pending'] = {
                    'spool_id': sync.get('spool_id'), 'slot': slots_used[0],
                    'reason': 'Spoolman unavailable at print start; usage not yet reported',
                    'recorded_at': time.time()}
                try:
                    write_job(Path(job_path).parent, job)
                except Exception as exc:
                    logger.warning('could not record pending Spoolman usage: %s', exc)
        _announce_print_name(st, job, slots_used, g)
        _publish_job_card(st, job, remote, text)  # the text read at the top: the same file, read once
    except BaseException:
        lease.release()  # nothing was sent to start
        release_start_announcement(remote)  # the claim was taken in the block above
        if checkpoint_data is not None:
            _write_checkpoint(checkpoint, {**checkpoint_data, 'phase': 'not_submitted'})
        raise
    # What the printer shows just before the one POST. A reprint's remote name is the previous run's
    # (state complete, same file, axes homed), so only a change from this counts as this start's evidence.
    start_baseline = _start_baseline(st, native=True)
    if _baseline_heats(start_baseline):
        # A heater came on after the re-read above (during the spool and display steps): the
        # start still goes out, but such heat is never this job's evidence after a lost response.
        heaters_were_off = False
    lease.submitting()
    # #848: capture the gcode-store watermark immediately before the one POST, so a `!!` line
    # the store gains afterwards is attributable to this submission by position — a line that
    # was already there (an unrelated console error a second earlier) is not, and no printer
    # or host clock is ever compared.
    store_watermark = _gcode_store_watermark(st)
    # #843/#842: opt-in (PRINT_HANDY_RECORD_STARTS=1) read-only recording of the printer from here
    # until this start settles. A no-op when off; never raises, never delays or posts.
    recorder = start_recorder.begin(st, remote, command=command, method='SET_ZCOLOR', native=True,
                                    delivery=delivery, watermark=store_watermark,
                                    store_after=_entries_after_watermark, baseline=status)
    recorder.watch_lease(lease)
    recorder.note('post_sent', path='/printer/gcode/script', command=command)
    try:
        receipt = _mr(st, '/printer/gcode/script', data={'script': command})
    except Exception as exc:
        recorder.note('receipt', http_status=getattr(exc, 'code', None), error=f'{type(exc).__name__}: {exc}'[:300])
        _timing(job_path, 'start_response_lost', detail=type(exc).__name__)
        # A synchronous POST can time out after Moonraker accepted SET_ZCOLOR. Reconcile by this exact
        # submitted file only (_settle_lost_start); heater targets alone may belong to unrelated work and
        # are never attribution evidence. heaters_were_off is the re-read taken immediately before
        # this POST above, not the idle guard's pre-upload proof (#836).
        out, rejection = _settle_lost_start(lease, st, remote, exc, base=checkpoint_data,
                                            heaters_were_off=heaters_were_off, baseline=start_baseline)
        if out is None:
            _write_checkpoint(checkpoint, {**checkpoint_data, 'phase': 'rejected', 'rejection': rejection})
            recorder.finish('refused', rejection=rejection)
            raise SystemExit(_rejected_start_message(rejection)) from exc
        if out['accepted']:
            # The POST response was lost: preserve the duplicate guard until
            # explicit reconciliation, even if a matching readback is active.
            _write_checkpoint(checkpoint, out)
            if out.get('print_number') is None:
                try: out['print_number'] = _assign_print_number(job_path, artifact=g)
                except Exception as exc: print(f"WARNING: could not assign a print number: {exc}")
            try:
                out['record_path'] = str(record_start(g, remote, print_number=out.get('print_number')))
            except Exception as record_exc:
                logger.warning('start record failed after reconciled submission: %s', record_exc)
        _write_checkpoint(checkpoint, out)
        _emit_mapped_outcome(out)
        recorder.finish(out.get('outcome') or 'unknown', phase=out.get('phase'), message=out.get('message'))
        return out
    recorder.note('receipt', http_status=200, receipt=receipt)
    # Klipper can answer the POST with HTTP 200 while the macro itself failed: the receipt (or the
    # gcode store readback, past the pre-POST watermark) carries a `!!` line. That is a refusal,
    # not a pending start (#836).
    receipt_error = _start_receipt_error(st, receipt, watermark=store_watermark)
    if receipt_error is not None and _start_visibly_running(st, remote, heaters_were_off):
        # A held-open SET_ZCOLOR POST spans the printer's whole preparation, so the store window
        # can hold a `!!` from anything that ran meanwhile. A printer already running or heating
        # for this start has not refused it: confirm it like any other start instead.
        print(f"WARNING: console error after the start ({receipt_error}), but the printer is running "
              "this start; not treated as a refusal.", flush=True)
        receipt_error = None
    if receipt_error is not None:
        lease.release()
        release_start_announcement(remote)
        _write_checkpoint(checkpoint, {**checkpoint_data, 'phase': 'rejected', 'rejection': receipt_error})
        recorder.finish('refused', rejection=receipt_error)
        raise SystemExit(_rejected_start_message(receipt_error))
    _timing(job_path, 'start_submitted')
    out = {'accepted': False, 'started': False, 'phase': start_truth.PHASE_SUBMITTED,
           'file': remote, 'artifact': str(g), 'sha256': sha,
           'gcode_sha256': sha, 'upload_sha256': upload_sha, 'mapping': mapping,
           'command': command, 'receipt': receipt, 'submission_count': 1, 'print_number': None,
           'lease_token': lease.token}
    try:
        return _confirm_mapped_start(st, g, lease, remote, wait_s, out, checkpoint, job_path, delivery,
                                     recorder=recorder, baseline=start_baseline)
    except Exception as exc:
        # The request is out: a transient 503 or a Klippy restart while reading back is not a traceback
        # and not a refusal. Uncertain, never resubmitted, claim freed for the listener.
        out = _uncertain_after_submission(lease, remote, exc, method='mapped_native',
                                          phase=start_truth.PHASE_UNRESOLVED, extra=dict(out),
                                          mark='MAPPED START READBACK ')
        _write_checkpoint(checkpoint, out)
        recorder.finish(out.get('outcome') or 'unknown', phase=out.get('phase'), message=out.get('message'))
        return out


def _confirm_mapped_start(st, g, lease, remote, wait_s, out, checkpoint, job_path, delivery,
                          recorder=start_recorder.NULL, baseline=None):
    """Everything after the one SET_ZCOLOR request was accepted: number, record, wait, classify, settle.

    `baseline` is the status read just before the request (_start_baseline): a readback equal to it is
    the previous run's leftover, never this start's evidence."""
    # Number at submission, not at "printing": native preparation can outlast the readback
    # and the listener's completion card only reads the registry (2026-10-14: 6 unnumbered).
    try:
        out['print_number'] = _assign_print_number(job_path, artifact=g)
    except Exception as exc:
        logger.warning('could not assign a print number: %s', exc)
    if delivery:
        out['delivery_format'] = delivery
    _write_checkpoint(checkpoint, out)
    print('MAPPED START SUBMITTED ' + json.dumps(out), flush=True)
    # Record immediately so a long native preparation cannot lose its start record.
    try:
        out['record_path'] = str(record_start(g, remote, print_number=out.get('print_number')))
    except Exception as exc:
        logger.warning('start record failed after submission: %s', exc)
    state = _wait_printing(st, remote, wait_s, prep_check=_preparation_evidence, baseline=baseline)

    def read_readback():
        try:
            return start_truth.moonraker_readback(_mr(st, START_READBACK_QUERY), remote, baseline)
        except Exception:
            return None

    # The request was answered, and the native route heats for its own start, so set heater targets
    # are this start's preparation here (#836: provisional; the lease stays uncertain until printing).
    truth = start_truth.moonraker(read_readback, waited=state, heating=lambda: _preparation_evidence(st),
                                  heating_counts=True)
    if truth.verdict == start_truth.NOT_STARTED and truth.source == 'abandoned':
        # #848 review: the printer heated for this start (targets proven off just before the POST),
        # then went back to standby cold without ever printing it. Nothing is printing, so the
        # printer is free: release it now instead of leaving an uncertain lease that nothing
        # settles (no history entry, no print) and that refuses every later start.
        reason = 'it heated for this job, then went back to standby without printing; check the IFS slot and HelixScreen'
        lease.release()
        release_start_announcement(remote)
        _write_checkpoint(checkpoint, {**out, 'phase': 'abandoned', 'rejection': reason})
        recorder.finish('abandoned', phase='abandoned', moonraker_state=state)
        raise SystemExit(_rejected_start_message(reason))
    out.update(start_truth.mapped_fields(truth))
    _write_checkpoint(checkpoint, out)
    if truth.verdict == start_truth.STARTED:
        lease.release()  # printing; the busy printer now refuses another start
    elif truth.verdict == start_truth.PREPARING:
        # #836: preparation evidence is provisional. The printer may still abandon this start
        # (heat, fail the IFS load, drop back to standby), so the lease stays uncertain until
        # the printer reports this file printing; launch_state.acquire() self-settles it from
        # Moonraker history when the file does run. The start claim stays held too: the
        # listener edits this start's card when it prints, or reports the abandoned start.
        lease.uncertain(out['message'])
    else:
        lease.uncertain(out['message'])  # reconciled when the printer reports this file
        release_start_announcement(remote)  # unannounced: the listener posts the start card
    _emit_mapped_outcome(out)
    recorder.finish(out.get('outcome') or 'unknown', phase=out.get('phase'),
                    moonraker_state=out.get('moonraker_state'), message=out.get('message'))
    return out


def _rerecord_selected_slot(job, selection):
    """Carry a launch-time slot remap into the recorded slice recipe (2.2.5 review).

    The recipe hashes filament_selection.slot, and a remap launches the same
    G-code on another physical slot, so the recorded recipe and the binding's
    recipe_sha256 move with it. Only that one field changes: a recipe that was
    already inconsistent stays so, and every other input still has to match at
    require_current_artifact.
    """
    from ..core.slice_recipe import recipe_sha256, record
    stored = job.get('slice_recipe')
    recipe = stored.get('recipe') if isinstance(stored, dict) else None
    if not isinstance(recipe, dict) or recipe_sha256(recipe) != stored.get('sha256'):
        return
    recipe = {**recipe, 'filament': {**(recipe.get('filament') or {}), 'selected_slot': selection.get('slot')}}
    job['slice_recipe'] = record(recipe)
    binding = job.get('artifact_binding')
    if isinstance(binding, dict) and binding.get('recipe_sha256') == stored['sha256']:
        binding['recipe_sha256'] = job['slice_recipe']['sha256']


class ToolRemapRefused(RuntimeError):
    """A slot remap names a tool the file does not use, or cannot tell which tool is meant."""


def remap_slot(target, new_slot, tool=None, state=None):
    """Remap one logical tool of a job or G-code to a different physical IFS slot.

    Rewrites tool mapping metadata and checksum, updating job filament_selection
    when given a JOB.json, allowing printing with a different physical spool
    without physical changes or screen interaction.

    The tool must be one the file uses (#1305): a plate that prints only T2
    remaps T2, never an unused T0. With tool=None the file's single used tool
    is remapped; a file that uses more than one tool is refused
    (ToolRemapRefused), since one slot cannot name which tool is meant.
    """
    st = state or printer_state.load()
    resolved = resolve_print_target(target)
    target_path = Path(resolved).expanduser().resolve()

    job = None
    job_path = None
    if target_path.name == "JOB.json":
        job_path = target_path
        job = json.loads(job_path.read_text())
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: job has no sliced gcode")
        gcode_path = Path(job["gcode"]).expanduser().resolve()
    else:
        gcode_path = target_path
        temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
        for parent in list(gcode_path.parents)[:5]:
            if parent in (Path("/"), temp_root) or temp_root in parent.parents:
                break
            if (parent / "JOB.json").is_file():
                job_path = parent / "JOB.json"
                job = json.loads(job_path.read_text())
                break

    # A job bound to another roster printer remaps against that printer's
    # record and native contract, not the default's (SAFE-4 / P1-75). An
    # explicit state argument from the caller still wins for the record.
    routed_id = _recorded_printer_id(job)
    if routed_id and state is None:
        st = printer_state.load(routed_id)
    routed_adapter = _job_adapter(job) if routed_id else printers.current()

    # A split job's artifact binding hashes each derived plate apart (a dict
    # keyed like require_current_artifact builds it). The remap below must
    # update only the remapped plate's entry — and a file no derived plate
    # names has no entry to update, so it is refused here, before finalize
    # rewrites anything (P2-29).
    binding_key = None
    if job is not None and isinstance(job.get("artifact_binding"), dict):
        recorded = job["artifact_binding"].get("gcode_sha256")
        if isinstance(recorded, dict):
            for row in job.get("derived_plates") or []:
                if isinstance(row, dict) and row.get("gcode") \
                        and Path(row["gcode"]).expanduser().resolve() == gcode_path:
                    binding_key = str(row.get("binding_key") or row.get("slot"))
                    break
            if binding_key is None or binding_key not in recorded:
                raise RuntimeError(
                    f"split job artifact binding has no binding entry for {gcode_path}; "
                    "refusing to remap a plate the binding does not name")

    slot_int = int(new_slot)
    if slot_int not in range(1, FILAMENT_SLOTS + 1):
        raise ValueError(f"physical IFS slot must be an integer from 1 through {FILAMENT_SLOTS}")

    selection = resolve_filament_selection(st, {"slot": slot_int})

    raw_tools = {}
    used = set()
    if gcode_path.is_file():
        gcode_text = gcode_path.read_text()
        used = routed_adapter.native.used_tools(gcode_text)
        for line in gcode_text.splitlines():
            if line.startswith(MAP_PREFIX):
                try:
                    data = json.loads(line[len(MAP_PREFIX):])
                    for t, v in data.items():
                        raw_tools[str(t)] = v.get("physical_slot") if isinstance(v, dict) else v
                except Exception:
                    pass
                break
    used_names = ", ".join(f"T{t}" for t in sorted(used))
    if tool is None:
        if len(used) > 1:
            raise ToolRemapRefused(
                f"{gcode_path.name} uses {len(used)} tools ({used_names}); one slot cannot say which "
                "tool to move. Remap one tool with remap-slot --tool N --slot S")
        tool = str(next(iter(used))) if used else "0"
    tool = str(tool)
    if not re.fullmatch(r"\d+", tool):
        raise ToolRemapRefused(f"malformed logical tool {tool!r}")
    if used and int(tool) not in used:
        raise ToolRemapRefused(
            f"{gcode_path.name} never uses T{tool} (it uses {used_names}); "
            "remapping it would change nothing the file prints")
    mapping = dict(raw_tools) if raw_tools else {}
    if used:
        # Entries an older finalize kept for tools the file never selects
        # (a support tool with no support generated) are dropped by the
        # same rule the slice path applies (#1305).
        mapping = {t: s for t, s in mapping.items() if int(t) in used}
    mapping[tool] = slot_int
    normalize_mapping = routed_adapter.native.normalize_mapping
    mapping = normalize_mapping(mapping, st)

    new_sha = None
    if gcode_path.is_file():
        finalize = routed_adapter.native.finalize
        new_sha = finalize(gcode_path, None, st, mapping)
        try:
            _verify_routed(gcode_path, routed_adapter, st, routed_id, job=job)
        except Exception:
            offline_st = dict(st)
            offline_st.setdefault("temps_c", {"bed": 60, "nozzle": 210})
            verify_gcode_offline(gcode_path, offline_st, routed_adapter if routed_id else None)

    if job is not None and job_path is not None:
        job["filament_selection"] = selection
        _rerecord_selected_slot(job, selection)
        if new_sha:
            job["gcode_verified"] = True
            if binding_key is not None:
                # Split job: the one plate's new hash, in its own entry only;
                # the job-level single hash does not exist for a split job.
                job["artifact_binding"]["gcode_sha256"][binding_key] = new_sha
                job["artifact_binding"]["decisions_sha256"] = decisions_digest(job.get("color_decisions"))
            else:
                job["gcode_sha256"] = new_sha
                if "artifact_binding" in job and isinstance(job["artifact_binding"], dict):
                    job["artifact_binding"]["gcode_sha256"] = new_sha
                    job["artifact_binding"]["decisions_sha256"] = decisions_digest(job.get("color_decisions"))
        write_job(job_path.parent, job)

    return {
        "ok": True,
        "slot": slot_int,
        "tool": str(tool),
        "mapping": mapping,
        "selection": selection,
        "gcode": str(gcode_path) if gcode_path.is_file() else None,
        "gcode_sha256": new_sha,
        "job_json": str(job_path) if job_path else None,
    }


def _print_octoprint(adapter, state, gcode, job, job_path, wait_s, reprint):
    """A separate transport path, retaining artifact and approval guards above."""
    client = adapter.client()
    try:
        status = client.status()
        client.require_ready(status)
        observed = adapter.normalize_status(status).get('filename')
        _verify_routed(gcode, adapter, state, _recorded_printer_id(job), job=job)
        _refuse_multicolor(gcode.read_text())
    except RuntimeError as exc:
        raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    # OctoPrint preheats from these, so they are the G-code spool's, resolved before anything is sent,
    # and held to the printer's one heater ceiling (#1147): a preheat above it refuses before the lease.
    try:
        from ..core import heater_ceiling
        from ..core.nozzle import effective_nozzle_temperature
        temps = _with_gcode_temperatures(state, Path(gcode).read_text(errors='ignore'))['temps_c']
        hotend = effective_nozzle_temperature((state.get('nozzle') or {}).get('type', 'hardened_steel'), temps['nozzle'])
        for heater, target in (('nozzle', hotend), ('bed', temps['bed'])):
            heater_ceiling.require(adapter, heater, target, state=state)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f'REFUSED: OctoPrint preheat: {exc}. Nothing uploaded or started.') from exc
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed)
    try:
        try:
            ready = client.status()  # again inside the lease; also the start-truth baseline
            client.require_ready(ready)
            start_baseline = adapter.normalize_status(ready)
        except RuntimeError as exc:
            raise SystemExit(f'REFUSED: {exc}. Nothing uploaded or started.') from exc
        _claim_bed_clear(job_path, job, gcode, reprint)
    except BaseException:
        lease.release()
        raise
    # Upload and start are one uncertain step here: the client reports either failure the same way.
    lease.submitting()
    try:
        remote = client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start(remote, temps['bed'], hotend)
    except BaseException as exc:
        _submission_failed(lease, remote, 'OctoPrint', exc)
    # Accepted is not proof of printing: poll the readback over the confirmation window. No resubmission.
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.status()), remote,
                                    job_path=job_path, stage='octoprint_start_confirm',
                                    truth=lambda live, rem: start_truth.octoprint(live, rem, start_baseline))
    out = _settle_polled_start(lease, poll, remote, method='octoprint')
    confirmed = out['started']
    if confirmed:
        try: out['print_number'] = _assign_print_number(job_path, artifact=gcode)
        except Exception as exc: print(f'WARNING: could not assign a print number: {exc}')
        try:
            out['record_path'] = record_start(gcode, remote, print_number=out.get('print_number'))
        except Exception as exc:
            print(f'WARNING: start record failed: {exc}')
        # The print path starts a launched-print monitor (#373): it makes
        # the completion post, attaches the timelapse OctoPrint itself
        # rendered, and posts this print's pause alerts (#460). Prints
        # started outside Print Handy belong to the octoprint listener
        # domain (family_event_listener). A monitor that cannot start
        # never fails the print.
        try:
            from print_handy.runtime import octo_watch
            _start_family_monitor(octo_watch, adapter, remote, job)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _bambu_start_confirm_window():
    """[bambu] start_confirm_s: how long a Bambu start is polled for confirmation (default 90 s)."""
    value = (settings.current().get("bambu") or {}).get("start_confirm_s", BAMBU_START_CONFIRM_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return float(BAMBU_START_CONFIRM_S)
    return float(value)


def _confirm_bambu_start(client, adapter, remote, job_path, window_s, poll_s=BAMBU_START_POLL_S, baseline=None):
    """Poll status snapshots until the printer reports `remote` running or preparing, or `window_s` ends.

    Returns (confirmed, last normalized status, snapshots taken). The shared poll
    (_poll_start_confirmation) does the work; Bambu adds its own evidence fields (gcode_state, print_stage)
    and matcher (_bambu_reports_file). Every snapshot is logged at INFO and written to the job's
    TIMING.jsonl so an unconfirmed start leaves evidence of what the printer said, not just that it said
    nothing (#271). A read error counts as a snapshot and the window keeps running. `baseline` is the
    idle snapshot read inside the lease: only a subtask transition from it counts (start_truth.bambu).
    """
    def log(evidence):
        error = evidence.get("error")
        logger.info("Bambu start confirmation snapshot %d at %ss: state=%s gcode_state=%s file=%r stage=%s%s",
                    evidence["snapshot"], evidence["elapsed_s"], evidence["state"], evidence["gcode_state"],
                    evidence["file"], evidence["print_stage"], f" error={error}" if error else "")

    poll = _poll_start_confirmation(
        lambda: adapter.normalize_status(client.snapshot()), remote, window_s=window_s, poll_s=poll_s,
        truth=lambda live, rem: start_truth.bambu(live, rem, baseline), job_path=job_path,
        stage="bambu_start_confirm", on_snapshot=log,
        evidence_of=lambda live: {"state": live.get("state"), "gcode_state": live.get("gcode_state"),
                                  "file": live.get("filename"), "print_stage": live.get("stage")})
    return poll.confirmed, poll.live, poll.snapshots


def _print_bambu(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The Bambu Lab LAN print path (#187 stage d), mirroring _print_octoprint's guard order.

    Every check that can refuse cleanly runs before the submission lease: LAN configured,
    live snapshot idle with no readiness problems (the firmware signature bit — Developer
    Mode off — is one of them), G-code verified, package built by the same delivery the
    handoff uses and confirmed to hold exactly Metadata/plate_1.gcode. Inside the lease
    the snapshot and readiness are taken again, the bed-clear approval is consumed, and
    only then does anything reach the printer: FTPS upload (size-verified), then the
    project_file command. Upload + start are one uncertain step from submitting() on,
    exactly as for OctoPrint; acceptance is confirmed from fresh snapshots polled over the
    [bambu] start_confirm_s window (#271), never assumed from the command being accepted.
    """
    from ..core import delivery as _delivery

    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_snapshot():
        # readiness_problems covers busy/print-error/HMS and the firmware signature bit
        # (Developer Mode off), which blocks every write including this start.
        snapshot = client.snapshot()
        problems = adapter.readiness_problems(state, snapshot)
        norm = adapter.normalize_status(snapshot)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return snapshot, norm

    try:
        _snapshot, observed_norm = ready_snapshot()
        _verify_routed(gcode, adapter, state, _recorded_printer_id(job), job=job)
        packages = _delivery.bambu_handoff(job if isinstance(job, dict) else {"printer_model": adapter.key},
                                            [gcode], settings.current())
        if len(packages) != 1:
            raise RuntimeError(f"expected exactly one Bambu package for this plate, got {len(packages)}")
        package = Path(packages[0])
        with zipfile.ZipFile(package) as archive:
            plates = [n for n in archive.namelist() if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
        if plates != ["Metadata/plate_1.gcode"]:
            raise RuntimeError(f"package holds plates {plates or 'none'}; LAN print start supports a "
                               "single-plate package (Metadata/plate_1.gcode) only")
    except RuntimeError as exc:
        raise refuse(exc) from exc
    remote = package.name
    sha = file_sha256(gcode)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    # AMS mapping (#308): resolved inside the lease, against the fresh snapshot, BEFORE the
    # bed-clear approval is consumed — a job whose filaments cannot be mapped refuses with the
    # approval intact. A JOB.json ams_mapping is trusted verbatim; anything else is recomputed
    # from the snapshot's reported trays. A single-filament job that matches nothing reported
    # keeps the pre-#308 payload (source "legacy"); a multi-filament one refuses loudly here.
    from ..printers.bambulab import ams_mapping as _ams
    try:
        try:
            live_snapshot, start_baseline = ready_snapshot()  # again inside the lease: the first read may predate another start
        except RuntimeError as exc:
            raise refuse(exc) from exc
        try:
            ams_map, ams_source = _ams.resolve_for_print(
                Path(gcode).read_text(errors="ignore"), live_snapshot,
                job_mapping=(job.get("ams_mapping") if isinstance(job, dict) else None),
                dual=adapter.extruder_count() > 1)
        except _ams.MappingError as exc:
            raise refuse(exc) from exc
        _claim_bed_clear(job_path, job, gcode, reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(package, remote)
        # Claim the start (#460): the Bambu listener's sink must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote, ams_mapping=ams_map)
    except BaseException as exc:
        _submission_failed(lease, remote, "Bambu Lab", exc)
    # Accepted is not proof of printing. No resubmission on delayed readback: the printer is polled
    # over the confirmation window (#271) and the lease stays uncertain if it never reports this file.
    window_s = _bambu_start_confirm_window()
    confirmed, live, snapshots = _confirm_bambu_start(client, adapter, remote, job_path, window_s,
                                                      baseline=start_baseline)
    # Bambu has always reported a confirmed start, PREPARE included, as executing (#271).
    poll = StartPoll(confirmed, False, live, snapshots, window_s)
    out = _settle_polled_start(
        lease, poll, remote, method="bambulab_lan",
        uncertain_detail=("submitted, awaiting confirmation; printer reported state "
                          f"{live.get('state') or 'unknown'} ({live.get('gcode_state') or 'no gcode_state'}), "
                          f"file {live.get('filename') or 'none'!r} on the last of {snapshots} snapshot(s) "
                          f"over {window_s:g}s"),
        ams_mapping=ams_map, ams_mapping_source=ams_source,
        confirmation={"snapshots": snapshots, "window_s": window_s})
    if confirmed:
        try: out['print_number'] = _assign_print_number(job_path, artifact=gcode)
        except Exception as exc: print(f'WARNING: could not assign a print number: {exc}')
        try:
            out["record_path"] = record_start(gcode, remote, print_number=out.get('print_number'))
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Camera parity (#294): a monitor watches this launched print — it makes
        # the completion post with the final camera image, runs the camera
        # watchdog, and posts this print's pause alerts (#460). The Bambu
        # listener domain covers prints started outside Print Handy; a
        # monitor that cannot start never fails the print.
        try:
            from print_handy.runtime import bambu_watch
            _start_family_monitor(bambu_watch, adapter, remote, job)
        except Exception as exc:
            print(f"WARNING: camera monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _print_elegoo_sdcp(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The SDCP print path (#243), mirroring _print_prusa's guard order.

    Every check that can refuse cleanly runs before the submission lease: SDCP configured,
    live status idle with no readiness problems, G-code verified, single-tool only. Inside
    the lease the status is read again and the bed-clear approval is consumed; only then does
    anything reach the printer: the verified chunked-HTTP upload (the client read the size
    back), then the SDCP start command for that exact file. Acceptance is confirmed from a
    fresh status answer, never assumed; an unconfirmed start is uncertain and is never
    resubmitted. SDCP has no authentication, so nothing here can add any.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_status():
        raw = client.status()
        problems = adapter.readiness_problems(state, raw)
        norm = adapter.normalize_status(raw)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return raw, norm

    try:
        _raw, observed_norm = ready_status()
        _verify_routed(gcode, adapter, state, _recorded_printer_id(job), job=job)
        _refuse_multicolor(gcode.read_text(errors="ignore"))
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    try:
        try:
            _raw, start_baseline = ready_status()  # again inside the lease; also the start-truth baseline
        except RuntimeError as exc:
            raise refuse(exc) from exc
        _claim_bed_clear(job_path, job, gcode, reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote)
    except BaseException as exc:
        _submission_failed(lease, remote, "SDCP", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.status()), remote,
                                    job_path=job_path, stage="sdcp_start_confirm",
                                    truth=lambda live, rem: start_truth.polled(live, rem, start_baseline))
    out = _settle_polled_start(lease, poll, remote, method="elegoo_sdcp")
    confirmed = out["started"]
    if confirmed:
        try: out['print_number'] = _assign_print_number(job_path, artifact=gcode)
        except Exception as exc: print(f'WARNING: could not assign a print number: {exc}')
        try:
            out["record_path"] = record_start(gcode, remote, print_number=out.get('print_number'))
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#295): a monitor watches this launched print — the
        # print path starts it; it makes the completion post, posts this
        # print's pause alerts (#460), and, once an SDCP camera source is
        # verified, captures frames for a timelapse attached to that post
        # (no source is verified today — printers/elegoo_sdcp/camera.py —
        # so for now the monitor is the completion post, with the reason
        # logged). Prints started outside Print Handy belong to the
        # elegoo_sdcp listener domain (family_event_listener). A monitor
        # that cannot start never fails the print.
        try:
            from print_handy.runtime import elegoo_watch
            _start_family_monitor(elegoo_watch, adapter, remote, job)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _print_prusa(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The PrusaLink print path (#242), mirroring _print_octoprint's guard order.

    Every check that can refuse cleanly runs before the submission lease: link configured,
    live snapshot idle with no readiness problems, G-code verified, tools within the model's
    toolheads. Inside
    the lease the snapshot is taken again and the bed-clear approval is consumed; only then
    does anything reach the printer: the verified upload (PUT, size read back by the client),
    then the POST that starts that exact file. Acceptance is confirmed from a fresh snapshot,
    never assumed; an unconfirmed start is uncertain and is never resubmitted.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    try:
        client = adapter.client()
    except RuntimeError as exc:
        raise refuse(exc) from exc

    def ready_snapshot():
        snapshot = client.snapshot()
        problems = adapter.readiness_problems(state, snapshot)
        norm = adapter.normalize_status(snapshot)
        if norm["state"] not in ("standby", "complete"):
            problems.append(f"printer state is {norm['state']!r}, not idle")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        return snapshot, norm

    try:
        _snapshot, observed_norm = ready_snapshot()
        _verify_routed(gcode, adapter, state, _recorded_printer_id(job), job=job)
        _refuse_multicolor(gcode.read_text(errors="ignore"))
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed_norm.get("filename"))
    try:
        try:
            _snapshot, start_baseline = ready_snapshot()  # again inside the lease; also the start-truth baseline
        except RuntimeError as exc:
            raise refuse(exc) from exc
        _claim_bed_clear(job_path, job, gcode, reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.start_print(remote)
    except BaseException as exc:
        _submission_failed(lease, remote, "PrusaLink", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.snapshot()), remote,
                                    job_path=job_path, stage="prusalink_start_confirm",
                                    truth=lambda live, rem: start_truth.polled(live, rem, start_baseline))
    out = _settle_polled_start(lease, poll, remote, method="prusalink")
    confirmed = out["started"]
    if confirmed:
        try: out['print_number'] = _assign_print_number(job_path, artifact=gcode)
        except Exception as exc: print(f'WARNING: could not assign a print number: {exc}')
        try:
            out["record_path"] = record_start(gcode, remote, print_number=out.get('print_number'))
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#295): a monitor watches this launched print — the
        # print path starts it; it makes the completion post, posts this
        # print's pause alerts (#460), and, when the printer registers a
        # camera with PrusaLink, captures frames for a timelapse attached
        # to that post. Prints started outside Print Handy belong to the
        # prusalink listener domain (family_event_listener). A monitor
        # that cannot start never fails the print.
        try:
            from print_handy.runtime import prusa_watch
            _start_family_monitor(prusa_watch, adapter, remote, job)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


_FILAMENT_COLOUR_RE = re.compile(r'(?im)^;\s*filament_colou?r\s*=\s*([^\n]*)$')


def _gcode_filament_colors(text):
    """The slicer's per-filament colors from a G-code header (`; filament_colour = #RRGGBB;...`), in order."""
    match = _FILAMENT_COLOUR_RE.search(text or "")
    return [part.strip() for part in match.group(1).split(";") if part.strip()] if match else []


def _print_flashforge_stock(adapter, state, gcode, job, job_path, wait_s, reprint):
    """The stock Flashforge LAN path, mirroring _print_bambu's guard order.

    Every check that can refuse cleanly runs before the submission lease: host and
    credentials present, G-code free of Klipper or ZMod commands, /detail idle, then
    the rest of the G-code check. Inside the lease the snapshot is taken again, the bed-clear approval is
    consumed, and only then does anything reach the printer: one upload with print
    disabled, then /printGcode for that exact name, naming the selected IFS bay for T0
    (material and colors from the fresh /detail, the G-code's own color for the tool;
    #1147). Upload and start are one uncertain step from submitting() on. Acceptance is
    confirmed from a fresh /detail, never assumed.
    """
    def refuse(exc):
        return SystemExit(f"REFUSED: {' '.join(str(exc).split())}. Nothing uploaded or started.")

    from ..printers.flashforge_stock.client import StockClient, StockLanError
    try:
        client = adapter.client()
    except (RuntimeError, StockLanError) as exc:
        raise refuse(exc) from exc

    def selected_slot():
        selection = job.get("filament_selection") if isinstance(job, dict) else None
        if not isinstance(selection, dict) or "slot" not in selection:
            raise RuntimeError("pick one IFS slot before a stock Flashforge print")
        slot = selection["slot"]
        if type(slot) is not int or not 1 <= slot <= adapter.slot_count:
            raise RuntimeError(f"IFS slot must be an integer from 1 through {adapter.slot_count}")
        return slot

    bay = {}  # the T0 -> bay mapping /printGcode sends, from the latest ready snapshot

    def bay_mapping(detail, slot, text):
        # The printer's own report for the bay first; else the job's selection and the hardware
        # record's spool for that bay (the record binds the four bays to spools, like the ZMod AD5X).
        material, slot_color = StockClient.slot_info(detail, slot)
        selection = job.get("filament_selection") if isinstance(job, dict) else None
        selection = selection if isinstance(selection, dict) else {}
        record = ((state or {}).get("filament_slots") or {}).get(str(slot)) or {}
        if not material:
            material = str(selection.get("material") or record.get("material") or "").strip()
        slot_color = slot_color or record.get("hex")
        tool_color = (_gcode_filament_colors(text) or [None])[0] or slot_color
        try:
            return StockClient.material_mapping(slot, material=material, tool_color=tool_color,
                                                slot_color=slot_color or tool_color)
        except StockLanError as exc:
            raise RuntimeError(f"IFS slot {slot} cannot be named in the start request: {exc}") from None

    def ready_snapshot():
        detail = client.detail()
        problems = adapter.readiness_problems(state, detail)
        reduced = adapter.normalize_status(detail)
        if reduced["state"] not in ("standby", "complete", "cancelled"):
            problems.append(f"printer state is {reduced['state']!r}, not idle")
        try:
            occupied = client.slots_of(detail)
        except StockLanError as exc:
            problems.append(str(exc))
            occupied = {}
        slot = selected_slot()
        if occupied.get(slot) is not True:
            problems.append(f"IFS slot {slot} does not report filament")
        if problems:
            raise RuntimeError("; ".join(dict.fromkeys(problems)))
        bay["mapping"] = bay_mapping(detail, slot, text)
        return reduced

    try:
        text = gcode.read_text(errors="ignore")
        problems = adapter.gcode_problems(text)
        if problems:
            raise RuntimeError(problems[0])
        observed = ready_snapshot()
        _verify_routed(gcode, adapter, state, _recorded_printer_id(job), job=job)
        adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(_gcode_tools(text) | {0})}, state)
    except RuntimeError as exc:
        raise refuse(exc) from exc
    sha = file_sha256(gcode)
    remote = remote_name(gcode, sha, job)
    if not remote.endswith(".gcode"):
        remote += ".gcode"
    lease = launch_state.acquire(_printer_key(state, job), job_json=job_path, gcode_sha256=sha, remote=remote,
                                 observed_file=observed.get("filename"))
    try:
        try:
            # again inside the lease: the first read may predate another start; also the start-truth baseline
            start_baseline = ready_snapshot()
        except RuntimeError as exc:
            raise refuse(exc) from exc
        _claim_bed_clear(job_path, job, gcode, reprint)
    except BaseException:
        lease.release()
        raise
    lease.submitting()
    try:
        client.upload(gcode, remote)
        # Claim the start (#460): the family listener must not post a second start for this print.
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        client.print_gcode(remote, bay=bay["mapping"])
    except BaseException as exc:
        _submission_failed(lease, remote, "stock Flashforge", exc)
    poll = _poll_start_confirmation(lambda: adapter.normalize_status(client.detail()), remote,
                                    job_path=job_path, stage="flashforge_stock_start_confirm",
                                    truth=lambda live, rem: start_truth.polled(live, rem, start_baseline))
    out = _settle_polled_start(lease, poll, remote, method="flashforge_stock")
    confirmed = out["started"]
    if confirmed:
        try: out['print_number'] = _assign_print_number(job_path, artifact=gcode)
        except Exception as exc: print(f'WARNING: could not assign a print number: {exc}')
        try:
            out["record_path"] = record_start(gcode, remote, print_number=out.get('print_number'))
        except Exception as exc:
            print(f"WARNING: start record failed: {exc}")
        # Timelapse (#373): a monitor watches this launched print — the
        # print path starts it; it captures frames from the camera source
        # the printer advertises, makes the completion post with the
        # timelapse attached, and posts this print's pause alerts (#460).
        # Prints started outside Print Handy belong to the
        # flashforge_stock listener domain (family_event_listener). A
        # monitor that cannot start never fails the print.
        try:
            from print_handy.runtime import stock_watch
            _start_family_monitor(stock_watch, adapter, remote, job)
        except Exception as exc:
            print(f"WARNING: timelapse monitor did not start: {exc}")
    _announce_polled_outcome(out)
    return out


def _confirm_generic_start(st, remote, wait_s, lease, baseline=None):
    """Wait for the one accepted start to show as printing: (out, readback, state).

    Only a readback naming THIS file counts (launch_state.same_remote): printing, or preparing (lease
    released, claim kept: the start card is posted), or failed/cancelled (lease released, REFUSED-style
    SystemExit). Anything else, including a still-idle printer, a stale previous filename or an unreadable
    status, leaves the lease uncertain and frees the claim for the listener: after a 200 the printer may
    still be starting. `out` is None when printing. A readback exception propagates to the caller, which
    settles it as uncertain. `baseline` (_start_baseline) keeps a reprint's leftover state from counting.
    """
    state = _wait_printing(st, remote, wait_s, baseline=baseline)
    readable, readback = _read_start_readback(st, remote, native=False, baseline=baseline)
    truth = start_truth.moonraker(readback, waited=state, readable=readable)
    if truth.verdict == start_truth.STARTED:
        return None, readback, state
    if truth.verdict == start_truth.PREPARING:
        lease.release()  # accepted; the busy printer now refuses another start
        return start_truth.generic_fields(truth, file=remote, method="print/start"), readback, state
    if truth.verdict == start_truth.NOT_STARTED:
        release_start_announcement(remote)
        lease.release()  # the printer reports this file's start failed
        raise SystemExit("The printer did not start the print, and it was not retried. "
                         "Check the printer screen before trying again." + _detail_tail(readback.message))
    lease.uncertain("submitted, awaiting confirmation")  # reconciled when the printer reports this file
    release_start_announcement(remote)
    return start_truth.generic_fields(truth, file=remote, method="print/start"), readback, state


def _owning_job(g):
    """The (job, job_path) that owns a resolved print target, else (None, None).

    A JOB.json target names its job directly. A G-code target belongs to
    the nearest ancestor JOB.json: slices live at
    <job>/sliced/orca/<uuid>/plate_1.gcode (four levels), so search every
    nearby ancestor and an older slice is always bound to its JOB. The
    climb stops at the temp root too: this profile sets TMPDIR inside
    itself, so a fixture such as <scratch>/tmpXXX/x.gcode must not climb
    into a real profile JOB.json.
    """
    if g.name == "JOB.json":
        return json.loads(g.read_text()), g
    temp_root = Path(os.environ.get("TMPDIR") or "/tmp").resolve()
    for parent in list(g.parents)[:5]:
        if parent in (Path("/"), temp_root) or temp_root in parent.parents:
            break
        if (parent / "JOB.json").is_file():
            job_path = parent / "JOB.json"
            return json.loads(job_path.read_text()), job_path
    return None, None


def _print_now_control_lock(target):
    """The shared interprocess control lock for this print's printer (P1-33).

    Keyed by the owning job's recorded printer_id, resolved the same way
    _print_now_locked resolves the job it launches — from a JOB.json
    target, or from the ancestor JOB.json of a G-code target (the chat
    flow's print_next.start passes the G-code path, not JOB.json) — else
    the default printer: the same identity _printer_key leases by.
    Resolution is read-only and defensive: any failure falls back to the
    default printer and the print body raises its own error.
    """
    from ..core import control_lock as _cl
    key = None
    try:
        g = Path(resolve_print_target(target)).expanduser().resolve()
        job, _job_path = _owning_job(g)
        if isinstance(job, dict):
            key = job.get("printer_id")
    except Exception:
        key = None
    return _cl.control_lock(key)


# Receipt statuses a print-now start may settle: the intake sliced the job
# and no start has been recorded as confirmed. Terminal receipts (cancelled,
# expired, slice-failed, …) are history and are never rewritten by a start.
_RECEIPT_STARTABLE_STATUSES = frozenset(
    {"SLICED", "SLICED_NOT_STARTED", "START_REQUESTED", "START_REFUSED", "START_PENDING"})


def _start_confirmed(out):
    """Whether a print_now outcome dict confirms the printer took the start."""
    if not isinstance(out, dict):
        return False
    if out.get("started") is True:
        return True
    return bool(out.get("accepted")) and str(out.get("phase") or "") in start_truth.CONFIRMED_PHASES


def _start_pending(out):
    """Whether a print_now outcome was accepted but is not yet confirmed.

    Mirrors prepared_intake.classify_start: an accepted start in an
    unresolved phase is START_PENDING (started unknown), never STARTED.
    """
    if not isinstance(out, dict) or _start_confirmed(out):
        return False
    return bool(out.get("accepted")) and str(out.get("phase") or "") in start_truth.PENDING_PHASES


def _find_intake_receipt(job, job_path, gcode, sha):
    """(path, receipt) of the intake receipt owning this exact artifact, else (None, None).

    A receipt owns the artifact when the hash it recorded for this G-code
    (``gcode_sha256``) equals the artifact's hash. A candidate tied to this
    job — by its intake_id, its JOB.json or its G-code list — whose recorded
    hash differs (or that records no hash for the artifact) is a mismatch:
    it is logged and that receipt is left alone (#970).
    """
    iid = job.get("intake_id") if isinstance(job, dict) else None
    gname = str(gcode)
    resolved = str(Path(gcode).resolve())
    job_json = str(Path(job_path).resolve()) if job_path is not None else None
    candidates = []
    if isinstance(iid, str) and re.fullmatch(r"[a-f0-9]{64}", iid):
        candidates.append(INTAKE_RECEIPTS / f"{iid}.json")
    try:
        rest = sorted(INTAKE_RECEIPTS.glob("*.json"))
    except OSError:
        rest = []
    candidates.extend(p for p in rest if p not in candidates)
    for path in candidates:
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(receipt, dict):
            continue
        hashes = receipt.get("gcode_sha256") or {}
        recorded = None
        for key in (gname, resolved):
            if key in hashes:
                recorded = hashes[key]
                break
        tied = bool((iid and receipt.get("intake_id") == iid)
                    or (job_json and receipt.get("job_json") == job_json)
                    or gname in (receipt.get("gcodes") or [])
                    or resolved in (receipt.get("gcodes") or []))
        if recorded is not None:
            if recorded == sha:
                return path, receipt
            logger.warning("intake receipt hash mismatch: receipt %s recorded %s for %s, "
                           "artifact hash is %s; receipt left unchanged",
                           path.name, recorded, gname, sha)
        elif tied:
            logger.warning("intake receipt hash mismatch: receipt %s records no hash for %s, "
                           "artifact hash is %s; receipt left unchanged", path.name, gname, sha)
    return None, None


def _settle_intake_start_claim(receipt, gcode, sha):
    """Settle the chat one-start claim for this intake target as started (#970).

    The chat start path (print_next.start) claims (plan, part, copy, hash)
    before spawning print-now; an out-of-band CLI start takes no claim, so
    without this a later chat tap on the same target would not be refused
    as a duplicate. Best effort: any failure is logged, never raised.
    """
    try:
        iid = str(receipt.get("intake_id") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", iid):
            return
        target = None
        saved = receipt.get("retry_target")
        if isinstance(saved, dict) and saved.get("gcode_sha256") == sha and saved.get("plan_id"):
            target = {"plan_id": saved["plan_id"], "part_id": saved.get("part_id") or "plate1",
                      "copy": int(saved.get("copy") or 1), "gcode": str(gcode), "gcode_sha256": sha}
        if target is None:
            gcodes = receipt.get("gcodes") or []
            part_id = "plate1"
            if len(gcodes) > 1:
                resolved = [str(Path(x).resolve()) for x in gcodes]
                if str(Path(gcode).resolve()) in resolved:
                    part_id = f"plate{resolved.index(str(Path(gcode).resolve())) + 1}"
            plan_id = (f"yolo-{iid[:16]}" if receipt.get("yolo")
                       else (receipt.get("split_plan") or f"intake-{iid[:16]}"))
            target = {"plan_id": plan_id, "part_id": part_id, "copy": 1,
                      "gcode": str(gcode), "gcode_sha256": sha}
        print_next = _external.load("print_next")
        path = print_next.claim_path(target)
        if print_next._take_claim(path, target) is None:
            print_next._settle_claim(path, "started", release=False)
    except Exception as exc:
        logger.warning("intake start claim settle failed: %s", exc)


def _record_intake_receipt_started(job, job_path, gcode, sha, out):
    """Move the intake receipt owning this started artifact to started (#970).

    Confirmed starts become STARTED with started=True; accepted but
    unconfirmed starts become START_PENDING with started=None, exactly as
    the chat intake classifies its own starts. Records started_via as
    'print-now' and the print id print-now assigned. Best effort: the print
    already started, so a receipt problem is logged, never raised. Returns
    the updated receipt path, or None when no receipt was updated.
    """
    confirmed, pending = _start_confirmed(out), _start_pending(out)
    if not (confirmed or pending):
        return None
    try:
        path, receipt = _find_intake_receipt(job, job_path, gcode, sha)
        if path is None:
            return None
        if receipt.get("started") is True or receipt.get("status") == "STARTED":
            return None  # a start is already recorded; history stays as written
        if receipt.get("status") not in _RECEIPT_STARTABLE_STATUSES:
            logger.info("intake receipt %s left at %s by print-now start", path.name, receipt.get("status"))
            return None
        print_number = out.get("print_number") if isinstance(out, dict) else None
        if confirmed:
            receipt.update(status="STARTED", started=True, uploaded=True)
        else:
            receipt.update(status="START_PENDING", started=None, uploaded=True,
                           start_outcome="unknown")
        receipt.update(started_via="print-now", started_gcode=str(gcode),
                       started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        if print_number is not None:
            receipt.update(print_number=print_number, print_id=print_number)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(receipt, indent=2) + "\n")
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("intake receipt update failed for %s: %s", gcode, exc)
        return None
    _settle_intake_start_claim(receipt, gcode, sha)
    return path


def print_now(target, wait_s=25, accept_default_z_offset=False, reprint=False, remap_slot_num=None,
              yolo=False, bed_clear=None):
    """Bed is confirmed clear: verify, upload, start, confirm, record. One command.

    The whole start holds the printer's interprocess control lock (P1-33):
    a purge, slot load or nozzle change from any other process — a
    standalone CLI, the listener, another bot — refuses instead of
    overlapping the start, and the start refuses while one is in flight.

    ``yolo`` is the operator's explicit "print it, skip the checks": every
    waivable gate reports instead of refusing (see ``_waive``) and the
    single-use bed-clear record is not written. A yolo run is also a
    deliberate reprint.

    ``bed_clear`` is the operator's bed-clear confirmation, required by every
    transport before anything is uploaded (#1147): True for an explicit
    confirmation of this run, or the SHA-256 of the exact G-code confirmed.
    Without it the start refuses, yolo included.
    """
    lock = _print_now_control_lock(target)
    if not lock.acquire(blocking=False):
        raise SystemExit("REFUSED: another printer control action (purge, slot load or nozzle change) "
                         "is in flight for this printer. Nothing uploaded or started.")
    token = _YOLO.set(bool(yolo))
    clear_token = _BED_CLEAR.set(bed_clear) if bed_clear is not None else None
    try:
        if yolo:
            print("YOLO: the operator chose to print without checks; failed checks are reported, "
                  "not enforced, except the axis limits and heater maximums.", flush=True)
        out = _print_now_locked(target, wait_s=wait_s,
                                accept_default_z_offset=accept_default_z_offset,
                                reprint=reprint or bool(yolo), remap_slot_num=remap_slot_num)
    finally:
        if clear_token is not None:
            _BED_CLEAR.reset(clear_token)
        _YOLO.reset(token)
        lock.release()
    # The start happened: reconcile the intake receipt that owns this
    # artifact (#970). Best effort — it never fails a started print.
    if _start_confirmed(out) or _start_pending(out):
        try:
            g = Path(resolve_print_target(target)).expanduser().resolve()
            job, job_path = _owning_job(g)
            if g.name == "JOB.json" and isinstance(job, dict) and job.get("gcode"):
                g = Path(job["gcode"]).expanduser().resolve()
            if g.is_file():
                _record_intake_receipt_started(job, job_path, g, file_sha256(g), out)
        except Exception as exc:
            logger.warning("intake receipt reconciliation failed: %s", exc)
    return out


def _print_now_locked(target, wait_s=25, accept_default_z_offset=False, reprint=False, remap_slot_num=None):
    """print_now's body, run under the printer's interprocess control lock."""
    if remap_slot_num is not None:
        try:
            remap_res = remap_slot(target, remap_slot_num)
        except ToolRemapRefused as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing uploaded or started.") from exc
        print(f"SLOT REMAPPED: T{remap_res['tool']} -> slot {remap_res['slot']} ({remap_res['selection']['color']} {remap_res['selection']['material']})", flush=True)
    g=Path(resolve_print_target(target)).expanduser().resolve()
    job, job_path = _owning_job(g)
    if g.name=="JOB.json":
        if not job.get("gcode"):
            raise RuntimeError("stale color artifact: reslice current JOB before launch")
        g=Path(job["gcode"]).expanduser().resolve()
    cur = Path(job["gcode"]).expanduser().resolve() if isinstance(job, dict) and job.get("gcode") else None
    if (cur and cur.parent != g.parent and cur.is_file() and isinstance(job, dict)
            and not job.get("derived_plates")):
        # A file from an earlier slice of this job (2026-10-10: the bot reran the pre-reslice name).
        _refuse("older slice", f"REFUSED: {g.name} is an older slice of this job. The current slice is {cur}; "
                               f"run print-now {job_path} instead. Nothing uploaded or started.")
    partial = partial_job_refusal(job)
    if partial:
        _refuse("partial job", f"REFUSED: {partial}. Nothing uploaded or started.")
    _gate("current artifact", require_current_artifact, job, g)
    if not g.is_file(): raise SystemExit(f"REFUSED: no G-code at {g}")
    # The job's printer (#334): a recorded printer_id resolves the roster entry, its adapter
    # and its hardware record; a record written before the upgrade has no id and prints on
    # the configured single printer, exactly as it always has.
    printer_id = job.get("printer_id") if isinstance(job, dict) else None
    if printer_id:
        from print_handy import settings as _pb_settings  # this function shadows `settings` locally below
        try:
            entry = _pb_settings.by_id(printer_id)
        except _pb_settings.SettingsError as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing uploaded or started.")
        have = printers.job_model(job)
        if have != entry["model"]:
            _refuse("printer model", f"REFUSED: this job was prepared for {have}, but printer {printer_id!r} is an "
                                     f"{entry['model']}. Nothing uploaded or started.")
        adapter = printers.get(entry["model"], _pb_settings.view_for(printer_id))
    else:
        try:
            if isinstance(job, dict): printers.check_job_model(job)
        except printers.JobModelMismatch as exc:
            _refuse("printer model", f"REFUSED: {exc}. Reslice it for the configured printer; nothing uploaded or started.")
        adapter = printers.current()
    _timing(job_path, 'print_now_start')
    # accept_default_z_offset is kept for CLI compatibility only. With display_off=0 ZMod applies the
    # native screen zProbeOffset, which is the correct calibrated value; no Z_OFFSET in START_PRINT is fine.
    st=printer_state.load(printer_id) if printer_id else printer_state.load()
    if getattr(adapter, 'transport', None) == 'octoprint':
        return _print_octoprint(adapter, st, g, job, job_path, wait_s, reprint)
    if getattr(adapter, 'transport', None) == 'flashforge_stock':
        return _print_flashforge_stock(adapter, st, g, job, job_path, wait_s, reprint)
    if getattr(adapter, 'transport', None) == 'prusalink':
        from ..core import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_prusa(adapter, st, g, job, job_path, wait_s, reprint)
        try:
            _verify_routed(g, adapter, st, printer_id, job=job)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over PrusaLink; the link status alone is "
                         f"read-only). {g} passed offline checks; move the file yourself (not yet tested on "
                         "a printer). Nothing uploaded or started.")
    if getattr(adapter, 'transport', None) == 'elegoo_sdcp':
        from ..core import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_elegoo_sdcp(adapter, st, g, job, job_path, wait_s, reprint)
        try:
            _verify_routed(g, adapter, st, printer_id, job=job)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over SDCP; status alone is "
                         f"read-only). {g} passed offline checks; move the file yourself (not yet tested on "
                        "a printer). Nothing uploaded or started.")
    if getattr(adapter, 'transport', None) == 'bambulab_lan':
        from ..core import delivery as _delivery
        if 'printer' in _delivery.methods():
            return _print_bambu(adapter, st, g, job, job_path, wait_s, reprint)
        # Print delivery is not enabled for this printer: verify the file for manual handoff;
        # the bed-clear approval is left unused, exactly as for the offline path below.
        try:
            _verify_routed(g, adapter, st, printer_id, job=job)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} print delivery is not enabled in Print Handy (add "
                         f"'printer' to [delivery] methods to print over LAN; LAN status alone is "
                         f"read-only). {g} passed offline checks; use the Bambu package delivery and "
                         "move the file yourself (not yet tested on a printer). Nothing uploaded or started.")
    if adapter.connection == 'offline':
        # No connection exists. Verify the file for manual handoff; the bed-clear approval is left unused.
        try:
            _verify_routed(g, adapter, st, printer_id, job=job)
        except RuntimeError as exc:
            raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
        raise SystemExit(f"REFUSED: {adapter.label} has no live connection in Print Handy (no upload, start or "
                         f"control). {g} passed offline checks; use the Bambu package delivery and move the file "
                         "yourself (not yet tested on a printer). Nothing uploaded or started.")
    state,fn,busy=_require_idle(st)
    mesh=mesh_readiness(st)
    if mesh["problem"]:
        _refuse("bed mesh", f"REFUSED: {mesh['problem']}. Nothing uploaded or started.")
    selection = job.get("filament_selection") if isinstance(job, dict) else None
    mapping = None
    try:
        # Pre-upload checks. Report them as one REFUSED line so print_next releases the plate's
        # claim and the operator can retry after fixing the cause (a traceback kept it held).
        _gate("G-code safety checks", _verify_routed, g, adapter, st, printer_id, job=job)
        if yolo_active():
            require_mechanical_limits(g, st, adapter)  # never waived (S6)
        _timing(job_path, 'preflight_verify_done')
        # Read once: the tool-map/multicolor checks, the upload SHA and the job card all
        # use these bytes, so the SHA Moonraker checks is the SHA of the bytes checked here.
        gcode_raw = g.read_bytes()
        gcode_text = _text_like_read_text(gcode_raw)  # strict, like the g.read_text() it replaces
        if adapter.multi_slot:
            embedded_mapping = adapter.native.embedded_mapping
            normalize_mapping = adapter.native.normalize_mapping
            raw_mapping = _gate("slot mapping", embedded_mapping, gcode_text, st)
            mapping = (_gate("slot mapping", normalize_mapping, raw_mapping, st)
                       if raw_mapping is not None else None)
        else:
            _gate("multicolor", _refuse_multicolor, gcode_text)
    except (RuntimeError, MultiColorUnsupported) as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    if mapping is not None:
        return print_mapped_native(g, st, mesh, mapping, job, job_path, wait_s, reprint)
    try:
        _gate("filament readiness", _verify_filament_readiness, st, selection, adapter=adapter)
    except RuntimeError as exc:
        if adapter.multi_slot: raise
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing uploaded or started.")
    sha=hashlib.sha256(gcode_raw).hexdigest(); remote=remote_name(g, sha, job)
    text=gcode_text  # the strict decode succeeded, so errors="ignore" would give the same text
    if mesh.get('display_off') in (0, 1):
        try:
            validate_saved_mesh_start(text, mesh)
        except RuntimeError as exc:
            _refuse("saved-mesh start", f'REFUSED: {exc}. Reslice with the reviewed machine profile; nothing uploaded.')
    if mesh.get('display_off') == 0:
        try:
            validate_native_start(read_stock_detail(st), selection)
        except Exception as exc:
            _refuse("native preflight", f'REFUSED: native preflight: {exc}. Nothing uploaded or started.')
    upload_path, upload_sha, remote, delivery = _zmod_upload_artifact(adapter, st, g, sha, remote)
    lease=launch_state.acquire(_printer_key(st, job), job_json=job_path, gcode_sha256=sha, remote=remote, observed_file=fn,
                               started_files=lambda: _moonraker_started_files(st))
    try:
        _require_idle(st)  # again inside the lease: the first read may predate another job's start
        # Yolo owes no single-use button record, but the operator's bed-clear confirmation is never waived.
        _claim_bed_clear(job_path, job, g, reprint, consume=not yolo_active())
        _timing(job_path, 'upload_begin')
        r=subprocess.run([str(MOONRAKERCTL),"upload",str(upload_path),remote,upload_sha],text=True,capture_output=True,timeout=300,
                         env=_moonrakerctl_env(st))
        if r.returncode: raise SystemExit("The file could not be uploaded to the printer, so nothing started. "
                                          "Check the printer is on and reachable, then try again."
                                          + _detail_tail(r.stderr or r.stdout))
        try:
            up_json=json.loads(r.stdout)
        except Exception as e:
            raise SystemExit("The printer's reply to the upload could not be understood, so nothing started. "
                             "Try again; if it keeps happening, check the printer screen."
                             + _detail_tail(str(e)))
        res=up_json.get("result", up_json) if isinstance(up_json, dict) else {}
        item=res.get("item") if isinstance(res, dict) else None
        if not isinstance(item, dict) or item.get("root")!="gcodes" or item.get("path")!=remote:
            raise SystemExit("The printer did not confirm the uploaded file landed where it should, so nothing started. "
                             "Try again; if it keeps happening, check the printer screen."
                             + _detail_tail(json.dumps(up_json)[:300]))
        # Moonraker verified the SHA256 sent with the upload (mismatch = HTTP 400 = the upload failure above).
        _timing(job_path, 'upload_verified')
        if delivery: _note_delivery_format(job, job_path, delivery)
        try: claim_start_announcement(remote)
        except OSError as e: print(f"WARNING: start announcement claim failed, listener may also post: {e}")
        _announce_print_name(st, job, gcode=g)
        _publish_job_card(st, job, remote, text)
    except BaseException:
        lease.release()  # nothing was sent to start
        release_start_announcement(remote)  # the claim was taken in the block above
        raise
    # Baseline for the lost-response reconcile: heater targets seen later prove this start only if every
    # heater was off before it. Read-only; unreadable counts as not proven.
    # The same read is the readback baseline: a reprint's remote name is the previous run's (state
    # complete, same file), so only a change from the status just before the request is evidence.
    start_baseline = _start_baseline(st, native=False)
    heaters_were_off = _baseline_heaters_off(start_baseline)
    lease.submitting()
    # #843/#842: the opt-in read-only start recording (PRINT_HANDY_RECORD_STARTS=1); a no-op when off.
    recorder = start_recorder.begin(st, remote, command=f"print/start {remote}", method="print/start",
                                    native=False, delivery=delivery)
    recorder.watch_lease(lease)
    sent = 0
    try:
        for command in adapter.moonraker_start_commands(remote):
            recorder.note("post_sent", path=command["path"], body=command["body"])
            receipt = _mr(st, command["path"], data=command["body"])
            recorder.note("receipt", http_status=200, path=command["path"], receipt=receipt)
            sent += 1
    except BaseException as exc:
        recorder.note("receipt", http_status=getattr(exc, "code", None), error=f"{type(exc).__name__}: {exc}"[:300])
        if not isinstance(exc, Exception):
            lease.uncertain(f"start request did not complete: {exc!r}")
            recorder.finish("unknown", phase="interrupted")
            raise
        _timing(job_path, 'start_response_lost', detail=type(exc).__name__)
        # The 30 s POST can time out after Moonraker accepted it, like the mapped native route's:
        # reconcile from the printer (exact file, else heater evidence, else pending). An HTTP 4xx
        # answer to the first command is a refusal and ends as REFUSED with the printer's message.
        out, rejection = _settle_lost_start(lease, st, remote, exc, heaters_were_off=heaters_were_off,
                                            first_command=sent == 0, native=False,
                                            base={"file": remote, "method": "print/start"},
                                            baseline=start_baseline)
        if out is None:
            recorder.finish("refused", rejection=rejection)
            raise SystemExit(_rejected_start_message(rejection)) from exc
        if out["accepted"]:
            if out.get("print_number") is None:
                try: out["print_number"] = _assign_print_number(job_path, artifact=g)
                except Exception as exc: print(f"WARNING: could not assign a print number: {exc}")
            try:
                out["record_path"] = str(record_start(g, remote, print_number=out.get("print_number")))
            except Exception as record_exc:
                print(f"WARNING: print start record failed: {record_exc}")
        print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
        _emit_start_outcome(out)
        recorder.finish(out.get("outcome") or "unknown", phase=out.get("phase"))
        return out
    _timing(job_path, 'start_submitted')
    print("START REQUEST ACCEPTED " + json.dumps({"file": remote, "printing_confirmed": False}), flush=True)
    method="print/start"
    try:
        out, readback, state = _confirm_generic_start(st, remote, wait_s, lease, baseline=start_baseline)
    except SystemExit:
        recorder.finish("failed", phase="failed")  # the printer reported this file's start failed
        raise
    except Exception as exc:
        # The request is out: a transient 503 or a Klippy restart while reading back is neither a
        # traceback nor a refusal. Uncertain, never resubmitted, claim freed for the listener.
        out = _uncertain_after_submission(lease, remote, exc, method=method)
        recorder.finish("unknown", phase=out.get("phase"), message=out.get("message"))
        return out
    if out is not None:
        print("START SUBMITTED; AWAITING CONFIRMATION " + json.dumps(out), flush=True)
        _emit_start_outcome(out)
        recorder.finish(out.get("outcome") or "unknown", phase=out.get("phase"))
        return out
    lease.release()  # printing; the busy printer now refuses another start
    recorder.finish("printing", phase=start_truth.PHASE_EXECUTING, moonraker_state=state)
    # Mint this launch's number BEFORE the start record, so the record (and
    # the completion record built from it) carries this print's own number.
    try: print_no=_assign_print_number(job_path, artifact=g)
    except Exception as e: print_no=None; print(f"WARNING: could not assign a print number: {e}")
    record_path=None
    try: record_path=record_start(g, remote, print_number=print_no)
    except Exception as e: print(f"WARNING: print start record failed: {e}")
    if not isinstance(record_path,(str,os.PathLike)): record_path=None
    elif record_path is not None: record_path=str(record_path)
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    est=gcode_estimate_s(text); fg=_filament_total_g(text)
    warm=5*60; now=datetime.now(ZoneInfo(TIMEZONE))
    title=(job or {}).get("title") or g.stem
    start_verdict = start_truth.moonraker(readback, waited=state)  # the verdict _confirm_generic_start reached
    out={"started":start_verdict.confirmed,"title":title,"print_number":print_no,"file":remote,"method":method,
         **start_truth.generic_fields(start_verdict),
         "print_time":human_dur(est) if est else None,"filament_g":round(fg) if fg is not None else None,
         "finish_et":(now+timedelta(seconds=(est or 0)+warm)).strftime("%-I:%M %p %a") if est else None}
    if delivery: out["delivery_format"]=delivery
    settings=start_print_settings(text); z_notice=z_offset_notice(text)
    try: hardware=st.get("printer","Klipper")+", "+printer_state.summary(st)
    except (KeyError, TypeError): hardware=None
    out["z_offset_notice"]=z_notice; out["record_path"]=record_path
    live=live_snapshot(st); out["live"]=live
    screen_z=mesh.get("z_offset_mm"); out["screen_z_offset_mm"]=screen_z; out["native_screen_z_offset_mm"]=screen_z_offset(st); out["bed_mesh"]=mesh
    out["details"]=job_details(out, hardware=st, selection=selection, settings=settings,
                               record_path=record_path, z_notice=z_notice, live=live,
                               screen_z=screen_z, mesh=mesh, printer=st.get("printer") or (None if adapter.multi_slot else adapter.label),
                               nozzle_mm=(st.get("nozzle") or {}).get("diameter_mm"), calibration=adapter.has_native_screen)
    print("PRINT STARTED " + json.dumps(out))
    print(out["details"])
    clock_note = launch_state.clock_warning(launch_state.printer_clock_skew(st.get("moonraker_url")))
    if clock_note:  # a warning, never a blocker: the print already started
        out["clock_warning"] = clock_note
        print(clock_note, flush=True)
    _emit_start_outcome(out)  # last, so a short tail always carries the phase
    return out
