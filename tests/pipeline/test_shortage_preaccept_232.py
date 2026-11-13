"""The Ready screen's shown shortage covers only what it showed, and never a yolo run.

2.3.2 review (finding 14): the tolerance and the yolo skip had no test of their own.
A print button under a shown "short by" line pre-accepts that one spool, but only while
the slice needs no more than was shown (0.5 g rounding). A yolo run is never
pre-accepted, so the start gate still posts its "YOLO WAIVED: filament amount" line.
"""
import json
from unittest import mock

import pytest

from print_handy.core import prepared_intake, yolo
from print_handy.runtime import discord_print_summary as dps

KEY = "PLA|2|19"


@pytest.fixture
def job(tmp_path):
    path = tmp_path / "JOB.json"
    path.write_text(json.dumps({"title": "Lantern"}))
    return path


def _with_slice(grams):
    return mock.patch.object(dps, "plate_estimates", return_value=[{"plate": 1, "filament_g": grams}])


@pytest.mark.parametrize("sliced_g, accepted", [
    (20.0, True),    # what was shown
    (20.5, True),    # rounding
    (20.6, False),   # more than shown: ask at the start
    (30.0, False),   # the 2.3.2 bug: 1.25x + 5 g let this through unasked
])
def test_a_shown_shortage_covers_only_what_was_shown(job, sliced_g, accepted):
    with _with_slice(sliced_g):
        got = prepared_intake._preaccepted_shortage(0, [(KEY, 20.0)], job)
    assert got == ([KEY] if accepted else None)


def test_a_yolo_run_is_never_preaccepted_so_its_waiver_is_posted(job):
    token = yolo.ACTIVE.set(True)
    try:
        with _with_slice(10.0):
            assert prepared_intake._preaccepted_shortage(0, [(KEY, 20.0)], job) is None
    finally:
        yolo.ACTIVE.reset(token)
    with _with_slice(10.0):  # the same answer outside yolo is pre-accepted
        assert prepared_intake._preaccepted_shortage(0, [(KEY, 20.0)], job) == [KEY]


def test_slice_only_two_short_spools_or_unknown_grams_are_asked_at_the_start(job):
    with _with_slice(10.0):
        assert prepared_intake._preaccepted_shortage(1, [(KEY, 20.0)], job) is None
        assert prepared_intake._preaccepted_shortage(0, [(KEY, 20.0), ("PLA|1|7", 5.0)], job) is None
        assert prepared_intake._preaccepted_shortage(0, [], job) is None
    with _with_slice(None):
        assert prepared_intake._preaccepted_shortage(0, [(KEY, 20.0)], job) is None
