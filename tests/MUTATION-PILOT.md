# Mutation-testing pilot (deep-dive item 12)

A hand-built mutant list for two small, safety-relevant surfaces --
no mutation tool is installed or required:

1. **The bounds dispatcher** in `print_handy/core/model_job.py`:
   `gcode_motion_bounds`, `_load_native_bounds`,
   `_native_bounds_result`, `_bounds_impl_setting`. This decides
   whether the Rust bounds parser is trusted, when the Python parser
   is the fallback, and which result a fit decision sees.
2. **The slot-naming conversion**: `lane_index0` /
   `slot_from_lane_index0` in `print_handy/runtime/spool_sync.py` (the
   one 1-based <-> 0-based conversion, #1020 D2) and `slot_display`
   in `print_handy/core/filament_color.py` (#1020 D1).

Each mutant is a concrete one-line behavior flip. The "killer" is the
test that makes the flipped code fail. Mutants the existing suite
already killed are listed with their existing killer; mutants that
**survived** the existing suite got a new targeted test in
`tests/test_mutation_pilot.py`.

## Measured results

Every mutant was verified mechanically, not by reasoning: the
package was copied to a scratch directory (the tracked tree was never
touched), the flip was applied to the copy, and the killer check ran
in a subprocess importing the mutated copy. A control run first
proved the same check **passes** against the unmutated copy, so a
"kill" cannot be an artifact of the harness. Measured outcome on
2026-11-07: **17 of 17 mutants killed, 0 control failures.**
(The verification script and its finished output log were kept with
the lane's run artifacts, `.run/logs/mutation-pilot-results.txt`.)

## Bounds dispatcher mutants

| ID | Flip | Killer | Result |
|----|------|--------|--------|
| B1 | `text.isascii()` dropped from the dispatch condition (`setting != "python" and text.isascii()` -> `setting != "python"`) | **Survived the existing suite**: its ASCII-gate test skips when the real extension is not built. New: `test_kills_b1_...` in the pilot file, using a fake native module so it runs everywhere | Killed |
| B2 | `.strip().lower()` dropped from `_bounds_impl_setting` | **Survived**: the existing suite only sets the exact lowercase spellings. New: `test_kills_b2_...` (two tests: `" Native "` must stay required, `" PYTHON "` must stay python) | Killed |
| B3 | `(float(low), float(high))` -> `(float(high), float(low))` in `_native_bounds_result` | **Survived**: existing fake modules return Python-identical (often symmetric) bounds, so a swap was invisible. New: `test_kills_b3_and_b10_...` with asymmetric native bounds | Killed |
| B4 | `required = setting == "native"` -> `!=` | Existing: the strict-native tests in `tests/pipeline/test_native_bounds.py` (`test_native_setting_raises_when_the_module_is_missing`, `test_native_setting_stays_strict_after_a_failure`) plus the auto-mode fallback tests | Killed |
| B5 | API-version check `!=` -> `==` in `_load_native_bounds` | Existing: the strict dispatcher tests when the extension is built; pilot verification also killed it with a fake module (a version-correct module must be used) | Killed |
| B6 | `except module.BoundsRefusal: raise` -> swallow into the Python fallback | **Survived as indistinguishable**: the existing refusal test uses a refusal the Python parser produces identically, so propagation and fallback look the same. New: `test_kills_b6_...` with a fake refusal message the Python parser would never produce for parseable input | Killed |
| B7 | `_native_bounds_broken = True` dropped from the mid-parse failure branch (failure not sticky) | Existing: `test_native_setting_stays_strict_after_a_failure` (the third call must say "disabled after an earlier failure") | Killed |
| B8 | The `setting != "python"` gate replaced by `True` (python setting still touches the native path) | Existing: `test_python_setting_never_touches_native` | Killed |
| B9 | Golden self-check comparison `!=` -> `==` (a passing module is rejected) | Existing: `test_dispatcher_uses_a_verified_fake_native_module` / `test_self_check_failure_falls_back` | Killed |
| B10 | `_native_bounds_result` returns `None` instead of the shaped bounds (verified native result discarded, silent fallback) | **Survived** for the same reason as B3: no existing test has the native path return bounds that differ from the Python parser's. New: `test_kills_b3_and_b10_...` | Killed |

## Slot conversion mutants

| ID | Flip | Killer | Result |
|----|------|--------|--------|
| S1 | `lane_index0`: `int(slot) - 1` -> `int(slot)` | Existing: `ConversionTableTests` in `tests/test_slot_naming.py`; restated exhaustively in the pilot file (`test_kills_s1_s2_...`) | Killed |
| S2 | `slot_from_lane_index0`: `index0 + 1` -> `index0` | Existing: `ConversionTableTests`; restated in the pilot file | Killed |
| S3 | The `isinstance(index0, bool)` guard removed (`True` converts to slot 2) | Existing: `test_unknown_values_convert_to_none` (bad values include `True`) | Killed |
| S4 | `.strip()` dropped before the digit check and `int()` parse | **Survived**: existing string cases are clean spellings (`"0"`..`"3"`). New: `test_kills_s4_...` (`" 2 "` -> 3, `"\t0\n"` -> 1) | Killed |
| S5 | Range check widened: `slot in SLOTS` -> `1 <= slot <= 5` (index 4 becomes slot 5) | Existing: `test_unknown_values_convert_to_none` (bad values include `4`) | Killed |
| S6 | `slot_display`: the `(IFS channel N)` suffix dropped | Existing: `SlotDisplayTests.test_display_form` | Killed |
| S7 | The `isdigit` gate replaced by a bare `int()` parse (signed strings parse) | **Survived**: no signed or decimal string was in the existing table. New: `test_kills_s7_...` (`"+1"`, `"-1"`, `"1.0"` all read as None; `"03"` still parses to slot 4) | Killed |
| S8 | The S6 flip generalized: `slot_display` returning the bare number for every slot, not just slot 3 | Same flip as S6 (verified under S6); the pilot file pins it exhaustively for slots 1-4 in `test_kills_s8_...` | Killed |

## What the pilot found

The suite's kill rate on this list is high, and every survivor had
the same shape: an existing test existed for the *area* but its
fixtures were too symmetric or too clean to separate the mutant --
Python-identical fake native bounds (B3/B10), a refusal both parsers
produce (B6), a gate test that skips without the real extension (B1),
and clean-only spellings for settings and lane strings (B2/S4/S7).
The six new pilot tests close exactly those separations. No mutant
exposed a defect in the unmutated code: every pilot test passes
against it, and no surviving mutant represented a live bug -- only
assertions that could not have caught the flip.
