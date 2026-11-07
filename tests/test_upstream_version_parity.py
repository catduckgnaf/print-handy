"""Version parity between the shipped pins and the test harnesses.

The bump automation moves the surfaces it knows about. These tests are
the forcing function for the surfaces it historically did not: a pin
that moves without its harness leaves CI proving the old release while
the image ships the new one (the Spoolman 0.27.0 harness under a 0.27.1
pin, found by the 2026-11-07 testing deep dive), and a harness that
drifts the other way is just as silent. Offline and textual on
purpose: they run everywhere the offline suite runs.

Bambu's fallback leg is the deliberate exception, and it is NOT in
this file: it lives in tests/test_ci_bambu_workflow.py as a recorded
generation verdict, because that leg's divergence is a decision, not
an accident.
"""
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "bump_upstream", ROOT / "scripts" / "bump_upstream.py")
bump = importlib.util.module_from_spec(spec)
sys.modules["bump_upstream"] = bump  # dataclasses resolve the module by name
spec.loader.exec_module(bump)


def test_distro_components_install_from_the_same_debian_on_both_surfaces():
    """OpenSCAD and Tesseract are upstream-checked by parity, not by pin.

    Both are Debian packages: the shipped image installs them in the
    Dockerfile runtime stage (Debian 13), and the CI offline legs install
    them so the design, render-preview and label-OCR tests run against
    the same distro builds the image ships (2026-11-07 test-suite
    cleanup, at catduck's direction). If either surface drops a package
    or the distro bases drift apart (the runtime stage's debian:13 vs
    the offline container's slim-trixie, which is Debian 13), CI would
    silently prove a different tool than the image ships — this test is
    what fails instead. scripts/bump_upstream.py enumerates both in
    NON_BUMPED_COMPONENTS with the reason they are not auto-bumped.
    """
    assert set(bump.NON_BUMPED_COMPONENTS) >= {"openscad", "tesseract", "bounds-toolchain"}
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert re.search(r"FROM debian:13-slim AS runtime", dockerfile)
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "slim-trixie" in ci, "offline legs no longer run on Debian 13 (trixie)"
    for package in ("openscad", "tesseract-ocr"):
        assert package in runtime, f"Dockerfile runtime stage no longer installs {package}"
        assert package in ci, f"ci.yml no longer installs {package} for the offline legs"


def test_bounds_toolchain_pins_agree_on_every_surface():
    """The Rust bounds toolchain is one pin set, everywhere it is built.

    The extension is built in three places — the Dockerfile bounds-build
    stage, the CI native-bounds job and the CI offline legs — with a
    pinned rustup toolchain and a pinned maturin, and the crate's Cargo
    dependencies are locked. A surface that drifts would build a
    different parser than the one the image ships. Cargo dependencies
    themselves are not auto-bumped (see NON_BUMPED_COMPONENTS in
    scripts/bump_upstream.py); they move through Cargo.lock, and every
    build here must use --locked so the lock is what gets built.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    toolchains = set(re.findall(r"--default-toolchain ([\d.]+)", dockerfile + ci))
    assert toolchains == {"1.99.1"}, f"rustup toolchain pins disagree: {toolchains}"
    maturins = set(re.findall(r"maturin==([\d.]+)", dockerfile + ci))
    assert maturins == {"1.16.0"}, f"maturin pins disagree: {maturins}"
    assert "bounds-toolchain" in bump.NON_BUMPED_COMPONENTS
    recorded = bump.NON_BUMPED_COMPONENTS["bounds-toolchain"]["pin"]
    assert "1.99.1" in recorded and "1.16.0" in recorded, (
        "bump_upstream.py's bounds-toolchain record no longer names the pins")
    assert (ROOT / "native" / "bounds" / "Cargo.lock").is_file()
    for where, text in (("Dockerfile", dockerfile), ("ci.yml", ci)):
        builds = re.findall(r"maturin build[^\n]*", text)
        assert builds and all("--locked" in build for build in builds), (
            f"{where} builds the bounds wheel without --locked: {builds}")


def _recorded_spoolman_version():
    """(version, ref) the docs record for the Dockerfile pin."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    ref = bump.parse_spoolman(dockerfile)
    settings = (ROOT / "docs" / "SETTINGS.md").read_text()
    match = bump._SPOOLMAN_SETTINGS_RE.search(settings)
    assert match, "docs/SETTINGS.md lost its 'Pinned Spoolman vX.Y.Z source commit' record"
    assert ref in settings, "docs/SETTINGS.md does not record the Dockerfile's SPOOLMAN_REF"
    return match.group(1), ref


def _harness_pin(text, where):
    version = bump._SPOOLMAN_ZIP_URL_RE.search(text)
    sha = bump._SPOOLMAN_ZIP_SHA_RE.search(text)
    assert version, f"{where} has no Spoolman release-zip download to check"
    assert sha, f"{where} has no Spoolman release-zip SHA256 check to check"
    return version.group(1), sha.group(1)


def test_spoolman_harness_pin_matches_dockerfile():
    """The real-server harness installs the Spoolman the image ships.

    The CI spoolman job and scripts/run_spoolman_native.sh each
    download a Spoolman release zip and verify its SHA256. Their
    version must equal the version docs record for the Dockerfile's
    SPOOLMAN_REF, and their zip SHA256s must agree with each other
    (they pin the same artifact). When a bump moves the shipped pin,
    the bump automation now moves these surfaces too
    (scripts/bump_upstream.py); this test is what fails if a future
    surface is added to one side only.
    """
    recorded_version, _ref = _recorded_spoolman_version()
    ci_version, ci_sha = _harness_pin(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(), "ci.yml spoolman job")
    native_version, native_sha = _harness_pin(
        (ROOT / "scripts" / "run_spoolman_native.sh").read_text(),
        "scripts/run_spoolman_native.sh")
    assert ci_version == recorded_version, (
        f"CI spoolman harness pins v{ci_version} but the Dockerfile ships "
        f"Spoolman v{recorded_version}: CI would prove the old release")
    assert native_version == recorded_version, (
        f"scripts/run_spoolman_native.sh pins v{native_version} but the "
        f"Dockerfile ships Spoolman v{recorded_version}")
    assert ci_sha == native_sha, (
        "the CI job and the native runner pin different spoolman.zip "
        "SHA256 values for the same version")
    assert re.fullmatch(r"[0-9a-f]{64}", ci_sha)
