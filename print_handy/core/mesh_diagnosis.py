"""Read-only mesh diagnosis for the print repair skill (#1777).

Works on the welded mesh ``layer_scan.load`` returns (one load, welded by
the shared mesh kernel, #1790):

- ``bodies``: separate solids (faces joined through shared vertices), each
  with its bounds, area and volume.
- ``edge_report``: the intake audit's watertight and manifold verdict
  (``jobs.audit.mesh_topology``, native when available) plus where the open
  and non-manifold edges are, so a model can be pointed at them.
- ``underside_profile``: whether a low "gap" under a region is a real
  floating underside or a sloped flare that prints without support. On the
  2026-11-21 couch organizer the 0.562 mm low-clearance finding was a 53
  degree wall flare: a false positive.
"""
from __future__ import annotations

import math

import numpy as np

from .layer_scan import Mesh, ScanError, _components

# A downward face at least this steep (degrees from horizontal) holds itself up.
SELF_SUPPORTING_DEG = 45.0
MAX_EDGE_LOCATIONS = 50
BED_CONTACT_MM = 1e-3
_DEGENERATE_AREA2 = 1e-18


def _face_vectors(mesh):
    tris = mesh.triangles
    normal = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    return tris, normal


def _degenerate(mesh, normal):
    faces = mesh.faces
    repeated = (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 0] == faces[:, 2])
    return repeated | ((normal * normal).sum(axis=1) <= _DEGENERATE_AREA2)


def bodies(mesh: Mesh) -> dict:
    """Separate solids of the mesh, largest first."""
    if not len(mesh.faces):
        raise ScanError('the mesh has no triangles')
    faces = mesh.faces
    edges_a = np.concatenate((faces[:, 0], faces[:, 1]))
    edges_b = np.concatenate((faces[:, 1], faces[:, 2]))
    labels = _components(len(mesh.vertices), edges_a, edges_b)
    face_label = labels[faces[:, 0]]
    _unique, face_label = np.unique(face_label, return_inverse=True)
    count = int(face_label.max()) + 1
    tris, normal = _face_vectors(mesh)
    area = np.sqrt((normal * normal).sum(axis=1)) / 2.0
    volume = np.einsum('ij,ij->i', tris[:, 0], np.cross(tris[:, 1], tris[:, 2])) / 6.0
    out = []
    for k in range(count):
        sel = face_label == k
        points = tris[sel].reshape(-1, 3)
        out.append({'faces': int(sel.sum()),
                    'bounds_min': [round(float(v), 3) for v in points.min(axis=0)],
                    'bounds_max': [round(float(v), 3) for v in points.max(axis=0)],
                    'area_mm2': round(float(area[sel].sum()), 3),
                    'volume_mm3': round(float(abs(volume[sel].sum())), 3)})
    out.sort(key=lambda b: (-b['volume_mm3'], -b['faces']))
    return {'count': count, 'bodies': out}


def edge_report(mesh: Mesh, max_locations=MAX_EDGE_LOCATIONS) -> dict:
    """Watertight / manifold verdict and the first open and non-manifold edge locations."""
    if not len(mesh.faces):
        raise ScanError('the mesh has no triangles')
    from ..jobs.audit import mesh_topology
    from .compact_geometry import CompactTriangles
    compact = CompactTriangles()
    compact.coordinates.frombytes(np.ascontiguousarray(mesh.triangles, dtype=np.float64).tobytes())
    topology = mesh_topology(compact)
    _tris, normal = _face_vectors(mesh)
    faces = mesh.faces[~_degenerate(mesh, normal)]
    directed = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]))
    undirected = np.sort(directed, axis=1)
    edges, counts = np.unique(undirected, axis=0, return_counts=True)

    def where(selected):
        mid = (mesh.vertices[selected[:, 0]] + mesh.vertices[selected[:, 1]]) / 2.0
        return [[round(float(v), 3) for v in point] for point in mid[:max_locations]]

    return {'watertight': bool(topology['watertight']), 'manifold': bool(topology['manifold']),
            'shells': int(topology['shells']),
            'boundary_edges': int(topology['boundary_edges']),
            'non_manifold_edges': int(topology['non_manifold_edges']),
            'winding_inconsistencies': int(topology['winding_inconsistencies']),
            'degenerate_triangles': int(topology['degenerate_triangles']),
            'boundary_edge_midpoints': where(edges[counts == 1]),
            'non_manifold_edge_midpoints': where(edges[counts > 2])}


def underside_profile(mesh: Mesh, region, z_range=None,
                      self_supporting_deg=SELF_SUPPORTING_DEG) -> dict:
    """Downward faces over ``region`` ``[xmin, ymin, xmax, ymax]``: flat underside or flare?

    Faces whose XY bounds overlap the region (and whose Z range overlaps
    ``z_range`` ``[zmin, zmax]``, above the bed, when given) and that face
    down are measured; faces lying on the bed are the bed contact and are
    left out, and a face counts with the share of its XY bounds over the
    region. ``slope_deg`` is a face's angle from horizontal: 0 is a flat
    ceiling, 90 a vertical wall. ``verdict`` is ``sloped_flare`` when at least 80 % of
    the downward area is at least ``self_supporting_deg`` steep (it prints
    without support), ``flat_underside`` when at most 20 % is, ``mixed``
    otherwise, and ``none`` when no downward face is there. ``z_min`` is
    measured above the mesh's lowest point.
    """
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in region)
    except (TypeError, ValueError) as exc:
        raise ScanError('region must be [xmin, ymin, xmax, ymax]') from exc
    if not (xmin <= xmax and ymin <= ymax) or not all(map(math.isfinite, (xmin, ymin, xmax, ymax))):
        raise ScanError('region must be [xmin, ymin, xmax, ymax] with min <= max')
    tris, normal = _face_vectors(mesh)
    base = float(tris[:, :, 2].min())
    lo = tris.min(axis=1)
    hi = tris.max(axis=1)
    sel = (lo[:, 0] <= xmax) & (hi[:, 0] >= xmin) & (lo[:, 1] <= ymax) & (hi[:, 1] >= ymin)
    if z_range is not None:
        try:
            zlo, zhi = (float(v) + base for v in z_range)
        except (TypeError, ValueError) as exc:
            raise ScanError('z_range must be [zmin, zmax]') from exc
        sel &= (lo[:, 2] <= zhi) & (hi[:, 2] >= zlo)
    length = np.sqrt((normal * normal).sum(axis=1))
    with np.errstate(divide='ignore', invalid='ignore'):
        nz = np.where(length > 0, normal[:, 2] / length, 0.0)
    # Faces lying on the bed are the bed contact, not an underside.
    on_bed = hi[:, 2] <= base + BED_CONTACT_MM
    down = sel & (nz < -1e-6) & ~on_bed
    if not down.any():
        return {'verdict': 'none', 'faces': 0, 'area_mm2': 0.0}
    # Each face counts with the share of its XY bounds that lies over the
    # region, so one large face reaching into a narrow strip does not
    # outweigh the faces that are actually there.
    dx = np.maximum(hi[down, 0] - lo[down, 0], 1e-9)
    dy = np.maximum(hi[down, 1] - lo[down, 1], 1e-9)
    ox = np.clip(np.minimum(hi[down, 0], xmax) - np.maximum(lo[down, 0], xmin), 0.0, None)
    oy = np.clip(np.minimum(hi[down, 1], ymax) - np.maximum(lo[down, 1], ymin), 0.0, None)
    share_in = np.where(hi[down, 0] - lo[down, 0] > 1e-9, ox / dx, 1.0) *         np.where(hi[down, 1] - lo[down, 1] > 1e-9, oy / dy, 1.0)
    area = length[down] / 2.0 * share_in
    slope = np.degrees(np.arccos(np.clip(-nz[down], 0.0, 1.0)))
    steep = slope >= self_supporting_deg
    total = float(area.sum())
    share = float(area[steep].sum() / total) if total > 0 else 0.0
    verdict = 'sloped_flare' if share >= 0.8 else 'flat_underside' if share <= 0.2 else 'mixed'
    return {'verdict': verdict, 'faces': int(down.sum()), 'area_mm2': round(total, 3),
            'self_supporting_share': round(share, 3),
            'mean_slope_deg': round(float((slope * area).sum() / total), 1) if total > 0 else None,
            'min_slope_deg': round(float(slope.min()), 1),
            'z_min': round(float(lo[down, 2].min()) - base, 3),
            'z_max': round(float(hi[down, 2].max()) - base, 3),
            'self_supporting_deg': float(self_supporting_deg)}
