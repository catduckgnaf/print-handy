"""The gates a print start must pass: the current artifact, bed clear, idle printer, launch
checkpoint, filament and mesh readiness, mechanical limits, and the yolo waivers.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import contextvars, hashlib, json, math, re, time
import urllib.request
from pathlib import Path

from .. import printers
from ..core import launch_state
from ..core.heater_ceiling import MACRO_BED_ARGS as _MACRO_BED_ARGS, MACRO_NOZZLE_ARGS as _MACRO_HOTEND_ARGS
from ..gcode.artifact import file_sha256
from ..gcode.bounds import NUMBER, cached_motion_bounds
from ..jobs.common import FILAMENT_SLOTS, _mr, request_json
from ..jobs.filament import (
    FILAMENT_SENSOR_OBJECTS, _validate_slot_selection, apply_stock_presence, read_stock_detail,
    read_stock_filament_slots_from_detail, resolve_filament_selection, telemetry_state,
    validate_filament_selection,
)
from ..jobs.store import _job_printer_id, _recipe_changed, _recorded_printer_id, decisions_digest, write_job
from ..printers.base import MultiColorUnsupported


READINESS_MAX_AGE_SECONDS = 15


def _native():
    """True when the configured printer has the ZMod native-screen runtime (display_off, offsets, stock API)."""
    return printers.current().has_native_screen


def check_filament_readiness(state, status, selection=None, *, observed_at=None, now=None):
    """Fail closed unless explicit, fresh telemetry agrees with the job's AD5X selection."""
    problems = []
    now = time.time() if now is None else now
    if (not isinstance(now, (int, float)) or isinstance(now, bool) or
            not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool)):
        problems.append("telemetry freshness is unknown")
    elif (now - observed_at < 0 or now - observed_at > READINESS_MAX_AGE_SECONDS):
        problems.append("telemetry is stale or has an invalid timestamp")

    sensor = status.get("filament_switch_sensor head_switch_sensor") if isinstance(status, dict) else None
    if not isinstance(sensor, dict) or telemetry_state(sensor.get("enabled")) != "true":
        problems.append(f"head filament sensor enabled state is {telemetry_state(sensor.get('enabled') if isinstance(sensor, dict) else None)}")
    head = sensor.get("filament_detected") if isinstance(sensor, dict) else None
    print_stats = status.get('print_stats') if isinstance(status, dict) else None
    phase = print_stats.get('state') if isinstance(print_stats, dict) else None
    staged_idle = phase in ('standby', 'complete', 'cancelled')
    if telemetry_state(head) == "unknown" or (head is False and not staged_idle):
        if telemetry_state(head) == "unknown":
            problems.append(f"head filament sensor is unknown (value {head!r})")
        else:
            problems.append("head filament sensor reports no filament during a non-idle print phase (possible runout)")
    # An enabled, empty head is normal before IFS loading. Unknown execution
    # state and active/paused runout never receive the idle staging exemption.
    if isinstance(selection, dict) and "filament_selection" in selection:
        selection = selection.get("filament_selection")
    if isinstance(selection, dict):
        try:
            selection = validate_filament_selection(selection)
        except ValueError as exc:
            problems.append(str(exc))
            return problems
    color = status.get("zmod_color") if isinstance(status, dict) else None
    slots = color.get("slots") if isinstance(color, dict) else None
    if not isinstance(slots, list):
        problems.append("AD5X IFS slot inventory is unavailable")
        return problems
    if not isinstance(selection, dict):
        problems.append("job has no explicit IFS slot/color selection")
        return problems
    try:
        selection = validate_filament_selection(selection)
    except ValueError as exc:
        problems.append(str(exc))
        return problems
    slot_id = selection["slot"]
    def slot_id_of(item):
        raw = item.get("ID") if isinstance(item, dict) else None
        if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= FILAMENT_SLOTS:
            return raw
        if isinstance(raw, str) and raw.isdigit() and 1 <= int(raw) <= FILAMENT_SLOTS:
            return int(raw)
        return None

    malformed = [item for item in slots if slot_id_of(item) is None]
    if malformed:
        problems.append("AD5X IFS inventory contains malformed slot IDs")
    matches = [item for item in slots if slot_id_of(item) == selection["slot"]]
    if len(matches) > 1:
        problems.append(f"AD5X IFS inventory contains duplicate ID {slot_id}")
        return problems
    if not matches:
        problems.append(f"selected IFS slot {slot_id} is unavailable")
        return problems
    actual = matches[0]
    feeder = actual.get("hasFilament")
    feeder_state = telemetry_state(feeder)
    if feeder_state == "false":
        problems.append(
            f"selected IFS slot {slot_id} reports false (empty). Check the printer screen's IFS view for slot {slot_id}; "
            f"if it is empty, feed filament into slot {slot_id}'s inlet until the IFS grips it, then retry. "
            "The AD5X has no buffer switch to click")
    elif feeder_state != "true":
        problems.append(
            f"selected IFS slot {slot_id} presence is unknown (raw value {feeder!r}); "
            "this is a telemetry fault, not proof the slot is empty")
    for key, actual_key in (("material", "Material"), ("color", "Color")):
        wanted = selection.get(key)
        observed = actual.get(actual_key)
        if not isinstance(wanted, str) or not wanted.strip():
            problems.append(f"job selection is missing {key}")
        elif not isinstance(observed, str) or observed.casefold() != wanted.casefold():
            problems.append(f"selected IFS slot {slot_id} {key} does not match the job selection")
    return problems


def validate_native_start(detail, selection):
    """Require native ready and an explicit physical-slot route, not stale feeder state."""
    return printers.current().native.validate_native_start(detail, selection)


def _verify_readiness_adapter(adapter, state, selection=None):
    """Families without IFS: one Moonraker sample judged by the adapter's own readiness check."""
    try:
        objects = urllib.parse.quote("&".join(adapter.moonraker_objects()), safe="&")
        status = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")["result"]["status"]
        problems = adapter.readiness_problems(state, status, selection)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"{adapter.label} readiness unavailable; print refused: {exc}") from exc


def _verify_filament_readiness(state, selection=None, adapter=None):
    """Read current mode, then obtain one freshness-bounded sensor/occupancy sample.

    The adapter is the printer the state belongs to: print_now passes its
    job-resolved adapter, because resolving printers.current() here would
    judge a secondary printer's readiness by the default printer's family
    (its multi_slot shape, its readiness problems). Without one, the
    configured printer, as before.
    """
    adapter = adapter or printers.current()
    if not adapter.multi_slot:
        return _verify_readiness_adapter(adapter, state, selection)
    try:
        selection = resolve_filament_selection(state, selection)
        _validate_slot_selection(state, selection)
    except ValueError as exc:
        raise RuntimeError(f"filament readiness refused: {exc}") from exc
    # Phase 1: the Moonraker active spool is an accounting pointer that follows
    # the job at launch (_sync_active_spool); it is not a physical feed fact.
    try:
        mode_data = _mr(state, "/printer/objects/query?save_variables")["result"]["status"]
        mode = (mode_data.get("save_variables") or {}).get("variables", {}).get("display_off")
        if mode not in (0, 1):
            raise RuntimeError(f"printer mode unknown: display_off={mode!r}")
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{urllib.parse.quote('&'.join(FILAMENT_SENSOR_OBJECTS), safe='&')}")
        status = response["result"]["status"]
        observed_at = time.time()
        if mode == 0:
            detail = read_stock_detail(state)
            native_at = time.time()
            native_phase = detail.get("status")
            moonraker_phase, moonraker_file, _ = _printer_phase(state)
            if native_phase == "printing" and moonraker_phase not in ("printing", "paused"):
                raise RuntimeError("native status='printing' conflicts with Moonraker standby; execution state unresolved")
            if native_phase in ("ready", "idle", "standby") and moonraker_phase in ("printing", "paused"):
                raise RuntimeError(f"Moonraker {moonraker_phase} conflicts with native status={native_phase!r}; execution state unresolved")
            apply_stock_presence(status, read_stock_filament_slots_from_detail(detail))
            observed_at = native_at
        problems = check_filament_readiness(state, status, selection, observed_at=observed_at)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as exc:
        raise RuntimeError(f"filament readiness unavailable; print refused: {exc}") from exc


def start_print_settings(text):
    """Read the START_PRINT call the printer will actually execute."""
    m = re.search(r'^(?:START_PRINT|PRINT_START)\b(.*)$', text, re.M)
    if not m: return {"line": None, "args": {}}
    return {"line": m.group(0).strip(), "args": dict(re.findall(r'(\w+)=(\S+)', m.group(1)))}


def validate_saved_mesh_start(text, mesh):
    """Both modes must consume the reviewed mesh without new offset/calibration."""
    args = start_print_settings(text)['args']
    expected = mesh.get('profile')
    if (not expected or args.get('MESH') != expected or 'Z_OFFSET' in args or
            args.get('FORCE_LEVELING', 'false').lower() not in ('false', '0') or
            args.get('FORCE_KAMP', 'false').lower() not in ('false', '0')):
        raise RuntimeError(f'saved-mesh flow requires START_PRINT MESH={expected}, no explicit Z_OFFSET and no forced leveling'
                           f' (this file has MESH={args.get("MESH")}; it was sliced for a different screen mode'
                           ' or calibration, so prepare it again)')


# The 2026-10-03 "too high" prints logged `Printing with bed mesh: ` (blank). The good one loaded `auto`.
MESH_PROFILE = "auto"


def screen_z_offset(st):
    """zProbeOffset from the stock screen's Adventurer5M.json (what ZMod applies with display_off=0). Read-only.

    The captured schema stores it at leftExtruderOffset.zProbeOffset. A top-level or level.zProbeOffset
    is accepted only when the nested field is absent. Non-finite values fail closed as unreadable.
    """
    if not _native(): return None
    try:
        d=_mr(st,"/server/files/config/Adventurer5M.json",timeout=8)
    except Exception: return None
    def numeric(v):
        return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(float(v))
    nested=((d or {}).get("leftExtruderOffset") or {}).get("zProbeOffset") if isinstance(d,dict) else None
    if "leftExtruderOffset" in (d or {}) and isinstance(d.get("leftExtruderOffset"), dict) and "zProbeOffset" in d["leftExtruderOffset"]:
        return round(float(nested),3) if numeric(nested) else None
    def find(o):
        if isinstance(o,dict):
            v=o.get("zProbeOffset")
            if numeric(v): return v
            for x in o.values():
                r=find(x)
                if r is not None: return r
        return None
    v=find(d)
    return round(float(v),3) if numeric(v) else None


def mesh_readiness(st):
    """Read-only bed mesh check before upload. Returns dict; problem is None when the saved mesh is usable."""
    info: dict = {"profile":None,"saved_at":None,"print_leveling":None,"problem":None,"stale":None}
    if not _native():
        # The printer owns its mesh and offset; there is no saved-mesh contract to enforce.
        info.update(display_off=None, z_offset_mm=None, offset_source=None)
        return info
    try:
        q=_mr(st,"/printer/objects/query?bed_mesh=profiles&save_variables",timeout=8)["result"]["status"]
    except Exception as e:
        info["problem"]=f"could not read the bed mesh from Klipper ({type(e).__name__})"; return info
    profiles=(q.get("bed_mesh") or {}).get("profiles") or {}
    info["print_leveling"]=((q.get("save_variables") or {}).get("variables") or {}).get("print_leveling")
    variables=((q.get("save_variables") or {}).get("variables") or {})
    info["display_off"]=variables.get("display_off")
    saved_z=(variables.get("gcode_offsets") or {}).get("z")
    saved_numeric=not isinstance(saved_z, bool) and isinstance(saved_z, (int, float)) and math.isfinite(saved_z)
    # ZMod stores two offsets. display_off=1 (screenless) loads the numeric
    # save_variables gcode_offsets.z when LOAD_ZOFFSET=1. display_off=0 leaves
    # that value unused and applies the native screen zProbeOffset instead.
    # Neither path has a required magnitude. The two values are never added.
    if info["display_off"]==0:
        native=screen_z_offset(st)
        info["z_offset_mm"]=native
        info["saved_gcode_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="native screen zProbeOffset"
        if not isinstance(native, (int, float)) or isinstance(native, bool):
            info["problem"]="native screen is the offset owner but zProbeOffset is unreadable. Reconcile the screen calibration before starting; do not substitute the screenless saved offset"
            return info
    elif info["display_off"]==1:
        info["z_offset_mm"]=saved_z if saved_numeric else None
        info["offset_source"]="saved gcode offset"
        if variables.get("load_zoffset")!=1 or not saved_numeric:
            info["problem"]="screenless offset is not authoritative; expected LOAD_ZOFFSET=1 and a numeric saved gcode_offsets.z. The native screen offset is a separate store and is not used in this mode"
            return info
    else:
        info["z_offset_mm"]=None
        info["offset_source"]=None
        info["problem"]="display_off is neither 0 nor 1, so the offset owner is unknown. Reconcile before starting"
        return info
    if info["display_off"]==1 and variables.get("mesh_test", 1) not in (0, 1):
        info["problem"]="screenless saved-mesh flow permits MESH_TEST=0 or 1 only; modes 2/4 can rebuild and modes 3/4 auto-tune Z offset. Reconcile before starting"
        return info
    profile = MESH_PROFILE
    if info['display_off'] == 0:
        profile = (st.get('calibration_baseline') or {}).get('native_mesh', MESH_PROFILE)
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_]+', profile):
        info['problem'] = 'invalid calibration baseline mesh'
        return info
    if profile not in profiles:
        info["problem"]=f"no saved '{profile}' bed mesh. Reconcile the selected calibration baseline before using the saved-mesh print flow"
        return info
    if info["print_leveling"] in (1,True,"1"):
        info["problem"]="PRINT_LEVELING=1 selects per-print rebuilding in screenless ZMod. This saved-mesh workflow requires PRINT_LEVELING=0; reconcile the chosen strategy before starting"
        return info
    info["profile"]=profile
    try:
        files=_mr(st,"/server/files/list?root=config",timeout=8)["result"]
        m=[f.get("modified") for f in files if f.get("path")=="printer.cfg"]
        if m and isinstance(m[0],(int,float)): info["saved_at"]=m[0]
    except Exception: pass
    try:
        inst=(st.get("nozzle") or {}).get("installed")
        if inst and info["saved_at"]:
            from datetime import datetime
            info["stale"]=datetime.fromisoformat(inst).timestamp() > info["saved_at"]
    except (ValueError, TypeError, AttributeError): pass
    return info


def require_current_artifact(job, gcode):
    """Offline fail-closed guard before telemetry, approval consumption or upload."""
    if not isinstance(job, dict):
        return
    from ..core import slice_warnings
    finding = slice_warnings.support_finding(job)
    if finding is not None:
        # The slicer said supports are needed, the slice ran without them and nobody decided.
        # A question, not a block: answering it (re-slice with supports, or print without)
        # records the decision on the job and clears this.
        raise RuntimeError('supports decision needed: ' + slice_warnings.question(finding).splitlines()[0]
                           + ' Choose re-slice with supports or print without supports; nothing uploaded or started.')
    unsupported = slice_warnings.support_mismatch(job)
    if unsupported:
        # The operator chose supports but an artifact was sliced without them: never print it.
        raise RuntimeError('supports were chosen but ' + ', '.join(unsupported)
                           + ' was sliced without them; re-slice before printing. Nothing uploaded or started.')
    from ..core import support_clearance
    clearance = job.get('support_clearance')
    if clearance is None and job.get('downloaded_file'):
        clearance = support_clearance.safe_analyze(job.get('downloaded_file'))
    if support_clearance.risky(clearance):
        ack = job.get('clearance_acknowledgement') or {}
        src_path = job.get('downloaded_file')
        src_sha = file_sha256(src_path) if src_path and Path(src_path).is_file() else None
        binding = job.get('artifact_binding') or {}
        rec_sha = binding.get('recipe_sha256')
        gc_path = gcode or job.get('gcode')
        gc_sha = file_sha256(gc_path) if gc_path and Path(gc_path).is_file() else None
        # A "with supports" choice does not satisfy a low-clearance
        # finding (#922 gap 2): it is recorded with via='with_supports'
        # and deliberately fails this guard, because supports cannot fit
        # into a sub-1.0 mm gap. An explicit print-anyway choice, a
        # verified geometry repair, or the operator's explicit
        # without-supports (as-authored) decision does satisfy it.
        # Acknowledgements recorded before `via` existed keep their
        # original print-anyway meaning.
        if not (ack.get('acknowledged')
                and ack.get('via', 'print_anyway') in ('print_anyway', 'geometry_repair', 'without_supports')
                and (src_sha is None or ack.get('source_sha256') == src_sha)
                and (rec_sha is None or ack.get('recipe_sha256') == rec_sha)
                and (gc_sha is None or ack.get('gcode_sha256') == gc_sha)):
            desc = '; '.join(support_clearance.describe(clearance)[:2]) or 'insufficient support clearance'
            raise RuntimeError(f'insufficient support clearance: {desc}; choose a geometry repair or confirm print anyway. Nothing uploaded or started.')
    binding = job.get('artifact_binding')
    if binding is None and not job.get('color_decisions'):
        return  # Preserve legacy jobs without source-color decisions.
    if not binding or job.get('gcode_verified') is not True:
        raise RuntimeError('stale color artifact: reslice current JOB before launch')
    from ..core.slice_recipe import RECIPE_VERSION, require_recorded
    recipe_sha = require_recorded(job, binding)
    # The stored record hashing to its own digest only proves it was not edited;
    # it says nothing about the world since slicing. Recompute the recipe from
    # the launch-time job, hardware record, profiles and settings (with the
    # recorded overrides, exactly as _recipe_changed does during slicing) and
    # refuse when the inputs moved after the artifact was sliced (P2-52).
    try:
        recipe_changed = _recipe_changed(job, job, _recorded_printer_id(job))
    except Exception as exc:
        raise RuntimeError('stale color artifact: the current slice recipe could not be '
                           f'recomputed before launch ({exc}); reslice before launch') from exc
    if recipe_changed:
        raise RuntimeError('stale color artifact: the nozzle, profiles or job inputs changed '
                           'after slicing; reslice before launch')
    stored_hash = binding.get('gcode_sha256')
    presented = Path(gcode).resolve()
    if isinstance(stored_hash, dict):
        plates = job.get('derived_plates') or []
        normalized_hashes = {str(slot): digest for slot, digest in stored_hash.items()}
        normalized_slots = set()
        for row in plates:
            slot = row.get('slot')
            def _valid_slot(s):
                return not isinstance(s, bool) and (isinstance(s, int) or isinstance(s, str) and s.isdigit())
            # Painted multicolor plates carry every IFS slot they use.
            painted_ok = (row.get('binding_key') and isinstance(slot, list) and slot
                          and all(_valid_slot(s) for s in slot))
            if not painted_ok and not _valid_slot(slot):
                raise RuntimeError('stale color artifact: malformed physical IFS slot binding')
            slot_key = str(row.get('binding_key') or slot)
            if slot_key in normalized_slots:
                raise RuntimeError('stale color artifact: duplicate physical IFS slot binding')
            normalized_slots.add(slot_key)
        if normalized_slots != set(normalized_hashes):
            raise RuntimeError('stale color artifact: derived plates do not match the binding')
        allowed = set()
        for row in plates:
            plate = Path(row['gcode']).resolve()
            allowed.add(plate)
            if file_sha256(plate) != normalized_hashes[str(row.get('binding_key') or row.get('slot'))]:
                raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        if presented not in allowed or Path(job.get('gcode', '')).resolve() not in allowed:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
        expected_hash = stored_hash
    else:
        expected_hash = file_sha256(gcode)
        if Path(job.get('gcode', '')).resolve() != presented:
            raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')
    expected = {'source_sha256': file_sha256(job['downloaded_file']),
                'decisions_sha256': decisions_digest(job.get('color_decisions')),
                'gcode_sha256': expected_hash, 'recipe_version': RECIPE_VERSION, 'recipe_sha256': recipe_sha}
    if binding != expected:
        raise RuntimeError('stale color artifact: source, decisions or G-code changed; reslice before launch')


def _printer_phase(st, native=None):
    """Return a single adapter's reported phase; native 'printing' needs corroboration.

    native defaults to the configured printer's; a caller asking about a routed
    printer passes that printer's own has_native_screen (2.2.5 review).
    """
    if native is None:
        native=_native()
    q=_mr(st,"/printer/objects/query?"+("save_variables&" if native else "")+"print_stats&idle_timeout&virtual_sdcard&webhooks")["result"]["status"]
    variables=(q.get("save_variables") or {}).get("variables") or {}
    mode=variables.get("display_off")
    if native and mode not in (0,1):
        raise RuntimeError(f"live display_off mode is {mode!r}; refusing ambiguous hardware behavior")
    st["display_off"]=mode
    st["force_md5"]=variables.get("force_md5")
    stats=q.get("print_stats") or {}
    state=stats.get("state")
    filename=stats.get("filename")
    idle=(q.get("idle_timeout") or {}).get("state")
    virtual_sd = q.get("virtual_sdcard") or {}
    webhooks = q.get("webhooks") or {}
    # print_stats can remain standby/empty while PRINT_START is heating or
    # otherwise preparing. These independent Klipper signals still prove the
    # submitted job has been accepted; do not infer failure or retry it.
    accepted = (virtual_sd.get("is_active") is True or bool(virtual_sd.get("file_path"))
                or webhooks.get("state") == "Printing" or idle == "Printing")
    if state not in ("standby", "printing", "paused", "complete", "error", "cancelled"):
        raise RuntimeError(f"Moonraker print state unknown: {state!r}")
    if state in ("printing", "paused"):
        return state, filename, "Moonraker reports active execution"
    return state, filename, "Printing" if accepted else idle


# The operator's bed-clear confirmation for one print-now run (#1147), set by print_now from its
# ``bed_clear`` argument: True is an explicit confirmation for whatever this run prints (the CLI's
# --bed-clear-confirmed), a SHA-256 binds it to exactly that G-code (print_next passes the SHA of the
# artifact the operator confirmed). Unset, the start primitive refuses: the approval no longer lives
# only in the callers' button flows.
_BED_CLEAR = contextvars.ContextVar("print_now_bed_clear", default=None)
BED_CLEAR_REFUSAL = ("REFUSED: no bed-clear approval was given for this G-code, so nothing was uploaded or "
                     "started. Confirm the bed is clear (the chat's Start button, or print-now "
                     "--bed-clear-confirmed at the command line), then start again.")


def _require_bed_clear(gcode):
    """Refuse unless the operator's bed-clear confirmation covers this exact G-code (#1147).

    Runs at every transport's approval step, before anything is uploaded, and
    yolo does not waive it: saying yolo skips the checks, never the operator's
    word that the bed is clear.
    """
    grant = _BED_CLEAR.get()
    if grant is True:
        return
    if isinstance(grant, str) and grant.strip().lower() == file_sha256(gcode):
        return
    raise SystemExit(BED_CLEAR_REFUSAL)


def _claim_bed_clear(job_path, job, gcode, reprint=False, *, consume=True):
    """The approval step of every transport: the operator's confirmation, then the single-use record."""
    _require_bed_clear(gcode)
    if job_path is not None and consume:
        _consume_approval(job_path, job, gcode, reprint=reprint)


def _consume_approval(job_path, job, gcode, reprint=False):
    """Single-use bed-clear approval per G-code. Refuses reuse unless an explicit new reprint approval.
    Check and record run in one launch-state transaction on the current JOB.json, never the caller's
    possibly stale snapshot; the database copy survives another writer saving an older JOB.json."""
    sha=file_sha256(gcode); key=str(Path(job_path).resolve())
    with launch_state.transaction(_job_printer_id(job)) as con:
        current=json.loads(Path(job_path).read_text())
        require_current_artifact(current, gcode)
        now=time.strftime("%Y-%m-%dT%H:%M:%S%z")
        used=[a for a in current.get("approvals",[]) if a.get("gcode_sha256")==sha and a.get("consumed_at")]
        used+=launch_state.consumed(con, key, sha)
        if used and not reprint:
            raise SystemExit(f"REFUSED: the bed-clear approval for this G-code was already used at {used[-1]['consumed_at']}. "
                             "Ask the operator for a new bed-clear confirmation, then rerun with --reprint. Nothing uploaded or started.")
        kind="reprint" if used else "bed_clear"
        launch_state.record_consumed(con, key, sha, kind, now)
        current.setdefault("approvals",[]).append({"gcode_sha256":sha,"granted_at":now,"consumed_at":now,"kind":kind})
        write_job(Path(job_path).parent, current)
    job.clear(); job.update(current)


def _require_idle_before_mapped_launch(require_idle_settled, read_moonraker, read_native):
    """Report only an explicit idle-guard refusal as a clean pre-upload refusal."""
    try:
        require_idle_settled(read_moonraker, read_native)
    except RuntimeError as exc:
        message = str(exc)
        if not message.startswith("REFUSED: idle guard failed:"):
            raise
        raise SystemExit(message + ". Nothing uploaded or started.") from None


CHECKPOINT_NOT_SENT = ("rejected", "not_submitted")  # launch attempts that provably never reached the printer


def _require_launch_checkpoint_clear(checkpoint, reprint, remote=None):
    """`<plate>.launch.json` records one launch attempt (print-again reads it too), so it is never deleted.

    An earlier attempt's file must not stop a deliberate reprint (multi-copy plates, "Print again"): the
    operator's new bed-clear approval is exactly that decision, and the printer lease already refused if
    the earlier attempt is still unsettled. It does stop an unapproved replay of the same artifact, and
    an attempt that never reached the printer never blocks anything.

    A checkpoint whose recorded `file` is a DIFFERENT delivery of this plate than the current `remote`
    (a stale plain-.gcode attempt, this resubmission packaged as .gcode.3mf, or the reverse) does not
    block either (#838): it describes another artifact, and the printer lease — not this record — is
    what refuses while an earlier attempt is still unsettled. A checkpoint with no recorded file keeps
    the old fail-closed reading.
    """
    if not checkpoint.exists():
        return
    try:
        data = json.loads(checkpoint.read_text())
    except (OSError, ValueError):
        data = {}
    phase = data.get('phase') if isinstance(data, dict) else None
    if phase in CHECKPOINT_NOT_SENT or reprint:
        return
    recorded_file = data.get('file') if isinstance(data, dict) else None
    if remote is not None and recorded_file and recorded_file != remote:
        return
    raise RuntimeError('mapped submission checkpoint exists; reconcile or pass an explicit reprint approval '
                       'instead of replaying')


IDLE_PRINT_STATES = ("standby", "complete", "cancelled")  # print_next.live_idle_check's idle set


def _require_idle(st):
    try:
        state,fn,busy=_printer_phase(st)
    except OSError:
        # Moonraker unreachable: every transport failure urllib surfaces on this read
        # (refused, timeout, reset) is an OSError — URLError subclasses it. Name the
        # refusal like the busy guard below instead of leaking a raw traceback (#1049).
        # State/parse errors are RuntimeError/ValueError and keep propagating.
        raise SystemExit("REFUSED: printer is offline/unreachable. Nothing uploaded or started.")
    if state == "printing" or busy == "Printing":
        raise SystemExit(f"REFUSED: printer is busy ({state}, {fn}). Nothing uploaded or started.")
    # The idle set is print_next.live_idle_check's, exactly: a paused job (or an errored one the
    # operator has not cleared) still owns the printer, whichever caller asks for a start.
    if state not in IDLE_PRINT_STATES:
        raise SystemExit(f"REFUSED: printer is not idle ({state}, {fn}). Finish or cancel that job "
                         "on the printer first. Nothing uploaded or started.")
    return state,fn,busy


def refused_plates_digest(job):
    """Digest of the plates a slice refused, which partial_job_accepted must name."""
    refused = (job or {}).get('refused_plates') or []
    return hashlib.sha256(json.dumps(refused, sort_keys=True, default=str).encode()).hexdigest()


def partial_job_refusal(job):
    """Why launching this job would silently print an incomplete model, or None.

    A per-plate slice records every plate a guard refused in refused_plates
    and returns only the survivors. Those survivors are not the model the
    operator asked for, so launch stays blocked until the operator
    explicitly accepts the partial job (the chat intake discloses the
    refused plates and records partial_job_accepted when the operator
    confirms the start) (P1-20). The acceptance records the digest of the
    plates it was shown, so a reslice that refuses a different set is not
    covered by an earlier acceptance (2.2.5 review).
    """
    refused = (job or {}).get('refused_plates') or []
    if not refused or (job or {}).get('partial_job_accepted') == refused_plates_digest(job):
        return None
    named = '; '.join(f"plate {row.get('plate_id')} (slot {row.get('slot')}): {row.get('reason')}"
                      for row in refused)
    return (f"this job is incomplete: {len(refused)} plate(s) were refused during slicing and "
            f"will not print: {named}. Reslice after fixing the refusal, or accept the partial "
            "job explicitly (the chat intake records acceptance when you confirm the start "
            "after it shows you this list)")


# Yolo (print now, skip the checks): the operator vouches for the file. Every
# waivable gate still runs, but a failure prints one `YOLO WAIVED: <gate>:
# <reason>` line instead of refusing, so the chat can say exactly what was
# overridden. Mechanics are never waived: the control lock, a printer that is
# already printing, the launch lease and duplicate-start claim, the upload
# SHA256 check and the start readback. Neither are the mechanical limits
# (S6): require_mechanical_limits refuses a yolo toolpath past the axis
# limits (or one whose bounds cannot be verified) and a heater target above
# the printer's absolute maximum.
YOLO_WAIVED_MARK = "YOLO WAIVED:"
_YOLO = contextvars.ContextVar("print_now_yolo", default=False)


def yolo_active():
    """True inside a print_now(yolo=True) run."""
    return _YOLO.get()


def _waive(gate, reason):
    """Yolo: report a gate that would have refused. Otherwise the caller refuses."""
    if not _YOLO.get():
        return False
    reason = " ".join(str(reason).split())
    reason = re.sub(r"^REFUSED:\s*", "", reason)
    reason = re.sub(r"\s*Nothing (?:was )?uploaded(?: or started)?\.?$", "", reason).rstrip(" .;")
    print(f"{YOLO_WAIVED_MARK} {gate}: {reason}", flush=True)
    return True


def _refuse(gate, message):
    """Raise SystemExit(message) unless a yolo run waives this gate."""
    if not _waive(gate, message):
        raise SystemExit(message)


def _gate(gate, fn, *args, **kwargs):
    """Run a check; in a yolo run its refusal is reported instead of raised."""
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, ValueError, SystemExit, MultiColorUnsupported) as exc:
        if isinstance(exc, SystemExit) and not isinstance(exc.code, str):
            raise
        if not _waive(gate, exc):
            raise
        return None


# Heater targets a G-code file sets, for the mechanical-limit check (S6):
# M104/M109 (nozzle) and M140/M190 (bed) S/R values, Klipper's
# SET_HEATER_TEMPERATURE, and the start macros' temperature arguments.
_HEATER_M_RE = re.compile(r'(?im)^[^\S\n]*(?:N[0-9]+[^\S\n]*)?M[^\S\n]*0*(104|109|140|190)(?![0-9])([^;\n]*)')
_HEATER_VALUE_RE = re.compile(r'[SR]\s*(' + NUMBER + r')', re.I)
_HEATER_SET_RE = re.compile(r'(?im)^[^\S\n]*(?:N[0-9]+[^\S\n]*)?SET_HEATER_TEMPERATURE\b([^;\n]*)')
_HEATER_MACRO_RE = re.compile(r'(?im)^[^\S\n]*(?:N[0-9]+[^\S\n]*)?(?:START_PRINT|PRINT_START)\b([^;\n]*)')
_HEATER_ARG_RE = re.compile(r'([A-Z_]+)\s*=\s*(' + NUMBER + r')', re.I)


def _heater_limits(state, adapter):
    """(nozzle °C, bed °C): the printer's heater ceilings, from the one ceiling function (#1147).

    heater_ceiling.ceiling is what every temperature path asks: the fitted
    nozzle type's safe maximum lowered to the model's max_hotend_c, and the
    model's max_bed_c, else the hottest bed any configured material asks for.
    """
    from ..core import heater_ceiling
    return (heater_ceiling.ceiling(adapter, "nozzle", state=state),
            heater_ceiling.ceiling(adapter, "bed", state=state))


def _gcode_heater_targets(text):
    """(nozzle targets, bed targets) the G-code text sets, in °C."""
    nozzle, bed = [], []
    for match in _HEATER_M_RE.finditer(text):
        into = nozzle if match.group(1) in ("104", "109") else bed
        into.extend(float(v) for v in _HEATER_VALUE_RE.findall(match.group(2)))
    for match in _HEATER_SET_RE.finditer(text):
        args = match.group(1)
        heater = re.search(r'HEATER\s*=\s*"?([A-Za-z0-9_ ]+)', args, re.I)
        target = re.search(r'TARGET\s*=\s*(' + NUMBER + r')', args, re.I)
        if heater and target:
            name = heater.group(1).strip().lower()
            if name.startswith("extruder"):
                nozzle.append(float(target.group(1)))
            elif name == "heater_bed":
                bed.append(float(target.group(1)))
    for match in _HEATER_MACRO_RE.finditer(text):
        for key, value in _HEATER_ARG_RE.findall(match.group(1)):
            key = key.upper()
            if key in _MACRO_HOTEND_ARGS:
                nozzle.append(float(value))
            elif key in _MACRO_BED_ARGS:
                bed.append(float(value))
    return nozzle, bed


def require_mechanical_limits(gcode_path, state, adapter):
    """Refuse a toolpath past the axis limits or a heater past its maximum (S6).

    These are the printer's mechanical limits, so yolo never waives them:
    print_now runs this after the waivable G-code checks. A toolpath whose
    bounds the verifier cannot establish (a refusal of the bounds parser)
    refuses too, because nothing then shows it stays inside the limits.
    Raises RuntimeError naming every problem; returns None when all hold.
    """
    state = state or {}
    raw = Path(gcode_path).read_bytes()
    text = raw.decode(errors="ignore")
    problems = []
    try:
        bounds = cached_motion_bounds(raw, text)
    except RuntimeError as exc:
        problems.append(f"toolpath bounds unavailable: {exc}")
    else:
        limits = state.get("axis_limits_mm", {"x": [0, 223], "y": [0, 223],
                                               "z": [0, state.get("max_print_height_mm", 250)]})
        for axis in "xyz":
            low, high = bounds[axis]
            limit_low, limit_high = map(float, limits[axis])
            if low < limit_low - 1e-3 or high > limit_high + 1e-3:
                problems.append(f"{axis.upper()} bounds {low:.3f}..{high:.3f} exceed "
                                f"{limit_low:.3f}..{limit_high:.3f}")
    hotend_max, bed_max = _heater_limits(state, adapter)
    nozzle_targets, bed_targets = _gcode_heater_targets(text)
    for label, targets, limit in (("nozzle", nozzle_targets, hotend_max), ("bed", bed_targets, bed_max)):
        over = [t for t in targets if not t <= limit]
        if over:
            problems.append(f"{label} target {max(over):g} C exceeds the printer's {limit:g} C maximum")
    if problems:
        raise RuntimeError("mechanical limits are never waived: " + "; ".join(problems))
