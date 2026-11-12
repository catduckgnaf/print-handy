"""Offline and live verification of a sliced G-code artifact.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names
are still importable from there for one release.
"""
import hashlib, json, math, re, sys

from .. import printers, settings
from ..core import printer_state
from ..jobs.common import BED_MM, MAX_HEIGHT_MM, _mr
from .artifact import GcodeArtifact
from .bounds import BOUNDS_PARSER_VERSION, cached_motion_bounds


def zmod_md5_valid(path):
    """Validate ZMod preprocess format: first-line digest covers remaining bytes."""
    data = GcodeArtifact.of(path).raw
    first, sep, payload = data.partition(b"\n")
    match = re.fullmatch(rb"; MD5:([0-9a-fA-F]{32})\r?", first)
    if not sep or not match:
        return False
    return hashlib.md5(payload).hexdigest().encode("ascii") == match.group(1).lower()


def native_heater_lines_valid(text, bed, hotend, adapter=None):
    """Require the documented native startup prefix, not later layer heaters."""
    valid = (adapter or printers.current()).native.native_startup_valid
    return valid(text, bed, hotend)


_TOOL_LINE_RE = re.compile(r"T(\d+)(?:\s*;.*)?")
# First characters a stripped bare-Tn line can hide behind (#925): 'T', or
# whitespace strip() removes that splitlines() does not treat as a boundary.
_TOOL_CANDIDATES = frozenset('T \t\x1f\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005'
                             '\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000')


def _gcode_tools(text):
    """Tool indexes a G-code selects with a bare Tn line.

    One streaming pass (#925): candidate lines still go through the exact
    same strip + fullmatch as the original scan; the motion/comment bulk
    of the plate is skipped with one first-character lookup per line.
    """
    tools = set()
    add = tools.add
    fullmatch = _TOOL_LINE_RE.fullmatch
    for line in text.splitlines():
        if line[:1] in _TOOL_CANDIDATES and (m := fullmatch(line.strip())):
            add(int(m.group(1)))
    return tools


def _refuse_multicolor(text):
    """Non-multi-slot families refuse a G-code that selects any tool but T0, through the adapter."""
    adapter = printers.current()
    adapter.normalize_tool_mapping({str(t): t + 1 for t in sorted(_gcode_tools(text) | {0})}, {})
    return adapter


_VERIFY_LINE_PREFIXES = (
    '; filament_type = ', '; nozzle_diameter = ', '; first_layer_height = ', '; initial_layer_print_height = ',
    '; nozzle_temperature_initial_layer = ', '; nozzle_temperature = ',
    'START_PRINT', 'PRINT_START', 'END_PRINT', 'PRINT_END', 'M190', 'M109', 'M104', 'M140', ';LAYER_CHANGE',
    printer_state.TOOL_LINE_ANCHOR, printer_state.TOOL_SLOTS_ANCHOR,
)


def _verify_lines(text, adapter=None):
    """One GcodeLines scan for every line-anchored question offline verification asks (#1153 item 7).

    The adapter's own start and end macro names are declared too, read
    defensively: an adapter without them, or one whose property raises,
    simply adds nothing here, and the check that really reads the name
    behaves exactly as before.
    """
    from ..core.gcode_lines import GcodeLines
    macros = []
    for name in ('start_macro', 'end_macro'):
        try:
            value = getattr(adapter, name, None)
        except Exception:
            value = None
        if isinstance(value, str):
            macros.append(value)
    return GcodeLines(text, _VERIFY_LINE_PREFIXES + tuple(macros))


def _with_gcode_temperatures(state, text, recorded=None, lines=None):
    """state with temps_c set to what this G-code's own spool resolves to (printer_state.gcode_temperatures),
    so a slot job sliced at its spool's temperature is checked against that, not the printer-wide record.

    `recorded` temperatures — the ones preserved in the artifact's slice
    provenance (P2-275) — win outright when usable: re-resolving at verify
    time can see a different answer than slicing did (Spoolman down falls
    back to material defaults) and refuse a sound artifact.

    ``lines`` is the caller's GcodeLines scan of ``text`` (verification
    already has one), so the filament_type and spool-header lookups here
    cost no extra pass.
    """
    if recorded:
        try:
            temps = {'nozzle': float(recorded['nozzle']), 'bed': float(recorded['bed'])}
        except (KeyError, TypeError, ValueError):
            temps = None
        if temps is not None:
            return {**state, 'temps_c': temps}
    from ..core.orca_config import split_config_list
    from ..core import spoolman_client
    if lines is None:
        lines = _verify_lines(text)
    listed = lines.config_value('filament_type')
    try:
        materials = split_config_list(listed) if listed is not None else []
    except ValueError:
        materials = []

    def fetch(spool_id):
        record = spoolman_client.get_spool(spool_id, base_url=printer_state.spoolman_url(state))
        return None if not record or record.get('archived') else record.get('filament')
    try:
        temps = printer_state.gcode_temperatures(state, materials, fetch, text, lines=lines)
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(f"G-code temperature check failed: {exc}") from exc
    return {**state, 'temps_c': temps}


@GcodeArtifact.read_once
def verify_gcode_offline(gcode_path, state, adapter=None, recorded_temps=None):
    """Check startup, checksum, and bed bounds without contacting the printer.

    The live sibling reads display mode from Moonraker. This one validates the
    artifact against the already loaded hardware record: MD5 when present,
    native heater prefix, nozzle, material, start and end, and motion bounds.
    """
    adapter = adapter or printers.current()
    if adapter.connection in ('offline', 'bambulab_lan', 'prusalink', 'elegoo_sdcp', 'flashforge_stock'):
        kw = {'recorded_temps': recorded_temps} if recorded_temps else {}
        return _verify_offline(gcode_path, state, adapter, **kw)
    artifact = GcodeArtifact.of(gcode_path)
    p = artifact.path
    raw = artifact.raw
    text = artifact.text
    lines = _verify_lines(text, adapter)
    errors = []
    if raw.startswith(b'; MD5:') and not zmod_md5_valid(p):
        errors.append('invalid ZMod MD5 checksum')
    state = _with_gcode_temperatures(state, text, recorded_temps, lines=lines)
    from ..core.nozzle import effective_nozzle_temperature
    nozzle_type = (state.get('nozzle') or {}).get('type') or settings.current().get('printer', {}).get('nozzle_type') or 'hardened_steel'
    try:
        hotend = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle']))
    except Exception as exc:
        raise RuntimeError("offline G-code verification failed: invalid effective nozzle temperature: " + str(exc)) from exc
    if not native_heater_lines_valid(text, state['temps_c']['bed'], hotend, adapter):
        errors.append('startup heater prefix does not match the recorded bed and effective nozzle temperature')
    nozzle = lines.config_value('nozzle_diameter')
    if nozzle is None or abs(float(nozzle) - state['nozzle']['diameter_mm']) > 1e-3:
        errors.append(f"nozzle {nozzle} != recorded {state['nozzle']['diameter_mm']}")
    hotend, bed = effective_nozzle_temperature(nozzle_type, float(state['temps_c']['nozzle'])), state['temps_c']['bed']
    start_match = lines.search(r'^START_PRINT\s+([^;]*)', 'START_PRINT')
    start_args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', start_match.group(1))) if start_match else {}
    start_matches = (start_match is not None and
                     math.isclose(float(start_args.get('BED_TEMP', 'nan')), float(bed), abs_tol=1e-3) and
                     math.isclose(float(start_args.get('EXTRUDER_TEMP', 'nan')), float(hotend), abs_tol=1e-3))
    if not start_matches:
        errors.append(f'missing START_PRINT matching bed {bed} and effective hotend {hotend}')
    if not lines.search(r'^END_PRINT(?:\s|$)', 'END_PRINT'):
        errors.append('missing END_PRINT')
    bounds = cached_motion_bounds(raw, text)
    limits = state.get('axis_limits_mm', {'x': [0, BED_MM], 'y': [0, BED_MM], 'z': [0, state.get('max_print_height_mm', MAX_HEIGHT_MM)]})
    for axis in 'xyz':
        low, high = bounds[axis]
        limit_low, limit_high = map(float, limits[axis])
        if low < limit_low - 1e-3 or high > limit_high + 1e-3:
            errors.append(f'{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}')
    if errors:
        raise RuntimeError('offline G-code verification failed: ' + '; '.join(errors))
    return {'bounds_mm': bounds, 'nozzle': nozzle, 'offline': True}


def _verify_offline(gcode_path, state, adapter=None, recorded_temps=None):
    """A printer Print Handy cannot reach (bambulab.a1): the adapter's checks against the hardware record."""
    adapter = adapter or printers.current()
    from ..core.printer_state import _material_temperature_contract
    try:
        _material_temperature_contract(state)
    except RuntimeError as exc:
        raise RuntimeError(f'offline G-code verification failed: {exc}') from exc
    text = GcodeArtifact.of(gcode_path).read_text(errors='ignore')
    problems = adapter.gcode_problems(text, _with_gcode_temperatures(state, text, recorded_temps))
    if problems:
        raise RuntimeError('offline G-code verification failed: ' + '; '.join(problems))
    return {'offline': True, 'connection': adapter.connection}


def _print_check_warnings(facts):
    """Surface non-blocking check warnings (Spoolman, #235) without refusing work.

    Warnings go to stderr, the channel #240 established for verify_gcode, so
    every caller of this helper reports each warning exactly once.
    """
    for warning in (facts or {}).get("warnings") or []:
        print(f"WARNING: {warning}", file=sys.stderr, flush=True)


@GcodeArtifact.read_once
def verify_gcode(gcode_path, adapter=None, state=None, printer_id=None, recorded_temps=None):
    """Check a G-code artifact against live nozzle, spool and temperature state.

    Spoolman is never a gate here (#235): printer_state.check reports its
    outcomes as warnings, and verification proceeds on the recorded state —
    the spool's recorded material/density and the recorded temperatures are
    the fallback when Spoolman is unreachable.

    With no routing arguments this verifies against the configured printer,
    exactly as it always has. A caller launching a job for another roster
    printer passes that job's adapter, hardware record and printer_id, and
    every check below — connection, native contract, start/end macros,
    temperatures — is the target printer's, never the default's
    (SAFE-4 / P1-75).
    """
    routed = adapter is not None or state is not None or printer_id is not None
    if adapter is None:
        adapter = printers.current()
    if state is None:
        state = printer_state.load(printer_id) if printer_id else printer_state.load()
    st = state
    # The offline/LAN families verify without a live session, so their
    # write-readiness state (busy, Developer Mode, a live fault) must not
    # gate this check (P2-73): the check runs record-only for them, and the
    # print paths re-run the live readiness before any write.
    offline_verify = adapter.connection in ('offline', 'bambulab_lan', 'prusalink', 'elegoo_sdcp', 'flashforge_stock')
    live_kw = {'live': False} if offline_verify else {}
    if routed:
        ok, probs, facts = printer_state.check(st, printer_id=printer_id, adapter=adapter, **live_kw)
    else:
        ok, probs, facts = printer_state.check(st, **live_kw)
    _print_check_warnings(facts)
    if not ok: raise RuntimeError("the printer check did not pass:\n  " + "\n  ".join(probs))
    if offline_verify:
        kw = {'recorded_temps': recorded_temps} if recorded_temps else {}
        return _verify_offline(gcode_path, st, adapter, **kw)
    artifact=GcodeArtifact.of(gcode_path); p=artifact.path; raw=artifact.raw; text=artifact.text
    lines=_verify_lines(text, adapter)
    errors=[]
    # The installed native screen requires ZMod's documented first-line MD5.
    # Screenless files remain accepted without it, but any present checksum is validated.
    if raw.startswith(b"; MD5:") and not zmod_md5_valid(p):
        errors.append("invalid ZMod MD5 checksum")
    native = adapter.has_native_screen
    mode = _mr(st, "/printer/objects/query?save_variables")["result"]["status"]["save_variables"]["variables"].get("display_off") if native else None
    nozzle_type = (st.get("nozzle") or {}).get("type") or settings.current().get("printer", {}).get("nozzle_type") or "hardened_steel"
    st = _with_gcode_temperatures(st, text, recorded_temps, lines=lines)
    from ..core.printer_state import _material_temperature_contract
    try:
        _material_temperature_contract(st)
    except RuntimeError as exc:
        errors.append(str(exc))
    from ..core.nozzle import effective_nozzle_temperature
    effective_hotend = effective_nozzle_temperature(nozzle_type, st["temps_c"]["nozzle"])
    if not native:
        pass  # no native screen: no MD5 or startup-heater contract
    elif mode == 0:
        if not zmod_md5_valid(p):
            errors.append("native screen requires valid ZMod MD5 checksum")
        if not native_heater_lines_valid(text, st["temps_c"]["bed"], effective_hotend, adapter):
            errors.append("native screen requires immediate startup M190 Sbed / M104 Snozzle T0 consistent with START_PRINT; later heaters do not count")
    elif mode != 1:
        errors.append(f"unknown display_off mode {mode!r}; refusing hardware-dependent G-code validation")
    config=lines.config_value
    nozzle=config("nozzle_diameter")
    nozzle_value=None
    if nozzle is None:
        errors.append("missing embedded nozzle_diameter")
    else:
        try:
            nozzle_value=float(nozzle)
        except ValueError:
            errors.append(f"invalid nozzle_diameter {nozzle!r}")
        else:
            if abs(nozzle_value-st["nozzle"]["diameter_mm"])>1e-3:
                errors.append(f"G-code nozzle {nozzle} != live {st['nozzle']['diameter_mm']}")
    material=config("filament_type")
    if native:
        validate_materials = adapter.native.validate_materials
        try:
            validate_materials(text, material, st)
        except RuntimeError as exc:
            errors.append(str(exc))
    else:
        _refuse_multicolor(text)
        from ..core.orca_config import split_config_list
        try:
            # Orca quotes a material with a space: `"Silk PLA"` is the material Silk PLA.
            listed = split_config_list(material)
        except ValueError:
            listed = []
        if len(listed) == 1:
            material = listed[0]
        if not printer_state.same_material(material, st["spool"]["material"]):
            errors.append(f"G-code filament_type {material!r} != loaded {st['spool']['material']!r}")
    hotend,bed=effective_hotend,st["temps_c"]["bed"]
    def numeric_macro_match(macro_name, bed_key, hotend_key):
        match = lines.search(rf'^{re.escape(macro_name)}\s+([^;]*)', macro_name)
        args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', match.group(1))) if match else {}
        try:
            return (match is not None and
                    math.isclose(float(args.get(bed_key, 'nan')), float(bed), abs_tol=1e-3) and
                    math.isclose(float(args.get(hotend_key, 'nan')), float(hotend), abs_tol=1e-3))
        except ValueError:
            return False
    octoprint = getattr(adapter, 'transport', None) == 'octoprint'
    if octoprint:
        for command, target in (('M190', bed), ('M109', hotend)):
            first = lines.search(rf'^{command}\s+S([\d.]+)', command)
            if not first or not math.isclose(float(first.group(1)), float(target), abs_tol=1e-3):
                errors.append(f'missing {command} matching target {target}')
        # Validate the slicer-owned shutdown, never append a guessed end sequence.
        if not all(lines.search(rf'^{cmd}\s+S0(?:\s|$)', cmd) for cmd in ('M104', 'M140')):
            errors.append('Marlin slicer end G-code must turn off nozzle and bed (M104 S0, M140 S0)')
    elif not native:
        macro = adapter.start_macro
        if not (numeric_macro_match(macro, 'BED', 'HOTEND') or
                sovol_style_start_matches(text, bed, hotend, lines=lines)):
            errors.append(f"missing {macro} matching bed {bed} and effective hotend {hotend}"
                          " (accepted starts: the configured macro with BED/HOTEND arguments,"
                          " START_PRINT with BED_TEMP/EXTRUDER_TEMP arguments, or a bare"
                          " START_PRINT whose M190/M109 heater lines match those temperatures)")
    elif not (numeric_macro_match('PRINT_START', 'BED', 'HOTEND') or
              numeric_macro_match('START_PRINT', 'BED_TEMP', 'EXTRUDER_TEMP')):
        errors.append(f"missing START_PRINT/PRINT_START matching bed {bed} and effective hotend {hotend}")
    def ends_with(macro_name):
        return lines.search(rf'^{re.escape(macro_name)}(?:\s|$)', macro_name)
    if not octoprint:
        if native:
            if not (ends_with('PRINT_END') or ends_with('END_PRINT')):
                errors.append("missing END_PRINT/PRINT_END safe end sequence")
        else:
            end_macro = adapter.end_macro
            default_end_macro = (adapter.model.defaults().get("printer") or {}).get("end_macro")
            if end_macro != default_end_macro:
                # A customized [printer] end_macro is the only safe end (P2-13): a file
                # ending in a different macro would run a shutdown sequence this printer
                # does not define, so PRINT_END/END_PRINT no longer substitute for it.
                if not ends_with(end_macro):
                    errors.append(f"missing {end_macro} safe end sequence")
            elif not (ends_with('PRINT_END') or ends_with('END_PRINT') or ends_with(end_macro)):
                errors.append("missing END_PRINT/PRINT_END safe end sequence")
    try:
        bounds=cached_motion_bounds(raw, text)
    except RuntimeError as exc:
        errors.append(f"toolpath bounds unavailable: {exc}")
        bounds=None
    if bounds:
        limits=st.get("axis_limits_mm", {"x":[0,223],"y":[0,223],"z":[0,st.get("max_print_height_mm",250)]})
        for axis in "xyz":
            low,high=bounds[axis]
            limit_low,limit_high=map(float,limits[axis])
            if low<limit_low-1e-3 or high>limit_high+1e-3:
                errors.append(f"{axis.upper()} bounds {low:.3f}..{high:.3f} exceed {limit_low:.3f}..{limit_high:.3f}")
    first_layer_height=config("first_layer_height") or config("initial_layer_print_height")
    try:
        if first_layer_height is None or float(first_layer_height)<=0:
            errors.append(f"invalid first_layer_height {first_layer_height!r}")
    except ValueError:
        errors.append(f"invalid first_layer_height {first_layer_height!r}")
    # Lines narrower than 90% of the orifice are refused, first layer and body alike.
    if nozzle_value is not None:
        floor=0.9*nozzle_value
        # The first six ;LAYER_CHANGE pieces are all this check reads (layer 0,
        # the first layer, layers 2-5); the scan already knows where they are.
        layers, layer_count = lines.split(';LAYER_CHANGE', 6)
        # Orca (notably with Arachne walls) writes ;WIDTH only when it changes, so a
        # layer can inherit its width from an earlier one. Carry the width in effect
        # across layers and count it once per structural feature (;TYPE) as well.
        carried = [None]
        def structural_widths(block):
            # Gap infill deliberately fills spaces narrower than the nozzle. Its
            # transition count is not a wall-flow measurement; never remove model
            # features merely to raise this metadata median.
            role = None
            widths = []
            # The width in effect when a feature starts is inherited, but it
            # counts for that feature only once extrusion actually uses it: a
            # ;WIDTH line that follows replaces it before any plastic is laid,
            # so counting it anyway would credit the feature with the previous
            # feature's width and could lift the median past the floor (P2-66).
            inherited = None
            for line in block.splitlines():
                if line.startswith(";TYPE:"):
                    role = line[6:].strip().casefold()
                    inherited = carried[0]
                elif line.startswith(";WIDTH:"):
                    carried[0] = float(line[7:])
                    inherited = None
                    if role not in ("gap infill", "gap fill"):
                        widths.append(carried[0])
                elif inherited is not None and re.match(r'G[01] [^;]*E[0-9.]*[1-9]', line):
                    if role not in ("gap infill", "gap fill"):
                        widths.append(inherited)
                    inherited = None
            if not widths and carried[0] is not None and role not in ("gap infill", "gap fill") \
                    and re.search(r'^G[01] [^;\n]*E[0-9.]*[1-9]', block, re.M):
                # Extrudes with no new ;TYPE/;WIDTH line: the width in effect still applies.
                widths.append(carried[0])
            return widths
        structural_widths(layers[0] if layers else "")
        first=structural_widths(layers[1] if layer_count>1 else "")
        body=structural_widths("".join(layers[2:6]))
        for name,ws in (("first layer",first),("layers 2-5",body)):
            if name == "layers 2-5" and layer_count <= 2:
                # Single-layer test print (e.g. first-layer calibration pad) has no subsequent layers
                continue
            if not ws:
                errors.append(f"missing {name} WIDTH metadata")
                continue
            med=sorted(ws)[len(ws)//2]
            if med<floor: errors.append(f"{name} line width {med:.2f} mm < {floor:.2f} mm (90% of {nozzle} nozzle): starved extrusion, reslice")
    if errors: raise RuntimeError("the G-code did not pass its safety checks: " + "; ".join(errors))
    print(f"G-CODE OK: {p.name} | {printer_state.summary(st)}")
    return True


def _recorded_temperatures(job, gcode_path):
    """The temperatures a job's artifact was sliced at, from its slice provenance (P2-275).

    The single-plate provenance lives on the job; a split job's lives on
    the derived_plates row for this file. None when the job records none —
    artifacts sliced before temperatures were preserved verify as before.
    """
    if not isinstance(job, dict):
        return None
    target = str(gcode_path)
    provenance = None
    if str(job.get('gcode') or '') == target:
        provenance = job.get('provenance')
    else:
        for row in job.get('derived_plates') or []:
            if isinstance(row, dict) and str(row.get('gcode')) == target:
                provenance = row.get('provenance')
                break
    temps = (provenance or {}).get('temperatures') if isinstance(provenance, dict) else None
    return temps if isinstance(temps, dict) else None


def _verification_facts_digest(state):
    """Digest of the hardware facts a full verification was checked against.

    The nozzle diameter and type (the embedded nozzle check and the heater
    limit), the axis limits and print height (the bounds check) and the
    bounds parser version. A change to any of them after slicing means the
    earlier verdict says nothing about the printer as it is now.
    """
    state = state or {}
    nozzle = state.get("nozzle") or {}
    facts = {"nozzle_diameter_mm": nozzle.get("diameter_mm"), "nozzle_type": nozzle.get("type"),
             "axis_limits_mm": state.get("axis_limits_mm"),
             "max_print_height_mm": state.get("max_print_height_mm"),
             "bounds_parser": BOUNDS_PARSER_VERSION}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _full_verified_marker(gcode_sha, state):
    """The gcode_full_verified_sha256 value: the artifact digest bound to the
    hardware facts it was verified against (``<sha>+<facts digest>``)."""
    return f"{gcode_sha}+{_verification_facts_digest(state)}"


def _verify_state(state, printer_id):
    """The hardware record verify_gcode checks against on this route, or None
    when it cannot be read (then no skip is taken and no marker is written)."""
    try:
        if printer_id:
            return state if state is not None else printer_state.load(printer_id)
        return printer_state.load()
    except Exception:
        return None


def _already_verified_artifact(job, gcode_path, state=None):
    """True only when the full verification already passed on these bytes (#925).

    The evidence is threefold: ``gcode_verified`` must be true, the
    artifact's current SHA-256 must equal the bound
    ``artifact_binding.gcode_sha256`` (the plain digest for a single
    artifact, or this plate's entry of the per-plate digest map for a
    split job), and ``gcode_full_verified_sha256`` must name that same
    digest bound to the current hardware facts (_full_verified_marker:
    nozzle, axis limits, parser version), so a nozzle swap, a changed bed
    limit or a parser upgrade after slicing forces the full verification
    again; a marker written before the facts were bound (a bare digest)
    never matches. The marker is recorded only by _verify_routed after the full
    verify_gcode — including its first-layer and line-width checks — has
    passed on the artifact; slice paths that run only the offline verify
    set ``gcode_verified`` without it, so their artifacts still get the
    full launch verification. Anything else (no job, no binding, an
    unverified job, a missing or stale marker, a malformed binding, a
    changed file) is not evidence and returns False, so the caller runs
    the full verification exactly as before.
    """
    if not isinstance(job, dict) or job.get('gcode_verified') is not True or not isinstance(state, dict):
        return False
    binding = job.get('artifact_binding')
    if not isinstance(binding, dict):
        return False
    stored = binding.get('gcode_sha256')
    if isinstance(stored, dict):
        stored = None
        target = str(gcode_path)
        for row in job.get('derived_plates') or []:
            if isinstance(row, dict) and str(row.get('gcode')) == target:
                key = str(row.get('binding_key') or row.get('slot'))
                digest = binding['gcode_sha256'].get(key)
                stored = digest if isinstance(digest, str) else None
                break
    if not isinstance(stored, str) or not stored:
        return False
    if job.get('gcode_full_verified_sha256') != _full_verified_marker(stored, state):
        return False
    try:
        return GcodeArtifact.of(gcode_path).sha256 == stored
    except OSError:
        return False


def _run_live_printer_check(adapter, state, printer_id):
    """printer_state.check exactly as verify_gcode runs it (live hardware record).

    The verified-artifact skip in _verify_routed replaces only the G-code
    body parse; the live check — live nozzle, pressure advance and probe
    calibration against the hardware record — still runs, with the same
    routing and the same refusal text verify_gcode produces.
    """
    if printer_id:
        use_adapter = adapter or printers.current()
        use_state = state if state is not None else printer_state.load(printer_id)
    else:
        use_adapter = printers.current()
        use_state = printer_state.load()
    offline = use_adapter.connection in ('offline', 'bambulab_lan', 'prusalink', 'elegoo_sdcp', 'flashforge_stock')
    live_kw = {'live': False} if offline else {}
    if printer_id:
        ok, probs, facts = printer_state.check(use_state, printer_id=printer_id, adapter=use_adapter, **live_kw)
    else:
        ok, probs, facts = printer_state.check(use_state, **live_kw)
    _print_check_warnings(facts)
    if not ok:
        raise RuntimeError("the printer check did not pass:\n  " + "\n  ".join(probs))


@GcodeArtifact.read_once
def _verify_routed(gcode_path, adapter, state, printer_id, job=None):
    """verify_gcode for a launch path that already resolved its printer.

    A job naming a roster printer verifies against that printer's adapter
    and hardware record (SAFE-4 / P1-75); anything else verifies exactly
    as before, against the configured printer. A job's recorded slice
    temperatures ride along so the verify checks the artifact against
    what it was sliced at (P2-275).

    #925: when the full verification already passed on this exact
    artifact (see _already_verified_artifact), the G-code body is not
    re-parsed for the offline bounds/tool checks — slicing ran them on
    these bytes seconds earlier. Only the body parse is skipped: the
    live printer_state.check verify_gcode performs still runs here, and
    the launch paths around this call keep their own live guards
    (idle/mesh/readiness checks, require_current_artifact's recipe and
    binding re-checks, the embedded tool-map read). A SHA mismatch, a
    missing verified flag or a missing full-verification marker falls
    back to the full verification below. The result mirrors the offline
    shape verify_gcode returns for the offline/LAN families, with
    skipped_reparse naming what was skipped.

    A successful full verification records the artifact's digest in the
    job's gcode_full_verified_sha256 marker, so a later launch of the
    same bytes can take the skip; callers that persist the job (slice,
    remap) persist the marker with it.
    """
    facts_state = _verify_state(state, printer_id) if isinstance(job, dict) else None
    if _already_verified_artifact(job, gcode_path, facts_state):
        _run_live_printer_check(adapter, state, printer_id)
        connection = getattr(adapter, 'connection', None) or 'offline'
        return {'offline': True, 'connection': connection, 'skipped_reparse': True}
    recorded = _recorded_temperatures(job, gcode_path)
    kw = {'recorded_temps': recorded} if recorded else {}
    if printer_id:
        result = verify_gcode(gcode_path, adapter=adapter, state=state, printer_id=printer_id, **kw)
    else:
        result = verify_gcode(gcode_path, **kw)
    if isinstance(job, dict) and isinstance(facts_state, dict):
        try:
            job['gcode_full_verified_sha256'] = _full_verified_marker(GcodeArtifact.of(gcode_path).sha256, facts_state)
        except OSError:
            pass
    return result


def sovol_style_start_matches(text, bed, hotend, lines=None):
    """Whether a START_PRINT-led job (Sovol stock style, issue #612) proves its temperatures.

    Sovol's own profiles lead with START_PRINT, not the PRINT_START BED/HOTEND
    form Print Handy's Klipper templates emit. Two shapes prove the job's
    temperatures, and only these:

    - START_PRINT carrying BED_TEMP=<bed> EXTRUDER_TEMP=<hotend> arguments, or
    - a bare START_PRINT whose first M190 (bed wait) and first M109 (nozzle
      wait) lines carry the temperatures — the shape the Sovol Zero profiles
      emit, where the macro takes no temperature arguments at all.

    A START_PRINT that carries temperature arguments which disagree never
    passes on its heater lines: the macro would run with its arguments, so
    the arguments are the contract the file states.
    """
    if lines is None:
        from ..core.gcode_lines import GcodeLines
        lines = GcodeLines(text, ('START_PRINT', 'M190', 'M109'))
    m = lines.search(r'^START_PRINT\b\s*([^;]*)', 'START_PRINT')
    if not m:
        return False
    args = dict(re.findall(r'([A-Z_]+)=([+-]?(?:\d+(?:\.\d*)?|\.\d+))', m.group(1)))
    if 'BED_TEMP' in args or 'EXTRUDER_TEMP' in args:
        try:
            return (math.isclose(float(args.get('BED_TEMP', 'nan')), float(bed), abs_tol=1e-3) and
                    math.isclose(float(args.get('EXTRUDER_TEMP', 'nan')), float(hotend), abs_tol=1e-3))
        except ValueError:
            return False

    def first_temp(command):
        match = lines.search(rf'^{command}\s+S([+-]?(?:\d+(?:\.\d*)?|\.\d+))', command)
        return float(match.group(1)) if match else None

    bed_line, hotend_line = first_temp('M190'), first_temp('M109')
    return (bed_line is not None and hotend_line is not None and
            math.isclose(bed_line, float(bed), abs_tol=1e-3) and
            math.isclose(hotend_line, float(hotend), abs_tol=1e-3))
