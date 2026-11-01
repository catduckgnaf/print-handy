"""Offline deterministic checks around Orca slicing; no printer or slicer calls."""
from __future__ import annotations
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings

@dataclass
class Result:
    status: str = "ok"
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    effective_settings: dict = field(default_factory=dict)
    @property
    def ok(self): return self.status == "ok"
    def warn(self, text):
        self.warnings.append(text)
        if self.status == "ok": self.status = "warnings"
    def block(self, text): self.blocked.append(text); self.status = "blocked"
    def as_dict(self):
        return {"status": self.status, "ok": self.ok, "warnings": self.warnings,
                "blocked": self.blocked, "effective_settings": self.effective_settings}

def _printer(bed, filament_slots):
    printer = settings.current()['printer']
    return tuple(bed or printer['bed_mm']), int(filament_slots or printer['filament_slots'])

def pre_slice(*, bounds=None, bed=None, profile=None, required_keys=(), before_profile=None,
              filaments=1, filament_slots=None, tool_slots=None, nozzle_type=None, materials=None,
              nozzle_temp=None, nozzle_temp_is_effective=False):
    """bounds is placed XY bbox (xmin,ymin,xmax,ymax); tools map T-index to 1-based IFS slot."""
    r=Result(); profile=profile or {}; tool_slots=tool_slots or {}; bed,filament_slots=_printer(bed,filament_slots)
    if nozzle_type:
        from .nozzle import check_nozzle_material_safety, AbrasiveNozzleError, check_nozzle_temperature_safety, NozzleError
        mats = [materials] if isinstance(materials, str) else (materials or [])
        for mat in mats:
            try:
                check_nozzle_material_safety(nozzle_type, mat)
            except AbrasiveNozzleError as exc:
                r.block(str(exc))
        if nozzle_temp is not None:
            try:
                if nozzle_temp_is_effective:
                    check_nozzle_temperature_safety(nozzle_type, nozzle_temp)
                else:
                    from .nozzle import effective_nozzle_temperature
                    effective_nozzle_temperature(nozzle_type, nozzle_temp)
            except NozzleError as exc:
                r.block(str(exc))
    missing=[k for k in required_keys if k not in profile or profile[k] in (None, "", [])]
    if missing: r.block("Required slicer profile settings are missing: "+", ".join(missing)+".")
    if before_profile is not None:
        protected={"machine_start_gcode","machine_end_gcode","post_process","print_host","print_host_webui","host_type","printer_agent","printer_technology"}
        protected.update(k for k in set(before_profile)|set(profile) if k.startswith("printhost_") or (k.endswith("_gcode") and k!="emit_machine_limits_to_gcode"))
        changed=sorted(k for k in protected if before_profile.get(k)!=profile.get(k))
        if changed: r.block("Bot attempted to change protected printer behavior/settings: "+", ".join(changed)+"; edit these only in Orca GUI.")
    if bounds is None:
        r.warn("Placed mesh bounds are unavailable; bed-fit validation is advisory only.")
    if bounds is not None:
        x0,y0,x1,y1=map(float,bounds)
        if x0<0 or y0<0 or x1>bed[0] or y1>bed[1]: r.block(f"Placed model bounds ({x0:g}..{x1:g} x, {y0:g}..{y1:g} mm) exceed the {bed[0]:g}x{bed[1]:g} mm bed.")
    if int(filaments)>int(filament_slots): r.block(f"Profile has {filaments} filaments but only {filament_slots} IFS slots are available.")
    if int(filaments)>1 and not tool_slots: r.block("Multi-filament job has no explicit T-index to physical IFS-slot mapping.")
    for t,slot in tool_slots.items():
        try: ti,si=int(str(t).lstrip('T')),int(slot)
        except (ValueError,TypeError): r.block(f"Invalid tool-to-slot mapping {t!r} → {slot!r}."); continue
        if ti<0 or si<1 or si>filament_slots: r.block(f"T{ti} maps to IFS slot {si}; valid slots are 1 through {filament_slots}.")
    if tool_slots and len(tool_slots)!=int(filaments): r.block("Explicit T-index mapping count does not match the profile filament count.")
    return r

# Prusa style "; estimated printing time (normal mode) = 1h 2m" and Orca/Bambu style
# "; model printing time: 1h 2m; total estimated time: 1h 5m", plus older "; TIME:123".
TIME_HEADER=re.compile(r"(?im)^;\s*(?:estimated printing time(?:\s*\([^)]*\))?|estimated time|printing time)\s*=\s*\S"
                       r"|^;.*\b(?:model printing time|total estimated time)\s*:\s*\S|^;\s*time\s*:\s*\S")
MOVE=re.compile(r"^\s*G[0-3]\b([^;]*)",re.I)
WORD=re.compile(r"\b([XYZE])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))",re.I)
HERMES_SLOT_PATTERN = re.compile(r"^Hermes slot \d+(?:\s+\S.*)?$", re.I)
# Per-line patterns, compiled once: a large plate runs to 10M+ lines.
OPCODE = re.compile(r"^([GMT])0*(\d)")
WORD_TOKEN = re.compile(r"([A-Z])(-?(?:\d+(?:\.\d*)?|\.\d+))")
BRIM_TYPE = re.compile(r"^;\s*brim_type\s*=\s*(.*)$", re.I)
Z_HOP_TYPES = re.compile(r"^;\s*(filament_z_hop_types|z_hop_types)\s*=\s*(.*)$", re.I)
SETTINGS_ID = re.compile(r"^;\s*((?:filament|printer|print)_settings_id)\s*=\s*(.*)$", re.I)
ARC_FITTING = re.compile(r"^;\s*enable_arc_fitting\s*=\s*(.*)$", re.I)
EFFECTIVE_SETTING = re.compile(
    r"^;\s*(filament_z_hop_types|z_hop_types|filament_wipe|"
    r"filament_wipe_distance|filament_settings_id|printer_settings_id|"
    r"print_settings_id|brim_type)\s*=\s*(.*)$", re.I)


def is_allowed_filament_id(value, own_ids=None):
    """Check if a filament profile ID is an allowed template ID or Print Handy generated Hermes slot."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip().strip('"')
    if own_ids and v in own_ids:
        return True
    return bool(HERMES_SLOT_PATTERN.match(v))


# Slicer identity, read from what each supported slicer actually writes (#434). OrcaSlicer and
# Flash Studio open the file with a "; generated by <name> <version>" line. Bambu Studio never
# writes one: its identity is the header block, "; HEADER_BLOCK_START" followed immediately by
# "; BambuStudio <version>". A "; generated by BambuStudio" line is not a Bambu Studio identity
# (no released Bambu Studio writes it; the offline suites once fabricated it, which hid #434), so
# it deliberately does not match here.
_GENERATED_BY = re.compile(r"^; generated by ((?:OrcaSlicer|Flash Studio).+)$", re.M)
_BAMBU_HEADER = re.compile(r"^; HEADER_BLOCK_START[ \t]*\n; (BambuStudio [^\n]+)$", re.M)


def generator_identity(text):
    """The producing slicer's identity ("OrcaSlicer …", "BambuStudio …", "Flash Studio …"), or None.

    Every consumer that checks who produced a G-code file (the adapter's post-slice gate, the
    Bambu Lab guard) reads identity through here, so the accepted forms cannot drift apart.
    """
    if not text:
        return None
    generated = _GENERATED_BY.search(text)
    if generated:
        return generated.group(1).strip()
    header = _BAMBU_HEADER.search(text)
    if header:
        return header.group(1).strip()
    return None


# Orca 2.5+ tags the end of the machine start G-code and the start of the end G-code. Orca 2.4 does not:
# there the first layer change opens the toolpath and the last "Custom" role tag (written just before the
# filament and machine end G-code) closes it. Bambu Lab profiles use "; CHANGE_LAYER" and "; FEATURE: ".
# These are comments, so they only split the file: a printer that trusts its vendor blocks must also check
# them against its verified templates (printers.bambulab.guard). A spoofed or duplicated marker then moves vendor
# lines into the checked toolpath, or toolpath lines into a template check, and is refused either way.
EXEC_START = re.compile(r"^; EXECUTABLE_BLOCK_START\s*$", re.M)
EXEC_END = re.compile(r"^; EXECUTABLE_BLOCK_END\s*$", re.M)
START_TAG = re.compile(r"^; MACHINE_START_GCODE_END\s*$", re.M)
END_TAG = re.compile(r"^; MACHINE_END_GCODE_START\s*$", re.M)
LAYER_TAG = re.compile(r"^;(?: CHANGE_LAYER|LAYER_CHANGE)\s*$", re.M)
CUSTOM_TAG = re.compile(r"^;(?: FEATURE: |TYPE:)Custom\s*$", re.M)

def split_toolpath(text):
    """(prelude, toolpath, epilogue) of the executable G-code, or None when the boundaries cannot be found.

    prelude is everything executable before the toolpath (machine and filament start G-code and Orca's own
    preamble), epilogue everything after it (filament and machine end G-code).
    """
    first = EXEC_START.search(text)
    last = EXEC_END.search(text, first.end() if first else 0)
    exe = text[first.end() if first else 0:last.start() if last else len(text)]
    start = START_TAG.search(exe) or LAYER_TAG.search(exe)
    if start is None:
        return None
    rest = exe[start.end():]
    end = END_TAG.search(rest)
    if end is None:
        ends = list(CUSTOM_TAG.finditer(rest))
        end = ends[-1] if ends else None
    if end is None:
        return None
    return exe[:start.start()], rest[:end.start()], rest[end.start():]

def toolpath(text):
    """Orca's own toolpath, without the machine start and end G-code; None when it cannot be located."""
    split = split_toolpath(text)
    return split[1] if split else None

def _mm(value):
    return "?" if value is None else f"{value:g}"

# G-codes allowed in a checked toolpath. Moves, modes (G17 selects the XY arc plane and moves nothing),
# E-only G92, dwell, units, Bambu clog detection (G392) and the in-print nozzle clump and head-wrap probes
# (G39, G39.3), which leave Z unknown. Anything else (homing, probing, coordinate resets) is refused there
# rather than guessed. G2/G3 arcs are not in this set: they are admitted only through the verified
# non-extruding form in toolpath_motion_problems below.
TOOLPATH_GCODES = {"G0", "G1", "G4", "G17", "G21", "G90", "G91", "G92", "G39", "G39.3", "G392"}
# G-codes that move an axis without saying where it ends up.
UNKNOWN_AFTER = {"G28": "XYZ", "G29": "XYZ", "G29.2": "", "G380": "Z", "G39": "Z", "G39.3": "Z", "G39.4": "Z"}

class Motion:
    """Modal G-code state: positioning (G90/G91), extruder mode (M82/M83, reset by G90/G91), G92 and position.

    Unknown is a value: a position after homing or probing, a relative move from an unknown position, an
    extruder mode not set since the last G90/G91. Checks treat unknown as unsafe where it matters.
    """

    def __init__(self):
        self.pos = {"X": None, "Y": None, "Z": None, "E": None}
        self.relative = None  # None: never set
        self.e_relative = None

    def forget(self, axes="XYZ"):
        for axis in axes:
            self.pos[axis] = None

    def step(self, line):
        """Apply one line. Returns (opcode, words, target position, extrusion delta) for moves, else (opcode, words, None, None)."""
        code = line.split(";", 1)[0].strip().upper()
        if not code:
            return None, {}, None, None
        parts = code.split()
        op = OPCODE.sub(r"\1\2", parts[0])
        words = {}
        for part in parts[1:]:
            m = WORD_TOKEN.fullmatch(part)
            if m:
                words[m.group(1)] = float(m.group(2))
        if op in ("G90", "G91"):
            self.relative = op == "G91"
            self.e_relative = None  # Marlin moves E with G90/G91; unknown until the next M82/M83
        elif op in ("M82", "M83"):
            self.e_relative = op == "M83"
        elif op == "G92":
            for axis in "XYZE":
                if axis in words:
                    self.pos[axis] = words[axis]
            if not words:
                self.forget("XYZE")
        elif op in UNKNOWN_AFTER:
            given = [a for a in "XYZ" if a in words and op == "G28"]
            self.forget("".join(given) or UNKNOWN_AFTER[op])
        elif op in ("G0", "G1", "G2", "G3"):
            target = dict(self.pos)
            for axis in "XYZ":
                if axis in words:
                    if self.relative is None:
                        target[axis] = None
                    elif self.relative:
                        target[axis] = None if self.pos[axis] is None else self.pos[axis] + words[axis]
                    else:
                        target[axis] = words[axis]
            delta = None
            if "E" in words:
                if self.e_relative:
                    delta = words["E"]
                    target["E"] = None if self.pos["E"] is None else self.pos["E"] + words["E"]
                elif self.e_relative is False:
                    delta = None if self.pos["E"] is None else words["E"] - self.pos["E"]
                    target["E"] = words["E"]
                else:
                    target["E"] = None
            else:
                delta = 0.0
            self.pos = target
            return op, words, target, delta
        return op, words, None, None

def _toolpath_arc_problem(words, target, start, envelope, height, z_headroom):
    """Why a toolpath G2/G3 cannot be verified; None when it is a verifiable non-extruding arc.

    Bambu Studio ends its toolpath with a helical Z lift — "G17" then "G3 Z<z> I<x> J<y> P1" — which
    OrcaSlicer does not emit (#434). An arc is admitted only in the form whose whole path can be
    bounded without guessing: no extrusion (where plastic lands along a curve is not endpoint
    geometry), a fully known start, an I/J centre offset (the R form is ambiguous), the swept
    circle's whole bounding box inside the envelope (conservative: every point of every turn lies on
    that circle), and the helical Z range inside 0..height + headroom. Anything else is refused.
    """
    if start is None or target is None or any(start[a] is None or target[a] is None for a in "XYZ"):
        return "Arc from an unknown position or positioning mode in the toolpath"
    if "E" in words:
        return "Extruding arc in the toolpath; arc extrusion cannot be verified"
    if "I" not in words and "J" not in words:
        return "Arc without a centre offset (I/J) in the toolpath; its path cannot be verified"
    radius = math.hypot(words.get("I", 0.0), words.get("J", 0.0))
    centre_x, centre_y = start["X"] + words.get("I", 0.0), start["Y"] + words.get("J", 0.0)
    if not (envelope[0] <= centre_x - radius and centre_x + radius <= envelope[2]
            and envelope[1] <= centre_y - radius and centre_y + radius <= envelope[3]):
        return "Arc path leaves the machine envelope"
    z_low, z_high = min(start["Z"], target["Z"]), max(start["Z"], target["Z"])
    if z_low < 0:
        return f"Arc move to Z{target['Z']:g} is below 0 mm"
    if height is not None and z_high > height + z_headroom:
        return f"Arc move to Z{target['Z']:g} is outside 0..{height + z_headroom:g} mm"
    return None


def toolpath_motion_problems(text, bed, envelope, height=None, z_headroom=1.0):
    """Motion checks for G-code whose vendor start/end G-code parks the head off the bed (Bambu wiper, purge lane).

    Modal state runs through the whole file. In the prelude and epilogue every move with a known XY position must
    stay inside envelope (xmin, ymin, xmax, ymax). In the toolpath (see split_toolpath) every move must have a known
    start for each axis it moves relatively, stay inside the envelope, keep Z between 0 and height + z_headroom,
    and every possibly extruding move must be at a known position on the bed at 0 < Z <= height. An E move whose
    extruder mode is unknown counts as extruding. Homing, probing and XYZ coordinate resets are refused there;
    arcs are refused too, except the verified non-extruding form (see _toolpath_arc_problem).
    """
    split = split_toolpath(text)
    if split is None:
        return ["Cannot locate Orca's toolpath (no layer change or end G-code marker); bounds cannot be checked."]
    problems = []
    motion = Motion()

    def outside(x, y):
        return (x is not None and not envelope[0] <= x <= envelope[2]) or (y is not None and not envelope[1] <= y <= envelope[3])

    for part, lines in (("prelude", split[0]), ("toolpath", split[1]), ("epilogue", split[2])):
        if part == "toolpath":
            # The vendor start runs firmware-conditional blocks; never carry a position into the toolpath.
            motion.forget("XYZ")
        for number, line in enumerate(lines.splitlines(), 1):
            # An arc's centre is relative to where the head is before the move; step() advances
            # the modal position, so snapshot the start of a toolpath arc first.
            arc_start = None
            if part == "toolpath":
                tokens = line.split(";", 1)[0].split()
                if tokens and OPCODE.sub(r"\1\2", tokens[0].upper()) in ("G2", "G3"):
                    arc_start = dict(motion.pos)
            op, words, target, delta = motion.step(line)
            where = f"{part} line {number}: {line.split(';', 1)[0].strip()[:60]}"
            if op is None:
                continue
            if part == "toolpath" and op in ("G2", "G3"):
                problem = _toolpath_arc_problem(words, target, arc_start, envelope, height, z_headroom)
                if problem:
                    problems.append(f"{problem} ({where}).")
                continue
            if part == "toolpath" and op.startswith("G") and op not in TOOLPATH_GCODES:
                problems.append(f"Unsupported command in the toolpath ({where}).")
                continue
            if part == "toolpath" and op == "G92" and any(a in words for a in "XYZ"):
                problems.append(f"Coordinate reset in the toolpath ({where}).")
                continue
            if target is None:
                continue
            if outside(target["X"], target["Y"]):
                problems.append(f"G-code move leaves the machine envelope at X{_mm(target['X'])} Y{_mm(target['Y'])} ({where}).")
                continue
            if part != "toolpath":
                continue
            moved = [a for a in "XYZ" if a in words]
            if any(target[a] is None for a in moved):
                problems.append(f"Move from an unknown position or positioning mode ({where}).")
                continue
            z = target["Z"]
            if "Z" in words and (z < 0 or (height is not None and z > height + z_headroom)):
                problems.append(f"G-code move to Z{z:g} is outside 0..{height + z_headroom:g} mm ({where}).")
                continue
            extruding = ("E" in words and words["E"] != 0) if delta is None else delta > 0
            if extruding:
                if any(target[a] is None for a in "XYZ"):
                    problems.append(f"Extrusion at an unknown position ({where}).")
                elif not (0 <= target["X"] <= bed[0] and 0 <= target["Y"] <= bed[1]
                          and 0 < z and (height is None or z <= height)):
                    problems.append(f"G-code extrudes off the {bed[0]:g}x{bed[1]:g} mm bed at "
                                    f"X{target['X']:g} Y{target['Y']:g} Z{z:g} ({where}).")
    return problems[:10]

def post_slice(gcode, *, filament_slots=None, tool_slots=None, bed=None, result_json=None,
               templates=None, profile_paths=(), envelope=None):
    """Check bounds + effective motion/profile settings and capture provenance.

    envelope (xmin, ymin, xmax, ymax) is for printers whose vendor start/end G-code moves off the bed:
    only Orca's toolpath (see split_toolpath) is held to the bed and tool-checked (toolpath_motion_problems).
    """
    r=Result(); tool_slots=tool_slots or {}; bed,filament_slots=_printer(bed,filament_slots)
    if isinstance(gcode, Path):
        text=gcode.read_text(errors="replace")
    elif isinstance(gcode, str) and "\\n" not in gcode and len(gcode)<4096 and Path(gcode).is_file():
        text=Path(gcode).read_text(errors="replace")
    else:
        text=str(gcode or "")
    low=text.lower()
    if not text.strip(): r.block("Orca produced no G-code; inspect its return code and error log."); return r
    # Auto can resolve to Spiral on overhang-crossing travels in Orca; permit only Normal Lift.
    template_root = Path(templates or settings.current()['orca']['templates'])
    own_settings = {}
    for filename in ('machine.json', 'process.json', 'filament.json'):
        path = template_root / filename
        if path.is_file():
            own_settings.update(json.loads(path.read_text()))
    own_filament_ids = set()
    for path in (Path(p) for p in profile_paths):
        if path.is_file():
            data = json.loads(path.read_text())
            values = data.get('filament_settings_id', [])
            own_filament_ids.update(str(value).strip().strip('"') for value in (values if isinstance(values, list) else [values]))
    if not own_filament_ids:
        values = own_settings.get('filament_settings_id', [])
        own_filament_ids.update(str(value).strip().strip('"') for value in (values if isinstance(values, list) else [values]))
    if re.search(r"(?m)^;\s*filament_settings_id\s*=", text, re.I) and not own_filament_ids:
        r.block("Cannot verify filament_settings_id slots: no active template filament IDs are available.")
    # Every setting pattern below is anchored at a leading ';', so only comment lines can match.
    comments = [line for line in text.splitlines() if line.startswith(';')]
    for line in comments:
        match_brim = BRIM_TYPE.match(line)
        if match_brim:
            val = match_brim.group(1).strip().strip('"')
            allowed_brim = own_settings.get('brim_type')
            # Turning the brim off is Print Handy's own choice (fit policy, flex prints), never foreign.
            if allowed_brim is not None and val.lower() not in (str(allowed_brim).strip().lower(), 'no_brim'):
                r.block(f"Foreign effective setting brim_type: {val!r}; expected {allowed_brim!r}.")
        match = Z_HOP_TYPES.match(line)
        if match:
            key, value = match.group(1), match.group(2)
            for slot, entry in enumerate(value.split(','), 1):
                raw = entry.strip().strip('"')
                if not raw:
                    continue
                if raw.lower() != 'normal lift':
                    r.block(f"Unsafe effective setting {key} slot {slot}: {raw!r}; only Normal Lift is permitted.")
        match = SETTINGS_ID.match(line)
        if match:
            key = match.group(1).lower()
            entries = match.group(2).split(';') if key == 'filament_settings_id' else [match.group(2)]
            raw_ids = own_filament_ids if key == 'filament_settings_id' else own_settings.get(key, [])
            own_ids = {str(value).strip().strip('"') for value in (raw_ids if isinstance(raw_ids, (list, set, tuple)) else [raw_ids])}
            for slot, entry in enumerate(entries, 1):
                value = entry.strip().strip('"')
                is_allowed = is_allowed_filament_id(value, own_ids) if key == 'filament_settings_id' else (value in own_ids)
                if not value or not is_allowed:
                    r.block(f"Foreign effective setting {match.group(1)} slot {slot}: {value!r}.")
        match = ARC_FITTING.match(line)
        if match and match.group(1).strip() == '1':
            r.block("Unsafe effective setting enable_arc_fitting=1.")
    if "g-code conflicts detected" in low or "slicing result conflict" in low or "gcode path conflicts" in low:
        r.block("Orca reported a G-code conflict; the slice cannot be used. Inspect overlapping geometry/wipe-tower placement (see Orca adapter diagnostic).")
    # With an envelope, the vendor start/end blocks are the printer maker's (Bambu uses T1000 and T255 there);
    # tools are counted and bounds held to the bed on Orca's toolpath only (toolpath_motion_problems).
    body = text
    if envelope is not None:
        body = toolpath(text)
        if body is None:
            body = text
    tools={int(n) for n in re.findall(r"(?im)^\s*T(\d+)\s*(?:;.*)?$",body)}
    for n in sorted(tools):
        if n not in tool_slots: r.block(f"G-code uses T{n}, but no physical IFS slot mapping is recorded.")
        elif not 1<=int(tool_slots[n])<=filament_slots: r.block(f"T{n} maps outside the available IFS slots.")
    if tools and max(tools)>=filament_slots: r.block(f"G-code uses tool T{max(tools)} but only {filament_slots} IFS slots are available.")
    coords=[]; x=y=0.0
    if envelope is not None:
        for problem in toolpath_motion_problems(text, bed, envelope):
            r.block(problem)
    for line in (text.splitlines() if envelope is None else ()):
        m=MOVE.match(line)
        if not m: continue
        vals={k.upper():float(v) for k,v in WORD.findall(m.group(1))}
        x=vals.get('X',x); y=vals.get('Y',y)
        if 'X' in vals or 'Y' in vals:
            coords.append((x,y))
            if x<0 or y<0 or x>bed[0] or y>bed[1]: r.block(f"G-code move goes off the {bed[0]:g}x{bed[1]:g} mm bed at X{x:g} Y{y:g}."); break
    effective = {}
    for line in comments:
        match = EFFECTIVE_SETTING.match(line)
        if match:
            effective[match.group(1)] = match.group(2).strip()
    slot_details = []
    per_slot = {key: value.split(',') for key, value in effective.items()
                if key in ('filament_z_hop_types', 'filament_wipe', 'filament_wipe_distance')}
    ids = effective.get('filament_settings_id', '').split(';')
    for index in range(max([len(v) for v in per_slot.values()] + [len(ids)])):
        item = {'slot': index + 1}
        for key, values in per_slot.items():
            if index < len(values):
                item[key] = values[index].strip()
        if index < len(ids):
            item['filament_settings_id'] = ids[index].strip().strip('"')
        slot_details.append(item)
    effective['filament_slots'] = slot_details
    r.effective_settings = effective
    if not TIME_HEADER.search(text):
        r.warn("G-code header has no estimated print-time metadata; estimate is unavailable offline.")
    if result_json:
        try:
            data=json.loads(Path(result_json).read_text() if isinstance(result_json,(str,Path)) else result_json)
            if int(data.get("return_code",0))!=0 or data.get("error_string","").strip().lower() not in ("","success.","success"):
                msg=str(data.get("error_string") or f"Orca returned code {data.get('return_code')}.")
                if "-101" in msg or "conflict" in msg.lower(): r.block("Orca slice failed with a G-code conflict (-101); inspect the Orca conflict diagnostic.")
                else: r.block("Orca did not complete the slice: "+msg)
        except (OSError,ValueError,TypeError) as e: r.warn(f"Could not parse Orca result metadata: {e}.")
    return r

def classify_orca_failure(text, return_code=None):
    s=(text or "").lower()
    if "conflict" in s or return_code==-101: return "gcode_conflict"
    if "not precalculated placeable areas" in s or return_code==-101: return "orca_geometry_or_conflict"
    if "param values" in s or return_code==-18: return "invalid_3mf_profile_value"
    if return_code in (-50,-24): return "orca_cli_failure"
    return "unknown_orca_failure"


PROTECTED_KEYS = ("machine_start_gcode", "machine_end_gcode", "post_process", "print_host", "print_host_webui", "host_type", "printer_agent", "printer_technology")
BASELINE_PATH = settings.ROOT / "config" / "protected-settings-baseline.json"

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _effective_config(artifact):
    # Orca's embedded config is authoritative; G-code header includes the
    # same key/value settings emitted as comments.
    if isinstance(artifact, dict): return artifact
    text = Path(artifact).read_text(errors="replace") if isinstance(artifact, (str, Path)) and Path(artifact).is_file() else str(artifact or "")
    found = {}
    for line in text.splitlines():
        m = re.match(r"^;\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) in PROTECTED_KEYS: found[m.group(1)] = m.group(2)
    return found

def _normalize(key, value):
    # The bed mesh name in START_PRINT is chosen per job by orca_adapter from the
    # validated calibration state (MESH_DATA when calibrated, auto otherwise).
    # It is not a hand edited printer setting, so it must not trip the guard.
    if key == "machine_start_gcode" and isinstance(value, str):
        return re.sub(r"\bMESH=[A-Za-z0-9_]+", "MESH=<mesh>", value)
    return value

def _baseline(config):
    import hashlib
    out = {}
    for k in PROTECTED_KEYS:
        if k not in config: continue
        v = _normalize(k, config.get(k))
        out[k] = {"sha256": hashlib.sha256(_canonical(v).encode()).hexdigest(),
                  **({"value": v} if len(_canonical(v)) <= 160 else {})}
    return out

def approve_baseline(config, path=BASELINE_PATH):
    current = _baseline(_effective_config(config))
    previous = json.loads(Path(path).read_text()) if Path(path).exists() else {}
    for key in sorted(set(previous) | set(current)):
        if previous.get(key) != current.get(key): print(f"{key}: {previous.get(key, '<missing>')} -> {current.get(key, '<missing>')}")
    Path(path).write_text(json.dumps(current, indent=2, sort_keys=True) + chr(10))
    return current

def _approve_command(path):
    command = "python3 -m print_handy.core.approve_baseline approve-baseline <artifact>"
    if Path(path).resolve() != BASELINE_PATH.resolve():
        command += f" --baseline config/{Path(path).name}"
    return command

def enforce_approved_baseline(artifact, path=BASELINE_PATH):
    r = Result()
    if not Path(path).exists():
        r.warn(f"Protected-settings baseline missing; slicing allowed. Approve with: {_approve_command(path)}")
        return r
    if not isinstance(artifact, (str, Path)) or not Path(artifact).is_file():
        r.warn("Protected-settings artifact unavailable; slicing allowed pending baseline check.")
        return r
    expected = json.loads(Path(path).read_text())
    actual = _effective_config(artifact)
    for key, record in expected.items():
        if key not in actual or _baseline({key: actual[key]}).get(key, {}).get("sha256") != record.get("sha256"):
            r.block(f"Protected printer setting {key} differs from the approved baseline. Re-approve deliberately with: {_approve_command(path)}")
    return r
