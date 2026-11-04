"""Write the corpus models. Run `python -m tests.corpus.make_models` from the repository root to regenerate every model fixture.

Each generated case is described by a `[model]` table in its case.toml (see "declarative specs" below), or
by an entry of tests/corpus/matrix.toml that expands to a whole case; this module reads those specs and writes
the model files. Adding a case is one case.toml edit plus running the generator.

The shapes are generated here, not copied from anyone, so each one is CC0 and safe to commit.
Fixture content is deterministic (fixed ZIP timestamps and sorted entries); compressed ZIP bytes may vary by zlib version.
The writers use only the standard library. Sizes that a spec derives from the merge planner (`fit`) import
print_handy, with the test suite's part spacing pinned below. A million-triangle torus is generated at test time only.
"""
import html
import json
import math
import os
import struct
import sys
import tomllib
import zipfile
from pathlib import Path

# The planner reads its part spacing from the settings when print_handy is first imported. The test suite
# pins the "medium" preset (tests/conftest.py); pin it here too so a size computed by the generator equals
# the one computed inside pytest. Must run before anything imports print_handy.
os.environ.setdefault("PRINT_HANDY__LAYOUT__SPACING", "medium")

HERE = Path(__file__).resolve().parent
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
STAMP = (2026, 1, 1, 0, 0, 0)


def box(w, d, h):
    """Closed box with its corner at the origin."""
    v = [(x, y, z) for z in (0, h) for y in (0, d) for x in (0, w)]
    f = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
         (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    return v, f


def cylinder(radius, height, sides=48):
    """Closed cylinder standing on z=0, centered on x=y=radius."""
    v, f = [], []
    for z in (0, height):
        v += [(radius + radius * math.cos(2 * math.pi * i / sides),
               radius + radius * math.sin(2 * math.pi * i / sides), z) for i in range(sides)]
    bottom, top = len(v), len(v) + 1
    v += [(radius, radius, 0), (radius, radius, height)]
    for i in range(sides):
        j = (i + 1) % sides
        f += [(bottom, j, i), (top, sides + i, sides + j), (i, j, sides + j), (i, sides + j, sides + i)]
    return v, f


def extrude_y(profile, triangles, depth):
    """Prism along +Y from an XZ profile (counter clockwise seen from -Y)."""
    n = len(profile)
    v = [(x, 0, z) for x, z in profile] + [(x, depth, z) for x, z in profile]
    f = [(a, b, c) for a, b, c in triangles] + [(n + a, n + c, n + b) for a, b, c in triangles]
    for i in range(n):
        j = (i + 1) % n
        f += [(i, n + j, j), (i, n + i, n + j)]
    return v, f


def t_shape(bar_w, stem_w, height, bar_t, depth):
    """A T: the bar overhangs a narrow stem."""
    s0, s1 = (bar_w - stem_w) / 2, (bar_w + stem_w) / 2
    profile = [(s0, 0), (s1, 0), (s1, height - bar_t), (bar_w, height - bar_t),
               (bar_w, height), (0, height), (0, height - bar_t), (s0, height - bar_t)]
    triangles = [(0, 1, 2), (0, 2, 7), (5, 6, 7), (5, 7, 2), (5, 2, 3), (5, 3, 4)]
    return extrude_y(profile, triangles, depth)


def _normal(a, b, c):
    ux, uy, uz = (b[i] - a[i] for i in range(3))
    vx, vy, vz = (c[i] - a[i] for i in range(3))
    n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = math.sqrt(sum(value * value for value in n)) or 1.0
    return tuple(value / length for value in n)


def write_stl(path, mesh, *, binary=True, invert=False, lying_solid=False):
    """Write deterministic generated STL; supports ASCII and binary header edge cases."""
    vertices, faces = mesh
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not binary:
        rows = ["solid Print Handy CC0 generated model"]
        for face in faces:
            points = [vertices[index] for index in face]
            n = _normal(*points)
            if invert:
                n = tuple(-v for v in n)
                points[1], points[2] = points[2], points[1]
            rows.append(" facet normal " + " ".join(f"{x:.7g}" for x in n))
            rows.append("  outer loop")
            rows.extend("   vertex " + " ".join(f"{x:.7g}" for x in point) for point in points)
            rows.extend(("  endloop", " endfacet"))
        rows.append("endsolid Print Handy CC0 generated model")
        target.write_text("\n".join(rows) + "\n", encoding="ascii")
        return
    head = (b"solid generated binary model" if lying_solid else b"Print Handy generated corpus model (CC0)").ljust(80, b"\0")[:80]
    with target.open("wb") as stream:
        stream.write(head)
        stream.write(struct.pack("<I", len(faces)))
        for face in faces:
            points = [vertices[index] for index in face]
            n = _normal(*points)
            if invert:
                n = tuple(-v for v in n)
                points[1], points[2] = points[2], points[1]
            stream.write(struct.pack("<12fH", *n, *points[0], *points[1], *points[2], 0))


def write_binary_stl(path, triangles):
    """Stream binary STL triangles without retaining face indices or XML text."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as stream:
        stream.write(b"Print Handy generated binary STL".ljust(80, b"\0"))
        stream.write(struct.pack("<I", len(triangles)))
        for tri in triangles:
            n = _normal(*tri)
            stream.write(struct.pack("<12fH", *n, *tri[0], *tri[1], *tri[2], 0))


def write_binary_stl_stream(path, triangles):
    """Write generated facets with a streaming header that permits count backpatching."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with target.open("wb") as stream:
        stream.write(b"Print Handy generated binary STL".ljust(80, b"\0"))
        stream.write(b"\0\0\0\0")
        for tri in triangles:
            n = _normal(*tri)
            stream.write(struct.pack("<12fH", *n, *tri[0], *tri[1], *tri[2], 0))
            count += 1
        stream.seek(80)
        stream.write(struct.pack("<I", count))


def _xml(objects, items):
    """3dmodel.model. objects: [(id, kind, payload)]; items: [(object_id, (x, y) or (x, y, z))]."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}">',
           '<metadata name="Title">Corpus model (CC0)</metadata>', '<resources>']
    for oid, kind, payload in objects:
        if kind == "mesh":
            vertices, faces = payload
            out.append(f'<object id="{oid}" type="model"><mesh><vertices>')
            out += [f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in vertices]
            out.append('</vertices><triangles>')
            out += [f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces]
            out.append('</triangles></mesh></object>')
        else:
            members = payload if isinstance(payload, (list, tuple)) else [payload]
            out.append(f'<object id="{oid}" type="model"><components>'
                       + "".join(f'<component objectid="{m}"/>' for m in members) + '</components></object>')
    out.append('</resources><build>')
    for oid, (x, y, *z) in items:
        out.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} {z[0] if z else 0}"/>')
    out.append('</build></model>')
    return "\n".join(out)


def _settings(parts, plates):
    """Bambu style model_settings.config: parts is [(assembly_id, mesh_id, name, extruder)]."""
    out = ["<config>"]
    for assembly, mesh, name, extruder in parts:
        safe_name = html.escape(name, quote=True)
        out.append(f'<object id="{assembly}"><metadata key="name" value="{safe_name}"/>'
                   f'<metadata key="extruder" value="{extruder}"/>'
                   f'<part id="{mesh}" subtype="normal_part"><metadata key="name" value="{safe_name}"/>'
                   f'<metadata key="extruder" value="{extruder}"/></part></object>')
    for number, (name, assemblies) in enumerate(plates, 1):
        if not assemblies:
            continue
        out.append(f'<plate><metadata key="plater_id" value="{number}"/>'
                   f'<metadata key="plater_name" value="{html.escape(name, quote=True)}"/>')
        out += [f'<model_instance><metadata key="object_id" value="{a}"/></model_instance>' for a in assemblies]
        out.append("</plate>")
    out.append("</config>")
    return "\n".join(out)


def write_assembly_3mf(path, bodies, palette, at=(10, 10)):
    """One build item whose bodies share coordinates, like a two color panel (base plus overlay).

    bodies: [(name, extruder, mesh)]. The bodies are components of one assembly object, so they
    must travel together; the planner may never place them as independent objects.
    """
    objects = [(i + 1, "mesh", mesh) for i, (_, _, mesh) in enumerate(bodies)]
    assembly = len(bodies) + 1
    objects.append((assembly, "components", [i + 1 for i in range(len(bodies))]))
    out = ["<config>", f'<object id="{assembly}"><metadata key="name" value="Assembly"/>']
    for i, (name, extruder, _) in enumerate(bodies):
        safe = html.escape(name, quote=True)
        out.append(f'<part id="{i + 1}" subtype="normal_part"><metadata key="name" value="{safe}"/>'
                   f'<metadata key="extruder" value="{extruder}"/></part>')
    out += ["</object>", '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="One plate"/>',
            f'<model_instance><metadata key="object_id" value="{assembly}"/></model_instance></plate>', "</config>"]
    project = {"filament_colour": [c for c, _ in palette], "filament_type": [m for _, m in palette],
               "enable_support": "0"}
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="config" ContentType="text/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        "3D/3dmodel.model": _xml(objects, [(assembly, at)]),
        "Metadata/model_settings.config": "\n".join(out),
        "Metadata/project_settings.config": json.dumps(project, indent=2),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])


def write_3mf(path, shapes, plates, palette, support=False, *, omit_plate_metadata=False, extra_empty_plates=0,
              project_extra=None):
    """shapes: [(name, extruder, mesh, (x, y) or (x, y, z))]; plates: [(plate name, [shape index])].
    project_extra: more project_settings.config keys (prime tower, an embedded foreign slicer profile)."""
    objects, items, parts = [], [], []
    for index, (name, extruder, mesh, at) in enumerate(shapes):
        mesh_id, assembly = 2 * index + 2, 2 * index + 1
        objects += [(mesh_id, "mesh", mesh), (assembly, "components", mesh_id)]
        items.append((assembly, at))
        parts.append((assembly, mesh_id, name, extruder))
    project = {"filament_colour": [c for c, _ in palette], "filament_type": [m for _, m in palette],
               "enable_support": "1" if support else "0"}
    if support:
        project["support_type"] = "tree(auto)"
    project.update(project_extra or {})
    # Plates arrive as shape indices; model_settings.config must reference the
    # assembly object ids (2 * index + 1), as real Orca/Bambu files do.
    plate_rows = [] if omit_plate_metadata else [
        (name, [2 * index + 1 for index in indices]) for name, indices in plates]
    if extra_empty_plates:
        plate_rows = list(plate_rows) + [(f"Empty {i}", []) for i in range(1, extra_empty_plates + 1)]
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="config" ContentType="text/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        "3D/3dmodel.model": _xml(objects, items),
        "Metadata/model_settings.config": _settings(parts, plate_rows),
        "Metadata/project_settings.config": json.dumps(project, indent=2),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])


def write_core_3mf(path, shapes, *, style="basematerials", triangle_colors=None):
    """Plain core 3MF (no slicer metadata), the shape Blender/Fusion/generators export (#711, #714).

    shapes: [(name, hex color or None, mesh, (x, y))]. style "basematerials" puts
    every shape as a component of one assembly with object pid/pindex into
    <basematerials>; "colorgroup" uses separate build items with m:colorgroup.
    triangle_colors: per-triangle core colors on the first shape (p1 index list).
    """
    palette = []
    for _, color, _, _ in shapes:
        if color and color not in palette:
            palette.append(color)
    for color in triangle_colors or ():
        if color not in palette:
            palette.append(color)
    if style == "colorgroup":
        res = ['<m:colorgroup id="1">'] + [f'<m:color color="{c}FF"/>' for c in palette] + ['</m:colorgroup>']
    else:
        res = ['<basematerials id="1">'] + [f'<base name="{c}" displaycolor="{c}"/>' for c in palette] + ['</basematerials>']
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}" '
           'xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
           '<metadata name="Title">Corpus core 3MF (CC0)</metadata>', '<resources>'] + res
    for index, (name, color, (vertices, faces), (x, y)) in enumerate(shapes):
        prop = f' pid="1" pindex="{palette.index(color)}"' if color else ""
        out.append(f'<object id="{index + 2}" name="{html.escape(name, quote=True)}" type="model"{prop}><mesh><vertices>')
        out += [f'<vertex x="{vx + x:.4f}" y="{vy + y:.4f}" z="{vz:.4f}"/>' for vx, vy, vz in vertices]
        out.append('</vertices><triangles>')
        for t, (a, b, c) in enumerate(faces):
            extra = ""
            if index == 0 and triangle_colors:
                extra = f' pid="1" p1="{palette.index(triangle_colors[t % len(triangle_colors)])}"'
            out.append(f'<triangle v1="{a}" v2="{b}" v3="{c}"{extra}/>')
        out.append('</triangles></mesh></object>')
    count = len(shapes)
    if style == "colorgroup":
        build = [f'<item objectid="{i + 2}"/>' for i in range(count)]
    else:
        out.append(f'<object id="{count + 2}" name="Assembly" type="model"><components>'
                   + "".join(f'<component objectid="{i + 2}"/>' for i in range(count)) + '</components></object>')
        build = [f'<item objectid="{count + 2}"/>']
    out += ['</resources><build>'] + build + ['</build></model>']
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        "3D/3dmodel.model": "\n".join(out),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])


def write_face_props_3mf(path, name, mesh, at):
    """Plain core 3MF whose only color is face level: m:multiproperties over a
    colorgroup plus an m:texture2dgroup, per-triangle p1 (#714). Not slicer paint."""
    vertices, faces = mesh
    x, y = at
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}" '
           'xmlns:m="http://schemas.microsoft.com/3dmanufacturing/material/2015/02">',
           '<metadata name="Title">Corpus face properties 3MF (CC0)</metadata>', '<resources>',
           '<m:colorgroup id="1"><m:color color="#FF0000FF"/><m:color color="#0000FFFF"/></m:colorgroup>',
           '<m:texture2d id="2" path="/3D/Texture/blank.png" contenttype="image/png"/>',
           '<m:texture2dgroup id="3" texid="2"><m:tex2coord u="0" v="0"/><m:tex2coord u="1" v="1"/></m:texture2dgroup>',
           '<m:multiproperties id="4" pids="1 3"><m:multi pindices="0 0"/><m:multi pindices="1 1"/></m:multiproperties>',
           f'<object id="5" name="{html.escape(name, quote=True)}" type="model" pid="4" pindex="0"><mesh><vertices>']
    out += [f'<vertex x="{vx + x:.4f}" y="{vy + y:.4f}" z="{vz:.4f}"/>' for vx, vy, vz in vertices]
    out.append('</vertices><triangles>')
    out += [f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="4" p1="{t % 2}"/>' for t, (a, b, c) in enumerate(faces)]
    out += ['</triangles></mesh></object>', '</resources><build><item objectid="5"/></build></model>']
    _write_package(path, {"3D/3dmodel.model": "\n".join(out),
                          "3D/Texture/blank.png": _BLANK_PNG})


def write_prusa_mmu_3mf(path, name, mesh, at, colors):
    """PrusaSlicer MMU project: Metadata/Slic3r_PE.config palette plus
    slic3rpe:mmu_segmentation painted triangles (#717)."""
    vertices, faces = mesh
    x, y = at
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}" '
           'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">',
           '<metadata name="slic3rpe:Version3mf">1</metadata>',
           '<metadata name="Title">Corpus PrusaSlicer MMU project (CC0)</metadata>', '<resources>',
           f'<object id="1" name="{html.escape(name, quote=True)}" type="model"><mesh><vertices>']
    out += [f'<vertex x="{vx + x:.4f}" y="{vy + y:.4f}" z="{vz:.4f}"/>' for vx, vy, vz in vertices]
    out.append('</vertices><triangles>')
    # Upper half of the faces painted with extruder 2 ("8" in PrusaSlicer's encoding).
    half = len(faces) // 2
    out += [f'<triangle v1="{a}" v2="{b}" v3="{c}"' + (' slic3rpe:mmu_segmentation="8"' if t >= half else '') + '/>'
            for t, (a, b, c) in enumerate(faces)]
    out += ['</triangles></mesh></object>', '</resources><build><item objectid="1"/></build></model>']
    config = "\n".join([
        "; generated by PrusaSlicer 2.8.1 (corpus fixture, CC0)",
        "; extruder_colour = " + ";".join('""' for _ in colors),
        "; filament_colour = " + ";".join(colors),
        "; filament_type = " + ";".join("PLA" for _ in colors),
        "; printer_technology = FFF", ""])
    _write_package(path, {"3D/3dmodel.model": "\n".join(out), "Metadata/Slic3r_PE.config": config})


# 1x1 transparent PNG, so the texture2d reference resolves.
_BLANK_PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                           "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _write_package(path, members):
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="config" ContentType="text/plain"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        **members,
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            data = files[name]
            archive.writestr(info, data if isinstance(data, bytes) else data.encode())


def _mesh_xml(mesh, prefix=""):
    vertices, faces = mesh
    p = prefix
    return (f"<{p}mesh><{p}vertices>" + "".join(f'<{p}vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in vertices)
            + f"</{p}vertices><{p}triangles>" + "".join(f'<{p}triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
            + f"</{p}triangles></{p}mesh>")


def _transform(at):
    x, y, *z = at
    return f"1 0 0 0 1 0 0 0 1 {x} {y} {z[0] if z else 0}"


def write_raw_core_3mf(path, objects, items, *, unit="millimeter", prefix=""):
    """Plain core 3MF written exactly as described, valid or not, for intake and unit cases.

    objects: [(id, mesh or [(component id, at or None)])], items: [(object id, at or None)]. Ids are written as
    given and need not resolve (a dangling build item, a component cycle). prefix "ns0:" binds the core
    namespace to a prefix instead of the default namespace, the shape ElementTree writes (#814).
    """
    p = prefix
    xmlns = f'xmlns:{p[:-1]}="{NS}"' if p else f'xmlns="{NS}"'
    resources = []
    for oid, body in objects:
        if isinstance(body, tuple):
            resources.append(f'<{p}object id="{oid}" type="model">{_mesh_xml(body, p)}</{p}object>')
        else:
            parts = "".join(f'<{p}component objectid="{cid}"' + (f' transform="{_transform(at)}"' if at else "") + "/>"
                            for cid, at in body)
            resources.append(f'<{p}object id="{oid}" type="model"><{p}components>{parts}</{p}components></{p}object>')
    build = "".join(f'<{p}item objectid="{oid}"' + (f' transform="{_transform(at)}"' if at else "") + "/>"
                    for oid, at in items)
    _write_package(path, {"3D/3dmodel.model": (
        f'<?xml version="1.0" encoding="UTF-8"?><{p}model {xmlns} unit="{unit}" xml:lang="en-US">'
        f"<{p}resources>{''.join(resources)}</{p}resources><{p}build>{build}</{p}build></{p}model>")})


def torus_triangles(major_radius=28.0, minor_radius=9.7, major_segments=1000, minor_segments=500):
    """Stream a 75.4 x 75.4 x 19.4 mm torus as exactly one million facets."""
    tau = 2 * math.pi
    def vertex(i, j):
        u, v = tau * (i % major_segments) / major_segments, tau * (j % minor_segments) / minor_segments
        radial = major_radius + minor_radius * math.cos(v)
        return radial * math.cos(u), radial * math.sin(u), minor_radius * math.sin(v)
    for i in range(major_segments):
        for j in range(minor_segments):
            p00, p10 = vertex(i, j), vertex(i + 1, j)
            p11, p01 = vertex(i + 1, j + 1), vertex(i, j + 1)
            yield p00, p10, p11
            yield p00, p11, p01


def generate_large_torus(path):
    """Generate a 1,000,000-triangle test STL in the caller's temporary directory."""
    target = Path(path)
    write_binary_stl_stream(target, torus_triangles())
    return target


# ----------------------------------------------------------------------------- declarative specs (#824)
#
# A generated case is a folder with a case.toml. Its [model] table is the only description of the model
# file next to it, and this module the only writer: there is no hand written build() list to keep in step.
#
#   [model]
#   format = "3mf"          "3mf" (default), "stl", "core-3mf", "face-props-3mf", "prusa-mmu-3mf" or "raw-core-3mf"
#   layout = "plates"       3mf only: "plates" (explicit [[model.plate]]), "loose" (every part on one plate,
#                           auto placed when a part has no `at`) or "assembly" (all parts are bodies of one
#                           build item that must travel together)
#   fit = "tight"           optional: "roomy", "tight" or "none". Sizes the parts that carry a `footprint`
#                           from the merge planner (tests/corpus/fit.py), for the case's own [printer]
#   aspect = 0.8667         depth / width of the fitted footprint (default 1)
#   margin_mm = 5           optional margin over the planner's limit (tight: below it, none: above it)
#
#   [[model.part]]          one per object or body, in order
#   name = "Base"           extruder = 1 (default)   shape = "box" (default), "cylinder", "t", "degenerate"
#   size = [70, 50, 6]      mm: box w d h, cylinder radius height, t bar_w stem_w height bar_t depth
#   at = [60, 60]           build position of the part (3mf, core and face-props formats); a third value is Z
#   footprint = [1.0, 0.7]  with fit: the part's footprint as fractions of the fitted one; `height` is in mm
#
# `[[model.grid]]` stands for rows x cols parts in one entry (name = "Tile {row:02} {col:02}", rows, cols,
# origin, pitch, size, z, extruders = N, mix = [a, b] gives part extruder (col * a + row * b) % N + 1). Grid parts
# follow the explicit ones. `project = {...}` adds keys to project_settings.config (prime tower, a foreign
# slicer's embedded profile). The "raw-core-3mf" format writes ids and references exactly as given, valid or not:
# unit, prefix (a namespace prefix such as "ns0:"), `[[model.object]]` id + (shape, size) or components =
# [{object = 2, at = [x, y]}], and `[[model.item]]` object + optional at.
#
# Other keys: palette (filament hex colors), support, omit_plate_metadata, extra_empty_plates, [[model.plate]]
# name + parts (indices), STL flags (binary, invert, lying_solid), part drop_faces and color, core-3mf
# style and triangle_colors, prusa-mmu-3mf colors, [[model.layer_change]] top_z + extruder (height-based
# color changes, written as Metadata/custom_gcode_per_layer.xml). `[[matrix]]` entries (tests/corpus/matrix.toml) expand to
# whole cases, case.toml included, through tests/corpus/matrix.py.

SOURCE = Path(__file__).resolve().parent  # where the checked in specs are read; HERE is where files are written
DEFAULT_PALETTE = ["#FFFFFF", "#000000", "#FF00FF", "#067749", "#C0392B"]
GRID_GAP_MM = 10


def _sibling(name):
    import importlib
    return importlib.import_module(f"{__package__ or 'tests.corpus'}.{name}")


def _parts(model):
    """The model's parts: the explicit `[[model.part]]` entries, then each `[[model.grid]]` expanded."""
    parts = list(model.get("part", []))
    for grid in model.get("grid", []):
        ox, oy = grid["origin"]
        px, py = grid["pitch"]
        mix_col, mix_row = grid.get("mix", (1, 0))
        for row in range(grid["rows"]):
            for col in range(grid["cols"]):
                part = {"name": grid["name"].format(row=row, col=col), "size": grid["size"],
                        "extruder": (col * mix_col + row * mix_row) % grid["extruders"] + 1,
                        "at": [round(ox + col * px, 4), round(oy + row * py, 4)]}
                if "shape" in grid:
                    part["shape"] = grid["shape"]
                parts.append(part)
    return parts


def _mesh(part, width=None, depth=None):
    """The part's mesh, its size taken from `size` or, with a `footprint`, from the fitted footprint."""
    kind = part.get("shape", "box")
    size = part.get("size")
    if "footprint" in part:
        fx, fy = part["footprint"]
        size = [round(width * fx, 2), round(depth * fy, 2), part["height"]]
    if kind == "degenerate":
        return [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2), (0, 1, 2), (0, 1, 2)]
    builders = {"box": box, "cylinder": cylinder, "t": t_shape}
    if kind not in builders or not size:
        raise ValueError(f"part {part.get('name')!r} needs a shape of {sorted(builders)} and a size or footprint")
    vertices, faces = builders[kind](*size)
    drop = int(part.get("drop_faces", 0))
    return (vertices, faces[:-drop] if drop else faces)


def _positions(parts, meshes):
    """Build positions: each part's `at`, else a grid with gaps, so loose parts never share space."""
    free = [i for i, part in enumerate(parts) if "at" not in part]
    spans = [(max(v[0] for v in m[0]), max(v[1] for v in m[0])) for m in meshes]
    columns = max(1, math.ceil(math.sqrt(len(free))))
    cell = (math.ceil(max((spans[i][0] for i in free), default=0)) + GRID_GAP_MM,
            math.ceil(max((spans[i][1] for i in free), default=0)) + GRID_GAP_MM)
    out = []
    for index, part in enumerate(parts):
        if "at" in part:
            out.append(tuple(part["at"]))
        else:
            slot = free.index(index)
            out.append((GRID_GAP_MM + slot % columns * cell[0], GRID_GAP_MM + slot // columns * cell[1]))
    return out


def _palette(model, parts):
    colors = model.get("palette") or DEFAULT_PALETTE[:max(int(p.get("extruder", 1)) for p in parts)]
    return [(color, "PLA") for color in colors]


def model_suffix(model):
    return ".stl" if model.get("format", "3mf") == "stl" else ".3mf"


def render(data, target, width=None):
    """Write the model a case.toml's [model] table describes to `target`.

    `data` is the parsed case.toml. `width` overrides the fitted footprint width (the fit search tries
    candidate widths this way); left None, a `fit` resolves it from the planner.
    """
    model = data["model"]
    if model.get("format") == "raw-core-3mf":
        _render_raw_core(model, target)
        return
    parts = _parts(model)
    if "fit" in model and width is None:
        width = resolve_width(data)
    depth = None if width is None else round(width * float(model.get("aspect", 1.0)))
    meshes = [_mesh(part, width, depth) for part in parts]
    kind = model.get("format", "3mf")
    names = [part["name"] for part in parts]
    if kind == "stl":
        write_stl(target, meshes[0], binary=model.get("binary", True), invert=model.get("invert", False),
                  lying_solid=model.get("lying_solid", False))
        return
    places = _positions(parts, meshes)
    if kind == "core-3mf":
        write_core_3mf(target, [(n, part.get("color"), m, at) for n, part, m, at in zip(names, parts, meshes, places)],
                       style=model.get("style", "basematerials"), triangle_colors=model.get("triangle_colors"))
    elif kind == "face-props-3mf":
        write_face_props_3mf(target, names[0], meshes[0], places[0])
    elif kind == "prusa-mmu-3mf":
        write_prusa_mmu_3mf(target, names[0], meshes[0], places[0], model["colors"])
    elif kind == "3mf":
        palette = _palette(model, parts)
        extruders = [int(part.get("extruder", 1)) for part in parts]
        layout = model.get("layout", "plates")
        if layout == "assembly":
            write_assembly_3mf(target, list(zip(names, extruders, meshes)), palette,
                               at=tuple(model.get("at", (10, 10))))
            return
        if layout == "loose":
            plates = [(model.get("plate_name", "One plate"), list(range(len(parts))))]
        elif layout == "plates":
            plates = [(plate["name"], list(plate["parts"])) for plate in model["plate"]]
        else:
            raise ValueError(f"unknown layout {layout!r}: use plates, loose or assembly")
        write_3mf(target, list(zip(names, extruders, meshes, places)), plates, palette,
                  support=model.get("support", False), omit_plate_metadata=model.get("omit_plate_metadata", False),
                  extra_empty_plates=model.get("extra_empty_plates", 0), project_extra=model.get("project"))
        if model.get("layer_change"):
            _append_layer_changes(target, model, palette)
    else:
        raise ValueError(f"unknown model format {kind!r}")


def _append_layer_changes(path, model, palette):
    """Add Metadata/custom_gcode_per_layer.xml: [[model.layer_change]] entries
    (top_z, extruder) as tool changes at layer heights (#870), colored from the
    palette like a slicer would write them."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>', '<custom_gcodes_per_layer>', '<plate>',
             '<plate_info id="1"/>']
    for change in model["layer_change"]:
        extruder = int(change["extruder"])
        color = palette[extruder - 1][0] if 0 < extruder <= len(palette) else ""
        top_z = change["top_z"]
        top_z = int(top_z) if float(top_z) == int(float(top_z)) else top_z
        lines.append(f'<layer top_z="{top_z}" type="2" extruder="{extruder}" color="{color}" '
                     'extra="" gcode="tool_change"/>')
    lines += ['<mode value="MultiAsSingle"/>', '</plate>', '</custom_gcodes_per_layer>']
    info = zipfile.ZipInfo("Metadata/custom_gcode_per_layer.xml", STAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(info, "\n".join(lines) + "\n")


def _render_raw_core(model, target):
    objects = []
    for entry in model["object"]:
        if "components" in entry:
            objects.append((entry["id"], [(c["object"], c.get("at")) for c in entry["components"]]))
        else:
            objects.append((entry["id"], _mesh(entry)))
    write_raw_core_3mf(target, objects, [(item["object"], item.get("at")) for item in model["item"]],
                       unit=model.get("unit", "millimeter"), prefix=model.get("prefix", ""))


_WIDTHS = {}


def resolve_width(data):
    """The footprint width in mm a case's `fit` means on its `[printer]`, from the merge planner."""
    model, printer = data["model"], data.get("printer")
    key = json.dumps([model, printer], sort_keys=True, default=str)
    if key not in _WIDTHS:
        _WIDTHS[key] = _sibling("fit").fitted_width(
            lambda path, width: render(data, path, width), model["fit"],
            len({int(p.get("extruder", 1)) for p in _parts(model)}), printer, model.get("margin_mm"))
    return _WIDTHS[key]


def specs():
    """{case name: (case.toml text, parsed data)} for every generated case: [model] tables and the matrix.

    A matrix case's case.toml is itself generated, so a checked in copy of it is not read as a spec.
    """
    found = {}
    for case in _sibling("matrix").expand_all():
        found[case.name] = (case.text, tomllib.loads(case.text))
    for manifest in sorted((SOURCE / "cases").glob("*/case.toml")):
        if manifest.parent.name in found:
            continue
        text = manifest.read_text(encoding="utf-8")
        if text.startswith(_sibling("matrix").HEADER.splitlines()[0]):
            raise ValueError(f"{manifest.parent.name} was generated from the matrix, which no longer yields it: delete the folder")
        data = tomllib.loads(text)
        if "model" in data:
            found[manifest.parent.name] = (text, data)
    return found


def build():
    """Write every generated case's model (and the case.toml of matrix cases) under HERE/cases."""
    cases = HERE / "cases"
    matrix_names = {case.name for case in _sibling("matrix").expand_all()}
    for name, (text, data) in specs().items():
        folder = cases / name
        folder.mkdir(parents=True, exist_ok=True)
        render(data, folder / f"model{model_suffix(data['model'])}")
        if name in matrix_names:
            (folder / "case.toml").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.path.insert(0, str(HERE.parents[1]))  # so the sibling modules import when run as a script
    build()
    cases = HERE / "cases"
    counts = {suffix: sum(path.suffix.lower() == suffix for case in cases.iterdir() for path in case.iterdir())
              for suffix in (".3mf", ".stl")}
    print(f"wrote {counts['.3mf']} 3MF projects and {counts['.stl']} STL meshes", file=sys.stderr)
