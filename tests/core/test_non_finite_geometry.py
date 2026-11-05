"""NaN/inf coordinates are refused at intake with a clear error, not measured as nan/inf extents."""
import struct

import pytest

from print_handy.core import model_job

GOOD = [((0, 0, 0), (10, 0, 0), (0, 10, 0)), ((0, 0, 0), (0, 0, 10), (10, 0, 0)),
        ((0, 0, 0), (0, 10, 0), (0, 0, 10)), ((10, 0, 0), (0, 0, 10), (0, 10, 0))]


def _stl(path, tris):
    data = b"\0" * 80 + struct.pack("<I", len(tris))
    for tri in tris:
        data += struct.pack("<12fH", 0, 0, 0, *[c for v in tri for c in v], 0)
    path.write_bytes(data)
    return path


def test_finite_model_still_measures(tmp_path):
    info = model_job.model_info_output(_stl(tmp_path / "ok.stl", GOOD))
    assert "size_x = 10.00" in info


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_coordinate_is_refused(tmp_path, bad):
    path = _stl(tmp_path / "bad.stl", GOOD + [((bad, 0, 0), (1, 1, 1), (2, 2, 2))])
    with pytest.raises(model_job.NoPrintableVolume, match="non-finite"):
        model_job.model_info_output(path)
    audit = model_job.audit(path)
    assert audit["no_printable_volume"] is True
    assert "non-finite" in audit["geometry_error"]
