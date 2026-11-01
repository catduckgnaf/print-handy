"""Write the corpus models. Run `python3 tests/corpus/make_models.py` to regenerate every model fixture.

The shapes are generated here, not copied from anyone, so each one is CC0 and safe to commit.
Fixture content is deterministic (fixed ZIP timestamps and sorted entries); compressed ZIP bytes may vary by zlib version.
Only the standard library is used. A million-triangle torus is generated at test time only.
"""
import html
import json
import math
import struct
import sys
import zipfile
from pathlib import Path

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
    """3dmodel.model. objects: [(id, kind, payload)]; items: [(object_id, (x, y))]."""
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
            out.append(f'<object id="{oid}" type="model"><components>'
                       f'<component objectid="{payload}"/></components></object>')
    out.append('</resources><build>')
    for oid, (x, y) in items:
        out.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {x} {y} 0"/>')
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


def write_3mf(path, shapes, plates, palette, support=False, *, omit_plate_metadata=False, extra_empty_plates=0):
    """shapes: [(name, extruder, mesh, (x, y))]; plates: [(plate name, [shape index])]."""
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


def build():
    cases = HERE / "cases"
    write_3mf(cases / "parts-one-plate/model.3mf",
              [("Base", 1, box(70, 50, 6), (60, 60)),
               ("Post", 1, cylinder(10, 30), (70, 130)),
               ("Tab", 1, box(30, 12, 4), (130, 140))],
              # Post is assigned to both plates; production tolerates the
              # overlap and prints it with the later plate (see the case).
              [("Plate 1", [0, 1]), ("Plate 2", [1, 2])], [("#FFFFFF", "PLA")])
    write_3mf(cases / "plates-multicolor/model.3mf",
              [("Body", 1, box(60, 40, 12), (50, 60)), ("Lid", 2, box(60, 40, 4), (50, 60)),
               ("Knob", 3, cylinder(9, 14), (60, 130))],
              [("Body", [0]), ("Lid and knob", [1, 2])],
              [("#FFFFFF", "PLA"), ("#000000", "PLA"), ("#C0392B", "PLA")])
    write_3mf(cases / "needs-supports/model.3mf",
              [("T bracket", 1, t_shape(60, 12, 40, 6, 30), (80, 90))],
              [("Plate 1", [0])], [("#FFFFFF", "PLA")], support=True)
    write_3mf(cases / "must-shrink/model.3mf", [("Big plate", 1, box(260, 260, 230), (0, 0))],
              [("Plate 1", [0])], [("#FFFFFF", "PLA")])
    write_3mf(cases / "split-plan/model.3mf",
              [("Left half", 1, box(150, 150, 10), (0, 0)), ("Right half", 1, box(150, 150, 10), (160, 0))],
              [("Plate 1", [0, 1])], [("#FFFFFF", "PLA")])
    write_3mf(cases / "single-part/model.3mf", [("Cube", 1, box(30, 30, 20), (90, 90))],
              [("Plate 1", [0])], [("#FFFFFF", "PLA")])
    names8 = [(f"Part {i}", 1, box(10, 10, 5), (10 + ((i - 1) % 4) * 22, 10 + ((i - 1) // 4) * 22)) for i in range(1, 9)]
    write_3mf(cases / "many-parts/model.3mf", names8, [("One plate", list(range(8)))], [("#FFFFFF", "PLA")])
    palette = [("#FFFFFF", "PLA"), ("#000000", "PLA"), ("#FF00FF", "PLA"), ("#067749", "PLA"), ("#C0392B", "PLA")]
    four = [(f"Part {i}", i, box(18, 18, 5), (15 + (i - 1) * 28, 40)) for i in range(1, 5)]
    write_3mf(cases / "four-colors-one-plate/model.3mf", four, [("One plate", [0, 1, 2, 3])], palette[:4])
    five = [(f"Part {i}", i, box(15, 15, 5), (10 + (i - 1) * 24, 50)) for i in range(1, 6)]
    write_3mf(cases / "five-colors-over-slots/model.3mf", five, [("One plate", [0, 1, 2, 3, 4])], palette)
    two = [("Plate A", 1, box(150, 150, 10), (0, 0)), ("Plate B", 1, box(150, 150, 10), (0, 0))]
    write_3mf(cases / "multiplate-select/model.3mf", two, [("Plate A", [0]), ("Plate B", [1])], [palette[0]])
    write_3mf(cases / "unicode-part-extra-plate/model.3mf",
              [("Unicode 🐙 – part", 1, box(25, 25, 6), (60, 60))],
              [("One plate", [0])], [palette[0]], extra_empty_plates=1)
    write_3mf(cases / "rotation-fit/model.3mf", [("Rotatable plate", 1, box(223, 100, 8), (0, 0))],
              [("Plate 1", [0])], [palette[0]])
    write_stl(cases / "stl-single-part/model.stl", box(30, 30, 20))
    write_stl(cases / "stl-must-shrink/model.stl", box(260, 260, 230))
    write_stl(cases / "stl-needs-supports/model.stl", t_shape(60, 12, 40, 6, 30))
    write_stl(cases / "stl-thin-footprint/model.stl", box(8, 8, 180))
    write_stl(cases / "stl-ascii/model.stl", box(24, 18, 8), binary=False)
    write_stl(cases / "stl-text-solid-header/model.stl", box(24, 18, 8), lying_solid=True)
    write_stl(cases / "stl-tiny/model.stl", box(1.5, 1.5, 1.5))
    write_stl(cases / "stl-open-thin-wall/model.stl", box(30, 30, 1.0)[:1] + (box(30, 30, 1.0)[1][:-2],))
    write_stl(cases / "stl-degenerate-zero-volume/model.stl", ([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2), (0, 1, 2), (0, 1, 2)]))
    write_stl(cases / "stl-inverted-normals/model.stl", box(24, 18, 8), invert=True)
    write_stl(cases / "stl-tall/model.stl", box(12, 16, 275))
    write_stl(cases / "stl-very-thin-wall/model.stl", box(0.25, 20, 15))
    write_3mf(cases / "duplicate-part-names/model.3mf",
              [("Duplicate", 1, box(18, 18, 5), (20, 40)), ("Duplicate", 1, box(18, 18, 5), (60, 40))],
              [("One plate", [0, 1])], [palette[0]])
    write_3mf(cases / "missing-plate-metadata/model.3mf",
              [("No plate metadata", 1, box(24, 18, 8), (40, 50))],
              [("One plate", [0])], [palette[0]], omit_plate_metadata=True)
    # Plain core 3MFs with core material colors and no slicer metadata (#711, #714).
    write_core_3mf(cases / "core-basematerials-parts/model.3mf",
                   [("body", "#FAF9F7", box(30, 30, 20), (40, 60)), ("hat", "#383845", box(20, 20, 15), (80, 60)),
                    ("band", "#FFB3C4", box(20, 6, 4), (120, 60)), ("gold", "#067749", cylinder(5, 4), (40, 110))])
    write_core_3mf(cases / "core-colorgroup-items/model.3mf",
                   [("Left", "#FFFFFF", box(25, 25, 8), (40, 60)), ("Middle", "#000000", box(25, 25, 8), (80, 60)),
                    ("Right", "#FF80C0", box(25, 25, 8), (120, 60))], style="colorgroup")
    write_core_3mf(cases / "core-one-color/model.3mf",
                   [("Gear", "#C0392B", cylinder(15, 6), (50, 60)), ("Axle", "#C0392B", cylinder(4, 20), (100, 60))])
    write_core_3mf(cases / "core-triangle-colors/model.3mf",
                   [("Checker", None, box(30, 30, 10), (60, 60))], triangle_colors=["#FFFFFF", "#000000"])
    write_face_props_3mf(cases / "core-multiproperties-texture/model.3mf", "Swatch", box(30, 30, 6), (60, 60))
    write_prusa_mmu_3mf(cases / "prusa-mmu-project/model.3mf", "Banner", box(40, 20, 10), (60, 60),
                        ["#E74C3C", "#2980B9"])
    quantity_parts = [("Quantity A", 1, box(35, 30, 8), (35, 40)),
                      ("Quantity B", 1, box(35, 30, 8), (95, 40))]
    write_3mf(cases / "copies-multiple-plates/model.3mf", quantity_parts,
              [("Plate A", [0]), ("Plate B", [1])], [palette[0]])


if __name__ == "__main__":
    build()
    cases = HERE / "cases"
    counts = {suffix: sum(path.suffix.lower() == suffix for case in cases.iterdir() for path in case.iterdir())
              for suffix in (".3mf", ".stl")}
    print(f"wrote {counts['.3mf']} 3MF projects and {counts['.stl']} STL meshes", file=sys.stderr)
