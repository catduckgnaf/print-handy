"""Journeys that slice from state built by the real create_spool and bind_slot flows."""
import json

import pytest

from . import harness, spool_fixture


def _case(name):
    return next(harness.load_case(f) for f in harness.case_dirs() if f.name == name)


def test_built_state_has_four_bound_slots(tmp_path):
    path, spoolman = spool_fixture.build_state(tmp_path, harness.FIXTURE_STATE)
    slots = json.loads(path.read_text())["filament_slots"]
    assert sorted(slots) == ["1", "2", "3", "4"]
    assert [slots[k]["spoolman_id"] for k in "1234"] == [1, 2, 3, 4]
    assert {s["location"] for s in spoolman.spools} == {f"IFS Slot {n}" for n in (1, 2, 3, 4)}
    assert all(s.get("hex") for s in slots.values())


def test_four_color_journey_slices_from_built_state(tmp_path):
    path, _ = spool_fixture.build_state(tmp_path, harness.FIXTURE_STATE)
    case = _case("four-colors-one-plate")
    result = harness.run_case(case, tmp_path / "run", state_file=path)
    assert result.receipt["status"] == "STARTED"
    assert len(result.gcodes) == 1


def test_bind_slot_during_spoolman_503_changes_nothing_then_succeeds(tmp_path):
    from print_handy.hermes import spool_sync
    path, spoolman = spool_fixture.build_state(tmp_path, harness.FIXTURE_STATE, spools=spool_fixture.SPOOLS[:1])
    before = path.read_text()
    spoolman.fail_first = 1
    with pytest.raises(spool_sync.SpoolError, match="503"):
        spool_sync.bind_slot(2, 1, client=spoolman, mr=spool_fixture.fake_moonraker, state_path=path,
                             push_screen_update=False)
    assert path.read_text() == before, "a refused bind must not touch the saved state"
    assert not any(m == "PATCH" for m, _ in spoolman.calls[-1:])
