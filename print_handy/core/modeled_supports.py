"""Modeled supports inside a 3MF: detection and deterministic mesh measurement.

Some designers ship supports as a separate object, for example
"REMOVABLE SUPPORTS (delete object to remove)". The slicer still reports
floating regions for such a file, because it does not treat that object as
support. This module finds those objects by name and measures, from the mesh
alone (no AI), for each horizontal overhang of the part:

- its Z height and area,
- the gap between the highest support tops below it and the overhang, and
- roughly how much of the overhang area those support tops touch.

Areas come from a 0.25 mm XY raster, so they are approximate by design. The
touched area is the XY overlap of the support tops and the overhang; any part
of a support top that lies outside the overhang footprint does not count.
"""

import re
import xml.etree.ElementTree as ET
import zipfile

import numpy as np

from print_handy.core import orca_adapter

SUPPORT_NAME = re.compile(r'\bsupports?\b', re.IGNORECASE)
CELL = 0.25           # raster cell, mm
FLAT = 0.98           # |normal z| above this is a horizontal facet
LEVEL_TOL = 0.02      # facets within this Z belong to one level, mm
MAX_GAP = 2.0         # support tops further below an overhang do not count, mm
MIN_AREA = 5.0        # ignore overhang levels smaller than this, mm2


def _local(tag):
    return tag.split('}')[-1]


def objects(source):
    """[(object_id, name)] for every build object in the 3MF's root model."""
    with zipfile.ZipFile(source) as archive:
        root = ET.fromstring(archive.read('3D/3dmodel.model'))
    names = {}
    built = []
    for node in root.iter():
        tag = _local(node.tag)
        if tag == 'object' and node.get('id'):
            names[node.get('id')] = node.get('name') or ''
        elif tag == 'item' and node.get('objectid'):
            built.append(node.get('objectid'))
    return [(oid, names.get(oid, '')) for oid in dict.fromkeys(built)]


def support_objects(source):
    """Build objects whose name says they are supports."""
    return [(oid, name) for oid, name in objects(source) if SUPPORT_NAME.search(name)]


def _triangles(source, ids):
    parts = [np.asarray(orca_adapter._object_triangles(source, oid), dtype=float) for oid in ids]
    parts = [p.reshape(-1, 3, 3) for p in parts if p.size]
    return np.concatenate(parts) if parts else np.zeros((0, 3, 3))


def _normals(tris):
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(n, axis=1)
    return n / np.maximum(length, 1e-12)[:, None]


class _Grid:
    def __init__(self, tris):
        pts = tris.reshape(-1, 3)[:, :2]
        self.lo = pts.min(0) - 1
        self.w, self.h = np.ceil((pts.max(0) + 1 - self.lo) / CELL).astype(int)

    def raster(self, tris):
        mask = np.zeros((self.h, self.w), bool)
        for tri in tris[:, :, :2]:
            x0, y0 = np.floor((tri.min(0) - self.lo) / CELL).astype(int)
            x1, y1 = np.ceil((tri.max(0) - self.lo) / CELL).astype(int)
            xs = self.lo[0] + (np.arange(x0, x1) + 0.5) * CELL
            ys = self.lo[1] + (np.arange(y0, y1) + 0.5) * CELL
            X, Y = np.meshgrid(xs, ys)
            (ax, ay), (bx, by), (cx, cy) = tri
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            if abs(d) < 1e-12:
                continue
            l1 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) / d
            l2 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) / d
            mask[y0:y1, x0:x1] |= (l1 >= -1e-9) & (l2 >= -1e-9) & (1 - l1 - l2 >= -1e-9)
        return mask


def measure(source):
    """The modeled-support report for a 3MF, or None when it has no support object.

    {'objects': [names], 'levels': [{'z', 'area_mm2', 'touch_mm2', 'touch_pct',
    'gap_mm' (None when unsupported)}]}, levels ordered top to bottom.
    """
    found = support_objects(source)
    if not found:
        return None
    support_ids = {oid for oid, _ in found}
    part = _triangles(source, [oid for oid, _ in objects(source) if oid not in support_ids])
    sup = _triangles(source, sorted(support_ids))
    report = {'objects': [name for _, name in found], 'levels': []}
    if not len(part):
        return report
    grid = _Grid(np.concatenate([part, sup]) if len(sup) else part)
    cell_area = CELL * CELL
    n = _normals(part)
    down, up = n[:, 2] < -FLAT, n[:, 2] > FLAT
    dz, uz = part[down][:, :, 2].mean(1), part[up][:, :, 2].mean(1)
    zmin = part[:, :, 2].min()
    if len(sup):
        sn = _normals(sup)
        stops = sup[sn[:, 2] > FLAT]
        sz = stops[:, :, 2].mean(1)
    else:
        stops, sz = sup, np.zeros(0)
    for z in sorted({round(float(v), 2) for v in dz}, reverse=True):
        if z < zmin + 0.05:
            continue
        overhang = grid.raster(part[down][np.abs(dz - z) < LEVEL_TOL])
        overhang &= ~grid.raster(part[up][np.abs(uz - z) < LEVEL_TOL * 2])
        area = overhang.sum() * cell_area
        if area < MIN_AREA:
            continue
        below = sz[(sz < z) & (sz > z - MAX_GAP)]
        level = {'z': z, 'area_mm2': round(area, 1), 'touch_mm2': 0.0,
                 'touch_pct': 0.0, 'gap_mm': None}
        if len(below):
            top = below.max()
            tops = grid.raster(stops[np.abs(sz - top) < LEVEL_TOL])
            touch = (tops & overhang).sum() * cell_area
            if touch:
                level.update(touch_mm2=round(touch, 1), touch_pct=round(100 * touch / area, 1),
                             gap_mm=round(z - float(top), 2))
        report['levels'].append(level)
    return report


def describe(report):
    """Plain-text lines for a chat message, or [] when there is nothing to say."""
    if not report:
        return []
    names = ', '.join(f'"{name}"' for name in report['objects'])
    lines = [f'This file has modeled supports: {names}.']
    for level in report['levels']:
        if level['gap_mm'] is None:
            lines.append(f"- Overhang at Z {level['z']:g} mm ({level['area_mm2']:.0f} mm2): "
                         'no modeled support under it.')
        else:
            lines.append(f"- Overhang at Z {level['z']:g} mm ({level['area_mm2']:.0f} mm2): "
                         f"support tops sit {level['gap_mm']:.2f} mm below it and touch about "
                         f"{level['touch_mm2']:.0f} mm2 ({level['touch_pct']:.1f}%).")
    return lines


def safe_describe(source):
    """describe(measure(source)), or [] for a missing or unreadable file."""
    try:
        return describe(measure(source)) if source else []
    except Exception:
        return []
