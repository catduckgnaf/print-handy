"""Mutation-testing pilot (deep-dive item 12): targeted tests that kill
the surviving mutants listed in ``tests/MUTATION-PILOT.md``.

The pilot hand-builds one-line behavior flips for two safety-relevant
modules -- the bounds dispatcher in ``print_handy/core/model_job.py``
(``gcode_motion_bounds`` and its native-load helpers) and the slot-naming
conversion in ``print_handy/hermes/spool_sync.py``
(``lane_index0`` / ``slot_from_lane_index0``) plus
``print_handy/core/filament_color.py``'s ``slot_display``.

Most listed mutants are already killed by the existing suite; the tests
in this file are the ones added for the mutants that SURVIVED it. Each
test names the mutant it kills. All of them pass against the unmutated
code, and the kill verification (each flip applied to a scratch copy of
the package, never to the tracked tree) is recorded in
``tests/MUTATION-PILOT.md``.
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy.core import filament_color, model_job  # noqa: E402
from print_handy.hermes import spool_sync  # noqa: E402

TEXT = "G90\nG1 X10 Y20 Z0.2\nG1 X-5 Y30\nG0 Z5\n"
EXPECTED_TEXT = {"x": (-5.0, 10.0), "y": (20.0, 30.0), "z": (0.2, 5.0)}


class _FakeRefusal(RuntimeError):
    pass


def _fake_module(**overrides):
    module = types.SimpleNamespace()
    module.API_VERSION = model_job._BOUNDS_NATIVE_API_VERSION
    module.BoundsRefusal = _FakeRefusal

    def parse_bounds(text):
        try:
            return model_job._gcode_motion_bounds_python(text)
        except RuntimeError as exc:
            raise _FakeRefusal(str(exc))

    module.parse_bounds = parse_bounds
    module.__dict__.update(overrides)
    return module


def _install_fake(monkeypatch, fake):
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", fake)
    monkeypatch.setattr(core_pkg, "_bounds_native", fake, raising=False)
    model_job._reset_native_bounds_cache()


@pytest.fixture(autouse=True)
def _reset_native_state(monkeypatch):
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "auto")
    model_job._reset_native_bounds_cache()
    yield
    model_job._reset_native_bounds_cache()


# ------------------------------------------------------- bounds dispatcher


def test_kills_b1_ascii_gate_removed_fake_native_never_sees_non_ascii(monkeypatch):
    """Mutant B1: ``text.isascii()`` dropped from the dispatch condition.

    The existing ASCII-gate test skips when the real extension is not
    built, so B1 survived every environment without it. This version
    installs a fake native module, so it runs everywhere.
    """
    calls = []
    fake = _fake_module()
    real = fake.parse_bounds

    def counting(text):
        calls.append(text)
        return real(text)

    fake.parse_bounds = counting
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT  # verifies + caches the fake
    calls.clear()
    # Arabic-Indic digits: the Python parser reads X12; the native crate
    # implements only the ASCII subset and must never be handed this.
    text = "G1 X١٢ Y1 Z1\n"
    assert model_job.gcode_motion_bounds(text) == {
        "x": (12.0, 12.0), "y": (1.0, 1.0), "z": (1.0, 1.0)}
    assert calls == [], "the ASCII gate must route non-ASCII input to the Python parser"


def test_kills_b2_setting_value_not_normalized(monkeypatch):
    """Mutant B2: ``.strip().lower()`` dropped from ``_bounds_impl_setting``.

    The existing suite only ever sets the exact lowercase spellings, so
    an unnormalized ``" Native "`` silently degrading to auto survived.
    """
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", None)
    monkeypatch.delattr(core_pkg, "_bounds_native", raising=False)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", " Native ")
    model_job._reset_native_bounds_cache()
    with pytest.raises(RuntimeError, match="native bounds parser required but unavailable"):
        model_job.gcode_motion_bounds(TEXT)


def test_kills_b2_python_spelling_with_case_and_whitespace(monkeypatch):
    """Mutant B2, second face: ``" PYTHON "`` must still mean python."""
    calls = []
    fake = _fake_module()

    def counting(text):
        calls.append(text)
        return model_job._gcode_motion_bounds_python(text)

    fake.parse_bounds = counting
    _install_fake(monkeypatch, fake)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", " PYTHON ")
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert calls == []


def test_kills_b3_and_b10_native_result_low_high_swapped_or_order_flipped(monkeypatch):
    """Mutants B3 (low/high swapped in ``_native_bounds_result``) and B10
    (fallback order swapped: the Python result wins over a verified
    native result).

    The fake returns asymmetric bounds that deliberately disagree with
    the Python parser for TEXT (while passing the golden self-check,
    whose inputs are different strings). The dispatcher must return the
    native pair in (low, high) order, exactly as the native path gave it.
    """
    fake = _fake_module()
    real = fake.parse_bounds
    custom = {"x": (1.0, 9.0), "y": (2.0, 8.0), "z": (3.0, 7.0)}

    def disagreeing(text):
        if text == TEXT:
            return dict(custom)
        return real(text)

    fake.parse_bounds = disagreeing
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == custom


def test_kills_b6_native_refusal_swallowed_into_the_python_fallback(monkeypatch):
    """Mutant B6: ``except module.BoundsRefusal: raise`` becomes a swallow.

    For ordinary refusals the Python fallback refuses identically, which
    is why the existing refusal test cannot tell the two apart. A fake
    refusal with a message the Python parser would never produce for
    this (fully parseable) input separates them: the dispatcher's
    contract is that a native refusal propagates unchanged.
    """
    fake = _fake_module()
    real = fake.parse_bounds

    def refusing(text):
        if text == TEXT:
            raise _FakeRefusal("line 99: fake native refusal")
        return real(text)

    fake.parse_bounds = refusing
    _install_fake(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="fake native refusal"):
        model_job.gcode_motion_bounds(TEXT)


# ------------------------------------------------------- slot conversion


def test_kills_s4_slot_from_lane_index0_trims_surrounding_whitespace():
    """Mutant S4: ``.strip()`` dropped before the digit check / int parse.

    HelixScreen writes the inner lane value as a string; the existing
    table only uses clean spellings (``"0"``..``"3"``), so a padded
    spelling survived. The unmutated conversion trims first.
    """
    assert spool_sync.slot_from_lane_index0(" 2 ") == 3
    assert spool_sync.slot_from_lane_index0("\t0\n") == 1
    assert spool_sync.slot_from_lane_index0(" 3") == 4


def test_kills_s7_slot_from_lane_index0_rejects_signed_and_decimal_strings():
    """Mutant S7: the ``isdigit`` gate replaced by a bare ``int()`` parse.

    ``int("+1")`` and ``int("-1")`` parse fine in Python, so a mutant
    that parses first and range-checks later would map ``"+1"`` to
    slot 2. The unmutated digit gate reads every signed or decimal
    spelling as unknown (None), never as a slot.
    """
    assert spool_sync.slot_from_lane_index0("+1") is None
    assert spool_sync.slot_from_lane_index0("-1") is None
    assert spool_sync.slot_from_lane_index0("1.0") is None
    assert spool_sync.slot_from_lane_index0("03") == 4  # leading zeros still parse


def test_kills_s1_s2_round_trip_is_exact_for_every_slot_and_index():
    """Mutants S1 (``lane_index0`` off by one) and S2 (the inverse ``+1``
    dropped): restated here as an exhaustive two-direction pin so the
    pilot file stands alone as the conversion's kill record.
    """
    for slot in (1, 2, 3, 4):
        assert spool_sync.lane_index0(slot) == slot - 1
        assert spool_sync.slot_from_lane_index0(slot - 1) == slot
        record = spool_sync.build_lane_record(
            slot, {"id": 7, "filament": {"name": "PLA", "material": "PLA",
                                         "color_hex": "FF0000"}}, now=1.0)
        assert record["lane"] == slot - 1


def test_kills_s8_slot_display_always_names_the_ifs_base():
    """Mutant S8: ``slot_display`` reduced to the bare number."""
    for slot in (1, 2, 3, 4):
        assert filament_color.slot_display(slot) == f"slot {slot} (IFS channel {slot})"
