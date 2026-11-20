"""Native paint census: differential and fallback tests (#1715).

The Rust census in the bounds extension counts every mesh object of a
.model member in one pass; painted_geometry.leaf_states serves parts
from it when the module verifies. Python's counting is the permanent
fallback and the oracle here: both paths must agree exactly — counts,
per-mesh decode errors, and whole inventory manifests — and every
failure of the native side (module missing, wrong version, self-check
mismatch, mid-census error) must degrade to the Python counting with
identical results.

Parts with triangle_ranges always take the Python counting by design
(the documented #1715 decision): a whole-mesh census cannot answer
for a subset of triangles.

In the native-bounds CI job the built extension is placed in
print_handy/core/ and PRINT_HANDY_PAINT_REQUIRE_NATIVE=1 turns a
missing extension from a skip into a failure, so the differential is
always exercised there.
"""

import collections
import io
import os
import re
import sys
import types
import zipfile

import pytest

from print_handy.core import painted_geometry as pg
from print_handy.core import source_colors


def _native_module():
    try:
        from print_handy.core import _bounds_native
    except Exception:
        return None
    return _bounds_native


@pytest.fixture(autouse=True)
def _clean_native_paint_cache():
    # The dispatcher caches its module verdict process-wide; fake-module
    # tests must not leak that state into their neighbours.
    pg._reset_native_paint_cache()
    yield
    pg._reset_native_paint_cache()


def _python_member_census(model):
    """The oracle census: _leaf_states_python's counting, per mesh."""
    out = {}
    for mesh_id, (start, end) in pg._object_blocks(model).items():
        body = model[start:end]
        counter, unpainted, error = collections.Counter(), 0, None
        for tri in re.finditer(rb'<(?:\w+:)?triangle\b[^>]*>', body):
            match = pg.PAINT_RE.search(tri.group(0)) or pg.MMU_RE.search(tri.group(0))
            if match:
                try:
                    counter.update(pg.decode_states(match.group(1).decode()))
                except ValueError as exc:
                    error = str(exc)
                    break
            else:
                unpainted += 1
        out[mesh_id] = ({'error': error} if error is not None
                        else {'states': dict(counter), 'unpainted': unpainted})
    return out


# Crafted members covering the decode's corners. Every one is checked
# against the Python oracle; several pin behavior the original
# std-only prototype got wrong or never covered (namespace prefixes,
# attribute-scan corners, duplicate ids).
MEMBERS = [
    ("plain multi-object",
     b'<model><resources>'
     b'<object id="1"><mesh><triangles>'
     b'<triangle paint_color="4"/><triangle paint_color="8"/><triangle/>'
     b'</triangles></mesh></object>'
     b'<object id="2"><mesh><triangles>'
     b'<triangle paint_color="48"/><triangle paint_color="84"/>'
     b'</triangles></mesh></object>'
     b'</resources></model>'),
    ("namespace-prefixed tags",
     b'<model><resources>'
     b'<p:object id="7"><mesh><triangles>'
     b'<p:triangle paint_color="841"/><p:triangle paint_color="0FC"/>'
     b'<p:triangle slic3rpe:mmu_segmentation="8"/>'
     b'</triangles></mesh></p:object>'
     b'</resources></model>'),
    ("mmu plain and prefixed",
     b'<object id="1"><mesh><triangles>'
     b'<triangle mmu_segmentation="4"/><triangle slic3rpe:mmu_segmentation="8"/>'
     b'</triangles></mesh></object>'),
    ("paint_color wins over mmu on the same triangle",
     b'<object id="1"><mesh><triangles>'
     b'<triangle mmu_segmentation="8" paint_color="4"/>'
     b'</triangles></mesh></object>'),
    ("truncated code errors for that mesh only",
     b'<object id="1"><mesh><triangles><triangle paint_color="1"/></triangles></mesh></object>'
     b'<object id="2"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'),
    ("trailing data error",
     b'<object id="1"><mesh><triangles><triangle paint_color="44"/></triangles></mesh></object>'),
    ("first valid paint occurrence wins",
     b'<object id="1"><mesh><triangles>'
     b'<triangle paint_color="ZZ" paint_color="4"/>'
     b'<triangle paint_color="" paint_color="8"/>'
     b'</triangles></mesh></object>'),
    ("attribute substring has no word boundary",
     b'<object id="1"><mesh><triangles>'
     b'<triangle xpaint_color="8"/>'
     b'</triangles></mesh></object>'),
    ("duplicate object ids: the last span wins",
     b'<object id="1"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'
     b'<object id="1"><mesh><triangles><triangle paint_color="8"/><triangle/></triangles></mesh></object>'),
    ("two id attributes: the last one wins",
     b'<object id="5" id="7"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'),
    ("data-id names the object; objectid does not",
     b'<object data-id="9"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'
     b'<object objectid="3"><mesh><triangles><triangle paint_color="8"/></triangles></mesh></object>'),
    ("an id with junk after the digits names nothing",
     b'<object id="5x"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'),
    ("an unclosed object swallows the inner close",
     b'<object id="1"><mesh><triangles><triangle paint_color="4"/></triangles></mesh>'
     b'<object id="2"><mesh><triangles><triangle paint_color="8"/></triangles></mesh></object>'),
    ("greater-than inside an attribute value ends the tag scan",
     b'<object data-x="a>b" id="4"><mesh><triangles><triangle paint_color="4"/></triangles></mesh></object>'),
    ("no objects at all",
     b'<model><resources></resources></model>'),
    ("extended states nest deep",
     b'<object id="1"><mesh><triangles>'
     b'<triangle paint_color="C0FF0FC"/>'
     b'</triangles></mesh></object>'),
]


def test_census_matches_python_on_crafted_members():
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    for label, member in MEMBERS:
        assert module.paint_census(member) == _python_member_census(member), label


def test_census_matches_python_on_corpus_members():
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    cases = os.path.join(os.path.dirname(__file__), "..", "corpus", "cases")
    checked = 0
    for case in ("prusa-mmu-project", "core-prefixed-namespace", "core-triangle-colors"):
        archive = zipfile.ZipFile(os.path.join(cases, case, "model.3mf"))
        for name in archive.namelist():
            if name.endswith(".model"):
                member = archive.read(name)
                assert module.paint_census(member) == _python_member_census(member), (case, name)
                checked += 1
    assert checked >= 3


def test_census_version_handshake():
    module = _native_module()
    if module is None:
        if os.environ.get("PRINT_HANDY_PAINT_REQUIRE_NATIVE"):
            pytest.fail("native paint census required but the extension is not importable")
        pytest.skip("native bounds extension not built")
    assert module.PAINT_CENSUS_VERSION == pg.PAINT_CENSUS_VERSION
    assert callable(module.paint_census)


# ------------------------------------------------------------ leaf_states

MODEL = (
    b'<model><resources>'
    b'<object id="1"><mesh><triangles>'
    b'<triangle paint_color="4"/><triangle paint_color="8"/><triangle/>'
    b'</triangles></mesh></object>'
    b'<object id="2"><mesh><triangles>'
    b'<triangle paint_color="1"/>'
    b'</triangles></mesh></object>'
    b'<p:object id="3"><mesh><triangles>'
    b'<p:triangle paint_color="0FC"/>'
    b'</triangles></mesh></p:object>'
    b'</resources></model>')


def _source(tmp_path, model=MODEL, member="3D/3dmodel.model"):
    path = tmp_path / "source.3mf"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, model)
    return str(path)


def _part(mesh_id, **extra):
    part = {'ancestry': [["3D/3dmodel.model", str(mesh_id)]]}
    part.update(extra)
    return part


def _outcome(fn, source, part):
    try:
        return ("ok", fn(source, part))
    except Exception as exc:
        return ("raise", type(exc).__name__, str(exc))


def test_leaf_states_dispatcher_matches_python(tmp_path):
    source = _source(tmp_path)
    for mesh_id in ("1", "2", "3", "9"):
        part = _part(mesh_id)
        expected = _outcome(pg._leaf_states_python, source, part)
        assert _outcome(pg.leaf_states, source, part) == expected, mesh_id
        with pg.decode_scope():
            assert _outcome(pg.leaf_states, source, part) == expected, mesh_id


def test_leaf_states_error_rows_raise_like_python(tmp_path):
    source = _source(tmp_path)
    with pytest.raises(ValueError, match="truncated paint_color"):
        pg.leaf_states(source, _part("2"))
    with pytest.raises(RuntimeError, match="painted part mesh 9 not found"):
        pg.leaf_states(source, _part("9"))


def test_census_is_computed_once_per_member_inside_a_scope(tmp_path, monkeypatch):
    module = _native_module()
    if module is None:
        pytest.skip("native bounds extension not built")
    calls = []
    real = module.paint_census

    def counting(model):
        calls.append(model)
        return real(model)

    monkeypatch.setattr(module, "paint_census", counting)
    pg._reset_native_paint_cache()
    source = _source(tmp_path)
    pg.leaf_states(source, _part("1"))  # warm-up: load + self-check + census
    calls.clear()
    with pg.decode_scope():
        pg.leaf_states(source, _part("1"))
        pg.leaf_states(source, _part("3"))
        pg.leaf_states(source, _part("1"))
    assert len(calls) == 1
    calls.clear()
    pg.leaf_states(source, _part("1"))
    pg.leaf_states(source, _part("1"))
    assert len(calls) == 2  # outside a scope nothing is kept


def test_strict_native_dispatcher_proof(tmp_path, monkeypatch):
    """With PRINT_HANDY_BOUNDS_IMPL=native the dispatcher must serve
    through the real extension. Skips when the extension is not built —
    except under PRINT_HANDY_PAINT_REQUIRE_NATIVE (the CI job), where
    a missing extension is a failure, never a skip."""
    module = _native_module()
    if module is None:
        if os.environ.get("PRINT_HANDY_PAINT_REQUIRE_NATIVE"):
            pytest.fail("native paint census required but not importable")
        pytest.skip("native bounds extension not built")
    calls = []
    real = module.paint_census

    def counting(model):
        calls.append(model)
        return real(model)

    monkeypatch.setattr(module, "paint_census", counting)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    pg._reset_native_paint_cache()
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert calls


# ------------------------------------------------------------- fake modules

def _fake_module(**overrides):
    module = types.SimpleNamespace()
    module.PAINT_CENSUS_VERSION = pg.PAINT_CENSUS_VERSION

    def paint_census(model):
        return _python_member_census(model)

    module.paint_census = paint_census
    module.__dict__.update(overrides)
    return module


def _install_fake(monkeypatch, fake):
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", fake)
    monkeypatch.setattr(core_pkg, "_bounds_native", fake, raising=False)
    pg._reset_native_paint_cache()


def test_dispatcher_uses_a_verified_fake_native_module(tmp_path, monkeypatch):
    calls = []
    fake = _fake_module()
    real = fake.paint_census

    def counting(model):
        calls.append(model)
        return real(model)

    fake.paint_census = counting
    _install_fake(monkeypatch, fake)
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert calls


def test_missing_module_falls_back(tmp_path, monkeypatch):
    """The artifact-renamed case: the extension cannot be imported, so
    the Python counting serves, with identical results."""
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", None)
    # A previously imported real module also lives as an attribute of
    # the package, which `from . import ...` would find first.
    monkeypatch.delattr(core_pkg, "_bounds_native", raising=False)
    pg._reset_native_paint_cache()
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert pg._native_paint_broken


def test_version_mismatch_falls_back(tmp_path, monkeypatch):
    _install_fake(monkeypatch, _fake_module(PAINT_CENSUS_VERSION=999))
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert pg._native_paint_broken


def test_self_check_failure_falls_back(tmp_path, monkeypatch):
    def wrong_census(model):
        if model == MODEL:
            return _python_member_census(model)
        return {}

    _install_fake(monkeypatch, _fake_module(paint_census=wrong_census))
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert pg._native_paint_broken


def test_malformed_result_falls_back(tmp_path, monkeypatch):
    fake = _fake_module()
    real = fake.paint_census

    def census(model):
        if model == MODEL:
            return {"1": {"states": "not-a-dict", "unpainted": 0}}
        return real(model)

    fake.paint_census = census
    _install_fake(monkeypatch, fake)
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert pg._native_paint_broken


def test_mid_census_error_falls_back_and_sticks(tmp_path, monkeypatch):
    fake = _fake_module()
    real = fake.paint_census
    state = {"armed": False}

    def flaky(model):
        if state["armed"]:
            raise ValueError("boom")
        return real(model)

    fake.paint_census = flaky
    _install_fake(monkeypatch, fake)
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    state["armed"] = True
    pg._reset_native_paint_cache()
    _install_fake(monkeypatch, fake)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert pg._native_paint_broken


def test_python_setting_never_consults_the_module(tmp_path, monkeypatch):
    calls = []
    fake = _fake_module()

    def counting(model):
        calls.append(model)
        return _python_member_census(model)

    fake.paint_census = counting
    _install_fake(monkeypatch, fake)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "python")
    source = _source(tmp_path)
    assert pg.leaf_states(source, _part("1")) == {'states': {1: 1, 2: 1}, 'unpainted': 1}
    assert calls == []


def test_native_setting_with_missing_module_raises(tmp_path, monkeypatch):
    import print_handy.core as core_pkg

    monkeypatch.setitem(sys.modules, "print_handy.core._bounds_native", None)
    monkeypatch.delattr(core_pkg, "_bounds_native", raising=False)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    pg._reset_native_paint_cache()
    source = _source(tmp_path)
    with pytest.raises(RuntimeError, match="native paint census required"):
        pg.leaf_states(source, _part("1"))


def test_triangle_ranges_parts_never_consult_the_census(tmp_path, monkeypatch):
    """Ranges parts take the Python counting by design, even with a
    verified native module and the strict setting."""
    calls = []
    fake = _fake_module()

    def counting(model):
        calls.append(model)
        return _python_member_census(model)

    fake.paint_census = counting
    _install_fake(monkeypatch, fake)
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", "native")
    source = _source(tmp_path)
    part = _part("1", triangle_ranges=((0, 1),))
    expected = pg._leaf_states_python(source, part)
    assert pg.leaf_states(source, part) == expected == {'states': {1: 1, 2: 1}, 'unpainted': 0}
    assert calls == []


# ------------------------------------------------------- inventory end to end

CORPUS = os.path.join(os.path.dirname(__file__), "..", "corpus", "cases")
INVENTORY_CASES = ("prusa-mmu-project", "core-prefixed-namespace", "core-triangle-colors",
                   "plates-multicolor", "four-colors-one-plate", "assembly-two-color-roomy")


def _inventory_with(path, impl, monkeypatch):
    monkeypatch.setenv("PRINT_HANDY_BOUNDS_IMPL", impl)
    pg._reset_native_paint_cache()
    return source_colors.inventory(path)


@pytest.mark.parametrize("case", INVENTORY_CASES)
def test_inventory_manifests_match_on_corpus_cases(case, monkeypatch):
    path = os.path.join(CORPUS, case, "model.3mf")
    assert _inventory_with(path, "python", monkeypatch) == _inventory_with(path, "native", monkeypatch)


def _rewrite_member(source_bytes, member, transform):
    zin = zipfile.ZipFile(io.BytesIO(source_bytes))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            payload = zin.read(item.filename)
            if item.filename == member:
                payload = transform(payload)
            zout.writestr(item, payload)
    return out.getvalue()


def test_inventory_malformed_paint_parity(tmp_path, monkeypatch):
    """A truncated paint code marks the painting class unknown on the
    affected plate, with the Python decoder's exact reason, on both
    paths — and the whole manifests stay identical."""
    raw = open(os.path.join(CORPUS, "prusa-mmu-project", "model.3mf"), "rb").read()
    bad = _rewrite_member(raw, "3D/3dmodel.model",
                          lambda m: m.replace(b'mmu_segmentation="8"', b'mmu_segmentation="1"', 1))
    path = tmp_path / "bad.3mf"
    path.write_bytes(bad)
    manifests = {impl: _inventory_with(str(path), impl, monkeypatch)
                 for impl in ("python", "native")}
    assert manifests["python"] == manifests["native"]
    painting = manifests["native"]["plate_usage"]["1"]["classes"]["painting"]
    assert painting["status"] == "unknown"
    assert painting["reason"] == "paint states could not be decoded: truncated paint_color"
