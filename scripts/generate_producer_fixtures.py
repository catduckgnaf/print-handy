#!/usr/bin/env python3
"""Generate the cross-slicer producer fixtures for issue #1525.

Two honesty classes, never blurred:

* ``genuine`` — a real producer binary made the file. The command, the
  binary version and the file SHA-256 are recorded in
  ``tests/corpus/producer-fixtures/PROVENANCE.json``.
* ``spec-synthetic`` — the file was built here from the producer's
  published or open-source format (for Cura: setting stacks serialized
  as INI-style ``.cfg`` members under ``Cura/``, per Cura's workspace
  writer). It is labeled spec-synthetic everywhere it is used and never
  stands in for genuine-producer evidence.

Usage:
    python3 scripts/generate_producer_fixtures.py [--prusa /path/to/prusa-slicer]
                                                  [--orca /path/to/orca-slicer]
                                                  [--bambu /path/to/bambu-studio]
                                                  [--cura-genuine /path/to/cura-saved.3mf]

Without a producer binary the script (re)builds only the spec-synthetic
fixtures and leaves genuine entries untouched. The Orca/Bambu exports
run under a display (wrap the call in xvfb-run on a headless machine),
and a file is recorded as genuine only when its own archive carries
the producer proof: an OrcaSlicer metadata element for OrcaSlicer, an
Application metadata naming BambuStudio (and no OrcaSlicer element)
for Bambu Studio.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "corpus" / "producer-fixtures"
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

CUBE_MODEL = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="{NS}">
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
     <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
     <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
     <vertex x="0" y="0" z="10"/><vertex x="10" y="0" z="10"/>
     <vertex x="10" y="10" z="10"/><vertex x="0" y="10" z="10"/>
    </vertices>
    <triangles>
     <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="3" v3="2"/>
     <triangle v1="4" v2="5" v3="6"/><triangle v1="4" v2="6" v3="7"/>
     <triangle v1="0" v2="1" v3="5"/><triangle v1="0" v2="5" v3="4"/>
     <triangle v1="2" v2="3" v3="7"/><triangle v1="2" v2="7" v3="6"/>
     <triangle v1="1" v2="2" v3="6"/><triangle v1="1" v2="6" v3="5"/>
     <triangle v1="3" v2="0" v3="4"/><triangle v1="3" v2="4" v3="7"/>
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1"/>
 </build>
</model>
"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

# Cura stack serialization shape: each container stack becomes one INI
# file under Cura/ in the archive; settings live in its [values] section.
# The global stack below carries the validated-subset keys plus machine
# and G-code keys a translation must never carry.
CURA_GLOBAL_CFG = """[general]
version = 4
name = spec-synthetic

[metadata]
setting_version = 22

[values]
layer_height = 0.16
initial_layer_height = 0.24
wall_line_count = 3
top_layers = 5
bottom_layers = 4
infill_sparse_density = 25
infill_pattern = gyroid
support_enable = True
support_structure = tree
support_placement = everywhere
adhesion_type = brim
brim_width = 6
z_seam_type = sharpest_corner
machine_name = SpecSynthetic Machine
machine_start_gcode = G28 ; SPEC SYNTHETIC START - MUST NOT LEAK
machine_end_gcode = M104 S0 ; SPEC SYNTHETIC END - MUST NOT LEAK
material_print_temperature = 215
material_bed_temperature = 70
machine_width = 220
machine_depth = 220
machine_height = 250
"""

def _cube_stl(size: float = 20.0) -> str:
    """A closed, outward-facing cube (12 facets) as ASCII STL.

    A real solid: a flat square has no volume, and a slicer drops it as
    "no object fully inside the print volume" (#1578 review).
    """
    s = size
    corners = {"a": (0, 0, 0), "b": (s, 0, 0), "c": (s, s, 0), "d": (0, s, 0),
               "e": (0, 0, s), "f": (s, 0, s), "g": (s, s, s), "h": (0, s, s)}
    faces = ("acb", "adc", "efg", "egh", "abf", "afe",
             "bcg", "bgf", "cdh", "chg", "dae", "deh")
    lines = ["solid cube"]
    for face in faces:
        lines += [" facet normal 0 0 0", "  outer loop"]
        lines += ["   vertex %g %g %g" % corners[k] for k in face]
        lines += ["  endloop", " endfacet"]
    lines.append("endsolid cube")
    return "\n".join(lines) + "\n"


CUBE_STL = _cube_stl()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_cura_spec_synthetic() -> Path:
    target = OUT / "cura-spec-synthetic.3mf"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("3D/3dmodel.model", CUBE_MODEL)
        archive.writestr("Cura/global.cfg", CURA_GLOBAL_CFG)
    return target


PRUSA_RICH_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">
 <metadata name="Title">prusa-rich-spec-synthetic</metadata>
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
     <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
     <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
     <vertex x="0" y="0" z="10"/><vertex x="10" y="0" z="10"/>
     <vertex x="10" y="10" z="10"/><vertex x="0" y="10" z="10"/>
    </vertices>
    <triangles>
     <triangle v1="0" v2="2" v3="1" slic3rpe:mmu_segmentation="8"/>
     <triangle v1="0" v2="3" v3="2" slic3rpe:mmu_segmentation="8"/>
     <triangle v1="0" v2="1" v3="5"/>
     <triangle v1="0" v2="5" v3="4"/>
     <triangle v1="4" v2="5" v3="6"/>
     <triangle v1="4" v2="6" v3="7"/>
     <triangle v1="2" v2="3" v3="7"/>
     <triangle v1="2" v2="7" v3="6"/>
     <triangle v1="1" v2="2" v3="6"/>
     <triangle v1="1" v2="6" v3="5"/>
     <triangle v1="3" v2="0" v3="4"/>
     <triangle v1="3" v2="4" v3="7"/>
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>
 </build>
</model>
"""

# Volume triangle ranges partition the mesh exactly (0-11), as a real
# PrusaSlicer project save lays them out: a model part on extruder 1, a
# parameter modifier on extruder 2, then negative, support-blocker and
# support-enforcer volumes.
PRUSA_RICH_VOLUMES = (
    ("body", "ModelPart", 0, 3, 1),
    ("mod", "ParameterModifier", 4, 5, 2),
    ("cut", "NegativeVolume", 6, 7, None),
    ("block", "SupportBlocker", 8, 9, None),
    ("force", "SupportEnforcer", 10, 11, None),
)

PRUSA_RICH_CONFIG = """; generated by PrusaSlicer-format fixture builder (spec-synthetic)
; filament_colour = #FF0000;#00FF00;#0000FF;#FFFF00
; extruder_colour = #FF0000;#00FF00;#0000FF;#FFFF00
; filament_type = PLA;PLA;PETG;PLA
; layer_height = 0.2
; perimeters = 2
; fill_density = 20%
; support_material = 1
; support_material_extruder = 4
; support_material_interface_extruder = 0
"""


def _prusa_rich_model_config() -> str:
    volumes = []
    for name, kind, first, last, extruder in PRUSA_RICH_VOLUMES:
        ext = (f'\n   <metadata type="volume" key="extruder" value="{extruder}"/>'
               if extruder else "")
        volumes.append(
            f'  <volume firstid="{first}" lastid="{last}">\n'
            f'   <metadata type="volume" key="name" value="{name}"/>\n'
            f'   <metadata type="volume" key="volume_type" value="{kind}"/>{ext}\n'
            f'  </volume>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
            ' <object id="1" instances_count="1">\n'
            '  <metadata type="object" key="name" value="cube"/>\n'
            '  <metadata type="object" key="extruder" value="1"/>\n'
            + "\n".join(volumes) + '\n </object>\n</config>\n')


def build_prusa_rich_spec_synthetic() -> Path:
    """A Prusa-format project with the roles fidelity is claimed for.

    Spec-synthetic: built here from PrusaSlicer's open 3MF layout (a
    Slic3r_PE.config settings snapshot, a Slic3r_PE_model.config whose
    volumes partition the mesh, MMU segmentation paint on two
    triangles, and a support-only filament named by
    support_material_extruder that no part prints with). No PrusaSlicer
    binary produced it; PROVENANCE.json says so.
    """
    target = OUT / "prusa-rich-spec-synthetic.3mf"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("3D/3dmodel.model", PRUSA_RICH_MODEL)
        archive.writestr("Metadata/Slic3r_PE.config", PRUSA_RICH_CONFIG)
        archive.writestr("Metadata/Slic3r_PE_model.config", _prusa_rich_model_config())
    return target


def build_prusa_genuine(prusa_binary: str, work: Path) -> tuple[Path, str] | None:
    """Run a real PrusaSlicer CLI to export a genuine .3mf, or None."""
    stl = work / "cube.stl"
    stl.write_text(CUBE_STL)
    target = OUT / "prusa-genuine.3mf"
    version = subprocess.run([prusa_binary, "--help"], capture_output=True,
                             text=True, timeout=120)
    banner = (version.stdout + version.stderr).strip().splitlines()
    result = subprocess.run(
        [prusa_binary, "--export-3mf", "--output", str(target), str(stl)],
        capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not target.is_file():
        print(f"PrusaSlicer genuine export failed: {result.stderr[-500:]}",
              file=sys.stderr)
        return None
    return target, banner[0] if banner else "PrusaSlicer"


def build_orca_genuine(orca_binary: str, work: Path) -> tuple[Path, str] | None:
    """Run a real OrcaSlicer CLI to export a genuine .3mf, or None.

    The archive itself must prove the producer: a genuine OrcaSlicer
    save carries an OrcaSlicer metadata element in its root model
    (its Application metadata names the Bambu base, so that element
    is the proof, and its value is the producer version).
    """
    work.mkdir(parents=True, exist_ok=True)
    stl = work / "cube.stl"
    stl.write_text(CUBE_STL)
    out = work / "orca-out"
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [orca_binary, "--export-3mf", "orca-genuine.3mf",
         "--outputdir", str(out), str(stl)],
        capture_output=True, text=True, timeout=300)
    made = out / "orca-genuine.3mf"
    if result.returncode != 0 or not made.is_file():
        print(f"OrcaSlicer genuine export failed: {result.stderr[-500:]}",
              file=sys.stderr)
        return None
    with zipfile.ZipFile(made) as archive:
        model = archive.read("3D/3dmodel.model").decode("utf-8", "replace")
    match = re.search(r'<metadata name="OrcaSlicer">\s*([^<]+?)\s*</metadata>', model)
    if not match:
        print("OrcaSlicer export carries no OrcaSlicer metadata; "
              "refusing to call it genuine", file=sys.stderr)
        return None
    target = OUT / "orca-genuine.3mf"
    shutil.copyfile(made, target)
    return target, f"OrcaSlicer-{match.group(1)}"


def build_bambu_genuine(bambu_binary: str, work: Path) -> tuple[Path, str] | None:
    """Run a real Bambu Studio CLI to export a genuine .3mf, or None.

    The archive itself must prove the producer: its Application
    metadata names BambuStudio with its version, and it carries no
    OrcaSlicer metadata element (which would make it an Orca save).
    """
    work.mkdir(parents=True, exist_ok=True)
    stl = work / "cube.stl"
    stl.write_text(CUBE_STL)
    out = work / "bambu-out"
    out.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [bambu_binary, "--export-3mf", "bambu-genuine.3mf",
         "--outputdir", str(out), str(stl)],
        capture_output=True, text=True, timeout=300)
    made = out / "bambu-genuine.3mf"
    if result.returncode != 0 or not made.is_file():
        print(f"Bambu Studio genuine export failed: {result.stderr[-500:]}",
              file=sys.stderr)
        return None
    with zipfile.ZipFile(made) as archive:
        model = archive.read("3D/3dmodel.model").decode("utf-8", "replace")
    match = re.search(r'<metadata name="Application">\s*(BambuStudio-[^<]+?)\s*</metadata>',
                      model)
    if not match:
        print("Bambu Studio export does not name BambuStudio in its "
              "Application metadata; refusing to call it genuine",
              file=sys.stderr)
        return None
    if re.search(r'<metadata name="OrcaSlicer">', model):
        print("Bambu Studio export carries an OrcaSlicer metadata "
              "element; refusing to call it a Bambu save", file=sys.stderr)
        return None
    target = OUT / "bambu-genuine.3mf"
    shutil.copyfile(made, target)
    return target, match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prusa", default=None,
                        help="path to a real prusa-slicer binary")
    parser.add_argument("--orca", default=None,
                        help="path to a real OrcaSlicer binary (or its "
                             "extracted AppImage AppRun)")
    parser.add_argument("--bambu", default=None,
                        help="path to a real Bambu Studio binary (or its "
                             "extracted AppImage AppRun)")
    parser.add_argument("--cura-genuine", default=None,
                        help="path to a project file the real Cura "
                             "application saved through its GUI; recorded "
                             "as the genuine Cura fixture after its own "
                             "Cura/version.ini is checked")
    parser.add_argument("--work", default=None,
                        help="scratch directory (defaults to a temp dir)")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    provenance_path = OUT / "PROVENANCE.json"
    provenance = {"schema_version": 1, "fixtures": []}
    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text())
    by_name = {row["file"]: row for row in provenance.get("fixtures", [])}

    target = build_cura_spec_synthetic()
    by_name[target.name] = {
        "file": target.name, "class": "spec-synthetic",
        "producer": "UltiMaker Cura (format)",
        "basis": "Cura workspace writer layout: setting stacks serialized as "
                 "INI .cfg members under Cura/; built by "
                 "scripts/generate_producer_fixtures.py, not by a Cura binary.",
        "sha256": _sha256(target)}

    target = build_prusa_rich_spec_synthetic()
    by_name[target.name] = {
        "file": target.name, "class": "spec-synthetic",
        "producer": "PrusaSlicer (format)",
        "basis": "PrusaSlicer 3MF layout: Slic3r_PE.config settings snapshot "
                 "(four filaments, support_material_extruder naming a "
                 "support-only filament), Slic3r_PE_model.config volumes "
                 "partitioning the mesh (ModelPart, ParameterModifier, "
                 "NegativeVolume, SupportBlocker, SupportEnforcer) and "
                 "slic3rpe:mmu_segmentation paint; built by "
                 "scripts/generate_producer_fixtures.py, not by a "
                 "PrusaSlicer binary.",
        "sha256": _sha256(target)}

    if args.prusa:
        import tempfile
        work = Path(args.work) if args.work else Path(
            tempfile.mkdtemp(prefix="producer-fixtures-"))
        work.mkdir(parents=True, exist_ok=True)
        made = build_prusa_genuine(args.prusa, work)
        if made:
            target, banner = made
            by_name[target.name] = {
                "file": target.name, "class": "genuine",
                "producer": banner,
                # The binary's name only: a local install path is not
                # provenance and must not land in a public fixture record.
                "basis": f"{Path(args.prusa).name} --export-3mf --output "
                         f"{target.name} cube.stl (a closed 20 mm cube)",
                "sha256": _sha256(target)}
    for flag, builder in (("orca", build_orca_genuine),
                          ("bambu", build_bambu_genuine)):
        binary = getattr(args, flag)
        if not binary:
            continue
        import tempfile
        work = Path(args.work) if args.work else Path(
            tempfile.mkdtemp(prefix="producer-fixtures-"))
        work.mkdir(parents=True, exist_ok=True)
        made = builder(binary, work)
        if made:
            target, producer = made
            by_name[target.name] = {
                "file": target.name, "class": "genuine",
                "producer": producer,
                # The binary's name only: a local install path is not
                # provenance and must not land in a public fixture record.
                "basis": f"{Path(binary).name} --export-3mf {target.name} "
                         f"cube.stl (a closed 20 mm cube); the archive's "
                         f"own metadata records {producer} as its producer",
                "sha256": _sha256(target)}
    if args.cura_genuine:
        # Cura has no headless project save: the genuine fixture is a
        # project the real application saved through its own GUI (first
        # run wizard, a local UltiMaker S5, the cube opened, infill
        # pattern changed to Gyroid, File > Save Project). Record it
        # only when its own Cura/version.ini proves the version.
        import shutil
        source = Path(args.cura_genuine)
        target = OUT / "cura-genuine.3mf"
        shutil.copyfile(source, target)
        with zipfile.ZipFile(target) as archive:
            version = archive.read("Cura/version.ini").decode("utf-8", "replace")
        if "cura_version = 5.8.1" not in version:
            raise SystemExit(
                f"{source} does not record cura_version = 5.8.1 in "
                "Cura/version.ini; refusing to call it genuine")
        by_name[target.name] = {
            "file": target.name, "class": "genuine",
            "producer": "UltiMaker Cura 5.8.1",
            "basis": "Saved by the real UltiMaker Cura 5.8.1 application "
                     "(official AppImage, SHA-256 "
                     "54b77e574d0b851658a6565b2a4129e035eca80840f562d0845f77dd"
                     "0019457d as pinned in "
                     ".github/workflows/cross-slicer-extended.yml) through "
                     "its own GUI under Xvfb: first-run wizard completed "
                     "with a local UltiMaker S5, a closed 20 mm cube "
                     "opened, the infill pattern changed to Gyroid, then "
                     "File > Save Project. The archive's own "
                     "Cura/version.ini records cura_version = 5.8.1, and "
                     "both extruder user stacks carry the changed "
                     "infill_pattern = gyroid value.",
            "sha256": _sha256(target)}
    provenance["fixtures"] = [by_name[name] for name in sorted(by_name)]
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"fixtures in {OUT}: {[row['file'] for row in provenance['fixtures']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
