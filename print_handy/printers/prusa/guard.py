"""Offline checks that keep foreign start code and unsupported jobs away from Prusa hardware.

Pure text checks, in the bambulab/guard.py spirit but for Prusa's Marlin-family dialect: the
slicer owns the start/end sequences (from the verified generated templates), so this guard does
not match vendor blocks line by line. It refuses what must never reach a Prusa printer — Klipper
or ZMod macro start code, a file sliced for a different printer model, temperatures above the
model's published limits, tool changes the model cannot make — the single-tool models
refuse any T1+ line, while the 5-tool XL (a multi_tool model, #670) accepts T0-T4 and refuses
a tool beyond its toolheads — and toolpath motion outside the model's bed, motion envelope
and height: a standalone or legacy file reaches this guard without passing through slicing,
so the guard runs the shared modal validator (slice_guard.toolpath_motion_problems) itself.
"""
from __future__ import annotations

import math
import re

from .. import job_checks

# Commands that belong to other vendors' start code. Prusa output is plain G/M/T codes plus
# Prusa's own M8xx extensions; none of these words appear in it.
FOREIGN_MARKERS = ("PRINT_START", "START_PRINT", "CHANGE_ZCOLOR", "LOAD_ZOFFSET", "BED_MESH_CALIBRATE",
                   "BED_MESH_PROFILE", "SAVE_VARIABLE", "project_file")


def _config(text, key):
    match = re.search(rf"^; {re.escape(key)} = (.*)$", text, re.M)
    return match.group(1).strip().strip('"') if match else None


def _numbers(text, command):
    return [float(v) for v in re.findall(rf"^[ \t]*{command}\b[^;\n]*?\bS(-?\d+(?:\.\d+)?)", text, re.M)]


def gcode_problems(text, adapter, state=None):
    fact = adapter.fact
    name = adapter.label
    problems = []
    for marker in FOREIGN_MARKERS:
        if re.search(rf"^[ \t]*{marker}\b", text, re.M):
            problems.append(f"{marker} is another vendor's start code; it is never sent to a {name}")
    model = _config(text, "printer_model")
    if model is not None and model != fact("printer_model"):
        problems.append(f"file was sliced for {model!r}, not the {name} ({fact('printer_model')!r})")
    used_tools = sorted({int(n) for n in re.findall(r"^[ \t]*T(\d+)\b", text, re.M)})
    if getattr(adapter, "multi_tool", False):
        count = int(getattr(adapter, "tool_count", 1))
        over = [t for t in used_tools if t >= count]
        if over:
            problems.append(f"G-code changes to tools {over}; the {name} has {count} toolheads "
                            f"(T0-T{count - 1})")
    else:
        tools = [t for t in used_tools if t != 0]
        if tools:
            problems.append(f"G-code changes to tools {tools}; {name} jobs are sliced single-tool only")
    hotend_max, bed_max = fact("max_hotend_c"), fact("max_bed_c")
    for command, limit in (("M104", hotend_max), ("M109", hotend_max), ("M140", bed_max), ("M190", bed_max)):
        over = [v for v in _numbers(text, command) if v > limit]
        if over:
            problems.append(f"{command} S{max(over):g} exceeds the {name} limit of {limit} C")
    nozzle = _config(text, "nozzle_diameter")
    if nozzle is not None:
        # The slicer serialises per-extruder vectors comma-joined: a multi-tool file
        # carries one value per toolhead, a single-tool file the scalar form. Both
        # are accepted; every listed value must be a supported size.
        try:
            nozzle_values = [float(v) for v in nozzle.split(",")]
        except ValueError:
            problems.append(f"invalid embedded nozzle_diameter {nozzle!r}")
        else:
            for nozzle_mm in nozzle_values:
                if not any(math.isclose(nozzle_mm, s, abs_tol=1e-3) for s in adapter.supported_nozzle_sizes):
                    problems.append(f"nozzle {nozzle_mm:g} mm has no {name} template here (supported: "
                                    f"{', '.join(f'{s:g}' for s in adapter.supported_nozzle_sizes)})")
    # Motion bounds (P1-63): the toolpath is held to the model's bed, motion envelope and
    # height by the shared modal validator, exactly as the Bambu guard holds its files.
    # Standalone and legacy files reach this guard without passing through slicing, so
    # the check runs here, before the file can be accepted for upload; a file whose
    # toolpath cannot be located is refused because its bounds cannot be checked.
    from ...core import slice_guard
    problems += slice_guard.toolpath_motion_problems(
        text, fact("bed_mm"), fact("motion_envelope_mm"), fact("max_print_height_mm"),
        fact("travel_z_headroom_mm"), exclusions=fact("bed_exclude_area"))
    # The loaded material, installed nozzle and heater targets (job_checks, shared with the other guards).
    problems += job_checks.recorded_job_problems(text, adapter, state)
    return problems
