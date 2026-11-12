"""A gap under the first layer height prints as touching (2.3.0 live run).

A part's underside sat 0.07 mm above the plate while the profile's first
layer is 0.24 mm. The first layer's squish closes such a gap, yet the
intake flagged it as a low-clearance underside: the supports option was
marked "not recommended", the post-slice gate asked for Print anyway, and
extend/sink repairs were suggested. A gap at least one first layer high
keeps the existing low-clearance behavior.
"""
import math

from print_handy.core import support_clearance
from print_handy.intake import support_flow

from .test_support_clearance_phase2 import box, prism_xz, write_3mf

FIRST_LAYER_MM = 0.24  # the 0.4 mm nozzle profile's first layer height


def slab_file(tmp_path, z0, name):
    """A 30 x 30 mm slab whose flat underside floats ``z0`` mm over the plate,
    beside a foot that stands on the plate."""
    return write_3mf(tmp_path / name, [('FOOT', box(-20, -20, 0, -10, -10, 3), '', None),
                                       ('SLAB', box(0, 0, z0, 30, 30, 3), '', None)])


def sloped_peg_file(tmp_path, z0=0.07):
    """The live case's shape: a 21 mm long peg whose underside slopes up at
    30 degrees from a lowest edge ``z0`` mm over the plate."""
    rise = 2.0 * math.tan(math.radians(30))
    poly = [(0, z0), (2.0, z0 + rise), (2.0, 4.0), (-2.0, 4.0), (-2.0, z0 + rise)]
    return write_3mf(tmp_path / 'peg.3mf', [('PEG', prism_xz(poly, 0, 21), '', None)])


def _low_clearance(rep):
    return [r for r in rep['regions'] if r.get('kind') == 'low_clearance']


def test_a_gap_under_the_first_layer_is_not_flagged(tmp_path):
    rep = support_clearance.analyze(str(slab_file(tmp_path, 0.07, 'thin.3mf')),
                                    first_layer_mm=FIRST_LAYER_MM)
    assert rep['risky'] is False
    assert support_clearance.CODE_CLEARANCE not in support_clearance.codes(rep)
    assert _low_clearance(rep) == []
    assert support_clearance.describe(rep) == []  # no "bridges a 0.07 mm gap" line, no extend/sink
    assert rep['thresholds']['first_layer_mm'] == FIRST_LAYER_MM


def test_a_gap_above_the_first_layer_keeps_todays_finding(tmp_path):
    rep = support_clearance.analyze(str(slab_file(tmp_path, 0.5, 'half.3mf')),
                                    first_layer_mm=FIRST_LAYER_MM)
    assert rep['risky'] is True
    finding = next(f for f in rep['findings'] if f['code'] == support_clearance.CODE_CLEARANCE)
    assert finding['clearance_mm']['min'] == 0.5
    assert {fix['fix'] for fix in finding['fixes']} >= {'extend_underside', 'sink'}


def test_without_a_first_layer_height_the_contact_threshold_is_unchanged(tmp_path):
    rep = support_clearance.analyze(str(slab_file(tmp_path, 0.07, 'thin.3mf')))
    assert rep['risky'] is True
    assert rep['thresholds']['first_layer_mm'] is None


def test_a_slope_rising_from_under_the_first_layer_is_not_flagged(tmp_path):
    # Today (no first layer) the slope is one floating 21 mm strip; with the
    # first layer it roots into the squished band at its lowest edge.
    path = str(sloped_peg_file(tmp_path))
    assert support_clearance.analyze(path)['risky'] is True
    rep = support_clearance.analyze(path, first_layer_mm=FIRST_LAYER_MM)
    assert rep['risky'] is False
    assert _low_clearance(rep) == []


def test_intake_analysis_uses_the_profile_first_layer(tmp_path, monkeypatch):
    """safe_analyze (every intake, repair and start-guard caller) reads the
    printer profile's first layer height, so the supports question carries
    no "not recommended" reason and the post-slice gate has nothing to grade."""
    monkeypatch.setattr(support_clearance, 'profile_first_layer_mm', lambda printer_id=None: FIRST_LAYER_MM,
                        raising=False)  # absent on the base commit, which then flags the gap
    rep = support_clearance.safe_analyze(str(slab_file(tmp_path, 0.07, 'thin.3mf')))
    assert rep is not None and rep['risky'] is False
    show, reason, _ = support_flow.supports_decision(False, None, rep)
    assert show is False and 'too low for supports' not in reason
    assert [r for r in rep['regions'] if r.get('flagged')] == []  # no post-slice coverage target

    still = support_clearance.safe_analyze(str(slab_file(tmp_path, 0.5, 'half.3mf')))
    show, reason, _ = support_flow.supports_decision(False, None, still)
    assert show is True and '0.5 mm' in reason


def test_rotated_report_uses_the_profile_first_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(support_clearance, 'profile_first_layer_mm', lambda printer_id=None: FIRST_LAYER_MM,
                        raising=False)  # absent on the base commit, which then flags the gap
    identity = [1, 0, 0, 0, 1, 0, 0, 0, 1]
    # Rotated reports drop the model onto the plate, so lift only the slab's
    # underside relative to a foot that is already on it.
    rep = support_clearance.rotated_report(str(slab_file(tmp_path, 0.07, 'thin.3mf')), identity)
    assert rep['risky'] is False


def test_a_job_edit_of_the_first_layer_height_wins_over_the_profile(monkeypatch):
    monkeypatch.setattr(support_clearance, "profile_first_layer_mm", lambda printer_id=None: 0.24)
    job = {"edit_settings": {"profile": {"initial_layer_height": 0.1}}}
    assert support_clearance.job_first_layer_mm(job) == 0.1
    assert support_clearance.job_first_layer_mm({}) == 0.24
    assert support_clearance.job_first_layer_mm({"edit_settings": {"profile": {"initial_layer_height": "x"}}}) == 0.24
