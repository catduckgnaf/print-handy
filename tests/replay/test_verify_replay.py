"""verify_gcode against the recorded AD5X and Spoolman (#689, #692).

2026-10-31: a Silk PLA spool in IFS slot 2 sliced at 210 C instead of 220 C (PLA 210 + the Silk
adjustment), and Orca quotes the material it writes ("Silk PLA"). The G-code here carries the real
header and config block of an AD5X Silk PLA slice; verify_gcode reads the live recorded printer state
(Klipper config, saved variables, active spool) and Spoolman spool #17 over HTTP.
"""
import pytest

from print_handy.core import model_job
from print_handy.gcode import verify as gcode_verify

from .helpers import FIXTURES, sliced_gcode


def write(tmp_path, nozzle):
    path = tmp_path / f"silk_{nozzle}.gcode"
    path.write_text(sliced_gcode(nozzle))
    return path


def test_the_recorded_slice_quotes_its_material():
    text = (FIXTURES / "silk_pla_slot2_sliced.gcode.txt").read_text()
    assert '; filament_type = "Silk PLA"' in text
    assert '; print_handy_tool_slots = {"0":{"physical_slot":2,"spoolman_id":17}}' in text


def test_silk_pla_sliced_at_220_passes_against_the_live_state(ad5x, tmp_path):
    # The hardware record still holds the stale 210/60 an earlier PLA spool left; the job's own spool decides.
    assert model_job.printer_state.load()["temps_c"] == {"nozzle": 210, "bed": 60}
    gcode_verify.verify_gcode(write(tmp_path, 220))
    assert any(path == "/api/v1/spool/17" for _m, path, _q, _b in ad5x.requests)  # Spoolman was asked


def test_silk_pla_sliced_at_the_stale_210_is_refused(ad5x, tmp_path):
    with pytest.raises(RuntimeError, match=r"missing START_PRINT/PRINT_START matching bed 60 and effective hotend 220"):
        gcode_verify.verify_gcode(write(tmp_path, 210))


def test_print_now_refuses_the_stale_slice_before_anything_is_sent(ad5x, tmp_path):
    from .helpers import make_job, run_print_now
    job, _ = make_job(tmp_path, nozzle=210)
    ok, tail = run_print_now(job)
    assert not ok and "effective hotend 220" in tail
    assert ad5x.uploads == [] and ad5x.zcolor() == []
