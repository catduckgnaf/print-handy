#!/bin/sh
# Install the pinned Rust toolchain for a CI job, into a folder of its own
# that actions/cache keeps between runs (see the "Cache the pinned Rust
# toolchain" steps in .github/workflows/ci.yml and model-corpus.yml).
#
# sh.rustup.rs, static.rust-lang.org and crates.io used to be downloaded
# fresh on every CI run. With the cache restored this script downloads
# nothing. It never touches a toolchain the runner image ships, so the
# pinned version is always the one used.
#
# Usage: sh scripts/ci_install_rust.sh   (exports CARGO_HOME, RUSTUP_HOME
# and PATH to later steps through $GITHUB_ENV and $GITHUB_PATH.)
set -eu

RUST_VERSION=1.99.1
ROOT="$HOME/.rust-pinned"
CARGO_HOME="$ROOT/cargo"
RUSTUP_HOME="$ROOT/rustup"
export CARGO_HOME RUSTUP_HOME
PATH="$CARGO_HOME/bin:$PATH"
export PATH

if "$CARGO_HOME/bin/rustup" run "$RUST_VERSION" cargo --version >/dev/null 2>&1; then
    echo "Rust $RUST_VERSION restored from the cache"
else
    echo "Rust $RUST_VERSION not cached; installing it"
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
        sh -s -- -y --no-modify-path --profile minimal --default-toolchain "$RUST_VERSION"
fi
rustup default "$RUST_VERSION" >/dev/null
cargo --version

if [ -n "${GITHUB_ENV:-}" ]; then
    printf 'CARGO_HOME=%s\nRUSTUP_HOME=%s\n' "$CARGO_HOME" "$RUSTUP_HOME" >>"$GITHUB_ENV"
fi
if [ -n "${GITHUB_PATH:-}" ]; then
    echo "$CARGO_HOME/bin" >>"$GITHUB_PATH"
fi
