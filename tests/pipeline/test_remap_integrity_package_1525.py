"""#1525 PR C: remap integrity on the package the slicer is handed, and per-plate slice records.

remap_integrity.validate reads a source before a remap. These tests drive the
production entry points (orca_adapter.slice_model, model_job.slice_job and
editable_export.export_editable) and check that the package each one is about
to write carries every role, paint state, support designation, height range
and per-layer entry of its source on the filament the mapping names, or the
job refuses by name with nothing written and the slicer never run. A remapper
defect is simulated by patching one of its rules; the refusal must name the
fact that would have been lost.
"""
import io
import json
import urllib.request
import zipfile
from unittest.mock import patch

import pytest

from print_handy.core import (editable_export, layer_gcode, model_job, orca_adapter, package_remap,
                              painted_geometry, remap_integrity, slice_route)
from tests.pipeline.test_remap_package_prusa_1155 import PRUSA_CASE
from tests.pipeline.test_slice_route_1279 import _fake_orca, decisions, two_part_source

# A height tool change to filament 2 at 3 mm and an authored pause at 5 mm.
LAYERS = (b'<?xml version="1.0"?><custom_gcodes_per_layer><plate><plate_info id="1"/>'
          b'<layer top_z="3" type="0" extruder="2" color="#2980B9" extra="" gcode="tool_change"/>'
          b'<layer top_z="5" type="1" extruder="1" color="" extra="" gcode=""/>'
          b'<mode value="MultiAsSingle"/></plate></custom_gcodes_per_layer>')
CUSTOM = LAYERS.replace(b'type="1" extruder="1" color="" extra="" gcode=""',
                        b'type="4" extruder="1" color="" extra="" gcode="M117 Halfway"')
RANGES = (b'<objects><object id="3"><range min_z="2" max_z="4">'
          b'<option opt_key="extruder">1</option></range></object></objects>')


def _slice(source, tmp_path, picks, *, object_ids=None):
    """slice_model with a fake Orca; returns (provenance, captured). captured stays empty
    when the slicer never ran."""
    state = model_job.printer_state.load()
    captured = {}
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca(captured)), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        _out, provenance, _configs = orca_adapter.slice_model(
            source, tmp_path / "out", state, color_decisions=decisions(source, picks),
            **({"object_ids": object_ids} if object_ids is not None else {}))
    return provenance, captured


def _assert_nothing_written(tmp_path, captured):
    out = tmp_path / "out"
    assert captured == {}, "the slicer ran on a package that failed its integrity check"
    leftovers = sorted(p.name for p in out.rglob("*") if p.suffix in (".3mf", ".tmp")) if out.exists() else []
    assert leftovers == [], f"a refused slice left a package behind: {leftovers}"


# --- layered changes on the remap route ----------------------------------------------

def test_the_remap_route_keeps_an_authored_pause_and_renumbers_the_height_tool_change(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={layer_gcode.MEMBER: LAYERS})
    provenance, captured = _slice(source, tmp_path, {1: 3, 2: 1})
    assert provenance["geometry"]["slice_route"]["route"] == "remap"
    rows = layer_gcode.parse(captured["members"][layer_gcode.MEMBER])
    # Filament 1 is job filament 2 and filament 2 is job filament 1.
    assert [(r["top_z"], r["kind"], r["source_extruder"]) for r in rows] == [
        (3.0, "tool_change", 1), (5.0, "pause", 2)]


def test_custom_height_gcode_is_refused_by_the_remapper_by_name_never_dropped(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={layer_gcode.MEMBER: CUSTOM})
    provenance, captured = _slice(source, tmp_path, {1: 3, 2: 1})
    route = provenance["geometry"]["slice_route"]
    assert route["route"] == "rebuild"
    assert "custom G-code at height(s) [5.0]" in route["remap_refused"]
    assert "custom G-code" in slice_route.route_notice(provenance)


# --- a remapper that loses or misassigns a fact refuses the slice ----------------------

def _keep_support_designation(original):
    def permute(settings, mapping, **kwargs):
        out, notes = original(settings, mapping, **kwargs)
        return dict(out, support_filament=settings["support_filament"]), notes
    return permute


def _unmapped_paint(original):
    return lambda data, table: original(data, {state: state for state in table})


@pytest.mark.parametrize("extra, project, target, replacement, named", [
    ({"Metadata/layer_config_ranges.xml": RANGES}, None, "remap_ranges",
     lambda original: lambda data, mapping, object_map=None: data,
     "height-range extruder 1 names extruder 1, but source extruder 1 maps to 2"),
    ({layer_gcode.MEMBER: LAYERS}, None, "layer_gcode.remap",
     lambda original: lambda data, mapping, **kwargs: (data, {}),
     "per-layer entry 1 names extruder 2, but source extruder 2 maps to 1"),
    ({}, {"support_filament": "2"}, "permute_project_settings", _keep_support_designation,
     "support_filament names filament 2, but source filament 2 maps to 1"),
], ids=["height-range", "layer-change", "support-filament"])
def test_a_remapper_that_misassigns_a_fact_refuses_the_slice_and_writes_nothing(
        tmp_path, extra, project, target, replacement, named):
    source = two_part_source(tmp_path / "s.3mf", extra=extra, project=project)
    owner, name = (layer_gcode, "remap") if target == "layer_gcode.remap" else (package_remap, target)
    captured = {}
    with patch.object(owner, name, replacement(getattr(owner, name))):
        with pytest.raises(remap_integrity.IntegrityRefused) as refused:
            _slice_capturing(source, tmp_path, {1: 3, 2: 1}, captured)
    assert named in str(refused.value)
    assert str(refused.value).endswith("Nothing was written.")
    _assert_nothing_written(tmp_path, captured)


def test_paint_left_on_the_old_numbering_refuses_the_slice_and_writes_nothing(tmp_path):
    source = tmp_path / "prusa.3mf"
    source.write_bytes(PRUSA_CASE.read_bytes())
    captured = {}
    with patch.object(painted_geometry, "rewrite_paint_bytes",
                      _unmapped_paint(painted_geometry.rewrite_paint_bytes)):
        with pytest.raises(remap_integrity.IntegrityRefused) as refused:
            _slice_capturing(source, tmp_path, {1: 2, 2: 1}, captured)
    assert "painted regions changed filament or were lost" in str(refused.value)
    _assert_nothing_written(tmp_path, captured)


def test_a_modifier_the_remapper_retypes_refuses_the_export_and_writes_nothing(tmp_path):
    from tests.core.test_usage_reads_1155 import modifier_source
    source = modifier_source(tmp_path / "mod.3mf", 2)
    target = tmp_path / "export.3mf"
    original = package_remap.remap_extruder_metadata

    def retype(data, mapping, *, member):
        return original(data, mapping, member=member).replace(b'subtype="modifier_part"',
                                                              b'subtype="normal_part"')
    with patch.object(package_remap, "remap_extruder_metadata", retype):
        with pytest.raises(editable_export.ExportRefused, match=r"roles of object\(s\) \['1'\] changed"):
            editable_export.export_editable(source, target, mapping={1: 2, 2: 1, 3: 3})
    assert not target.exists()


def _slice_capturing(source, tmp_path, picks, captured):
    state = model_job.printer_state.load()
    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca(captured)), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        return orca_adapter.slice_model(source, tmp_path / "out", state,
                                        color_decisions=decisions(source, picks))


# --- the rebuilt export -----------------------------------------------------------------

def test_a_rebuild_that_drops_a_height_color_change_refuses_the_slice(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={layer_gcode.MEMBER: LAYERS})
    # Both filaments on one slot: the rebuild route, which carries the
    # height tool change (and, by design, not the pause).
    provenance, captured = _slice(source, tmp_path / "sound", {1: 2, 2: 2})
    assert provenance["geometry"]["slice_route"]["route"] == "rebuild"
    rows = layer_gcode.parse(captured["members"][layer_gcode.MEMBER])
    assert [row["kind"] for row in rows] == ["tool_change"]

    def drop_everything(xml, tool_map, z_scale=1.0, **kwargs):
        return xml, {"kept": 0, "dropped": 2}
    captured = {}
    with patch.object(layer_gcode, "remap", drop_everything):
        with pytest.raises(remap_integrity.IntegrityRefused,
                           match=r"1 height color change\(s\) were written as 0"):
            _slice_capturing(source, tmp_path, {1: 2, 2: 2}, captured)
    _assert_nothing_written(tmp_path, captured)


# --- the editable export ------------------------------------------------------------------

def test_an_editable_export_whose_remap_misassigns_a_height_range_is_refused_and_not_written(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={"Metadata/layer_config_ranges.xml": RANGES})
    target = tmp_path / "export" / "s.3mf"
    target.parent.mkdir()
    with patch.object(package_remap, "remap_ranges", lambda data, mapping, object_map=None: data):
        with pytest.raises(editable_export.ExportRefused, match="height-range extruder 1"):
            editable_export.export_editable(source, target, mapping={1: 2, 2: 1})
    assert not target.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == []


def test_an_editable_export_keeps_the_pause_and_moves_the_tool_change(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", extra={layer_gcode.MEMBER: LAYERS})
    target = tmp_path / "export.3mf"
    editable_export.export_editable(source, target, mapping={1: 2, 2: 1})
    with zipfile.ZipFile(target) as archive:
        rows = layer_gcode.parse(archive.read(layer_gcode.MEMBER))
    assert [(r["kind"], r["source_extruder"]) for r in rows] == [("tool_change", 1), ("pause", 2)]


# --- per-plate usage: which source plates a slice prints ---------------------------------

def test_a_slice_of_one_plate_records_that_plate_and_a_whole_slice_records_every_plate(tmp_path):
    source = two_part_source(tmp_path / "s.3mf", plates=[("Plate 1", [0]), ("Plate 2", [1])])
    provenance, _captured = _slice(source, tmp_path / "one", {1: 3, 2: 1}, object_ids=["3"])
    assert provenance["selected_plates"] == ["2"]
    provenance, _captured = _slice(source, tmp_path / "all", {1: 3, 2: 1})
    assert provenance["selected_plates"] == ["1", "2"]


def test_each_separately_sliced_source_plate_records_its_own_plate(tmp_path, monkeypatch):
    from tests.pipeline.test_plate_merge import _fake_slice, _job, _source
    monkeypatch.setattr(model_job.printer_state, "check",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("contacted the printer")))
    source = tmp_path / "s.3mf"
    _source(source, {"1": (40, 40, 10), "2": (40, 40, 10)}, [["1"], ["2"]])
    job_path = _job(tmp_path, source, plate_mode="separate")
    monkeypatch.setattr("print_handy.core.orca_adapter.slice_model", _fake_slice())
    model_job.slice_job(job_path)
    plates = json.loads(job_path.read_text())["derived_plates"]
    assert [(row["plate_id"], row["provenance"]["selected_plates"]) for row in plates] == [
        (1, ["1"]), (2, ["2"])]


# --- performance: the check reuses the slice's paint decode -------------------------------

def test_the_integrity_check_adds_no_paint_decode_to_a_remapped_slice(tmp_path):
    source = tmp_path / "prusa.3mf"
    source.write_bytes(PRUSA_CASE.read_bytes())
    original = painted_geometry.decode_states

    def decodes(check):
        calls = []

        def counted(code):
            calls.append(code)
            return original(code)
        with patch.object(painted_geometry, "decode_states", counted):
            if check:
                _slice(source, tmp_path / "with", {1: 2, 2: 1})
            else:
                with patch.object(remap_integrity, "verify_remapped_package", lambda *a, **k: []):
                    _slice(source, tmp_path / "without", {1: 2, 2: 1})
        return len(calls)
    assert decodes(check=True) == decodes(check=False)
