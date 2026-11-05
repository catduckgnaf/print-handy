"""Modeled supports: detection and deterministic mesh measurement (#903 supports question, #904 gate)."""

from pathlib import Path

import pytest

from print_handy.core import modeled_supports, slice_warnings

FIXTURE = Path(__file__).parent / 'fixtures' / 'modeled_supports_shelf.3mf'


def _level(report, z):
    return next(level for level in report['levels'] if abs(level['z'] - z) < 0.01)


def test_support_object_found_by_name():
    assert modeled_supports.support_objects(FIXTURE) == [
        ('4', 'REMOVABLE SUPPORTS (delete object to remove)')]


def test_measured_gap_and_contact_match_the_reference_shelf():
    report = modeled_supports.measure(FIXTURE)
    crossbar, shelf, base = _level(report, 13.1), _level(report, 2.65), _level(report, 0.4)
    assert crossbar['gap_mm'] == pytest.approx(0.22, abs=0.01)
    assert shelf['gap_mm'] == pytest.approx(0.20, abs=0.01)
    assert crossbar['area_mm2'] == pytest.approx(2024, rel=0.02)
    assert shelf['area_mm2'] == pytest.approx(2024, rel=0.02)
    assert 0 < crossbar['touch_pct'] < 5
    assert 0 < shelf['touch_pct'] < 5
    assert base['gap_mm'] is None and base['area_mm2'] == pytest.approx(9486, rel=0.02)


def test_no_support_object_means_no_report(tmp_path):
    assert modeled_supports.safe_describe(None) == []
    assert modeled_supports.safe_describe(tmp_path / 'missing.3mf') == []


def test_gate_question_names_modeled_supports_and_keeps_its_shape():
    finding = {'objects': ['BLACK BODY'], 'texts': ['Floating regions detected']}
    plain = slice_warnings.question(finding)
    with_file = slice_warnings.question(finding, str(FIXTURE))
    assert plain.startswith(slice_warnings.QUESTION_PREFIX)
    assert 'modeled supports' not in plain
    assert 'This file has modeled supports' in with_file
    assert '0.22 mm below' in with_file and '0.20 mm below' in with_file
    assert with_file.endswith('What should I do?')
