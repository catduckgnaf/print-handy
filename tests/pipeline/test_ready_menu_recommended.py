"""Catduck's bag clip review on develop: the Ready menu and the support coverage question.

1. When a choice is recommended, the button says so, not only the text above
   it. The "(bed is clear)" suffix leaves the buttons; the question says once
   that pressing a Slice and print button confirms the bed is clear.
2. The coverage question says in plain words what was checked, on which part,
   and what Print anyway and Cancel each do.
"""
import pytest

from print_handy.core import prepared_intake, support_clearance, yolo
from print_handy.core import model_job
from print_handy.runtime import print_next

from .test_intake_support_precheck import _coverage_only_report, _warning_slice, run_flow
from .test_intake_support_precheck import env  # noqa: F401  (pytest fixture)


def test_ready_menu_puts_the_recommendation_on_the_button(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    # one color (2 on the #1473 offer), slot 1 (0), Cancel at the Ready menu (last)
    adapter, _result, _receipt = run_flow(env, [2, 0, 6])
    ready = next(q for q in adapter.questions if q['question'].startswith('Ready to slice'))
    assert ready['choices'][0] == 'Slice and print with supports (recommended)'
    assert ready['choices'][1] == 'Slice and print without supports'
    assert not any('bed is clear' in c for c in ready['choices'])
    # The confirmation the buttons used to carry is stated once, in the question.
    assert prepared_intake.BED_CLEAR_NOTE in ready['question']
    assert ready['question'].count('bed is clear') == 1


def test_bed_clear_note_only_where_a_button_prints(env, monkeypatch):  # noqa: F811
    from print_handy.core import delivery
    monkeypatch.setattr(delivery, 'methods', lambda *a, **k: ['discord'])
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    adapter, _result, _receipt = run_flow(env, [2, 0, 5])
    ready = next(q for q in adapter.questions if q['question'].startswith('Ready to slice'))
    assert ready['choices'][0] == 'Slice with supports (recommended)'
    assert prepared_intake.BED_CLEAR_NOTE not in ready['question']


def test_yolo_still_takes_the_renamed_print_buttons():
    assert yolo.auto_answer(['Slice and print with supports (recommended)',
                             'Slice and print without supports', 'Cancel print request']) == 0
    assert yolo.auto_answer(['Slice and print now', 'Slice and Download', 'Cancel print request']) == 0
    assert yolo.auto_answer(['Slice and print without supports',
                             'Slice and print with supports (not recommended)',
                             'Print anyway, accept the risk', 'Cancel']) == 2


def test_coverage_text_names_the_part_and_the_plain_finding():
    text = prepared_intake.support_coverage_text({'regions': [
        {'part': 'Part 1 - Shell', 'coverage': 0.33, 'z_mm': {'min': 50.4}},
        {'part': 'Part 2 - Plunger', 'coverage': 0.0, 'z_mm': {'min': 24.6}},
    ]})
    assert 'found no support of an overhang on Part 2 - Plunger at about 25 mm up' in text
    assert '3 mm cells' not in text and 'flagged region' not in text
    partial = prepared_intake.support_coverage_text({'regions': [
        {'part': 'Shell', 'coverage': 0.33, 'z_mm': {'min': 50.4}}]}, resliced=True)
    assert partial.startswith('Re-sliced with supports')
    assert 'support under only about 33% of an overhang on Shell' in partial


@pytest.mark.parametrize('pick', [0, 1])
def test_coverage_question_says_what_each_button_does(env, monkeypatch, pick):  # noqa: F811
    monkeypatch.setattr(support_clearance, 'safe_analyze', _coverage_only_report)
    monkeypatch.setattr(model_job, 'slice_job', _warning_slice(env))
    adapter, _result, _receipt = run_flow(env, [2, 0, 0, pick])
    q = adapter.questions[-1]
    assert q['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    text = q['question']
    assert 'found no support of an overhang on Body' in text
    assert '- Print anyway: print this slice as it is.' in text
    assert '- Cancel: stop here.' in text
    # No low gap here, so no extend or sink advice the buttons cannot do.
    assert 'extend or sink' not in text and 'Extend underside' not in text
    assert 'repair the geometry' not in text
