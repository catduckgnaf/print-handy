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
                                                  [--cura /path/to/cura]

Without a producer binary the script (re)builds only the spec-synthetic
fixtures and leaves genuine entries untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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

CUBE_STL = """solid cube
 facet normal 0 0 0
  outer loop
   vertex 0 0 0
   vertex 10 10 0
   vertex 10 0 0
  endloop
 endfacet
 facet normal 0 0 0
  outer loop
   vertex 0 0 0
   vertex 0 10 0
   vertex 10 10 0
  endloop
 endfacet
endsolid cube
"""


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prusa", default=None,
                        help="path to a real prusa-slicer binary")
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
                "basis": f"{args.prusa} --export-3mf --output {target.name} cube.stl",
                "sha256": _sha256(target)}
    provenance["fixtures"] = [by_name[name] for name in sorted(by_name)]
    provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"fixtures in {OUT}: {[row['file'] for row in provenance['fixtures']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
