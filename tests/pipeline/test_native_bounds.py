"""Native bounds parser equivalence and dispatch (#952, D1).

Three layers are pinned against the frozen corpus in
``native_bounds_corpus`` (106 adversarial cases with outcomes recorded
from the Python implementation):

1. The Python parser itself — the corpus is its regression contract.
2. The Rust extension (``print_handy.core._bounds_native``) when it is
   built — the corpus runs against ``parse_bounds`` directly. In the
   offline CI legs, where the extension is not built, these tests skip;
   the dedicated ``native-bounds`` CI job builds the wheel, mirrors
   the extension into the source package, and runs this file with
   PRINT_HANDY_BOUNDS_REQUIRE_NATIVE=1, which turns a missing
   extension into a hard failure in the strict dispatcher test instead
   of a silent skip.
3. The dispatcher ``gcode_motion_bounds`` — ASCII input may take either
   path but must return the recorded outcome; non-ASCII input always
   takes the Python path (the ASCII gate), which is what makes the
   Unicode cases below exact by construction.

Fallback behavior is tested with fake modules: a missing module, a
version mismatch, a failing golden self-check and a mid-parse internal
error each land on the Python parser with identical results.
"""
import importlib
import os
import sys
import types

import pytest

from print_handy.core import model_job
from tests.pipeline.native_bounds_corpus import CASES, EXPECTED

assert len(CASES) == len(EXPECTED) == 112


def _outcome(fn, text):
    try:
        return ("ok", fn(text))
    except RuntimeError as exc:
        return ("raise", str(exc))


def _native_module():
    try:
        return importlib.import_module("print_handy.core._bounds_native")
    except ImportError:
        return None


@pytest.fixture(autouse=True)
def _reset_native_state(monkeypatch):
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "auto")
    model_job._reset_native_bounds_cache()
    yield
    model_job._reset_native_bounds_cache()


# ------------------------------------------------------------ corpus: Python

@pytest.mark.parametrize("text,expected", list(zip(CASES, EXPECTED)), ids=range(len(CASES)))
def test_python_parser_matches_the_frozen_corpus(text, expected):
    assert _outcome(model_job._gcode_motion_bounds_python, text) == expected


# ------------------------------------------------------------ corpus: native

@pytest.mark.parametrize("text,expected", list(zip(CASES, EXPECTED)), ids=range(len(CASES)))
def test_native_parser_matches_the_frozen_corpus(text, expected):
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    if not text.isascii():
        pytest.skip("non-ASCII corpus cases are Python-path by design (ASCII gate)")
    assert _outcome(module.parse_bounds, text) == expected


def test_native_module_reports_the_expected_api_version():
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    assert module.API_VERSION == model_job._BOUNDS_NATIVE_API_VERSION


# --------------------------------------------------------- corpus: dispatch

@pytest.mark.parametrize("text,expected", list(zip(CASES, EXPECTED)), ids=range(len(CASES)))
def test_dispatcher_matches_the_frozen_corpus(text, expected):
    assert _outcome(model_job.gcode_motion_bounds, text) == expected


# ------------------------------------------------------- Unicode (API level)
# The Python patterns are Unicode-aware (\d matches Unicode decimal
# digits, \s matches NBSP, IGNORECASE folds U+017F to S). The native
# crate is ASCII-only, so the dispatcher's ASCII gate routes these to
# the Python parser; the assertions below pin the API-level outcomes.

UNICODE_CASES = [
    # Arabic-Indic digits U+0661 U+0662 are \d to Python: X reads 12.
    ("G1 X١٢ Y1 Z1\n",
     ("ok", {"x": (12.0, 12.0), "y": (1.0, 1.0), "z": (1.0, 1.0)})),
    # Unicode digit in an exponent: 1e2.
    ("G1 X1e٢ Y1 Z1\n",
     ("ok", {"x": (100.0, 100.0), "y": (1.0, 1.0), "z": (1.0, 1.0)})),
    # NBSP between axis letter and number is \s to Python.
    ("G1 X 10 Y20 Z30\n",
     ("ok", {"x": (10.0, 10.0), "y": (20.0, 20.0), "z": (30.0, 30.0)})),
    # Long s (U+017F) case-folds to S under IGNORECASE: the Klipper
    # command is recognized and refused exactly like its ASCII form.
    ("ſET_KINEMATIC_POSITION X=10\nG1 X1 Y1 Z1\n",
     ("raise", "line 1: unsupported motion-affecting command SET_KINEMATIC_POSITION")),
]


@pytest.mark.parametrize("text,expected", UNICODE_CASES)
def test_unicode_inputs_match_the_python_parser_at_the_api(text, expected):
    assert not text.isascii()
    assert _outcome(model_job._gcode_motion_bounds_python, text) == expected
    assert _outcome(model_job.gcode_motion_bounds, text) == expected


def test_ascii_gate_never_sends_non_ascii_to_the_native_parser(monkeypatch):
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    calls = []
    real = module.parse_bounds

    def spy(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(module, "parse_bounds", spy)
    model_job._reset_native_bounds_cache()
    text, _expected = UNICODE_CASES[0]
    model_job.gcode_motion_bounds(text)
    # Only the ASCII self-check inputs reached the native parser; the
    # non-ASCII text itself never did.
    assert text not in calls
    assert all(call.isascii() for call in calls)


# ------------------------------------------------------------ number grammar

def test_dot_before_exponent_reads_the_python_way():
    # "1.e2" matches Python's NUMBER grammar (\d+(\.\d*)? then exponent);
    # Rust's float parser needs the equal-valued "1.0e2" normalization.
    text = "G1 X1.e2 Y1 Z1\n"
    expected = ("ok", {"x": (100.0, 100.0), "y": (1.0, 1.0), "z": (1.0, 1.0)})
    assert _outcome(model_job._gcode_motion_bounds_python, text) == expected
    assert _outcome(model_job.gcode_motion_bounds, text) == expected
    module = _native_module()
    if module is not None:
        assert _outcome(module.parse_bounds, text) == expected


# ---------------------------------------------------------------- large file

def _large_text(lines=120_000):
    parts = ["G90\n"]
    for i in range(lines):
        parts.append(f"G1 X{i % 210}.5 Y{(i * 13) % 210}.25 Z{0.2 + (i // 60000) * 0.2:.3f} E{i * 0.01:.2f}\n")
    return "".join(parts)


def test_large_generated_file_agrees_on_both_paths():
    text = _large_text()
    expected = ("ok", {"x": (0.5, 209.5), "y": (0.25, 209.25), "z": (0.2, 0.4)})
    assert _outcome(model_job._gcode_motion_bounds_python, text) == expected
    assert _outcome(model_job.gcode_motion_bounds, text) == expected
    module = _native_module()
    if module is not None:
        assert _outcome(module.parse_bounds, text) == expected


# -------------------------------------------------------------- fake modules

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


TEXT = "G90\nG1 X10 Y20 Z0.2\nG1 X-5 Y30\nG0 Z5\n"
EXPECTED_TEXT = {"x": (-5.0, 10.0), "y": (20.0, 30.0), "z": (0.2, 5.0)}


def test_dispatcher_uses_a_verified_fake_native_module(monkeypatch):
    calls = []
    fake = _fake_module()
    real = fake.parse_bounds

    def counting(text):
        calls.append(text)
        return real(text)

    fake.parse_bounds = counting
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert TEXT in calls


def test_dispatcher_uses_the_native_module_when_required(monkeypatch):
    """The strict end-to-end proof for the native-bounds CI job: with
    PRINT_HANDY_BOUNDS_IMPL=native the dispatcher must parse through
    the real extension. Skips when the extension is not built — except
    when PRINT_HANDY_BOUNDS_REQUIRE_NATIVE is set (the CI job), where
    a missing extension is a failure, never a skip."""
    module = _native_module()
    if module is None:
        if os.environ.get("PRINT_HANDY_BOUNDS_REQUIRE_NATIVE"):
            pytest.fail("native bounds extension required but not importable")
        pytest.skip("native bounds extension not built")
    calls = []
    real = module.parse_bounds

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(module, "parse_bounds", counting)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    model_job._reset_native_bounds_cache()
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert TEXT in calls


def test_missing_module_falls_back(monkeypatch):
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", None)
    # A previously imported real module also lives as an attribute of
    # the package, which `from . import ...` would find first.
    monkeypatch.delattr(core_pkg, "_bounds_native", raising=False)
    model_job._reset_native_bounds_cache()
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT


def test_version_mismatch_falls_back(monkeypatch):
    _install_fake(monkeypatch, _fake_module(API_VERSION=999))
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT


def test_self_check_failure_falls_back(monkeypatch):
    def wrong_bounds(text):
        if text == TEXT:
            return model_job._gcode_motion_bounds_python(text)
        return {"x": (0.0, 0.0), "y": (0.0, 0.0), "z": (0.0, 0.0)}

    _install_fake(monkeypatch, _fake_module(parse_bounds=wrong_bounds))
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT


def test_mid_parse_internal_error_falls_back_and_sticks(monkeypatch):
    fake = _fake_module()
    real = fake.parse_bounds
    state = {"armed": False, "calls": 0}

    def flaky(text):
        state["calls"] += 1
        if state["armed"]:
            raise ValueError("boom")
        return real(text)

    fake.parse_bounds = flaky
    _install_fake(monkeypatch, fake)
    # First call verifies the module and parses through it; only then
    # does the failure start, so this is a genuine mid-parse error.
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    state["armed"] = True
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    calls_after_failure = state["calls"]
    # The native path is disabled for the rest of the process.
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert state["calls"] == calls_after_failure


def test_refusal_propagates_from_the_native_path(monkeypatch):
    fake = _fake_module()

    def refusing(text):
        if "G2" in text:
            raise _FakeRefusal("line 2: arc motion is unsupported by the bounds verifier")
        return model_job._gcode_motion_bounds_python(text)

    fake.parse_bounds = refusing
    _install_fake(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="arc motion is unsupported"):
        model_job.gcode_motion_bounds("G90\nG2 X1 Y1\n")


def test_malformed_native_result_falls_back(monkeypatch):
    fake = _fake_module()
    real = fake.parse_bounds

    def malformed(text):
        if text == TEXT:
            return {"x": (1.0,), "y": (2.0, 2.0), "z": (3.0, 3.0)}
        return real(text)

    fake.parse_bounds = malformed
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT


def test_python_setting_never_touches_native(monkeypatch):
    calls = []
    fake = _fake_module()

    def counting(text):
        calls.append(text)
        return model_job._gcode_motion_bounds_python(text)

    fake.parse_bounds = counting
    _install_fake(monkeypatch, fake)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "python")
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert calls == []


def test_native_setting_raises_when_the_module_is_missing(monkeypatch):
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", None)
    monkeypatch.delattr(core_pkg, "_bounds_native", raising=False)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    model_job._reset_native_bounds_cache()
    with pytest.raises(RuntimeError, match="native bounds parser required but unavailable"):
        model_job.gcode_motion_bounds(TEXT)


class _FakePanic(BaseException):
    """Stands in for PyO3's PanicException, which is a BaseException."""


def test_a_native_panic_falls_back_and_sticks(monkeypatch):
    fake = _fake_module()
    real = fake.parse_bounds
    state = {"armed": False}

    def panicking(text):
        if state["armed"]:
            raise _FakePanic("rust panicked")
        return real(text)

    fake.parse_bounds = panicking
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    state["armed"] = True
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    assert model_job._native_bounds_broken


def test_a_panic_during_the_self_check_falls_back(monkeypatch):
    def panicking(text):
        raise _FakePanic("rust panicked")

    _install_fake(monkeypatch, _fake_module(parse_bounds=panicking))
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT


def test_keyboard_interrupt_is_never_swallowed(monkeypatch):
    fake = _fake_module()
    real = fake.parse_bounds
    state = {"armed": False}

    def interrupted(text):
        if state["armed"]:
            raise KeyboardInterrupt
        return real(text)

    fake.parse_bounds = interrupted
    _install_fake(monkeypatch, fake)
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    state["armed"] = True
    with pytest.raises(KeyboardInterrupt):
        model_job.gcode_motion_bounds(TEXT)


def test_native_setting_stays_strict_after_a_failure(monkeypatch):
    fake = _fake_module()
    real = fake.parse_bounds
    state = {"armed": False}

    def flaky(text):
        if state["armed"]:
            raise ValueError("boom")
        return real(text)

    fake.parse_bounds = flaky
    _install_fake(monkeypatch, fake)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    assert model_job.gcode_motion_bounds(TEXT) == EXPECTED_TEXT
    state["armed"] = True
    with pytest.raises(RuntimeError, match="native bounds parser required but failed"):
        model_job.gcode_motion_bounds(TEXT)
    # Later calls never degrade silently to the Python parser.
    with pytest.raises(RuntimeError, match="disabled after an earlier failure"):
        model_job.gcode_motion_bounds(TEXT)
