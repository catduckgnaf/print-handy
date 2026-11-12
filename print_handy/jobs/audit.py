"""The source audit: mesh topology, the mesh audit and the 3MF/STL audit a job records.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import hashlib, re, zipfile
import xml.etree.ElementTree as ET

from ..gcode.artifact import file_sha256
from .slice import NoPrintableVolume, model_info_output


def mesh_topology(triangles):
    """Conservative positional topology; no repair or self-intersection test.

    Weld coordinates on a six-decimal grid. Exclude faces whose squared
    double-area is <= 1e-18 source-units**4 from all topology adjacency.
    """
    if hasattr(triangles, '__len__') and len(triangles) > 100_000:
        from ..core.compact_topology import mesh_topology_compact
        return mesh_topology_compact(triangles)
    vertices = {}
    faces = []
    degenerate = 0
    for tri in triangles:
        points = [tuple(float(v) for v in point) for point in tri]
        ids = []
        for point in points:
            key = tuple(round(v, 6) for v in point)
            ids.append(vertices.setdefault(key, len(vertices)))
        u = tuple(b - a for a, b in zip(points[0], points[1]))
        v = tuple(b - a for a, b in zip(points[0], points[2]))
        cross = (u[1]*v[2] - u[2]*v[1], u[2]*v[0] - u[0]*v[2],
                 u[0]*v[1] - u[1]*v[0])
        if len(set(ids)) < 3 or sum(c*c for c in cross) <= 1e-18:
            degenerate += 1
            continue
        faces.append(ids)

    edges = {}
    vertex_faces = {}
    adjacency = [[] for _ in faces]
    vertex_links = {}
    for fi, face in enumerate(faces):
        for vertex in face:
            vertex_faces.setdefault(vertex, set()).add(fi)
        for a, b in zip(face, face[1:] + face[:1]):
            edges.setdefault((min(a, b), max(a, b)), []).append((fi, a < b))
    singular_ids = set()
    for edge, uses in edges.items():
        # A star preserves shell connectivity with k-1 links, even for a dense
        # non-manifold fan. Never expand k*(k-1) face pairs (including k > 64).
        first = uses[0][0]
        for other, _ in uses[1:]:
            adjacency[first].append(other)
            adjacency[other].append(first)
        if len(uses) != 2:
            singular_ids.update(edge)
        else:
            a, b = uses[0][0], uses[1][0]
            for vertex in edge:
                links = vertex_links.setdefault(vertex, {})
                links.setdefault(a, []).append(b)
                links.setdefault(b, []).append(a)

    seen = set()
    shells = 0
    for start in range(len(faces)):
        if start in seen:
            continue
        shells += 1
        stack = [start]
        seen.add(start)
        while stack:
            for other in adjacency[stack.pop()]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)

    # A closed edge neighborhood can still contain disconnected face fans,
    # e.g. two tetrahedra touching at one vertex. Traverse only local links,
    # not every edge for every vertex, so this remains linear in mesh size.
    for vertex, attached in vertex_faces.items():
        if vertex in singular_ids:
            continue
        links = vertex_links.get(vertex, {})
        reached = {next(iter(attached))}
        stack = list(reached)
        while stack:
            for other in links.get(stack.pop(), ()):
                if other not in reached:
                    reached.add(other)
                    stack.append(other)
        if len(reached) != len(attached):
            singular_ids.add(vertex)

    boundary = sum(len(uses) == 1 for uses in edges.values())
    nonmanifold = sum(len(uses) > 2 for uses in edges.values())
    winding = sum(len(uses) == 2 and uses[0][1] == uses[1][1] for uses in edges.values())
    singular = len(singular_ids)
    manifold = not (boundary or nonmanifold or singular or degenerate or winding)
    count = len(faces) + degenerate
    return {"format": "triangle-mesh", "triangles": count,
            "shells": shells, "triangle_count": count, "connected_shells": shells,
            "boundary_edges": boundary, "non_manifold_edges": nonmanifold,
            "non_manifold_vertices": singular, "singular_vertices": singular, "winding_inconsistencies": winding,
            "degenerate_triangles": degenerate, "watertight": boundary == 0 and nonmanifold == 0,
            "manifold": manifold}


def mesh_audit(path, printer_model=None):
    """Read-only, versioned topology report for STL/3MF source geometry."""
    from ..core.orca_adapter import source_triangles
    triangles = source_triangles(path)
    if not triangles:
        raise RuntimeError("model has no printable geometry")
    from ..core.compact_geometry import IndexedTriangles
    if isinstance(triangles, IndexedTriangles):
        from ..core.mesh_fast import bounds_and_volume
        lo, hi, _ = bounds_and_volume(triangles)
    else:
        lo = [min(point[axis] for triangle in triangles for point in triangle) for axis in range(3)]
        hi = [max(point[axis] for triangle in triangles for point in triangle) for axis in range(3)]
    source_sha256 = file_sha256(path)
    from ..core.orca_adapter import placement_candidates
    return {"schema_version": 1, "sha256": source_sha256,
            "source_sha256": source_sha256,
            "dimensions_mm": [round(hi[axis] - lo[axis], 2) for axis in range(3)],
            "units": "mm", "printer_model": printer_model,
            "placement_candidates": placement_candidates(path, printer_model=printer_model),
            **mesh_topology(triangles)}


def audit(path, printer_model=None):
    from ..core.intake_resources import check_source
    check_source(path)
    with open(path,"rb") as source:
        digest=hashlib.file_digest(source,"sha256").hexdigest()
    out={"path":path.name,"bytes":path.stat().st_size,"sha256":digest,"kind":path.suffix.lower()}
    if path.suffix.lower()==".3mf":
        with zipfile.ZipFile(path) as z:
            names=z.namelist(); out["archive_files"]=len(names)
            plate_ids={int(m.group(1)) for n in names if (m:=re.search(r'plate_(\d+)\.(?:gcode|png)$',n))}
            if "Metadata/model_settings.config" in names:
                try:
                    settings = ET.fromstring(z.read("Metadata/model_settings.config"))
                    for plate in settings.iter("plate"):
                        for metadata in plate.iter("metadata"):
                            if metadata.get("key") == "plater_id":
                                plate_id = metadata.get("value")
                                if plate_id is None: raise ValueError("plate has no plater_id value")
                                plate_ids.add(int(plate_id))
                except (ET.ParseError, ValueError) as exc:
                    raise RuntimeError(f"unable to inspect 3MF plate assignments: {exc}") from exc
            out["plates"]=len(plate_ids); out["plate_ids"]=sorted(plate_ids)
            text=""
            for n in names:
                if n.endswith(("3dmodel.model","model_settings.config")):
                    try:text+=z.read(n).decode(errors="ignore")
                    except:pass
            out["objects"]=len(re.findall(r'<(?:\w+:)?object\b',text)); out["components"]=len(re.findall(r'<(?:\w+:)?component\b',text)); out["build_items"]=len(re.findall(r'<(?:\w+:)?item\b',text)); out["named_parts"]=re.findall(r'<metadata key="name" value="([^"]+)',text)[:30]
    from ..core.source_colors import inventory, prompts
    manifest = inventory(path)
    out['color_manifest'] = manifest
    out['color_prompts'] = prompts(manifest)
    try:
        out["mesh_audit"] = mesh_audit(path, printer_model=printer_model)
    except Exception as exc:
        out["mesh_audit_error"] = str(exc)
    try:
        info = model_info_output(path)
    except Exception as exc:  # unreadable mesh: slice_job re-measures and refuses clearly
        info = ""; out["geometry_error"] = str(exc)
        out["no_printable_volume"] = isinstance(exc, NoPrintableVolume)
    for key in ("size_x","size_y","size_z","number_of_parts","manifold"):
        m=re.search(rf'^{key}\s*=\s*(.+)$',info,re.M)
        if m: out[key]=m.group(1).strip()
    return out
