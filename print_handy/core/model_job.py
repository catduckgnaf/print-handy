#!/usr/bin/env python3
"""Fetch, inspect, and (only with --approved) slice Printables, MakerWorld or Thingiverse jobs,
plus direct file links to a .stl/.3mf on any public host (#607).

Normal path, one command:
  python3 -m print_handy.core.model_job fetch URL
It verifies current hardware against Klipper + Moonraker + Spoolman, inventories every
listed source profile/file, downloads only the selected source, audits the 3MF/STL, and
creates JOB.json. It DOES NOT slice, upload, or start a print.

After the operator selects a numbered source and explicitly approves slicing:
  python3 -m print_handy.core.model_job fetch URL --select N --approved
The baked site G-code is never used on this printer. Geometry is resliced against
the live-verified nozzle, loaded Spoolman filament, and configured motion limits. Upload and
print stay separate unless --print-now is given, which means the operator clicked
"bed clear" for exactly this job. `python3 -m print_handy.core.model_job print-now FILE|JOB.json` starts an
already sliced job the same way.
"""
import argparse, base64, contextvars, hashlib, html, http.client, ipaddress, json, logging, math, os, re, shutil, socket, subprocess, sys, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, NamedTuple

from .. import firmware_update, printers, settings
from ..printers.base import MultiColorUnsupported
from . import _external, filament_color, gcode_3mf, launch_state, printer_state, purge, spoolman_client, start_recorder
from . import start_truth
from .filament_warnings import FilamentWarningPending
from .site_accounts import SiteLoginNeeded
from .threemf_canonical import intake_working_copy

logger = logging.getLogger(__name__)

from ..launch.print_now import _read_start_readback


# ---------------------------------------------------------------------------
# Login-token web-API sites (#464): Thangs and Nexprint (MakerOnline's
# inventory shares the transport but is inventory-only; see below).
#
# Like Creality Cloud above, each of these uses the JSON API its own web app
# calls. Model detail and the file list answer anonymously; the download
# needs the operator's login token (see site_accounts), which the operator
# copies from their signed-in browser into ~/.config/<site>/token. No
# password is stored, no captcha is solved, and no site check is bypassed:
# a robot check or captcha answer is a plain stop. Verified live 2026-10-29
# end to end for Thangs and Nexprint with operator tokens (inventory, the
# tokenless refusal, and a real download); answers are still parsed
# defensively, refusing with a named gap instead of guessing.
# ---------------------------------------------------------------------------


from . import model_sources as _model_sources


from ..jobs.store import remote_name, write_job


from ..jobs.audit import audit


def format_color_title(c):
    return ' '.join(w.upper() if w.casefold() in {'pla', 'pla+', 'petg', 'petg-cf', 'abs', 'tpu'} else w.capitalize() for w in str(c or '').split())


def _build_position_labels(selections, noun):
    counts = {}
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        counts[name] = counts.get(name, 0) + 1
    labels = []
    for s in selections:
        c_title = format_color_title(s.get('color'))
        mat = printer_state.display_material(s.get('material', ''))
        name = c_title if mat and c_title.casefold().endswith(mat.casefold()) else f'{c_title} {mat}'.strip()
        if counts[name] > 1:
            labels.append(f'{name} ({noun} {s["slot"]})')
        else:
            labels.append(name)
    return labels


def build_slot_labels(selections):
    return _build_position_labels(selections, 'Slot')


def build_tool_labels(selections):
    """Choice labels for a multi_tool printer: positions are toolheads, never slots (#346)."""
    return _build_position_labels(selections, 'Toolhead')


from ..launch.gates import YOLO_WAIVED_MARK


def record_supports_question(folder, audit):
    """Record why the supports question was (or was not) shown (#1020 F3).

    ``audit`` carries the decision's reason and inputs plus the
    clearance report and the slicer finding the decision was made from,
    so a shown or hidden menu can be audited from JOB.json alone.
    """
    path = Path(folder) / 'JOB.json'
    job = json.loads(path.read_text())
    job['supports_question'] = {**audit, 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    write_job(folder, job)
    return job


def _fetch_remote_gcode_sha256(moonraker_url, remote_name, timeout=60):
    escaped = urllib.parse.quote(remote_name)
    url = f"{moonraker_url}/server/files/gcodes/{escaped}"
    hasher = hashlib.sha256()
    from .. import moonraker as _moonraker
    with _moonraker.open_url(url, timeout=timeout, moonraker=True) as resp:
        total_read = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            total_read += len(chunk)
            if total_read > 1024 * 1024 * 1024:
                raise RuntimeError(f"Remote file {remote_name} exceeds 1GB limit")
    return hasher.hexdigest()


# The Moonraker start truth (#1318): the baseline classifier and its helpers live in start_truth.
StartReadback = start_truth.StartReadback
classify_start_readback = start_truth.classify_start_readback
_same_job = start_truth._same_job


def _fmt_slots(slots):
    if not isinstance(slots,list) or not slots: return "unknown"
    def u(v): return v if v not in (None,"") else "unknown"
    def loaded(v): return "loaded" if v is True else "empty" if v is False else "presence unknown"
    return "; ".join(f"{u(s.get('slot'))}: {u(s.get('color'))} {u(s.get('material'))} ({loaded(s.get('loaded'))})" for s in slots)


def _native_file_matches(reported, remote):
    """Match only documented Klipper paths for this upload, including ZMod's 3MF unpack path."""
    return launch_state.same_remote(reported, remote)


def _reconcile_native_submission(st, remote):
    """Read status once after lost POST response; never attribute heaters alone to this job."""
    return _read_start_readback(st, remote)[1]


# Normalized states that mean "accepted, still starting" (heating the bed, running its calibration).
START_TRANSITIONAL_STATES = start_truth.START_TRANSITIONAL_STATES


def _reports_started(live, remote):
    """Printing or a transitional state, naming this upload (launch_state.same_remote): start_truth.polled."""
    return start_truth.polled(live, remote).confirmed


def _bambu_reports_file(live, remote):
    """True when a normalized Bambu status shows `remote` running or preparing.

    `printing` covers gcode_state RUNNING, PREPARE and SLICING: an A1 unpacking a package sits
    in PREPARE before RUNNING, and both prove the start reached it. The file name is the
    `subtask_name` the start command set, so exact; the bare project name (the printer's
    display spelling of the same package) is accepted too. The rule is start_truth.bambu's.
    """
    return start_truth.bambu(live, remote).confirmed


# The argument names are heater_ceiling's, shared with the macro run guard (#1147).
from .heater_ceiling import MACRO_BED_ARGS as _MACRO_BED_ARGS, MACRO_NOZZLE_ARGS as _MACRO_HOTEND_ARGS  # noqa: E402


def yolo_waivers(tail):
    """The `YOLO WAIVED:` lines of one print-now output, without the mark."""
    return [line.split(YOLO_WAIVED_MARK, 1)[1].strip()
            for line in str(tail or "").splitlines() if YOLO_WAIVED_MARK in line]


# Intake receipt reconciliation (#970). The chat intake leaves a sliced job's
# receipt at SLICED_NOT_STARTED until its own button flow starts it. Starting
# the same artifact out-of-band with `model_job print-now` used to leave the
# receipt there: chat could then offer to start a job already on the printer,
# and the receipt history was wrong (intake 16fd2142…, printing as print #83,
# still "not started"). A confirmed print-now start therefore moves the
# owning receipt to STARTED itself, records that the start was manual
# (started_via: print-now) and the print id, and settles the chat one-start
# claim so a later chat start of the same target is refused as a duplicate.


if __name__ == "__main__":
    # ``python3 -m print_handy.core.model_job`` is the documented job command line.
    from ..jobs.cli import main
    main()
