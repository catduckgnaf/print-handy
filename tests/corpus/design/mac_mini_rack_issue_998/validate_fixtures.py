"""Checks the reference fixture artifacts, not a certification of mechanical safety."""
import hashlib
from pathlib import Path
import trimesh

ROOT = Path(__file__).resolve().parent
stls = {
    "01_CENTER_TRAY_214x214.stl": (214, 214, 24),
    "02_RIGHT_2U_ARM.stl": (172.3, 188, 88.5),
    "03_LEFT_2U_ARM.stl": (172.3, 188, 88.5),
    "00_TEST_LOWER_M4_NUT.stl": (24, 24, 10),
    "00_TEST_UPPER_M4_BUTTON_HEAD.stl": (24, 24, 10),
}
steps = [
    "01_CENTER_TRAY_214x214.step",
    "02_RIGHT_2U_ARM.step",
    "03_LEFT_2U_ARM.step",
    "ASSEMBLY_2014_MAC_MINI_2U.step",
    "REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step",
]
pngs = ["PREVIEW_assembled_rack_mount.png", "DIMENSIONED_OVERVIEW.png"]
errors = []
for name, expected in stls.items():
    f = ROOT / name
    if not f.is_file():
        errors.append("Missing STL: " + name)
        continue
    mesh = trimesh.load(str(f), force="mesh", process=False)
    dims = tuple(float(x) for x in mesh.extents)
    if not mesh.is_watertight:
        errors.append(f"{name}: mesh is not watertight")
    if any(abs(x-y) > 0.5 for x,y in zip(dims, expected)):
        errors.append(f"{name}: unexpected dimensions {dims}, expected {expected}")
    if mesh.volume <= 0:
        errors.append(f"{name}: nonpositive volume")
    print(f"{name} dimensions {dims}, watertight={mesh.is_watertight}")
for name in steps + pngs:
    if not (ROOT / name).is_file() or (ROOT / name).stat().st_size < 100:
        errors.append("Missing/empty artifact: " + name)
for name in pngs:
    if (ROOT/name).is_file() and (ROOT/name).read_bytes()[:8] != b"\\x89PNG\\r\\n\\x1a\\n":
        errors.append("Invalid PNG header: " + name)
# RECORD known fixture flaws explicitly. Do not confuse passing STL tests with safety.
(ROOT / "WARNING_NOT_VALIDATED.txt").write_text(
    "REFERENCE CORPUS ONLY, NOT A VERIFIED RACK MOUNT.\\n"
    "Known issue A: 88.5mm tall ears may exceed nominal 2U panel clearance ~88.11mm.\\n"
    "Known issue B: source assembly translates tray +10mm Z; mating faces/bolt holes do not register.\\n"
    "Known issue C: 214mm tray on 220mm bed leaves only 3mm nominal per edge.\\n"
    "Known issue D: no PLA mechanical, creep or actual print tests.\\n",
    encoding="utf8",
)
hashable = sorted(p for p in ROOT.iterdir() if p.is_file() and p.suffix.lower() in (".stl",".step",".png"))
(ROOT / "SHA256SUMS.txt").write_text(
    "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\\n" for p in hashable),
    encoding="utf8",
)
if errors:
    raise SystemExit("\\n".join(errors))
print("Reference geometry/file validation passed (NOT mechanical validation).")
