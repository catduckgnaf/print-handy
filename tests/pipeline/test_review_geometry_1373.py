"""#1373 review, second batch: clearance follows the job's pose, Reorient guards,
manual supports, and the job's own support angle in the orientation scorer."""
import asyncio
import json
import math
import zipfile

from print_handy.core import orca_adapter, prepared_intake, support_clearance
from tests.pipeline.test_designed_overhang_1373 import LOWER, SHELF, _write


def _min_gap(report):
    gaps = [f["clearance_mm"]["min"] for f in report["findings"] if f["code"] == support_clearance.CODE_CLEARANCE]
    return min(gaps) if gaps else None


def test_a_scaled_job_is_analyzed_at_its_printed_size(tmp_path):
    # Two parts, 0.5 mm apart: a risk at full size; at 50% the gap is 0.25 mm.
    src = _write(tmp_path / "two.3mf", [[LOWER], [SHELF]])
    full = support_clearance.posed_report(src)
    half = support_clearance.posed_report(src, scale=0.5)
    assert abs(_min_gap(full) - 0.5) < 0.05 and abs(_min_gap(half) - 0.25) < 0.05
    assert half["pose"] == support_clearance.pose_key(None, 0.5)


def test_a_turned_job_is_analyzed_in_the_turned_pose(tmp_path):
    src = _write(tmp_path / "two.3mf", [[LOWER], [SHELF]])
    on_side = [1, 0, 0, 0, 0, -1, 0, 1, 0]  # 90 degrees about X: the shelf no longer floats over the block
    turned = support_clearance.posed_report(src, matrix=on_side)
    assert turned["orientation_applied"] is True
    assert not support_clearance.risky(turned)


def test_the_job_report_is_refreshed_only_when_the_pose_changes(tmp_path):
    src = _write(tmp_path / "two.3mf", [[LOWER], [SHELF]])
    job = {"downloaded_file": src, "scale": 0.5}
    first = asyncio.run(prepared_intake._refresh_posed_clearance(job, src))
    assert job["support_clearance"] is first and first["pose"]["scale"] == 0.5
    again = asyncio.run(prepared_intake._refresh_posed_clearance(job, src))
    assert again is first  # same pose: no re-analysis
    job["scale"] = 1.0
    full = asyncio.run(prepared_intake._refresh_posed_clearance(job, src))
    assert full is not first and full["pose"]["scale"] == 1.0


def test_the_scorer_uses_the_designers_support_angle(tmp_path):
    src = tmp_path / "angle.3mf"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("Metadata/project_settings.config", json.dumps({"support_threshold_angle": "45"}))
    assert abs(orca_adapter.support_threshold_nz(src) + math.cos(math.radians(45))) < 1e-9
    assert orca_adapter.support_threshold_nz(tmp_path / "none.stl") == orca_adapter.SUPPORT_THRESHOLD_NZ
