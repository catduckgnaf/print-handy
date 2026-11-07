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
