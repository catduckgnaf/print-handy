import io
import json
import hashlib
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
import struct
import subprocess

import pytest
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.printers.flashforge.zmod import stock
from print_handy.core import _external, launch_state, model_job

from .helpers import external

CTL = settings.ROOT / "bin" / "moonrakerctl"
CLI = [sys.executable, "-m", "print_handy.core.model_job"]
CLI_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(settings.ROOT), os.environ.get("PYTHONPATH", "")]))}


def mock_upload_run(cmd, *args, **kwargs):
    if len(cmd) >= 4 and cmd[1] == "upload":
        remote = cmd[3]
        return mock.Mock(returncode=0, stdout=json.dumps({"result": {"item": {"root": "gcodes", "path": remote}}}), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


class AuthorSettingsTests(unittest.TestCase):
    def test_plain_stl_has_no_implicit_brim_or_supports(self):
        with tempfile.TemporaryDirectory() as td:
            stl = Path(td) / "plain.stl"
            stl.write_text("solid x\nendsolid x\n")
            self.assertEqual(model_job.get_author_settings(stl), {})

    def test_extracts_author_brim_and_tree_support(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "author.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/project_settings.config", json.dumps({
                    "brim_width": 4,
                    "enable_support": 1,
                    "support_type": "tree(auto)",
                }))
            self.assertEqual(model_job.get_author_settings(path), {
                "brim_width": "4",
                "support_material": "1",
                "support_material_style": "organic",
            })

    def test_malformed_author_settings_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "broken.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/project_settings.config", "{broken")
            with self.assertRaisesRegex(RuntimeError, "author settings"):
                model_job.get_author_settings(path)


class GcodeBoundsTests(unittest.TestCase):
    def test_bounds_1m_lines_under_performance_budget(self):
        text = "G90\n" + "G1 X10 Y20 Z0.2 E1.5\n; extrusion payload metadata\n" * 333333
        started = time.perf_counter()
        result = model_job.gcode_motion_bounds(text)
        elapsed = time.perf_counter() - started
        self.assertEqual(result, {"x": (10.0, 10.0), "y": (20.0, 20.0), "z": (0.2, 0.2)})
        # Gross-regression guard only: this host parses ~70k lines/s (baseline 16 s).
        # The launch-latency fix is cached_motion_bounds, asserted separately.
        self.assertLess(elapsed, 30.0, f"1M-line G-code bounds took {elapsed:.2f}s")

    def test_cached_bounds_reuse_identical_bytes_and_never_cache_failures(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as td, mock.patch.object(model_job, "BOUNDS_CACHE", Path(td)):
            raw = b"G90\nG1 X10 Y20 Z0.2\nG1 X30 Y40 Z0.4\n"
            calls = []
            real = model_job.gcode_motion_bounds
            with mock.patch.object(model_job, "gcode_motion_bounds", side_effect=lambda t: calls.append(1) or real(t)):
                first = model_job.cached_motion_bounds(raw)
                second = model_job.cached_motion_bounds(raw)
                self.assertEqual(first, second)
                self.assertEqual(first, {"x": (10.0, 30.0), "y": (20.0, 40.0), "z": (0.2, 0.4)})
                self.assertEqual(len(calls), 1)
                bad = b"G91\nG1 X1\n"
                for _ in range(2):
                    with self.assertRaises(RuntimeError):
                        model_job.cached_motion_bounds(bad)
                self.assertEqual(len(calls), 3)

    def test_bounds_match_reference_on_real_gcode_samples(self):
        import random
        import re

        def reference(text):
            absolute=True
            position={axis:float("nan") for axis in "XYZ"}
            seen={axis:[] for axis in "XYZ"}
            recognized=0
            for lineno,raw in enumerate(text.splitlines(),1):
                line=raw.split(";",1)[0].strip().upper()
                if not line: continue
                command=line.split()[0]
                if command=="G90": absolute=True; continue
                if command=="G91": absolute=False; continue
                if command in ("G28","G53","FORCE_MOVE","MANUAL_STEPPER","STEPPER_BUZZ","SET_KINEMATIC_POSITION"):
                    raise RuntimeError(f"line {lineno}: unsupported motion-affecting command {command}")
                words={axis:float(value) for axis,value in re.findall(r'([A-Z])\s*('+model_job.NUMBER+r')',line)}
                if command=="G92" and any(axis in words for axis in "XYZ"):
                    raise RuntimeError(f"line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous")
                if command in ("G2","G02","G3","G03"):
                    raise RuntimeError(f"line {lineno}: arc motion is unsupported by the bounds verifier")
                if command not in ("G0","G00","G1","G01"): continue
                xyz={axis:value for axis,value in words.items() if axis in "XYZ"}
                if not xyz: continue
                recognized+=1
                for axis,value in xyz.items():
                    if absolute: position[axis]=value
                    else:
                        if position[axis] != position[axis]:
                            raise RuntimeError(f"line {lineno}: relative {axis} motion before an absolute {axis} position")
                        position[axis]+=value
                    seen[axis].append(position[axis])
            if not recognized: raise RuntimeError("no bounded XYZ motion found")
            missing=[axis for axis in "XYZ" if not seen[axis]]
            if missing: raise RuntimeError("missing bounded motion for axis " + ", ".join(missing))
            return {axis.lower():(min(seen[axis]),max(seen[axis])) for axis in "XYZ"}

        import tempfile
        fixtures = [
            "G90\nG1 X0 Y0 Z0\nG1 X10 Y4 Z1\nG1 X-2 Y8 Z2\n",
            "G90\nG1 X1 Y2 Z3\nG91\nG1 X4 Y-1 Z2\nG1 X-2 Y3 Z-1\n",
            "G90\nG1 X-20 Y5 Z0\nG1 X30 Y-5 Z12\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, content in enumerate(fixtures):
                path = Path(directory) / f"reference-{index}.gcode"
                path.write_text(content)
                paths.append(path)
            random.Random(0).shuffle(paths)
            for path in paths:
                text = path.read_text(errors="replace")
                def outcome(fn):
                    try: return ("ok", fn(text))
                    except RuntimeError as exc: return (type(exc), str(exc))
                with self.subTest(path=str(path)):
                    self.assertEqual(outcome(reference), outcome(model_job.gcode_motion_bounds))

    def test_absolute_and_relative_xyz_bounds(self):
        text = """G90
G1 X10 Y20 Z0.2
G91
G1 X5 Y-3 Z0.1
G90
G1 X2 Y4 Z1e0
"""
        self.assertEqual(model_job.gcode_motion_bounds(text), {
            "x": (2.0, 15.0), "y": (4.0, 20.0), "z": (0.2, 1.0)
        })

    def test_xyz_g92_is_refused_as_ambiguous(self):
        with self.assertRaisesRegex(RuntimeError, "G92.*XYZ"):
            model_job.gcode_motion_bounds("G90\nG1 X100 Y100 Z1\nG92 X0 Y0\nG1 X5 Y6\n")

    def test_arc_motion_is_refused_as_unsupported(self):
        with self.assertRaisesRegex(RuntimeError, "arc motion"):
            model_job.gcode_motion_bounds("G90\nG1 X10 Y10 Z1\nG2 X20 Y20 I5 J0\n")

    def test_relative_motion_without_known_position_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "relative X motion"):
            model_job.gcode_motion_bounds("G91\nG1 X5\n")

    def test_no_xyz_motion_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "no bounded XYZ motion"):
            model_job.gcode_motion_bounds("G90\nG1 E10\n")

    def test_homing_is_refused_as_unmodeled_motion(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported motion-affecting command G28"):
            model_job.gcode_motion_bounds("G28\nG90\nG1 X10 Y10 Z1\n")

    def test_parameter_parser_ignores_extrusion_words(self):
        self.assertEqual(model_job.gcode_motion_bounds("G90\nG1 X10 Y10 Z0.2 E1.5\n"), {
            "x": (10.0, 10.0), "y": (10.0, 10.0), "z": (0.2, 0.2)
        })

    def state(self):
        return {
            "nozzle": {"diameter_mm": 0.4, "type": "brass"},
            "spool": {"material": "PLA", "spoolman_id": 1, "name": "Black"},
            "temps_c": {"nozzle": 210, "bed": 60},
            "z_offset_mm": 1.5,
            "axis_limits_mm": {"x": [0, 223], "y": [0, 223], "z": [0, 250]},
        }

    def valid_gcode(self, motion="G1 X10 Y10 Z0.2"):
        return f"""PRINT_START BED=60 HOTEND=210
G90
{motion}
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
;WIDTH:0.45
;LAYER_CHANGE
;WIDTH:0.45
PRINT_END
; nozzle_diameter = 0.4
; filament_type = PLA
; first_layer_height = 0.2
"""

    def verify(self, gcode):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "job.gcode"
            path.write_text(gcode)
            with mock.patch.object(model_job.printer_state, "load", return_value=self.state()), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job, "_mr", return_value={"result": {"status": {"save_variables": {"variables": {"display_off": 1}}}}}):
                return model_job.verify_gcode(path)

    def test_verify_refuses_out_of_range_xy_motion(self):
        with self.assertRaisesRegex(RuntimeError, "X bounds"):
            self.verify(self.valid_gcode("G1 X224 Y10 Z0.2"))

    def test_verify_refuses_out_of_range_z_motion(self):
        with self.assertRaisesRegex(RuntimeError, "Z bounds"):
            self.verify(self.valid_gcode("G1 X10 Y10 Z251"))

    def test_verify_refuses_missing_width_metadata(self):
        with self.assertRaisesRegex(RuntimeError, "missing first layer WIDTH"):
            self.verify(self.valid_gcode().replace(";WIDTH:0.45\n", "", 1))

    def test_verify_refuses_missing_safe_end(self):
        with self.assertRaisesRegex(RuntimeError, "missing END_PRINT/PRINT_END"):
            self.verify(self.valid_gcode().replace("PRINT_END\n", ""))

    def test_verify_refuses_invalid_first_layer_height(self):
        with self.assertRaisesRegex(RuntimeError, "first_layer_height"):
            self.verify(self.valid_gcode().replace("first_layer_height = 0.2", "first_layer_height = 0"))

    def test_verify_refuses_malformed_nozzle_metadata(self):
        with self.assertRaisesRegex(RuntimeError, "invalid nozzle_diameter"):
            self.verify(self.valid_gcode().replace("nozzle_diameter = 0.4", "nozzle_diameter = nope"))

    def test_verify_accepts_complete_safe_gcode(self):
        self.assertTrue(self.verify(self.valid_gcode()))


class SliceVerificationTests(unittest.TestCase):
    def test_slice_job_runs_full_verifier_before_recording(self):
        state = {
            "nozzle": {"diameter_mm": 0.4, "type": "brass"},
            "spool": {"material": "PLA", "spoolman_id": 1, "name": "Black", "density": 1.24},
            "temps_c": {"nozzle": 210, "bed": 60},
            "z_offset_mm": 1.5,
            "bed_mm": [220, 220],
            "max_print_height_mm": 250,
        }
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "part.stl"
            source.write_text("solid x\nendsolid x\n")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({
                "downloaded_file": str(source),
                "audit": {"size_x": "10", "size_y": "10", "size_z": "10"},
                "title": "Part",
            }))
            def write_slicer_output(cmd, **_kwargs):
                output = Path(cmd[cmd.index("--outputdir") + 1]) / 'plate_1.gcode'
                output.write_text("; generated by OrcaSlicer 2.4.2\nmock output")
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(model_job, "ARCHIVE", work), \
                 mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run", side_effect=write_slicer_output), \
                 mock.patch.object(model_job, "verify_gcode", side_effect=RuntimeError("bad generated file")) as verify:
                with self.assertRaisesRegex(RuntimeError, "bad generated file"):
                    model_job.slice_job(job_path)
                verify.assert_called_once()
            saved = json.loads(job_path.read_text())
            self.assertNotIn("gcode_verified", saved)

    def test_slice_job_on_a_single_slot_printer_keeps_single_color_decisions(self):
        # #268: the Discord intake writes single-color decisions for every printer, but a
        # single-slot printer slices as "generic" with decisions=None. The post-slice check
        # compares the file's decisions with the digest taken before slicing, so the two
        # must hash the same thing or every Discord slice on such a printer is refused.
        state = {
            "nozzle": {"diameter_mm": 0.4, "type": "brass"},
            "spool": {"material": "PLA", "spoolman_id": 1, "name": "Black", "density": 1.24},
            "temps_c": {"nozzle": 210, "bed": 60},
            "z_offset_mm": 1.5,
            "bed_mm": [220, 220],
            "max_print_height_mm": 250,
            "filament_slots": {},
        }
        decisions = {"mode": "single_color", "schema": "part_slots_v1", "source_sha256": "0" * 64,
                     "selection": {"slot": 1, "color": "Black", "material": "PLA", "spoolman_id": 1}}
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "part.stl"
            source.write_text("solid x\nendsolid x\n")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({
                "downloaded_file": str(source),
                "audit": {"size_x": "10", "size_y": "10", "size_z": "10"},
                "title": "Part",
                "printer_model": "klipper.generic",
                "color_decisions": decisions,
                "filament_selection": decisions["selection"],
            }))
            def write_slicer_output(cmd, **_kwargs):
                output = Path(cmd[cmd.index("--outputdir") + 1]) / 'plate_1.gcode'
                output.write_text("; generated by OrcaSlicer 2.4.2\nmock output")
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(model_job, "ARCHIVE", work),                  mock.patch.object(model_job.printer_state, "load", return_value=state),                  mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})),                  mock.patch.object(model_job.subprocess, "run", side_effect=write_slicer_output),                  mock.patch.object(model_job, "verify_gcode", return_value=True):
                out = model_job.slice_job(job_path)
            self.assertTrue(str(out).endswith("plate_1.gcode"))
            saved = json.loads(job_path.read_text())
            self.assertEqual(saved["color_decisions"], decisions)
            self.assertEqual(saved["artifact_binding"]["decisions_sha256"], model_job.decisions_digest(decisions))

    def test_slice_job_forwards_requested_copy_count_to_orca(self):
        state = {
            "nozzle": {"diameter_mm": 0.4, "type": "brass"},
            "spool": {"material": "PLA", "spoolman_id": 1, "name": "Black", "density": 1.24},
            "temps_c": {"nozzle": 210, "bed": 60},
            "z_offset_mm": 1.5,
            "bed_mm": [220, 220],
            "max_print_height_mm": 250,
        }
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "part.stl"
            source.write_text("solid x\nendsolid x\n")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({
                "downloaded_file": str(source),
                "audit": {"size_x": "10", "size_y": "10", "size_z": "10"},
                "title": "Part",
            }))
            gcode = work / "plate_1.gcode"
            gcode.write_text("; generated by OrcaSlicer 2.4.2\nmock output")
            provenance = {"source_sha256": model_job.file_sha256(source)}
            fake_adapter = mock.Mock()
            fake_adapter.slice_model.return_value = (gcode, provenance, [])
            with mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job, "verify_gcode"), \
                 mock.patch.dict(sys.modules, {"print_handy.core.orca_adapter": fake_adapter}):
                model_job.slice_job(job_path, copies=3)
            self.assertEqual(fake_adapter.slice_model.call_args.kwargs["copies"], 3)
            self.assertEqual(json.loads(job_path.read_text())["copies"], 3)




def _box_stl(path, width, depth, height):
    vertices = [(0, 0, 0), (width, 0, 0), (width, depth, 0), (0, depth, 0),
                (0, 0, height), (width, 0, height), (width, depth, height), (0, depth, height)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6),
             (0, 4, 5), (0, 5, 1), (1, 5, 6), (1, 6, 2),
             (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    path.write_text('solid box\n' + ''.join(
        'facet normal 0 0 0\nouter loop\n' + ''.join(
            f'vertex {vertices[index][0]} {vertices[index][1]} {vertices[index][2]}\n' for index in face)
        + 'endloop\nendfacet\n' for face in faces) + 'endsolid box\n')


def _slice_state():
    return {'bed_mm': [220, 220], 'max_print_height_mm': 250,
            'nozzle': {'diameter_mm': 0.4},
            'spool': {'name': 'PLA', 'material': 'PLA', 'density': 1.24},
            'temps_c': {'nozzle': 210, 'bed': 60}}


def _unit_decision(source, units, dims):
    return model_job.make_unit_decision(units, model_job.file_sha256(source), dims)


def _fake_adapter_slice(tmp_path, monkeypatch, source):
    fake_adapter = mock.Mock()
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text('; generated by OrcaSlicer 2.4.2\n')
    fake_adapter.slice_model.return_value = (gcode, {'source_sha256': model_job.file_sha256(source)}, [])
    monkeypatch.setattr(model_job.printer_state, 'load', _slice_state)
    monkeypatch.setattr(model_job, 'verify_gcode', lambda *_args: None)
    return fake_adapter


def test_inch_stl_auto_scales_exported_geometry_and_records_job(tmp_path, monkeypatch):
    source = tmp_path / 'inch.stl'
    _box_stl(source, 2, 3, 4)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source), 'audit': {'size_x': '2', 'size_y': '3', 'size_z': '4'}, 'title': 'Inch part',
                                    'unit_decision': _unit_decision(source, 'inch', [2, 3, 4])}))
    observed = {}

    def fake_slice(cmd, **_kwargs):
        mesh = Path(cmd[-1]).read_bytes()
        triangles = struct.unpack('<I', mesh[80:84])[0]
        assert len(mesh) == 84 + triangles * 50
        coords = [struct.unpack('<3f', mesh[84 + triangle * 50 + vertex:84 + triangle * 50 + vertex + 12])
                  for triangle in range(triangles) for vertex in (12, 24, 36)]
        observed['x_values'] = sorted({round(v[0], 3) for v in coords})
        output = Path(cmd[cmd.index('--outputdir') + 1]) / 'plate_1.gcode'
        output.write_text('; generated by OrcaSlicer 2.4.2\nG90\n')
        return mock.Mock(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(model_job.printer_state, 'load', _slice_state)
    monkeypatch.setattr(model_job, 'verify_gcode', lambda *_args: None)
    monkeypatch.setattr(model_job.subprocess, 'run', fake_slice)
    model_job.slice_job(manifest, copies=2)
    saved = json.loads(manifest.read_text())
    assert saved['copies'] == 2
    assert saved['auto_scale'] == {'factor': 25.4, 'reason': 'inch units',
                                   'original_mm': [2.0, 3.0, 4.0], 'final_mm': [50.8, 76.2, 101.6]}
    assert saved['unit_provenance'] == {'units': 'inch', 'source': 'user_confirmation',
                                        'original_mm': [2.0, 3.0, 4.0], 'factor': 25.4}
    x_values = observed['x_values']
    assert 50.8 in {round(x_values[i + 1] - x_values[i], 3) for i in range(len(x_values) - 1)}


def test_explicit_scale_overrides_inch_auto_scale(tmp_path, monkeypatch):
    source = tmp_path / 'inch.stl'
    _box_stl(source, 2, 3, 4)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source), 'scale': 2,
                                    'audit': {'size_x': '2', 'size_y': '3', 'size_z': '4'}, 'title': 'Explicit'}))
    fake_adapter = mock.Mock()
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text('; generated by OrcaSlicer 2.4.2\n')
    fake_adapter.slice_model.return_value = (gcode, {'source_sha256': model_job.file_sha256(source)}, [])
    monkeypatch.setattr(model_job.printer_state, 'load', _slice_state)
    monkeypatch.setattr(model_job, 'verify_gcode', lambda *_args: None)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        model_job.slice_job(manifest)
    assert fake_adapter.slice_model.call_args.kwargs['scale'] == 2.0
    assert 'auto_scale' not in json.loads(manifest.read_text())


def test_inch_stl_that_would_not_fit_fails_with_measured_size(tmp_path, monkeypatch):
    source = tmp_path / 'too_big.stl'
    _box_stl(source, 9, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source),
                                    'audit': {'size_x': '9', 'size_y': '2', 'size_z': '2'}, 'title': 'Too big',
                                    'unit_decision': _unit_decision(source, 'inch', [9, 2, 2])}))
    monkeypatch.setattr(model_job.printer_state, 'load', _slice_state)
    with pytest.raises(RuntimeError, match=r'model measures 9x2x2 mm.*228.6x50.8x50.8 mm'):
        model_job.slice_job(manifest)


def test_small_stl_without_unit_confirmation_is_not_sliced(tmp_path, monkeypatch):
    source = tmp_path / 'pin.stl'
    _box_stl(source, 5, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source),
                                    'audit': {'size_x': '5', 'size_y': '2', 'size_z': '2'}, 'title': 'Pin'}))
    fake_adapter = _fake_adapter_slice(tmp_path, monkeypatch, source)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        with pytest.raises(RuntimeError, match=r'do not record units.*confirm'):
            model_job.slice_job(manifest)
    fake_adapter.slice_model.assert_not_called()
    assert 'auto_scale' not in json.loads(manifest.read_text())


def test_5mm_pin_stays_5mm_after_mm_confirmation(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'pin.stl'
    _box_stl(source, 5, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source),
                                    'audit': {'size_x': '5', 'size_y': '2', 'size_z': '2'}, 'title': 'Pin'}))
    # The intake gate asks first: noninteractive callers get the packet, nothing is recorded.
    assert model_job.intake_unit_confirmation(manifest) is False
    packet = json.loads(capsys.readouterr().out)
    assert packet['status'] == 'UNIT_CONFIRMATION_REQUIRED'
    assert packet['choices'][0].startswith('Millimeters')
    assert 'unit_decision' not in json.loads(manifest.read_text())
    # Confirming millimeters records source-bound provenance on the same job.
    assert model_job.intake_unit_confirmation(manifest, units='mm') is True
    saved = json.loads(manifest.read_text())
    assert saved['unit_decision']['units'] == 'mm'
    assert saved['unit_decision']['source_sha256'] == model_job.file_sha256(source)
    assert saved['unit_provenance']['source'] == 'user_confirmation'
    fake_adapter = _fake_adapter_slice(tmp_path, monkeypatch, source)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        model_job.slice_job(manifest)
    assert fake_adapter.slice_model.call_args.kwargs.get('scale') is None
    saved = json.loads(manifest.read_text())
    assert 'auto_scale' not in saved
    assert saved['unit_provenance'] == {'units': 'mm', 'source': 'user_confirmation',
                                        'original_mm': [5.0, 2.0, 2.0], 'factor': 1.0}


def test_unit_confirmation_for_another_source_is_refused(tmp_path, monkeypatch):
    source = tmp_path / 'pin.stl'
    _box_stl(source, 5, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source),
                                    'audit': {'size_x': '5', 'size_y': '2', 'size_z': '2'}, 'title': 'Pin',
                                    'unit_decision': {'source_sha256': 'some-other-file', 'units': 'mm',
                                                      'original_mm': [5.0, 2.0, 2.0],
                                                      'source': 'user_confirmation'}}))
    fake_adapter = _fake_adapter_slice(tmp_path, monkeypatch, source)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        with pytest.raises(RuntimeError, match=r'do not record units'):
            model_job.slice_job(manifest)
    fake_adapter.slice_model.assert_not_called()


def test_explicit_scale_1_is_never_auto_scaled(tmp_path, monkeypatch):
    source = tmp_path / 'pin.stl'
    _box_stl(source, 5, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source), 'scale': 1.0,
                                    'audit': {'size_x': '5', 'size_y': '2', 'size_z': '2'}, 'title': 'Pin'}))
    assert model_job.unit_question(json.loads(manifest.read_text())) is None
    fake_adapter = _fake_adapter_slice(tmp_path, monkeypatch, source)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        model_job.slice_job(manifest)
    assert fake_adapter.slice_model.call_args.kwargs['scale'] == 1.0
    saved = json.loads(manifest.read_text())
    assert 'auto_scale' not in saved
    assert saved['unit_provenance']['source'] == 'explicit_scale'


def test_generated_mm_part_is_never_auto_scaled_or_asked(tmp_path, monkeypatch):
    source = tmp_path / 'generated.stl'
    _box_stl(source, 5, 2, 2)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source), 'generated': True,
                                    'audit': {'size_x': '5', 'size_y': '2', 'size_z': '2'}, 'title': 'Generated'}))
    assert model_job.unit_question(json.loads(manifest.read_text())) is None
    fake_adapter = _fake_adapter_slice(tmp_path, monkeypatch, source)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        model_job.slice_job(manifest)
    assert fake_adapter.slice_model.call_args.kwargs.get('scale') is None
    saved = json.loads(manifest.read_text())
    assert 'auto_scale' not in saved
    assert saved['unit_provenance'] == {'units': 'mm', 'source': 'generated',
                                        'original_mm': [5.0, 2.0, 2.0], 'factor': 1.0}


def test_normal_size_stl_is_not_auto_scaled(tmp_path, monkeypatch):
    source = tmp_path / 'normal.stl'
    _box_stl(source, 20, 30, 40)
    manifest = tmp_path / 'JOB.json'
    manifest.write_text(json.dumps({'downloaded_file': str(source),
                                    'audit': {'size_x': '20', 'size_y': '30', 'size_z': '40'}, 'title': 'Normal'}))
    fake_adapter = mock.Mock()
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text('; generated by OrcaSlicer 2.4.2\n')
    fake_adapter.slice_model.return_value = (gcode, {'source_sha256': model_job.file_sha256(source)}, [])
    monkeypatch.setattr(model_job.printer_state, 'load', _slice_state)
    monkeypatch.setattr(model_job, 'verify_gcode', lambda *_args: None)
    with mock.patch.dict(sys.modules, {'print_handy.core.orca_adapter': fake_adapter}):
        model_job.slice_job(manifest)
    assert fake_adapter.slice_model.call_args.kwargs.get('scale') is None
    saved = json.loads(manifest.read_text())
    assert 'auto_scale' not in saved
    assert saved['unit_provenance'] == {'units': 'mm', 'source': 'authored_size',
                                        'original_mm': [20.0, 30.0, 40.0], 'factor': 1.0}


class PlateManifestTests(unittest.TestCase):
    def test_manifest_binds_named_plates_to_exact_source_hash(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "project.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/plate_1.png", b"one")
                archive.writestr("Metadata/plate_2.png", b"two")
                archive.writestr("Metadata/model_settings.config", ('<config>'
                    '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="left"/>'
                    '<model_instance><metadata key="object_id" value="8"/></model_instance></plate>'
                    '<plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value="right"/>'
                    '<model_instance><metadata key="object_id" value="9"/></model_instance></plate></config>'))
            manifest = model_job.plate_manifest(source)
            self.assertEqual([(p["id"], p["name"], p["object_ids"]) for p in manifest["plates"]],
                             [(1, "left", [8]), (2, "right", [9])])
            self.assertEqual(manifest["source_sha256"], model_job.file_sha256(source))
            self.assertEqual([p["status"] for p in manifest["plates"]], ["planned", "planned"])

    def test_manifest_refuses_thumbnails_without_assignments(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "project.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/plate_1.png", b"one")
                archive.writestr("Metadata/plate_2.png", b"two")
            with self.assertRaisesRegex(RuntimeError, "assignments"):
                model_job.plate_manifest(source)

    def test_manifest_rejects_duplicate_plate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "project.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/model_settings.config", ('<config>'
                    '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="8"/></model_instance></plate>'
                    '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="9"/></model_instance></plate></config>'))
            with self.assertRaisesRegex(RuntimeError, "duplicated"):
                model_job.plate_manifest(source)

    def test_manifest_cli_prints_inventory_without_slicing(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "project.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/model_settings.config", ('<config>'
                    '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="left"/>'
                    '<model_instance><metadata key="object_id" value="8"/></model_instance></plate></config>'))
            result = model_job.subprocess.run([*CLI, "plate-manifest", str(source)],
                                              capture_output=True, text=True, timeout=15, env=CLI_ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["plates"][0]["name"], "left")

class MultipartSafetyTests(unittest.TestCase):
    def test_multiplate_archive_requires_explicit_plate_workflow(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "multi.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/plate_1.png", b"png")
                archive.writestr("Metadata/plate_2.png", b"png")
                archive.writestr("Metadata/plate_3.png", b"png")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({"downloaded_file": str(source), "audit": {
                "size_x": "100", "size_y": "100", "size_z": "10",
                "plates": 3, "build_items": 5}, "title": "Five parts"}))
            state = {"bed_mm": [220, 220], "max_print_height_mm": 250,
                     "nozzle": {"diameter_mm": 0.4}, "spool": {"name": "PLA", "material": "PLA", "density": 1.24},
                     "temps_c": {"nozzle": 210, "bed": 60}}
            with mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "multi.*plate|multipart"):
                    model_job.slice_job(job_path)
                run.assert_not_called()

    def test_multiplate_archive_cannot_bypass_guard_with_stale_audit(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "multi.3mf"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Metadata/plate_1.png", b"png")
                archive.writestr("Metadata/plate_2.png", b"png")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({"downloaded_file": str(source), "audit": {
                "size_x": "10", "size_y": "10", "size_z": "10", "plates": 1}, "title": "Stale audit"}))
            state = {"bed_mm": [220, 220], "max_print_height_mm": 250,
                     "nozzle": {"diameter_mm": 0.4}, "spool": {"name": "PLA", "material": "PLA", "density": 1.24},
                     "temps_c": {"nozzle": 210, "bed": 60}}
            with mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "multi-plate"):
                    model_job.slice_job(job_path)
                run.assert_not_called()

    def test_multiplate_archive_cannot_bypass_guard_without_plate_thumbnails(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "multi.3mf"
            model_xml = ('<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                         '<resources/><build><item objectid="1"/><item objectid="2"/></build></model>')
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("3D/3dmodel.model", model_xml)
                archive.writestr("Metadata/model_settings.config", '<config><plate><metadata key="plater_id" value="1"/></plate><plate><metadata key="plater_id" value="2"/></plate></config>')
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({"downloaded_file": str(source), "audit": {
                "size_x": "10", "size_y": "10", "size_z": "10"}, "title": "Unpictured plates"}))
            state = {"bed_mm": [220, 220], "max_print_height_mm": 250,
                     "nozzle": {"diameter_mm": 0.4}, "spool": {"name": "PLA", "material": "PLA", "density": 1.24},
                     "temps_c": {"nozzle": 210, "bed": 60}}
            with mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "multi-plate"):
                    model_job.slice_job(job_path)
                run.assert_not_called()

    def test_slicer_success_without_output_reports_missing_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            source = work / "part.stl"
            source.write_text("solid x\\nendsolid x\\n")
            job_path = work / "JOB.json"
            job_path.write_text(json.dumps({"downloaded_file": str(source), "audit": {
                "size_x": "10", "size_y": "10", "size_z": "10"}, "title": "Part"}))
            state = {"bed_mm": [220, 220], "max_print_height_mm": 250,
                     "nozzle": {"diameter_mm": 0.4}, "spool": {"name": "PLA", "material": "PLA", "density": 1.24},
                     "temps_c": {"nozzle": 210, "bed": 60}}
            with mock.patch.object(model_job.printer_state, "load", return_value=state), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
                with self.assertRaisesRegex(RuntimeError, "no G-code output"):
                    model_job.slice_job(job_path)
            self.assertNotIn("gcode_verified", json.loads(job_path.read_text()))

class MeshTopologyAuditTests(unittest.TestCase):
    def test_closed_tetrahedron_is_watertight_manifold(self):
        tris = [[(0,0,0),(1,0,0),(0,1,0)], [(0,0,0),(0,1,0),(0,0,1)],
                [(0,0,0),(0,0,1),(1,0,0)], [(1,0,0),(0,0,1),(0,1,0)]]
        result = model_job.mesh_topology(tris)
        self.assertEqual(result["triangle_count"], 4)
        self.assertEqual(result["connected_shells"], 1)
        self.assertTrue(result["watertight"])
        self.assertTrue(result["manifold"])

    def test_open_and_disconnected_meshes_are_reported(self):
        tri = [(0,0,0),(1,0,0),(0,1,0)]
        result = model_job.mesh_topology([tri, [(3,0,0),(4,0,0),(3,1,0)]])
        self.assertEqual(result["connected_shells"], 2)
        self.assertEqual(result["boundary_edges"], 6)
        self.assertFalse(result["watertight"])
        self.assertFalse(result["manifold"])

    def test_prepared_job_audit_persists_versioned_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            path = folder / "part.stl"
            tri = [(0,0,0),(2,0,0),(0,3,4)]
            with path.open("wb") as f:
                f.write(b"part".ljust(80,b" ") + struct.pack("<I",1))
                f.write(struct.pack("<12fH",0,0,0,*(v for point in tri for v in point),0))
            job = {"printer_model": "ad5x", "downloaded_file": str(path), "audit": model_job.audit(path, printer_model="ad5x")}
            model_job.write_job(folder, job)
            stored = json.loads((folder / "JOB.json").read_text())["audit"]["mesh_audit"]
            self.assertEqual(stored["schema_version"], 1)
            self.assertEqual(stored["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(stored["dimensions_mm"], [2.0, 3.0, 4.0])
            self.assertEqual(stored["units"], "mm")
            self.assertEqual(stored["printer_model"], "ad5x")
            self.assertEqual(stored["triangle_count"], 1)

    def test_vertex_only_touching_tetrahedra_is_singular(self):
        tetra=[[(0,0,0),(1,0,0),(0,1,0)],[(0,0,0),(0,1,0),(0,0,1)],[(0,0,0),(0,0,1),(1,0,0)],[(1,0,0),(0,0,1),(0,1,0)]]
        second=[[(0,0,0),(-1,0,0),(0,-1,0)],[(0,0,0),(0,-1,0),(0,0,-1)],[(0,0,0),(0,0,-1),(-1,0,0)],[(-1,0,0),(0,0,-1),(0,-1,0)]]
        r=model_job.mesh_topology(tetra+second)
        self.assertEqual(r["boundary_edges"],0); self.assertEqual(r["non_manifold_edges"],0)
        self.assertGreaterEqual(r["singular_vertices"],1); self.assertTrue(r["watertight"]); self.assertFalse(r["manifold"])

    def test_collinear_distinct_triangle_is_degenerate(self):
        r=model_job.mesh_topology([[(0,0,0),(1,1,1),(2,2,2)]])
        self.assertEqual(r["degenerate_triangles"],1); self.assertFalse(r["manifold"])

    def test_dense_edge_fan_is_bounded(self):
        # A child timeout makes quadratic regressions fail, rather than hang
        # the entire test runner. 20k faces would create ~400M pair links.
        code = (
            "import json; from print_handy.core.model_job import mesh_topology; "
            "print(json.dumps(mesh_topology([[(0,0,0),(1,0,0),(0,i+1,1)] "
            "for i in range(20000)])))"
        )
        proc = subprocess.run([sys.executable, "-c", code], env=CLI_ENV,
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["non_manifold_edges"], 1)
        self.assertEqual(result["connected_shells"], 1)
        self.assertFalse(result["manifold"])

    def test_near_collinear_area_threshold_and_exclusion(self):
        result = model_job.mesh_topology([
            [(0, 0, 0), (0.000001, 0, 0), (0.000002, 0.000001, 0)],
            [(10, 0, 0), (11, 0, 0), (10, 1, 0)],
        ])
        self.assertEqual(result["triangle_count"], 2)
        self.assertEqual(result["degenerate_triangles"], 1)
        self.assertEqual(result["boundary_edges"], 3)
        self.assertEqual(result["connected_shells"], 1)

    def test_duplicate_vertex_degenerate_triangle_is_counted(self):
        result = model_job.mesh_topology([[(0,0,0),(1,0,0),(1,0,0)]])
        self.assertEqual(result["degenerate_triangles"], 1)
        self.assertFalse(result["manifold"])

    def test_cli_audit_is_json_and_does_not_change_source(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tetra.stl"
            tris = [[(0,0,0),(1,0,0),(0,1,0)], [(0,0,0),(0,1,0),(0,0,1)],
                    [(0,0,0),(0,0,1),(1,0,0)], [(1,0,0),(0,0,1),(0,1,0)]]
            with path.open("wb") as f:
                f.write(b"tetra".ljust(80,b" ") + struct.pack("<I",len(tris)))
                for tri in tris:
                    f.write(struct.pack("<12fH",0,0,0,*(v for p in tri for v in p),0))
            before = path.read_bytes()
            proc = subprocess.run(CLI + ["audit", str(path)], env=CLI_ENV, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["triangle_count"], 4)
            self.assertTrue(report["watertight"])
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["source_sha256"], hashlib.sha256(before).hexdigest())
            self.assertEqual(report["dimensions_mm"], [1.0, 1.0, 1.0])
            self.assertEqual(report["units"], "mm")
            self.assertIsNone(report["printer_model"])
            self.assertEqual(path.read_bytes(), before)


class AuditTests(unittest.TestCase):
    def test_plate_count_uses_unique_plate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plates.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/plate_1.png", b"png")
                z.writestr("Metadata/plate_1.gcode", "gcode")
                z.writestr("Metadata/plate_2.png", b"png")
                z.writestr("3D/3dmodel.model", "<model><resources><object id='1'><mesh/></object></resources><build><item objectid='1'/></build></model>")
            with mock.patch.object(model_job.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="size_x = 10\nsize_y = 10\nsize_z = 10\n", stderr="")
                result = model_job.audit(path)
            self.assertEqual(result["plates"], 2)
            self.assertEqual(result["plate_ids"], [1, 2])


    def test_plate_count_from_metadata_without_thumbnails(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "plates.3mf"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("Metadata/model_settings.config", '<config><plate><metadata key="plater_id" value="1"/></plate><plate><metadata key="plater_id" value="2"/></plate></config>')
                z.writestr("3D/3dmodel.model", "<model><resources><object id='1'><mesh/></object></resources><build><item objectid='1'/></build></model>")
            with mock.patch.object(model_job.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="size_x = 10\nsize_y = 10\nsize_z = 10\n", stderr="")
                result = model_job.audit(path)
            self.assertEqual(result["plate_ids"], [1, 2])
            self.assertEqual(result["plates"], 2)


class EnglishAndPrintNowTests(unittest.TestCase):
    def test_english_prefers_translated_title(self):
        self.assertEqual(model_job.english("Retractable Ghost", "伸缩滑溜小幽灵"), "Retractable Ghost")
        self.assertEqual(model_job.english(None, "伸缩滑溜小幽灵", "Ghost"), "Ghost")
        self.assertEqual(model_job.english(None, "伸缩"), "伸缩")

    def test_gcode_estimate_parses(self):
        self.assertEqual(model_job.gcode_estimate_s("; estimated printing time (normal mode) = 11h 56m 20s\n"), 42980)
        self.assertEqual(model_job.human_dur(42980), "11h 56m")
        self.assertIsNone(model_job.gcode_estimate_s("nothing"))
        # Bambu Studio writes the header form only (#271): printing time first, the slicer total as fallback.
        bambu = "; HEADER_BLOCK_START\n; model printing time: 14h 22m 10s; total estimated time: 14h 31m 37s\n"
        self.assertEqual(model_job.gcode_estimate_s(bambu), 51730)
        self.assertEqual(model_job.gcode_estimate_s("; total estimated time: 9m\n"), 540)
        both = bambu + "; estimated printing time (normal mode) = 11h 56m 20s\n"
        self.assertEqual(model_job.gcode_estimate_s(both), 42980)

    def setUp(self):
        # live_snapshot reads the printer; keep every print_now test offline.
        p = mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"})
        p.start(); self.addCleanup(p.stop)
        for p in (mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None, "print_leveling": 0, "problem": None, "stale": None, "z_offset_mm": -0.005, "offset_source": "saved gcode offset"}),
                  mock.patch.object(model_job, "screen_z_offset", return_value=0.025),
                  mock.patch.object(model_job, "_fetch_remote_gcode_sha256", side_effect=lambda url, name: name.rsplit(".", 1)[0].split("_", 1)[1] if "_" in name else "")):
            p.start(); self.addCleanup(p.stop)

    def _gcode(self, d, z_offset=True):
        g = Path(d) / "x.gcode"
        start = "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60" + (" Z_OFFSET=-0.25" if z_offset else "") + "\n"
        g.write_text("; estimated printing time (normal mode) = 1h 2m 0s\n; total filament used [g] = 12.3\n" + start)
        return g

    def test_print_now_refuses_busy_printer(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("printing", "other.gcode", "Printing")), \
             mock.patch.object(model_job, "verify_gcode") as v:
            with self.assertRaisesRegex(SystemExit, "busy"):
                model_job.print_now(self._gcode(d))
            v.assert_not_called()

    def test_print_now_refuses_fallback_no_second_start_request(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_zmod_version", return_value=None), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run), \
             mock.patch.object(model_job, "_mr") as mr, \
             mock.patch.object(model_job, "_wait_printing", return_value="standby"), \
             mock.patch.object(model_job, "record_start"):
            out = model_job.print_now(self._gcode(d))
            self.assertEqual(out["phase"], "awaiting_confirmation")  # never "failed" without this file saying so
        # One start POST, then the #84 status readback. Not a second start.
        # (_zmod_version pinned unknown: this choreography is the plain .gcode path's.)
        # A read-only heater baseline before the start (evidence for a lost response), exactly one start POST, then the readback.
        paths = [c.args[1] for c in mr.call_args_list]
        self.assertEqual(paths.count("/printer/print/start"), 1)
        self.assertEqual(len(paths), 3)
        self.assertIn("extruder=target", paths[0])
        self.assertEqual(paths[1], "/printer/print/start")
        self.assertIn("/printer/objects/query", paths[2])

    def test_printer_phase_accepts_print_start_preparation_signals(self):
        payload = {'result': {'status': {
            'save_variables': {'variables': {'display_off': 1}},
            'print_stats': {'state': 'standby', 'filename': ''},
            'idle_timeout': {'state': 'Printing'},
            'virtual_sdcard': {'is_active': False, 'file_path': ''},
            'webhooks': {'state': 'ready'},
        }}}
        with mock.patch.object(model_job, '_mr', return_value=payload):
            self.assertEqual(model_job._printer_phase({'moonraker_url': 'http://offline'}),
                             ('standby', '', 'Printing'))

    def test_print_now_reports_pending_confirmation_without_retry(self):
        with tempfile.TemporaryDirectory() as d:
            patches = [
                mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}),
                mock.patch.object(model_job, "_printer_phase", return_value=("standby", "", "Ready")),
                mock.patch.object(model_job, "verify_gcode"),
                mock.patch.object(model_job, "_verify_filament_readiness"),
                mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run),
                mock.patch.object(model_job, "_mr"),
                mock.patch.object(model_job, "_wait_printing", return_value="standby"),
                mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"),
                mock.patch.object(model_job, "_zmod_version", return_value=None),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as mr, patches[6], patches[7], patches[8]:
                out = model_job.print_now(self._gcode(d))
                self.assertEqual(out['phase'], 'awaiting_confirmation')
                self.assertEqual(out['message'], 'submitted, awaiting confirmation')
                # One start POST, then the #84 status readback. Not a second start.
                # A read-only heater baseline before the start (evidence for a lost response), exactly one start POST, then the readback.
                paths = [c.args[1] for c in mr.call_args_list]
                self.assertEqual(paths.count("/printer/print/start"), 1)
                self.assertEqual(len(paths), 3)
                self.assertIn("extruder=target", paths[0])
                self.assertEqual(paths[1], "/printer/print/start")
                self.assertIn("/printer/objects/query", paths[2])

    def test_z_offset_notice_only_when_start_print_lacks_z_offset(self):
        plain = "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n"
        explicit = "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60 Z_OFFSET=-0.25\n"
        self.assertEqual(model_job.z_offset_notice(plain), model_job.Z_OFFSET_NOTICE)
        self.assertIsNone(model_job.z_offset_notice(explicit))
        self.assertEqual(model_job.start_print_settings(explicit)["args"]["Z_OFFSET"], "-0.25")
        self.assertEqual(model_job.z_offset_notice("no start macro\n"), model_job.Z_OFFSET_NOTICE)

    def test_job_details_is_multiline_and_labels_unknowns(self):
        out = {"title": "Ghost", "print_number": 42, "print_time": "1h 2m", "filament_g": 12, "finish_et": "4:10 PM ET Sat"}
        txt = model_job.job_details(out, printer="AD5X", nozzle_mm=0.4,
                                    selection={"slot": 1, "color": "Black", "material": "PLA", "spoolman_id": 10},
                                    settings=model_job.start_print_settings("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n"),
                                    record_path="/vault/Ghost.md", z_notice=model_job.Z_OFFSET_NOTICE,
                                    screen_z=-0.005, mesh={"profile": "auto", "saved_at": 1791141600})
        for want in ("**Job Details:**", "Print #42", "Model: Ghost", "1h 2m printing + ~5 min warmup; finish 4:10 PM Sat", "Filament: 12 g",
                     "slot 1: Black PLA", "Printer: AD5X", "Stock nozzle: 0.4 mm", "nozzle 220 C, bed 60 C",
                     "Z offset -0.005 mm (persisted)", "Bed mesh: auto (config saved"):
            self.assertIn(want, txt)
        for omitted in (" ET", " EDT", " EST", "Print record", "/vault", "Spoolman", "-0.25", "no explicit START_PRINT"):
            self.assertNotIn(omitted, txt)
        self.assertEqual(len(txt.splitlines()), 9)
        bare = model_job.job_details({"title": "X"})
        self.assertIn("Estimated time: unknown", bare)
        self.assertIn("Filament: unknown; unknown", bare)
        self.assertIn("Printer: unknown", bare)
        self.assertIn("Z offset unknown; Bed mesh: unknown", bare)

    def test_job_details_shows_stored_full_clean_when_history_fails(self):
        from datetime import datetime, timezone
        from datetime import timedelta
        # Relative to the real clock: job_details uses datetime.now() directly.
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=2, hours=1)).isoformat()
        runs = [{"kind": "full", "hours_total": 12.0, "at": old}]
        maintenance = _external.load("maintenance")
        offline = mock.Mock(_call=mock.Mock(side_effect=OSError("offline")))
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"PRINTER_MAINTENANCE_STATE": str(Path(d) / "maintenance.json")}), \
             mock.patch.dict("sys.modules", {"print_control": offline, "print_handy.core.print_control": offline}):
            state_path = Path(d) / "maintenance.json"
            maintenance.save({"runs": runs}, state_path)
            with mock.patch.object(maintenance, "load", return_value={"runs": runs}), \
                 mock.patch.object(maintenance, "datetime") as clock:
                clock.now.return_value = now
                clock.fromisoformat.side_effect = datetime.fromisoformat
                txt = model_job.job_details({"title": "Ghost"})
        self.assertIn("- Maintenance: last full clean 2 days / print hours unknown ago. (history totals unavailable)", txt)
        self.assertNotIn("0.0 h ago", txt)

    def test_start_claim_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"):
            model_job.claim_start_announcement("old.gcode", now=0)
            model_job.claim_start_announcement("x.gcode", now=10_000)
            claims = json.loads((Path(d) / "claims.json").read_text())
            self.assertIn("x.gcode", claims)
            self.assertNotIn("old.gcode", claims)  # expired claims are pruned
            model_job.release_start_announcement("x.gcode")
            self.assertEqual(json.loads((Path(d) / "claims.json").read_text()), {})

    def test_print_now_claims_start_and_emits_details(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run), \
             mock.patch.object(model_job, "_mr"), \
             mock.patch.object(model_job, "_wait_printing", return_value="printing"), \
             mock.patch.object(model_job, "record_start", return_value="/vault/x.md"):
            g = self._gcode(d, z_offset=False)
            sha = model_job.file_sha256(g)
            expected_remote = f"{g.stem}_{sha}{g.suffix}"
            out = model_job.print_now(g, accept_default_z_offset=True)
            self.assertIn(expected_remote, json.loads((Path(d) / "claims.json").read_text()))
        self.assertEqual(out["record_path"], "/vault/x.md")
        self.assertIn("**Job Details:**", out["details"])
        self.assertEqual(out["z_offset_notice"], model_job.Z_OFFSET_NOTICE)

    def test_print_now_failed_start_releases_claim(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run), \
             mock.patch.object(model_job, "_mr"), \
             mock.patch.object(model_job, "_wait_printing", return_value="standby"):
            g = self._gcode(d)
            sha = model_job.file_sha256(g)
            expected_remote = f"{g.stem}_{sha}{g.suffix}"
            # An idle printer that still shows a previous file is not proof the accepted start failed: the
            # lease stays uncertain, and the claim is freed so the listener can post the start.
            out = model_job.print_now(g)
            self.assertEqual(out["phase"], "awaiting_confirmation")
            self.assertNotIn(expected_remote, json.loads((Path(d) / "claims.json").read_text()))
            self.assertEqual([r["phase"] for r in launch_state.pending()], ["uncertain"])

    def test_print_now_artifact_identity_and_readback_hash_success(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_wait_printing", return_value="printing"), \
             mock.patch.object(model_job, "record_start", return_value="/vault/x.md"):
            g = self._gcode(d, z_offset=False)
            sha = model_job.file_sha256(g)
            expected_remote = f"{g.stem}_{sha}{g.suffix}"
            upload_stdout = json.dumps({"result": {"item": {"root": "gcodes", "path": expected_remote}}})
            g_bytes = g.read_bytes()
            def mock_urlopen(req, timeout=30):
                url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
                if "/server/files/gcodes/" in url:
                    return io.BytesIO(g_bytes)
                raise ValueError(f"unexpected url {url}")
            with mock.patch.object(model_job.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=upload_stdout, stderr="")) as mock_run, \
                 mock.patch("urllib.request.urlopen", side_effect=mock_urlopen), \
                 mock.patch.object(model_job, "_mr") as mock_mr:
                out = model_job.print_now(g, accept_default_z_offset=True)
                mock_run.assert_called_once()
                self.assertEqual(mock_run.call_args[0][0][3], expected_remote)
                start_calls = [c for c in mock_mr.call_args_list if c.args[1] == "/printer/print/start"]
                self.assertEqual(len(start_calls), 1)
                self.assertEqual(start_calls[0], mock.call(mock.ANY, "/printer/print/start", data={"filename": expected_remote}))
                # The heater baseline before the start, then the one readback.
                query_calls = [c for c in mock_mr.call_args_list if "/printer/objects/query" in c.args[1]]
                self.assertEqual(len(query_calls), 2)
                self.assertEqual(out["file"], expected_remote)

    def test_print_now_rejects_upload_target_mismatch(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_zmod_version", return_value=None), \
             mock.patch.object(model_job, "_mr") as mock_mr:
            g = self._gcode(d)
            upload_stdout = json.dumps({"result": {"item": {"root": "gcodes", "path": "different_file.gcode"}}})
            with mock.patch.object(model_job.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=upload_stdout, stderr="")):
                with self.assertRaisesRegex(SystemExit, "The printer did not confirm the uploaded file"):
                    model_job.print_now(g)
            mock_mr.assert_not_called()

    def test_print_now_rejects_readback_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_zmod_version", return_value=None), \
             mock.patch.object(model_job, "_mr") as mock_mr:
            g = self._gcode(d)
            sha = model_job.file_sha256(g)
            expected_remote = f"{g.stem}_{sha}{g.suffix}"
            with mock.patch.object(model_job.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr="HTTP 400: Checksum mismatch")) as mock_run:
                with self.assertRaisesRegex(SystemExit, "The file could not be uploaded to the printer, so nothing started"):
                    model_job.print_now(g)
            mock_mr.assert_not_called()
            # Verify moonrakerctl upload was called with the sha checksum parameter
            cmd = mock_run.call_args[0][0]
            self.assertEqual(cmd[-1], sha)

    def test_check_filament_readiness_requires_explicit_matching_loaded_slot(self):
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True},
            "zmod_color": {"slots": [
                {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True},
                {"ID": "2", "Material": "PLA", "Color": "pink", "hasFilament": False},
            ]},
        }
        selection = {"filament_selection": {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}}
        stale = {"filament_selection": {"slot": 2, "material": "PLA", "color": "pink", "spoolman_id": 11}}
        mismatch = {"filament_selection": {"slot": 1, "material": "PLA", "color": "black", "spoolman_id": 8}}
        self.assertEqual(model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100), [])
        self.assertTrue(any("no explicit" in p for p in model_job.check_filament_readiness({}, status, observed_at=100, now=100)))
        self.assertTrue(any("false" in p for p in model_job.check_filament_readiness({}, status, stale, observed_at=100, now=100)))
        self.assertTrue(any("color does not match" in p for p in model_job.check_filament_readiness({}, status, mismatch, observed_at=100, now=100)))

    def test_check_filament_readiness_requires_head_sensor(self):
        status = {"zmod_color": {"slots": [
            {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}
        ]}}
        selection = {"filament_selection": {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}}
        problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100)
        self.assertTrue(any("head filament sensor is unknown" in p for p in problems))
        self.assertTrue(any("telemetry freshness is unknown" in p for p in
                            model_job.check_filament_readiness({}, status, selection)))

    def test_check_filament_readiness_enforces_fresh_telemetry_and_strict_booleans(self):
        selection = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True},
            "zmod_color": {"slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}]},
        }
        for age in (16, -1):
            with self.subTest(age=age):
                problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100 + age)
                self.assertTrue(any("stale or has an invalid timestamp" in p for p in problems))
        for value in (None, "true", 1, 0):
            with self.subTest(value=value):
                bad = json.loads(json.dumps(status))
                bad["filament_switch_sensor head_switch_sensor"]["filament_detected"] = value
                bad["zmod_color"]["slots"][0]["hasFilament"] = value
                problems = model_job.check_filament_readiness({}, bad, selection, observed_at=100, now=100)
                self.assertTrue(any("head filament sensor is unknown" in p for p in problems))
                self.assertTrue(any("selected IFS slot 1 presence is unknown" in p for p in problems))

    def test_check_filament_readiness_rejects_duplicate_or_malformed_ids(self):
        selection = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}
        base = {"filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True}}
        cases = [
            ([{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True},
              {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}], "duplicate ID"),
            ([{"ID": True, "Material": "PLA", "Color": "white", "hasFilament": True}], "malformed slot IDs"),
        ]
        for slots, expected in cases:
            with self.subTest(expected=expected):
                status = {**base, "zmod_color": {"slots": slots}}
                problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100)
                self.assertTrue(any(expected in p for p in problems))

    def test_check_filament_readiness_rejects_disabled_sensor(self):
        selection = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": False},
            "zmod_color": {"slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}]},
        }
        problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100)
        self.assertTrue(any("enabled state is false" in p for p in problems))

    def test_check_filament_readiness_accepts_idle_staged_head_sensor_false(self):
        selection = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": False, "enabled": True},
            "zmod_color": {"slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}]},
        }
        status['print_stats'] = {'state': 'standby'}
        problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100)
        self.assertEqual(problems, [])
        status['print_stats']['state'] = 'printing'
        problems = model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100)
        self.assertTrue(any("head filament sensor reports no filament" in p for p in problems))
        self.assertFalse(any("selected IFS slot" in p for p in problems))

    def test_validate_selection_rejects_invalid_slot_types_and_missing_spool(self):
        for selection in (
            {"slot": True, "material": "PLA", "color": "white", "spoolman_id": 10},
            {"slot": 5, "material": "PLA", "color": "white", "spoolman_id": 10},
            {"slot": 1, "material": "PLA", "color": "white"},
        ):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                model_job.validate_filament_selection(selection)

    def test_save_selection_is_bound_to_model_job_and_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            job_path = Path(td) / "JOB.json"
            job_path.write_text(json.dumps({"model_id": "123", "title": "test"}))
            selection = model_job.save_filament_selection(job_path, {
                "slot": 3, "color": " black ", "material": "PLA", "spoolman_id": 8,
            })
            self.assertEqual(selection, {"slot": 3, "color": "black", "material": "PLA", "spoolman_id": 8})
            self.assertEqual(json.loads(job_path.read_text())["filament_selection"], selection)
            self.assertFalse(job_path.with_suffix(".json.tmp").exists())

    def test_filament_select_cli_writes_only_named_job(self):
        with tempfile.TemporaryDirectory() as td:
            job_path = Path(td) / "JOB.json"
            job_path.write_text(json.dumps({"model_id": "123", "title": "test"}))
            home = Path(td) / "hermes"
            (home / "state").mkdir(parents=True)
            shutil.copy(Path(__file__).parent / "fixtures" / "printer_hardware.json", home / "state" / "printer_hardware.json")
            result = model_job.subprocess.run([
                *CLI, "filament-select", str(job_path),
                "--slot", "4", "--color", "dark green", "--material", "PLA", "--spoolman-id", "12",
            ], capture_output=True, text=True, timeout=15, env={**CLI_ENV, "PRINT_HANDY__PATHS__HERMES_HOME": str(home)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(job_path.read_text())["filament_selection"]["slot"], 4)

    def test_verify_readiness_refuses_unmapped_slot_before_network(self):
        with mock.patch.object(model_job, "_mr") as transport, \
             mock.patch.object(model_job, "request_json") as http:
            with self.assertRaisesRegex(RuntimeError, "selected IFS slot 1"):
                model_job._verify_filament_readiness({"spool": {"spoolman_id": 8}}, {
                    "slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10,
                })
            transport.assert_not_called()
            http.assert_not_called()

    def test_check_filament_readiness_reports_readable_numeric_slot_id(self):
        selection = {"slot": 2, "material": "PLA", "color": "pink", "spoolman_id": 10}
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True},
            "zmod_color": {"slots": [
                {"ID": 3, "Material": "PLA", "Color": "black", "hasFilament": False},
                {"ID": 2, "Material": "PLA", "Color": "pink", "hasFilament": True},
            ]},
        }
        self.assertEqual(model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100), [])

    def test_check_filament_readiness_accepts_shuffled_ids_not_array_index(self):
        selection = {"slot": 2, "material": "PLA", "color": "pink", "spoolman_id": 10}
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True},
            "zmod_color": {"slots": [
                {"ID": "3", "Material": "PLA", "Color": "black", "hasFilament": False},
                {"ID": "2", "Material": "PLA", "Color": "pink", "hasFilament": True},
                {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": False},
            ]},
        }
        self.assertEqual(model_job.check_filament_readiness({}, status, selection, observed_at=100, now=100), [])

    def test_native_filament_status_fallback_404_preserves_raw_but_guards_presence(self):
        status = {"save_variables": {"variables": {"display_off": 0}},
                  "zmod_color": {"ifs": True, "slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": False}]},
                  "zmod_ifs": {"Ports": [False]},
                  "filament_switch_sensor head_switch_sensor": {"filament_detected": False}}
        with mock.patch.object(model_job, "request_json", return_value={"result": {"status": status}}), \
             mock.patch.object(model_job, "read_stock_filament_slots", side_effect=FileNotFoundError("404")):
            result = model_job.read_filament_telemetry({"moonraker_url": "http://test"})
        self.assertEqual(result["printer_mode"], "native")
        self.assertFalse(result["stock_detail_authoritative"])
        self.assertIn("FileNotFoundError", result["stock_detail_error"])
        self.assertEqual(result["raw_zmod_color_presence"], [{"ID": "1", "hasFilament": False}])
        self.assertEqual(result["feeder_slots"][0]["hasFilament"], "unknown")
        self.assertIn("conflicted", status["zmod_color"]["slots"][0]["presence_source"])

    def test_native_filament_status_detail_is_authoritative_and_idle_head_false_is_expected(self):
        status = {"save_variables": {"variables": {"display_off": 0}},
                  "zmod_color": {"slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": False}]},
                  "print_stats": {"state": "standby"},
                  "filament_switch_sensor head_switch_sensor": {"filament_detected": False, "enabled": True}}
        with mock.patch.object(model_job, "request_json", return_value={"result": {"status": status}}), \
             mock.patch.object(model_job, "read_stock_filament_slots", return_value={1: True}):
            result = model_job.read_filament_telemetry({"moonraker_url": "http://test"})
        self.assertTrue(result["stock_detail_authoritative"])
        self.assertEqual(result["feeder_slots"][0]["hasFilament"], "true")
        self.assertEqual(result["toolhead"]["filament_detected"], "false")
        self.assertEqual(status["zmod_color"]["slots"][0]["presence_source"], "stock /detail")
        # Idle/unloaded false is accepted, active-print false still refuses.
        selection = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 1}
        self.assertFalse(any("head filament sensor reports no filament" in p for p in
                             model_job.check_filament_readiness({}, {"print_stats": {"state": "standby"},
                                 "filament_switch_sensor head_switch_sensor": {"filament_detected": False},
                                 "zmod_color": {"ifs": True, "slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}]}},
                                 selection, observed_at=100, now=100)))
        self.assertTrue(any("head filament sensor reports no filament" in p for p in
                            model_job.check_filament_readiness({}, {"print_stats": {"state": "printing"},
                                "filament_switch_sensor head_switch_sensor": {"filament_detected": False},
                                "zmod_color": {"ifs": True, "slots": [{"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": True}]}},
                                selection, observed_at=100, now=100)))

    def test_screenless_filament_status_does_not_query_stock_detail(self):
        status = {"save_variables": {"variables": {"display_off": 1}},
                  "zmod_color": {"slots": [{"ID": "1", "hasFilament": False}]}}
        with mock.patch.object(model_job, "request_json", return_value={"result": {"status": status}}), \
             mock.patch.object(model_job, "read_stock_filament_slots") as stock:
            result = model_job.read_filament_telemetry({"moonraker_url": "http://test"})
        stock.assert_not_called()
        self.assertEqual(result["printer_mode"], "screenless")
        self.assertFalse(result["stock_detail_authoritative"])
        self.assertEqual(result["feeder_slots"][0]["hasFilament"], "false")

    def test_read_filament_telemetry_labels_sources_and_unknown_without_network(self):
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": None, "enabled": False},
            "zmod_color": {"ifs": True, "slots": [
                {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": "true"},
            ]},
        }
        with mock.patch.object(model_job, "request_json", return_value={"result": {"status": status}}):
            result = model_job.read_filament_telemetry({"moonraker_url": "http://test"})
        self.assertEqual(result["configured_palette"][0]["Color"], "white")
        self.assertEqual(result["feeder_slots"][0]["hasFilament"], "unknown")
        self.assertEqual(result["toolhead"]["filament_detected"], "unknown")
        self.assertIn("underlying sensor freshness is unknown", result["freshness_note"])
        self.assertEqual(result["feeder_ports_zero_based"], "unknown")

    def test_telemetry_query_failure_does_not_raise(self):
        with mock.patch.object(model_job, "request_json", side_effect=OSError("offline")):
            result = model_job.read_filament_telemetry({"moonraker_url": "http://test"})
        self.assertEqual(result["configured_palette"], "unknown")
        self.assertEqual(result["feeder_slots"], "unknown")
        self.assertEqual(result["toolhead"]["enabled"], "unknown")
        self.assertEqual(result["query_error"], "OSError: offline")

    def test_filament_status_cli_offline_without_printer_mutation(self):
        status = {"filament_switch_sensor head_switch_sensor": {"filament_detected": True},
                  "zmod_color": {"slots": []}}
        with mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://test"}), \
             mock.patch.object(model_job, "request_json", return_value={"result": {"status": status}}) as request, \
             mock.patch.object(sys, "argv", ["model_job.py", "filament-status"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            model_job.main()
        request.assert_called_once()
        result = json.loads(output.getvalue())
        self.assertEqual(result["toolhead"]["filament_detected"], "true")
        self.assertEqual(result["feeder_slots"], [])

    def test_fetch_select_requires_approval_before_download_or_slice(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download") as download, \
             mock.patch.object(model_job, "slice_job") as slice_job, \
             mock.patch.object(sys, "argv", ["model_job.py", "fetch", "https://makerworld.com/en/models/123", "--select", "1"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(SystemExit, "explicit --approved"):
                model_job.main()
        download.assert_not_called()
        slice_job.assert_not_called()

    def test_fetch_select_refuses_invalid_selection_before_download_or_slice(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download") as download, \
             mock.patch.object(model_job, "slice_job") as slice_job, \
             mock.patch.object(sys, "argv", ["model_job.py", "fetch", "https://makerworld.com/en/models/123", "--select", "0", "--approved"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(SystemExit, "invalid --select"):
                model_job.main()
        download.assert_not_called()
        slice_job.assert_not_called()

    def test_verify_readiness_requires_spool_selection_before_network(self):
        with mock.patch.object(model_job, "request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "explicit filament selection is required"):
                model_job._verify_filament_readiness({"spool": {"spoolman_id": 8}, "moonraker_url": "http://test"}, None)
            request.assert_not_called()

    def test_print_now_verifies_filament_readiness_before_upload(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness", side_effect=RuntimeError("filament readiness refused: head filament sensor is empty")), \
             mock.patch.object(model_job.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "filament readiness refused"):
                model_job.print_now(self._gcode(d))
            run.assert_not_called()

    def test_print_now_never_retries_twice(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_zmod_version", return_value=None), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run), \
             mock.patch.object(model_job, "_mr") as mr, \
             mock.patch.object(model_job, "_wait_printing", return_value="standby"):
            out = model_job.print_now(self._gcode(d))
            self.assertEqual(out["phase"], "awaiting_confirmation")
        # One start POST, then the #84 status readback. Not a second start.
        # (_zmod_version pinned unknown: this choreography is the plain .gcode path's.)
        # A read-only heater baseline before the start (evidence for a lost response), exactly one start POST, then the readback.
        paths = [c.args[1] for c in mr.call_args_list]
        self.assertEqual(paths.count("/printer/print/start"), 1)
        self.assertEqual(len(paths), 3)
        self.assertIn("extruder=target", paths[0])
        self.assertEqual(paths[1], "/printer/print/start")
        self.assertIn("/printer/objects/query", paths[2])

    def test_fetch_print_now_without_filament_selection_fails_closed_naming_command(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download") as download, \
             mock.patch.object(model_job, "slice_job") as slice_job, \
             mock.patch.object(model_job, "print_now") as print_now_call, \
             mock.patch.object(sys, "argv", ["model_job.py", "fetch", "https://makerworld.com/en/models/123", "--select", "1", "--approved", "--print-now"]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(SystemExit, "filament-select"):
                model_job.main()
        download.assert_not_called()
        slice_job.assert_not_called()
        print_now_call.assert_not_called()

    def test_fetch_with_explicit_filament_args_records_selection_and_allows_print_now(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download", return_value=Path(td) / "dummy.3mf") as download, \
             mock.patch.object(model_job, "audit", return_value={"plates": 1, "size_x": 10, "size_y": 10, "size_z": 10}), \
             mock.patch.object(model_job, "intake_color_choices", return_value=True), \
             mock.patch.object(model_job, "slice_job", return_value=Path(td) / "dummy.gcode") as slice_job, \
             mock.patch.object(model_job, "print_now") as print_now_call, \
             mock.patch.object(sys, "argv", [
                 "model_job.py", "fetch", "https://makerworld.com/en/models/123",
                 "--select", "1", "--approved", "--print-now",
                 "--filament-slot", "2", "--filament-color", "white", "--filament-material", "PLA", "--filament-spoolman-id", "8",
             ]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
                model_job.main()
                download.assert_called_once()
                slice_job.assert_called_once()
                print_now_call.assert_called_once()
                saved_job = json.loads((Path(td) / "offline-test" / "JOB.json").read_text())
                self.assertEqual(saved_job.get("filament_selection"), {
                    "slot": 2, "color": "white", "material": "PLA", "spoolman_id": 8,
                })

    def test_fetch_with_filament_args_without_print_now_persists_selection(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download", return_value=Path(td) / "dummy.3mf"), \
             mock.patch.object(model_job, "audit", return_value={"plates": 1, "size_x": 10, "size_y": 10, "size_z": 10}), \
             mock.patch.object(model_job, "intake_color_choices", return_value=True), \
             mock.patch.object(model_job, "slice_job", return_value=Path(td) / "dummy.gcode"), \
             mock.patch.object(model_job, "print_now") as print_now_call, \
             mock.patch.object(sys, "argv", [
                 "model_job.py", "fetch", "https://makerworld.com/en/models/123",
                 "--select", "1", "--approved",
                 "--filament-slot", "3", "--filament-color", "black", "--filament-material", "PLA", "--filament-spoolman-id", "8",
             ]), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
                model_job.main()
                print_now_call.assert_not_called()
                saved_job = json.loads((Path(td) / "offline-test" / "JOB.json").read_text())
                self.assertEqual(saved_job.get("filament_selection"), {
                    "slot": 3, "color": "black", "material": "PLA", "spoolman_id": 8,
                })

    def test_fetch_never_inherits_old_selection_from_existing_directory(self):
        source_job = {"slug": "offline-test", "site": "makerworld", "model_id": "123", "title": "Offline Test", "sources": [{"filename": "test.3mf", "title": "Test source", "site_nozzle_mm": 0.4, "plates": [], "materials": ["PLA"]}]}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(model_job, "ARCHIVE", Path(td)), \
             mock.patch.object(model_job, "maker_inventory", return_value=source_job), \
             mock.patch.object(model_job.printer_state, "load", return_value={"nozzle": {"diameter_mm": 0.4, "type": "brass"}, "z_offset_mm": 0.0, "temps_c": {"nozzle": 0, "bed": 0}, "spool": {"material": "PLA", "spoolman_id": 8, "name": "white"}}), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_download") as download, \
             mock.patch.object(model_job, "slice_job") as slice_job, \
             mock.patch.object(model_job, "print_now") as print_now_call:
            folder = Path(td) / "offline-test"
            folder.mkdir(parents=True)
            (folder / "JOB.json").write_text(json.dumps({
                "model_id": "123",
                "filament_selection": {"slot": 1, "color": "red", "material": "PETG", "spoolman_id": 99},
            }))
            with mock.patch.object(sys, "argv", ["model_job.py", "fetch", "https://makerworld.com/en/models/123", "--select", "1", "--approved", "--print-now"]), \
                 mock.patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaisesRegex(SystemExit, "filament-select"):
                    model_job.main()
            download.assert_not_called()
            slice_job.assert_not_called()
            print_now_call.assert_not_called()

    def test_validate_filament_selection_requires_explicit_selection_naming_command(self):
        with self.assertRaisesRegex(ValueError, "filament-select"):
            model_job.validate_filament_selection(None)

    def test_verify_readiness_without_selection_fails_closed_naming_command(self):
        with mock.patch.object(model_job, "request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "filament-select"):
                model_job._verify_filament_readiness({"spool": {"spoolman_id": 8}, "moonraker_url": "http://test"}, None)
            request.assert_not_called()

    def test_telemetry_and_readiness_never_write_spoolman_or_mutate_active_spool(self):
        import copy
        st = {
            "moonraker_url": "http://moonraker.local",
            "spoolman_url": "http://spoolman.local",
            "spool": {"spoolman_id": 8, "name": "white", "material": "PLA"},
            "nozzle": {"diameter_mm": 0.4, "type": "brass"},
        }
        original_st = copy.deepcopy(st)
        bad_status = {
            "filament_switch_sensor head_switch_sensor": {"enabled": True, "filament_detected": False},
            "zmod_color": {
                "slots": [
                    {"ID": 1, "Color": "white", "Material": "PLA", "hasFilament": False},
                    {"ID": 2, "Color": "unknown", "Material": "unknown", "hasFilament": "maybe"},
                ]
            },
            "zmod_ifs": {"Ports": [False, None], "ifs": True},
        }
        with mock.patch.object(model_job, "request_json", return_value={"result": {"status": bad_status}}) as request, \
             mock.patch("urllib.request.urlopen") as urlopen:
            telemetry = model_job.read_filament_telemetry(st)
            self.assertEqual(st, original_st)
            self.assertEqual(telemetry["feeder_slots"][0]["hasFilament"], "false")
            self.assertEqual(telemetry["feeder_slots"][1]["hasFilament"], "unknown")
            urlopen.assert_not_called()

            problems = model_job.check_filament_readiness(
                st, bad_status,
                {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 8},
                observed_at=time.time(),
            )
            self.assertTrue(any("selected IFS slot 1 reports false" in p for p in problems))
            self.assertTrue(any("head filament sensor reports no filament" in p for p in problems))
            self.assertEqual(st, original_st)
            urlopen.assert_not_called()

            with self.assertRaises(RuntimeError):
                model_job._verify_filament_readiness(
                    st,
                    {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 8},
                )
            self.assertEqual(st, original_st)
            urlopen.assert_not_called()

    def test_restricted_path_invokes_moonrakerctl_via_absolute_path(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_fetch_remote_gcode_sha256", side_effect=lambda url, name: name.rsplit(".", 1)[0].split("_", 1)[1] if "_" in name else ""), \
             mock.patch.object(model_job, "_mr"), \
             mock.patch.object(model_job, "_wait_printing", return_value="printing"), \
             mock.patch.object(model_job, "record_start"), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run) as run, \
             mock.patch.dict(os.environ, {"PATH": "/bin:/usr/bin"}):
            model_job.print_now(self._gcode(d))
            run.assert_called_once()
            called_cmd = run.call_args[0][0]
            self.assertTrue(Path(called_cmd[0]).is_absolute())
            self.assertEqual(called_cmd[0], str(CTL))


class ZmodDeliveryFormatTests(unittest.TestCase):
    """#564: print_now's Moonraker upload swaps to a .gcode.3mf package only when the
    printer's zmod_delivery_format resolves to it; every fallback is today's .gcode."""

    def setUp(self):
        p = mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"})
        p.start(); self.addCleanup(p.stop)
        for p in (mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None, "print_leveling": 0, "problem": None, "stale": None, "z_offset_mm": -0.005, "offset_source": "saved gcode offset"}),
                  mock.patch.object(model_job, "screen_z_offset", return_value=0.025)):
            p.start(); self.addCleanup(p.stop)

    def _gcode(self, d):
        g = Path(d) / "x.gcode"
        g.write_text("; estimated printing time (normal mode) = 1h 2m 0s\n; total filament used [g] = 12.3\n"
                     "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")
        return g

    def _print(self, d, version):
        g = self._gcode(d)
        with mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"), \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "verify_gcode"), \
             mock.patch.object(model_job, "_verify_filament_readiness"), \
             mock.patch.object(model_job, "_zmod_version", return_value=version), \
             mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run) as run, \
             mock.patch.object(model_job, "_mr"), \
             mock.patch.object(model_job, "_wait_printing", return_value="printing"), \
             mock.patch.object(model_job, "record_start", return_value="/vault/x.md"):
            out = model_job.print_now(g, accept_default_z_offset=True)
        return g, run.call_args[0][0], out

    def test_auto_with_zmod_175_uploads_the_3mf_package(self):
        from print_handy.core import gcode_3mf
        with tempfile.TemporaryDirectory() as d:
            g, cmd, out = self._print(d, (1, 7, 5))
            sha = model_job.file_sha256(g)
            self.assertEqual(cmd[1], "upload")
            self.assertTrue(cmd[2].endswith(".gcode.3mf"), cmd)
            package = Path(cmd[2])
            self.assertTrue(package.is_file())
            self.assertEqual(gcode_3mf.read_gcode_text(package), g.read_text())
            # The remote name keeps the G-code's stem+sha (find_job binding) with the package suffix,
            # and the upload checksum is the package's, not the G-code's.
            self.assertEqual(cmd[3], f"x_{sha}.gcode.3mf")
            self.assertEqual(cmd[4], model_job.file_sha256(package))
            self.assertNotEqual(cmd[4], sha)
            self.assertEqual(out["file"], cmd[3])
            self.assertEqual(out["delivery_format"]["format"], "3mf")
            self.assertEqual(out["delivery_format"]["zmod_version"], "1.7.5")

    def test_auto_with_unknown_version_uploads_plain_gcode(self):
        with tempfile.TemporaryDirectory() as d:
            g, cmd, out = self._print(d, None)
            sha = model_job.file_sha256(g)
            self.assertEqual(cmd[2], str(g))
            self.assertEqual(cmd[3], f"x_{sha}.gcode")
            self.assertEqual(cmd[4], sha)
            self.assertNotIn("delivery_format", out)
            self.assertFalse(Path(str(g) + ".3mf").exists())

    def test_auto_with_zmod_below_the_floor_uploads_plain_gcode(self):
        with tempfile.TemporaryDirectory() as d:
            g, cmd, out = self._print(d, (1, 7, 4))
            sha = model_job.file_sha256(g)
            self.assertEqual(cmd[2], str(g))
            self.assertEqual(cmd[3], f"x_{sha}.gcode")
            self.assertEqual(cmd[4], sha)
            self.assertNotIn("delivery_format", out)

    def test_note_delivery_format_persists_on_the_job(self):
        with tempfile.TemporaryDirectory() as d:
            job = {"title": "X"}
            model_job._note_delivery_format(job, Path(d) / "JOB.json", {"format": "3mf", "setting": "auto"})
            self.assertEqual(json.loads((Path(d) / "JOB.json").read_text())["delivery_format"]["format"], "3mf")


class StockIfsPresenceTests(unittest.TestCase):
    STATE = {"moonraker_url": "http://moonraker.test:7125"}

    def _reply(self, slots, code=0):
        return {"code": code, "message": "Success", "detail": {
            "hasMatlStation": True, "matlStationInfo": {"slotInfos": slots}}}

    def _read(self, reply):
        with mock.patch.object(stock, "_stock_credentials", return_value=("SN", "CC")):
            return model_job.read_stock_filament_slots(self.STATE, post=lambda url, body: reply)

    def test_stock_presence_overrides_hardcoded_false(self):
        stock = self._read(self._reply([{"slotId": 1, "hasFilament": True}, {"slotId": 2, "hasFilament": False},
                                        {"slotId": 3, "hasFilament": False}, {"slotId": 4, "hasFilament": False}]))
        status = {
            "filament_switch_sensor head_switch_sensor": {"filament_detected": True, "enabled": True},
            "zmod_color": {"display": True, "slots": [
                {"ID": "1", "Material": "PLA", "Color": "white", "hasFilament": False},
                {"ID": "2", "Material": "PLA", "Color": "pink", "hasFilament": False}]},
        }
        model_job.apply_stock_presence(status, stock)
        sel = {"slot": 1, "material": "PLA", "color": "white", "spoolman_id": 10}
        self.assertEqual(model_job.check_filament_readiness({}, status, sel, observed_at=100, now=100), [])
        sel2 = dict(sel, slot=2, color="pink")
        problems = model_job.check_filament_readiness({}, status, sel2, observed_at=100, now=100)
        self.assertTrue(any("no buffer switch" in p for p in problems))
        self.assertFalse(any("clicks" in p for p in problems))
        status["zmod_color"]["slots"][1]["hasFilament"] = None
        problems = model_job.check_filament_readiness({}, status, sel2, observed_at=100, now=100)
        self.assertTrue(any("presence is unknown" in p for p in problems))

    def test_stock_failures_fail_closed(self):
        for reply in ({"code": 1, "message": "Access code is different"}, None,
                      {"code": 0, "detail": {"hasMatlStation": False}},
                      self._reply([{"slotId": 1}, {"slotId": 1}])):
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                self._read(reply)

    def test_nonbool_stays_unknown(self):
        stock = self._read(self._reply([{"slotId": 1, "hasFilament": "yes"}, {"slotId": 2, "hasFilament": True},
                                        {"slotId": 3}, {"slotId": 4, "hasFilament": False}]))
        status = {"zmod_color": {"slots": [{"ID": "1"}, {"ID": "3"}]}}
        model_job.apply_stock_presence(status, stock)
        self.assertEqual([s["hasFilament"] for s in status["zmod_color"]["slots"]], [None, None])

    def test_missing_slot_refuses(self):
        # A partial slot list is not an authoritative sample (spool/IFS audit finding 4).
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self._read(self._reply([{"slotId": 1, "hasFilament": True}]))

    def test_missing_credentials_refuse(self):
        with mock.patch.dict(model_job.os.environ, {}, clear=True), \
             mock.patch.object(stock.settings, "current", return_value={"paths": {"hermes_home": Path("/nonexistent")}}):
            with self.assertRaisesRegex(RuntimeError, "credentials missing"):
                stock._stock_credentials()


class TaskT0b676477Tests(unittest.TestCase):
    """Offline: Z offset fail-closed, single-use approval, live Job Details, routine receipts."""

    def _ok_patches(self, d):
        return [mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"),
                mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}),
                mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")),
                mock.patch.object(model_job, "verify_gcode"),
                mock.patch.object(model_job, "_verify_filament_readiness"),
                mock.patch.object(model_job, "_mr"),
                mock.patch.object(model_job, "_wait_printing", return_value="printing"),
                mock.patch.object(model_job, "record_start", return_value="/vault/x.md"),
                mock.patch.object(model_job, "live_snapshot", return_value={"nozzle_c": 25.0, "bed_c": 24.0, "slots": "unknown"}),
                mock.patch.object(model_job, "mesh_readiness", return_value={"profile": "auto", "saved_at": None, "print_leveling": 0, "problem": None, "stale": None, "z_offset_mm": -0.005, "offset_source": "saved gcode offset"}),
                mock.patch.object(model_job, "screen_z_offset", return_value=0.025),
                mock.patch.object(model_job, "_fetch_remote_gcode_sha256", side_effect=lambda url, name: name.rsplit(".", 1)[0].split("_", 1)[1] if "_" in name else "")]

    def _job(self, d, z=True):
        folder = Path(d) / "job"; (folder / "out").mkdir(parents=True)
        g = folder / "out" / "x.gcode"
        g.write_text("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60" + (" Z_OFFSET=-0.2" if z else "") + "\n")
        (folder / "JOB.json").write_text(json.dumps({"title": "X", "gcode": str(g)}))
        return folder / "JOB.json"

    def test_missing_z_offset_no_longer_refuses_and_details_show_screen_offset_and_mesh(self):
        with tempfile.TemporaryDirectory() as d:
            ps = self._ok_patches(d)
            for p in ps: p.start()
            try:
                with mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run):
                    out = model_job.print_now(self._job(d, z=False))
            finally:
                for p in ps: p.stop()
        self.assertTrue(out["started"])
        self.assertIn("Z offset -0.005 mm (saved gcode offset)", out["details"])
        self.assertIn("Bed mesh: auto", out["details"])
        self.assertNotIn("printer default", out["details"])
        self.assertEqual(out["screen_z_offset_mm"], -0.005)
        self.assertEqual(out["native_screen_z_offset_mm"], 0.025)

    def test_missing_mesh_refuses_before_upload(self):
        bad = {"profile": None, "saved_at": None, "print_leveling": 0, "problem": "no saved 'auto' bed mesh", "stale": None}
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}), \
             mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")), \
             mock.patch.object(model_job, "mesh_readiness", return_value=bad), \
             mock.patch.object(model_job.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "REFUSED: no saved 'auto' bed mesh.*Nothing uploaded"):
                model_job.print_now(self._job(d))
            run.assert_not_called()

    def _mr_fake(self, profiles, modified=1791051432.0):
        def fake(st, path, data=None, timeout=30):
            if path.startswith("/printer/objects/query?bed_mesh"):
                return {"result": {"status": {"bed_mesh": {"profiles": profiles},
                                              "save_variables": {"variables": {"print_leveling": 0, "display_off": 1, "load_zoffset": 1, "gcode_offsets": {"z": -0.005}, "mesh_test": 0}}}}}
            if path.startswith("/server/files/list"):
                return {"result": [{"path": "printer.cfg", "modified": modified}]}
            if path.endswith("Adventurer5M.json"):
                return {"general": {"x": 1}, "level": {"zOffset": 0.0, "zProbeOffset": 0.02500000037252903, "zLevelOffset": -3.07}}
            raise AssertionError(path)
        return fake

    def test_mesh_readiness_reads_profiles_and_flags_stale_after_nozzle_change(self):
        st = {"moonraker_url": "http://m", "nozzle": {"installed": "2026-10-02"}}
        with mock.patch.object(model_job, "_mr", side_effect=self._mr_fake({"auto": {}, "MESH_DATA": {}})):
            ok = model_job.mesh_readiness(st)
            self.assertIsNone(ok["problem"]); self.assertEqual(ok["profile"], "auto"); self.assertFalse(ok["stale"])
            self.assertEqual(model_job.screen_z_offset(st), 0.025)
        with mock.patch.object(model_job, "_mr", side_effect=self._mr_fake({"auto": {}}, modified=1_700_000_000.0)):
            self.assertTrue(model_job.mesh_readiness(st)["stale"])
            self.assertIn("Nozzle changed after this mesh", model_job.mesh_line(model_job.mesh_readiness(st)))
        with mock.patch.object(model_job, "_mr", side_effect=self._mr_fake({"MESH_DATA": {}})):
            self.assertIn("no saved 'auto' bed mesh", model_job.mesh_readiness(st)["problem"])
        lv1 = self._mr_fake({"auto": {}})
        def fake_lv1(st_, path, data=None, timeout=30):
            r = lv1(st_, path, data, timeout)
            if path.startswith("/printer/objects/query?bed_mesh"):
                r["result"]["status"]["save_variables"]["variables"]["print_leveling"] = 1
            return r
        with mock.patch.object(model_job, "_mr", side_effect=fake_lv1):
            self.assertIn("PRINT_LEVELING=1", model_job.mesh_readiness(st)["problem"])
        with mock.patch.object(model_job, "_mr", side_effect=OSError("down")):
            self.assertIn("could not read the bed mesh", model_job.mesh_readiness(st)["problem"])
            self.assertIsNone(model_job.screen_z_offset(st))

    def test_screenless_authority_and_mesh_test_modes(self):
        for mode in (0, 1, 2, 3, 4, None):
            def reply(st, path, data=None, timeout=30):
                if path.startswith("/printer/objects/query?bed_mesh"):
                    return {"result": {"status": {"bed_mesh": {"profiles": {"auto": {}}},
                        "save_variables": {"variables": {"display_off": 1, "load_zoffset": 1,
                        "gcode_offsets": {"z": 0.025}, "print_leveling": 0, "mesh_test": mode}}}}}
                return {"result": []}
            with self.subTest(mode=mode), mock.patch.object(model_job, "_mr", side_effect=reply):
                result = model_job.mesh_readiness({})
                if mode in (0, 1):
                    self.assertIsNone(result["problem"])
                    self.assertEqual(result["z_offset_mm"], 0.025)
                else:
                    self.assertIn("MESH_TEST", result["problem"])

    def test_offset_owner_follows_display_mode(self):
        captured = {"leftExtruderOffset": {"zOffset": 0.0, "zProbeOffset": 0.02500000037252903},
                    "level": {"zProbeOffset": 9.0}}
        def reply(display_off, payload=captured):
            def fake(st, path, data=None, timeout=30):
                if path.startswith("/printer/objects/query?bed_mesh"):
                    return {"result": {"status": {"bed_mesh": {"profiles": {"auto": {}}},
                        "save_variables": {"variables": {"display_off": display_off, "load_zoffset": 1,
                        "gcode_offsets": {"z": -0.005}, "print_leveling": 0, "mesh_test": 0}}}}}
                if path.endswith("Adventurer5M.json"):
                    return payload
                return {"result": []}
            return fake
        with mock.patch.object(model_job, "_mr", side_effect=reply(1)):
            screenless = model_job.mesh_readiness({})
            self.assertIsNone(screenless["problem"])
            self.assertEqual(screenless["z_offset_mm"], -0.005)
            self.assertEqual(screenless["offset_source"], "saved gcode offset")
        with mock.patch.object(model_job, "_mr", side_effect=reply(0)):
            native = model_job.mesh_readiness({})
            self.assertIsNone(native["problem"])
            self.assertEqual(native["z_offset_mm"], 0.025)
            self.assertNotEqual(native["z_offset_mm"], 9.0)
            self.assertEqual(native["saved_gcode_offset_mm"], -0.005)
            self.assertEqual(native["offset_source"], "native screen zProbeOffset")
            self.assertNotEqual(native["z_offset_mm"], native["saved_gcode_offset_mm"])
        for bad in ({"leftExtruderOffset": {"zOffset": 0.0}},
                    {"leftExtruderOffset": {"zProbeOffset": float("inf")}, "level": {"zProbeOffset": 0.025}},
                    {"level": {"zProbeOffset": float("nan")}}):
            with self.subTest(payload=bad), mock.patch.object(model_job, "_mr", side_effect=reply(0, bad)):
                missing = model_job.mesh_readiness({})
                self.assertIn("zProbeOffset is unreadable", missing["problem"])
                self.assertIsNone(missing["z_offset_mm"])
        fallback = {"level": {"zProbeOffset": 0.025}}
        with mock.patch.object(model_job, "_mr", side_effect=reply(0, fallback)):
            legacy = model_job.mesh_readiness({})
            self.assertIsNone(legacy["problem"])
            self.assertEqual(legacy["z_offset_mm"], 0.025)

    def test_screenless_accepts_any_saved_numeric_offset_and_refuses_unloaded(self):
        def reply(z, load):
            def fake(st, path, data=None, timeout=30):
                if path.startswith("/printer/objects/query?bed_mesh"):
                    return {"result": {"status": {"bed_mesh": {"profiles": {"auto": {}}},
                        "save_variables": {"variables": {"display_off": 1, "load_zoffset": load,
                        "gcode_offsets": {"z": z}, "print_leveling": 0, "mesh_test": 0}}}}}
                return {"result": []}
            return fake
        with mock.patch.object(model_job, "_mr", side_effect=reply(-0.005, 1)):
            ok = model_job.mesh_readiness({})
            self.assertIsNone(ok["problem"])
            self.assertEqual(ok["z_offset_mm"], -0.005)
        with mock.patch.object(model_job, "_mr", side_effect=reply(0.025, 0)):
            bad = model_job.mesh_readiness({})
            self.assertIn("not authoritative", bad["problem"])
        with mock.patch.object(model_job, "_mr", side_effect=reply(None, 1)):
            missing = model_job.mesh_readiness({})
            self.assertIn("not authoritative", missing["problem"])
        for nonfinite in (float("nan"), float("inf"), float("-inf"), True, False):
            with self.subTest(z=nonfinite), mock.patch.object(model_job, "_mr", side_effect=reply(nonfinite, 1)):
                bad = model_job.mesh_readiness({})
                self.assertIn("not authoritative", bad["problem"])

    def test_z_offset_flag_allows_start_and_never_injects_value(self):
        with tempfile.TemporaryDirectory() as d:
            ps = self._ok_patches(d)
            for p in ps: p.start()
            try:
                with mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run):
                    jp = self._job(d, z=False)
                    before = Path(json.loads(jp.read_text())["gcode"]).read_text()
                    out = model_job.print_now(jp, accept_default_z_offset=True)
                    self.assertEqual(Path(json.loads(jp.read_text())["gcode"]).read_text(), before)
            finally:
                for p in ps: p.stop()
        self.assertEqual(out["z_offset_notice"], model_job.Z_OFFSET_NOTICE)

    def test_bed_clear_approval_is_single_use_per_gcode(self):
        with tempfile.TemporaryDirectory() as d:
            ps = self._ok_patches(d)
            for p in ps: p.start()
            try:
                with mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run) as run:
                    jp = self._job(d)
                    model_job.print_now(jp)
                    approvals = json.loads(jp.read_text())["approvals"]
                    self.assertEqual(len(approvals), 1); self.assertEqual(approvals[0]["kind"], "bed_clear")
                    self.assertTrue(approvals[0]["consumed_at"])
                    with self.assertRaisesRegex(SystemExit, "already used.*--reprint"):
                        model_job.print_now(jp)
                    self.assertEqual(run.call_count, 1)  # refused reuse uploaded nothing
                    model_job.print_now(jp, reprint=True)
                    self.assertEqual([a["kind"] for a in json.loads(jp.read_text())["approvals"]], ["bed_clear", "reprint"])
            finally:
                for p in ps: p.stop()

    def test_job_details_omits_live_temps_and_inventory_but_preserves_snapshot(self):
        live = {"nozzle_c": 27.4, "bed_c": None,
                "slots": [{"slot": "1", "color": "black", "material": "PLA", "loaded": True},
                          {"slot": "2", "color": None, "material": "PETG", "loaded": None}]}
        before = json.dumps(live, sort_keys=True)
        txt = model_job.job_details({"title": "X"}, live=live)
        self.assertNotIn("Current temps", txt)
        self.assertNotIn("IFS slots", txt)
        self.assertNotIn("PETG", txt)
        self.assertEqual(json.dumps(live, sort_keys=True), before)
        self.assertIn("Bed mesh: unknown", txt)

    def test_live_snapshot_reads_mocks_and_degrades_to_unknown(self):
        st = {"moonraker_url": "http://m"}
        with mock.patch.object(model_job, "_mr", return_value={"result": {"status": {
                 "extruder": {"temperature": 30.0}, "heater_bed": {"temperature": 22.0}}}}), \
             mock.patch.object(model_job, "read_filament_telemetry", return_value={"configured_palette": [
                 {"ID": "1", "Color": "white", "Material": "PLA"}, {"ID": "2", "Color": "pink", "Material": "PLA"}]}), \
             mock.patch.object(model_job, "read_stock_filament_slots", return_value={1: True, 2: False}):
            snap = model_job.live_snapshot(st)
        self.assertEqual((snap["nozzle_c"], snap["bed_c"]), (30.0, 22.0))
        self.assertEqual([s["loaded"] for s in snap["slots"]], [True, False])
        with mock.patch.object(model_job, "_mr", side_effect=OSError("down")), \
             mock.patch.object(model_job, "read_filament_telemetry", side_effect=OSError("down")):
            self.assertEqual(model_job.live_snapshot(st), {"nozzle_c": None, "bed_c": None, "slots": "unknown"})

    def test_routine_receipt_uses_static_typical_table(self):
        self.assertEqual(model_job.routine_receipt("leveling"),
                         "Running full bed leveling, about 5 to 10 min (typical, not measured on this printer).")
        for r in model_job.ROUTINE_TYPICAL_MIN:
            self.assertIn("typical", model_job.routine_receipt(r))
        with self.assertRaises(ValueError):
            model_job.routine_receipt("make_coffee")

    def test_routine_phase_reads_idle_timeout_only(self):
        st = {"moonraker_url": "http://m"}
        def q(state): return {"result": {"status": {"idle_timeout": {"state": state}}}}
        with mock.patch.object(model_job, "_mr", return_value=q("Printing")):
            self.assertEqual(model_job.routine_phase(st), "running")
        with mock.patch.object(model_job, "_mr", return_value=q("Ready")):
            self.assertEqual(model_job.routine_phase(st), "completed")
        with mock.patch.object(model_job, "_mr", side_effect=OSError("down")):
            self.assertEqual(model_job.routine_phase(st), "unknown")


class SelectionConsistencyTests(unittest.TestCase):
    def state(self):
        return {"spool": {"spoolman_id": 10}, "filament_slots": {
            "1": {"spoolman_id": 10, "color": "white", "material": "PLA"},
            "2": {"spoolman_id": 11, "color": "pink", "material": "PLA"}}}

    def test_missing_spool_resolves_from_requested_slot_not_global_spool(self):
        result = model_job.resolve_filament_selection(self.state(), {
            "slot": 2, "color": "pink", "material": "PLA", "spoolman_id": None})
        self.assertEqual(result["spoolman_id"], 11)

    def test_star_contradiction_is_rejected_before_network(self):
        selection = {"slot": 2, "color": "pink", "material": "PLA", "spoolman_id": 10}
        with mock.patch.object(model_job, "request_json") as request:
            with self.assertRaisesRegex(RuntimeError, "slot.*spool"):
                model_job._verify_filament_readiness(self.state(), selection)
            request.assert_not_called()

    def test_unknown_mapping_never_inherits_active_spool(self):
        with self.assertRaisesRegex(ValueError, "explicit Spoolman"):
            model_job.resolve_filament_selection({"spool": {"spoolman_id": 10}}, {
                "slot": 2, "color": "pink", "material": "PLA", "spoolman_id": None})

    def test_configured_color_and_material_contradictions_are_rejected(self):
        for key, value in (("color", "white"), ("material", "PETG")):
            selection = {"slot": 2, "color": "pink", "material": "PLA", "spoolman_id": 11}
            selection[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                model_job.resolve_filament_selection(self.state(), selection)

    def test_fetch_contradiction_refuses_before_source_inventory(self):
        with mock.patch.object(sys, "argv", ["model_job.py", "fetch", "https://makerworld.com/en/models/123",
                "--filament-slot", "2", "--filament-color", "pink", "--filament-material", "PLA",
                "--filament-spoolman-id", "10"]), \
             mock.patch.object(model_job.printer_state, "load", return_value=self.state()), \
             mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
             mock.patch.object(model_job, "maker_inventory") as inventory:
            with self.assertRaisesRegex(SystemExit, "slot.*spool"):
                model_job.main()
            inventory.assert_not_called()

    def verified_state(self):
        st = self.state()
        for slot in st["filament_slots"].values():
            slot["physical_color_verified"] = True
        return st

    def test_selected_slot_may_differ_from_active_spool(self):
        # Regression: a verified slot 2 job must not be refused because
        # Moonraker's active spool still points at slot 1.
        st = self.verified_state()
        slot = model_job._validate_slot_selection(st, {
            "slot": 2, "color": "pink", "material": "PLA", "spoolman_id": 11})
        self.assertEqual(slot["spoolman_id"], 11)

    def test_slice_rejects_unverified_selected_slot_before_slicer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "JOB.json"
            path.write_text(json.dumps({"filament_selection": {
                "slot": 2, "color": "pink", "material": "PLA", "spoolman_id": 11}}))
            with mock.patch.object(model_job.printer_state, "load", return_value=self.state()), \
                 mock.patch.object(model_job.printer_state, "check", return_value=(True, [], {})), \
                 mock.patch.object(model_job.subprocess, "run") as slicer:
                with self.assertRaisesRegex(RuntimeError, "selected IFS slot 2"):
                    model_job.slice_job(path)
                slicer.assert_not_called()

    def sync_transport(self, active, fail_write=False):
        calls = []
        def reply(st, path, data=None, **kwargs):
            calls.append((path, data))
            self.assertEqual(path, "/server/spoolman/spool_id")
            if data is not None and not fail_write:
                active[0] = data["spool_id"]
            return {"result": {"spool_id": active[0]}}
        return reply, calls

    def test_sync_active_spool_moves_pointer_to_launch_slot(self):
        st = self.verified_state()
        st["spoolman_url"] = "http://spoolman.invalid"
        active = [10]
        reply, calls = self.sync_transport(active)
        spool = {"archived": False, "filament": {"material": "PLA", "name": "Pink PLA"}}
        with mock.patch.object(model_job, "_mr", side_effect=reply), \
             mock.patch.object(model_job.spoolman_client, "get_spool", return_value=spool), \
             mock.patch.object(model_job.printer_state, "load", return_value=self.verified_state()), \
             mock.patch.object(model_job.printer_state, "save") as save:
            result = model_job._sync_active_spool(st, 2)
        self.assertEqual(result["spool_id"], 11)
        self.assertEqual(active[0], 11)
        self.assertIn(("/server/spoolman/spool_id", {"spool_id": 11}), calls)
        self.assertEqual(save.call_args.args[0]["spool"]["spoolman_id"], 11)
        self.assertEqual(st["spool"]["spoolman_id"], 11)

    def test_sync_active_spool_noop_when_already_current(self):
        st = self.verified_state()
        reply, calls = self.sync_transport([10])
        with mock.patch.object(model_job, "_mr", side_effect=reply), \
             mock.patch.object(model_job.spoolman_client, "get_spool") as http, \
             mock.patch.object(model_job.printer_state, "save") as save:
            self.assertFalse(model_job._sync_active_spool(st, 1)["changed"])
        self.assertEqual(calls, [("/server/spoolman/spool_id", None)])
        http.assert_not_called()
        save.assert_not_called()

    def test_sync_active_spool_refuses_on_failed_readback(self):
        st = self.verified_state()
        st["spoolman_url"] = "http://spoolman.invalid"
        reply, _ = self.sync_transport([10], fail_write=True)
        spool = {"archived": False, "filament": {"material": "PLA"}}
        with mock.patch.object(model_job, "_mr", side_effect=reply), \
             mock.patch.object(model_job.spoolman_client, "get_spool", return_value=spool), \
             mock.patch.object(model_job.printer_state, "save") as save:
            with self.assertRaisesRegex(RuntimeError, "readback.*nothing uploaded"):
                model_job._sync_active_spool(st, 2)
        save.assert_not_called()

    def test_sync_active_spool_refuses_archived_spool(self):
        st = self.verified_state()
        st["spoolman_url"] = "http://spoolman.invalid"
        reply, calls = self.sync_transport([10])
        with mock.patch.object(model_job, "_mr", side_effect=reply), \
             mock.patch.object(model_job.spoolman_client, "get_spool", return_value={"archived": True, "filament": {"material": "PLA"}}):
            with self.assertRaisesRegex(RuntimeError, "archived"):
                model_job._sync_active_spool(st, 2)
        self.assertNotIn(("/server/spoolman/spool_id", {"spool_id": 11}), calls)

    def test_material_family_treats_pla_plus_as_pla_but_not_petg(self):
        same = model_job.printer_state.same_material
        self.assertTrue(same("PLA+", "PLA"))
        self.assertTrue(same("PLA Pro", "pla"))
        self.assertTrue(same("Matte PLA", "PLA"))
        self.assertFalse(same("PETG", "PLA"))
        self.assertFalse(same("PLA-CF", "PLA"))
        self.assertFalse(same("", "PLA"))

    def test_sync_active_spool_accepts_pla_plus_spool_in_pla_slot(self):
        st = self.verified_state()
        st["spoolman_url"] = "http://spoolman.invalid"
        reply, _ = self.sync_transport([10], fail_write=True)
        spool = {"archived": False, "filament": {"material": "PLA+"}}
        with mock.patch.object(model_job, "_mr", side_effect=reply), \
             mock.patch.object(model_job.spoolman_client, "get_spool", return_value=spool), \
             mock.patch.object(model_job.printer_state, "save"):
            # Material check passes; the forced write failure is the next gate.
            with self.assertRaisesRegex(RuntimeError, "readback.*nothing uploaded"):
                model_job._sync_active_spool(st, 2)

    def test_filament_select_rejects_contradiction_before_job_write(self):
        with mock.patch.object(sys, "argv", ["model_job.py", "filament-select", "unused.json",
                "--slot", "2", "--color", "pink", "--material", "PLA", "--spoolman-id", "10"]), \
             mock.patch.object(model_job.printer_state, "load", return_value=self.state()), \
             mock.patch.object(model_job, "save_filament_selection") as save:
            with self.assertRaisesRegex(ValueError, "slot.*spool"):
                model_job.main()
            save.assert_not_called()

    def test_z_notice_does_not_claim_historical_cause(self):
        self.assertNotIn("that is why", model_job.Z_OFFSET_NOTICE)
        self.assertIn("not established", model_job.Z_OFFSET_NOTICE)


if __name__ == "__main__":
    unittest.main()


class JobCardTests(unittest.TestCase):
    def test_card_lists_copies_and_each_spool_once(self):
        job = {"title": "Can Holder", "copies": 3, "color_decisions": {"choices": {
            "a": {"slot": 3, "color": "black", "material": "PLA", "spoolman_id": 8},
            "b": {"slot": 3, "color": "black", "material": "PLA", "spoolman_id": 8},
            "c": {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 5}}}}
        card = model_job.job_card(job, "Can-Holder_plate_1_x.gcode", "; total filament used [g] = 95.56\n")
        self.assertEqual((card["copies"], card["filament_g"], [s["spoolman_id"] for s in card["spools"]]), (3, 95.6, [8, 5]))
        self.assertEqual(model_job.job_card(None, "plate_1_x.gcode")["spools"], [])

    def test_card_reads_bambu_studio_weight_spelling(self):
        """#444: Bambu Studio's header block spells the total 'total filament weight [g] : X'.

        Header lines shaped from a real Bambu Studio 2.8.2.61 A1 slice.
        """
        card = model_job.job_card({"title": "Cube"}, "plate_1.gcode",
                                  "; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n"
                                  "; total filament weight [g] : 3.77\n; HEADER_BLOCK_END\n")
        self.assertEqual(card["filament_g"], 3.8)
        # A genuinely absent total stays unknown (None), never zero.
        self.assertIsNone(model_job.job_card({"title": "Cube"}, "plate_1.gcode", "G1 X1\n")["filament_g"])


class RemoteNameTests(unittest.TestCase):
    def test_title_leads_the_moonraker_name(self):
        sha = "a" * 64
        self.assertEqual(model_job.remote_name(Path("/x/plate_1.gcode"), sha, {"title": "Slim 12oz Can Holder, Red Bull"}),
                         f"Slim-12oz-Can-Holder-Red-Bull_plate_1_{sha}.gcode")
        self.assertEqual(model_job.remote_name(Path("/x/plate_1.gcode"), sha, None), f"plate_1_{sha}.gcode")
        self.assertEqual(model_job.remote_name(Path("/x/Cube.gcode"), sha, {"title": "cube"}), f"Cube_{sha}.gcode")


class AnnouncePrintNameTests(unittest.TestCase):
    def test_publishes_friendly_title_as_display_message(self):
        with mock.patch.object(model_job, "request_json") as http:
            name = model_job._announce_print_name({"moonraker_url": "http://m"}, {"title": 'Build "Plate" Cleaner'})
        self.assertEqual(name, "Build Plate Cleaner")
        http.assert_called_once()
        self.assertEqual(http.call_args.kwargs["data"], {"script": 'SET_DISPLAY_TEXT MSG="Build Plate Cleaner"'})

    def test_appends_slot_colors(self):
        st = {"moonraker_url": "http://m", "filament_slots": {"1": {"color": "white"}, "4": {"color": "dark green"}}}
        with mock.patch.object(model_job, "request_json"):
            self.assertEqual(model_job._announce_print_name(st, {"title": "Cleaner"}, [4, 1]),
                             "Cleaner (Dark Green + White)")
        with mock.patch.object(model_job, "request_json"):
            self.assertEqual(model_job._announce_print_name(st, {"title": "Cleaner", "filament_selection": {"color": "pink"}}),
                             "Cleaner (Pink)")

    def test_failure_never_blocks_print(self):
        with mock.patch.object(model_job, "request_json", side_effect=OSError("down")):
            self.assertIsNone(model_job._announce_print_name({"moonraker_url": "http://m"}, {"title": "X"}))

    def test_numbers_multi_plate_parts(self):
        st = {"moonraker_url": "http://m", "filament_slots": {"2": {"color": "pink"}}}
        job = {"title": "Holder", "derived_plates": [{"gcode": "/a/p1.gcode"}, {"gcode": "/b/p1.gcode"}]}
        with mock.patch.object(model_job, "request_json"):
            self.assertEqual(model_job._announce_print_name(st, job, [2], Path("/b/p1.gcode")), "Holder 2/2 (Pink)")


def test_assign_print_number_appends_once_and_ignores_scratch_jobs(tmp_path, monkeypatch):
    print_control = external('print_control')
    archive = tmp_path / 'archive'
    job = archive / 'ghost--abc' / 'JOB.json'
    job.parent.mkdir(parents=True)
    job.write_text('{}')
    numbers = tmp_path / 'print_numbers.json'
    numbers.write_text('{"numbers": {"/old/JOB.json": 4}}')
    monkeypatch.setattr(print_control, 'ARCHIVE', archive)
    monkeypatch.setattr(print_control, 'PRINT_NUMBERS', numbers)
    assert model_job._assign_print_number(job) == 5
    assert model_job._assign_print_number(job) == 5
    scratch = tmp_path / 'scratch' / 'JOB.json'
    scratch.parent.mkdir()
    scratch.write_text('{}')
    assert model_job._assign_print_number(scratch) is None
    assert model_job._assign_print_number(None) is None


def test_shared_extruder_total_is_split_across_parts_and_checked_against_model_page():
    """Valentines stand regression: 5 parts on one guessed extruder showed 5 x 247.8 g = 1,239 g."""
    from print_handy.core import launch_state, model_job as mj
    parts = [{'key': f'p{i}', 'source_extruder': 1, 'volume_mm3': v} for i, v in enumerate([10, 10, 30, 50, 100])]
    manifest = {'parts': parts, 'filament_stats': {'total_g': 247.8, 'extruders': {1: {'g': 247.8}}, 'estimated': True}}
    pick = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 13}
    job = {'color_manifest': manifest,
           'selected_source': {'plates': [{'prediction_s': 3600, 'weight_g': 100}, {'prediction_s': 600, 'weight_g': 60}]},
           'color_decisions': {'mode': 'per_part', 'choices': {f'part:p{i}': dict(pick) for i in range(5)}}}
    usage = mj.filament_usage_by_selection(job)
    assert len(usage) == 1 and abs(usage[0][1] - 160.0) < 0.01  # model page weight, not 1,239 g
    del job['selected_source']
    assert abs(mj.filament_usage_by_selection(job)[0][1] - 247.8) < 0.01  # never N x the total
    assert mj.source_site_estimate({'plates': [{'prediction_s': 3600, 'weight_g': 100}]}) == (3600.0, 100.0)
