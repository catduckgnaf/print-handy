"""The print repair skill's callable tools: JSON schemas and a bounded dispatcher (#1777).

Each tool is a deterministic Print Handy function with an OpenAI-compatible
function schema. A model picks and sequences them; ``Session.run`` executes
one call and returns a JSON-able dict. Tools only read model files inside
the session's workspace (the job folder): a ``mesh`` argument is a path
relative to it, and anything outside, absent, or of another type is refused
before any work. Every mesh is loaded and welded once per session.

Diagnosis tools are read-only. A repair tool writes a new mesh only when the
geometry actually changes, and reports that volume change. A repair that
cannot be done returns an error and does not leave a copy behind. The
verification loop re-slices after a repair.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ...core import layer_scan, mesh_diagnosis, mesh_repair, plane_cut, support_clearance

logger = logging.getLogger(__name__)

MESH_SUFFIXES = ('.stl', '.3mf')
MAX_MESH_BYTES = 512 * 1024 * 1024
MAX_ITEMS = 60


class ToolError(ValueError):
    """A tool call that cannot run: unknown tool, bad arguments, or a refused path."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., dict]

    def schema(self) -> dict:
        return {'type': 'function',
                'function': {'name': self.name, 'description': self.description,
                             'parameters': self.parameters}}


_MESH = {'type': 'string',
         'description': 'Model file (STL or 3MF) as a path inside the job folder, for example "source/part.stl".'}
_LAYER = {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1.0,
          'description': 'Layer height in mm of the job profile (default 0.2).'}
_FIRST = {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1.0,
          'description': 'First layer height in mm (default: the layer height).'}
_REGION = {'type': 'array', 'items': {'type': 'number'}, 'minItems': 4, 'maxItems': 4,
           'description': 'XY box [xmin, ymin, xmax, ymax] in mm, in the model file\'s coordinates.'}


def _params(properties, required=('mesh',)):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'additionalProperties': False}


def _clip(items, key):
    items = list(items)
    out = {key: items[:MAX_ITEMS]}
    if len(items) > MAX_ITEMS:
        out[f'{key}_not_listed'] = len(items) - MAX_ITEMS
    return out


def _island_scan(session, mesh, layer_mm=layer_scan.DEFAULT_LAYER_MM, first_layer_mm=None):
    result = layer_scan.island_scan(session.mesh(mesh), layer_mm, first_layer_mm=first_layer_mm)
    out = {k: result[k] for k in ('layers', 'layer_mm', 'pitch_mm')}
    out['island_count'] = len(result['islands'])
    out.update(_clip(result['islands'], 'islands'))
    out['open_layers'] = len(result['open_layers'])
    return out


def _overhang_scan(session, mesh, layer_mm=layer_scan.DEFAULT_LAYER_MM, first_layer_mm=None,
                   angle_deg=layer_scan.DEFAULT_ANGLE_DEG, min_area_mm2=layer_scan.MIN_OVERHANG_MM2):
    result = layer_scan.overhang_scan(session.mesh(mesh), layer_mm, first_layer_mm=first_layer_mm,
                                      angle_deg=angle_deg, min_area_mm2=min_area_mm2)
    overhangs = sorted(result['overhangs'], key=lambda o: -o['area_mm2'])
    out = {k: result[k] for k in ('layers', 'layer_mm', 'pitch_mm', 'angle_deg')}
    out['overhang_count'] = len(overhangs)
    out.update(_clip(overhangs, 'overhangs'))
    return out


def _bodies(session, mesh):
    result = mesh_diagnosis.bodies(session.mesh(mesh))
    out = {'count': result['count']}
    out.update(_clip(result['bodies'], 'bodies'))
    return out


def _watertight(session, mesh):
    report = mesh_diagnosis.edge_report(session.mesh(mesh))
    return {k: report[k] for k in ('watertight', 'manifold', 'shells', 'boundary_edges',
                                    'winding_inconsistencies', 'degenerate_triangles',
                                    'boundary_edge_midpoints')}


def _nonmanifold_edges(session, mesh):
    report = mesh_diagnosis.edge_report(session.mesh(mesh))
    return {'non_manifold_edges': report['non_manifold_edges'],
            'non_manifold_edge_midpoints': report['non_manifold_edge_midpoints']}


def _underside_profile(session, mesh, region, z_range=None):
    return mesh_diagnosis.underside_profile(session.mesh(mesh), region, z_range)


def _clearance_report(session, mesh, first_layer_mm=None):
    path = session.path(mesh)
    if first_layer_mm is None:
        first_layer_mm = support_clearance.profile_first_layer_mm(source=path)
    report = support_clearance.analyze(path, first_layer_mm=first_layer_mm)
    drop = ('footprint', 'fixes', 'material')
    findings = [{k: v for k, v in f.items() if k not in drop} for f in report.get('findings', [])]
    regions = [{k: v for k, v in r.items() if k not in drop} for r in report.get('regions', [])]
    out = {'risky': bool(support_clearance.risky(report)), 'codes': support_clearance.codes(report),
           'first_layer_mm': first_layer_mm}
    out.update(_clip(findings, 'findings'))
    out.update(_clip(regions, 'regions'))
    return out


# ---------------------------------------------------------------- repair tools

def _dest(session, original_rel: str, suffix: str, *, stl: bool = False) -> tuple[Path, str]:
    """A new workspace path for a repair copy. Never the source path."""
    src = session.path(original_rel)
    ext = '.stl' if stl else src.suffix
    dest = src.parent / f'{src.stem}{suffix}{ext}'
    n = 1
    while dest.exists() or dest.resolve() == src.resolve():
        dest = src.parent / f'{src.stem}{suffix}-{n}{ext}'
        n += 1
    return dest, str(dest.relative_to(session.workspace))


def _discard(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning('could not remove an unused repair copy')


def _fail(dest: Path | None, error: str) -> dict:
    """A repair that did not happen: no file, and not ok."""
    _discard(dest)
    return {'ok': False, 'error': error}


def _mesh_differs(src: Path, dest: Path) -> bool:
    """True when the two files are not the same triangles (a format rewrite is not a change)."""
    try:
        before = np.asarray(mesh_repair.load_triangles(src), dtype=float)
        after = np.asarray(mesh_repair.load_triangles(dest), dtype=float)
    except Exception:
        try:
            return src.read_bytes() != dest.read_bytes()
        except OSError:
            return False
    if before.shape != after.shape:
        return True
    return not np.allclose(before, after, rtol=0, atol=1e-3)


def _ok_if_changed(src: Path, dest: Path, rel: str, *, volume_key: str, volume: float) -> dict:
    """ok only when the volume change is real or the triangles actually differ."""
    delta = float(volume)
    if not dest.is_file() or (abs(delta) <= 1e-3 and not _mesh_differs(src, dest)):
        return _fail(dest, 'the repair did not change the mesh, so nothing was changed')
    return {'ok': True, 'new_mesh': rel, volume_key: round(delta, 3)}


def _unavailable(session, mesh: str, name: str) -> dict:
    session.path(mesh)  # same path refusal as a tool that does run
    return _fail(None, f'{name} is not available yet, so nothing was changed')


def _trim_below_plane(session, mesh, region, z):
    """Drop solid below ``z`` when that sag is inside ``region``.

    ``plane_cut.cut`` splits the whole mesh. That is this tool only when
    every vertex below the plane is inside the region; a partial trim would
    cut design that was not asked for, so it is refused and nothing is written.
    """
    src = session.path(mesh)
    try:
        tris = mesh_repair.load_triangles(src)
    except mesh_repair.FixError as exc:
        return _fail(None, str(exc))
    except Exception as exc:
        return _fail(None, f'could not read the mesh: {exc}')
    if len(tris) == 0:
        return _fail(None, 'the mesh has no triangles to trim, so nothing was changed')
    xmin, ymin, xmax, ymax = (float(value) for value in region)
    if not (xmin < xmax and ymin < ymax):
        return _fail(None, 'region must be an XY box with positive area, so nothing was changed')
    z = float(z)
    below = tris[:, :, 2] < z - 1e-6
    if not bool(below.any()):
        return _fail(None, 'nothing sags below that plane, so nothing was changed')
    xs, ys = tris[:, :, 0], tris[:, :, 1]
    outside = ((xs < xmin - 1e-6) | (xs > xmax + 1e-6)
               | (ys < ymin - 1e-6) | (ys > ymax + 1e-6))
    if bool((below & outside).any()):
        return _fail(None, 'geometry below the plane sits outside the region, and a partial '
                     'trim is not available, so nothing was changed')
    try:
        cut = plane_cut.cut(tris, 2, z)
    except mesh_repair.FixError as exc:
        return _fail(None, str(exc))
    except Exception as exc:
        logger.exception('trim_below_plane failed')
        return _fail(None, f'trim_below_plane failed: {type(exc).__name__}: {exc}')
    if cut is None:
        return _fail(None, 'the plane does not separate any geometry, so nothing was changed')
    removed = abs(float(plane_cut.volume(*cut.lower)))
    points, faces = cut.upper
    if removed <= 1e-3 or len(faces) == 0:
        return _fail(None, 'the trim would not remove a measurable volume, so nothing was changed')
    dest, rel = _dest(session, mesh, '-trimmed', stl=True)
    try:
        support_clearance._write_binary_stl(dest, points[faces])
    except Exception as exc:
        return _fail(dest, f'could not write the trimmed mesh: {exc}')
    return _ok_if_changed(src, dest, rel, volume_key='volume_removed_mm3', volume=removed)


def _gusset_45(session, mesh, overhang_region, wall_x):
    return _unavailable(session, mesh, 'gusset_45')


def _fill_under_lip(session, mesh, region):
    return _unavailable(session, mesh, 'fill_under_lip')


def _straight_base(session, mesh, height):
    return _unavailable(session, mesh, 'straight_base')


def _extend_underside(session, mesh, mm):
    """Move a low underside onto the plate. A no-op write is not a repair."""
    src = session.path(mesh)
    dest, rel = _dest(session, mesh, '-extended')
    try:
        report = support_clearance.extend_underside(src, dest_path=dest, gap_mm=float(mm))
    except Exception as exc:
        return _fail(dest, f'extend_underside failed: {exc}')
    added = float(report.get('added_volume_mm3') or 0.0)
    modified = int(report.get('modified_vertices') or 0)
    if modified <= 0 and abs(added) <= 1e-3:
        return _fail(dest, 'no underside at that height was extended, so nothing was changed')
    return _ok_if_changed(src, dest, rel, volume_key='volume_added_mm3', volume=added)


def _sink(session, mesh, mm):
    """Sink the model by ``mm``. The original file is not opened for writing."""
    src = session.path(mesh)
    dest, rel = _dest(session, mesh, '-sunk')
    try:
        report = support_clearance.sink_model(src, dest_path=dest, dz=float(mm))
    except Exception as exc:
        return _fail(dest, f'sink failed: {exc}')
    removed = float(report.get('removed_volume_mm3') or 0.0)
    return _ok_if_changed(src, dest, rel, volume_key='volume_removed_mm3', volume=removed)


def _repair_mesh(session, mesh):
    """Run ``mesh_repair.repair`` (not ``repair_mesh``). Failure writes nothing."""
    src = session.path(mesh)
    dest, rel = _dest(session, mesh, '-repaired', stl=True)
    try:
        report = mesh_repair.repair(src, dest)
    except Exception as exc:
        return _fail(dest, str(exc))
    before = (report.get('before') or {}).get('signed_volume_mm3')
    after = (report.get('after') or {}).get('signed_volume_mm3')
    delta = 0.0 if before is None or after is None else float(after) - float(before)
    out = _ok_if_changed(src, dest, rel, volume_key='volume_change_mm3', volume=delta)
    if out.get('ok'):
        out['note'] = 'mesh repaired'
    return out


TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    Tool('island_scan',
         'Find floating regions: parts of a printed layer with no overlap with the layer below, '
         'with Z (above the bed), area and XY bounds. A slicer prints these into the air unless '
         'supported.',
         _params({'mesh': _MESH, 'layer_mm': _LAYER, 'first_layer_mm': _FIRST}), _island_scan),
    Tool('overhang_scan',
         'Find overhangs: new area of a layer further than the self-supporting angle (45 degrees '
         'by default) beyond the layer below, joined across layers, with Z range, area, bounds, '
         'size and whether the overhang starts floating. Cantilevers, lips and bridges show here.',
         _params({'mesh': _MESH, 'layer_mm': _LAYER, 'first_layer_mm': _FIRST,
                  'angle_deg': {'type': 'number', 'exclusiveMinimum': 0, 'exclusiveMaximum': 90,
                                'description': 'Self-supporting angle from horizontal (default 45).'},
                  'min_area_mm2': {'type': 'number', 'minimum': 0,
                                   'description': 'Smallest per-layer area reported (default 1 mm2).'}}),
         _overhang_scan),
    Tool('bodies',
         'List the separate solids in the model, with faces, bounds, area and volume.',
         _params({'mesh': _MESH}), _bodies),
    Tool('watertight',
         'Is the mesh closed and manifold? Counts of open edges, winding problems and degenerate '
         'triangles, and where the first open edges are.',
         _params({'mesh': _MESH}), _watertight),
    Tool('nonmanifold_edges',
         'Count the edges shared by more than two faces and say where the first ones are.',
         _params({'mesh': _MESH}), _nonmanifold_edges),
    Tool('underside_profile',
         'Is the downward-facing surface over a region a flat floating underside (needs support '
         'or a fix) or a sloped flare that holds itself up (a low-clearance finding there is a '
         'false positive)? Reports the area-weighted slope.',
         _params({'mesh': _MESH, 'region': _REGION,
                  'z_range': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 2, 'maxItems': 2,
                              'description': 'Only faces within [zmin, zmax] mm above the bed.'}},
                 required=('mesh', 'region')),
         _underside_profile),
    Tool('clearance_report',
         'Print Handy\'s support clearance check: undersides too close to the plate or a part '
         'below for slicer supports to fit, below-bed geometry, ineffective modeled supports.',
         _params({'mesh': _MESH, 'first_layer_mm': _FIRST}), _clearance_report),
    # Repair tools
    Tool('trim_below_plane',
         'Remove geometry sagging below a face plane in the given region. Writes a new copy '
         'only when the cut removes volume.',
         _params({'mesh': _MESH, 'region': _REGION,
                  'z': {'type': 'number',
                        'description': 'Z of the face plane in the model file, millimeters.'}},
                 required=('mesh', 'region', 'z')),
         _trim_below_plane),
    Tool('gusset_45',
         'Add a self-supporting 45-degree gusset under a cantilever. Writes a new copy.',
         _params({'mesh': _MESH, 'overhang_region': _REGION,
                  'wall_x': {'type': 'number',
                             'description': 'X of the wall the gusset grows from, millimeters.'}},
                 required=('mesh', 'overhang_region', 'wall_x')),
         _gusset_45),
    Tool('fill_under_lip',
         'Fill under a lip with a 45-degree ramp to the plate. Writes a new copy.',
         _params({'mesh': _MESH, 'region': _REGION}, required=('mesh', 'region')),
         _fill_under_lip),
    Tool('straight_base',
         'Replace the bottom with a straight-walled base of the given height. Writes a new copy.',
         _params({'mesh': _MESH,
                  'height': {'type': 'number', 'exclusiveMinimum': 0,
                             'description': 'Height of the straight base, millimeters.'}},
                 required=('mesh', 'height')),
         _straight_base),
    Tool('extend_underside',
         'Extend a low underside down to the plate. Writes a new copy only when vertices move.',
         _params({'mesh': _MESH,
                  'mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 5,
                         'description': 'Underside height to extend to the plate, millimeters (at most 5).'}},
                 required=('mesh', 'mm')),
         _extend_underside),
    Tool('sink',
         'Sink the model so the underside meets the plate (clips below). Writes a new copy.',
         _params({'mesh': _MESH,
                  'mm': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 5,
                         'description': 'How far to sink the model, millimeters (at most 5).'}},
                 required=('mesh', 'mm')),
         _sink),
    Tool('repair_mesh',
         'Deterministic mesh repair (holes, inverted normals, non-manifold). Writes a new copy.',
         _params({'mesh': _MESH}),
         _repair_mesh),
)}


def openai_tools() -> list[dict]:
    """The function schemas to pass to an OpenAI-compatible endpoint, in registry order."""
    return [tool.schema() for tool in TOOLS.values()]


def _check_type(name, key, value, schema):
    kind = schema.get('type')
    if kind == 'string' and not isinstance(value, str):
        raise ToolError(f'{name}: {key} must be a string')
    if kind == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f'{name}: {key} must be a number')
        if 'exclusiveMinimum' in schema and not value > schema['exclusiveMinimum']:
            raise ToolError(f'{name}: {key} must be above {schema["exclusiveMinimum"]}')
        if 'minimum' in schema and not value >= schema['minimum']:
            raise ToolError(f'{name}: {key} must be at least {schema["minimum"]}')
        if 'maximum' in schema and not value <= schema['maximum']:
            raise ToolError(f'{name}: {key} must be at most {schema["maximum"]}')
        if 'exclusiveMaximum' in schema and not value < schema['exclusiveMaximum']:
            raise ToolError(f'{name}: {key} must be below {schema["exclusiveMaximum"]}')
    if kind == 'array':
        if not isinstance(value, list):
            raise ToolError(f'{name}: {key} must be a list')
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', len(value)):
            raise ToolError(f'{name}: {key} has the wrong number of values')
        for item in value:
            _check_type(name, key, item, schema.get('items', {}))


def _arguments(tool, arguments):
    if isinstance(arguments, (str, bytes)):
        try:
            arguments = json.loads(arguments or '{}')
        except ValueError as exc:
            raise ToolError(f'{tool.name}: arguments are not valid JSON') from exc
    if not isinstance(arguments, dict):
        raise ToolError(f'{tool.name}: arguments must be an object')
    schema = tool.parameters
    unknown = sorted(set(arguments) - set(schema['properties']))
    if unknown:
        raise ToolError(f'{tool.name}: unknown argument(s) {", ".join(unknown)}')
    missing = [key for key in schema['required'] if key not in arguments]
    if missing:
        raise ToolError(f'{tool.name}: missing argument(s) {", ".join(missing)}')
    for key, value in arguments.items():
        _check_type(tool.name, key, value, schema['properties'][key])
    return arguments


class Session:
    """One repair conversation's tool runner over one workspace (the job folder)."""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ToolError('the workspace folder does not exist')
        self._meshes: dict[Any, layer_scan.Mesh] = {}

    def path(self, relative) -> Path:
        """A model file inside the workspace, or ToolError."""
        if not isinstance(relative, str) or not relative.strip():
            raise ToolError('mesh must be a file path inside the job folder')
        candidate = Path(relative)
        if candidate.is_absolute() or candidate.drive:
            raise ToolError('mesh must be a path relative to the job folder')
        resolved = (self.workspace / candidate).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ToolError('mesh must stay inside the job folder')
        if resolved.suffix.lower() not in MESH_SUFFIXES:
            raise ToolError('mesh must be an STL or 3MF file')
        if not resolved.is_file():
            raise ToolError(f'{relative} does not exist in the job folder')
        if resolved.stat().st_size > MAX_MESH_BYTES:
            raise ToolError(f'{relative} is larger than {MAX_MESH_BYTES // (1024 * 1024)} MB')
        return resolved

    def mesh(self, relative) -> layer_scan.Mesh:
        """The welded mesh of a workspace file, loaded once per file version."""
        path = self.path(relative)
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._meshes:
            self._meshes[key] = layer_scan.load(path)
        return self._meshes[key]

    def run(self, name, arguments=None) -> dict:
        """Run one tool call: ``{'ok': True, 'result': ...}`` or ``{'ok': False, 'error': ...}``.

        Bad calls (unknown tool, bad arguments, refused paths, input a tool
        cannot measure) come back as errors the model can read and correct.
        A repair result with ``ok`` false is an error too: a no-op must not
        look like a successful tool call. An unexpected failure is logged
        and reported without its traceback.
        """
        tool = TOOLS.get(name)
        try:
            if tool is None:
                raise ToolError(f'unknown tool {name!r}')
            args = _arguments(tool, {} if arguments is None else arguments)
            result = tool.run(self, **args)
        except (ToolError, layer_scan.ScanError) as exc:
            return {'ok': False, 'error': str(exc)}
        except Exception as exc:
            logger.exception('print repair tool %s failed', name)
            return {'ok': False, 'error': f'{name} failed: {type(exc).__name__}: {exc}'}
        if isinstance(result, dict) and result.get('ok') is False:
            error = str(result.get('error') or f'{name} did not change the file')
            return {'ok': False, 'error': error, 'result': result}
        return {'ok': True, 'result': result}

