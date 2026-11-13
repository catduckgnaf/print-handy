"""#1373: "without supports" never reuses a first slice that was made with supports.

Found by re-running the cute-dino-mmu corpus case: the operator chose no
supports, JOB.json recorded it, and the started G-code still said
``enable_support = 1``. The first background slice runs before any decision,
so a designer project with supports on (or painted enforcers) slices with
supports, and the "off" choice took that artifact as it stood.
"""
import asyncio
import json

from print_handy.core.background_slice import BackgroundSlice, _sliced_with_supports


def _gcode(path, enable_support):
    path.write_text(f"G1 X0\n; CONFIG_BLOCK_START\n; enable_support = {enable_support}\n; CONFIG_BLOCK_END\n")
    return str(path)


def test_sliced_with_supports_reads_the_gcode_config(tmp_path):
    assert _sliced_with_supports([_gcode(tmp_path / 'a.gcode', 1)]) is True
    assert _sliced_with_supports([_gcode(tmp_path / 'b.gcode', 0)]) is False
    assert _sliced_with_supports(None) is False
    assert _sliced_with_supports([str(tmp_path / 'missing.gcode')]) is False


def test_take_no_supports_refuses_a_slice_made_with_supports(tmp_path):
    job = tmp_path / 'JOB.json'
    job.write_text(json.dumps({'support_decision': {'supports': False, 'by': 'operator'}}))
    bg = BackgroundSlice()
    bg.job_path = job
    bg.job_no_supports = {'title': 'dino'}
    bg.result = [_gcode(tmp_path / 'plate_1.gcode', 1)]
    assert asyncio.run(bg.take_no_supports()) is None
    # The operator's recorded decision is left as it was.
    assert json.loads(job.read_text())['support_decision']['supports'] is False


def test_take_no_supports_keeps_a_real_no_supports_slice(tmp_path):
    job = tmp_path / 'JOB.json'
    job.write_text(json.dumps({}))
    bg = BackgroundSlice()
    bg.job_path = job
    bg.job_no_supports = {'title': 'dino'}
    bg.result = [_gcode(tmp_path / 'plate_1.gcode', 0)]
    assert asyncio.run(bg.take_no_supports()) == bg.result
