#!/usr/bin/env python3
"""Probe for issue #969: run #966's support-coverage grading on an intake.

Usage:
    python3 scripts/probe_support_coverage.py <archive folder>

The folder is one archived intake: a source model (.3mf or .stl,
optionally under a ``source/`` subfolder) plus one ``plate_*.gcode``
file per plate. The probe runs ``analyze()`` on the source and
``verify_coverage_by_plate()`` on the plates and prints a JSON report
with the per-region results, any excluded targets, and any named
``unverified`` entries - the same numbers the intake receipt records.

A folder holding ``regions.json`` + ``bounds.json`` instead of a source
model (the layout of the committed fixtures under
``tests/fixtures/support_coverage_969/``) is graded directly from those
files, which is how the synthetic fixtures are exercised.

The real intakes named in #969 (``724a5b63...`` and ``16fd2142...``)
live in the archive on catduck's Unraid/Uranium host
(``/mnt/user/appdata/print-handy/archive``); run this probe there, or
on a copy of an intake folder brought over from that host.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from print_handy.core import support_clearance  # noqa: E402


def _source_bounds(source):
    import numpy as np
    parts = support_clearance.leaves(str(source))
    pts = np.concatenate([p['tris'].reshape(-1, 3) for p in parts])
    lo, hi = pts[:, :2].min(0), pts[:, :2].max(0)
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))


def probe(folder):
    folder = Path(folder)
    plates = sorted(folder.rglob('plate_*.gcode')) or sorted(folder.rglob('*.gcode'))
    if not plates:
        raise SystemExit(f'no plate G-code found under {folder}')
    fixture_meta = folder / 'regions.json'
    if fixture_meta.exists():
        regions = json.loads(fixture_meta.read_text())
        bounds = tuple(json.loads((folder / 'bounds.json').read_text())['source_bounds'])
    else:
        models = (sorted((folder / 'source').glob('*.*')) if (folder / 'source').is_dir()
                  else sorted(folder.rglob('*.3mf')) + sorted(folder.rglob('*.stl')))
        models = [m for m in models if m.suffix.lower() in ('.3mf', '.stl')]
        if not models:
            raise SystemExit(f'no source model (.3mf/.stl) found under {folder}')
        report = support_clearance.analyze(str(models[0]))
        regions = report['regions']
        bounds = _source_bounds(models[0])
    result = support_clearance.verify_coverage_by_plate(
        regions, [{'gcode': str(p), 'source_bounds': bounds} for p in plates])
    return {'archive': str(folder), 'checked': result['checked'], 'ok': result['ok'],
            'plate_mode': result['plate_mode'], 'regions': result['regions'],
            'excluded_targets': result.get('excluded_targets', []),
            'unverified': result['unverified']}


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(json.dumps(probe(sys.argv[1]), indent=2))
