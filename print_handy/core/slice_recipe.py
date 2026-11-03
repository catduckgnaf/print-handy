"""Canonical identity of everything that decides the G-code bytes for one slice.

The recipe is versioned. Its SHA256 keys background-slice reuse, binds the worker request,
is recorded with the artifact, and is checked again before launch. A stored artifact
without a recipe of this version is refused, never assumed to match.
"""
import dataclasses
import hashlib
import json
import os
import shutil
from pathlib import Path

RECIPE_VERSION = 2  # 2: the layout spacing and prime-tower settings are inputs too
PROFILE_FILES = ('machine', 'process', 'filament')


def canonical(recipe):
    return json.dumps(recipe, sort_keys=True, separators=(',', ':'), allow_nan=False)


def binding_sha256(binding):
    """SHA256 of a canonical recipe string (what slice_binding returns)."""
    return hashlib.sha256(binding.encode()).hexdigest()


def recipe_sha256(recipe):
    return binding_sha256(canonical(recipe))


def _file_sha256(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _executable(command):
    """The command and the file it runs. A version that is only known after slicing is in provenance."""
    command = [str(part) for part in command or []]
    found = shutil.which(command[0]) if command else None
    if not found:
        return {'command': command, 'executable': None}
    real = os.path.realpath(found)
    stat = os.stat(real)
    return {'command': command, 'executable': {'path': real, 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}}


def slice_recipe(job, source_sha256, state=None, *, copies=None, scale=None, brim_override=None):
    """Recipe for slicing `job` from source bytes `source_sha256` with `state` (default: the hardware record).

    copies, scale and brim_override are the slice_job arguments that take precedence over the job.
    """
    from .. import printers, settings
    from . import model_job as mj, orca_adapter, printer_state, purge
    from .nozzle import get_tuned_nozzle_profile
    if state is None:
        # Slicing passes the loaded hardware record. A binding compared before any record
        # exists (intake fit choices) falls back to the configured nozzle so the recipe
        # stays computable, and a missing file cannot make two different jobs look identical.
        # The job's own printer record, exactly as slice_job loads it (P1-78): an
        # intake binding computed from the default record never matches the recipe
        # slice_job recomputes for a secondary printer's job (2.2.5 review).
        try:
            state = (printer_state.load(job['printer_id']) if job.get('printer_id')
                     else printer_state.load())
        except FileNotFoundError:
            state = {'nozzle': {'diameter_mm': settings.current()['printer']['nozzle_diameter_mm']}}
    adapter = mj._job_adapter(job)
    # Mirrors slice_job/slice_model: the configured model slices from [orca] templates.
    templates = (Path(orca_adapter.TEMPLATES) if adapter.key == printers.current().key
                 else Path(adapter.templates_dir()))
    orientation = dict(job.get('orientation') or {})
    supports, orientation_brim = orientation.pop('supports', None), orientation.pop('brim', None)
    decisions = job.get('color_decisions') or {}
    nozzle = state.get('nozzle') or {}
    tuned = get_tuned_nozzle_profile(nozzle['diameter_mm']) if nozzle.get('diameter_mm') else None
    slots = state.get('filament_slots') or {}
    current = settings.current()
    return {
        'recipe_version': RECIPE_VERSION,
        'source_sha256': source_sha256,
        'printer_model': adapter.key,
        'transform': {'scale': job.get('scale'), 'orientation': orientation or None,
                      'unit_decision': job.get('unit_decision')},
        'copies': int(copies or job.get('copies') or 1),
        'supports_brim': {'supports': supports, 'orientation_brim': orientation_brim,
                          'fit_brim_policy': job.get('fit_brim_policy')},
        'layout': {'plate_mode': job.get('plate_mode'), 'full_size_split_plan': job.get('full_size_split_plan'),
                    'full_size_rearrange_plan': job.get('full_size_rearrange_plan'),
                    # Part gaps, edge margins and the prime tower decide where
                    # parts land, so a settings change must not reuse an old slice.
                    'spacing': settings.spacing_summary(current)['values'],
                    'prime_tower': dict(current.get('prime_tower') or {})},
        # Where a ZMod printer's color-change purge goes decides whether the slice has a prime
        # tower and its flush volumes (#832), so a change of plan must not reuse an old slice.
        'purge': purge.resolve(job.get('printer_id'), state=state, adapter=adapter).recipe(),
        'filament': {'decisions_sha256': mj.decisions_digest(decisions) if decisions else None,
                     'selected_slot': (job.get('filament_selection') or {}).get('slot'),
                     'slots': {str(key): {field: (row or {}).get(field) for field in ('material', 'color', 'hex')}
                               for key, row in slots.items()},
                     'nozzle': {'diameter_mm': nozzle.get('diameter_mm'), 'type': nozzle.get('type')}},
        'profiles': {**{f'{name}.json': _file_sha256(templates / f'{name}.json') for name in PROFILE_FILES},
                     'nozzle_profile': dataclasses.asdict(tuned) if tuned is not None else None},
        'slicer': {'orca': _executable(orca_adapter.ORCA_COMMAND), 'bambu': _executable(orca_adapter.BAMBU_COMMAND)},
        'overrides': {'scale': scale, 'brim_override': brim_override},
    }


def record(recipe):
    """What JOB.json keeps next to the artifact: the version, the hash and the recipe itself."""
    return {'version': RECIPE_VERSION, 'sha256': recipe_sha256(recipe), 'recipe': recipe}


def require_recorded(job, binding):
    """Refuse an artifact whose recipe is missing, of another version, or does not hash to its record."""
    stored = job.get('slice_recipe')
    if (not isinstance(stored, dict) or stored.get('version') != RECIPE_VERSION
            or not isinstance(stored.get('recipe'), dict)
            or stored['recipe'].get('recipe_version') != RECIPE_VERSION
            or recipe_sha256(stored['recipe']) != stored.get('sha256')
            or binding.get('recipe_version') != RECIPE_VERSION
            or binding.get('recipe_sha256') != stored.get('sha256')):
        raise RuntimeError(f'stale color artifact: no matching slice recipe v{RECIPE_VERSION} for this G-code '
                           '(sliced before recipe identity, or the record changed); reslice before launch')
    return stored['sha256']
