"""Editable destination 3MF export (#1525, tranche E).

The export strips stale slice artifacts, preserves the source SHA-256
in provenance, writes atomically, refuses sliced-only sources with a
concrete reason, and never starts a print or touches a spool (it has no
printer or inventory surface at all).
"""
import json
import zipfile
from pathlib import Path

import pytest

from print_handy.core import editable_export as ee
from print_handy.webui import editable_download

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="{NS}">
 <resources><object id="1" type="model"><mesh>
  <vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>
  <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
 </mesh></object></resources>
 <build><item objectid="1"/></build>
</model>"""


def _write(tmp_path, name, members):
    target = tmp_path / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, data in members.items():
            archive.writestr(member, data)
    return target


def _project(tmp_path, name="proj.3mf"):
    return _write(tmp_path, name, {
        "3D/3dmodel.model": MODEL,
        "Metadata/project_settings.config": json.dumps(
            {"filament_colour": ["#FF0000"], "filament_type": ["PLA"]}),
        "Metadata/plate_1.gcode": "G1 X1 Y1 ; stale toolpath",
        "Metadata/plate_1.json": "{}",
        "Metadata/plate_1.gcode.md5": "deadbeef",
        "Metadata/slice_info.config": "<config/>"})


def test_export_strips_stale_artifacts_and_preserves_source_sha(tmp_path):
    source = _project(tmp_path)
    target = tmp_path / "out.3mf"
    report = ee.export_editable(source, target, target_printer="test-printer")
    assert target.is_file()
    assert set(report["stripped_members"]) == {
        "Metadata/plate_1.gcode", "Metadata/plate_1.json",
        "Metadata/plate_1.gcode.md5", "Metadata/slice_info.config"}
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "Metadata/plate_1.gcode" not in names
        provenance = json.loads(archive.read(ee.PROVENANCE_MEMBER))
    assert provenance["source_sha256"] == report["source_sha256"]
    assert provenance["target_printer"] == "test-printer"
    # The source is untouched, byte for byte.
    assert ee._sha256(source) == report["source_sha256"]


def test_export_refuses_sliced_only_with_a_concrete_reason(tmp_path):
    source = _write(tmp_path, "plate.gcode.3mf",
                    {"Metadata/plate_1.gcode": "G1 X1\n"})
    with pytest.raises(ee.ExportRefused, match="sliced-only"):
        ee.export_editable(source, tmp_path / "out.3mf")
    assert not (tmp_path / "out.3mf").exists()


def test_export_refuses_corrupt_source(tmp_path):
    source = tmp_path / "bad.3mf"
    source.write_bytes(b"junk")
    with pytest.raises(ee.ExportRefused):
        ee.export_editable(source, tmp_path / "out.3mf")


def test_failed_export_leaves_no_partial_file(tmp_path):
    source = _project(tmp_path)
    target = tmp_path / "out.3mf"
    target.write_bytes(b"previous")
    with pytest.raises(ee.ExportRefused):
        ee.export_editable(source, target, mapping={1: 1, 2: 1})
    assert target.read_bytes() == b"previous"


def test_webui_export_resolves_the_jobs_own_source(tmp_path):
    archive = tmp_path / "archive"
    job = archive / "job1"
    job.mkdir(parents=True)
    source = _project(job, "model.3mf")
    (job / "JOB.json").write_text(json.dumps({"source_file": str(source)}))
    path, report = editable_download.export_for_job(archive, "job1")
    assert path is not None and path.is_file()
    assert report["source_kind"] in ("orca", "generic-core", "bambu")
    path2, reason = editable_download.export_for_job(archive, "missing")
    assert path2 is None and reason


def test_webui_export_surfaces_the_sliced_only_refusal(tmp_path):
    archive = tmp_path / "archive"
    job = archive / "job2"
    job.mkdir(parents=True)
    source = _write(job, "plate.gcode.3mf", {"Metadata/plate_1.gcode": "G1 X1\n"})
    (job / "JOB.json").write_text(json.dumps({"source_file": str(source)}))
    path, reason = editable_download.export_for_job(archive, "job2")
    assert path is None
    assert "sliced-only" in reason
