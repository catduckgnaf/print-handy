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
  are measured per ray, not only horizontal facets.
- ``INEFFECTIVE_MODELED_SUPPORT``: a modeled support object (named support
  or 3MF type support) sits under an overhang but touches less than
  ``min_coverage`` of it, or intersects a printed part.
- ``BELOW_BED_GEOMETRY``: a part extends below Z 0; the volume that would be
  cut is stated.

Each region also carries fix estimates (extend the underside down, sink the
part, reorient, print anyway). These are screening thresholds, not a promise
of printability: the post-slice toolpath check consumes ``regions``.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

VERSION = 1
CODE_CLEARANCE = 'INSUFFICIENT_SUPPORT_CLEARANCE'
CODE_MODELED = 'INEFFECTIVE_MODELED_SUPPORT'
CODE_BELOW_BED = 'BELOW_BED_GEOMETRY'

GAP_MM = 1.0             # an underside closer than this to what is below cannot take support
AREA_MM2 = 500.0         # connected low-clearance area that triggers the finding
SPAN_MM = 15.0           # a smaller low-clearance region this long still triggers it
CONTACT_MM = 0.05        # clearance at or below this is contact, not a gap
OVERHANG_NZ = 0.5        # downward faces within 60 degrees of horizontal
MIN_COVERAGE = 0.10      # modeled supports must touch at least this share of the overhang
CONTACT_WINDOW_MM = 0.6  # a support top this far below an overhang still counts as touching
MIN_OVERHANG_MM2 = 20.0  # smaller overhang regions are not checked for modeled supports
CELL_MM = 0.5            # ray grid pitch
MAX_CELLS = 1_500_000    # the grid pitch grows past CELL_MM to stay under this
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
    with zipfile.ZipFile(source) as archive:
        try:
            cutters = source_colors.cutter_ids(archive)
        except Exception:
            cutters = {}
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

    def __init__(self, cell, z, sign):
        order = np.lexsort((z, cell))
        self.cell, self.z, self.sign = cell[order], z[order], sign[order]
        self.keys = _key(self.cell, self.z)
        self.cum = np.concatenate([[0], np.cumsum(self.sign)])
        up = self.sign < 0
        self.ucell, self.uz = self.cell[up], self.z[up]
        self.ukeys = self.keys[up]

    def top_below(self, cell, z):
        """Highest upward surface at or below z in the same ray, or NaN."""
        if not len(self.ukeys):
            return np.full(len(cell), np.nan)
        idx = np.searchsorted(self.ukeys, _key(cell, z), side='right') - 1
        safe = np.maximum(idx, 0)
        ok = (idx >= 0) & (self.ucell[safe] == cell)
        return np.where(ok, self.uz[safe], np.nan)

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


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def analyze(source, gap_mm=GAP_MM, area_mm2=AREA_MM2, span_mm=SPAN_MM, min_coverage=MIN_COVERAGE):
    """The structured support clearance report for a 3MF (see the module docstring)."""
    parts = leaves(source)
    report = {'version': VERSION, 'source_sha256': _sha256(source),
              'thresholds': {'gap_mm': gap_mm, 'area_mm2': area_mm2, 'span_mm': span_mm,
                             'contact_mm': CONTACT_MM, 'min_coverage': min_coverage},
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
    part_ev = _Events(cel[~is_sup], zz[~is_sup], sign[~is_sup])
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
    floor = part_ev.top_below(cel[di], zz[di] + 1e-3)
    over_part = ~np.isnan(floor) & (floor > 0.01)
    clear = zz[di] - np.where(over_part, floor, 0.0)
    keep = clear > CONTACT_MM
    di, clear, over_part = di[keep], clear[keep], over_part[keep]

    def sink_loss(item, dz):
        m = np.array([p['item'] == item for p in parts])[leaf] & (zz < dz)
        return float(np.sum(np.where(sign[m] > 0, dz - zz[m], zz[m] - dz))) * area_cell

    def region(sel, labels, lab):
        idx = di[sel][labels == lab]
        cl = clear[sel][labels == lab]
        op = over_part[sel][labels == lab]
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
        p = parts[int(leaf[idx[0]])]
        zv = zz[idx]
        return {'object': p['object'], 'part': p['name'], 'item': p['item'], 'material': p['material'],
                'area_mm2': round(len(cells) * area_cell, 1),
                'z_mm': {'min': round(float(zv.min()), 3), 'max': round(float(zv.max()), 3)},
                'clearance_mm': {'min': round(float(cl.min()), 3), 'median': round(float(np.median(cl)), 3),
                                 'max': round(float(cl.max()), 3)},
                'over': 'part' if op.mean() > 0.5 else 'plate',
                'sloped': bool(zv.max() - zv.min() > 0.2),
                'span_mm': round(length, 1), 'width_mm': round(width, 1),
                'bbox_mm': [[round(float(cx.min()), 2), round(float(cy.min()), 2)],
                            [round(float(cx.max()), 2), round(float(cy.max()), 2)]],
                'gap_fill_mm3': round(float(cl.sum()) * area_cell, 1),
                '_idx': idx}

    # INSUFFICIENT_SUPPORT_CLEARANCE: connected low-clearance surfaces.
    low = clear < gap_mm
    labels = _components(leaf[di[low]], cel[di[low]], zz[di[low]], w)
    flags = 0
    for lab in range(int(labels.max()) + 1 if len(labels) else 0):
        r = region(low, labels, lab)
        r.pop('_idx')
        trigger = 'area' if r['area_mm2'] >= area_mm2 else 'span' if r['span_mm'] >= span_mm else None
        r['kind'] = 'low_clearance'
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

    # INEFFECTIVE_MODELED_SUPPORT: overhangs with modeled supports under them.
    if is_sup.any():
        allsel = np.ones(len(di), bool)
        olabels = _components(leaf[di], cel[di], zz[di], w)
        for lab in range(int(olabels.max()) + 1 if len(olabels) else 0):
            r = region(allsel, olabels, lab)
            idx = r.pop('_idx')
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


def safe_analyze(source):
    """analyze(source), or None for a missing or unreadable file (intake never fails on it)."""
    try:
        return analyze(source) if source and Path(source).is_file() else None
    except Exception:
        return None


def risky(report):
    """True when the report holds a finding that should be decided before printing."""
    return bool(report and report.get('risky'))


def codes(report):
    return sorted({f['code'] for f in (report or {}).get('findings') or []})


def _fmt(v):
    return f'{v:.2f}'.rstrip('0').rstrip('.')


def describe(report):
    """Plain-text lines for the pre-slice summary, or [] when there is nothing to say."""
    lines = []
    for f in (report or {}).get('findings') or []:
        if f['code'] == CODE_CLEARANCE:
            below = 'the build plate' if f['over'] == 'plate' else 'the part below'
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
        elif f['code'] == CODE_BELOW_BED:
            lines.append(f'{f["part"]} reaches {_fmt(-f["min_z_mm"])} mm below the plate; '
                         f'{f["volume_below_mm3"]:.0f} mm3 would be cut off.')
    return lines
