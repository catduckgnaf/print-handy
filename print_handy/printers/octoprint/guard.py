"""Offline checks that keep foreign start code and unsupported jobs away from OctoPrint hardware.

Pure text checks, in the prusa/guard.py spirit and for the same Marlin dialect: the slicer
owns the start/end sequences (plain Marlin G/M/T codes from the pinned templates), so this
guard does not match vendor blocks line by line. It refuses what must never reach one of
these printers — Klipper or ZMod macro start code Marlin cannot execute, a file sliced for
a different printer model, tool changes on a single-tool machine, temperatures above the
model's declared limits — and toolpath motion outside the model's bed and height: a
standalone or legacy file reaches this guard without passing through slicing, so the guard
runs the shared modal validator (slice_guard.toolpath_motion_problems) itself, the way the
Prusa guard does.

The facts come from the model's own declarations, read through adapter.fact(): bed and
height from the model settings, the expected printer_model from the model's pinned Orca
template, the nozzle sizes from the model record. No OctoPrint model declares temperature
limits, so the temperature check skips for them: an undeclared limit stays unknown and is
never invented here. A model (or an operator config) that does declare max_hotend_c /
max_bed_c gets the check against exactly those values.

The foreign-marker list is deliberately limited to commands Marlin cannot execute
(Klipper/ZMod macros and macro-adjacent words). Plain Marlin start code a person wrote by
hand — G28, G29, M104/M109/M140/M190 waits, purge moves — contains none of them and passes.
"""
from __future__ import annotations

import logging
import math
import re

from .. import job_checks

logger = logging.getLogger(__name__)

# Commands that belong to other vendors' start code. Marlin output is plain G/M/T codes;
# none of these words appear in it, and Marlin cannot execute any of them.
FOREIGN_MARKERS = ("PRINT_START", "START_PRINT", "CHANGE_ZCOLOR", "LOAD_ZOFFSET", "BED_MESH_CALIBRATE",
                   "BED_MESH_PROFILE", "SAVE_VARIABLE", "project_file")


def _config(text, key):
    match = re.search(rf"^; {re.escape(key)} = (.*)$", text, re.M)
    return match.group(1).strip().strip('"') if match else None


def _numbers(text, command):
    return [float(v) for v in re.findall(rf"^[ \t]*{command}\b[^;\n]*?\bS(-?\d+(?:\.\d+)?)", text, re.M)]


def _fact(adapter, name):
    """The adapter's fact for name, or None when the model declares none.

    Mirrors core.heater_ceiling._fact: a model without the fact has no such
    limit or identity to check against, and the check that needed it skips.
    """
    fact = getattr(adapter, "fact", None)
    if not callable(fact):
        return None
    try:
        value = fact(name)
    except Exception:  # a model without that fact declares nothing to check against
        logger.debug("printer fact %s unavailable", name, exc_info=True)
        return None
    return value


def gcode_problems(text, adapter, state=None):
    text = text or ""
    name = adapter.label
    problems = []
    for marker in FOREIGN_MARKERS:
        if re.search(rf"^[ \t]*{marker}\b", text, re.M):
            problems.append(f"{marker} is another vendor's start code; it is never sent to a {name}")
    expected_model = _fact(adapter, "printer_model")
    model = _config(text, "printer_model")
    if model is not None and expected_model is not None and model != expected_model:
        problems.append(f"file was sliced for {model!r}, not the {name} ({expected_model!r})")
    # Every OctoPrint model is single-tool: any T1+ line is a tool change the
    # machine cannot make. (A future multi-tool OctoPrint model would key this
    # on its tool count the way the Prusa XL does; none exists today.)
    used_tools = sorted({int(n) for n in re.findall(r"^[ \t]*T(\d+)\b", text, re.M)})
    tools = [t for t in used_tools if t != 0]
    if tools:
        problems.append(f"G-code changes to tools {tools}; {name} jobs are sliced single-tool only")
    # Temperature limits the model declares; undeclared limits skip (see the
    # module docstring) rather than being guessed.
    for command, limit in (("M104", _fact(adapter, "max_hotend_c")), ("M109", _fact(adapter, "max_hotend_c")),
                           ("M140", _fact(adapter, "max_bed_c")), ("M190", _fact(adapter, "max_bed_c"))):
        if limit is None:
            continue
        over = [v for v in _numbers(text, command) if v > limit]
        if over:
            problems.append(f"{command} S{max(over):g} exceeds the {name} limit of {limit:g} C")
    nozzle = _config(text, "nozzle_diameter")
    if nozzle is not None:
        # The slicer serialises per-extruder vectors comma-joined; a single-tool
        # file carries the scalar form. Every listed value must be a supported size.
        try:
            nozzle_values = [float(v) for v in nozzle.split(",")]
        except ValueError:
            problems.append(f"invalid embedded nozzle_diameter {nozzle!r}")
        else:
            for nozzle_mm in nozzle_values:
                if not any(math.isclose(nozzle_mm, s, abs_tol=1e-3) for s in adapter.supported_nozzle_sizes):
                    problems.append(f"nozzle {nozzle_mm:g} mm has no {name} template here (supported: "
                                    f"{', '.join(f'{s:g}' for s in adapter.supported_nozzle_sizes)})")
    # Motion bounds: the toolpath is held to the model's bed, motion envelope and
    # height by the shared modal validator, exactly as the Prusa guard holds its
    # files. Standalone and legacy files reach this guard without passing through
    # slicing, so the check runs here, before the file can be accepted for upload;
    # a file whose toolpath cannot be located is refused because its bounds
    # cannot be checked.
    bed, height = _fact(adapter, "bed_mm"), _fact(adapter, "max_print_height_mm")
    if bed is not None and height is not None:
        envelope = _fact(adapter, "motion_envelope_mm") or (0.0, 0.0, float(bed[0]), float(bed[1]))
        headroom = _fact(adapter, "travel_z_headroom_mm")
        from ...core import slice_guard
        problems += slice_guard.toolpath_motion_problems(
            text, bed, envelope, height, 1.0 if headroom is None else headroom,
            exclusions=_fact(adapter, "bed_exclude_area") or ())
    # The loaded material, installed nozzle and heater targets (job_checks, shared with the other guards).
    problems += job_checks.recorded_job_problems(text, adapter, state)
    return problems
