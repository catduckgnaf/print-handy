from datetime import datetime, timezone
from pathlib import Path

from print_handy.core import model_job
from print_handy.hermes import print_records as records


def test_normalize_utc_handles_various_formats():
    # Epoch numbers
    epoch = 1762354187  # 2025-11-05T14:49:47Z
    assert records.normalize_utc(epoch) == "2025-11-05T14:49:47Z"
    assert records.normalize_utc(float(epoch)) == "2025-11-05T14:49:47Z"
    assert records.normalize_utc(str(epoch)) == "2025-11-05T14:49:47Z"

    # Invalid / empty / bool
    assert records.normalize_utc(None) is None
    assert records.normalize_utc(True) is None
    assert records.normalize_utc(False) is None
    assert records.normalize_utc(0) is None
    assert records.normalize_utc(-10) is None
    assert records.normalize_utc("") is None
    assert records.normalize_utc("   ") is None
    assert records.normalize_utc("invalid-date") is None

    # ISO string with Z
    assert records.normalize_utc("2026-11-05T14:49:47Z") == "2026-11-05T14:49:47Z"
    assert records.normalize_utc("2026-11-05T14:49:47z") == "2026-11-05T14:49:47Z"

    # ISO string with numeric offset (-05:00 EST)
    assert records.normalize_utc("2026-11-05T09:49:47-05:00") == "2026-11-05T14:49:47Z"
    assert records.normalize_utc("2026-11-05T09:49:47-0500") == "2026-11-05T14:49:47Z"

    # Naive ISO string assumes UTC
    assert records.normalize_utc("2026-11-05T14:49:47") == "2026-11-05T14:49:47Z"

    # Datetime objects
    dt_utc = datetime(2026, 11, 5, 14, 49, 47, tzinfo=timezone.utc)
    assert records.normalize_utc(dt_utc) == "2026-11-05T14:49:47Z"
    dt_naive = datetime(2026, 11, 5, 14, 49, 47)
    assert records.normalize_utc(dt_naive) == "2026-11-05T14:49:47Z"


def test_build_record_data_normalizes_timestamps():
    # Context with offset-bearing started_at
    data = records.build_record_data(
        "test.gcode",
        stats={"state": "complete"},
        context={"started_at": "2026-11-05T09:49:47-05:00"},
        completed_at="2026-11-05T10:00:00-05:00",
    )
    assert data["started_at"] == "2026-11-05T14:49:47Z"
    assert data["completed_at"] == "2026-11-05T15:00:00Z"

    # Stats with start_time epoch
    epoch = 1762354187
    data2 = records.build_record_data(
        "test.gcode",
        stats={"state": "complete", "start_time": epoch},
        completed_at="2026-11-05T14:49:47Z",
    )
    assert data2["started_at"] == "2025-11-05T14:49:47Z"
    assert data2["completed_at"] == "2026-11-05T14:49:47Z"


def test_model_job_card_started_at_is_utc_z():
    card = model_job.job_card({"title": "Test Card"}, "test.gcode")
    assert "started_at" in card
    assert card["started_at"].endswith("Z")
    assert records.normalize_utc(card["started_at"]) == card["started_at"]
