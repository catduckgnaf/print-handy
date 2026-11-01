"""#716: color plate export refusals are a named slicing failure, never unknown."""
import pytest

from print_handy import diagnostics


@pytest.mark.parametrize("message", [
    "single-color plate requires production model settings",
    "production subset expects at least one component per build object",
    "geometry and selection inventory disagree",
])
def test_color_plate_refusal_is_a_known_cause(message):
    known = diagnostics.known_cause(RuntimeError(message))
    assert known is not None
    assert known.category is not diagnostics.Category.UNKNOWN
    assert "One color for everything" in known.remedy
