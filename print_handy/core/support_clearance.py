"""Support clearance at intake: undersides that supports cannot reach (#911).

Reads the 3MF as it will be sliced: every build item, its components and
their transforms, in printer coordinates (the arranged source). For each
downward facing surface of a printed part it measures, by casting vertical
rays on a regular XY grid, the clearance to whatever is directly below it:
the build plate or the top of another part. No AI, no slicer.

Structured findings (``analyze(source)['findings']``):

- ``INSUFFICIENT_SUPPORT_CLEARANCE``: a connected underside floats less than
  ``gap_mm`` (default 1.0 mm) above the plate or a part below, across at least
  ``area_mm2`` (default 500 mm2, trigger ``area``), or as a long narrow
  region whose span reaches ``span_mm`` (trigger ``span``). Slicer supports
  cannot fit in such a gap and a raft does not change it. Sloped undersides
  are measured per ray, not only horizontal facets. A gap smaller than the
  first layer height (``first_layer_mm``) is contact, not a gap: the first
  layer's squish closes it, so it is never a low-clearance finding.
- ``INEFFECTIVE_MODELED_SUPPORT``: a modeled support object (named support
  or 3MF type support) sits under an overhang but touches less than
  ``min_coverage`` of it, or intersects a printed part.
- ``BELOW_BED_GEOMETRY``: a part extends below Z 0; the volume that would be
  cut is stated.
- ``SINGLE_MESH_CANTILEVER`` (warning only, #922): one mesh whose downward
  overhang dwarfs its bed contact — baked-in pillars are part of the mesh,
  not a modeled support object.

Regions also include ``supportable_overhang`` entries (clearance at least
``gap_mm``, area at least ``area_mm2`` or span at least ``span_mm``): not
risky at pre-check, but the required targets for post-slice
``;TYPE:Support`` coverage verification (``verify_support_coverage``), and
``sloped_wedge_root`` entries: narrow sloped strips rooting into a
zero-clearance anchor edge, excluded from the clearance finding.

Every region persists a compact ``footprint`` (#963): the underside's
occupied cells aggregated to about 1 mm (``footprint_mm`` pitch, capped,
each entry ``[x, y, share]``), so post-slice coverage grades the cells the
underside actually occupies instead of its bounding box. Low-clearance
regions carry ``flagged``: True only when the region met the finding
thresholds, and only flagged regions are post-slice coverage targets.

Each region also carries fix estimates (extend the underside down, sink the
part, reorient, print anyway). These are screening thresholds, not a promise
of printability: the post-slice toolpath check consumes ``regions``.
"""

import hashlib
import json
import math
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote

import numpy as np

VERSION = 1
CODE_CLEARANCE = 'INSUFFICIENT_SUPPORT_CLEARANCE'
CODE_MODELED = 'INEFFECTIVE_MODELED_SUPPORT'
CODE_BELOW_BED = 'BELOW_BED_GEOMETRY'
CODE_CANTILEVER = 'SINGLE_MESH_CANTILEVER'
# A sloped face whose downward normal sits within this angle of vertical is
# in the self-supporting band for the wedge-root exclusion (#922 gap 1):
# cos(55 degrees) ~= 0.574. A near-horizontal face (|nz| above 0.95) is not
# a sloped wedge face even though its normal is also near vertical.
WEDGE_COS_MIN = 0.574
WEDGE_COS_MAX = 0.95
WEDGE_WIDTH_MM = 2.0     # a low-clearance strip narrower than this can be a wedge root
CANTILEVER_RATIO = 5.0   # single-mesh overhang / bed-contact ratio that names a cantilever risk
CANTILEVER_MIN_OVERHANG_MM2 = 500.0

GAP_MM = 1.0             # an underside closer than this to what is below cannot take support
AREA_MM2 = 500.0         # connected low-clearance area that triggers the finding
SPAN_MM = 15.0           # a smaller low-clearance region this long still triggers it
CONTACT_MM = 0.05        # clearance at or below this is contact, not a gap
#                          (and so is any gap under the first layer height, see first_layer_mm)
OVERHANG_NZ = 0.5        # downward faces within 60 degrees of horizontal
MIN_COVERAGE = 0.10      # modeled supports must touch at least this share of the overhang
CONTACT_WINDOW_MM = 0.6  # a support top this far below an overhang still counts as touching
MIN_OVERHANG_MM2 = 20.0  # smaller overhang regions are not checked for modeled supports
CELL_MM = 0.5            # ray grid pitch
MAX_CELLS = 1_500_000    # the grid pitch grows past CELL_MM to stay under this
FOOTPRINT_MM = 1.0       # persisted underside footprint pitch (#963 item 1)
FOOTPRINT_SHARE_MIN = 0.25  # a footprint/grading cell must hold at least this share of underside
FOOTPRINT_MAX_CELLS = 2048  # the footprint pitch doubles until a region fits under this cap
LINK_DZ = 0.6            # neighboring rays within this Z belong to one surface

SUPPORT_NAME = re.compile(r'\bsupports?\b', re.IGNORECASE)
_SKIP_TYPES = {'other', 'surface'}
_ZOFF = 1000.0
_STRIDE = 4000.0

LIMITATIONS = [
    'Screening thresholds only; they do not prove printability.',
    'Geometry is read as authored in the 3MF; a slicer that drops or lifts objects can change gaps.',
    'XY access for support towers and actual support toolpaths are checked after slicing, not here.',
]


def _local(tag):
    return tag.split('}')[-1]


def _kids(node, name):
    return [c for c in node if _local(c.tag) == name]


def _matrix(node):
    raw = node.get('transform')
    if not raw:
        return np.eye(4)
    t = [float(v) for v in raw.split()]
    if len(t) != 12:
        raise ValueError('invalid 3MF transform')
    return np.array([[t[0], t[1], t[2], 0], [t[3], t[4], t[5], 0],
                     [t[6], t[7], t[8], 0], [t[9], t[10], t[11], 1]], float)


def leaves(source):
    """Every printed mesh leaf in printer coordinates.

    [{'item', 'object', 'name', 'object_id', 'leaf_id', 'support', 'material', 'tris'}]
    ``tris`` is (n, 3, 3). Components and build transforms are composed, a
    mirroring transform keeps outward normals, Orca/Bambu cutter and modifier
    volumes and non-printable items are left out.
    """
    from print_handy.core import source_colors
    out = []
    if str(source).lower().endswith('.stl'):
        # A single-part STL is one unnamed mesh (#922 gaps 3 and 8): it gets
        # the same leaf shape a one-object 3MF without pid/pindex gets, so
        # every downstream check treats the two identically.
        from print_handy.core import orca_adapter
        tris = orca_adapter.read_stl_triangles(source)
        tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
        if len(tris):
            name = Path(source).stem or 'STL'
            out.append({'item': 0, 'object': name, 'name': name, 'object_id': '1',
                        'leaf_id': '1', 'support': bool(SUPPORT_NAME.search(name)),
                        'material': [None, None], 'tris': tris})
        return out
    with zipfile.ZipFile(source) as archive:
        try:
            cutters = source_colors.cutter_ids(archive)
        except Exception:
            cutters = {}
        # PrusaSlicer modifier, negative and support volumes (#1260): ranges of
        # one mesh keep only the model-part triangles, component volumes are
        # cutters. A model config that cannot be read refuses (strict).
        from print_handy.core.orca_adapter import _prusa_printed_ranges
        prusa_keep, prusa_cutters = _prusa_printed_ranges(archive)
        cutters = {**cutters, **prusa_cutters}
        models = {}

        def model(name):
            name = name.lstrip('/')
            if name not in models:
                root = ET.fromstring(archive.read(name))
                if root.get('unit', 'millimeter') != 'millimeter':
                    raise ValueError('3MF unit is not millimeter')
                objs = {}
                for res in _kids(root, 'resources'):
                    for obj in _kids(res, 'object'):
                        objs[obj.get('id')] = obj
                models[name] = (root, objs)
            return models[name]

        def collect(name, oid, matrix, item, top, top_oid, support, active):
            if (name, oid) in active:
                raise ValueError('cyclic 3MF component reference')
            obj = model(name)[1].get(oid)
            if obj is None:
                raise ValueError(f'unresolved 3MF object {oid}')
            kind = (obj.get('type') or 'model').lower()
            if kind in _SKIP_TYPES:
                return
            label = obj.get('name') or top
            support = support or 'support' in kind or bool(SUPPORT_NAME.search(label or ''))
            mesh = next(iter(_kids(obj, 'mesh')), None)
            if mesh is not None:
                if (top_oid, oid) in cutters:
                    return
                verts = np.array([[float(v.get('x')), float(v.get('y')), float(v.get('z'))]
                                  for vs in _kids(mesh, 'vertices') for v in _kids(vs, 'vertex')],
                                 float).reshape(-1, 3)
                faces = np.array([[int(t.get('v1')), int(t.get('v2')), int(t.get('v3'))]
                                  for ts in _kids(mesh, 'triangles') for t in _kids(ts, 'triangle')],
                                 int).reshape(-1, 3)
                if name.lstrip('/') == '3D/3dmodel.model' and oid == top_oid and oid in prusa_keep:
                    from print_handy.core.orca_adapter import _printed_faces
                    faces = _printed_faces(faces, prusa_keep[oid])
                if len(verts) and len(faces):
                    world = (np.c_[verts, np.ones(len(verts))] @ matrix)[:, :3]
                    if np.linalg.det(matrix[:3, :3]) < 0:
                        faces = faces[:, ::-1]
                    out.append({'item': item, 'object': top, 'name': label, 'object_id': top_oid,
                                'leaf_id': oid, 'support': support,
                                'material': [obj.get('pid'), obj.get('pindex')],
                                'tris': world[faces]})
            for wrap in _kids(obj, 'components'):
                for comp in _kids(wrap, 'component'):
                    path = next((v for k, v in comp.attrib.items() if _local(k) == 'path'), name)
                    collect(path.lstrip('/'), comp.get('objectid'), _matrix(comp) @ matrix,
                            item, top, top_oid, support, active | {(name, oid)})

        root = model('3D/3dmodel.model')[0]
        for build in _kids(root, 'build'):
            for i, item in enumerate(_kids(build, 'item')):
                if item.get('printable', '1') == '0':
                    continue
                oid = item.get('objectid')
                obj = model('3D/3dmodel.model')[1].get(oid)
                top = (obj.get('name') if obj is not None else None) or f'Object {oid}'
                collect('3D/3dmodel.model', oid, _matrix(item), i, top, oid, False, frozenset())
    return out


def _raster(tris, lo, cell, w, h):
    """Rays at cell centers that hit each triangle: (cell index, z, triangle index)."""
    xy = tris[:, :, :2]
    i0 = np.ceil((xy[:, :, 0].min(1) - lo[0]) / cell - 0.5).astype(np.int64)
    i1 = np.floor((xy[:, :, 0].max(1) - lo[0]) / cell - 0.5).astype(np.int64)
    j0 = np.ceil((xy[:, :, 1].min(1) - lo[1]) / cell - 0.5).astype(np.int64)
    j1 = np.floor((xy[:, :, 1].max(1) - lo[1]) / cell - 0.5).astype(np.int64)
    sx, sy = i1 - i0 + 1, j1 - j0 + 1
    size = np.maximum(sx, sy)
    cells, zs, ids = [], [], []
    keep = size > 0
    bucket = np.zeros(len(tris), np.int64)
    bucket[keep] = np.ceil(np.log2(np.maximum(size[keep], 1))).astype(np.int64)
    for b in np.unique(bucket[keep]):
        s = 1 << int(b)
        sel = np.nonzero(keep & (bucket == b))[0]
        oy, ox = np.divmod(np.arange(s * s), s)
        step = max(1, 2_000_000 // (s * s))
        for k in range(0, len(sel), step):
            t = sel[k:k + step]
            ix = i0[t, None] + ox[None]
            iy = j0[t, None] + oy[None]
            ok = (ox[None] < sx[t, None]) & (oy[None] < sy[t, None]) & (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
            px = lo[0] + (ix + 0.5) * cell
            py = lo[1] + (iy + 0.5) * cell
            (ax, ay), (bx, by), (cx, cy) = [(xy[t, v, 0, None], xy[t, v, 1, None]) for v in range(3)]
            d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
            dsafe = np.where(np.abs(d) < 1e-12, 1.0, d)
            l1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / dsafe
            l2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / dsafe
            l3 = 1 - l1 - l2
            ok &= (np.abs(d) >= 1e-12) & (l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9)
            z = l1 * tris[t, 0, 2, None] + l2 * tris[t, 1, 2, None] + l3 * tris[t, 2, 2, None]
            r, c = np.nonzero(ok)
            cells.append(iy[r, c] * w + ix[r, c])
            zs.append(z[r, c])
            ids.append(t[r])
    if not cells:
        return np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64)
    return np.concatenate(cells), np.concatenate(zs), np.concatenate(ids)


def _key(cell, z):
    return cell * _STRIDE + (z + _ZOFF)


class _Events:
    """Sorted ray hits of one class of parts, for 'what is below' and 'am I inside' queries."""

    def __init__(self, cell, z, sign, leaf=None):
        order = np.lexsort((z, cell))
        self.cell, self.z, self.sign = cell[order], z[order], sign[order]
        self.leaf = (np.full(len(cell), -1) if leaf is None else leaf)[order]
        self.keys = _key(self.cell, self.z)
        self.cum = np.concatenate([[0], np.cumsum(self.sign)])
        up = self.sign < 0
        self.ucell, self.uz, self.uleaf = self.cell[up], self.z[up], self.leaf[up]
        self.ukeys = self.keys[up]

    def top_below(self, cell, z, with_leaf=False):
        """Highest upward surface at or below z in the same ray, or NaN.

        ``with_leaf`` also returns the leaf that surface belongs to (-1 for none).
        """
        if not len(self.ukeys):
            empty = np.full(len(cell), np.nan)
            return (empty, np.full(len(cell), -1)) if with_leaf else empty
        idx = np.searchsorted(self.ukeys, _key(cell, z), side='right') - 1
        safe = np.maximum(idx, 0)
        ok = (idx >= 0) & (self.ucell[safe] == cell)
        top = np.where(ok, self.uz[safe], np.nan)
        if with_leaf:
            return top, np.where(ok, self.uleaf[safe], -1)
        return top

    def depth(self, cell, z):
        """How many solids of this class contain the point (entries minus exits below it)."""
        if not len(self.keys):
            return np.zeros(len(cell), np.int64)
        hi = np.searchsorted(self.keys, _key(cell, z), side='right')
        lo = np.searchsorted(self.keys, _key(cell, -_ZOFF + 1e-6), side='left')
        return self.cum[hi] - self.cum[lo]


def _components(leaf, cell, z, w):
    """Connected surface labels: 4-neighbor rays of the same leaf within LINK_DZ in Z."""
    n = len(cell)
    if not n:
        return np.zeros(0, np.int64)
    ncell = float(cell.max() + w + 2)
    k = (leaf * ncell + cell) * _STRIDE + (z + _ZOFF)
    order = np.argsort(k)
    ks, cs, ls = k[order], cell[order], leaf[order]
    src, dst = [], []
    for step in (1, w):
        target_cell = cell + step
        q = (leaf * ncell + target_cell) * _STRIDE + (z + _ZOFF)
        pos = np.searchsorted(ks, q)
        for cand in (pos - 1, pos):
            c = np.clip(cand, 0, n - 1)
            ok = ((cand >= 0) & (cand < n) & (cs[c] == target_cell) & (ls[c] == leaf)
                  & (np.abs(ks[c] - q) <= LINK_DZ))
            if step == 1:
                ok &= (cell % w) != (w - 1)
            src.append(np.nonzero(ok)[0])
            dst.append(order[c[ok]])
    a, b = np.concatenate(src), np.concatenate(dst)
    lab = np.arange(n)
    while True:
        m = np.minimum(lab[a], lab[b])
        new = lab.copy()
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]
        if np.array_equal(new, lab):
            break
        lab = new
    return np.unique(lab, return_inverse=True)[1]


def _thin_strip(cells, w, cell, width_mm):
    """True when a region's ray cells form a strip no wider than ``width_mm``.

    A strip n cells across disappears after ceil(n / 2) four-neighbor
    erosions, so the region is thin when it is gone after
    floor(width_mm / (2 * cell)) of them. A wide surface keeps its interior
    cells however small its rise is. Diagonal strips erode more slowly
    under four-neighbor erosion, which only makes this stricter.
    """
    cur = np.unique(np.asarray(cells, np.int64))
    for _ in range(int(width_mm // (2 * cell))):
        if not len(cur):
            break
        keep = np.ones(len(cur), bool)
        for d in (-1, 1, -w, w):
            nb = cur + d
            pos = np.clip(np.searchsorted(cur, nb), 0, len(cur) - 1)
            keep &= cur[pos] == nb
        cur = cur[keep]
    return not len(cur)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def profile_first_layer_mm(printer_id=None):
    """The first layer height the printer's tuned profile slices with, or None.

    Every slice sets ``initial_layer_print_height`` from the tuned profile of
    the printer's recorded nozzle (orca_adapter.slice_model), so that is the
    height whose squish closes a smaller gap. None when the printer record or
    its nozzle profile cannot be read; the analysis then keeps CONTACT_MM only.
    """
    try:
        from . import printer_state
        from .nozzle import get_tuned_nozzle_profile
        try:
            diameter = printer_state.load(printer_id)['nozzle']['diameter_mm']
        except FileNotFoundError:
            from .. import settings
            diameter = settings.current()['printer']['nozzle_diameter_mm']
        profile = get_tuned_nozzle_profile(float(diameter))
        return float(profile.default_initial_layer_height) if profile is not None else None
    except Exception:  # noqa: BLE001 - an unreadable record keeps the plain contact threshold
        return None


def job_first_layer_mm(job, printer_id=None):
    """The first layer height this job slices with: its own ``initial_layer_height``
    profile edit (edit_plan.PROFILE_SETTINGS) when it has one, else the profile's."""
    edited = (((job or {}).get('edit_settings') or {}).get('profile') or {}).get('initial_layer_height')
    try:
        if edited is not None and float(edited) > 0:
            return float(edited)
    except (TypeError, ValueError):
        pass
    return profile_first_layer_mm(printer_id)


def analyze(source, gap_mm=GAP_MM, area_mm2=AREA_MM2, span_mm=SPAN_MM, min_coverage=MIN_COVERAGE,
            first_layer_mm=None):
    """The structured support clearance report for a 3MF (see the module docstring).

    ``first_layer_mm`` is the first layer height the job slices with: a gap
    no taller than it is read as contact. None keeps CONTACT_MM alone.
    """
    return _analyze_parts(leaves(source), _sha256(source), gap_mm=gap_mm, area_mm2=area_mm2,
                          span_mm=span_mm, min_coverage=min_coverage, first_layer_mm=first_layer_mm,
                          authored_supports=authored_supports(source))


def authored_supports(source):
    """The designer's supports setting saved in a Bambu/Orca project 3MF: True, False or None.

    Read from ``Metadata/project_settings.config`` (``enable_support``). None
    for a plain 3MF, an STL, or a project that does not say.
    """
    try:
        if not zipfile.is_zipfile(source):
            return None
        with zipfile.ZipFile(source) as archive:
            try:
                raw = archive.read('Metadata/project_settings.config')
            except KeyError:
                return None
        value = json.loads(raw.decode('utf-8', errors='replace')).get('enable_support')
    except Exception:  # noqa: BLE001 - an unreadable project says nothing about supports
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    return str(value).strip().lower() in ('1', 'true')


def rotated_report(source, matrix, first_layer_mm=None):
    """The clearance report for ``source`` after a rotation (#922 gap 7).

    ``matrix`` is the 9-value row-major rotation edit_plan records in the
    job's orientation. The rotated geometry is shifted so its lowest point
    sits on the plate, which is how the slicer will place it; the report is
    marked ``orientation_applied`` so callers know it describes the rotated
    pose, not the file as authored. ``first_layer_mm`` defaults to the
    printer profile's first layer height, as in safe_analyze.
    """
    parts = []
    rot = np.asarray(matrix, dtype=float).reshape(3, 3)
    for p in leaves(source):
        tris = np.asarray(p['tris'], dtype=float) @ rot.T
        parts.append({**p, 'tris': tris})
    if parts:
        low = min(float(p['tris'][:, :, 2].min()) for p in parts)
        for p in parts:
            p['tris'][:, :, 2] -= low
    if first_layer_mm is None:
        first_layer_mm = profile_first_layer_mm()
    report = _analyze_parts(parts, _sha256(source), first_layer_mm=first_layer_mm)
    report['orientation_applied'] = True
    return report


def _analyze_parts(parts, source_sha256, gap_mm=GAP_MM, area_mm2=AREA_MM2, span_mm=SPAN_MM,
                   min_coverage=MIN_COVERAGE, first_layer_mm=None, authored_supports=None):
    first_layer_mm = float(first_layer_mm) if first_layer_mm else None
    report = {'version': VERSION, 'source_sha256': source_sha256,
              'authored_supports': authored_supports,
              'thresholds': {'gap_mm': gap_mm, 'area_mm2': area_mm2, 'span_mm': span_mm,
                             'contact_mm': CONTACT_MM, 'min_coverage': min_coverage,
                             'first_layer_mm': first_layer_mm},
              'objects': [{'object': p['object'], 'name': p['name'], 'support': p['support'],
                           'material': p['material'], 'triangles': int(len(p['tris']))} for p in parts],
              'regions': [], 'findings': [], 'limitations': list(LIMITATIONS)}
    if not parts:
        report.update(cell_mm=CELL_MM, risky=False, low_gap_flags=0)
        return report
    allpts = np.concatenate([p['tris'].reshape(-1, 3) for p in parts])
    span = allpts[:, :2].max(0) - allpts[:, :2].min(0) + 2
    cell = max(CELL_MM, float(np.sqrt(span[0] * span[1] / MAX_CELLS)))
    lo = allpts[:, :2].min(0) - 1
    w, h = (np.ceil(span / cell).astype(int) + 1).tolist()
    report['cell_mm'] = round(cell, 4)
    area_cell = cell * cell

    rows = []
    for li, p in enumerate(parts):
        t = p['tris']
        nrm = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        nz = nrm[:, 2] / np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)
        flat = np.abs(nz) > 1e-6
        idx = np.nonzero(flat)[0]
        c, z, ti = _raster(t[idx], lo, cell, w, h)
        tri = idx[ti]
        sign = np.where(nz[tri] < 0, 1, -1)
        # One hit per ray, surface and leaf: a ray through a shared edge hits both triangles.
        _, first = np.unique(np.stack([c, np.round(z, 4), sign]), axis=1, return_index=True)
        rows.append((np.full(len(first), li), c[first], z[first], sign[first], nz[tri][first]))
    leaf, cel, zz, sign, nzs = (np.concatenate(col) for col in zip(*rows))
    is_sup = np.array([p['support'] for p in parts])[leaf]
    part_ev = _Events(cel[~is_sup], zz[~is_sup], sign[~is_sup], leaf[~is_sup])
    sup_ev = _Events(cel[is_sup], zz[is_sup], sign[is_sup])

    # Below the plate: the solid length under Z 0 along each ray.
    for li, p in enumerate(parts):
        m = (leaf == li) & (zz < 0)
        below = float(np.sum(np.where(sign[m] > 0, -zz[m], zz[m]))) * area_cell
        zmin = float(p['tris'][:, :, 2].min())
        if zmin < -0.01 and below > 0.01:
            report['findings'].append({'code': CODE_BELOW_BED, 'severity': 'warning', 'object': p['object'],
                                       'part': p['name'], 'min_z_mm': round(zmin, 3),
                                       'volume_below_mm3': round(below, 1)})

    # Undersides of printed parts that are not inside another part.
    down = (~is_sup) & (sign > 0) & (nzs < -OVERHANG_NZ) & (zz > -1e-3)
    di = np.nonzero(down)[0]
    inside = part_ev.depth(cel[di], zz[di] - 1e-3) > 0
    di = di[~inside]
    floor, floor_leaf = part_ev.top_below(cel[di], zz[di] + 1e-3, with_leaf=True)
    over_part = ~np.isnan(floor) & (floor > 0.01)
    # The surface below belongs to the same mesh (#1373): one pinecone scale
    # over the next, a step of the same part. Not a gap between two parts.
    over_own = over_part & (floor_leaf == leaf[di])
    clear = zz[di] - np.where(over_part, floor, 0.0)
    # Zero-clearance anchor cells (#922 gap 1): underside rays that already
    # touch what is below them. A narrow sloped strip whose edge touches one
    # of these cells roots into the part below instead of floating over it.
    # A gap no taller than the first layer height touches too: the first
    # layer is squished into it, so the underside prints as resting on what
    # is below it and is never a low-clearance gap.
    touching = clear <= CONTACT_MM
    if first_layer_mm:
        # A gap exactly one first layer tall is filled by that layer too
        # (#1373): rounding must not turn a 0.24 mm layer's own gap into a finding.
        touching |= clear <= first_layer_mm + 1e-3
    anchor_cells = {int(c) for c in cel[di[touching]]}
    keep = ~touching
    di, clear, over_part, over_own = di[keep], clear[keep], over_part[keep], over_own[keep]

    def sink_loss(item, dz):
        m = np.array([p['item'] == item for p in parts])[leaf] & (zz < dz)
        return float(np.sum(np.where(sign[m] > 0, dz - zz[m], zz[m] - dz))) * area_cell

    def region(sel, labels, lab):
        idx = di[sel][labels == lab]
        cl = clear[sel][labels == lab]
        op = over_part[sel][labels == lab]
        own = over_own[sel][labels == lab]
        cells = np.unique(cel[idx])
        cx = lo[0] + (cells % w + 0.5) * cell
        cy = lo[1] + (cells // w + 0.5) * cell
        pts = np.c_[cx, cy]
        if len(pts) > 1:
            centered = pts - pts.mean(0)
            _, vecs = np.linalg.eigh(np.cov(centered.T))
            proj = centered @ vecs
            ext = proj.max(0) - proj.min(0) + cell
            width, length = float(ext.min()), float(ext.max())
        else:
            width = length = cell
        # The persisted underside footprint (#963 item 1): the occupied
        # ray cells aggregated to about 1 mm cells with the share of each
        # cell the underside covers, pitch doubled until under the cap.
        fpitch = FOOTPRINT_MM
        while True:
            kx = np.floor(cx / fpitch).astype(np.int64)
            ky = np.floor(cy / fpitch).astype(np.int64)
            rows, counts = np.unique(np.stack([kx, ky], axis=1), axis=0, return_counts=True)
            if len(rows) <= FOOTPRINT_MAX_CELLS or fpitch > 32.0:
                break
            fpitch *= 2.0
        foot = [[round(float(r[0]) * fpitch + fpitch / 2, 2),
                 round(float(r[1]) * fpitch + fpitch / 2, 2),
                 round(min(1.0, float(c) * area_cell / (fpitch * fpitch)), 3)]
                for r, c in zip(rows, counts)
                if c * area_cell >= FOOTPRINT_SHARE_MIN * fpitch * fpitch]
        p = parts[int(leaf[idx[0]])]
        zv = zz[idx]
        return {'object': p['object'], 'part': p['name'], 'item': p['item'], 'material': p['material'],
                'footprint': foot, 'footprint_mm': fpitch,
                'area_mm2': round(len(cells) * area_cell, 1),
                'z_mm': {'min': round(float(zv.min()), 3), 'max': round(float(zv.max()), 3)},
                'clearance_mm': {'min': round(float(cl.min()), 3), 'median': round(float(np.median(cl)), 3),
                                 'max': round(float(cl.max()), 3)},
                'over': 'part' if op.mean() > 0.5 else 'plate',
                'over_self': bool(op.mean() > 0.5 and own.sum() > 0.5 * op.sum()),
                'sloped': bool(zv.max() - zv.min() > 0.2),
                'slope_nz': round(float(np.median(np.abs(nzs[idx]))), 4),
                'span_mm': round(length, 1), 'width_mm': round(width, 1),
                'bbox_mm': [[round(float(cx.min()), 2), round(float(cy.min()), 2)],
                            [round(float(cx.max()), 2), round(float(cy.max()), 2)]],
                'gap_fill_mm3': round(float(cl.sum()) * area_cell, 1),
                '_idx': idx, '_cells': cells}

    # INSUFFICIENT_SUPPORT_CLEARANCE: connected low-clearance surfaces.
    low = clear < gap_mm
    labels = _components(leaf[di[low]], cel[di[low]], zz[di[low]], w)
    flags = 0
    for lab in range(int(labels.max()) + 1 if len(labels) else 0):
        r = region(low, labels, lab)
        r.pop('_idx')
        cells = r.pop('_cells')
        # Wedge-root exclusion (#922 gap 1): a sloped face in the
        # self-supporting band (downward normal <= 55 degrees from vertical,
        # but not near-horizontal), whose sub-gap strip is narrower than
        # 2.0 mm and touches a zero-clearance anchor cell at its root, is a
        # wall rooting into the shelf below it -- not a floating underside.
        anchored = any((int(c) + d) in anchor_cells for c in cells for d in (-1, 1, -w, w))
        # The overall bounding width of a strip that follows a curved or
        # L-shaped edge (a bottom chamfer) says nothing about how wide the
        # strip is. On a sloped face the rise over the slope gives the real
        # width across the strip.
        # The rise alone is not enough: a wide faceted underside (a sawtooth
        # or ribbed bottom) has a small rise and a steep median facet, so its
        # rise width is tiny however wide the surface is. The across-slope
        # width is accepted only when the region's cells really form a thin
        # strip (they erode away within half the wedge width), so a wide
        # surface is never looser than the bounding width it had before.
        nz = r['slope_nz']
        rise_width = (r['z_mm']['max'] - r['z_mm']['min']) * nz / math.sqrt(max(1.0 - nz * nz, 1e-6))
        strip_width = r['width_mm']
        if (strip_width >= WEDGE_WIDTH_MM and rise_width < WEDGE_WIDTH_MM
                and _thin_strip(cells, w, cell, WEDGE_WIDTH_MM)):
            strip_width = rise_width
        if (r['sloped'] and strip_width < WEDGE_WIDTH_MM
                and WEDGE_COS_MIN <= r['slope_nz'] <= WEDGE_COS_MAX and anchored):
            r['kind'] = 'sloped_wedge_root'
            report['regions'].append(r)
            continue
        trigger = 'area' if r['area_mm2'] >= area_mm2 else 'span' if r['span_mm'] >= span_mm else None
        if r['over_self'] and authored_supports is False:
            # A designed overhang (#1373): the underside sits over the same
            # part (one stacked scale or step over the next) and the
            # designer's project prints it with supports off. Supports are
            # not the answer and neither is a geometry repair; it is
            # recorded as a note, never a risk or a coverage target.
            r['kind'] = 'designed_overhang'
            r['flagged'] = False
            r['trigger'] = trigger
            report['regions'].append(r)
            continue
        r['kind'] = 'low_clearance'
        # Only a region that met the finding thresholds is a post-slice
        # coverage target (#963 item 2); sub-threshold regions are recorded
        # but never graded.
        r['flagged'] = trigger is not None
        report['regions'].append(r)
        if not trigger:
            continue
        flags += trigger == 'area'
        fixes = [{'fix': 'extend_underside', 'to': r['over'], 'added_volume_mm3': r['gap_fill_mm3']}]
        if r['over'] == 'plate':
            dz = r['clearance_mm']['max']
            fixes.append({'fix': 'sink', 'sink_mm': dz, 'removed_volume_mm3': round(sink_loss(r['item'], dz), 1)})
        fixes += [{'fix': 'reorient'}, {'fix': 'print_anyway'}]
        report['findings'].append({'code': CODE_CLEARANCE, 'severity': 'risk', 'trigger': trigger,
                                   **{k: v for k, v in r.items() if k != 'kind'}, 'fixes': fixes})

    # SUPPORTABLE_OVERHANG (#922 gap 4): connected downward overhangs with
    # room for supports (clearance >= gap_mm). Not risky at pre-check, but
    # they are the required target regions for post-slice ;TYPE:Support
    # coverage verification when the job is sliced with supports.
    ample = clear >= gap_mm
    if ample.any():
        alabels = _components(leaf[di[ample]], cel[di[ample]], zz[di[ample]], w)
        for lab in range(int(alabels.max()) + 1 if len(alabels) else 0):
            r = region(ample, alabels, lab)
            r.pop('_idx')
            r.pop('_cells')
            if r['area_mm2'] >= area_mm2 or r['span_mm'] >= span_mm:
                r['kind'] = 'supportable_overhang'
                r['flagged'] = True   # a coverage target by definition (#963 item 2)
                report['regions'].append(r)

    # SINGLE-MESH CANTILEVER (#922 gap 3): one unnamed mesh can bake its
    # support pillars into the part itself, so no named support object ever
    # appears. A large downward overhang held up by a much smaller bed
    # contact is named as a warning (never a low-clearance risk), so the
    # operator's no-supports choice can be recorded as an explicit override.
    if len(parts) == 1 and not parts[0]['support']:
        contact = {int(c) for c in cel[(leaf == 0) & (sign > 0) & (zz <= CONTACT_MM)]}
        bed_contact = round(len(contact) * area_cell, 1)
        overhang_area = round(len(np.unique(cel[di])) * area_cell, 1) if len(di) else 0.0
        ratio = round(overhang_area / bed_contact, 2) if bed_contact > 0 else None
        high = bool(bed_contact > 0 and overhang_area >= CANTILEVER_MIN_OVERHANG_MM2
                    and ratio is not None and ratio >= CANTILEVER_RATIO)
        report['single_mesh'] = {'bed_contact_mm2': bed_contact, 'overhang_mm2': overhang_area,
                                 'ratio': ratio, 'high_overhang_ratio': high}
        if high:
            report['findings'].append({'code': CODE_CANTILEVER, 'severity': 'warning',
                                       'object': parts[0]['object'], 'part': parts[0]['name'],
                                       'overhang_mm2': overhang_area, 'bed_contact_mm2': bed_contact,
                                       'ratio': ratio})

    # INEFFECTIVE_MODELED_SUPPORT: overhangs with modeled supports under them.
    if is_sup.any():
        allsel = np.ones(len(di), bool)
        olabels = _components(leaf[di], cel[di], zz[di], w)
        for lab in range(int(olabels.max()) + 1 if len(olabels) else 0):
            r = region(allsel, olabels, lab)
            idx = r.pop('_idx')
            r.pop('_cells')
            if r['area_mm2'] < MIN_OVERHANG_MM2:
                continue
            top = sup_ev.top_below(cel[idx], zz[idx] + 1e-3)
            under = ~np.isnan(top)
            if not under.any():
                continue
            gaps = zz[idx] - top
            touch = under & (gaps <= CONTACT_WINDOW_MM)
            touched = len(np.unique(cel[idx][touch])) * area_cell
            coverage = touched / max(r['area_mm2'], 1e-9)
            r.update(kind='modeled_support', support_contact_mm2=round(touched, 1),
                     support_coverage=round(coverage, 4),
                     support_gap_mm=round(float(np.median(gaps[touch])), 3) if touch.any() else None)
            report['regions'].append(r)
            if coverage < min_coverage:
                report['findings'].append({'code': CODE_MODELED, 'severity': 'risk', 'reason': 'low_coverage',
                                           **{k: v for k, v in r.items() if k != 'kind'},
                                           'support_objects': sorted({p['name'] for p in parts if p['support']})})
        # A modeled support that runs into a printed part.
        si = np.nonzero(is_sup)[0]
        hit = part_ev.depth(cel[si], zz[si] - CONTACT_MM) > 0
        pi = np.nonzero(~is_sup)[0]
        hit_p = sup_ev.depth(cel[pi], zz[pi] - CONTACT_MM) > 0
        if hit.any() or hit_p.any():
            cells = np.unique(np.concatenate([cel[si][hit], cel[pi][hit_p]]))
            report['findings'].append({'code': CODE_MODELED, 'severity': 'risk', 'reason': 'intersects_part',
                                       'support_objects': sorted({parts[int(leaf[i])]['name'] for i in si[hit]}
                                                                 or {p['name'] for p in parts if p['support']}),
                                       'parts': sorted({parts[int(leaf[i])]['name'] for i in pi[hit_p]}),
                                       'area_mm2': round(len(cells) * area_cell, 1)})

    report['low_gap_flags'] = flags
    report['risky'] = any(f['severity'] == 'risk' for f in report['findings'])
    return report


def safe_analyze(source, first_layer_mm=None):
    """analyze(source), or None for a missing or unreadable file (intake never fails on it).

    ``first_layer_mm`` defaults to the printer profile's first layer height
    (profile_first_layer_mm), so a gap the first layer closes is not flagged.
    """
    try:
        if not (source and Path(source).is_file()):
            return None
        if first_layer_mm is None:
            first_layer_mm = profile_first_layer_mm()
        return analyze(source, first_layer_mm=first_layer_mm)
    except Exception:
        return None


def risky(report):
    """True when the report holds a finding that should be decided before printing."""
    return bool(report and report.get('risky'))


def codes(report):
    return sorted({f['code'] for f in (report or {}).get('findings') or []})


_MOVE_RE = re.compile(r'^\s*G0?1(?=\s|$)([^;]*)', re.I)
_WORD_RE = re.compile(r'([XYZE])\s*(-?\d+(?:\.\d+)?|-?\.\d+)', re.I)
_OBJ_START = re.compile(r'EXCLUDE_OBJECT_START|;\s*printing object', re.I)
_OBJ_END = re.compile(r'EXCLUDE_OBJECT_END|;\s*stop printing object', re.I)


def gcode_model_bounds(gcode_text):
    """XY bounds of the printed model (non-support extrusion), or None.

    When the file carries object markers, only extrusion inside an object
    block counts, so brims, purge lines and support do not move the bounds.
    Used to map source-coordinate coverage targets into the sliced
    coordinate system: slicing may center, scale or rearrange the model,
    which a whole-model translation + scale derived from these bounds
    captures.
    """
    has_objects = bool(_OBJ_START.search(gcode_text))
    in_object = not has_objects
    current_type = ''
    pos = {'X': None, 'Y': None}
    lo = [None, None]
    hi = [None, None]
    for raw in gcode_text.splitlines():
        line = raw.strip()
        if line.startswith(';TYPE:'):
            current_type = line.split(':', 1)[1].strip().casefold()
            continue
        if has_objects:
            if _OBJ_START.search(line):
                in_object = True
                continue
            if _OBJ_END.search(line):
                in_object = False
                continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        extrudes = False
        for axis, number in _WORD_RE.findall(move.group(1)):
            axis = axis.upper()
            if axis == 'E':
                extrudes = True
            elif axis in pos:
                pos[axis] = float(number)
        if (extrudes and in_object and 'support' not in current_type
                and pos['X'] is not None and pos['Y'] is not None):
            for k, v in enumerate((pos['X'], pos['Y'])):
                lo[k] = v if lo[k] is None else min(lo[k], v)
                hi[k] = v if hi[k] is None else max(hi[k], v)
    if lo[0] is None:
        return None
    return (lo[0], lo[1], hi[0], hi[1])


_PRINT_OBJ_RE = re.compile(
    r'printing object:?\s+(.+?)(?:\s+id\s*:\s*(\S+)\s+copy\s+(\d+))?\s*$', re.I)
_EXCLUDE_SUFFIX_RE = re.compile(r'^(.*)_id_(.+)_copy_(\d+)$')


def _instance_key(line):
    """(display name, identity key) for an object-block start line.

    OrcaSlicer stable repeats its object markers once per layer per
    instance (#969): ``; printing object <Name> id:<id> copy <n>``,
    paired with ``EXCLUDE_OBJECT_START NAME=<Name>_id_<id>_copy_<n>``.
    Both spellings reduce to the same identity - base name, id and
    copy - so the per-layer blocks of one copy merge into one instance.
    A marker carrying no id/copy identifies the object by name alone.
    """
    m = _PRINT_OBJ_RE.search(line)
    if m:
        name = unquote(m.group(1).strip()) or None
        return name, (_norm_object_name(name), m.group(2), m.group(3))
    m = re.search(r'NAME=([^\s;]+)', line)
    if m:
        raw = unquote(m.group(1))
        suffix = _EXCLUDE_SUFFIX_RE.match(raw)
        if suffix:
            return (suffix.group(1) or None,
                    (_norm_object_name(suffix.group(1)), suffix.group(2), suffix.group(3)))
        return raw or None, (_norm_object_name(raw), None, None)
    return None, (None, None, None)


def gcode_object_bounds(gcode_text):
    """XY bounds of each object instance in a G-code text (#963 item 3).

    Returns [{'name': name or None, 'bounds': (x0, y0, x1, y1)}] in
    first-appearance order, one entry per object *instance*. OrcaSlicer
    emits one marker block per layer per instance (#969), so blocks
    that share an instance identity (base name + id + copy, from either
    the ``; printing object`` spelling or the ``EXCLUDE_OBJECT_START
    NAME=`` spelling) merge into a single entry whose bounds are the
    union over all of the instance's blocks: a single layer's extrusion
    is only that layer's cross-section, not the copy's footprint.
    Copies of one object appear as separate instances with the same
    name. Only non-support extrusion inside a block counts, mirroring
    ``gcode_model_bounds``.
    """
    order = []
    by_key = {}
    current = None
    current_type = ''
    pos = {'X': None, 'Y': None}
    for raw in gcode_text.splitlines():
        line = raw.strip()
        if line.startswith(';TYPE:'):
            current_type = line.split(':', 1)[1].strip().casefold()
            continue
        if _OBJ_START.search(line):
            name, key = _instance_key(line)
            current = by_key.get(key)
            if current is None:
                current = {'name': name, 'lo': [None, None], 'hi': [None, None]}
                by_key[key] = current
                order.append(current)
            elif current['name'] is None:
                current['name'] = name
            continue
        if _OBJ_END.search(line):
            current = None
            continue
        if current is None:
            continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        extrudes = False
        for axis, number in _WORD_RE.findall(move.group(1)):
            axis = axis.upper()
            if axis == 'E':
                extrudes = True
            elif axis in pos:
                pos[axis] = float(number)
        if extrudes and 'support' not in current_type and pos['X'] is not None and pos['Y'] is not None:
            for k, v in enumerate((pos['X'], pos['Y'])):
                current['lo'][k] = v if current['lo'][k] is None else min(current['lo'][k], v)
                current['hi'][k] = v if current['hi'][k] is None else max(current['hi'][k], v)
    return [{'name': entry['name'],
             'bounds': (entry['lo'][0], entry['lo'][1], entry['hi'][0], entry['hi'][1])}
            for entry in order if entry['lo'][0] is not None]


def _norm_object_name(value):
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def _object_match(instance_name, region):
    """Whether an object instance plausibly is a region's source object."""
    n = _norm_object_name(instance_name)
    if len(n) < 3:
        return False
    for candidate in (region.get('object'), region.get('part')):
        c = _norm_object_name(candidate)
        if c and (n == c or n in c or c in n):
            return True
    return False


def gcode_model_height(gcode_text):
    """Highest Z reached by model (non-support) extrusion, or None.

    Both the source and the slice sit on the plate, so the ratio of this
    height to the source model's height is the slice's Z scale.
    """
    has_objects = bool(_OBJ_START.search(gcode_text))
    in_object = not has_objects
    current_type = ''
    z = 0.0
    top = None
    for raw in gcode_text.splitlines():
        line = raw.strip()
        if line.startswith(';TYPE:'):
            current_type = line.split(':', 1)[1].strip().casefold()
            continue
        if has_objects:
            if _OBJ_START.search(line):
                in_object = True
                continue
            if _OBJ_END.search(line):
                in_object = False
                continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        extrudes = False
        for axis, number in _WORD_RE.findall(move.group(1)):
            axis = axis.upper()
            if axis == 'E':
                extrudes = True
            elif axis == 'Z':
                z = float(number)
        if extrudes and in_object and 'support' not in current_type:
            top = z if top is None else max(top, z)
    return top


def _clip_segment_to_box(ax, ay, az, bx, by, bz, x0, y0, x1, y1, zlo, zhi):
    """The part of a support segment inside a target box and Z window.

    Liang–Barsky clipping in XY against [x0, x1] x [y0, y1], then the
    parameter interval is narrowed to where the segment's (linear) Z is
    inside [zlo, zhi] (either bound None means no Z constraint). Returns
    ((px, py), (qx, qy)) for the surviving sub-segment, or None when no
    part of the segment is inside both.
    """
    dx, dy, dz = bx - ax, by - ay, bz - az
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    if zlo is not None:
        if dz == 0:
            if not zlo <= az <= zhi:
                return None
        else:
            ta, tb = (zlo - az) / dz, (zhi - az) / dz
            t0 = max(t0, min(ta, tb))
            t1 = min(t1, max(ta, tb))
            if t0 > t1:
                return None
    return (ax + dx * t0, ay + dy * t0), (ax + dx * t1, ay + dy * t1)


_SUPPORT_TYPE_RE = re.compile(r';\s*support_type\s*=\s*([^\n;]+)', re.I)


def verify_support_coverage(regions, gcode, cell_mm=3.0, source_bounds=None, gcode_bounds=None,
                            z_scale=None, object_bounds=None, matched_only=False,
                            recorded_scale=None, placement_mm=None, support_style=None):
    """Post-slice toolpath coverage for support target regions (#922 gaps 2 and 4).

    ``regions`` is a report's region list (or a whole report). Targets are
    ``supportable_overhang`` regions and *flagged* ``low_clearance``
    regions (#963 item 2: a low-clearance region that never met the
    finding thresholds is recorded but not graded; regions from reports
    predating the ``flagged`` field are treated as flagged). Excluded
    regions are named in the result's ``excluded_targets``.

    Grading (#963 item 1): a region carrying a persisted ``footprint``
    is graded on the ``cell_mm`` cells that hold a meaningful share of
    its underside (at least ``FOOTPRINT_SHARE_MIN`` of the cell), not on
    every cell of its bounding box; regions without a footprint keep the
    bounding-box grading. A grading cell counts as covered when support
    toolpath (``;TYPE:Support`` / ``;TYPE:Support interface`` extruding
    moves, sampled along the whole segment, not just endpoints) passes
    through it at a height that can hold the underside up: within 1.0 mm
    below the region's ``z_mm`` minimum. Acceptance is 100% of cells.

    Acceptance by support style (#963 item 6): grid/normal supports tile
    the underside, so every support toolpath counts. Tree supports do
    not tile — their branches only touch the underside through the
    interface layer — so for a tree slice (``support_style`` or the
    G-code's own ``support_type`` setting) only ``Support interface``
    segments count when the file has any; tree acceptance on the
    interface is still 100% of footprint cells. The style and basis are
    named in the result (``support_style`` / ``acceptance_basis``).

    Mapping: region boxes are in source coordinates; slicing may center,
    scale or rearrange the model. With ``recorded_scale`` (the job's
    recorded scale, #963 item 5) the mapping uses that scale and anchors
    at ``placement_mm`` when given, else at the G-code bounds' minimum;
    ``scale_source`` in the result names which mapping ran. Otherwise,
    when ``source_bounds`` and ``gcode_bounds`` are both given, the
    mapping is the translation + scale between them, and ``z_scale``
    scales the target heights. An explicit ``z_scale`` wins; when it is
    None the Z scale follows the mapping scale (the job scale is
    uniform), or 1.0 with no mapping (#938 item 1).

    Copies (#963 item 3): when ``object_bounds`` (from
    ``gcode_object_bounds``) is given, a region whose object matches one
    or more object instances is graded once per instance against that
    instance's own bounds — every copy must be covered. With
    ``matched_only``, regions matching no instance are skipped (named in
    ``not_on_plate``); without it they fall back to the whole-model
    mapping. Returns {'checked', 'ok', 'regions': [...], ...}; with no
    targets, checked is False and ok is True (nothing to verify).
    """
    if isinstance(regions, dict):
        regions = regions.get('regions') or []
    targets, excluded = [], []
    for r in regions or []:
        if r.get('kind') not in ('supportable_overhang', 'low_clearance') or not r.get('bbox_mm'):
            continue
        if r.get('kind') == 'low_clearance' and r.get('flagged') is False:
            excluded.append({'kind': r['kind'], 'part': r.get('part'),
                             'reason': 'low_clearance below the finding thresholds; '
                                       'not a coverage target'})
            continue
        targets.append(r)
    scale_source = 'recorded' if recorded_scale else 'bounds' if (source_bounds and gcode_bounds) else 'none'
    if not targets:
        return {'checked': False, 'ok': True, 'regions': [], 'excluded_targets': excluded,
                'scale_source': scale_source}
    if not isinstance(gcode, str):
        gcode = Path(gcode).read_text(errors='replace')
    segments = []          # (ax, ay, az, bx, by, bz, is_interface)
    current_type = ''
    pos = {'X': None, 'Y': None, 'Z': 0.0}
    for raw in gcode.splitlines():
        line = raw.strip()
        if line.startswith(';TYPE:'):
            current_type = line.split(':', 1)[1].strip().casefold()
            continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        start = (pos['X'], pos['Y'], pos['Z'])
        # An E word on the move is an extrusion, in absolute or relative E
        # mode alike; a bare G1 travel carries no E.
        extrudes = False
        for axis, number in _WORD_RE.findall(move.group(1)):
            axis = axis.upper()
            if axis == 'E':
                extrudes = True
            elif axis in pos:
                pos[axis] = float(number)
        if extrudes and 'support' in current_type and pos['X'] is not None and pos['Y'] is not None:
            seg = (start[0] if start[0] is not None else pos['X'],
                   start[1] if start[1] is not None else pos['Y'],
                   start[2], pos['X'], pos['Y'], pos['Z'])
            segments.append(seg + ('interface' in current_type,))
    if support_style:
        style = str(support_style).casefold()
    else:
        m = _SUPPORT_TYPE_RE.search(gcode)
        style = 'tree' if (m and 'tree' in m.group(1).casefold()) else 'grid'
    has_interface = any(s[6] for s in segments)
    if style == 'tree' and has_interface:
        basis, grading_segments = 'interface', [s[:6] for s in segments if s[6]]
    elif style == 'tree':
        basis, grading_segments = 'all_support_fallback', [s[:6] for s in segments]
    else:
        basis, grading_segments = 'all_support', [s[:6] for s in segments]

    def _mapping(gb):
        """(map_point, sx, sy) from source coordinates into a slice whose
        model bounds are gb (or one object instance's bounds)."""
        if recorded_scale:
            rs = float(recorded_scale)
            anchor = placement_mm if placement_mm is not None else (gb[0], gb[1]) if gb else (0.0, 0.0)
            ref = (source_bounds[0], source_bounds[1]) if source_bounds else (0.0, 0.0)
            return (lambda x, y: (anchor[0] + (x - ref[0]) * rs, anchor[1] + (y - ref[1]) * rs)), rs, rs
        if source_bounds and gb:
            sx = ((gb[2] - gb[0]) / (source_bounds[2] - source_bounds[0])
                  if source_bounds[2] > source_bounds[0] else 1.0)
            sy = ((gb[3] - gb[1]) / (source_bounds[3] - source_bounds[1])
                  if source_bounds[3] > source_bounds[1] else 1.0)
            return (lambda x, y: (gb[0] + (x - source_bounds[0]) * sx,
                                  gb[1] + (y - source_bounds[1]) * sy)), sx, sy
        return (lambda x, y: (x, y)), 1.0, 1.0

    out, not_on_plate = [], []
    ok = True
    for r in targets:
        instances = [None]
        if object_bounds:
            matches = [ob for ob in object_bounds if _object_match(ob.get('name'), r)]
            if matches:
                instances = matches
            elif matched_only:
                not_on_plate.append({'kind': r['kind'], 'part': r.get('part'),
                                     'reason': 'no matching object instance on this plate'})
                continue
        for instance in instances:
            gb = instance['bounds'] if instance else gcode_bounds
            _map_point, sx, sy = _mapping(gb)
            zs = z_scale
            if zs is None:
                # The job scale is uniform: the mapping's scale is the
                # slice's Z scale as well (#938 item 1).
                zs = recorded_scale if recorded_scale else (sx + sy) / 2.0 if (source_bounds and gb) else 1.0
            (x0, y0) = _map_point(*r['bbox_mm'][0])
            (x1, y1) = _map_point(*r['bbox_mm'][1])
            zmin = (r.get('z_mm') or {}).get('min')
            if zmin is not None:
                # A scaled slice scales the underside height with the model.
                zmin = zmin * zs
            nx = max(1, int(np.ceil((x1 - x0) / cell_mm)))
            ny = max(1, int(np.ceil((y1 - y0) / cell_mm)))
            covered = set()
            # Support at the wrong height does not hold this underside up:
            # only the part of a segment near the underside's own Z counts.
            zlo = zmin - 1.0 if zmin is not None else None
            zhi = zmin + 0.5 if zmin is not None else None
            for ax, ay, az, bx, by, bz in grading_segments:
                # Clip the segment to the target's own box before sampling
                # (#938 item 2): a path that never enters the box contributes
                # nothing, however narrow the box is, while a crossing still
                # counts every cell it passes through — including a partial
                # edge cell whose in-box interval the unclipped samples
                # would jump over.
                hit = _clip_segment_to_box(ax, ay, az, bx, by, bz, x0, y0, x1, y1, zlo, zhi)
                if hit is None:
                    continue
                (px, py), (qx, qy) = hit
                # Sample along the clipped segment: a long straight support
                # line covers every cell it crosses, not only its endpoints.
                dist = max(abs(qx - px), abs(qy - py))
                steps = max(1, int(dist // (cell_mm / 2)) + 1)
                for s in range(steps + 1):
                    t = s / steps
                    x = px + (qx - px) * t
                    y = py + (qy - py) * t
                    # A sample exactly on the box's upper edge indexes one
                    # past the last cell; the clamp keeps it in that cell.
                    i = min(nx - 1, max(0, int((x - x0) // cell_mm)))
                    j = min(ny - 1, max(0, int((y - y0) // cell_mm)))
                    covered.add((i, j))
            graded = 'bbox'
            total = nx * ny
            hit_cells = covered
            footprint = r.get('footprint')
            if footprint:
                fpitch = float(r.get('footprint_mm') or FOOTPRINT_MM)
                area_by_cell = {}
                for fx, fy, share in footprint:
                    mx, my = _map_point(float(fx), float(fy))
                    i = min(nx - 1, max(0, int((mx - x0) // cell_mm)))
                    j = min(ny - 1, max(0, int((my - y0) // cell_mm)))
                    area_by_cell[(i, j)] = (area_by_cell.get((i, j), 0.0)
                                            + float(share) * fpitch * fpitch * abs(sx * sy))
                target_cells = {c for c, a in area_by_cell.items()
                                if a >= FOOTPRINT_SHARE_MIN * cell_mm * cell_mm}
                if target_cells:
                    graded = 'footprint'
                    total = len(target_cells)
                    hit_cells = covered & target_cells
            cov = len(hit_cells) / total if total else 1.0
            ok = ok and cov >= 1.0
            entry = {'kind': r['kind'], 'part': r.get('part'), 'z_mm': r.get('z_mm'),
                     'bbox_mm': r['bbox_mm'], 'coverage': round(cov, 4),
                     'covered_cells': len(hit_cells), 'total_cells': total,
                     'graded': graded, 'support_style': style, 'acceptance_basis': basis}
            if instance:
                entry['object_instance'] = instance.get('name')
                entry['mapping'] = 'object_instance'
            out.append(entry)
    result = {'checked': True, 'ok': ok, 'regions': out, 'excluded_targets': excluded,
              'scale_source': scale_source, 'support_style': style, 'acceptance_basis': basis}
    if not_on_plate:
        result['not_on_plate'] = not_on_plate
    return result


def verify_coverage_by_plate(regions, plates):
    """Per-plate post-slice coverage (#963 items 4 and 7).

    ``plates`` is a list of {'gcode': text or path, ...} dicts; every
    other key is forwarded to ``verify_support_coverage`` for that plate
    (``source_bounds``, ``z_scale`` or ``source_height`` — the plate's Z
    scale is derived from its own model height — ``recorded_scale``,
    ``placement_mm``, ``support_style``, ``cell_mm``; ``gcode_bounds``
    defaults to the plate's own model bounds). A region is graded only
    on plates whose G-code carries a matching object instance, with that
    plate's own bounds, so one plate's support can never cover another
    plate's region. When any plate's G-code has no object markers,
    attribution is impossible and the plates fall back to one grading
    over the concatenated text, named ``concatenated_fallback``.

    Nothing fails open silently: an unreadable plate file, a plate whose
    check raises, a caller-supplied ``unverified_reason`` (for example
    unreadable source bounds) and a target region matched by no plate
    all come back as named ``unverified`` entries, with ``ok`` left True
    for whatever could not be checked — the caller records them in the
    receipt instead of treating them as a pass or a refusal.
    """
    plates = list(plates or [])
    if not plates:
        return {'checked': False, 'ok': True, 'regions': [], 'unverified': [], 'plate_mode': 'none'}
    keys = ('source_bounds', 'gcode_bounds', 'z_scale', 'recorded_scale',
            'placement_mm', 'support_style', 'cell_mm')
    read, unverified = [], []
    for i, plate in enumerate(plates):
        kw = {k: plate[k] for k in keys if plate.get(k) is not None}
        g = plate.get('gcode')
        label = plate.get('label')
        try:
            if isinstance(g, str) and '\n' not in g or not isinstance(g, str):
                text = Path(g).read_text(errors='replace')
                label = label or Path(g).name
            else:
                text = g
        except OSError as exc:
            unverified.append({'plate': i + 1,
                               'reason': f'plate {i + 1} G-code unreadable ({label or g}): {exc}'})
            continue
        if plate.get('source_height'):
            height = gcode_model_height(text)
            if height:
                kw.setdefault('z_scale', height / float(plate['source_height']))
        read.append((i + 1, text, kw, plate))
        if plate.get('unverified_reason'):
            unverified.append({'plate': i + 1, 'reason': str(plate['unverified_reason'])})
    if not read:
        return {'checked': False, 'ok': True, 'regions': [], 'unverified': unverified,
                'plate_mode': 'single' if len(plates) == 1 else 'per_plate'}

    def _run(i, text, kw, matched_only):
        try:
            result = verify_support_coverage(
                regions, text, object_bounds=gcode_object_bounds(text) or None,
                matched_only=matched_only, **kw)
        except Exception as exc:  # noqa: BLE001 - a failed check is named, never a silent pass
            unverified.append({'plate': i, 'reason': f'plate {i} coverage check failed: {exc}'})
            return None
        for entry in result['regions']:
            entry['plate'] = i
        return result

    results = []
    if len(read) == 1:
        i, text, kw, _plate = read[0]
        kw.setdefault('gcode_bounds', gcode_model_bounds(text))
        result = _run(i, text, kw, matched_only=False)
        if result is not None:
            results.append(result)
        mode = 'single'
    elif all(gcode_object_bounds(text) for _i, text, _kw, _p in read):
        mode = 'per_plate'
        graded_parts = set()
        for i, text, kw, _plate in read:
            kw.setdefault('gcode_bounds', gcode_model_bounds(text))
            result = _run(i, text, kw, matched_only=True)
            if result is None:
                continue
            results.append(result)
            graded_parts.update(e['part'] for e in result['regions'])
        target_parts = {r.get('part') for r in (regions.get('regions') if isinstance(regions, dict)
                                                else regions) or []
                        if r.get('kind') in ('supportable_overhang', 'low_clearance')
                        and r.get('bbox_mm')
                        and not (r.get('kind') == 'low_clearance' and r.get('flagged') is False)}
        for part in sorted(target_parts - graded_parts, key=str):
            unverified.append({'plate': None,
                               'reason': f'region {part} matched no object instance on any '
                                         'plate; its coverage is unverified'})
    else:
        mode = 'concatenated_fallback'
        text = '\n'.join(t for _i, t, _kw, _p in read)
        kw = dict(read[0][2])
        kw['gcode_bounds'] = gcode_model_bounds(text)
        result = _run(read[0][0], text, kw, matched_only=False)
        if result is not None:
            results.append(result)
    out = [entry for result in results for entry in result['regions']]
    checked = any(result['checked'] for result in results)
    result = {'checked': checked,
              'ok': all(result['ok'] for result in results if result['checked']),
              'regions': out, 'unverified': unverified, 'plate_mode': mode,
              'plates': [{'checked': r['checked'], 'ok': r['ok']} for r in results]}
    for key in ('excluded_targets', 'scale_source', 'support_style', 'acceptance_basis'):
        if results and key in results[0]:
            result[key] = results[0][key]
    return result


def _mesh_volume(source):
    """Absolute signed volume of every leaf mesh in a source (mm3)."""
    total = 0.0
    for p in leaves(source):
        t = p['tris']
        total += float(np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])).sum()) / 6.0
    return abs(total)


def _volume_below(source, dz):
    """Solid volume at or below Z dz, measured on the analyze() ray grid."""
    parts = leaves(source)
    if not parts:
        return 0.0
    allpts = np.concatenate([p['tris'].reshape(-1, 3) for p in parts])
    span = allpts[:, :2].max(0) - allpts[:, :2].min(0) + 2
    cell = max(CELL_MM, float(np.sqrt(span[0] * span[1] / MAX_CELLS)))
    lo = allpts[:, :2].min(0) - 1
    w, h = (np.ceil(span / cell).astype(int) + 1).tolist()
    rows = []
    for p in parts:
        t = p['tris']
        nrm = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        nz = nrm[:, 2] / np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)
        idx = np.nonzero(np.abs(nz) > 1e-6)[0]
        c, z, ti = _raster(t[idx], lo, cell, w, h)
        tri = idx[ti]
        sign = np.where(nz[tri] < 0, 1, -1)
        _, first = np.unique(np.stack([c, np.round(z, 4), sign]), axis=1, return_index=True)
        rows.append((c[first], z[first], sign[first]))
    cel, zz, sign = (np.concatenate(col) for col in zip(*rows))
    ev = _Events(cel, zz, sign)
    if len(ev.z) < 2:
        return 0.0
    same = ev.cell[:-1] == ev.cell[1:]
    # depth[i] is the solid depth in the interval starting at hit i.
    depth = ev.cum[1:]
    z0, z1 = ev.z[:-1], ev.z[1:]
    overlap = np.minimum(z1, dz) - np.minimum(z0, dz)
    solid = same & (depth[:len(z0)] > 0) & (overlap > 0)
    return float(overlap[solid].sum()) * cell * cell


def _fmt(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


DESCRIBE_MAX_LINES = 3


def describe(report):
    """Plain-text lines for the pre-slice summary, or [] when there is nothing to say.

    Identical lines (one underside found on every copy or layer of a part)
    are shown once with "(xN)", and at most DESCRIBE_MAX_LINES distinct
    lines are shown; a last line counts the findings left out.
    """
    counts = {}
    for line in _finding_lines(report):
        counts[line] = counts.get(line, 0) + 1
    lines = [line if n == 1 else f'{line} (x{n})' for line, n in counts.items()]
    if len(lines) > DESCRIBE_MAX_LINES:
        hidden = sum(list(counts.values())[DESCRIBE_MAX_LINES:])
        lines = lines[:DESCRIBE_MAX_LINES] + [f'+{hidden} more finding{"s" if hidden != 1 else ""}.']
    return lines


def _finding_lines(report):
    """One plain-text line per finding, in report order."""
    lines = []
    for f in (report or {}).get('findings') or []:
        if f['code'] == CODE_CLEARANCE:
            below = ('the build plate' if f['over'] == 'plate'
                     else 'its own surface below' if f.get('over_self') else 'the part below')
            shape = (f'across {f["area_mm2"]:.0f} mm2' if f['trigger'] == 'area'
                     else f'along a {f["span_mm"]:.0f} mm span ({f["area_mm2"]:.0f} mm2)')
            slope = ' sloped' if f['sloped'] else ''
            lines.append(f'{f["part"]}: this{slope} underside at Z {_fmt(f["z_mm"]["min"])} mm bridges a '
                         f'{_fmt(f["clearance_mm"]["min"])} mm gap to {below} {shape}; supports cannot reach it.')
        elif f['code'] == CODE_MODELED and f.get('reason') == 'intersects_part':
            lines.append(f'Modeled support {", ".join(f["support_objects"])} runs into '
                         f'{", ".join(f["parts"]) or "the model"}; it cannot print as a separate support.')
        elif f['code'] == CODE_MODELED:
            lines.append(f'{f["part"]}: the modeled supports touch only {100 * f["support_coverage"]:.1f}% of the '
                         f'{f["area_mm2"]:.0f} mm2 overhang at Z {_fmt(f["z_mm"]["min"])} mm.')
        elif f['code'] == CODE_CANTILEVER:
            lines.append(f'{f["part"]}: one single mesh holds a {f["overhang_mm2"]:.0f} mm2 overhang '
                         f'over only {f["bed_contact_mm2"]:.0f} mm2 of bed contact; baked-in pillars '
                         'in the same mesh are not separate modeled supports.')
        elif f['code'] == CODE_BELOW_BED:
            lines.append(f'{f["part"]} reaches {_fmt(-f["min_z_mm"])} mm below the plate; '
                         f'{f["volume_below_mm3"]:.0f} mm3 would be cut off.')
    return lines


def _instance_matrices(source):
    """{(model file, object id): [composed world matrix, ...]} for printable meshes.

    The same traversal leaves() uses (build items, components and their
    transforms, cutter/modifier volumes left out), recording where each mesh
    object lands in printer coordinates instead of its triangles.
    """
    from print_handy.core import source_colors
    out = {}
    with zipfile.ZipFile(source) as archive:
        try:
            cutters = source_colors.cutter_ids(archive)
        except Exception:
            cutters = {}
        # PrusaSlicer modifier, negative and support volumes (#1260): ranges of
        # one mesh keep only the model-part triangles, component volumes are
        # cutters. A model config that cannot be read refuses (strict).
        from print_handy.core.orca_adapter import _prusa_printed_ranges
        prusa_keep, prusa_cutters = _prusa_printed_ranges(archive)
        cutters = {**cutters, **prusa_cutters}
        models = {}

        def model(name):
            name = name.lstrip('/')
            if name not in models:
                root = ET.fromstring(archive.read(name))
                objs = {}
                for res in _kids(root, 'resources'):
                    for obj in _kids(res, 'object'):
                        objs[obj.get('id')] = obj
                models[name] = (root, objs)
            return models[name]

        def collect(name, oid, matrix, top_oid, active):
            if (name, oid) in active:
                raise ValueError('cyclic 3MF component reference')
            obj = model(name)[1].get(oid)
            if obj is None:
                raise ValueError(f'unresolved 3MF object {oid}')
            if (obj.get('type') or 'model').lower() in _SKIP_TYPES:
                return
            mesh = next(iter(_kids(obj, 'mesh')), None)
            if mesh is not None and (top_oid, oid) not in cutters:
                out.setdefault((name, oid), []).append(matrix)
            for wrap in _kids(obj, 'components'):
                for comp in _kids(wrap, 'component'):
                    path = next((v for k, v in comp.attrib.items() if _local(k) == 'path'), name)
                    collect(path.lstrip('/'), comp.get('objectid'), _matrix(comp) @ matrix,
                            top_oid, active | {(name, oid)})

        root = model('3D/3dmodel.model')[0]
        for build in _kids(root, 'build'):
            for item in _kids(build, 'item'):
                if item.get('printable', '1') == '0':
                    continue
                oid = item.get('objectid')
                collect('3D/3dmodel.model', oid, _matrix(item), oid, frozenset())
    return out


UP_NZ = 0.5  # a face whose outward normal points this far up is a top, never an underside


def _extend_bands(source_path, gap_mm, tol):
    """The underside bands extend_underside moves when no vertex sits at gap_mm.

    A sloped underside's measured clearance is sampled per ray between its
    vertices, so the gap a finding (or the AI fix) names is usually no vertex
    height at all. Each low-clearance finding over the plate whose measured
    band holds ``gap_mm`` gives one band: its XY bounding box grown by one
    ray cell, and world Z from just above the plate up to the top of the
    sampled underside.
    """
    try:
        rep = analyze(source_path)
    except Exception:  # noqa: BLE001 - nothing measurable means nothing to extend
        return []
    margin = float(rep.get('cell_mm') or CELL_MM) + tol
    bands = []
    for f in rep.get('findings') or []:
        if f.get('code') != CODE_CLEARANCE or f.get('over') != 'plate' or not f.get('bbox_mm'):
            continue
        window = f.get('clearance_mm') or {}
        if not (window.get('min', 0) - tol <= gap_mm <= window.get('max', 0) + tol):
            continue
        (x0, y0), (x1, y1) = f['bbox_mm']
        bands.append((x0 - margin, y0 - margin, x1 + margin, y1 + margin, float(f['z_mm']['max'])))
    return bands


def _band_select(world, up, bands, tol):
    """Vertices inside a band that are on no upward-facing face (never a part's top)."""
    keep = np.zeros(len(world), bool)
    for x0, y0, x1, y1, ztop in bands:
        keep |= ((world[:, 0] >= x0) & (world[:, 0] <= x1) & (world[:, 1] >= y0) & (world[:, 1] <= y1)
                 & (world[:, 2] > tol) & (world[:, 2] <= ztop + tol))
    return keep & ~up


def _up_vertices(world, tris_idx, mirrored=False):
    """Per-vertex flag: the vertex belongs to a face whose normal points up."""
    up = np.zeros(len(world), bool)
    if len(tris_idx) == 0:
        return up
    t = world[tris_idx]
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    nz = n[:, 2] / np.maximum(np.linalg.norm(n, axis=1), 1e-12)
    if mirrored:
        nz = -nz
    up[np.unique(tris_idx[nz > UP_NZ])] = True
    return up


def extend_underside(source_path, dest_path=None, gap_mm=0.4, tol=1e-3):
    """Extend downward underside vertices at gap_mm down to the build plate.

    gap_mm is a printer-coordinate height (findings are measured in world
    coordinates): each vertex is matched by its world Z under its build /
    component transform and moved in local coordinates by the amount that
    puts its world Z on the plate, so transformed and component-based 3MFs
    are repaired as well as root-file meshes at the same local height.
    Preserves all 3MF components, materials, colors and other metadata.
    The measured volume change is returned as ``added_volume_mm3`` (#922
    gap 5); instances that disagree on a shared vertex's target leave it
    alone and count it in ``skipped_shared_vertices``.

    A flat underside has its vertices at exactly gap_mm (``mode`` 'exact').
    A sloped one does not: its measured clearance is sampled between its
    vertices, so no vertex matched and the repair moved nothing while the
    analysis promised the gap fill. Then the low-clearance finding whose
    measured band holds gap_mm is extended instead (``mode`` 'band'): its
    underside vertices from just above the plate up to the top of the band
    move to the plate, never a vertex of an upward-facing face.
    """
    dest_path = dest_path or source_path
    result = _extend(source_path, dest_path,
                     lambda world, up: np.abs(world[:, 2] - gap_mm) <= tol)
    result['mode'] = 'exact'
    if result['modified_vertices'] or result['skipped_shared_vertices']:
        return result
    bands = _extend_bands(source_path, gap_mm, tol)
    if not bands:
        return result
    result = _extend(source_path, dest_path, lambda world, up: _band_select(world, up, bands, tol))
    result['mode'] = 'band'
    return result


def _extend(source_path, dest_path, select):
    """Move every vertex that ``select(world_xyz, on_upward_face)`` picks onto the plate."""
    before_volume = _mesh_volume(source_path)
    if str(source_path).lower().endswith('.stl'):
        # An STL has no transforms to compose: its vertices are already in
        # printer coordinates, so the same rule applies directly (#922
        # gap 8 made STL findings offer these repairs, so they must run).
        tris = _stl_tris(source_path)
        flat = tris.reshape(-1, 3)
        keys, inverse = np.unique(np.round(flat, 5), axis=0, return_inverse=True)
        inverse = np.asarray(inverse).reshape(-1)
        up = _up_vertices(keys, inverse.reshape(-1, 3))
        mask = select(flat, up[inverse]).reshape(tris.shape[:2])
        modified = int(mask.sum())
        tris[:, :, 2] = np.where(mask, 0.0, tris[:, :, 2])
        _write_binary_stl(dest_path, tris)
        return {'modified_vertices': modified, 'skipped_shared_vertices': 0,
                'added_volume_mm3': round(_mesh_volume(dest_path) - before_volume, 1),
                'output_path': str(dest_path)}
    instances = _instance_matrices(source_path)
    tmp_out = Path(dest_path).with_suffix(f'.tmp_ext_{os.getpid()}.3mf')
    modified_count = 0
    skipped_shared = 0
    with zipfile.ZipFile(source_path, 'r') as zin, zipfile.ZipFile(tmp_out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith('.model'):
                root = ET.fromstring(data)
                changed = False
                for obj in root.iter():
                    if _local(obj.tag) != 'object':
                        continue
                    mats = (instances.get((item.filename, obj.get('id')))
                            or instances.get((item.filename.lstrip('/'), obj.get('id'))))
                    if not mats:
                        continue
                    mesh = next(iter(_kids(obj, 'mesh')), None)
                    if mesh is None:
                        continue
                    verts = [v for vs in _kids(mesh, 'vertices') for v in _kids(vs, 'vertex')]
                    if not verts:
                        continue
                    local = np.array([[float(v.get(a) or 0) for a in ('x', 'y', 'z')] for v in verts])
                    tri_idx = np.array([[int(t.get(a)) for a in ('v1', 'v2', 'v3')]
                                        for ts in _kids(mesh, 'triangles') for t in _kids(ts, 'triangle')],
                                       dtype=np.int64).reshape(-1, 3)
                    targets = [[] for _ in verts]
                    for m in mats:
                        if abs(m[2, 2]) <= 1e-9:
                            continue
                        world = local @ m[:3, :3] + m[3, :3]
                        up = _up_vertices(world, tri_idx, mirrored=np.linalg.det(m[:3, :3]) < 0)
                        for i in np.nonzero(select(world, up))[0]:
                            targets[i].append(local[i, 2] - world[i, 2] / m[2, 2])
                    for v, tg in zip(verts, targets):
                        if tg and max(tg) - min(tg) <= 1e-6:
                            v.set('z', f'{tg[0]:.8f}')
                            modified_count += 1
                            changed = True
                        elif tg:
                            # Instances disagree on the local target:
                            # leave the shared vertex alone (#922 gap 5).
                            skipped_shared += 1
                if changed:
                    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            zout.writestr(item, data)
    shutil.move(tmp_out, dest_path)
    after_volume = _mesh_volume(dest_path)
    return {'modified_vertices': modified_count, 'skipped_shared_vertices': skipped_shared,
            'added_volume_mm3': round(after_volume - before_volume, 1),
            'output_path': str(dest_path)}


def _write_binary_stl(path, tris):
    """Write triangles as a binary STL (the repair output for STL sources)."""
    import struct
    tris = np.asarray(tris, dtype='<f4').reshape(-1, 3, 3)
    with open(path, 'wb') as fh:
        fh.write(b'\0' * 80)
        fh.write(struct.pack('<I', len(tris)))
        zeros = np.zeros((len(tris), 3), dtype='<f4')
        for tri, zero in zip(tris, zeros):
            fh.write(zero.tobytes())
            fh.write(tri.tobytes())
            fh.write(struct.pack('<H', 0))


def _stl_tris(source_path):
    from print_handy.core import orca_adapter
    tris = orca_adapter.read_stl_triangles(source_path)
    return np.asarray(tris, dtype=float).reshape(-1, 3, 3)


def sink_model(source_path, dest_path=None, dz=0.4):
    """Sink build items in the 3MF by dz mm into the build plate.

    The slicer clips geometry below Z=0, removing the low-clearance underside.
    An STL source has no build items: every vertex moves down by dz instead.
    """
    dest_path = dest_path or source_path
    if str(source_path).lower().endswith('.stl'):
        tris = _stl_tris(source_path)
        tris[:, :, 2] -= dz
        _write_binary_stl(dest_path, tris)
        return {'sink_mm': dz, 'removed_volume_mm3': round(_volume_below(dest_path, 0.0), 1),
                'output_path': str(dest_path)}
    tmp_out = Path(dest_path).with_suffix(f'.tmp_sink_{os.getpid()}.3mf')
    with zipfile.ZipFile(source_path, 'r') as zin, zipfile.ZipFile(tmp_out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == '3D/3dmodel.model':
                root = ET.fromstring(data)
                ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
                for it in root.findall('.//m:build/m:item', ns):
                    tr = it.get('transform')
                    if tr:
                        vals = [float(v) for v in tr.split()]
                        vals[11] -= dz
                        it.set('transform', ' '.join(str(v) for v in vals))
                    else:
                        it.set('transform', f'1 0 0 0 1 0 0 0 1 0 0 {-dz}')
                data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
            zout.writestr(item, data)
    shutil.move(tmp_out, dest_path)
    # Measured, not estimated (#922 gap 5): the volume the slicer will clip
    # is the solid that now sits below Z 0 in the sunk file.
    return {'sink_mm': dz, 'removed_volume_mm3': round(_volume_below(dest_path, 0.0), 1),
            'output_path': str(dest_path)}


def record_clearance_acknowledgement(folder_or_job, gcode=None, source_file=None, via='print_anyway'):
    """Record operator acknowledgement of an unfixable support clearance risk.

    ``via`` names how the operator satisfied the finding (#922 gap 2): an
    explicit ``print_anyway`` choice, a verified geometry repair (which
    clears the finding itself), or the operator's explicit
    ``without_supports`` as-authored decision satisfies a low-clearance
    finding at the start guard. A ``with_supports`` choice is recorded for
    provenance but deliberately does not satisfy it: slicer supports
    cannot fit into a sub-1.0 mm gap, so that choice still owes post-slice
    toolpath coverage verification for the flagged regions.
    """
    is_path = isinstance(folder_or_job, (str, Path))
    if is_path:
        p = Path(folder_or_job)
        jp = p if p.name == 'JOB.json' else p / 'JOB.json'
        job = json.loads(jp.read_text())
    else:
        job = folder_or_job
        jp = None

    from print_handy.core import model_job as mj
    src = source_file or job.get('downloaded_file')
    src_sha = mj.file_sha256(src) if src and Path(src).is_file() else None
    binding = job.get('artifact_binding') or {}
    rec_sha = binding.get('recipe_sha256')
    gc = gcode or job.get('gcode')
    gc_sha = mj.file_sha256(gc) if gc and Path(gc).is_file() else binding.get('gcode_sha256')

    job['clearance_acknowledgement'] = {
        'acknowledged': True,
        'via': via,
        'source_sha256': src_sha,
        'recipe_sha256': rec_sha,
        'gcode_sha256': gc_sha,
        'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }

    if is_path and jp is not None:
        mj.write_job(jp.parent, job)
    return job
