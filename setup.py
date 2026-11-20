"""Build the optional paint census into Linux distribution wheels."""
import os
import sys
from pathlib import Path

from setuptools import setup
from setuptools_rust import Binding, RustExtension

# The dependency-only layer in the Dockerfile does not stage the Rust sources.
build_native = sys.platform == "linux" and Path("native/paint_census/Cargo.toml").is_file()

setup(
    rust_extensions=[
        RustExtension(
            "print_handy.core._paint_census_native",
            path="native/paint_census/Cargo.toml",
            binding=Binding.PyO3,
            args=["--locked"],
            optional=os.environ.get("PRINT_HANDY_BUILD_REQUIRE_PAINT_CENSUS") != "1",
        )
    ] if build_native else [],
    options={"bdist_wheel": {"py_limited_api": "cp312"}} if build_native else {},
)
