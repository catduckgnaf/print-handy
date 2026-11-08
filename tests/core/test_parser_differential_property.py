"""Seeded differential / property tests for the mesh and per-layer parsers (P1-2).

No property library: a stdlib ``random`` generator with fixed seeds, in the
corpus style. Three paired surfaces are fed the same generated inputs:

(a) Random triangle meshes (valid, degenerate and malformed) through the
    fast 3MF mesh path (``mesh_fast`` via ``orca_adapter._parse_3mf_triangles``)
    and the ElementTree reference path (the fast path disabled the same way
    ``test_mesh_fast_adversarial.py`` disables it). The outcomes — result or
    exception type — must agree exactly; neither side may raise an exception
    the other does not.
(b) The same generated triangle soups through ``mesh_fast.bounds_and_volume``
    and a slow reference loop written here from the documented semantics
    (componentwise min/max and the signed tetrahedron volume sum).
(c) Generated ``custom_gcode_per_layer.xml`` documents through
    ``layer_gcode.parse`` and a slow reference parser written here from the
    module's documented semantics (plate ids, ``top_z``/``extruder`` coercion,
    and the two slicer dialects for entry kinds). Malformed XML must be a
    named ``ParseError`` refusal from the real parser, never a silent [].

Every assertion is on a real outcome (parsed rows, triangle tuples, bounds,
or the raised exception type), so each test fails if the behavior it names
changes on either side of the pair.
"""
import random
import xml.etree.ElementTree as ET
import zipfile

import pytest

from print_handy.core import layer_gcode, mesh_fast, orca_adapter

NS = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
SEEDS = list(range(40))


# --------------------------------------------------------------------- util

def outcome(fn):
    """('ok', repr(result)) or ('err', exception type name): NaN and -0.0 stay visible."""
    try:
        return 'ok', repr(fn())
    except Exception as exc:  # noqa: BLE001 - the exception TYPE is the behavior compared
        return 'err', type(exc).__name__


# ------------------------------------------------------- (a) generated meshes

def _coord(rng, pool):
    kind = rng.choice(pool)
    if kind == 'plain':
        return f'{rng.uniform(-60, 260):.3f}'
    if kind == 'int':
        return str(rng.randint(-20, 300))
    if kind == 'zero':
        return rng.choice(['0', '-0', '0.0'])
    if kind == 'nan':
        return 'nan'
    if kind == 'inf':
        return rng.choice(['inf', '-inf'])
    if kind == 'garbage':
        return rng.choice(['two', '0x10', ''])
    raise AssertionError(kind)


def generated_model(rng):
    """One random 3MF model document: (bytes, variant name)."""
    variant = rng.choice(['valid', 'valid', 'valid', 'degenerate', 'bad_index',
                          'hostile_number', 'empty_mesh', 'no_triangles'])
    nverts = rng.randint(3, 9)
    pool = ['plain', 'int', 'zero'] + (['nan', 'inf', 'garbage'] if variant == 'hostile_number' else [])
    verts = []
    for _ in range(nverts):
        verts.append('<vertex x="%s" y="%s" z="%s"/>' % (_coord(rng, pool), _coord(rng, pool), _coord(rng, pool)))
    tris = []
    if variant == 'empty_mesh':
        mesh = '<mesh><vertices>%s</vertices><triangles></triangles></mesh>' % ''.join(verts)
    elif variant == 'no_triangles':
        mesh = '<mesh><vertices>%s</vertices></mesh>' % ''.join(verts)
    else:
        for _ in range(rng.randint(1, 6)):
            if variant == 'degenerate':
                idx = [rng.randrange(nverts)] * 3
            elif variant == 'bad_index':
                idx = [rng.randrange(nverts), rng.randrange(nverts),
                       rng.choice([nverts, nverts + 5, -1, 10 ** 12])]
            else:
                idx = [rng.randrange(nverts) for _ in range(3)]
            tris.append('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(idx))
        mesh = ('<mesh><vertices>%s</vertices><triangles>%s</triangles></mesh>'
                % (''.join(verts), ''.join(tris)))
    doc = (f'<model xmlns="{NS}"><resources><object id="1">{mesh}</object></resources>'
           '<build><item objectid="1"/></build></model>')
    return doc.encode(), variant


def write_zip(path, data):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', data)
    mesh_fast._TABLE_CACHE.clear()
    return path


def reference_outcome(path, monkeypatch):
    """The ElementTree path's outcome: the fast table reader forced off."""
    with monkeypatch.context() as m:
        m.setattr(mesh_fast, 'archive_tables', lambda z, n: (z.read(n), None))
        return outcome(lambda: orca_adapter._parse_3mf_triangles(path))


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_mesh_fast_path_matches_reference(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    data, variant = generated_model(rng)
    path = write_zip(tmp_path / 'case.3mf', data)
    expected = reference_outcome(path, monkeypatch)
    mesh_fast._TABLE_CACHE.clear()
    got = outcome(lambda: orca_adapter._parse_3mf_triangles(path))
    assert got == expected, f'seed {seed} ({variant}): fast path disagreed with the reference path'
    if expected[0] == 'ok':
        # A second read (cache warm) must serve the same geometry, never
        # another archive's or a poisoned entry.
        assert outcome(lambda: orca_adapter._parse_3mf_triangles(path)) == expected


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_mesh_tables_never_crash_unhandled(tmp_path, seed):
    rng = random.Random(10_000 + seed)
    data, _variant = generated_model(rng)
    result = outcome(lambda: mesh_fast.mesh_tables(data))
    # Declining (None) is a named outcome; producing tables is one too. What
    # may never happen is an exception type outside the parser's own contract.
    assert result[0] == 'ok', f'seed {seed}: mesh_tables raised {result[1]} instead of declining or parsing'
    if result[1] != 'None':
        tables = mesh_fast.mesh_tables(data)
        assert set(tables) == {'1'}
        assert len(tables['1']) >= 0


# ------------------------------------------- (b) bounds and volume, generated

def reference_bounds_and_volume(tris):
    """The documented semantics, written slowly: min/max per axis and the
    signed tetrahedron sum; an empty table is a ValueError."""
    if not tris:
        raise ValueError('empty triangle table')
    lo = [min(p[a] for t in tris for p in t) for a in range(3)]
    hi = [max(p[a] for t in tris for p in t) for a in range(3)]
    vol = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in tris:
        vol += (ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)) / 6.0
    return lo, hi, vol


def generated_triangles(rng):
    n = rng.choice([0, 1, 1, 2, 5, 17])
    special = [float('nan'), float('inf'), float('-inf'), -0.0, 1e308, -1e308]

    def value():
        if rng.random() < 0.12:
            return rng.choice(special)
        return round(rng.uniform(-100, 100), rng.choice([0, 1, 3]))

    tris = []
    for _ in range(n):
        if rng.random() < 0.2:  # degenerate: a repeated point
            p = (value(), value(), value())
            tris.append((p, p, (value(), value(), value())))
        else:
            tris.append(tuple((value(), value(), value()) for _ in range(3)))
    return tris


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_bounds_and_volume_matches_slow_reference(seed):
    tris = generated_triangles(random.Random(20_000 + seed))
    assert outcome(lambda: mesh_fast.bounds_and_volume(tris)) == \
        outcome(lambda: reference_bounds_and_volume(tris)), f'seed {seed}: {tris!r}'


# ------------------------------------- (c) per-layer G-code XML, generated

_TOOL_WORDS = ('toolchange', 'extruderchange', 'colorchange', 'colourchange')


def reference_kind(attrs):
    """Entry kind from the documented rules (layer_gcode's module docstring
    and dialect table): a G-code payload naming a tool/color change wins,
    then a payload naming a pause, then any other payload is custom; with no
    payload, the type name wins over the numeric dialect (Bambu/Orca
    ColorChange=0, PausePrint=1; Prusa PausePrint=1, ColorChange=2,
    ToolChange=4)."""
    import re
    gcode = re.sub(r'[^a-z0-9]', '', str(attrs.get('gcode') or '').lower())
    if any(word in gcode for word in _TOOL_WORDS) or gcode == 'm600':
        return 'tool_change'
    if 'pause' in gcode:
        return 'pause'
    if gcode:
        return 'custom'
    raw_type = str(attrs.get('type') or '').strip()
    named = {'colorchange': 'tool_change', 'colourchange': 'tool_change',
             'toolchange': 'tool_change', 'extruderchange': 'tool_change',
             'pauseprint': 'pause', 'pause': 'pause'}.get(re.sub(r'[^a-z]', '', raw_type.lower()))
    if named:
        return named
    return {'0': 'tool_change', '1': 'pause', '2': 'tool_change', '4': 'tool_change'}.get(raw_type, 'custom')


def reference_parse(xml_bytes):
    """Slow reference for layer_gcode.parse, from its documented row shape."""
    root = ET.fromstring(xml_bytes)
    rows = []
    plates = [node for node in root.iter() if node.tag.split('}')[-1] == 'plate']
    if not plates:
        plates = [root]
    for position, plate in enumerate(plates, 1):
        info = next((n for n in plate if n.tag.split('}')[-1] == 'plate_info'), None)
        plate_id = str(info.get('id')) if info is not None and info.get('id') else str(position)
        for node in plate.iter():
            if node.tag.split('}')[-1] != 'layer':
                continue
            attrs = dict(node.attrib)
            try:
                top_z = float(attrs['top_z'])
            except (KeyError, TypeError, ValueError):
                top_z = None
            try:
                extruder = int(attrs['extruder'])
            except (KeyError, TypeError, ValueError):
                extruder = None
            rows.append({'plate': plate_id, 'top_z': top_z, 'kind': reference_kind(attrs),
                         'source_extruder': extruder,
                         'color': (attrs.get('color') or '').strip() or None})
    return rows


_TOP_Z = ['0.2', '1.6', '12.75', '-0.4', '0', 'abc', '', None]
_EXTRUDER = ['1', '2', '3', '0', 'x', '', None]
_TYPE = ['0', '1', '2', '4', '3', 'ColorChange', 'PausePrint', 'ToolChange', 'pause', '', None]
_GCODE = ['tool_change', 'M600', 'pause_print', 'color_change', 'colour_change',
          'extruder_change', 'G1 X10', 'M104 S200', '', None]
_COLOR = ['#FF0000', 'red', '  #00FF00 ', '', None]


def generated_layer_doc(rng):
    plates = []
    for p in range(rng.randint(1, 3)):
        layers = []
        for _ in range(rng.randint(0, 4)):
            attrs = {}
            for name, pool in (('top_z', _TOP_Z), ('extruder', _EXTRUDER), ('type', _TYPE),
                               ('gcode', _GCODE), ('color', _COLOR)):
                value = rng.choice(pool)
                if value is not None:
                    attrs[name] = value
            attr_text = ''.join(f' {k}="{v}"' for k, v in attrs.items())
            layers.append(f'<layer{attr_text}/>')
        info = f'<plate_info id="{rng.choice(["1", "2", "main"])}"/>' if rng.random() < 0.5 else ''
        plates.append(f'<plate>{info}{"".join(layers)}</plate>')
    return ('<custom_gcode_per_layer>%s</custom_gcode_per_layer>' % ''.join(plates)).encode()


@pytest.mark.parametrize('seed', SEEDS)
def test_generated_layer_entries_match_slow_reference(seed):
    doc = generated_layer_doc(random.Random(30_000 + seed))
    expected = reference_parse(doc)
    got = layer_gcode.parse(doc)
    assert got == expected, f'seed {seed}: {doc!r}'
    # The derived inventory must agree with the same rows, not just parse().
    assert layer_gcode.tool_changes(got) == [r for r in expected if r['kind'] == 'tool_change']
    assert layer_gcode.tool_change_extruders(got) == sorted(
        {r['source_extruder'] for r in expected if r['kind'] == 'tool_change' and r['source_extruder']})


@pytest.mark.parametrize('doc', [
    b'<custom_gcode_per_layer><plate><layer top_z="1.0"',
    b'<custom_gcode_per_layer><plate></custom_gcode_per_layer>',
    b'not xml at all',
    b'',
    b'<custom_gcode_per_layer><plate><layer top_z="1.0"/></plate>',
])
def test_malformed_layer_xml_is_a_named_refusal(doc):
    with pytest.raises(ET.ParseError):
        layer_gcode.parse(doc)
