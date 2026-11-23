"""The single-pass coverage scan must match the legacy per-function scans exactly (#1795)."""

import random

import pytest

from print_handy.core import support_clearance as sc

TYPES = ['Outer wall', 'Inner wall', 'Support', 'Support interface', 'Sparse infill', 'Skirt']


def _gcode(seed, markers):
    rng = random.Random(seed)
    lines = ['; support_type = tree(auto)' if seed % 2 else '; support_type = normal(auto)', 'G90', 'M83']
    for layer in range(rng.randint(3, 12)):
        lines.append(f'G1 Z{layer * 0.2 + rng.choice([0, 0.1]):.3f} F600')
        for inst in range(rng.randint(1, 3)):
            name = f'Part{inst}'
            if markers == 'orca':
                lines.append(f'; printing object {name} id:{inst} copy 0')
            elif markers == 'klipper':
                lines.append(f'EXCLUDE_OBJECT_START NAME={name}_id_{inst}_copy_0')
            for _ in range(rng.randint(2, 6)):
                lines.append(f';TYPE:{rng.choice(TYPES)}')
                for _ in range(rng.randint(1, 8)):
                    words = []
                    for ax in 'XYZ':
                        if rng.random() < (0.9 if ax != 'Z' else 0.1):
                            words.append(f'{ax}{rng.uniform(-5, 200):.3f}')
                    if rng.random() < 0.7:
                        words.append(f'E{rng.uniform(0, 1):.4f}')
                    cmd = rng.choice(['G1', 'G0', 'g1', 'G1'])
                    tail = rng.choice(['', ' ; printing object x', ' ; stop printing object'])
                    lines.append(f'{cmd} ' + ' '.join(words) + tail)
                if rng.random() < 0.3:
                    lines.append('G92 E0')
            if markers == 'orca':
                lines.append(f'; stop printing object {name} id:{inst} copy 0')
            elif markers == 'klipper':
                lines.append(f'EXCLUDE_OBJECT_END NAME={name}_id_{inst}_copy_0')
        if markers == 'mixed' and rng.random() < 0.5:
            lines.append('EXCLUDE_OBJECT_END NAME=stray')
    return '\n'.join(lines) + '\n'


@pytest.mark.parametrize('markers', ['none', 'orca', 'klipper', 'mixed'])
@pytest.mark.parametrize('seed', range(25))
def test_scan_matches_legacy_functions(seed, markers):
    text = _gcode(seed, markers)
    scan = sc.coverage_scan(text)
    assert scan is not None
    assert scan.model_bounds == sc.gcode_model_bounds(text)
    assert scan.object_bounds == sc.gcode_object_bounds(text)
    assert scan.model_height == sc.gcode_model_height(text)


@pytest.mark.parametrize('markers', ['none', 'orca', 'klipper'])
@pytest.mark.parametrize('seed', range(8))
def test_coverage_result_identical_with_and_without_scan(seed, markers, monkeypatch):
    text = _gcode(seed, markers)
    regions = [
        {'kind': 'supportable_overhang', 'part': 'Part0', 'bbox_mm': [[10, 10], [60, 60]],
         'z_mm': {'min': 0.4, 'max': 0.4}},
        {'kind': 'low_clearance', 'part': 'Part1', 'bbox_mm': [[20, 0], [120, 100]],
         'z_mm': {'min': 1.0, 'max': 1.0}, 'flagged': True},
    ]
    plates = [{'gcode': text, 'source_bounds': (0.0, 0.0, 100.0, 100.0), 'source_height': 2.0}]
    fast = sc.verify_coverage_by_plate(regions, [dict(p) for p in plates])
    monkeypatch.setenv('PRINT_HANDY_COVERAGE_SCAN', 'legacy')
    assert sc.coverage_scan(text) is None
    legacy = sc.verify_coverage_by_plate(regions, [dict(p) for p in plates])
    assert fast == legacy


def test_scan_failure_falls_back_to_legacy(monkeypatch):
    def boom(text):
        raise RuntimeError('boom')
    monkeypatch.setattr(sc, '_GcodeScan', boom)
    assert sc.coverage_scan('G1 X1 Y1 E1\n') is None
