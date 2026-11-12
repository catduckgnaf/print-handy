"""Multi-slot prints charge each slot's spool when the print ends.

The case: a two-color AD5X print mapped tool 0 -> slot 2 and tool 1 -> slot 3
with the G-code header `; filament used [g] = 6.80, 28.15`. The launch cleared
Moonraker's active spool (one spool cannot carry a multi-color job), and
nothing charged the spools afterwards: the black spool read the same weight
before and after several multi-color prints. These tests drive the charging
module with a fake Spoolman; test_multislot_usage_listener.py drives the
listener's end transition.
"""
import json

import pytest

from print_handy.core import spoolman_client
from print_handy.runtime import multislot_usage

FILE = "dragon-two-color.gcode"
PLANNED_MM = 11627.68


def _context(**extra):
    ctx = {
        "job_key": FILE,
        "print_number": 41,
        "started_at": "2026-11-10T14:00:00Z",
        "tool_to_physical_slot": {"0": 2, "1": 3},
        "slicer_settings": {
            "filament_used_g_list": [6.8, 28.15],
            "mapped_materials": {"0": {"spoolman_id": 12, "material": "PLA"},
                                 "1": {"spoolman_id": 15, "material": "PLA"}},
        },
        "planned_filament_mm": PLANNED_MM,
    }
    ctx.update(extra)
    return ctx


class FakeSpoolman:
    def __init__(self, fail_status=None, down=False):
        self.calls = []
        self.down = down
        self.fail_status = fail_status

    def __call__(self, method, path, body=None, **kw):
        if self.down:
            raise spoolman_client.SpoolmanUnavailable("connection refused")
        if self.fail_status:
            raise spoolman_client.SpoolmanUnavailable("HTTP 404", status=self.fail_status)
        self.calls.append((method, path, body))
        return {"id": int(path.split("/")[4])}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "multislot_usage_charges.json"
    monkeypatch.setenv(multislot_usage.LEDGER_ENV, str(path))
    return path


def test_completed_print_charges_each_slot_its_tool_grams_once(ledger):
    fake = FakeSpoolman()
    entry = multislot_usage.charge_finished_print(_context(), "complete", request=fake)
    assert fake.calls == [("PUT", "/api/v1/spool/12/use", {"use_weight": 6.8}),
                          ("PUT", "/api/v1/spool/15/use", {"use_weight": 28.15})]
    assert entry["status"] == "charged"
    # A duplicate end event or a listener restart charges nothing more.
    again = multislot_usage.charge_finished_print(_context(), "complete", request=fake)
    assert len(fake.calls) == 2
    assert again["status"] == "charged"
    assert json.loads(ledger.read_text())[f"{FILE}#41"]["status"] == "charged"


def test_a_reprint_of_the_same_file_is_a_new_charge(ledger):
    fake = FakeSpoolman()
    multislot_usage.charge_finished_print(_context(), "complete", request=fake)
    multislot_usage.charge_finished_print(_context(print_number=42), "complete", request=fake)
    assert len(fake.calls) == 4


def test_stopped_print_is_scaled_by_filament_actually_used(ledger):
    fake = FakeSpoolman()
    multislot_usage.charge_finished_print(_context(), "stopped", actual_mm=PLANNED_MM * 0.4, request=fake)
    grams = [body["use_weight"] for _, _, body in fake.calls]
    assert grams == [pytest.approx(2.72, abs=0.01), pytest.approx(11.26, abs=0.01)]


def test_stopped_print_with_unknown_use_charges_nothing(ledger):
    fake = FakeSpoolman()
    entry = multislot_usage.charge_finished_print(_context(), "stopped", actual_mm=None, request=fake)
    assert fake.calls == []
    assert entry["status"] == "skipped"


def test_open_spool_question_suspends_the_charge(ledger):
    fake = FakeSpoolman()
    entry = multislot_usage.charge_finished_print(_context(), "complete", suspended=True, request=fake)
    assert fake.calls == []
    assert "paused" in multislot_usage.summary_line(entry)
    # Suspended is final for this print: a later call does not charge it.
    multislot_usage.charge_finished_print(_context(), "complete", request=fake)
    assert fake.calls == []


def test_spoolman_outage_stays_pending_and_is_retried_once(ledger):
    down = FakeSpoolman(down=True)
    entry = multislot_usage.charge_finished_print(_context(), "complete", request=down)
    assert entry["status"] == "pending"
    assert "not yet charged" in multislot_usage.summary_line(entry)
    up = FakeSpoolman()
    assert multislot_usage.retry_pending(request=up) == [f"{FILE}#41"]
    assert len(up.calls) == 2
    assert multislot_usage.retry_pending(request=up) == []
    assert len(up.calls) == 2


def test_spool_spoolman_refuses_is_not_retried(ledger):
    entry = multislot_usage.charge_finished_print(_context(), "complete", request=FakeSpoolman(fail_status=404))
    assert entry["status"] == "failed"
    assert multislot_usage.retry_pending(request=FakeSpoolman()) == []


def test_single_slot_job_is_left_to_moonraker(ledger):
    fake = FakeSpoolman()
    ctx = _context(tool_to_physical_slot={"0": 2})
    assert multislot_usage.charge_finished_print(ctx, "complete", request=fake) is None
    assert multislot_usage.charge_finished_print({}, "complete", request=fake) is None
    assert fake.calls == []


def test_non_contiguous_tools_use_their_own_grams(ledger):
    fake = FakeSpoolman()
    ctx = _context(tool_to_physical_slot={"0": 1, "2": 4},
                   slicer_settings={"filament_used_g_list": [5.0, 0.0, 9.5],
                                    "mapped_materials": {"0": {"spoolman_id": 7}, "2": {"spoolman_id": 9}}})
    multislot_usage.charge_finished_print(ctx, "complete", request=fake)
    assert fake.calls == [("PUT", "/api/v1/spool/7/use", {"use_weight": 5.0}),
                          ("PUT", "/api/v1/spool/9/use", {"use_weight": 9.5})]


def test_summary_line_names_each_slot_charge(ledger):
    entry = multislot_usage.charge_finished_print(_context(), "complete", request=FakeSpoolman())
    assert multislot_usage.summary_line(entry) == "Spool usage: slot 2 spool #12 6.8 g; slot 3 spool #15 28.15 g"


def test_planned_filament_mm_from_header():
    text = "; filament used [mm] = 2262.45, 9365.23\n; filament used [g] = 6.80, 28.15\n"
    assert multislot_usage.planned_filament_mm(text) == pytest.approx(11627.68)
    assert multislot_usage.planned_filament_mm("G1 X0\n") is None
