# Native 3MF paint census

The Rust/PyO3 census accelerates whole-mesh inventory without changing who
validates a 3MF, identifies its parts, or builds its color manifest. Python
supplies object spans from the existing member reader; Rust decodes the paint
attributes and returns leaf counts and an unpainted triangle count per object.
One census is reused per source and model member within the decode scope.
No member data or census survives the scope.

Python remains the reference and fallback when the extension cannot load,
its API version differs, or a census fails. Retrying the specific mesh in
Python preserves error isolation: a malformed code in one object does not
change the result for a different object in the same model. Prusa parts with
`triangle_ranges` deliberately stay on Python because whole-mesh totals do not
retain triangle indexes.

The extension build follows the `native/bounds` PyO3 and maturin layout. The
main package and runtime image do not ship this extension yet; packaging,
strict CI parity, and performance measurements are tracked in #1715.
