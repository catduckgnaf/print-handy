"""#1697: the painted fit must not measure the same scale twice to learn a tool map.

``_plate_fit``'s first measurement at the ceiling can refuse only because
the prime tower has no place beside the full-size layout; that refusal hides
the tool map the export computed, and recovering it used to cost a second
full measure-only export at the same scale. A one-tool plate whose first
measurement succeeded was likewise measured again towerless at the same
scale, and a fit entered without an inventory scope inventoried the source
once per export. These tests count measure-only exports and inventory
computations around ``_plate_fit`` and pin that none of that repeated work
happens, on sources whose fit verdicts are unchanged.
"""
from __future__ import annotations

from print_handy.core import color_geometry, model_job, source_colors
from print_handy.jobs import slice as slice_mod

from .test_color_geometry import _painted_two_plate_source
from .test_painted_fit_measure_1246 import _decisions, _dense_painted_source, _plates


def _counted_fit(monkeypatch):
    """Run _plate_fit with the measure-only exports and inventories counted."""
    exports, inventories = [], []
    real_export = color_geometry.export_color_geometry
    real_inventory = source_colors._inventory
    monkeypatch.setattr(color_geometry, 'export_color_geometry',
                        lambda *a, **k: (exports.append((k.get('scale'), k.get('tower_mm'))),
                                         real_export(*a, **k))[1])
    monkeypatch.setattr(source_colors, '_inventory',
                        lambda *a, **k: (inventories.append(a[0]), real_inventory(*a, **k))[1])
    return exports, inventories


def test_tower_refusal_recovers_the_tool_map_without_measuring_again(tmp_path, monkeypatch):
    path = _dense_painted_source(tmp_path / 'dense.3mf')
    manifest, decisions = _decisions(path)
    view = model_job._per_part_view(manifest, decisions)
    exports, inventories = _counted_fit(monkeypatch)
    fitted = slice_mod._plate_fit(path, view, {'1'}, 3.0, None)
    # The verdict is the one the three-export fit produced.
    assert fitted == {'scale': 1.0, 'yaw': 0.0, 'extent_mm': [90.0, 90.0, 2.0], 'tower_mm': 45.0}
    # The towered measurement at the ceiling refused and its map was recovered
    # from the shared inventory: no second export at the ceiling, and the
    # source was inventoried once for the whole fit.
    assert [scale for scale, _tower in exports].count(3.0) == 1
    assert len(exports) == 2
    assert len(inventories) == 1


def test_one_tool_plate_is_not_measured_again_towerless_at_the_same_scale(tmp_path, monkeypatch):
    path = _dense_painted_source(tmp_path / 'dense.3mf')
    manifest, decisions = _decisions(path)
    for row in decisions['choices'].values():
        row['slot'] = 1
    view = model_job._per_part_view(manifest, decisions)
    exports, _inventories = _counted_fit(monkeypatch)
    fitted = slice_mod._plate_fit(path, view, {'1'}, 1.0, None)
    assert fitted == {'scale': 1.0, 'yaw': 0.0, 'extent_mm': [90.0, 90.0, 2.0], 'tower_mm': 0.0}
    # The first measurement already fits and already says the plate uses one
    # tool: it is the towerless verdict too, so nothing is measured again.
    assert exports == [(1.0, None)]


def test_selection_tool_map_matches_the_exports_mapping(tmp_path):
    path = _dense_painted_source(tmp_path / 'dense.3mf')
    manifest, decisions = _decisions(path)
    _mapping, geometry = color_geometry.export_color_geometry(path, None, decisions, scale=1.0, tower_mm=0.0)
    assert geometry['measure_only']
    assert color_geometry.selection_tool_map(path, decisions, object_ids={'1'}) == _mapping
    assert len(set(_mapping.values())) == 2  # the dense plate paints with two filaments

    two_plate = tmp_path / 'two-plates.3mf'
    _painted_two_plate_source(two_plate)
    manifest, decisions = _decisions(two_plate)
    plates = _plates(two_plate, manifest)
    assert len(plates) == 2
    for plate in plates:
        ids = {str(o) for o in plate['object_ids']}
        mapping, _geometry = color_geometry.export_color_geometry(
            two_plate, None, decisions, object_ids=ids, scale=1.0, tower_mm=0.0)
        assert color_geometry.selection_tool_map(two_plate, decisions, object_ids=ids) == mapping
