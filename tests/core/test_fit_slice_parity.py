"""Shared conservative footprint checks keep fit and slice prechecks aligned."""
import pytest

from print_handy.core import orca_adapter as oa


@pytest.mark.parametrize('dims,bed,height,margin,expected', [
    ((231, 74, 85), (220, 220), 220, 1, False),  # authored long bar; only a yaw can fit
    ((180, 150, 40), (220, 220), 0, 1, True),
    ((20, 20, 221), (220, 220), 0, 1, False),
    ((200, 200, 20), (220, 220), 1, 1, True),  # centered placement ignores authored origin
    ((219, 100, 20), (220, 220), 1, 1, False),  # single-copy edge reserve
    ((200, 200, 20), (220, 220), 3, 1, True),  # multi-copy margins are separate
])
def test_fit_envelope_precheck(dims, bed, height, margin, expected):
    assert oa.fit_envelope_fits(dims, bed, height, margin_mm=margin) is expected


def test_fit_envelope_uses_scale_and_rotated_dimensions():
    assert oa.fit_envelope_fits((150, 74, 85), (220, 220), 220, margin_mm=1)
    assert not oa.fit_envelope_fits((150, 74, 85), (220, 220), 220, margin_mm=1, scale=1.5)
