"""100%-scale geometry inspection, multi-object plate arrangement, and export.

Pure stdlib implementation for Print Handy (#152). Preserves 100% physical
dimensions, converts explicit length units to millimeters, detects authored
3MF instance scaling/shear without silently undoing it, packs multi-object or
single-object models onto a nominal build volume without resizing, and exports
verified geometry-only 3MF, combined/per-object STL, PNG layout preview, and a
5-stage validation report.

This module performs offline geometry preparation only; it never slices,
uploads, or starts a printer job.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

from .compact_geometry import CompactTriangles, IndexedTriangles, transform_vertices_array
from .mesh_fast import archive_tables, bounds_and_volume
from .model_job import mesh_topology
from .orca_adapter import _hull, read_stl_triangles
from .painted_geometry import paint_census
from .source_colors import cutter_ids, inventory as color_inventory

NS = {'c': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}


def parse_transform(value: str | None):
    if not value:
        return [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    vals = [float(v) for v in value.split()]
    if len(vals) != 12:
        raise ValueError(f'Invalid 3MF transform {value!r}')
    return vals


def apply(t, point):
    x, y, z = point
    return (
        t[0] * x + t[3] * y + t[6] * z + t[9],
        t[1] * x + t[4] * y + t[7] * z + t[10],
        t[2] * x + t[5] * y + t[8] * z + t[11],
    )

UNIT_TO_MM = {
    'millimeter': 1.0,
    'millimetre': 1.0,
    'mm': 1.0,
    'centimeter': 10.0,
    'centimetre': 10.0,
    'cm': 10.0,
    'meter': 1000.0,
    'metre': 1000.0,
    'm': 1000.0,
    'inch': 25.4,
    'in': 25.4,
    'foot': 304.8,
    'ft': 304.8,
    'micron': 0.001,
    'micrometer': 0.001,
}

STATUS_LADDER_STAGES = (
    'geometry_inspected',
    'geometry_nominal_fit',
    'slicer_capacity_verified',
    'gcode_sliced_and_guarded',
    'physical_print_tested',
)

DEFAULT_BED_MM = (220.0, 220.0, 220.0)


def _configured_spacing():
    """layout.arrange_edge_mm / layout.arrange_gap_mm (resolved from layout.spacing)."""
    try:
        from .. import settings
        layout = settings.current()['layout']
        return float(layout['arrange_edge_mm']), float(layout['arrange_gap_mm'])
    except Exception:  # standalone use without a settings tree keeps the Standard values
        return 5.0, 5.0


DEFAULT_MARGIN_MM, DEFAULT_GAP_MM = _configured_spacing()
DEFAULT_MAX_SEARCH_NODES = 40000


def _r4(values):
    return [round(float(v), 4) for v in values]


def _sha256(path: Path) -> str:
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _safe_name(name: str, idx: int) -> str:
    simple = re.sub(r'[^A-Za-z0-9_-]+', '_', str(name)).strip('_')[:75]
    return f'{idx:02d}_{simple or "part"}'


def _compose_transforms(outer, inner):
    """Compose two 12-value 3MF affine transforms (outer applied after inner)."""
    return [
        outer[0] * inner[0] + outer[3] * inner[1] + outer[6] * inner[2],
        outer[1] * inner[0] + outer[4] * inner[1] + outer[7] * inner[2],
        outer[2] * inner[0] + outer[5] * inner[1] + outer[8] * inner[2],
        outer[0] * inner[3] + outer[3] * inner[4] + outer[6] * inner[5],
        outer[1] * inner[3] + outer[4] * inner[4] + outer[7] * inner[5],
        outer[2] * inner[3] + outer[5] * inner[4] + outer[8] * inner[5],
        outer[0] * inner[6] + outer[3] * inner[7] + outer[6] * inner[8],
        outer[1] * inner[6] + outer[4] * inner[7] + outer[7] * inner[8],
        outer[2] * inner[6] + outer[5] * inner[7] + outer[8] * inner[8],
        outer[0] * inner[9] + outer[3] * inner[10] + outer[6] * inner[11] + outer[9],
        outer[1] * inner[9] + outer[4] * inner[10] + outer[7] * inner[11] + outer[10],
        outer[2] * inner[9] + outer[5] * inner[10] + outer[8] * inner[11] + outer[11],
    ]


def _linear_transform_deviation(transform_12):
    """Detect non-unit scaling or shear in the 3x3 linear block of a 3MF transform."""
    cols = [
        (transform_12[0], transform_12[1], transform_12[2]),
        (transform_12[3], transform_12[4], transform_12[5]),
        (transform_12[6], transform_12[7], transform_12[8]),
    ]
    norms = [math.sqrt(sum(c * c for c in col)) for col in cols]
    dots = [
        sum(cols[0][k] * cols[1][k] for k in range(3)),
        sum(cols[0][k] * cols[2][k] for k in range(3)),
        sum(cols[1][k] * cols[2][k] for k in range(3)),
    ]
    non_unit = any(abs(n - 1.0) > 1e-4 for n in norms)
    has_shear = any(abs(d) > 1e-4 for d in dots)
    return (non_unit or has_shear), sorted(norms, reverse=True)


def _triangle_area_and_signed_volume(triangles):
    if isinstance(triangles, IndexedTriangles) and len(triangles) >= 32768:
        # Batched twin of the scalar loop below: same per-face operation
        # order, chunk sums accumulated in face order.
        import numpy as np
        area = 0.0
        signed_vol = 0.0
        vertices, faces = triangles.vertices, triangles.faces
        for start in range(0, len(faces), 8192):
            pts = vertices[faces[start:start + 8192].ravel()].reshape((-1, 3, 3))
            a, b, c = pts[:, 0], pts[:, 1], pts[:, 2]
            ux, uy, uz = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1], b[:, 2] - a[:, 2]
            vx, vy, vz = c[:, 0] - a[:, 0], c[:, 1] - a[:, 1], c[:, 2] - a[:, 2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            area += float(np.sum(0.5 * np.sqrt(nx * nx + ny * ny + nz * nz)))
            signed_vol += float(np.sum(
                (a[:, 0] * (b[:, 1] * c[:, 2] - b[:, 2] * c[:, 1])
                 - a[:, 1] * (b[:, 0] * c[:, 2] - b[:, 2] * c[:, 0])
                 + a[:, 2] * (b[:, 0] * c[:, 1] - b[:, 1] * c[:, 0])) / 6.0))
        return area, abs(signed_vol)
    area = 0.0
    signed_vol = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx = uy * vz - uz * vy
        ny = uz * vx - ux * vz
        nz = ux * vy - uy * vx
        area += 0.5 * math.sqrt(nx * nx + ny * ny + nz * nz)
        signed_vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return area, abs(signed_vol)


def _bounds_from_triangles(triangles):
    lo, hi, _ = bounds_and_volume(triangles)
    ext = [hi[a] - lo[a] for a in range(3)]
    return lo, hi, ext


def describe_triangles(triangles):
    """Compute bounds, dimensions, topology, area, and volume for a triangle list."""
    lo, hi, ext = _bounds_from_triangles(triangles)
    topo = mesh_topology(triangles)
    area, vol = _triangle_area_and_signed_volume(triangles)
    return {
        'bounds_min_mm': _r4(lo),
        'bounds_max_mm': _r4(hi),
        'dimensions_mm': _r4(ext),
        'watertight': bool(topo['watertight']),
        'manifold': bool(topo['manifold']),
        'winding_consistent': topo['winding_inconsistencies'] == 0,
        'shell_count': int(topo['connected_shells']),
        'faces': int(topo['triangle_count']),
        'boundary_edges': int(topo['boundary_edges']),
        'non_manifold_edges': int(topo['non_manifold_edges']),
        'singular_vertices': int(topo['singular_vertices']),
        'winding_inconsistencies': int(topo['winding_inconsistencies']),
        'degenerate_triangles': int(topo['degenerate_triangles']),
        'area_mm2': round(float(area), 4),
        'abs_volume_mm3': round(float(vol), 4) if topo['watertight'] else None,
    }


def split_disconnected_shells(triangles):
    """Split a triangle list into topologically connected shells without repairing."""
    vertices = {}
    faces = []
    for tri in triangles:
        ids = []
        for point in tri:
            key = tuple(round(float(v), 6) for v in point)
            ids.append(vertices.setdefault(key, len(vertices)))
        faces.append(ids)
    edges = {}
    adjacency = [[] for _ in faces]
    for fi, face in enumerate(faces):
        for a, b in zip(face, face[1:] + face[:1]):
            key = (min(a, b), max(a, b))
            edges.setdefault(key, []).append(fi)
    for uses in edges.values():
        for fi in uses:
            for other in uses:
                if fi != other:
                    adjacency[fi].append(other)
    seen = set()
    shells = []
    for start in range(len(faces)):
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(triangles[cur])
            for other in adjacency[cur]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        shells.append(comp)
    return shells


def inspect_3mf_semantics(path: Path) -> dict:
    """Inspect 3MF semantic features that geometry-only exports cannot preserve."""
    path = Path(path)
    if path.suffix.lower() != '.3mf':
        return {
            'has_negative_volumes': False,
            'cutter_pairs': [],
            'has_painted_triangles': False,
            'painted_object_ids': [],
            'has_multi_extruder': False,
            'extruders': [1],
            'has_model_settings': False,
            'has_project_settings': False,
        }
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        cutters = sorted([list(pair) for pair in cutter_ids(archive)])
        has_model_settings = 'Metadata/model_settings.config' in names
        has_project_settings = 'Metadata/project_settings.config' in names
        raw_model = archive.read('3D/3dmodel.model') if '3D/3dmodel.model' in names else b''
    try:
        census = paint_census(path)
        painted_oids = sorted(oid for oid, row in census.items() if row.get('states'))
    except Exception:
        painted_oids = []
    has_raw_paint = bool(re.search(rb'\b(?:paint_color|mmu_segmentation)="[0-9A-Fa-f]+"', raw_model))
    try:
        manifest = color_inventory(path)
        extruders = sorted({int(p.get('source_extruder') or 1) for p in manifest.get('parts') or []} | {1})
        has_painted_manifest = bool(manifest.get('painted'))
    except Exception:
        extruders = [1]
        has_painted_manifest = False
    has_paint = bool(painted_oids) or has_raw_paint or has_painted_manifest
    return {
        'has_negative_volumes': bool(cutters),
        'cutter_pairs': cutters,
        'has_painted_triangles': has_paint,
        'painted_object_ids': painted_oids,
        'has_multi_extruder': len(extruders) > 1 or has_paint,
        'extruders': extruders,
        'has_model_settings': has_model_settings,
        'has_project_settings': has_project_settings,
    }


def _load_3mf_parts(path: Path, input_unit: str | None = None):
    models = {}
    with zipfile.ZipFile(path) as archive:
        if '3D/3dmodel.model' not in archive.namelist():
            raise ValueError(f'{path} is missing 3D/3dmodel.model')
        cutters = cutter_ids(archive)
        root_bytes, _ = archive_tables(archive, '3D/3dmodel.model')
        root_el = ET.fromstring(root_bytes)
        declared_unit = root_el.get('unit') or 'millimeter'
        raw_unit = input_unit or declared_unit
        if raw_unit.lower() not in UNIT_TO_MM:
            raise ValueError(f'Unrecognized/missing length units {raw_unit!r}; supply --input-unit')
        mm_factor = UNIT_TO_MM[raw_unit.lower()]

        object_names = {}
        if 'Metadata/model_settings.config' in archive.namelist():
            try:
                settings_el = ET.fromstring(archive.read('Metadata/model_settings.config'))
                for obj_el in settings_el.findall('object'):
                    oid = obj_el.get('id')
                    for meta in obj_el.findall('metadata'):
                        if meta.get('key') == 'name' and meta.get('value'):
                            object_names[oid] = meta.get('value')
            except ET.ParseError:
                pass

        def get_model(model_path):
            if model_path not in models:
                data, tables = archive_tables(archive, model_path)
                r = ET.fromstring(data)
                models[model_path] = (r, {o.get('id'): o for o in r.findall('c:resources/c:object', NS)}, tables)
            return models[model_path]

        def collect_triangles(model_path, object_id, chain, seen):
            if (model_path, object_id) in seen:
                raise RuntimeError('3MF component cycle')
            _, objects, tables = get_model(model_path)
            if object_id not in objects:
                raise RuntimeError(f'missing 3MF object {model_path}:{object_id}')
            obj = objects[object_id]
            mesh = obj.find('c:mesh', NS)
            tris = IndexedTriangles()
            if mesh is not None:
                transforms = list(chain)
                if mm_factor != 1.0:
                    transforms.append([mm_factor, 0, 0, 0, mm_factor, 0, 0, 0, mm_factor, 0, 0, 0])
                if tables is not None:
                    table = tables.get(object_id)
                    if table is not None:
                        # Compact indexed path: transform the shared vertices
                        # once (bit-identical to the scalar apply chain) and
                        # keep the faces as indices; never expand per-face
                        # point tuples.
                        verts = transform_vertices_array(table.vertices, transforms, apply)
                        tris.extend_indexed(verts, table.triangles)
                else:
                    import numpy as np
                    verts = [
                        (float(v.attrib['x']), float(v.attrib['y']), float(v.attrib['z']))
                        for v in mesh.findall('c:vertices/c:vertex', NS)
                    ]
                    faces = [(int(t.attrib['v1']), int(t.attrib['v2']), int(t.attrib['v3']))
                             for t in mesh.findall('c:triangles/c:triangle', NS)]
                    if faces:
                        face_array = np.array(faces, dtype=np.int64)
                        count = len(verts)
                        if int(face_array.max()) >= count or int(face_array.min()) < -count:
                            raise IndexError('3MF triangle vertex index out of range')
                        face_array = np.where(face_array < 0, face_array + count, face_array)
                        verts = transform_vertices_array(
                            np.array(verts, dtype=np.float64).reshape((-1, 3)), transforms, apply)
                        tris.extend_indexed(verts, face_array)
            for comp in obj.findall('c:components/c:component', NS):
                comp_oid = comp.get('objectid')
                if (object_id, comp_oid) in cutters:
                    continue
                ext = next((v for k, v in comp.attrib.items() if k.endswith('}path')), model_path)
                tris.extend(
                    collect_triangles(
                        ext.lstrip('/'),
                        comp_oid,
                        [parse_transform(comp.get('transform')), *chain],
                        seen | {(model_path, object_id)},
                    )
                )
            return tris

        root, objects, _ = get_model('3D/3dmodel.model')
        parts = []
        warnings = []
        items = [item for item in root.findall('c:build/c:item', NS) if item.get('printable', '1') == '1']
        for idx, item in enumerate(items, 1):
            oid = item.get('objectid')
            item_tf = parse_transform(item.get('transform'))
            obj = objects.get(oid)
            comp_tfs = [parse_transform(c.get('transform')) for c in (obj.findall('c:components/c:component', NS) if obj is not None else [])]
            effective_tf = item_tf
            if len(comp_tfs) == 1:
                effective_tf = _compose_transforms(item_tf, comp_tfs[0])
            deviated, norms = _linear_transform_deviation(effective_tf)
            node_label = object_names.get(oid) or (obj.get('name') if obj is not None else None) or f'object_{oid}'
            if deviated:
                warnings.append(
                    f'Object {node_label} (id={oid}) has authored source transform scaling/shear '
                    f'(column norms {_r4(norms)}); preserving it, not resetting to unscaled CAD dimensions.'
                )
            tris = collect_triangles('3D/3dmodel.model', oid, [item_tf], set())
            if not tris:
                continue
            parts.append({
                'id': _safe_name(node_label, idx),
                'node': str(node_label),
                'object_id': str(oid),
                'triangles': tris,
            })
        if not parts:
            raise ValueError('No printable mesh objects were found')
        return parts, str(raw_unit), mm_factor, warnings


def load_parts(path: Path, input_unit: str | None = None, split_stl: bool = False):
    """Load printable parts from an STL or 3MF in millimeters without user scaling."""
    from .intake_resources import check_source
    path = Path(path)
    check_source(path)
    suffix = path.suffix.lower()
    if suffix not in ('.stl', '.3mf'):
        raise ValueError('Only STL and 3MF are supported')
    if suffix == '.3mf':
        return _load_3mf_parts(path, input_unit=input_unit)
    raw_unit = input_unit or 'mm'
    if raw_unit.lower() not in UNIT_TO_MM:
        raise ValueError(f'Unrecognized/missing length units {raw_unit!r}; supply --input-unit')
    mm_factor = UNIT_TO_MM[raw_unit.lower()]
    tris = read_stl_triangles(path)
    if mm_factor != 1.0:
        tris = [[(p[0] * mm_factor, p[1] * mm_factor, p[2] * mm_factor) for p in tri] for tri in tris]
    submeshes = split_disconnected_shells(tris) if split_stl else [tris]
    parts = []
    stem = path.stem or 'part'
    for idx, shell in enumerate(submeshes, 1):
        node = stem if len(submeshes) == 1 else f'{stem}_shell_{idx}'
        parts.append({
            'id': _safe_name(node, idx),
            'node': node,
            'object_id': str(idx),
            'triangles': shell,
        })
    warnings = []
    if input_unit is None:
        warnings.append('An STL has no embedded units; assumed millimeters (1.0 mm/unit).')
    for p in parts:
        info = describe_triangles(p['triangles'])
        if info['shell_count'] > 1 and not split_stl:
            warnings.append(
                f"Part {p['id']} contains {info['shell_count']} disconnected triangle shells; "
                "verify whether shells overlap or should be separated with --split-stl-components."
            )
        if not info['watertight']:
            warnings.append(
                f"Part {p['id']} is non-watertight (boundary_edges={info['boundary_edges']}, "
                f"non_manifold_edges={info['non_manifold_edges']}); review before slicing."
            )
    return parts, str(raw_unit), mm_factor, warnings


def rotate_triangles_z(triangles, angle_deg: float):
    """Rotate triangles around Z by angle_deg and normalize min XYZ to (0, 0, 0)."""
    rad = math.radians(float(angle_deg))
    c, s = math.cos(rad), math.sin(rad)
    from .compact_geometry import TransformedTriangles
    rotated = TransformedTriangles(triangles, lambda x, y, z: (c * x - s * y, s * x + c * y, z))
    lo, hi, ext = _bounds_from_triangles(rotated)
    dx, dy, dz = -lo[0], -lo[1], -lo[2]
    normalized = TransformedTriangles(rotated, lambda x, y, z: (x + dx, y + dy, z + dz))
    return normalized, ext, (c, s, lo)


def rectangle_overlap(a, b, gap: float) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (
        ax + aw + gap <= bx + 1e-7
        or bx + bw + gap <= ax + 1e-7
        or ay + ah + gap <= by + 1e-7
        or by + bh + gap <= ay + 1e-7
    )


def build_status_ladder(
    *,
    geometry_inspected: bool = True,
    geometry_nominal_fit: bool = False,
    slicer_capacity_verified: str | bool = 'not_run',
    gcode_sliced_and_guarded: str | bool = 'not_run',
    physical_print_tested: str | bool = 'not_run',
) -> dict:
    """Return the 5-stage verification ladder distinguishing geometry fit from slicing and printing."""
    def _norm(val):
        if isinstance(val, bool):
            return 'passed' if val else 'failed'
        return str(val)

    return {
        'geometry_inspected': _norm(geometry_inspected),
        'geometry_nominal_fit': _norm(geometry_nominal_fit),
        'slicer_capacity_verified': _norm(slicer_capacity_verified),
        'gcode_sliced_and_guarded': _norm(gcode_sliced_and_guarded),
        'physical_print_tested': _norm(physical_print_tested),
    }


def inspect_geometry(
    path: Path,
    bed=DEFAULT_BED_MM,
    margin: float = DEFAULT_MARGIN_MM,
    input_unit: str | None = None,
    split_stl: bool = False,
) -> dict:
    """Read-only inspection of STL/3MF physical dimensions, topology, and nominal bed fit."""
    path = Path(path)
    bed = tuple(float(v) for v in bed)
    margin = float(margin)
    parts, unit, mm_factor, warnings = load_parts(path, input_unit=input_unit, split_stl=split_stl)
    semantics = inspect_3mf_semantics(path)
    records = []
    all_lo = [float('inf'), float('inf'), float('inf')]
    all_hi = [float('-inf'), float('-inf'), float('-inf')]
    for p in parts:
        desc = describe_triangles(p['triangles'])
        records.append({
            'name': p['id'],
            'original_node': p['node'],
            'object_id': p['object_id'],
            **desc,
        })
        for a in range(3):
            all_lo[a] = min(all_lo[a], desc['bounds_min_mm'][a])
            all_hi[a] = max(all_hi[a], desc['bounds_max_mm'][a])
    span = [all_hi[a] - all_lo[a] for a in range(3)]
    usable_xy = [bed[0] - 2.0 * margin, bed[1] - 2.0 * margin]
    fits_as_authored = (
        span[0] <= usable_xy[0] + 1e-4
        and span[1] <= usable_xy[1] + 1e-4
        and span[2] <= bed[2] + 1e-4
    )
    positioned_in_bed = (
        all_lo[0] >= margin - 1e-4
        and all_lo[1] >= margin - 1e-4
        and all_lo[2] >= -1e-4
        and all_hi[0] <= bed[0] - margin + 1e-4
        and all_hi[1] <= bed[1] - margin + 1e-4
        and all_hi[2] <= bed[2] + 1e-4
    )
    return {
        'source': str(path),
        'source_sha256': _sha256(path),
        'source_units': unit,
        'unit_conversion_to_mm': mm_factor,
        'additional_scale_factor': 1.0,
        'scale_preserved_100_percent': True,
        'bed_mm': _r4(bed),
        'margin_mm': margin,
        'original_bounds_min_mm': _r4(all_lo),
        'original_bounds_max_mm': _r4(all_hi),
        'original_assembly_dimensions_mm': _r4(span),
        'part_count': len(parts),
        'parts': records,
        'semantics': semantics,
        'can_fit_as_authored_dimensions': bool(fits_as_authored),
        'is_currently_positioned_inside_nominal_bed_with_margin': bool(positioned_in_bed),
        'status_ladder': build_status_ladder(
            geometry_inspected=True,
            geometry_nominal_fit=fits_as_authored,
        ),
        'warnings': warnings,
    }


def pack_parts(
    parts,
    bed=DEFAULT_BED_MM,
    margin: float = DEFAULT_MARGIN_MM,
    gap: float = DEFAULT_GAP_MM,
    angles=None,
    max_nodes: int = DEFAULT_MAX_SEARCH_NODES,
):
    """Conservative non-overlapping axis-aligned bounding-rectangle packer at 100% scale."""
    bed = tuple(float(v) for v in bed)
    margin = float(margin)
    gap = float(gap)
    if margin * 2.0 >= min(bed[:2]):
        raise ValueError('Margin leaves no printable XY area')
    if angles is None:
        angles = [float(a) for a in range(0, 91, 5)] if len(parts) == 1 else [0.0, 90.0]
    else:
        angles = [float(a) for a in angles]

    part_extents = []
    for p in parts:
        _, _, ext = _bounds_from_triangles(p['triangles'])
        part_extents.append((ext[0] * ext[1], p, ext))
    sorted_parts = [item[1] for item in sorted(part_extents, key=lambda item: item[0], reverse=True)]

    options = []
    usable_w = bed[0] - 2.0 * margin
    usable_h = bed[1] - 2.0 * margin
    for p in sorted_parts:
        variants = []
        for angle in angles:
            norm_tris, ext, rot_meta = rotate_triangles_z(p['triangles'], angle)
            w, h, z = (float(v) for v in ext)
            if z > bed[2] + 1e-7:
                continue
            if w <= usable_w + 1e-7 and h <= usable_h + 1e-7:
                if not any(abs(w - r[1]) < 1e-5 and abs(h - r[2]) < 1e-5 for r in variants):
                    variants.append((float(angle), w, h, norm_tris, ext, rot_meta))
        if not variants:
            raise RuntimeError(
                f"{p['id']} cannot fit the {bed[0]:g}x{bed[1]:g}x{bed[2]:g} mm bed at 100% scale "
                f"(margin {margin:g} mm) across allowed Z angles; try additional rotations, "
                "re-orient in slicer, or use an approved split plan."
            )
        options.append(variants)

    if len(sorted_parts) == 1:
        best = min(options[0], key=lambda x: (round(max(x[1], x[2]), 6), round(x[1] * x[2], 6), abs(x[0])))
        angle, w, h, norm_tris, ext, rot_meta = best
        x = (bed[0] - w) / 2.0
        y = (bed[1] - h) / 2.0
        return [{
            'part': sorted_parts[0],
            'triangles': norm_tris,
            'extents': ext,
            'xy': (x, y),
            'angle': angle,
            'rot_meta': rot_meta,
        }], 1

    nodes = 0
    placed = []

    def search(i: int) -> bool:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            return False
        if i == len(sorted_parts):
            return True
        candidates_x = sorted({round(margin, 5)} | {round(v['xy'][0] + v['extents'][0] + gap, 5) for v in placed})
        candidates_y = sorted({round(margin, 5)} | {round(v['xy'][1] + v['extents'][1] + gap, 5) for v in placed})
        positions = sorted(((x, y) for y in candidates_y for x in candidates_x), key=lambda xy: (xy[1], xy[0]))
        for angle, w, h, norm_tris, ext, rot_meta in options[i]:
            for x, y in positions:
                if x + w > bed[0] - margin + 1e-7 or y + h > bed[1] - margin + 1e-7:
                    continue
                if any(
                    rectangle_overlap(
                        (x, y, w, h),
                        (p['xy'][0], p['xy'][1], float(p['extents'][0]), float(p['extents'][1])),
                        gap,
                    )
                    for p in placed
                ):
                    continue
                placed.append({
                    'part': sorted_parts[i],
                    'triangles': norm_tris,
                    'extents': ext,
                    'xy': (x, y),
                    'angle': angle,
                    'rot_meta': rot_meta,
                })
                if search(i + 1):
                    return True
                placed.pop()
        return False

    if not search(0):
        raise RuntimeError(
            f'No 100%-scale arrangement found after {nodes} search nodes on {bed[0]:g}x{bed[1]:g} mm bed '
            f'(margin {margin:g} mm, gap {gap:g} mm).'
        )
    return placed, nodes


def write_binary_stl(triangles, target: Path, header_text: str = 'Print Handy 100pct geometry export') -> Path:
    """Write a binary STL from triangles without altering coordinates."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = header_text.encode('ascii', errors='ignore')[:80].ljust(80, b'\0')
    with target.open('wb') as out:
        out.write(header)
        out.write(struct.pack('<I', len(triangles)))
        for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles:
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            norm = math.sqrt(nx * nx + ny * ny + nz * nz)
            if norm > 1e-12:
                nx, ny, nz = nx / norm, ny / norm, nz / norm
            else:
                nx, ny, nz = 0.0, 0.0, 0.0
            out.write(struct.pack('<12fH', nx, ny, nz, ax, ay, az, bx, by, bz, cx, cy, cz, 0))
    return target


def write_geometry_only_3mf(placed_objects, target: Path) -> Path:
    """Write a clean geometry-only multi-object 3MF in millimeters with distinct selectable objects."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
        b'</Types>\n'
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Target="/3D/3dmodel.model" Id="rel0" '
        b'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        b'</Relationships>\n'
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
        '  <metadata name="Title">Print Handy 100% Geometry-Only Arrangement</metadata>',
        '  <metadata name="GeometryOnly">true</metadata>',
        '  <resources>',
    ]
    build_lines = ['  <build>']
    for idx, obj in enumerate(placed_objects, 1):
        oid = str(idx)
        name = str(obj.get('name') or f'part_{idx}')
        safe_xml_name = (
            name.replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
        )
        lines.append(f'    <object id="{oid}" name="{safe_xml_name}" type="model">')
        lines.append('      <mesh>')
        lines.append('        <vertices>')
        v_map = {}
        v_list = []
        t_list = []
        for tri in obj['triangles']:
            idxs = []
            for pt in tri:
                key = (round(float(pt[0]), 6), round(float(pt[1]), 6), round(float(pt[2]), 6))
                v_idx = v_map.get(key)
                if v_idx is None:
                    v_idx = len(v_list)
                    v_map[key] = v_idx
                    v_list.append(key)
                idxs.append(v_idx)
            t_list.append(idxs)
        for vx, vy, vz in v_list:
            lines.append(f'          <vertex x="{vx:.6f}" y="{vy:.6f}" z="{vz:.6f}"/>')
        lines.append('        </vertices>')
        lines.append('        <triangles>')
        for v1, v2, v3 in t_list:
            lines.append(f'          <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')
        lines.append('        </triangles>')
        lines.append('      </mesh>')
        lines.append('    </object>')
        tx, ty, tz = obj.get('translation_mm', (0.0, 0.0, 0.0))
        tf_str = f'1 0 0 0 1 0 0 0 1 {float(tx):.6f} {float(ty):.6f} {float(tz):.6f}'
        build_lines.append(f'    <item objectid="{oid}" transform="{tf_str}" printable="1"/>')
    lines.append('  </resources>')
    build_lines.append('  </build>')
    lines.extend(build_lines)
    lines.append('</model>\n')
    model_xml = '\n'.join(lines).encode('utf-8')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('3D/3dmodel.model', model_xml)
    return target


def preserve_3mf_package_layout(source: Path, placed, target: Path, bed=DEFAULT_BED_MM, margin: float = DEFAULT_MARGIN_MM, input_unit: str | None = None) -> dict:
    """Rewrite build item transforms in a 3MF archive in-place so metadata/colors stay intact."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        payload = {name: archive.read(name) for name in names}
        root = ET.fromstring(payload['3D/3dmodel.model'])
        declared_unit = (root.get('unit') or 'millimeter').lower()
        mm_factor = UNIT_TO_MM.get(declared_unit, 1.0)
    if input_unit is not None:
        # The placements were computed from geometry converted by the override,
        # but preservation rewrites transforms against the archive's original
        # vertices in its declared units: with differing units the preserved
        # package would have different physical dimensions (P2-46).
        override_factor = UNIT_TO_MM.get(str(input_unit).lower())
        if override_factor is None:
            raise RuntimeError(f'unrecognized input unit {input_unit!r}; cannot preserve the 3MF package')
        if abs(override_factor - mm_factor) > 1e-9:
            raise RuntimeError(
                f'input unit {input_unit!r} differs from the archive unit {declared_unit!r}; '
                'the preserved 3MF package would not match the arranged geometry')
    if abs(mm_factor - 1.0) > 1e-6:
        raise RuntimeError('in-place 3MF transform preservation requires millimeter source units')

    placement_by_oid = {}
    for item in placed:
        oid = str(item['part']['object_id'])
        if oid in placement_by_oid:
            raise RuntimeError(f'duplicate build object_id {oid} cannot be rewritten unambiguously')
        c, s, rot_lo = item['rot_meta']
        px, py = item['xy']
        tx = px - rot_lo[0]
        ty = py - rot_lo[1]
        tz = -rot_lo[2]
        placement_by_oid[oid] = [c, s, 0.0, -s, c, 0.0, 0.0, 0.0, 1.0, tx, ty, tz]

    model_bytes = payload['3D/3dmodel.model']
    updated_oids = []

    def edit_item(match):
        line = match.group(0)
        if re.search(br'\bprintable="0"', line):
            return line
        m_oid = re.search(br'\bobjectid="(\d+)"', line)
        if m_oid is None:
            return line
        oid = m_oid.group(1).decode()
        if oid not in placement_by_oid:
            return line
        outer = placement_by_oid[oid]
        m_tf = re.search(br'\btransform="([^"]+)"', line)
        if m_tf is not None:
            inner = list(map(float, m_tf.group(1).split()))
            if len(inner) != 12:
                raise RuntimeError(f'invalid 3MF build item transform on object {oid}')
            composed = _compose_transforms(outer, inner)
            tf_bytes = b' '.join(f'{v:.6f}'.encode('ascii') for v in composed)
            line = line[:m_tf.start(1)] + tf_bytes + line[m_tf.end(1):]
        else:
            tf_bytes = b' '.join(f'{v:.6f}'.encode('ascii') for v in outer)
            line = re.sub(br'\s*/?>$', lambda end: b' transform="' + tf_bytes + b'"' + end.group(0), line, count=1)
        updated_oids.append(oid)
        return line

    new_model_bytes = re.sub(br'<item\b[^>]*>', edit_item, model_bytes)
    if sorted(updated_oids) != sorted(placement_by_oid):
        raise RuntimeError('in-place 3MF layout rewrite did not match all printable build items')
    payload['3D/3dmodel.model'] = new_model_bytes
    if 'Metadata/model_settings.config' in payload:
        from .color_geometry import _collapse_to_one_plate
        try:
            payload['Metadata/model_settings.config'] = _collapse_to_one_plate(
                payload['Metadata/model_settings.config'],
                set(placement_by_oid),
            )
        except Exception:
            pass
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as out:
        for name, data in payload.items():
            out.writestr(name, data)
    return {
        'preserved_3mf': target.name,
        'preserved_3mf_sha256': _sha256(target),
        'updated_object_ids': sorted(updated_oids),
    }


def _write_minimal_png(width: int, height: int, rgb_buffer: bytearray, target: Path) -> Path:
    """Write an RGB image buffer to a PNG file using only Python stdlib (zlib + struct)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_rows = bytearray()
    stride = width * 3
    for y in range(height):
        raw_rows.append(0)  # Filter type 0 (None)
        row_start = y * stride
        raw_rows.extend(rgb_buffer[row_start:row_start + stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)

    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_rows), level=6)
    png_bytes = (
        b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', ihdr)
        + chunk(b'IDAT', idat)
        + chunk(b'IEND', b'')
    )
    target.write_bytes(png_bytes)
    return target


def render_layout_preview_png(placed, bed, margin: float, target: Path, size_px: int = 640) -> Path:
    """Render a top-down 2D plate layout preview PNG with stdlib-only rasterization."""
    w_px = h_px = int(size_px)
    buf = bytearray([248, 249, 250] * (w_px * h_px))
    pad = 32
    bed_w, bed_h = float(bed[0]), float(bed[1])
    scale = min((w_px - 2 * pad) / max(bed_w, 1.0), (h_px - 2 * pad) / max(bed_h, 1.0))

    def to_px(x_mm: float, y_mm: float):
        px = int(round(pad + x_mm * scale))
        py = int(round(h_px - pad - y_mm * scale))
        return max(0, min(w_px - 1, px)), max(0, min(h_px - 1, py))

    def set_px(x: int, y: int, color):
        if 0 <= x < w_px and 0 <= y < h_px:
            idx = (y * w_px + x) * 3
            buf[idx] = color[0]
            buf[idx + 1] = color[1]
            buf[idx + 2] = color[2]

    def draw_line(x0: int, y0: int, x1: int, y1: int, color):
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            set_px(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def draw_rect(x0_mm: float, y0_mm: float, rw_mm: float, rh_mm: float, border_color, fill_color=None):
        x0, y1 = to_px(x0_mm, y0_mm)
        x1, y0 = to_px(x0_mm + rw_mm, y0_mm + rh_mm)
        lx, rx = min(x0, x1), max(x0, x1)
        ty, by = min(y0, y1), max(y0, y1)
        if fill_color is not None:
            for py in range(ty + 1, by):
                for px in range(lx + 1, rx):
                    set_px(px, py, fill_color)
        draw_line(lx, ty, rx, ty, border_color)
        draw_line(rx, ty, rx, by, border_color)
        draw_line(rx, by, lx, by, border_color)
        draw_line(lx, by, lx, ty, border_color)

    # Bed boundary and usable margin box
    draw_rect(0.0, 0.0, bed_w, bed_h, (30, 41, 59), fill_color=(255, 255, 255))
    if margin > 0:
        draw_rect(margin, margin, bed_w - 2 * margin, bed_h - 2 * margin, (203, 213, 225))

    palette = [
        ((37, 99, 235), (219, 234, 254)),
        ((5, 150, 105), (209, 250, 229)),
        ((217, 119, 6), (254, 243, 199)),
        ((124, 58, 237), (237, 233, 254)),
        ((225, 29, 72), (255, 228, 230)),
        ((8, 145, 178), (207, 250, 254)),
    ]
    for idx, p in enumerate(placed):
        border_col, fill_col = palette[idx % len(palette)]
        px, py = p['xy']
        pw, ph = p['extents'][0], p['extents'][1]
        draw_rect(px, py, pw, ph, border_col, fill_color=fill_col)
        pts_2d = [(pt[0] + px, pt[1] + py) for tri in p['triangles'] for pt in tri]
        hull = _hull(pts_2d)
        if len(hull) >= 2:
            for a, b in zip(hull, hull[1:] + hull[:1]):
                hx0, hy0 = to_px(a[0], a[1])
                hx1, hy1 = to_px(b[0], b[1])
                draw_line(hx0, hy0, hx1, hy1, border_col)

    return _write_minimal_png(w_px, h_px, buf, target)


def arrange_and_export(
    path: Path,
    outdir: Path,
    bed=DEFAULT_BED_MM,
    margin: float = DEFAULT_MARGIN_MM,
    gap: float = DEFAULT_GAP_MM,
    angles=None,
    input_unit: str | None = None,
    split_stl: bool = False,
    max_nodes: int = DEFAULT_MAX_SEARCH_NODES,
    allow_geometry_only_strip: bool = True,
) -> dict:
    """Arrange STL/3MF at 100% physical scale and export verified 3MF/STL/PNG/JSON artifacts."""
    path = Path(path)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    bed = tuple(float(v) for v in bed)
    margin = float(margin)
    gap = float(gap)

    parts, unit, mm_factor, warnings = load_parts(path, input_unit=input_unit, split_stl=split_stl)
    semantics = inspect_3mf_semantics(path)
    if semantics['has_negative_volumes']:
        raise RuntimeError(
            f'{path.name} contains 3MF negative-volume cutters ({semantics["cutter_pairs"]}); '
            'refusing geometry-only arrangement that would drop boolean modifiers.'
        )
    if (semantics['has_painted_triangles'] or semantics['has_multi_extruder']) and not allow_geometry_only_strip:
        raise RuntimeError(
            f'{path.name} contains multi-color / painted 3MF assignments; '
            'refusing geometry-only strip without explicit approval.'
        )

    baseline = inspect_geometry(path, bed=bed, margin=margin, input_unit=input_unit, split_stl=split_stl)
    result, nodes = pack_parts(parts, bed=bed, margin=margin, gap=gap, angles=angles, max_nodes=max_nodes)

    placed_objects = []
    combined_triangles = []
    export_records = []
    for item in result:
        px, py = item['xy']
        norm_tris = item['triangles']
        local_name = f"{item['part']['id']}_100pct.stl"
        write_binary_stl(norm_tris, outdir / local_name)
        placed_tris = [
            [(x + px, y + py, z) for x, y, z in tri]
            for tri in norm_tris
        ]
        combined_triangles.extend(placed_tris)
        desc = describe_triangles(placed_tris)
        placed_objects.append({
            'name': item['part']['id'],
            'triangles': norm_tris,
            'translation_mm': (px, py, 0.0),
            'placed_triangles': placed_tris,
            'extents': item['extents'],
            'xy': (px, py),
        })
        export_records.append({
            'name': item['part']['id'],
            'original_node': item['part']['node'],
            'object_id': item['part']['object_id'],
            'file': local_name,
            'z_rotation_degrees': round(float(item['angle']), 4),
            'lower_left_xy_mm': _r4((px, py)),
            'bounds_min_mm': desc['bounds_min_mm'],
            'bounds_max_mm': desc['bounds_max_mm'],
            'size_mm': _r4(item['extents']),
            'watertight': desc['watertight'],
            'manifold': desc['manifold'],
            'area_mm2': desc['area_mm2'],
        })

    out3mf = outdir / f'{path.stem}_100pct_arranged_GEOMETRY_ONLY.3mf'
    write_geometry_only_3mf(placed_objects, out3mf)

    # Round-trip verify the exported geometry-only 3MF
    rt_parts, _, _, _ = load_parts(out3mf)
    if len(rt_parts) != len(placed_objects):
        raise RuntimeError(f'3MF object count changed on round-trip: expected {len(placed_objects)}, got {len(rt_parts)}')
    orig_areas = sorted(round(describe_triangles(p['placed_triangles'])['area_mm2'], 2) for p in placed_objects)
    rt_areas = sorted(round(describe_triangles(p['triangles'])['area_mm2'], 2) for p in rt_parts)
    if any(abs(a - b) > 0.05 for a, b in zip(orig_areas, rt_areas)):
        raise RuntimeError('3MF surface areas not preserved on round-trip')

    combined_path = outdir / f'{path.stem}_100pct_arranged.stl'
    write_binary_stl(combined_triangles, combined_path)
    reloaded_stl = read_stl_triangles(combined_path)
    comb_lo, comb_hi, comb_ext = _bounds_from_triangles(combined_triangles)
    rel_lo, rel_hi, rel_ext = _bounds_from_triangles(reloaded_stl)
    if any(abs(comb_ext[a] - rel_ext[a]) > 0.003 for a in range(3)):
        raise RuntimeError('STL round-trip changed overall extents')
    if (
        rel_lo[0] < margin - 0.002
        or rel_lo[1] < margin - 0.002
        or rel_lo[2] < -0.002
        or rel_hi[0] > bed[0] - margin + 0.002
        or rel_hi[1] > bed[1] - margin + 0.002
        or rel_hi[2] > bed[2] + 0.002
    ):
        raise RuntimeError('Exported mesh exceeded the nominal usable build envelope')

    for i, a in enumerate(placed_objects):
        for b in placed_objects[i + 1:]:
            ax, ay = a['xy']
            bx, by = b['xy']
            if rectangle_overlap((ax, ay, a['extents'][0], a['extents'][1]), (bx, by, b['extents'][0], b['extents'][1]), gap - 0.001):
                raise RuntimeError('Unexpected bounding-rectangle collision after exporting')

    preserved_info = None
    if path.suffix.lower() == '.3mf' and not split_stl:
        try:
            preserved_target = outdir / f'{path.stem}_100pct_arranged_preserved.3mf'
            preserved_info = preserve_3mf_package_layout(path, result, preserved_target, bed=bed, margin=margin, input_unit=input_unit)
        except Exception as exc:
            warnings.append(f'In-place 3MF metadata preservation skipped: {exc}')

    preview_path = outdir / 'plate_preview.png'
    preview_error = None
    try:
        render_layout_preview_png(result, bed, margin, preview_path)
    except Exception as exc:
        preview_error = str(exc)

    reloaded_topo = mesh_topology(reloaded_stl)
    report = {
        **baseline,
        # The arrange pass's own warnings (its load_parts warnings plus the
        # preservation-skip note) supersede the baseline inspection's list,
        # which otherwise silently swallows the skip.
        'warnings': warnings,
        'result': 'geometry_fits_nominal_bed',
        'packing_search_nodes': nodes,
        'gap_mm': gap,
        'exported_combined_file': combined_path.name,
        'exported_combined_sha256': _sha256(combined_path),
        'exported_3mf_geometry_only': out3mf.name,
        'exported_3mf_geometry_only_sha256': _sha256(out3mf),
        'exported_3mf_object_count': len(rt_parts),
        **({'preserved_3mf_package': preserved_info} if preserved_info else {}),
        'preview_png': preview_path.name if preview_error is None else None,
        **({'preview_error': preview_error} if preview_error else {}),
        'arranged_bounds_min_mm': _r4(rel_lo),
        'arranged_bounds_max_mm': _r4(rel_hi),
        'arranged_dimensions_mm': _r4(rel_ext),
        'placed': export_records,
        'exported_mesh_watertight': bool(reloaded_topo['watertight']),
        'exported_mesh_manifold': bool(reloaded_topo['manifold']),
        'status_ladder': build_status_ladder(
            geometry_inspected=True,
            geometry_nominal_fit=True,
            slicer_capacity_verified='not_run',
            gcode_sliced_and_guarded='not_run',
            physical_print_tested='not_run',
        ),
        'checks': {
            'bed_bounds': True,
            'bbox_separation': True,
            'original_surface_areas_preserved': all(
                abs(
                    describe_triangles(r['part']['triangles'])['area_mm2']
                    - describe_triangles(r['triangles'])['area_mm2']
                ) <= max(0.02, 1e-4 * describe_triangles(r['part']['triangles'])['area_mm2'])
                for r in result
            ),
            'no_user_scaling': True,
            'stl_roundtrip_extent_delta_below_0_003mm': True,
            '3mf_roundtrip_object_count_area_extents': True,
        },
        'important_limitations': [
            'Nominal geometry fit is not validated print G-code or collision clearance.',
            'Parts are packed using conservative axis-aligned bounding boxes only.',
            'Geometry-only 3MF and STL exports do not preserve source printer/filament presets, colors, modifiers, or support painting.',
            'Slicer brim, supports, skirt, prime tower, and printer exclusion zones can reduce usable bed area.',
            'Watertight does not imply no self-intersections or printable snap fits.',
        ],
    }
    (outdir / 'validation.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def probe_single_plate_rearrange(
    path: Path,
    bed=DEFAULT_BED_MM,
    margin: float = DEFAULT_MARGIN_MM,
    gap: float = DEFAULT_GAP_MM,
) -> dict | None:
    """Return a 100%-scale single-plate multi-object rearrangement option if feasible, else None."""
    path = Path(path)
    try:
        semantics = inspect_3mf_semantics(path)
        if semantics['has_negative_volumes']:
            return None
        parts, unit, mm_factor, warnings = load_parts(path)
        if len(parts) < 2:
            return None
        placed, nodes = pack_parts(parts, bed=bed, margin=margin, gap=gap)
    except Exception:
        return None
    min_x = min(p['xy'][0] for p in placed)
    min_y = min(p['xy'][1] for p in placed)
    max_x = max(p['xy'][0] + p['extents'][0] for p in placed)
    max_y = max(p['xy'][1] + p['extents'][1] for p in placed)
    max_z = max(p['extents'][2] for p in placed)
    span = [round(max_x - min_x, 2), round(max_y - min_y, 2), round(max_z, 2)]
    return {
        'id': 'rearrange_100',
        'scale': 1.0,
        'rearrange_plate': True,
        'part_count': len(parts),
        'turned_parts': sum(1 for p in placed if abs(float(p.get('angle') or 0.0)) > 1e-6),
        'size_mm': span,
        'label': f'100% · rearrange 1 plate ({len(parts)} parts, {span[0]:.0f}×{span[1]:.0f}×{span[2]:.0f} mm)',
        'search_nodes': nodes,
        'source_units': unit,
        'unit_conversion_to_mm': mm_factor,
        'warnings': warnings,
    }


def convert_stl_to_geometry_3mf(
    source_stl: Path,
    target_3mf: Path,
    *,
    separate_shells: bool = False,
    input_unit: str = 'mm',
    center_bed=None,
) -> dict:
    """Convert an STL into a geometry-only 3MF without resizing, optionally splitting shells."""
    source_stl = Path(source_stl)
    target_3mf = Path(target_3mf)
    parts, unit, mm_factor, warnings = load_parts(source_stl, input_unit=input_unit, split_stl=separate_shells)
    all_lo = [min( describe_triangles(p['triangles'])['bounds_min_mm'][a] for p in parts) for a in range(3)]
    all_hi = [max( describe_triangles(p['triangles'])['bounds_max_mm'][a] for p in parts) for a in range(3)]
    span = [all_hi[a] - all_lo[a] for a in range(3)]
    shift = [0.0, 0.0, 0.0]
    if center_bed is not None:
        bx, by = float(center_bed[0]), float(center_bed[1])
        if span[0] > bx + 1e-4 or span[1] > by + 1e-4:
            raise ValueError('Assembly does not fit supplied XY bed. No scaling performed.')
        shift = [bx / 2.0 - (all_lo[0] + all_hi[0]) / 2.0, by / 2.0 - (all_lo[1] + all_hi[1]) / 2.0, -all_lo[2]]

    placed_objects = []
    for p in parts:
        tris = [
            [(pt[0] + shift[0], pt[1] + shift[1], pt[2] + shift[2]) for pt in tri]
            for tri in p['triangles']
        ]
        placed_objects.append({
            'name': p['id'],
            'triangles': tris,
            'translation_mm': (0.0, 0.0, 0.0),
        })
    write_geometry_only_3mf(placed_objects, target_3mf)
    rt_parts, _, _, _ = load_parts(target_3mf)
    if len(rt_parts) != len(parts):
        raise RuntimeError(f'3MF object count mismatch: expected {len(parts)}, got {len(rt_parts)}')
    return {
        'source': str(source_stl),
        'target': str(target_3mf),
        'source_sha256': _sha256(source_stl),
        'target_sha256': _sha256(target_3mf),
        'part_count': len(parts),
        'source_units': unit,
        'unit_conversion_to_mm': mm_factor,
        'dimensions_mm': _r4(span),
        'translation_mm': _r4(shift),
        'warnings': warnings,
    }
