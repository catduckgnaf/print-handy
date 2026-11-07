"""Verify the issue #998 reference fixture artifacts.

This is a checker, not a certification of mechanical safety and not a
generator: in its default mode it writes nothing, so drift between the
committed files and the committed SHA256SUMS.txt / WARNING_NOT_VALIDATED.txt
is reported instead of being silently overwritten. Regeneration tooling can
pass --write-manifest to refresh those two files after rebuilding the
binaries (see FIXTURE_README.md).

Checks, all against the files as committed:

- inventory: every expected STL, STEP, PNG, source and document is present;
- SHA256SUMS.txt covers exactly the 12 binary artifacts (5 STL, 5 STEP,
  2 PNG) and every listed hash matches the file on disk. The regenerated
  ZIP is deliberately out of manifest scope: it is a derived bundle, it
  cannot hash itself, and it is verified directly instead (next check);
- the regenerated ZIP holds exactly the original 16-file layout under the
  mac_mini_2014_2U_rack_mount/ prefix, and every member's bytes are
  identical to the same-named file in this directory;
- PNG files carry a real PNG header; STEP files carry an ISO-10303-21
  header;
- STL bounding boxes match the recorded dimensions within 0.5 mm, meshes
  have positive volume, and (when trimesh is installed, as it is in the
  test environment) every mesh is watertight;
- the deliberate known defects of this reference design stay pinned, so a
  regeneration that quietly changes them fails here: the 88.5 mm rack ears
  still exceed the ~88.11 mm common 2U faceplate clearance, the generator
  still translates the tray +10 mm Z in the assembly export so its bolt
  holes do not register with the arms, and the 214 mm tray still leaves
  only 3 mm of margin per side on a 220 mm plate;
- WARNING_NOT_VALIDATED.txt still records all four known issues.

Run directly: python validate_fixtures.py [--write-manifest]
The pytest guard in tests/corpus/test_mac_mini_rack_fixture_998.py calls
collect_errors() so the same checks run in the offline CI suite.
"""
import hashlib
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

STLS = {
    "01_CENTER_TRAY_214x214.stl": (214, 214, 24),
    "02_RIGHT_2U_ARM.stl": (172.3, 188, 88.5),
    "03_LEFT_2U_ARM.stl": (172.3, 188, 88.5),
    "00_TEST_LOWER_M4_NUT.stl": (24, 24, 10),
    "00_TEST_UPPER_M4_BUTTON_HEAD.stl": (24, 24, 10),
}
STEPS = [
    "01_CENTER_TRAY_214x214.step",
    "02_RIGHT_2U_ARM.step",
    "03_LEFT_2U_ARM.step",
    "ASSEMBLY_2014_MAC_MINI_2U.step",
    "REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step",
]
PNGS = ["PREVIEW_assembled_rack_mount.png", "DIMENSIONED_OVERVIEW.png"]
SOURCES = ["generate_rack_mount.py", "make_previews.py", "make_dimension_diagram.py"]
DOCUMENTS = ["FIXTURE_README.md", "README_ASSEMBLY_AND_PRINTING.md", "WARNING_NOT_VALIDATED.txt"]
ZIP_NAME = "2014_Mac_Mini_19inch_2U_PLA_Rack_Mount_v1_REGENERATED.zip"
ZIP_PREFIX = "mac_mini_2014_2U_rack_mount/"
# The original 16-file ZIP layout the regenerated bundle must preserve.
ZIP_MEMBERS = [
    "README_ASSEMBLY_AND_PRINTING.md",
    "01_CENTER_TRAY_214x214.stl", "01_CENTER_TRAY_214x214.step",
    "02_RIGHT_2U_ARM.stl", "02_RIGHT_2U_ARM.step",
    "03_LEFT_2U_ARM.stl", "03_LEFT_2U_ARM.step",
    "00_TEST_LOWER_M4_NUT.stl", "00_TEST_UPPER_M4_BUTTON_HEAD.stl",
    "ASSEMBLY_2014_MAC_MINI_2U.step",
    "REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step",
    "generate_rack_mount.py", "make_previews.py", "make_dimension_diagram.py",
    "PREVIEW_assembled_rack_mount.png", "DIMENSIONED_OVERVIEW.png",
]
WARNING_TEXT = (
    "REFERENCE CORPUS ONLY, NOT A VERIFIED RACK MOUNT.\n"
    "Known issue A: 88.5mm tall ears may exceed nominal 2U panel clearance ~88.11mm.\n"
    "Known issue B: source assembly translates tray +10mm Z; mating faces/bolt holes do not register.\n"
    "Known issue C: 214mm tray on 220mm bed leaves only 3mm nominal per edge.\n"
    "Known issue D: no PLA mechanical, creep or actual print tests.\n"
)
# Deliberate defect pins (see module docstring).
EAR_HEIGHT_MM = 88.5
FACEPLATE_CLEARANCE_MM = 88.11
TRAY_MM = 214.0
PLATE_MM = 220.0


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binary_stl_extents(path):
    """(extents, triangle count) for a binary STL, stdlib only; None if not binary STL."""
    data = path.read_bytes()
    if len(data) < 84:
        return None
    (count,) = struct.unpack("<I", data[80:84])
    if 84 + 50 * count != len(data):
        return None
    mins = [float("inf")] * 3
    maxs = [float("-inf")] * 3
    for i in range(count):
        off = 84 + i * 50 + 12
        for v in range(3):
            vertex = struct.unpack("<fff", data[off + v * 12: off + v * 12 + 12])
            for axis, value in enumerate(vertex):
                mins[axis] = min(mins[axis], value)
                maxs[axis] = max(maxs[axis], value)
    return tuple(maxs[a] - mins[a] for a in range(3)), count


def manifest_entries(root=ROOT):
    """The {name: sha256} map SHA256SUMS.txt is expected to carry: the binary artifacts only."""
    names = sorted(p.name for p in root.iterdir() if p.is_file() and p.suffix.lower() in (".stl", ".step", ".png"))
    return {name: _sha256(root / name) for name in names}


def write_manifest(root=ROOT):
    """Regeneration helper: refresh SHA256SUMS.txt and WARNING_NOT_VALIDATED.txt."""
    entries = manifest_entries(root)
    (root / "SHA256SUMS.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in entries.items()), encoding="utf8")
    (root / "WARNING_NOT_VALIDATED.txt").write_text(WARNING_TEXT, encoding="utf8")


def collect_errors(root=ROOT, log=print):
    errors = []
    for name in list(STLS) + STEPS + PNGS + SOURCES + DOCUMENTS + [ZIP_NAME, "SHA256SUMS.txt"]:
        f = root / name
        if not f.is_file() or f.stat().st_size < 10:
            errors.append("Missing/empty artifact: " + name)

    try:
        import trimesh
    except ImportError:
        trimesh = None
        log("trimesh not installed: watertight/volume checks skipped, bounding boxes checked with stdlib parser")

    for name, expected in STLS.items():
        f = root / name
        if not f.is_file():
            continue
        if trimesh is not None:
            mesh = trimesh.load(str(f), force="mesh", process=True)
            dims = tuple(float(x) for x in mesh.extents)
            if not mesh.is_watertight:
                errors.append(f"{name}: mesh is not watertight")
            if mesh.volume <= 0:
                errors.append(f"{name}: nonpositive volume")
        else:
            parsed = _binary_stl_extents(f)
            if parsed is None:
                errors.append(f"{name}: not a binary STL of the recorded size")
                continue
            dims, _count = parsed
        if any(abs(x - y) > 0.5 for x, y in zip(dims, expected)):
            errors.append(f"{name}: unexpected dimensions {dims}, expected {expected}")
        log(f"{name} dimensions {dims}")

    for name in PNGS:
        f = root / name
        if f.is_file() and f.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            errors.append("Invalid PNG header: " + name)
    for name in STEPS:
        f = root / name
        if f.is_file() and not f.read_bytes().startswith(b"ISO-10303-21;"):
            errors.append("Invalid STEP header: " + name)

    # SHA256SUMS.txt must cover exactly the binary artifacts and match disk.
    manifest_path = root / "SHA256SUMS.txt"
    if manifest_path.is_file():
        listed = {}
        for line in manifest_path.read_text(encoding="utf8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            listed[name.strip()] = digest.strip()
        expected_entries = manifest_entries(root)
        if set(listed) != set(expected_entries):
            errors.append(
                f"SHA256SUMS.txt covers {sorted(listed)}, expected exactly {sorted(expected_entries)}")
        for name, digest in listed.items():
            if name in expected_entries and digest != expected_entries[name]:
                errors.append(f"SHA256SUMS.txt hash mismatch: {name}")

    # The regenerated ZIP must bundle exactly the original 16 files, byte-identical to disk.
    zip_path = root / ZIP_NAME
    if zip_path.is_file():
        with zipfile.ZipFile(zip_path) as bundle:
            names = bundle.namelist()
            expected_names = [ZIP_PREFIX + name for name in ZIP_MEMBERS]
            if names != expected_names:
                errors.append(f"ZIP layout {names}, expected {expected_names}")
            for name in ZIP_MEMBERS:
                member = ZIP_PREFIX + name
                if member in names and (root / name).is_file():
                    if hashlib.sha256(bundle.read(member)).hexdigest() != _sha256(root / name):
                        errors.append(f"ZIP member differs from committed file: {name}")

    # Deliberate known defects stay pinned (a fix changes this fixture on purpose, with review).
    arm_path = root / "02_RIGHT_2U_ARM.stl"
    arm = _binary_stl_extents(arm_path) if arm_path.is_file() else None
    if arm is not None:
        ear_z = arm[0][2]
        if abs(ear_z - EAR_HEIGHT_MM) > 0.5 or not ear_z > FACEPLATE_CLEARANCE_MM:
            errors.append(
                f"Known issue A unpinned: ear height {ear_z} mm, expected {EAR_HEIGHT_MM} mm "
                f"(above the ~{FACEPLATE_CLEARANCE_MM} mm 2U faceplate clearance)")
    tray_path = root / "01_CENTER_TRAY_214x214.stl"
    tray = _binary_stl_extents(tray_path) if tray_path.is_file() else None
    if tray is not None:
        margin = (PLATE_MM - tray[0][0]) / 2
        if abs(tray[0][0] - TRAY_MM) > 0.5 or abs(margin - 3.0) > 0.25:
            errors.append(
                f"Known issue C unpinned: tray width {tray[0][0]} mm leaves {margin} mm per side "
                f"on a {PLATE_MM} mm plate, expected {TRAY_MM} mm / 3 mm")
    generator = root / "generate_rack_mount.py"
    if generator.is_file() and "tray.translate((0,0,10))" not in generator.read_text(encoding="utf8"):
        errors.append("Known issue B unpinned: generate_rack_mount.py no longer translates the tray +10 mm Z "
                      "in the assembly export")
    warning = root / "WARNING_NOT_VALIDATED.txt"
    if warning.is_file() and warning.read_text(encoding="utf8") != WARNING_TEXT:
        errors.append("WARNING_NOT_VALIDATED.txt no longer records the four known issues verbatim")
    return errors


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--write-manifest" in argv:
        write_manifest()
        print("Wrote SHA256SUMS.txt and WARNING_NOT_VALIDATED.txt")
        return 0
    errors = collect_errors()
    if errors:
        raise SystemExit("\n".join(errors))
    print("Reference geometry/file validation passed (NOT mechanical validation).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
