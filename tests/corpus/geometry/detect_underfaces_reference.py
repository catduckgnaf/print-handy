#!/usr/bin/env python3
"""Small deterministic reference for horizontal underside *intake* tests (#911).

Operates on core 3MF geometry, honoring build transforms. Uses standard library
xml.etree.ElementTree, numpy, and trimesh (no lxml or shapely dependency required).
"""
import argparse
import collections
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import trimesh

NS = '{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}'


def transforms(s):
    if not s:
        return np.eye(4)
    v = np.array([float(i) for i in s.split()], dtype=float)
    if len(v) != 12 or not np.all(np.isfinite(v)):
        raise ValueError('invalid 3MF item transform')
    t = np.eye(4)
    t[:, :3] = v.reshape((4, 3))
    return t


def _extract_meshes(obj, base_transform, obj_by_id):
    mesh = obj.find(NS + 'mesh')
    if mesh is not None:
        v_el = mesh.find(NS + 'vertices')
        f_el = mesh.find(NS + 'triangles')
        if v_el is not None and f_el is not None:
            v = np.array([[float(x.get(k) or 0.0) for k in ('x', 'y', 'z')] for x in v_el], float)
            f = np.array([[int(x.get(k) or 0) for k in ('v1', 'v2', 'v3')] for x in f_el], int)
            m = trimesh.Trimesh(vertices=v, faces=f, process=False)
            m.apply_transform(base_transform.T)
            name = obj.get('name') or 'object'
            return [{
                'name': name,
                'id': obj.get('id'),
                'type': obj.get('type', 'model'),
                'mesh': m,
                'support': obj.get('type', 'model') in ('support', 'solidsupport') or 'SUPPORT' in name.upper()
            }]
    comps = obj.find(NS + 'components')
    if comps is not None:
        sub = []
        for comp in comps.findall(NS + 'component'):
            target = obj_by_id.get(comp.get('objectid'))
            if target is not None:
                ct = transforms(comp.get('transform'))
                sub.extend(_extract_meshes(target, base_transform @ ct, obj_by_id))
        return sub
    return []


def geometry(path):
    with zipfile.ZipFile(path) as src:
        root = ET.fromstring(src.read('3D/3dmodel.model'))
    resources = root.find(NS + 'resources')
    obj_by_id = {x.get('id'): x for x in (resources.findall(NS + 'object') if resources is not None else [])}
    out = []
    build = root.find(NS + 'build')
    if build is not None:
        for item_idx, item in enumerate(build.findall(NS + 'item')):
            obj = obj_by_id.get(item.get('objectid'))
            if obj is not None:
                item_transform = transforms(item.get('transform'))
                out.extend(_extract_meshes(obj, item_transform, obj_by_id))
    return out


class Box2D:
    """Axis-aligned 2D bounding rectangle representing a planar component."""

    def __init__(self, x1, x2, y1, y2):
        self.x1, self.x2 = float(min(x1, x2)), float(max(x1, x2))
        self.y1, self.y2 = float(min(y1, y2)), float(max(y1, y2))

    @property
    def area(self):
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)

    def intersect(self, other):
        ix1 = max(self.x1, other.x1)
        ix2 = min(self.x2, other.x2)
        iy1 = max(self.y1, other.y1)
        iy2 = min(self.y2, other.y2)
        if ix2 > ix1 and iy2 > iy1:
            return Box2D(ix1, ix2, iy1, iy2)
        return None


def _connected_components(edges, num_nodes):
    """Pure-Python connected components over an edge list without scipy/networkx."""
    adj_map = collections.defaultdict(list)
    for edge in edges:
        u, v = int(edge[0]), int(edge[1])
        adj_map[u].append(v)
        adj_map[v].append(u)
    visited = set()
    components = []
    for i in range(num_nodes):
        if i not in visited:
            comp = []
            queue = [i]
            visited.add(i)
            for node in queue:
                comp.append(node)
                for neighbor in adj_map[node]:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)
            components.append(comp)
    return components


def flat_surface_groups(objects, sign):
    """Find horizontal flat surface components grouped by (object_index, z_height)."""
    groups = collections.defaultdict(list)
    for idx, o in enumerate(objects):
        m = o['mesh']
        ids = np.where(m.face_normals[:, 2] * sign > 0.999)[0]
        if len(ids) == 0:
            continue
        by_z = collections.defaultdict(list)
        for i in ids:
            z = round(float(m.triangles_center[i, 2]), 3)
            by_z[z].append(i)
        for z, f_ids in by_z.items():
            sub_faces = m.faces[f_ids]
            adj = trimesh.graph.face_adjacency(faces=sub_faces)
            components = _connected_components(adj, len(sub_faces))
            for comp in components:
                comp_faces = sub_faces[comp]
                comp_verts = m.vertices[comp_faces.flatten()]
                bx = Box2D(comp_verts[:, 0].min(), comp_verts[:, 0].max(),
                           comp_verts[:, 1].min(), comp_verts[:, 1].max())
                if bx.area > 1e-4:
                    groups[(idx, z)].append(bx)
    return groups


def diagnose(filename, gap=1.0, area=500.0, contact_window=0.6):
    """Evaluate candidate underside surfaces against low-gap thresholds."""
    objs = geometry(filename)
    up = flat_surface_groups(objs, +1)
    down = flat_surface_groups(objs, -1)
    findings = []
    for (i, z), comp_boxes in sorted(down.items(), key=lambda x: (x[0][1], x[0][0])):
        if objs[i]['support']:
            continue
        for comp in comp_boxes:
            if comp.area < 1e-4:
                continue
            near_roofs = []
            for (j, upz), roof_boxes in up.items():
                if objs[j]['support']:
                    continue
                if 0.001 < z - upz < gap:
                    near_roofs.extend(roof_boxes)
            if 0.02 < z < gap:
                low_area = comp.area
            elif near_roofs:
                low_area = sum((comp.intersect(r).area if comp.intersect(r) else 0.0) for r in near_roofs)
            else:
                low_area = 0.0

            supports = []
            for (j, uz), roof_boxes in up.items():
                if not objs[j]['support']:
                    continue
                airgap = z - uz
                if -0.01 <= airgap <= contact_window:
                    coverage = sum((comp.intersect(r).area if comp.intersect(r) else 0.0) for r in roof_boxes)
                    if coverage > 1e-3:
                        supports.append({
                            'object': objs[j]['name'],
                            'air_gap_mm': round(airgap, 3),
                            'projected_coverage_mm2': round(coverage, 2),
                            'coverage_fraction': round(coverage / comp.area, 5)
                        })
            f = {
                'object': objs[i]['name'],
                'underside_z_mm': z,
                'connected_underside_area_mm2': round(comp.area, 2),
                'low_clearance_area_mm2': round(low_area, 2),
                'low_clearance_fraction': round(low_area / comp.area, 4),
                'low_gap_flag': bool(low_area >= area),
                'named_modeled_supports': supports
            }
            if low_area or supports or comp.area >= area:
                findings.append(f)
    return {
        'file': str(filename),
        'thresholds': {'gap_mm': gap, 'connected_area_mm2': area},
        'bounds_mm': [
            np.round(np.min([o['mesh'].bounds[0] for o in objs], axis=0), 3).tolist() if objs else [0, 0, 0],
            np.round(np.max([o['mesh'].bounds[1] for o in objs], axis=0), 3).tolist() if objs else [0, 0, 0]
        ],
        'objects': [{'name': o['name'], 'watertight': bool(o['mesh'].is_watertight), 'support_heuristic': o['support']} for o in objs],
        'low_gap_flags': sum(f['low_gap_flag'] for f in findings),
        'findings': findings,
        'limitations': [
            'Only near-horizontal downward triangles are measured.',
            'Clearance under sloped faces, mesh collisions, narrow long bridges, XY access and actual support toolpaths are not certified.',
            'Support naming is heuristic; production requires semantic support provenance.'
        ]
    }


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('filename')
    p.add_argument('--gap', type=float, default=1.0)
    p.add_argument('--area', type=float, default=500.0)
    args = p.parse_args()
    print(json.dumps(diagnose(args.filename, args.gap, args.area), indent=2))
