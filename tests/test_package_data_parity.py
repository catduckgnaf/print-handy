"""The wheel's package-data and the sdist's MANIFEST.in ship the same runtime data files (#1157, #1322).

print_handy reads data files beside its modules through Path(__file__): Orca templates, the
data/*.toml registries, HMS data, the Bambu CA bundle, the Discord SDK script. pyproject.toml's
[tool.setuptools.package-data] decides what the wheel carries; MANIFEST.in decides what the sdist
carries, and a wheel built from that sdist (what pip does with an sdist) can only hold what the
sdist kept. setuptools merges the two (package-data files go into the sdist, and with
include_package_data the MANIFEST files inside the package go into the wheel), so a pattern one
list drops is covered by the other; what breaks an install is a data file neither list matches,
such as a new file type. So this test builds all three artifacts with setuptools' own backend,
from a copy of the tree, and checks that each carries exactly the data files on disk. A file the
lists miss fails here instead of at runtime on someone's install.

The native bounds parser (native/bounds, built by maturin into its own wheel) installs its
extension as print_handy.core._bounds_native, inside this package. The main artifacts must never
carry a built copy of it: the image installs the extension from the bounds wheel alone.
"""
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "print_handy"
NATIVE = re.compile(r"_bounds_native[^/]*\.(?:so|pyd|dylib)$")
# Files the build needs beside the package: metadata, the MANIFEST includes, the plugin handlers.
BUILD_FILES = ("pyproject.toml", "MANIFEST.in", "README.md", "LICENSE", "NOTICE",
               "plugins/print-feedback/__init__.py")
_BUILD = ("import sys\nfrom setuptools import build_meta as backend\n"
          "print(backend.build_sdist(sys.argv[1]) if sys.argv[2] == 'sdist' else backend.build_wheel(sys.argv[1]))\n")


def _is_data(name):
    return not name.endswith((".py", ".pyc")) and "__pycache__" not in name.split("/")


def _copy_tree(dest):
    for name in BUILD_FILES:
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest / name)
    shutil.copytree(ROOT / PACKAGE, dest / PACKAGE, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _build(source, out, kind):
    result = subprocess.run([sys.executable, "-c", _BUILD, str(out), kind], cwd=source,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    return out / result.stdout.strip().splitlines()[-1]


def _wheel_data(path):
    with zipfile.ZipFile(path) as wheel:
        return {n for n in wheel.namelist() if n.startswith(PACKAGE + "/") and _is_data(n)}


def _sdist_members(path):
    with tarfile.open(path) as sdist:
        names = [m.name for m in sdist.getmembers() if m.isfile()]
    prefix = names[0].split("/", 1)[0] + "/"
    return {n[len(prefix):] for n in names if n.startswith(prefix)}


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory):
    base = tmp_path_factory.mktemp("package-data")
    tree, out = base / "tree", base / "dist"
    tree.mkdir()
    out.mkdir()
    _copy_tree(tree)
    sdist = _build(tree, out, "sdist")
    wheel = _build(tree, out, "wheel")
    unpacked = base / "from-sdist"
    with tarfile.open(sdist) as archive:
        archive.extractall(unpacked, filter="data")
    (sdist_tree,) = unpacked.iterdir()
    sdist_out = base / "dist-from-sdist"
    sdist_out.mkdir()
    wheel_from_sdist = _build(sdist_tree, sdist_out, "wheel")
    on_disk = {p.relative_to(ROOT).as_posix() for p in (ROOT / PACKAGE).rglob("*") if p.is_file()}
    expected = {n for n in on_disk if _is_data(n) and not NATIVE.search(n)}
    return {"expected": expected, "sdist": _sdist_members(sdist),
            "wheel": _wheel_data(wheel), "wheel_from_sdist": _wheel_data(wheel_from_sdist)}


def test_the_package_ships_data_files_at_all(artifacts):
    # A walk that found nothing would make every comparison below pass vacuously.
    kinds = {Path(n).suffix for n in artifacts["expected"]}
    assert {".json", ".toml", ".md", ".pem", ".js"} <= kinds, kinds
    assert len(artifacts["expected"]) > 150


def test_the_wheel_package_data_carries_every_runtime_data_file(artifacts):
    assert sorted(artifacts["expected"] - artifacts["wheel"]) == []
    assert sorted(artifacts["wheel"] - artifacts["expected"]) == []


def test_the_sdist_manifest_carries_the_same_data_files_as_the_wheel(artifacts):
    sdist_data = {n for n in artifacts["sdist"] if n.startswith(PACKAGE + "/") and _is_data(n)}
    assert sorted(artifacts["wheel"] - sdist_data) == [], "in package-data but not MANIFEST.in"
    assert sorted(sdist_data - artifacts["wheel"]) == [], "in MANIFEST.in but not package-data"
    # The command handlers live outside the package and ride in the sdist by name (#1146).
    assert "plugins/print-feedback/__init__.py" in artifacts["sdist"]


def test_a_wheel_built_from_the_sdist_loses_no_data_file(artifacts):
    assert artifacts["wheel_from_sdist"] == artifacts["wheel"]


def test_no_artifact_carries_a_built_native_bounds_extension(artifacts):
    for kind in ("sdist", "wheel", "wheel_from_sdist"):
        assert [n for n in artifacts[kind] if NATIVE.search(n)] == [], kind


def test_the_native_bounds_wheel_targets_a_module_inside_this_package():
    """The reason the check above matters: maturin installs the extension into print_handy/core."""
    config = tomllib.loads((ROOT / "native" / "bounds" / "pyproject.toml").read_text(encoding="utf-8"))
    module = config["tool"]["maturin"]["module-name"]
    assert module == "print_handy.core._bounds_native"
    assert (ROOT / Path(*module.split(".")[:-1]) / "__init__.py").is_file()
    assert NATIVE.search("print_handy/core/_bounds_native.abi3.so")
