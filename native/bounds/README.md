# Native G-code bounds parser (Rust / PyO3)

An optional accelerator for `print_handy.core.model_job.gcode_motion_bounds`
(issue #952, decision D1). It parses the same fail-closed motion-bounds
semantics as the Python implementation, roughly an order of magnitude
faster on plate-scale files.

The Python implementation is **not** replaced: it stays in
`print_handy/core/model_job.py` forever as the fallback and the test
oracle. The dispatcher uses this extension only when it imports, its
`API_VERSION` matches the Python side's expectation, and a golden
self-check passes — and only for pure-ASCII input (the Python patterns
are Unicode-aware; non-ASCII input always takes the Python path, so
equivalence at the public API is exact by construction). Any unexpected
native error falls back to the Python parser for that input and is
logged; legitimate refusals propagate through the dedicated
`BoundsRefusal` exception with the Python parser's exact message.

## Layout

- `src/lib.rs` — the parser core (pure Rust) plus the PyO3 wrapper.
  Exposes `parse_bounds(text)` and `API_VERSION` as the importable
  module `print_handy.core._bounds_native`.
- `Cargo.toml` / `Cargo.lock` — the crate; dependencies are locked.
- `pyproject.toml` — the maturin build backend for the wheel.
- `rust-toolchain.toml` — the pinned Rust toolchain.

## Pins

- rustup-init: 1.28.2, SHA256-pinned per architecture in the Dockerfile.
- Rust toolchain: see `rust-toolchain.toml` (channel is the exact
  compiler version).
- PyO3: see `Cargo.lock` (abi3-py312: one wheel per CPU architecture
  serves CPython 3.12, 3.13 and 3.14).
- maturin: `>=1.16,<2` in `pyproject.toml`.

## Building

The Docker image builds the wheel in its `bounds-build` stage; nothing
else needs to. For local development:

```sh
pip install "maturin>=1.16,<2"
cd native/bounds
maturin develop --release   # installs into the current environment
```

Without the built extension everything still works — the Python parser
handles every input. Set `PRINT_HANDY_BOUNDS_IMPL=python` to force the
fallback, or `=native` to make a missing/broken extension an error.
The native-bounds CI job sets `PRINT_HANDY_BOUNDS_REQUIRE_NATIVE=1`
so its strict dispatcher test can never pass by silently skipping.
