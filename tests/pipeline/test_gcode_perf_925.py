"""Phase 3 G-code scan performance (#925): identical outcomes, fewer passes.

The three optimizations pinned here:

1. ``used_tools`` / ``_gcode_tools`` make one streaming pass that pays the
   ``strip()`` + ``fullmatch`` only for candidate lines (first character T/M
   or whitespace) instead of for every line. The old per-line algorithm is
   reproduced below as the reference; the optimized functions must agree
   with it on every case, including tool lines at the file start/end, CRLF
   files and heater-only files.
2. ``_verify_routed`` skips the G-code body re-parse at launch when the job
   already verified this exact artifact (``gcode_verified`` plus a matching
   ``artifact_binding.gcode_sha256``). Any other state falls back to the full
   verification — the skip never applies to unverified or changed artifacts.
3. ``gcode_3mf.plate_metadata`` reads its header/footer comments from the
   head and tail slices only, and ``package`` still produces a readable
   package whose inner G-code is byte-identical.

Motion bounds (the parse inside ``verify_gcode_offline``) get the same
treatment: outputs and refusal messages must match the original semantics.
"""
import hashlib
import re
import zipfile
from pathlib import Path

import pytest

from print_handy.core import gcode_3mf, model_job, printer_state
from print_handy.printers.flashforge.zmod import multicolor_contract as contract

# ---------------------------------------------------------------- used_tools

_TOOL_LINE = re.compile(r'T(\d+)(?:\s*;.*)?')
_HEATER_TOOL_LINE = re.compile(r'M10[49].*\bT(\d+)\b.*')


def _reference_used_tools(text):
    """The pre-#925 per-line algorithm, kept as the equivalence oracle."""
    tools = {int(m.group(1)) for line in text.splitlines()
             if (m := _TOOL_LINE.fullmatch(line.strip()))}
    if not tools:
        tools = {int(m.group(1)) for line in text.splitlines()
                 if (m := _HEATER_TOOL_LINE.fullmatch(line.strip()))}
    return tools


USED_TOOL_CASES = [
    "",
    "T0\n",
    "T0",
    "G90\nT3",
    "T2\nG1 X10 Y10\n",
    "  T2  \nG1 X1\n",
    "\tT1\t\n",
    "T1 ; swap\n",
    "T1;swap\n",
    "T10\nT0\n",
    "G1 X10 T1\n",
    "T1 X5\n",
    "; T1\n;T2\n",
    "t1\n",
    "M104 S210 T1\n",
    "M109 S210 T2\nG1 X1\n",
    "M104 S210 T1\nT0\n",
    "M104 S210\n",
    "T1\r\nT2\r\nG1 X1\r\n",
    "T1\r\n",
    "G1 X10 Y10 E1.5\n" * 50 + "T4\n" + "G1 X1\n" * 50,
    "M104 S210 T0\nM190 S60\n",
]


@pytest.mark.parametrize("text", USED_TOOL_CASES)
def test_used_tools_matches_the_per_line_reference(text):
    assert contract.used_tools(text) == _reference_used_tools(text)


@pytest.mark.parametrize("text", USED_TOOL_CASES)
def test_gcode_tools_matches_the_per_line_reference(text):
    # model_job._gcode_tools shares the bare-Tn semantics, without the
    # heater-declaration fallback multicolor_contract adds.
    expected = {int(m.group(1)) for line in text.splitlines()
                if (m := _TOOL_LINE.fullmatch(line.strip()))}
    assert model_job._gcode_tools(text) == expected


def test_used_tools_on_a_dense_generated_file():
    lines = ["; header", "M104 S210 T0", "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210"]
    for i in range(5000):
        lines.append(f"G1 X{i % 200}.5 Y{(i * 7) % 200}.25 E{i * 0.01:.2f} F3000")
        if i % 1000 == 0:
            lines.append(f"T{i // 1000}")
    lines.append("T5 ; final tool, no trailing newline variant")
    text = "\n".join(lines)
    assert contract.used_tools(text) == _reference_used_tools(text) == {0, 1, 2, 3, 4, 5}
    assert model_job._gcode_tools(text) == {0, 1, 2, 3, 4, 5}


# ------------------------------------------------------------- motion bounds

def test_motion_bounds_absolute_basics():
    text = "G90\nG1 X10 Y20 Z0.2\nG1 X-5 Y30\nG0 Z5\n"
    assert model_job.gcode_motion_bounds(text) == {
        "x": (-5.0, 10.0), "y": (20.0, 30.0), "z": (0.2, 5.0)}


def test_motion_bounds_comment_tokens_do_not_count():
    text = "G90\nG1 X10 Y10 Z0.2 ; X999 Y999 Z999\n; G1 X500 Y500 Z500\n"
    assert model_job.gcode_motion_bounds(text) == {
        "x": (10.0, 10.0), "y": (10.0, 10.0), "z": (0.2, 0.2)}


def test_motion_bounds_glued_and_partial_number_tokens():
    # No space between axis words, and a number the original NUMBER pattern
    # reads as a prefix: both must keep the original reading.
    text = "G90\nG1 X10Y20 Z0.2\nG1 X1.2.3\n"
    bounds = model_job.gcode_motion_bounds(text)
    assert bounds["x"] == (1.2, 10.0)
    assert bounds["y"] == (20.0, 20.0)


def test_motion_bounds_relative_mode():
    text = "G90\nG1 X10 Y10 Z1\nG91\nG1 X5 Y-2 Z0.5\nG90\nG1 X0\n"
    assert model_job.gcode_motion_bounds(text) == {
        "x": (0.0, 15.0), "y": (8.0, 10.0), "z": (1.0, 1.5)}


def test_motion_bounds_relative_before_absolute_refused():
    with pytest.raises(RuntimeError, match=r"line 2: relative X motion before an absolute X position"):
        model_job.gcode_motion_bounds("G91\nG1 X5 Y5 Z5\n")


def test_motion_bounds_forbidden_and_arc_refusals_keep_line_numbers():
    with pytest.raises(RuntimeError, match=r"line 3: unsupported motion-affecting command G28"):
        model_job.gcode_motion_bounds("G90\nG1 X1 Y1 Z1\nG28\n")
    with pytest.raises(RuntimeError, match=r"line 2: G92 changes XYZ coordinates"):
        model_job.gcode_motion_bounds("G90\nG92 X10\nG1 X1 Y1 Z1\n")
    with pytest.raises(RuntimeError, match=r"line 2: arc motion is unsupported"):
        model_job.gcode_motion_bounds("G90\nG2 X10 Y10 I5 J5\n")


def test_motion_bounds_g92_extruder_only_is_benign():
    text = "G90\nG92 E0\nG1 X10 Y10 Z0.2 E5\n"
    assert model_job.gcode_motion_bounds(text)["x"] == (10.0, 10.0)


def test_motion_bounds_no_motion_and_missing_axis():
    with pytest.raises(RuntimeError, match="no bounded XYZ motion found"):
        model_job.gcode_motion_bounds("G90\nM104 S210\n")
    with pytest.raises(RuntimeError, match="missing bounded motion for axis Z"):
        model_job.gcode_motion_bounds("G90\nG1 X10 Y10 E5\n")


def test_motion_bounds_lowercase_and_crlf():
    text = "g90\r\ng1 x10 y20 z0.2\r\ng1 x30\r\n"
    assert model_job.gcode_motion_bounds(text) == {
        "x": (10.0, 30.0), "y": (20.0, 20.0), "z": (0.2, 0.2)}


def test_motion_bounds_empty_and_tiny():
    with pytest.raises(RuntimeError, match="no bounded XYZ motion found"):
        model_job.gcode_motion_bounds("")
    with pytest.raises(RuntimeError, match="missing bounded motion for axis"):
        model_job.gcode_motion_bounds("G1 X1\n")


# ------------------------------------------------------- verified-artifact skip

GCODE = '; filament_type = PLA\nM190 S60\nM104 S210\nG1 X10 Y10\n'


class _CountingAdapter:
    connection = 'offline'
    transport = None
    label = 'Offline test printer'

    def __init__(self):
        self.calls = 0

    def gcode_problems(self, text, state):
        self.calls += 1
        return []


def _verified_job(path, sha=None):
    return {'gcode': str(path), 'gcode_verified': True,
            'artifact_binding': {'gcode_sha256': sha if sha is not None
                                 else hashlib.sha256(Path(path).read_bytes()).hexdigest()}}


@pytest.fixture
def skip_rig(tmp_path, monkeypatch):
    path = tmp_path / 'plate_1.gcode'
    path.write_text(GCODE)
    monkeypatch.setattr(model_job.printer_state, 'check', lambda *a, **k: (True, [], {}))
    monkeypatch.setattr(printer_state, 'gcode_temperatures',
                        lambda state, materials, fetch=None, text=None: {'nozzle': 210, 'bed': 60})
    adapter = _CountingAdapter()
    state = {'temps_c': {'nozzle': 210, 'bed': 60},
             'spool': {'material': 'PLA', 'spoolman_id': 7},
             'nozzle': {'diameter_mm': 0.4, 'type': 'hardened_steel'}}
    return path, adapter, state


def test_routed_verify_skips_body_parse_for_a_verified_unchanged_artifact(skip_rig):
    path, adapter, state = skip_rig
    result = model_job._verify_routed(path, adapter, state, 'p1', job=_verified_job(path))
    assert result == {'offline': True, 'connection': 'offline', 'skipped_reparse': True}
    assert adapter.calls == 0  # the G-code body was never re-parsed


def test_routed_verify_reparses_when_the_sha_no_longer_matches(skip_rig):
    path, adapter, state = skip_rig
    job = _verified_job(path)
    path.write_text(GCODE + 'G1 X20\n')  # artifact changed after verification
    result = model_job._verify_routed(path, adapter, state, 'p1', job=job)
    assert 'skipped_reparse' not in result
    assert adapter.calls == 1


def test_routed_verify_reparses_without_the_verified_flag(skip_rig):
    path, adapter, state = skip_rig
    job = _verified_job(path)
    job['gcode_verified'] = False
    result = model_job._verify_routed(path, adapter, state, 'p1', job=job)
    assert 'skipped_reparse' not in result
    assert adapter.calls == 1


def test_routed_verify_reparses_without_a_binding(skip_rig):
    path, adapter, state = skip_rig
    job = _verified_job(path)
    del job['artifact_binding']
    result = model_job._verify_routed(path, adapter, state, 'p1', job=job)
    assert 'skipped_reparse' not in result
    assert adapter.calls == 1


def test_routed_verify_skip_supports_a_split_job_plate_hash(skip_rig):
    path, adapter, state = skip_rig
    sha = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    job = {'gcode': '/elsewhere/first.gcode', 'gcode_verified': True,
           'derived_plates': [{'gcode': str(path), 'slot': 1}],
           'artifact_binding': {'gcode_sha256': {'1': sha}}}
    result = model_job._verify_routed(path, adapter, state, 'p1', job=job)
    assert result.get('skipped_reparse') is True
    assert adapter.calls == 0


def test_routed_verify_never_skips_without_a_job(skip_rig):
    path, adapter, state = skip_rig
    result = model_job._verify_routed(path, adapter, state, 'p1', job=None)
    assert 'skipped_reparse' not in result
    assert adapter.calls == 1


# ------------------------------------------------------------- package metadata

def _orca_like_text(head_extra="", tail_extra="", body_lines=2000):
    head = ("; generated by OrcaSlicer 2.3.0\n"
            "; printer_model = Flashforge AD5X\n"
            "; filament_type = PLA;PETG\n"
            "; filament_colour = #FF0000;#00FF00\n"
            "; filament used [g] = 20.00, 15.50\n"
            "; estimated printing time (normal mode) = 1h 2m 3s\n"
            + head_extra)
    body = "".join(f"G1 X{i % 200} Y{(i * 3) % 200} E{i * 0.01:.2f}\n" for i in range(body_lines))
    tail = "; footer config follows\n" + tail_extra
    return head + body + tail


def test_plate_metadata_reads_the_head():
    meta = gcode_3mf.plate_metadata(_orca_like_text())
    assert meta["filaments"] == [
        {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": 20.0},
        {"id": 2, "type": "PETG", "color": "#00FF00", "used_g": 15.5}]
    assert meta["prediction"] == 3723
    assert meta["weight"] == 35.5
    assert gcode_3mf._printer_model_id(_orca_like_text()) == "AD5X"


def test_plate_metadata_reads_the_tail_when_the_head_lacks_a_key():
    # Orca repeats its full config in the footer; a key that only lives there
    # must still be found, and a key in both places keeps its head (first)
    # value, exactly as a whole-text search reported it.
    text = _orca_like_text(tail_extra="; filament_type = ABS\n; total estimated time: 0h 10m 0s\n")
    meta = gcode_3mf.plate_metadata(text)
    assert meta["filaments"][0]["type"] == "PLA"  # head wins, as before
    tail_only = ("G1 X1 Y1\n" * 10) + "; filament_type = ABS\n; filament used [g] = 7.25\n"
    meta = gcode_3mf.plate_metadata(tail_only)
    assert meta["filaments"] == [{"id": 1, "type": "ABS", "used_g": 7.25}]
    assert meta["weight"] == 7.25


def test_plate_metadata_empty_and_tiny():
    assert gcode_3mf.plate_metadata("") == {"index": 1, "filaments": []}
    assert gcode_3mf.plate_metadata("; filament_type = PLA\n")["filaments"] == [
        {"id": 1, "type": "PLA"}]


def test_package_round_trip_keeps_the_gcode_byte_identical(tmp_path):
    source = tmp_path / "plate_1.gcode"
    text = _orca_like_text()
    source.write_text(text)
    target = gcode_3mf.package(source, tmp_path / "plate_1.gcode.3mf")
    with zipfile.ZipFile(target) as archive:
        assert archive.read("Metadata/plate_1.gcode") == text.encode()
        meta = archive.read("Metadata/plate_1.json").decode()
    assert '"type": "PLA"' in meta
    # Deterministic: the same G-code packs to the same package bytes.
    again = gcode_3mf.package(source, tmp_path / "again.gcode.3mf")
    assert Path(again).read_bytes() == Path(target).read_bytes()
    # And the packaged artifact reads back through the package reader.
    assert gcode_3mf.read_gcode_text(target) == text


def test_package_of_an_empty_file(tmp_path):
    source = tmp_path / "plate_1.gcode"
    source.write_bytes(b"")
    target = gcode_3mf.package(source, tmp_path / "plate_1.gcode.3mf")
    with zipfile.ZipFile(target) as archive:
        assert archive.read("Metadata/plate_1.gcode") == b""


# ------------------------------------------------------------------ large file

def test_large_file_scan_agrees_with_reference(tmp_path):
    """A 2M-line dense file: the single-pass scans return the old answers."""
    path = tmp_path / "big.gcode"
    with path.open("w") as fh:
        fh.write("; filament_type = PLA\nM104 S210 T0\nG90\n")
        fh.write("T0\n")
        for i in range(120_000):
            fh.write(f"G1 X{i % 210}.5 Y{(i * 13) % 210}.25 Z{0.2 + (i // 60000) * 0.2:.3f} E{i * 0.01:.2f}\n")
            if i == 60_000:
                fh.write("T1 ; mid-file tool change\n")
        fh.write("END_PRINT\n")
    text = path.read_text()
    assert contract.used_tools(text) == _reference_used_tools(text) == {0, 1}
    bounds = model_job.gcode_motion_bounds(text)
    assert bounds["x"] == (0.5, 209.5)
    assert bounds["z"] == (0.2, 0.4)
