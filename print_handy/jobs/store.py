"""Job records: reading and writing JOB.json, the job a G-code file belongs to, the printer
and adapter a job targets, and the digests that bind a job's decisions.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import logging
import hashlib, json, os, re, time
from pathlib import Path

from .. import printers, settings
from ..core import launch_state, printer_state
from .common import ARCHIVE
from .filament import position_limit, validate_filament_selection

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.core.model_job")


def _timing(job_path, stage, **extra):
    """Launch latency instrumentation: append one line to <job>/TIMING.jsonl. Never raises.

    Stage map for the single-plate slice path (#952 — intervals are
    wall-clock, ``time.time()``): ``slice_start`` -> ``inventory_done`` is
    source inventory and the slice recipe; ``inventory_done`` ->
    ``orca_begin`` is orientation/derived-source preparation;
    ``orca_begin`` -> ``orca_finalize_done`` is all of ``slice_model`` —
    the slicer subprocess **plus** finalize and the post-slice guards —
    with the split recorded as ``slicer_s``/``python_s`` extras on
    ``orca_finalize_done`` (and as ``timing_s`` in the slice provenance);
    ``verify_begin`` -> ``verify_done`` is ``_verify_routed``; packaging
    has no marker of its own (on the ZMod upload route it sits between
    ``preflight_verify_done`` and ``upload_begin``, mixed with approval
    consumption and lease acquisition). Other slice paths (multi-plate,
    split) emit ``slice_done`` without the ``orca_*``/``verify_*`` stages.
    """
    try:
        line={"stage":stage,"t":round(time.time(),3),"pid":os.getpid(),**extra}
        logger.debug("TIMING %s", json.dumps(line))  # stdout stays clean in production logs
        if job_path is not None:
            with (Path(job_path).parent/"TIMING.jsonl").open("a") as out: out.write(json.dumps(line)+"\n")
    except Exception:
        pass


def write_job(folder, job):
    # Atomic replace: the background slice and the intake flow read JOB.json
    # concurrently; a plain write_text truncates first, so a reader could see
    # an empty file and fail with "Expecting value: line 1 column 1".
    target = Path(folder) / "JOB.json"
    tmp = target.with_name(f".JOB.json.{os.getpid()}.{time.monotonic_ns()}.tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, target)


def _detail_tail(detail):
    """Demote raw machinery to a secondary Detail line (#467).

    User-facing pipeline exits lead with a plain sentence; the raw stderr
    tail / response body that used to *be* the message rides along after
    it, whitespace-collapsed and capped, so a terminal or log reader keeps
    the evidence without a chat reader having to start there.
    """
    text = " ".join(str(detail or "").split())[:300]
    return f"\nDetail: {text}" if text else ""


def _recorded_printer_id(job):
    """A job's recorded printer_id, or None (unlike _job_printer_id, no default fallback:
    launch helpers route verification and monitors only when the job itself names a printer)."""
    return job.get("printer_id") if isinstance(job, dict) else None


CONTEXT = Path(os.environ.get("PRINT_HANDY_CONTEXT_PATH", settings.state_dir()/"print_handy_context.json"))
RECORD_KEYS = ("layer_height","first_layer_height","perimeters","fill_density","fill_pattern","support_material","brim_width","nozzle_diameter","filament_type","temperature","bed_temperature")

def _job_record_for_gcode(g):
    """(job, JOB.json path) for the archived job that produced G-code g, else (None, None)."""
    for jp in ARCHIVE.glob("*/JOB.json"):
        try: j=json.loads(jp.read_text())
        except (OSError, ValueError): continue
        if not isinstance(j, dict): continue
        paths=[j.get("gcode")]
        paths+=[r.get("gcode") for r in j.get("derived_plates") or [] if isinstance(r, dict)]
        paths+=[p.get("gcode_path") for p in (j.get("multipart_plan") or {}).get("parts") or [] if isinstance(p, dict)]
        if any(p and Path(p).resolve()==g for p in paths): return j, jp
    return None, None


def _job_for_gcode(g):
    """The archived JOB.json that produced G-code g.

    A split job keeps only plate 1 in JOB.json["gcode"]; plates 2..N live in
    derived_plates and multipart_plan.parts, so match those too (#48).
    """
    return _job_record_for_gcode(g)[0]


def decisions_digest(decisions):
    """Canonical geometry-to-filament choice identity, independent of dict order.

    A filament fallback (#1189) is identified by the operator decisions it
    was derived from: slice_job derives it in memory from JOB.json's
    color_decisions and the printer record (whose slots the slice recipe
    already binds), so the artifact binding and every "decisions changed
    during slicing" check keep comparing against what the job recorded.
    """
    if isinstance(decisions, dict) and isinstance(decisions.get('fallback'), dict) \
            and 'original' in decisions['fallback']:
        return decisions_digest(decisions['fallback']['original'])
    return hashlib.sha256(json.dumps(decisions, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _recipe_changed(job, latest, printer_id=None):
    """True when JOB.json, the hardware record, profiles or slicer now give another recipe than the one sliced.

    The recomputation loads the job's own printer record, exactly as
    slice_job did when it recorded the recipe: a secondary printer whose
    nozzle or filament slots differ from the default printer's must not
    read as a changed recipe, and reslicing must be able to clear the
    mismatch. A job with no recorded printer keeps the legacy load.
    """
    from ..core.slice_recipe import recipe_sha256, slice_recipe
    sliced = job['slice_recipe']['recipe']
    if printer_id is None:
        printer_id = _recorded_printer_id(job) or _recorded_printer_id(latest)
    state = printer_state.load(printer_id) if printer_id else printer_state.load()
    current = slice_recipe(latest, sliced['source_sha256'], state, **sliced['overrides'])
    return recipe_sha256(current) != job['slice_recipe']['sha256']


def _job_printer_id(job):
    """The roster printer id a job belongs to: its recorded printer_id, else the default printer."""
    if isinstance(job, dict) and job.get("printer_id"):
        return job["printer_id"]
    return settings.default_printer_id()


def _job_adapter(job):
    """Adapter for the printer a job belongs to.

    With a recorded printer_id (#334) the adapter is that roster entry's —
    its model, built on the entry's settings view (the uploaded-G-code path's
    record-first resolution, generalised). Without one, the model the job was
    prepared for: its recorded override, else the configured model.
    """
    if isinstance(job, dict) and job.get("printer_id"):
        entry = settings.by_id(job["printer_id"])
        return printers.get(entry["model"], settings.view_for(entry["id"]))
    key = job.get("printer_model") if isinstance(job, dict) else None
    if not key or key == printers.current().key:
        return printers.current()
    return printers.get(key)


def effective_color_decisions(job):
    """The decisions the job's artifact was sliced with: a recorded #1189 fallback, else color_decisions."""
    job = job if isinstance(job, dict) else {}
    recorded = job.get('filament_fallback')
    decisions = job.get('color_decisions')
    if isinstance(recorded, dict) and isinstance(recorded.get('decisions'), dict) \
            and decisions_digest(recorded['decisions']) == decisions_digest(decisions):
        return recorded['decisions']
    return decisions


def gcode_estimate_s(text):
    m=re.search(r'^; estimated printing time \(normal mode\) = (.+)$',text,re.M)
    if not m:
        # Bambu Studio (and Orca's header block) write "; model printing time: 1h 2m 3s; total estimated
        # time: 1h 7m 3s" instead (#271): printing time first, the slicer's total as the fallback.
        m=(re.search(r'^;.*?\bmodel printing time\s*:\s*([^;\n]+)',text,re.M)
           or re.search(r'^;.*?\btotal estimated time\s*:\s*([^;\n]+)',text,re.M))
    if not m: return None
    return sum(int(n)*{"d":86400,"h":3600,"m":60,"s":1}[u] for n,u in re.findall(r'(\d+)\s*([dhms])',m.group(1))) or None


def human_dur(sec):
    h,m=divmod(int(round(sec/60)),60)
    return f"{h}h {m}m" if h else f"{m}m"


def remote_name(gcode, sha, job=None):
    """Moonraker file name: readable model title first, then the plate stem and exact SHA256.

    The title makes Moonraker, Fluidd and the printer screen show what is printing; the
    stem plus full hash keeps the name unique and lets discord_print_summary.find_job
    bind it back to the exact local artifact.
    """
    g = Path(gcode)
    title = job.get("title") if isinstance(job, dict) else None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "")).strip("-")[:40].strip("-")
    stem = g.stem if not slug or slug.lower() == g.stem.lower() else f"{slug}_{g.stem}"
    return f"{stem}_{sha}{g.suffix}"


def _printer_key(st=None, job=None):
    """The submission-lease identity for a launch: the job's roster printer id (#334).

    Kept under its old name for the callers and tests that keyed a lease by
    the hardware record; the record no longer names the lease — the job's
    printer does, and a job without one belongs to the default printer.
    """
    return _job_printer_id(job)


def resolve_print_target(target, archive=None):
    """A bare printer file name (what Moonraker shows) maps to the archived local artifact
    that was uploaded under that name. Never searches outside the archive, never downloads."""
    raw = str(target)
    p = Path(raw).expanduser()
    if p.is_absolute() or p.exists():
        return raw
    # A printer-reported path for an upload resolves too: ZMod reports a 3MF delivery as
    # 3mf_print/X.gcode (launch_state.same_remote), and the listener passes that name as is
    # (#847 review: the pending spool retry and runout slot lookup never found the job).
    reported = "/" in raw
    archive = Path(archive or ARCHIVE)
    for pat in ("*/sliced/*/*/*.launch.json", "*/sliced/*/*/*.upload.json"):
        for rec in sorted(archive.glob(pat), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                d = json.loads(rec.read_text())
            except (OSError, ValueError):
                continue
            item = d.get("item") if isinstance(d.get("item"), dict) else {}
            names = [n for n in (d.get("file"), d.get("remote"), d.get("remote_name"), item.get("path")) if n]
            if raw in names or (reported and any(launch_state.same_remote(raw, n) for n in names)):
                artifact = rec.with_name(rec.name.split(".", 1)[0] + ".gcode")
                if artifact.is_file():
                    return str(artifact)
    if reported:
        return raw  # a relative local path, as before: the caller reports it missing
    raise SystemExit(f"REFUSED: {raw} is a printer file name, not a local path, and no archived job uploaded a "
                     "file with that name. Pass the job's JOB.json instead. Nothing uploaded or started.")


def safe_slug(s): return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')[:70]
def intake_id_from_message(source, message_id):
    """Return the canonical intake identity for a Discord message, never its snowflake."""
    if not message_id:
        raise ValueError('plain model link requires a stable Discord message ID')
    identity = [getattr(source, 'profile', ''), str(getattr(source, 'chat_id', '')),
                str(getattr(source, 'thread_id', '') or ''), str(message_id)]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def intake_folder(archive, slug, intake_id=None):
    if intake_id is not None and not re.fullmatch(r'[a-f0-9]{64}', intake_id):
        raise ValueError('intake ID must be a 64 character lowercase SHA256')
    return Path(archive) / (safe_slug(slug) + ('--' + intake_id if intake_id else ''))


def save_filament_selection(job_path, selection):
    """Persist a confirmed selection only in the explicitly named job file."""
    path = Path(job_path)
    job = json.loads(path.read_text())
    if not isinstance(job, dict) or not job.get("model_id"):
        raise ValueError("not a model JOB.json")
    job["filament_selection"] = validate_filament_selection(selection, limit=position_limit(_job_adapter(job)))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, indent=2) + "\n")
    os.replace(tmp, path)
    return job["filament_selection"]


def set_color_decisions(job, decisions):
    if decisions_digest(job.get('color_decisions')) != decisions_digest(decisions):
        if job.get('gcode'):
            job.setdefault('artifact_history', []).append({key: job[key] for key in
                ('gcode', 'gcode_verified', 'gcode_full_verified_sha256', 'provenance', 'artifact_binding',
                 'sliced_at', 'slice_config', 'slice_recipe') if key in job})
        for key in ('gcode', 'provenance', 'artifact_binding', 'sliced_at', 'slice_config', 'slice_recipe',
                    'gcode_full_verified_sha256'):
            job.pop(key, None)
        job['gcode_verified'] = False
    job['color_decisions'] = decisions
    if decisions['mode'] == 'single_color':
        job['filament_selection'] = decisions['selection']
    else:
        job.pop('filament_selection', None)


def _recipe_binding(job):
    """Recipe fields every artifact_binding carries; slice_job records the recipe before slicing."""
    return {'recipe_version': job['slice_recipe']['version'], 'recipe_sha256': job['slice_recipe']['sha256']}


def stamp_job_printer(job, printer_id=None):
    """Record the creating printer's roster id on a new job (#334, design §2.2).

    A job learns its printer at creation and never re-derives it: every later
    path (state, lease, approval, adapter) keys off JOB.json's printer_id.
    Jobs written before the upgrade carry no id and belong to the configured
    single printer — see _job_printer_id.

    An explicit ``printer_id`` — the printer the intake channel routes to
    (#335) — wins over the settings default when the job does not name one
    yet; an unknown explicit id raises, naming it, rather than silently
    stamping a different printer.
    """
    if isinstance(job, dict) and not job.get("printer_id"):
        if printer_id:
            job["printer_id"] = settings.by_id(str(printer_id))["id"]
        else:
            try:
                job["printer_id"] = settings.default_printer_id()
            except Exception:
                pass
    return job


def record_support_decision(folder, supports, by='operator'):
    """Record the operator's supports choice on JOB.json (retries and reprints never ask again)."""
    path = Path(folder) / 'JOB.json'
    job = json.loads(path.read_text())
    job['support_decision'] = {'supports': bool(supports), 'by': by,
                               'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_job(folder, job)
    return job
