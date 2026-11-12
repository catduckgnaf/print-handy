"""The job command line: ``python3 -m print_handy.core.model_job`` (fetch, slice, print-now,
reconcile-launch and the rest). The old module still runs it.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import argparse, json, os, shutil, sys, time
from pathlib import Path

from .. import printers
from ..core import launch_state, printer_state
from ..core.filament_warnings import FilamentWarningPending
from ..core.threemf_canonical import intake_working_copy
from ..gcode.verify import _print_check_warnings, verify_gcode
from .audit import audit, mesh_audit
from .common import ARCHIVE, _mr
from .filament import read_filament_telemetry, resolve_filament_selection
from .prompts import (
    intake_color_choices, intake_unit_confirmation, record_filament_warning_acknowledgement, show,
)
from .slice import plate_manifest, prepare_first_layer, slice_job
from .sources import download_for_site, inventory_for_url
from .store import (
    _printer_key, intake_folder, save_filament_selection, set_color_decisions, stamp_job_printer,
    write_job,
)
from ..launch.print_now import ToolRemapRefused, print_now, record_start, remap_slot
from ..printers.base import MultiColorUnsupported


# Typical durations only, from common Klipper guidance; never a measurement of this printer.
ROUTINE_TYPICAL_MIN = {
    "home": (1, 2), "bed_mesh": (3, 6), "leveling": (5, 10), "z_offset": (3, 5),
    "pid_hotend": (5, 8), "pid_bed": (8, 15), "input_shaper": (5, 10),
}
ROUTINE_NAMES = {"home": "homing", "bed_mesh": "bed mesh probe", "leveling": "full bed leveling",
                 "z_offset": "Z offset calibration", "pid_hotend": "hotend PID tuning",
                 "pid_bed": "bed PID tuning", "input_shaper": "input shaper calibration"}


def routine_receipt(routine):
    """Deterministic immediate receipt; the estimate comes only from the static typical table."""
    if routine not in ROUTINE_TYPICAL_MIN: raise ValueError(f"unknown routine {routine!r}; known: {', '.join(ROUTINE_TYPICAL_MIN)}")
    lo,hi=ROUTINE_TYPICAL_MIN[routine]
    return f"Running {ROUTINE_NAMES[routine]}, about {lo} to {hi} min (typical, not measured on this printer)."


def routine_phase(st, accepted=True):
    """accepted -> running -> completed, read from Klipper idle_timeout. Never inferred from time."""
    try: busy=_mr(st,"/printer/objects/query?idle_timeout")["result"]["status"]["idle_timeout"].get("state")
    except Exception: return "unknown"
    if busy=="Printing": return "running"
    if busy in ("Ready","Idle"): return "completed" if accepted else "idle"
    return "unknown"


def _print_now_cli(target, **kw):
    """print-now with an honest outcome line. A failure before any lease reached submitting()
    never asked the printer to start, so it ends as a REFUSED line (the chat then says it did
    not start and the one-start claim is released); after submitting() it stays uncertain."""
    import traceback
    launch_state.reset_submitted()
    try:
        return print_now(target, **kw)
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()  # the detail stays in the log and receipt
        reason = ' '.join(str(exc).split()) or type(exc).__name__
        if launch_state.submitted():
            raise SystemExit(f"UNCERTAIN: the start was sent and then failed: {reason}") from exc
        raise SystemExit(f"REFUSED: {reason}. Nothing started.") from exc


def _print_filament_warning_packet(job_json, pending, resume):
    """Print the pending filament warnings for the operator (CLI surface).

    The CLI cannot ask interactively mid-run, so a pending warning round is
    reported as data and the job stops there; accepting is the explicit
    resume command, which records the bound acceptance first.
    """
    print(json.dumps({'status': 'FILAMENT_WARNINGS_PENDING', 'job_json': str(job_json),
                      'warnings': pending.warnings, 'resume': resume}, indent=1))
    print('Filament warnings need your acceptance before slicing. Review them, then re-run '
          'with --accept-filament-warnings to accept exactly these warnings and continue.')


def main():
    try:
        _main()
    except MultiColorUnsupported as exc:
        raise SystemExit("REFUSED: " + " ".join(str(exc).split()) + ". Nothing sliced, uploaded or started.")


def _main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("url")
    f.add_argument("--intake-id", help="fresh Discord request identity; isolates history")
    f.add_argument("--inspect-only", action="store_true", help="download selected geometry and audit without slicing approval or slicing")
    f.add_argument("--select", type=int)
    f.add_argument("--approved", action="store_true", help="the operator approved exactly the selected source and any supports")
    f.add_argument("--accept-filament-warnings", action="store_true", help="record acceptance of the job's pending filament warnings (bound to those warnings, the source and the selected plates) and continue")
    f.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    f.add_argument("--printer", choices=printers.MODELS, help="printer model this job is prepared for (default: [printer] model)")
    f.add_argument("--filament-slot", type=int, help="AD5X IFS feeder slot (1-4)")
    f.add_argument("--filament-color", type=str, help="filament color matching IFS slot")
    f.add_argument("--filament-material", type=str, help="filament material matching IFS slot")
    f.add_argument("--filament-spoolman-id", "--spoolman-id", dest="filament_spoolman_id", type=int, default=None, help="Spoolman spool ID")
    s = sub.add_parser("slice")
    s.add_argument("job_json")
    s.add_argument("--approved", action="store_true")
    s.add_argument("--accept-filament-warnings", action="store_true", help="record acceptance of the job's pending filament warnings (bound to those warnings, the source and the selected plates) and continue")
    s.add_argument("--printer", choices=printers.MODELS, help="printer model to slice this job for (recorded on the job)")
    s.add_argument("--brim", type=int, default=None, help="override brim width mm (e.g. 0 to disable)")
    s.add_argument("--separate", action="store_true", help="multi-plate source: slice one G-code per plate instead of merging all parts onto one bed")
    filament_select=sub.add_parser("filament-select", help="record a user-confirmed slot/color choice in a model JOB.json")
    filament_select.add_argument("job_json")
    filament_select.add_argument("--slot", type=int, required=True)
    filament_select.add_argument("--color", required=True)
    filament_select.add_argument("--material", required=True)
    filament_select.add_argument("--spoolman-id", type=int, required=True)
    unit_select = sub.add_parser("unit-select", help="record a user-confirmed inch vs mm choice for a small STL in a model JOB.json; no slicing or start")
    unit_select.add_argument("job_json")
    unit_select.add_argument("--units", choices=["mm", "inch"], required=True)
    cp = sub.add_parser('color-plan', help='offline per-part and source-filament selection questions')
    cp.add_argument('job_json')
    cs = sub.add_parser('color-select', help='save source-bound color decisions; no slicing or start')
    cs.add_argument('job_json')
    cs.add_argument('decisions_json')
    filament_status = sub.add_parser("filament-status", help="read-only labeled AD5X filament telemetry")
    final=sub.add_parser("finalize-gcode", help="finalize reviewed multicolor G-code metadata and native checksum; no upload")
    final.add_argument("gcode")
    final.add_argument("--tool-map", required=True, help="JSON tool-to-physical-slot mapping")
    v=sub.add_parser("verify-gcode"); v.add_argument("gcode")
    remap=sub.add_parser("remap-slot", help="remap single-color job or G-code tool to another physical IFS slot")
    remap.add_argument("target", help="JOB.json or plate.gcode")
    remap.add_argument("--slot", type=int, required=True, help="physical IFS slot (1-4)")
    remap.add_argument("--tool", type=str, default=None, help="logical tool index the file uses (default: the file's only used tool)")
    pn=sub.add_parser("print-now",help="bed confirmed clear: verify, upload, start, confirm, record (G-code or JOB.json)"); pn.add_argument("target")
    pn.add_argument("--accept-spool-change",action="store_true",help="operator confirmed printing with the spool now in a slot that changed since slicing (same material family only)")
    pn.add_argument("--yolo",action="store_true",help="the operator vouches for the file: report failed checks instead of refusing, no single-use approval record (the bed-clear confirmation, mechanics, axis limits and heater maximums are never waived)")
    pn.add_argument("--bed-clear-sha256",default=None,help="the operator confirmed the bed is clear for the G-code with this SHA-256 (what the chat's Start button passes)")
    pn.add_argument("--remap-slot", "--slot", dest="remap_slot", type=int, default=None, help="remap the file's one used tool to another physical IFS slot (1-4) without physical spool changes; a file using several tools is refused (use remap-slot --tool)")
    for x in (pn,f,s):
        x.add_argument("--accept-default-z-offset",action="store_true",help="the operator accepted the printer default Z offset for this start")
        x.add_argument("--reprint",action="store_true",help="the operator gave a NEW bed-clear approval to reprint the same G-code")
        x.add_argument("--bed-clear-confirmed",action="store_true",help="the operator confirms the bed is clear for this start (required to start; #1147)")
    rl=sub.add_parser("reconcile-launch",help="show, or after checking the printer settle, an uncertain start; never resubmits")
    outcome=rl.add_mutually_exclusive_group()
    outcome.add_argument("--started",action="store_true",help="the operator saw that start reach the printer")
    outcome.add_argument("--not-started",action="store_true",help="the operator saw that the printer never started it")
    rt=sub.add_parser("routine-receipt",help="immediate typical-duration receipt for a calibration routine (no printer contact)"); rt.add_argument("routine",choices=sorted(ROUTINE_TYPICAL_MIN))
    sub.add_parser("routine-status",help="read-only accepted/running/completed phase from Klipper idle_timeout")
    for x in (f,s):
        x.add_argument("--print-now",action="store_true",help="bed confirmed clear: start the print right after slicing")
        x.add_argument('--units', choices=['mm', 'inch'], default=None, help='user-confirmed units for a small STL (largest dimension under 10 mm); required before slicing one')
        x.add_argument('--color-mode', choices=['per_part', 'single_color'])
        x.add_argument('--color-slot', type=int, action='append', help='slot for each displayed part/filament question, in order')
    ma=sub.add_parser("audit",help="read-only mesh topology audit; no repair or slicing")
    ma.add_argument("source")
    lay = sub.add_parser("layout", help="offline 100%-scale geometry inspection, arrangement, and export; no slicing or start")
    lay.add_argument("source")
    lay.add_argument("-o", "--outdir", default=None, help="directory for arranged 3MF/STL/PNG/JSON outputs")
    lay.add_argument("--inspect-only", action="store_true", help="inspect geometry and nominal bed fit without writing arranged files")
    lay.add_argument("--bed", nargs=3, type=float, default=[220.0, 220.0, 220.0], metavar=("X", "Y", "Z"))
    lay.add_argument("--margin", type=float, default=5.0)
    lay.add_argument("--gap", type=float, default=5.0)
    lay.add_argument("--input-unit", default=None, help="explicit length unit for STL or unitless model (mm, cm, m, inch, ft)")
    lay.add_argument("--split-stl-components", action="store_true", help="treat disconnected STL shells as separate parts")
    lay.add_argument("--allow-rotations", nargs="*", type=float, default=None, help="candidate Z rotations in degrees")
    cad = sub.add_parser("cad-split", help="offline opt-in reviewed CAD split plan and printable dowel preparation; no slicing or start")
    cad.add_argument("source", help="input .stl path")
    cad.add_argument("-o", "--outdir", default=None, help="output directory for split artifacts and split_validation.json")
    cad.add_argument("--plan", required=True, help="reviewed split plan preset (rack_adapter_10to19, macmini_mount_a1347) or JSON path")
    cad.add_argument("--execute", action="store_true", help="invoke OpenSCAD CLI to render boolean split halves and verify volume conservation")
    m=sub.add_parser("plate-manifest",help="read-only source plate inventory; does not authorize slicing")
    m.add_argument("source_3mf")
    r=sub.add_parser("record-start",help="after a confirmed print start: write the JSON print record"); r.add_argument("gcode"); r.add_argument("--moonraker-name"); r.add_argument("--started-at",help="ISO UTC, default now")
    prep = sub.add_parser("prepare-first-layer", help="offline: generate and slice a one-layer test; never upload or start")
    prep.add_argument("--slot", type=int, required=True)
    prep.add_argument("--color", required=True)
    prep.add_argument("--material", required=True)
    prep.add_argument("--spoolman-id", type=int, required=True)
    prep.add_argument("--slicer", choices=["orca"], default="orca", help="canonical Orca engine; no legacy fallback")
    a=ap.parse_args();
    if a.cmd == 'unit-select':
        intake_unit_confirmation(a.job_json, units=a.units)
        saved = json.loads(Path(a.job_json).read_text())
        print('UNIT PROVENANCE RECORDED ' + json.dumps(saved.get('unit_decision') or saved.get('unit_provenance')))
        return
    if a.cmd in ('color-plan', 'color-select'):
        from ..core.source_colors import inventory, prompts, validate_decisions
        path = Path(a.job_json)
        job = json.loads(path.read_text())
        manifest = inventory(job['downloaded_file'])
        job['color_manifest'], job['color_prompts'] = manifest, prompts(manifest)
        if a.cmd == 'color-select':
            set_color_decisions(job, validate_decisions(manifest, json.loads(Path(a.decisions_json).read_text()),
                                                        printer_state.load(), resolve_filament_selection))
        write_job(path.parent, job)
        print(json.dumps({'manifest': manifest, 'questions': prompts(manifest),
                          'decisions': job.get('color_decisions')}, indent=2))
        return
    if a.cmd=="audit":
        print(json.dumps(mesh_audit(a.source), indent=2)); return
    if a.cmd=="layout":
        from ..core.geometry_layout import arrange_and_export, inspect_geometry
        src_path = Path(a.source)
        if a.inspect_only:
            res = inspect_geometry(
                src_path,
                bed=tuple(a.bed),
                margin=a.margin,
                input_unit=a.input_unit,
                split_stl=a.split_stl_components,
            )
            if a.outdir:
                out_dir = Path(a.outdir)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "inspection.json").write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(res, indent=2))
            return
        out_dir = Path(a.outdir) if a.outdir else (src_path.parent / f"{src_path.stem}_layout_100pct")
        res = arrange_and_export(
            src_path,
            out_dir,
            bed=tuple(a.bed),
            margin=a.margin,
            gap=a.gap,
            angles=a.allow_rotations,
            input_unit=a.input_unit,
            split_stl=a.split_stl_components,
        )
        print(json.dumps(res, indent=2))
        return
    if a.cmd=="cad-split":
        from ..core.cad_split import prepare_cad_split
        src_path = Path(a.source)
        out_dir = Path(a.outdir) if a.outdir else (src_path.parent / f"{src_path.stem}_cad_split")
        res = prepare_cad_split(src_path, out_dir, a.plan, execute_openscad=bool(a.execute))
        print(json.dumps(res, indent=2))
        return
    if a.cmd=="prepare-first-layer":
        prepare_first_layer(a.slot, a.color, a.material, a.spoolman_id, slicer=a.slicer); return
    if a.cmd=="plate-manifest":
        print(json.dumps(plate_manifest(a.source_3mf),indent=2)); return
    if a.cmd=="record-start":
        record_start(a.gcode, a.moonraker_name, a.started_at); return
    if a.cmd=="filament-select":
        selection = resolve_filament_selection(printer_state.load(), vars(a))
        selection = save_filament_selection(a.job_json, selection)
        print("FILAMENT SELECTION RECORDED " + json.dumps(selection))
        return
    if a.cmd=="finalize-gcode":
        finalize = printers.current().native.finalize
        st = printer_state.load()
        ok, probs, facts = printer_state.check(st)
        _print_check_warnings(facts)
        if not ok:
            raise RuntimeError('the printer check did not pass, so the G-code was not finalized: ' + '; '.join(probs))
        path = Path(a.gcode)
        backup = path.with_suffix(path.suffix + '.before-finalize')
        if not backup.exists():
            shutil.copy2(path, backup)
        sha = finalize(path, None, st, json.loads(Path(a.tool_map).read_text()))
        verify_gcode(path)
        print('FINALIZED ' + json.dumps({'path': str(path), 'sha256': sha, 'backup': str(backup)}))
        return
    if a.cmd=="verify-gcode":
        verify_gcode(a.gcode); return
    if a.cmd=="filament-status":
        st = printer_state.load()
        print(json.dumps(read_filament_telemetry(st), indent=2))
        return
    if a.cmd=="reconcile-launch":
        key=_printer_key(printer_state.load())
        if a.started or a.not_started:
            launch_state.resolve(key, "started" if a.started else "not_started")
        else:
            print(json.dumps(launch_state.pending(key), indent=2))
        return
    if a.cmd=="routine-receipt":
        print(routine_receipt(a.routine)); return
    if a.cmd=="routine-status":
        print(routine_phase(printer_state.load())); return
    pn_kw=dict(accept_default_z_offset=getattr(a,"accept_default_z_offset",False),reprint=getattr(a,"reprint",False),remap_slot_num=getattr(a,"remap_slot",None))
    if a.cmd=="print-now" and getattr(a,"yolo",False): pn_kw["yolo"]=True
    if getattr(a,"bed_clear_sha256",None): pn_kw["bed_clear"]=a.bed_clear_sha256
    elif getattr(a,"bed_clear_confirmed",False): pn_kw["bed_clear"]=True
    if a.cmd=="remap-slot":
        try:
            res = remap_slot(a.target, a.slot, a.tool)
        except ToolRemapRefused as exc:
            raise SystemExit(f"REFUSED: {exc}. Nothing changed.") from exc
        print("SLOT REMAPPED " + json.dumps(res, indent=2))
        return
    if a.cmd=="print-now":
        if a.accept_spool_change:
            os.environ["PRINT_ACCEPT_SPOOL_CHANGE"] = "1"
            print("SPOOL CHANGE ACCEPTED: slots may hold a different spool than at slicing (same material family)", flush=True)
        _print_now_cli(a.target, **pn_kw); return
    st=printer_state.load()
    if a.cmd=="slice":
        if not a.approved: raise SystemExit("REFUSED: explicit slicing approval required")
        if a.printer:
            _job = json.loads(Path(a.job_json).read_text()); _job["printer_model"] = a.printer
            write_job(Path(a.job_json).parent, _job)
        if getattr(a, 'accept_filament_warnings', False):
            try:
                record_filament_warning_acknowledgement(a.job_json)
            except (ValueError, OSError) as ack_error:
                raise SystemExit(f"REFUSED: {ack_error}")
        try:
            if not intake_unit_confirmation(a.job_json, units=a.units, interactive=sys.stdin.isatty()):
                return
            if not intake_color_choices(a.job_json, st, a.color_mode, a.color_slot, sys.stdin.isatty()):
                return
            if a.separate:
                _job = json.loads(Path(a.job_json).read_text()); _job["plate_mode"] = "separate"
                write_job(Path(a.job_json).parent, _job)
            out=slice_job(a.job_json, brim_override=a.brim)
        except FilamentWarningPending as pending:
            _print_filament_warning_packet(
                a.job_json, pending, f'slice {a.job_json} --approved --accept-filament-warnings')
            return
        if a.print_now: print_now(out, **pn_kw)
        return
    ok,probs,facts=printer_state.check(st)
    _print_check_warnings(facts)
    if not ok: raise SystemExit("I could not check the printer before starting, so nothing was sliced, uploaded or started. "
                                "Fix the check below, then try again:\n  " + "\n  ".join(probs))
    selection = None
    filament_specified = (a.filament_slot is not None or a.filament_color is not None or
                          a.filament_material is not None or a.filament_spoolman_id is not None)
    if filament_specified:
        try:
            selection = resolve_filament_selection(st, {
                "slot": a.filament_slot, "color": a.filament_color,
                "material": a.filament_material, "spoolman_id": a.filament_spoolman_id,
            })
        except ValueError as exc:
            raise SystemExit(f"That filament choice does not work: {exc} Nothing was sliced, uploaded or started.")
    job=inventory_for_url(a.url)
    if not job.get("sources"):
        raise SystemExit("REFUSED: source inventory contains no selectable files or profiles")
    if job.get("no_geometry_reason"):
        raise SystemExit(f"REFUSED: {job['no_geometry_reason']}")
    job["url"],job["hardware"],job["fetched_at"]=a.url,st,time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stamp_job_printer(job)
    if a.printer: job["printer_model"]=a.printer
    for n,x in enumerate(job["sources"],1): x["n"]=n
    if selection is not None:
        job["filament_selection"] = selection
    folder=intake_folder(ARCHIVE, job["slug"], a.intake_id)
    if a.intake_id:
        job['intake_id'] = a.intake_id
        if (folder/'JOB.json').exists():
            previous = json.loads((folder/'JOB.json').read_text())
            if (previous.get('url') != a.url or previous.get('intake_id') != a.intake_id
                    or previous.get('downloaded_file') or previous.get('gcode')
                    or previous.get('color_decisions') or previous.get('approvals')):
                raise SystemExit('REFUSED: intake already has a prepared job; resume its exact JOB.json explicitly, never overwrite history')
    folder.mkdir(parents=True,exist_ok=True); write_job(folder,job); show(job)
    if a.select is None:
        print(f"\nSTOPPED: inventory saved at {folder/'JOB.json'}. Choose --select N; slicing requires --approved."); return
    if a.inspect_only and (a.approved or a.print_now):
        raise SystemExit('REFUSED: inspect-only cannot approve slicing or printing')
    if not a.approved and not a.inspect_only: raise SystemExit("REFUSED: --select requires the operator's explicit --approved slicing authorization; use --inspect-only for preparation")
    if a.select<1 or a.select>len(job["sources"]): raise SystemExit("invalid --select")
    # The explicit-selection gate guards the record-bound slot printers (AD5X pair):
    # the wrong record slot is the wrong filament, silently. A Bambu AMS model's
    # explicit choice is its job ams_mapping (or the print path's snapshot
    # resolution), never a record slot, so the gate does not apply to it (#671).
    _print_now_adapter = printers.get(job.get("printer_model") or printers.current().key)
    if a.print_now and _print_now_adapter.multi_slot and _print_now_adapter.slots_bound_by_record and not job.get("filament_selection"):
        raise SystemExit("REFUSED: --print-now requires explicit filament selection (--filament-slot, --filament-color, --filament-material, or use 'filament-select' command)")
    chosen=job["sources"][a.select-1]
    # Site profile settings are never imported. The selected 3MF/STL supplies geometry
    # only, and is resliced to the live verified local nozzle and spool. Preserve the
    # metadata as a warning so a PLA CF or 0.4 Bambu preset cannot masquerade as local
    # compatibility.
    if job["site"]=="makerworld":
        notes=[]
        if chosen.get("site_nozzle_mm") not in (None, st["nozzle"]["diameter_mm"]): notes.append(f"site profile declares {chosen['site_nozzle_mm']} mm")
        site_mats=set(chosen.get("materials") or [])
        if site_mats and st["spool"]["material"] not in site_mats: notes.append(f"site profile material is {', '.join(sorted(site_mats))}; loaded material is {st['spool']['material']}")
        if notes: print("SITE PROFILE NOT REUSED: " + "; ".join(notes) + ". Reslicing geometry to live local state.")
    srcdir=folder/"source"; path=download_for_site(job,chosen,srcdir)
    intake_working_copy(path)  # canonical namespace and millimeters for everything below (#814, #825)
    job["selected_source"],job["downloaded_file"],job["audit"]=chosen,str(path),audit(path, printer_model=job.get("printer_model") or printers.current().key); write_job(folder,job)
    print("\nAUDIT: "+json.dumps(job["audit"],indent=2))
    if a.inspect_only:
        print('SOURCE_INSPECTED ' + json.dumps({'job_json': str(folder/'JOB.json'), 'sliced': False, 'uploaded': False, 'started': False}))
        return
    if not intake_unit_confirmation(folder/'JOB.json', units=a.units, interactive=sys.stdin.isatty()):
        print(f"\nSTOPPED: unit confirmation pending at {folder/'JOB.json'}. Resume with slice --approved --units mm|inch; slicing requires --approved.")
        return
    if getattr(a, 'accept_filament_warnings', False):
        try:
            record_filament_warning_acknowledgement(folder/'JOB.json')
        except (ValueError, OSError) as ack_error:
            raise SystemExit(f"REFUSED: {ack_error}")
    try:
        if not intake_color_choices(folder/'JOB.json', st, a.color_mode, a.color_slot, sys.stdin.isatty()):
            return
        out=slice_job(folder/"JOB.json", brim_override=a.brim)
    except FilamentWarningPending as pending:
        _print_filament_warning_packet(
            folder/'JOB.json', pending,
            f'fetch {a.url} --select {a.select} --approved --accept-filament-warnings' if a.select
            else 'slice ' + str(folder/'JOB.json') + ' --approved --accept-filament-warnings')
        return
    if a.print_now: print_now(out, **pn_kw)
