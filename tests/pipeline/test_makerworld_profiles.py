import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from print_handy.core import makerworld_profiles as profiles
from tests.bound_names import setattr_bound
from print_handy.jobs import common as jobs_common
from print_handy.jobs import sources as jobs_sources


def test_902337_one_profile_one_plate_not_23_designs(monkeypatch):
    fixture = json.loads((HERE / 'test-data/makerworld_902337.json').read_text())
    monkeypatch.setattr(jobs_sources, 'maker_headers', lambda: {})
    setattr_bound(monkeypatch, jobs_common, 'request_json', lambda *args, **kwargs: fixture)
    job = jobs_sources.maker_inventory('https://makerworld.com/en/models/902337-raskog-ikea-large-bin')
    assert len(job['sources']) == 1
    row = job['sources'][0]
    assert row['instance_id'] == 861646
    assert len(row['plates']) == 1
    assert sum(plate['objects'] for plate in row['plates']) == 0
    assert row['recommended_profile']['printer'] == 'A1'
    assert row['recommended_profile']['nozzle_mm'] == 0.4


def test_profile_fragment_selects_exact_instance():
    rows = [
        {'instance_id': 1, 'title': 'small', 'plates': [], 'compatibility': {'devProductName': 'A1 mini', 'nozzleDiameter': 0.4}},
        {'instance_id': 861646, 'title': 'linked profile', 'plates': [{'name': 'plate 1'}], 'compatibility': {'devProductName': 'X1 Carbon', 'nozzleDiameter': 0.4}},
    ]
    profiles.annotate(rows)
    selected = profiles.choose(rows, '861646')
    assert selected is rows[1]
    assert profiles.selection_summary(selected) == 'linked profile | X1 Carbon | 1 plate(s)'


def test_a1_mini_first_and_nozzle_and_plate_penalties():
    rows = [
        {'instance_id': 1, 'title': 'large nozzle', 'plates': [{'name': 'a'}, {'name': 'b'}],
         'compatibility': {'devProductName': 'A1', 'nozzleDiameter': 0.8}},
        {'instance_id': 2, 'title': 'mini', 'plates': [{'name': 'a'}],
         'compatibility': {'devProductName': 'A1 mini', 'nozzleDiameter': 0.4}},
    ]
    profiles.annotate(rows)
    assert profiles.choose(rows) is rows[1]


def test_missing_token_names_the_file_and_the_doc(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    with pytest.raises(RuntimeError) as exc:
        jobs_sources.maker_headers()
    assert str(tmp_path / '.config/bambu/token') in str(exc.value)
    assert 'docs/PROJECT-REFERENCE.md' in str(exc.value)


def test_token_file_becomes_bearer_header(monkeypatch, tmp_path):
    (tmp_path / '.config/bambu').mkdir(parents=True)
    (tmp_path / '.config/bambu/token').write_text('example-token\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    assert jobs_sources.maker_headers()['Authorization'] == 'Bearer example-token'
