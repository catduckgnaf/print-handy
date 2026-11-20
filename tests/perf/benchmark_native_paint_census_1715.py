"""Deterministic scaled 3MF inventory A/B benchmark for #1715.

Run from the repository root with:
    python tests/perf/benchmark_native_paint_census_1715.py

The two scales match the issue's synthetic reference sizes (900k and 240k
triangles). This measures full source_colors manifests, not just the Rust kernel.
"""
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
import zipfile
from pathlib import Path

from print_handy.core import painted_geometry as pg
from print_handy.core import source_colors as sc

MODEL = "3D/3dmodel.model"
PALETTE = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]
SAMPLES = (
    ("primary", 900_000, 3, ((0, 1), (2,))),
    ("second", 240_000, 4, ((0, 1, 2, 3),)),
)


def _code(state: int) -> str:
    return f"{state << 2:X}" if state < 3 else f"{state - 3:X}C"


def _write_sample(path: Path, triangle_count: int, part_count: int,
                  plates: tuple[tuple[int, ...], ...]) -> None:
    """Generate the same deterministic 80%-painted inventory shape each run."""
    chunk_size = 30_000
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=1) as archive:
        # Emit model XML to a temporary spool so constructing the 43 MB primary
        # does not require holding the entire uncompressed model in RAM.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as model:
            model.write('<?xml version="1.0" encoding="UTF-8"?>'
                        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                        '<resources>')
            for part_index in range(part_count):
                mesh_id, assembly_id = str(2 * part_index + 2), str(2 * part_index + 1)
                model.write(f'<object id="{mesh_id}" type="model"><mesh><vertices>'
                            '<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>'
                            '<vertex x="0" y="1" z="0"/></vertices><triangles>')
                remaining = triangle_count // part_count + (part_index < triangle_count % part_count)
                for start in range(0, remaining, chunk_size):
                    stop = min(start + chunk_size, remaining)
                    chunk = []
                    for index in range(start, stop):
                        if index % 5:
                            state = 1 + ((index + part_index) % 3)
                            attr = f' paint_color="{_code(state)}"'
                        else:
                            attr = ""
                        chunk.append(f'<triangle v1="0" v2="1" v3="2"{attr}/>')
                    model.write("".join(chunk))
                model.write(f'</triangles></mesh></object><object id="{assembly_id}" type="model">'
                            f'<components><component objectid="{mesh_id}"/></components></object>')
            model.write('</resources><build>')
            for part_index in range(part_count):
                model.write(f'<item objectid="{2 * part_index + 1}"/>')
            model.write('</build></model>')
            model.seek(0)
            archive.writestr(MODEL, model.read())
        settings = ["<config>"]
        for index in range(part_count):
            mesh_id, assembly_id = str(2 * index + 2), str(2 * index + 1)
            settings.append(f'<object id="{assembly_id}"><metadata key="name" value="Part {index + 1}"/>'
                            f'<metadata key="extruder" value="{index % 4 + 1}"/>'
                            f'<part id="{mesh_id}" subtype="normal_part"><metadata key="name" '
                            f'value="Part {index + 1}"/><metadata key="extruder" '
                            f'value="{index % 4 + 1}"/></part></object>')
        for plate_number, indexes in enumerate(plates, 1):
            refs = "".join(f'<model_instance><metadata key="object_id" value="{2 * i + 1}"/>'
                           '</model_instance>' for i in indexes)
            settings.append(f'<plate><metadata key="plater_id" value="{plate_number}"/>'
                            f'<metadata key="plater_name" value="Plate {plate_number}"/>{refs}</plate>')
        settings.append("</config>")
        archive.writestr("Metadata/model_settings.config", "\n".join(settings))
        archive.writestr("Metadata/project_settings.config", json.dumps(
            {"filament_colour": PALETTE, "filament_type": ["PLA"] * 4}, sort_keys=True))


def _time_inventory(path: Path, native: bool):
    loader = pg._load_native_paint_census
    if native and loader() is None:
        raise RuntimeError("native paint census is unavailable; build the extension first")
    pg._load_native_paint_census = (lambda: loader()) if native else (lambda: None)
    try:
        started = time.perf_counter()
        manifest = sc.inventory(path)
        return time.perf_counter() - started, manifest
    finally:
        pg._load_native_paint_census = loader


def benchmark(rounds: int = 5) -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory(prefix="paint-census-1715-") as temp:
        root = Path(temp)
        for name, triangles, parts, plates in SAMPLES:
            source = root / f"{name}.3mf"
            _write_sample(source, triangles, parts, plates)
            times = {"python": [], "native": []}
            expected = None
            for _ in range(rounds):
                # Interleave order to reduce systematic warm/cold and load bias.
                for backend in ("python", "native"):
                    elapsed, manifest = _time_inventory(source, backend == "native")
                    if expected is None:
                        expected = manifest
                    elif manifest != expected:
                        raise AssertionError(f"{name}: full manifest differs on {backend} run")
                    times[backend].append(elapsed)
            row = {"sample": name, "triangles": triangles, "parts": parts,
                   "rounds": rounds, "manifests_equal": True}
            for backend, values in times.items():
                row[backend] = {"median_s": statistics.median(values),
                                "range_s": [min(values), max(values)], "runs_s": values}
            row["speedup"] = row["python"]["median_s"] / row["native"]["median_s"]
            results.append(row)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    for result in benchmark(args.rounds):
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
