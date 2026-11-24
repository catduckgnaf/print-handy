"""Every generated-template model verifies against the installed (pinned) OrcaSlicer's own profiles.

Each vendor's profiles.install refuses unless the flattened vendor profiles hash to an identity, at
that vendor version, recorded in the model's facts.py. An Orca bump that moves a vendor's profiles
without recording the new identities therefore leaves operators on the shipped slicer unable to
generate templates (#1871: the 2.5.2 Prusa profiles). The Bambu A1 real-slicer tests only cover BBL,
so this sweeps every model whose templates are generated from Orca, in the packaging CI installs.

Skipped unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs, the pinned OrcaSlicer bundle).
"""
import importlib
import os

import pytest

from print_handy import printers, settings

from .test_orca_real_bambu_a1 import installed_resources

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to check against the real Orca")

# The profile pipeline each vendor package uses; the SDCP Centauri Carbon shares Elegoo's.
PROFILE_MODULE = {"elegoo_sdcp": "elegoo"}
GENERATED = sorted(key for key in printers.MODELS if printers.MODELS[key].generated_templates)


def test_the_sweep_covers_every_generated_template_vendor():
    assert {printers.MODELS[key].vendor_key for key in GENERATED} >= {
        "bambulab", "prusa", "creality", "elegoo", "elegoo_sdcp", "qidi", "anycubic"}


@pytest.mark.parametrize("key", GENERATED)
def test_installed_orca_profiles_match_a_recorded_identity(key, tmp_path):
    vendor = printers.MODELS[key].vendor_key
    profiles = importlib.import_module(f"print_handy.printers.{PROFILE_MODULE.get(vendor, vendor)}.profiles")
    env = {"PRINT_HANDY__PRINTER__MODEL": key, "PRINT_HANDY__PATHS__STATE_HOME": str(tmp_path / "state")}
    adapter = printers.get(key, settings.load(local=False, environ=env))
    known = profiles.install(adapter, installed_resources(), folder=tmp_path / "templates")
    print(f"\n{key}: {known['label']}")
    assert (tmp_path / "templates" / "machine.json").is_file()
