"""Seeded differential tests for the G-code motion-bounds parsers (P1-2c).

Generated G-code snippets go through three paths that must never disagree:

- ``model_job._gcode_motion_bounds_python`` — the Python parser, the
  contract's oracle;
- ``model_job.gcode_motion_bounds`` — the dispatcher (runs unconditionally);
- ``print_handy.core._bounds_native.parse_bounds`` — the Rust extension,
  when it is built. It is not built in every environment, so the native
  side skips with the same gate ``test_native_bounds.py`` uses (module
  import, plus the ASCII gate for non-ASCII input); the Python-vs-dispatcher
  differential itself never skips.

Outcomes are compared as (status, value-or-refusal-message) pairs, so a
refusal from one side and a result from the other is a failure, as is any
message drift. The valid-only generator subset must actually succeed —
a generator that only produced refusals would prove nothing — and the
wild subset mixes in the forbidden commands, relative motion and G92
forms whose refusals are part of the documented semantics.
"""
import importlib
import random

import pytest

from print_handy.core import model_job

SEEDS = list(range(40))


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


def _number(rng):
    kind = rng.choice(['int', 'decimal', 'negative', 'exponent', 'leading_dot', 'trailing_dot'])
    if kind == 'int':
        return str(rng.randint(0, 220))
    if kind == 'decimal':
        return f'{rng.uniform(-20, 220):.2f}'
    if kind == 'negative':
        return f'-{rng.uniform(0, 30):.1f}'
    if kind == 'exponent':
        return f'{rng.randint(1, 22)}e1'
    if kind == 'leading_dot':
        return f'.{rng.randint(1, 9)}'
    return f'{rng.randint(0, 200)}.'


def _move(rng, axes=('X', 'Y', 'Z')):
    chosen = [a for a in axes if rng.random() < 0.75] or [rng.choice(axes)]
    words = ' '.join(f'{a}{_number(rng)}' for a in chosen)
    extras = rng.choice(['', '', f' E{rng.uniform(0, 5):.2f}', f' F{rng.randint(600, 3000)}'])
    comment = rng.choice(['', '', ' ; move'])
    return f'{rng.choice(["G0", "G1", "G00", "G01"])} {words}{extras}{comment}'


def generated_valid(rng):
    """Absolute-mode moves only: every axis is positioned, so these must parse."""
    lines = ['G90']
    for _ in range(rng.randint(2, 10)):
        lines.append(_move(rng))
    lines.append(f'G1 X{_number(rng)} Y{_number(rng)} Z{_number(rng)}')  # all axes certain
    return '\n'.join(lines) + '\n'


_WILD_EXTRA = [
    'G91', 'G90', 'G2 X10 Y10', 'G3 X5 Y5', 'G20', 'G28', 'G53 G1 X1',
    'SET_KINEMATIC_POSITION X=10', 'FORCE_MOVE STEPPER=stepper_x DISTANCE=1',
    'G92 X0', 'G92 E0', 'G92 X10 Y10', 'M104 S200', 'T1', 'G1 Xabc Y1',
    'G1 X١٢ Y1 Z1',  # Arabic-Indic digits: Python-path by design (ASCII gate)
    'MANUAL_STEPPER STEPPER=extruder MOVE=10', '; just a comment', '',
]


def generated_wild(rng):
    lines = [rng.choice(['G90', 'G91', ''])]
    for _ in range(rng.randint(2, 10)):
        lines.append(rng.choice([_move(rng), rng.choice(_WILD_EXTRA)]))
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_valid_gcode_agrees_on_python_and_dispatcher(seed):
    text = generated_valid(random.Random(40_000 + seed))
    expected = _outcome(model_job._gcode_motion_bounds_python, text)
    assert expected[0] == 'ok', f'seed {seed}: the valid generator produced a refusal: {expected}'
    assert _outcome(model_job.gcode_motion_bounds, text) == expected


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_wild_gcode_agrees_on_python_and_dispatcher(seed):
    text = generated_wild(random.Random(50_000 + seed))
    expected = _outcome(model_job._gcode_motion_bounds_python, text)
    assert _outcome(model_job.gcode_motion_bounds, text) == expected, f'seed {seed}: {text!r}'


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_gcode_agrees_on_native_when_built(seed):
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    rng = random.Random(40_000 + seed)
    for text in (generated_valid(rng), generated_wild(random.Random(50_000 + seed))):
        if not text.isascii():
            pytest.skip("non-ASCII generated cases are Python-path by design (ASCII gate)")
        assert _outcome(module.parse_bounds, text) == \
            _outcome(model_job._gcode_motion_bounds_python, text), f'seed {seed}: {text!r}'


def test_generated_corpus_exercises_both_outcomes():
    """Guard the generator itself: the wild corpus must contain real bounds
    and real refusals, or the differentials above prove nothing."""
    outcomes = [_outcome(model_job._gcode_motion_bounds_python,
                         generated_wild(random.Random(50_000 + seed))) for seed in SEEDS]
    assert any(status == 'ok' for status, _ in outcomes)
    assert any(status == 'raise' for status, _ in outcomes)
    valid = [_outcome(model_job._gcode_motion_bounds_python,
                      generated_valid(random.Random(40_000 + seed))) for seed in SEEDS]
    assert all(status == 'ok' for status, _ in valid)
