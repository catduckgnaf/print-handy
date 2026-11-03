"""3MF intake canonicalization (#814, #825): one namespace spelling, millimeters, applied once.

The fixtures rebuild the CC0 gap-pack shapes G01 (core namespace bound to ns0:) and G03 (a 1 x 0.6 x 0.25 inch
box under two levels of component transforms) inline. The corpus cases for them land in #830.
"""
import zipfile
import xml.etree.ElementTree as ET

import pytest

from print_handy import diagnostics
from print_handy.core import model_job, orca_adapter
from print_handy.core.painted_geometry import paint_census
from print_handy.core.threemf_canonical import (
    UnsupportedThreeMFUnit, canonical_model_parts, canonicalize_3mf, intake_working_copy)

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'


def _box(x, y, z, p=''):
    corners = [(0, 0, 0), (x, 0, 0), (0, y, 0), (x, y, 0), (0, 0, z), (x, 0, z), (0, y, z), (x, y, z)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    vertices = ''.join(f'<{p}vertex x="{a:.6f}" y="{b:.6f}" z="{c:.6f}"/>' for a, b, c in corners)
    triangles = ''.join(f'<{p}triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    return f'<{p}mesh><{p}vertices>{vertices}</{p}vertices><{p}triangles>{triangles}</{p}triangles></{p}mesh>'


def _g01():
    """Prefixed millimeter assembly: item -> object 1 -> component -> mesh object 2 (32 x 23 x 8 mm)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ns0:model xmlns:ns0="{CORE}" unit="millimeter" xml:lang="en-US"><ns0:resources>'
        f'<ns0:object id="2" type="model">{_box(32, 23, 8, "ns0:")}</ns0:object>'
        '<ns0:object id="1" type="model"><ns0:components><ns0:component objectid="2"/></ns0:components></ns0:object>'
        '</ns0:resources><ns0:build>'
        '<ns0:item objectid="1" transform="1 0 0 0 1 0 0 0 1 50.000000 60.000000 0.000000"/>'
        '</ns0:build></ns0:model>')


def _g03(unit='inch', item='<item objectid="1" transform="1 0 0 0 1 0 0 0 1 1.000000 1.000000 0.000000"/>'):
    """Inch box 1 x 0.6 x 0.25 under two levels of component transforms."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model xmlns="{CORE}" unit="{unit}" xml:lang="en-US"><resources>'
        f'<object id="3" type="model">{_box(1, 0.6, 0.25)}</object>'
        '<object id="2" type="model"><components>'
        '<component objectid="3" transform="1 0 0 0 1 0 0 0 1 0.200000 0.150000 0.000000"/></components></object>'
        '<object id="1" type="model"><components>'
        '<component objectid="2" transform="1 0 0 0 1 0 0 0 1 0.100000 0.000000 0.000000"/></components></object>'
        f'</resources><build>{item}</build></model>')


def _package(path, model, extra=None):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr('3D/3dmodel.model', model)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


def _part(path, name='3D/3dmodel.model'):
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def _item_transform(model_bytes):
    item = ET.fromstring(model_bytes).find(f'{{{CORE}}}build/{{{CORE}}}item')
    return [float(v) for v in item.get('transform').split()]


def _resources(model_bytes):
    return model_bytes[model_bytes.index(b'<resources>'):model_bytes.index(b'</resources>')]


def _extent(path):
    hull, z_min, z_max = orca_adapter._footprint(path)
    xs, ys = [p[0] for p in hull], [p[1] for p in hull]
    return (min(xs), min(ys)), (max(xs) - min(xs), max(ys) - min(ys), z_max - z_min)


def test_prefixed_core_namespace_becomes_the_default_namespace(tmp_path):
    path = _package(tmp_path / 'g01.3mf', _g01())
    assert canonicalize_3mf(path) is True
    model = _part(path)
    assert b'ns0' not in model
    root = ET.fromstring(model)
    assert root.tag == f'{{{CORE}}}model'
    assert [i.get('objectid') for i in root.findall(f'{{{CORE}}}build/{{{CORE}}}item')] == ['1']


def test_inch_model_is_converted_by_scaling_build_item_transforms_only(tmp_path):
    path = _package(tmp_path / 'g03.3mf', _g03())
    before = _part(path)
    assert canonicalize_3mf(path) is True
    after = _part(path)
    assert ET.fromstring(after).get('unit') == 'millimeter'
    assert _item_transform(after) == pytest.approx([25.4, 0, 0, 0, 25.4, 0, 0, 0, 25.4, 25.4, 25.4, 0])
    # Meshes and component transforms are untouched, byte for byte.
    assert _resources(after) == _resources(before)


def test_centimeter_model_uses_its_own_factor(tmp_path):
    path = _package(tmp_path / 'cm.3mf', _g03('cm', '<item objectid="1" transform="2 0 0 0 2 0 0 0 2 1 0 0"/>'))
    canonicalize_3mf(path)
    assert _item_transform(_part(path)) == pytest.approx([20, 0, 0, 0, 20, 0, 0, 0, 20, 10, 0, 0])


def test_item_without_a_transform_gets_the_unit_scale(tmp_path):
    path = _package(tmp_path / 'bare.3mf', _g03('inch', '<item objectid="1" printable="1"/>'))
    canonicalize_3mf(path)
    model = _part(path)
    assert _item_transform(model) == pytest.approx([25.4, 0, 0, 0, 25.4, 0, 0, 0, 25.4, 0, 0, 0])
    assert ET.fromstring(model).find(f'{{{CORE}}}build/{{{CORE}}}item').get('printable') == '1'


def test_nested_inch_components_land_at_the_right_millimeter_position_and_size(tmp_path):
    path = _package(tmp_path / 'g03.3mf', _g03())
    canonicalize_3mf(path)
    (min_x, min_y), size = _extent(path)
    assert size == pytest.approx((25.4, 15.24, 6.35))
    # item (1, 1) + component (0.1, 0) + component (0.2, 0.15), all inches.
    assert (min_x, min_y) == pytest.approx((1.3 * 25.4, 1.15 * 25.4))


def test_unknown_unit_is_refused_by_name_with_a_classified_message(tmp_path):
    path = _package(tmp_path / 'furlong.3mf', _g03('furlong'))
    original = path.read_bytes()
    with pytest.raises(UnsupportedThreeMFUnit) as caught:
        canonicalize_3mf(path)
    assert "'furlong'" in str(caught.value)
    assert diagnostics.known_cause(caught.value) is not None
    assert path.read_bytes() == original


def test_canonical_files_are_not_rewritten(tmp_path):
    path = _package(tmp_path / 'ok.3mf', _g03('millimeter'))
    original = path.read_bytes()
    assert canonicalize_3mf(path, keep_original=tmp_path / 'kept.3mf') is False
    assert path.read_bytes() == original
    assert not (tmp_path / 'kept.3mf').exists()
    assert canonical_model_parts({'3D/3dmodel.model': _part(path)}) == {}


def test_external_part_in_a_non_millimeter_unit_is_refused(tmp_path):
    root = (f'<model xmlns="{CORE}" xmlns:p="{PROD}" unit="millimeter"><resources>'
            '<object id="1"><components><component objectid="1" p:path="/3D/Objects/a.model"/></components></object>'
            '</resources><build><item objectid="1"/></build></model>')
    external = f'<model xmlns="{CORE}" unit="inch"><resources><object id="1">{_box(1, 1, 1)}</object></resources></model>'
    path = _package(tmp_path / 'prod.3mf', root, {'3D/Objects/a.model': external})
    with pytest.raises(UnsupportedThreeMFUnit, match='3D/Objects/a.model'):
        canonicalize_3mf(path)


def test_external_part_without_a_unit_follows_an_inch_root(tmp_path):
    root = (f'<model xmlns="{CORE}" xmlns:p="{PROD}" unit="inch"><resources>'
            '<object id="1"><components><component objectid="1" p:path="/3D/Objects/a.model"/></components></object>'
            '</resources><build><item objectid="1"/></build></model>')
    external = f'<ns0:model xmlns:ns0="{CORE}"><ns0:resources><ns0:object id="1">{_box(1, 1, 1, "ns0:")}</ns0:object></ns0:resources></ns0:model>'
    path = _package(tmp_path / 'prod.3mf', root, {'3D/Objects/a.model': external})
    canonicalize_3mf(path)
    assert _item_transform(_part(path)) == pytest.approx([25.4, 0, 0, 0, 25.4, 0, 0, 0, 25.4, 0, 0, 0])
    assert b'ns0' not in _part(path, '3D/Objects/a.model')


def test_intake_working_copy_keeps_the_users_bytes_and_is_idempotent(tmp_path):
    (tmp_path / 'source').mkdir()
    path = _package(tmp_path / 'source' / 'g03.3mf', _g03())
    original = path.read_bytes()
    assert intake_working_copy(path) == path
    assert (path.parent / 'original' / 'g03.3mf').read_bytes() == original
    canonical = path.read_bytes()
    assert intake_working_copy(path) == path
    assert path.read_bytes() == canonical


def test_inch_model_passes_the_fit_footprint_after_intake(tmp_path):
    # #825: the fit check read the raw inch file and died on an unclassified "only millimeter" error.
    path = _package(tmp_path / 'g03.3mf', _g03())
    with pytest.raises(UnsupportedThreeMFUnit) as caught:
        orca_adapter.source_triangles(path)  # a file that skipped intake: still a classified refusal
    assert diagnostics.known_cause(caught.value) is not None
    intake_working_copy(path)
    _, size = _extent(path)
    assert size == pytest.approx((25.4, 15.24, 6.35))


def test_prefixed_core_model_is_audited_with_the_right_object_and_item_counts(tmp_path):
    raw = _package(tmp_path / 'raw.3mf', _g01())
    # The audit and the paint census read raw tag names, so they must tolerate the prefix on their own too.
    report = model_job.audit(raw)
    assert (report['objects'], report['components'], report['build_items']) == (2, 1, 1)
    assert set(paint_census(raw)) == {'1'}
    working = _package(tmp_path / 'work.3mf', _g01())
    intake_working_copy(working)
    report = model_job.audit(working)
    assert (report['objects'], report['components'], report['build_items']) == (2, 1, 1)
    assert len(report['color_manifest']['parts']) == 1
    assert report['color_manifest']['source_sha256'] == model_job.file_sha256(working)
    _, size = _extent(working)
    assert size == pytest.approx((32, 23, 8))
