"""Lifecycle regression tests for Discord print progress and state transitions.

Verifies:
1. Receipt is valid JSON (no literal '\\n') at the START_REQUESTED transition and after completion.
2. Immediate preparation message sent after bed-clear approval, before start.
3. Message sequence and ordering: slice summary -> preparation message -> started confirmation.
4. Refused/failed start: slice summary -> preparation message -> precise failure message (no printing claim).
5. Repeated event deduplication: no duplicate start calls or duplicate messages.
6. Mapped multicolor flow consistency: same lifecycle, preparation message, and truthfulness.
7. Slice-only then button start flow lifecycle and ordering.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S

import pytest


from print_handy.core import model_job  # noqa: E402
from print_handy.core import prepared_intake  # noqa: E402
from .helpers import external  # noqa: E402
print_next = external("print_next")
from .test_color_intake import state  # noqa: E402
from .test_prepared_intake import Adapter, env, run_flow  # noqa: E402


PREP_TEXT = 'Preparing to start'
WAIT_TEXT = 'Waiting for the background slice to finish. Press Stop to cancel.'


def test_start_refusal_filters_diagnostics_and_explains_stale_heater_targets():
    tail = '\n'.join([
        'TIMING {"stage": "print_now"}',
        'G-CODE OK: plate_1.gcode | validated for upload',
        'REFUSED: idle guard failed: native rightTargetTemp == 0 (observed 210.0); '
        'native platTargetTemp == 0 (observed 60.0). Nothing uploaded or started.',
    ])

    message = prepared_intake._refusal_text(tail)

    assert message == (
        "Didn't start: the printer still has the nozzle set to 210 °C and the bed to 60 °C "
        "from the last job. Clear heating on the printer screen (or power cycle it), then press Retry."
    )
    assert 'TIMING' not in message and 'G-CODE OK:' not in message
    assert prepared_intake._safe_preupload_refusal(tail)


def test_unknown_refusal_is_plain_and_keeps_raw_detail_out_of_chat():
    tail = 'REFUSED: internal probe 7 failed with opaque_code=QX-19. Nothing uploaded or started.'

    message = prepared_intake._refusal_text(tail)

    assert message == (
        "Didn't start: the printer could not confirm it was ready. Check the printer screen, "
        "resolve any warning, then press Retry."
    )
    assert 'opaque_code' not in message
    assert prepared_intake._safe_preupload_refusal(tail)


def test_single_color_successful_flow_messages_and_order(env):
    """After bed-clear approved, prep message is sent before start, followed by started confirmation."""
    adapter, result, receipt = run_flow(env, [1, 0, 0])  # single color, slot 1, slice and print now

    # P2-58: the still-running background slice is acknowledged first.
    assert len(adapter.log) == 3, f'Expected wait, prep and one start message: {adapter.log}'
    assert adapter.log[0] == WAIT_TEXT

    assert not any(m.startswith('Sliced:') for m in adapter.log)
    assert any(PREP_TEXT in m for m in adapter.log), f'Missing preparation message in: {adapter.log}'
    assert any(m.startswith('Print started:') for m in adapter.log), f'Missing started confirmation in: {adapter.log}'

    # No separate slice narration in the start flow.
    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    start_idx = next(i for i, m in enumerate(adapter.log) if m.startswith('Print started:'))

    assert prep_idx < start_idx, f'Wrong order: prep={prep_idx}, start={start_idx}'

    # Verify receipt state
    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1


def test_receipt_parseable_at_start_requested_transition(env, monkeypatch):
    """Regression: receipt on disk must be valid JSON at the START_REQUESTED transition (no literal \\n)."""
    real_start = print_next.start
    transition_verified = []

    async def checked_start(target):
        # Inspect receipt right as print_next.start is entered
        receipt_files = list(env.root.glob('*.json'))
        assert len(receipt_files) == 1, f'Expected 1 receipt file, found {len(receipt_files)}'
        rp = receipt_files[0]
        raw = rp.read_text()
        assert not raw.endswith('\\\\n'), 'Receipt ends with literal \\\\n instead of newline'
        assert not raw.endswith('\\n'), 'Receipt ends with literal \\n characters instead of newline character'
        assert raw.endswith('\n'), 'Receipt does not end with newline character'
        data = json.loads(raw)  # MUST parse without JSONDecodeError
        assert data.get('status') == 'START_REQUESTED'
        assert data.get('bed_clear_approval') is not None
        assert data['bed_clear_approval']['via'] == 'button'
        transition_verified.append(True)
        return await real_start(target)

    monkeypatch.setattr(print_next, 'start', checked_start)

    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert len(transition_verified) == 1, 'Transition check did not run'
    assert receipt['status'] == 'STARTED'

    # Final receipt on disk must also be valid JSON
    final_file = next(env.root.glob('*.json'))
    final_data = json.loads(final_file.read_text())
    assert final_data['status'] == 'STARTED'


def test_refused_start_sends_prep_then_precise_failure_no_printing_claim(env, monkeypatch):
    """When start is refused, send prep message followed by precise failure; do not claim printing."""
    refusal_reason = 'REFUSED: fresh printer check failed: printer is not idle (busy, active.gcode, Printing). Nothing uploaded or started.'

    async def refused_start(target):
        return False, refusal_reason

    monkeypatch.setattr(print_next, 'start', refused_start)

    adapter, result, receipt = run_flow(env, [1, 0, 0])

    assert len(adapter.log) == 3, f'Expected wait, prep and one refusal message: {adapter.log}'
    assert adapter.log[0] == WAIT_TEXT

    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)

    assert not any(m.startswith('Sliced:') for m in adapter.log)
    assert not any('print started' in m.lower() for m in adapter.log)
    assert adapter.log[2] == (
        "Didn't start: the printer could not confirm it was ready. Check the printer screen, "
        "resolve any warning, then press Retry."
    )
    assert 'REFUSED:' not in adapter.log[2]

    # Raw refusal remains in the receipt for status/debug details.
    assert receipt['status'] == 'START_REFUSED'
    assert receipt['started'] is False
    assert receipt['start_refusal_detail'] == refusal_reason

    # Receipt is valid JSON
    receipt_file = next(env.root.glob('*.json'))
    data = json.loads(receipt_file.read_text())
    assert data['status'] == 'START_REFUSED'
    assert data['start_refusal_detail'] == refusal_reason


def test_duplicate_event_deduplication_and_no_duplicate_messages(env):
    """A duplicate model-link event does not trigger a second start or duplicate Discord messages."""
    adapter1, result1, receipt1 = run_flow(env, [1, 0, 0], mid='dedup-1')
    assert len(env.starts) == 1
    initial_sent_count = len(adapter1.log)
    assert initial_sent_count == 3

    # Send identical event with a new adapter
    adapter2 = Adapter([])
    event2 = S(
        source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
        message_id='dedup-1',
        text='https://www.printables.com/model/1-ghost',
    )
    result2 = asyncio.run(prepared_intake.prepare_link(event2, adapter2, state(), env.root, timeout=1))

    assert result2['action'] == 'skip'
    assert result2['reason'] == 'duplicate model-link event'
    # No new start was initiated
    assert len(env.starts) == 1
    # No messages sent on the duplicate attempt
    assert len(adapter2.sent) == 0


def test_multicolor_mapped_flow_lifecycle_and_messages(env, monkeypatch):
    """Mapped multicolor flow follows the exact same lifecycle: slice -> prep -> started."""
    transition_verified = []
    real_start = print_next.start

    async def checked_start(target):
        rp = next(env.root.glob('*.json'))
        raw = rp.read_text()
        assert not raw.endswith('\\\\n') and not raw.endswith('\\n')
        assert raw.endswith('\n')
        data = json.loads(raw)
        assert data.get('status') == 'START_REQUESTED'
        transition_verified.append(True)
        return await real_start(target)

    monkeypatch.setattr(print_next, 'start', checked_start)

    # picks:
    # 0: Choose colors for these parts (per_part multicolor)
    # 0: Shell -> Slot 1 (white PLA)
    # 1: Eyes -> Slot 3 (black PLA)
    # 0: Slice and print now (bed is clear)
    adapter, result, receipt = run_flow(env, [0, 0, 1, 0])

    assert len(transition_verified) == 1
    assert receipt['color_decisions']['mode'] == 'per_part'
    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1

    # Messages check
    has_prep = any(PREP_TEXT in m for m in adapter.log)
    has_started = any(m.startswith('Print started:') for m in adapter.log)

    assert has_prep, f'Missing preparation message in: {adapter.log}'
    assert has_started, f'Missing started confirmation in: {adapter.log}'

    assert not any('Sliced:' in m for m in adapter.log)
    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    start_idx = next(i for i, m in enumerate(adapter.log) if m.startswith('Print started:'))
    assert prep_idx < start_idx


def test_multicolor_refused_start_sends_prep_then_precise_failure(env, monkeypatch):
    """Multicolor flow with start refusal sends prep then precise failure without claiming start."""
    refusal_reason = 'REFUSED: the next plate G-code changed after the prompt. Nothing uploaded or started.'

    async def refused_start(target):
        return False, refusal_reason

    monkeypatch.setattr(print_next, 'start', refused_start)

    # picks: per_part (0), Shell->slot 1 (0), Eyes->slot 3 (1), Slice and print now (0)
    adapter, result, receipt = run_flow(env, [0, 0, 1, 0])

    prep_idx = next(i for i, m in enumerate(adapter.log) if PREP_TEXT in m)
    fail_idx = next(i for i, m in enumerate(adapter.log) if m.startswith("Didn't start:"))
    assert prep_idx < fail_idx
    assert not any('Print started:' in m for m in adapter.log)
    assert receipt['status'] == 'START_REFUSED'
    assert receipt['started'] is False


def test_slice_only_then_start_button_lifecycle(env, monkeypatch):
    """Slice only, then explicit Start button confirmation sends prep before start."""
    # picks:
    # 1: One color for everything
    # 0: Slot 1
    # 1: Slice only
    # 0: Start this exact plate? -> Start next plate · bed is clear
    adapter, result, receipt = run_flow(env, [1, 0, 1, 0])

    assert receipt['status'] == 'STARTED'
    assert receipt['started'] is True
    assert len(env.starts) == 1

    # The noisy 'Sliced:' post was retired; the Start button question is the only pre-start prompt.
    assert not any(m.startswith('Sliced:') for m in adapter.log)
    # 'Preparing to start' rides on the single Stop/Uploading prompt, not a separate post.
    assert sum(PREP_TEXT in m for m in adapter.log) == 1
    assert not any(m.startswith(PREP_TEXT) and 'Uploading' not in m for m in adapter.log)
    assert any(m.startswith('Print started:') for m in adapter.log)


def test_slot_mismatch_refusal_names_the_slot_and_both_labels():
    tail = ("REFUSED: printer slot 2 is labeled PETG but Spoolman has Silk PLA there (T0). "
            "Nothing uploaded or started.")

    message = prepared_intake._refusal_text(tail)

    assert message == ("Didn't start: printer slot 2 is labeled PETG but Spoolman has Silk PLA there. "
                       "Fix the slot on the printer or the spool in Spoolman, then press Retry.")
def test_uncertain_earlier_start_refusal_names_the_file_and_how_it_clears():
    tail = ("REFUSED: an earlier start on this printer is uncertain (job /data/archive/bag/JOB.json, "
            "file Bag-Clip_plate_1.gcode.3mf, since 2026-10-30T19:21:32-0400). Check the printer, then run "
            "`python3 -m print_handy.core.model_job reconcile-launch --started` or `--not-started`. It is never "
            "resubmitted automatically. Nothing uploaded or started.")

    message = prepared_intake._refusal_text(tail)

    assert message.startswith("Didn't start: an earlier start (Bag-Clip_plate_1.gcode.3mf) was never confirmed")
    assert "could not confirm it was ready" not in message and "/data/archive" not in message
    assert prepared_intake._safe_preupload_refusal(tail)


COLOR_REFUSAL = ("REFUSED: printer slot 1 color is #E53935 but Spoolman has #CD7F32 there (T0). "
                 "Nothing uploaded or started.")


TWO_COLOR_REFUSAL = ("REFUSED: printer slot 1 color is #E53935 but Spoolman has #CD7F32 there (T0); "
                     "printer slot 2 color is #1E88E5 but Spoolman has #43A047 there (T1). "
                     "Nothing uploaded or started.")


def _color_mismatch_start(monkeypatch, refusal=COLOR_REFUSAL):
    """First start stops on the color difference; later starts record what JOB.json allowed."""
    seen = []

    async def start(target):
        job = json.loads(Path(target['job_json']).read_text())
        seen.append(job.get('color_mismatch_accepted'))
        if len(seen) == 1:
            return False, refusal
        return True, 'started'

    monkeypatch.setattr(print_next, 'start', start)
    return seen


def test_color_mismatch_asks_and_print_anyway_starts_this_gcode_once(env, monkeypatch):
    """#795: never a Retry-only refusal; Print anyway reaches the start exactly once more."""
    seen = _color_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])  # one color, slot 1, print now, Print anyway
    question = adapter.questions[-1]
    assert question['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, prepared_intake.SET_SCREEN_LABEL,
                                   print_next.CANCEL_LABEL]
    assert '#E53935' in question['question'] and '#CD7F32' in question['question']
    assert len(seen) == 2 and seen[0] is None
    assert seen[1]['slots'] == [model_job.color_mismatch_key(1, '#E53935', '#CD7F32')]
    assert not any(m.startswith("Didn't start:") for m in adapter.log)
    assert receipt['status'] == 'STARTED' and receipt['color_mismatch']['choice'] == 0
    job_json = next(env.tmp.rglob('JOB.json'))
    assert 'color_mismatch_accepted' not in json.loads(job_json.read_text())  # single use


def test_color_mismatch_cancel_uploads_nothing(env, monkeypatch):
    seen = _color_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 2])  # ... then Cancel
    assert len(seen) == 1
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt['started'] is False
    assert any('Cancelled. Nothing uploaded or started.' in m for m in adapter.log)


LABEL_REFUSAL = ("REFUSED: printer slot 2 is labeled PETG but Spoolman has Silk PLA there (T0). "
                 "Nothing uploaded or started.")


def test_label_mismatch_parses_separately_from_color():
    # #839: label differences have their own parser; the color parser still ignores them.
    assert prepared_intake.color_confirm(LABEL_REFUSAL) == []
    assert prepared_intake.label_confirm(LABEL_REFUSAL) == [{'slot': 2, 'live': 'PETG',
                                                             'recorded': 'Silk PLA', 'tool': 0}]
    assert prepared_intake.label_confirm(COLOR_REFUSAL) == []
    assert prepared_intake.color_confirm(COLOR_REFUSAL) == [{'slot': 1, 'live': '#E53935',
                                                             'recorded': '#CD7F32', 'tool': 0}]
    assert [c['slot'] for c in prepared_intake.color_confirm(TWO_COLOR_REFUSAL)] == [1, 2]


def _label_mismatch_start(monkeypatch, refusal=LABEL_REFUSAL):
    """First start stops on the label difference; later starts record what JOB.json allowed."""
    seen = []

    async def start(target):
        job = json.loads(Path(target['job_json']).read_text())
        seen.append(job.get('label_mismatch_accepted'))
        if len(seen) == 1:
            return False, refusal
        return True, 'started'

    monkeypatch.setattr(print_next, 'start', start)
    return seen


def test_label_mismatch_asks_and_print_anyway_starts_this_gcode_once(env, monkeypatch):
    """#839: the label mismatch mirrors the color flow — ask once, Print anyway
    binds the acceptance to this exact G-code, and it is consumed after."""
    seen = _label_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])  # one color, slot 1, print now, Print anyway
    question = adapter.questions[-1]
    assert question['choices'] == [prepared_intake.PRINT_ANYWAY_LABEL, print_next.CANCEL_LABEL]
    assert 'PETG' in question['question'] and 'Silk PLA' in question['question']
    assert len(seen) == 2 and seen[0] is None
    assert seen[1]['slots'] == [model_job.label_mismatch_key(2, 'PETG', 'Silk PLA')]
    assert not any(m.startswith("Didn't start:") for m in adapter.log)
    assert receipt['status'] == 'STARTED' and receipt['label_mismatch']['choice'] == 0
    job_json = next(env.tmp.rglob('JOB.json'))
    assert 'label_mismatch_accepted' not in json.loads(job_json.read_text())  # single use


def test_label_mismatch_cancel_uploads_nothing(env, monkeypatch):
    seen = _label_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])  # ... then Cancel
    assert len(seen) == 1
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt['started'] is False
    assert any('Cancelled. Nothing uploaded or started.' in m for m in adapter.log)


def test_two_mismatched_slots_get_one_prompt_and_one_print_anyway_for_both(env, monkeypatch):
    """#795 review: both slots are named in one question, and one answer covers both."""
    seen = _color_mismatch_start(monkeypatch, TWO_COLOR_REFUSAL)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])  # ... then Print anyway
    asked = [q for q in adapter.questions if prepared_intake.PRINT_ANYWAY_LABEL in q['choices']]
    assert len(asked) == 1
    question = asked[0]['question']
    assert 'slot 1' in question and 'slot 2' in question
    assert 'red #E53935' in question and 'bronze #CD7F32' in question
    assert 'blue #1E88E5' in question and 'green #43A047' in question
    assert len(seen) == 2  # one refused attempt, one that reaches the upload
    assert seen[1]['slots'] == sorted([model_job.color_mismatch_key(1, '#E53935', '#CD7F32'),
                                       model_job.color_mismatch_key(2, '#1E88E5', '#43A047')])
    assert receipt['status'] == 'STARTED' and receipt['color_mismatch']['choice'] == 0


def test_two_mismatched_slots_cancel_uploads_nothing(env, monkeypatch):
    seen = _color_mismatch_start(monkeypatch, TWO_COLOR_REFUSAL)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 2])
    assert len(seen) == 1
    assert receipt['status'] == 'SLICED_NOT_STARTED' and receipt['started'] is False


def test_color_refusal_that_surfaces_keeps_its_retry_text(env, monkeypatch):
    """When no question can follow (the rounds are used up), the refusal says Retry like any other."""
    async def start(target):
        return False, COLOR_REFUSAL

    monkeypatch.setattr(print_next, 'start', start)
    adapter, result, receipt = run_flow(env, [1, 0, 0] + [0] * (prepared_intake.COLOR_CONFIRM_ROUNDS - 1))
    asked = [q for q in adapter.questions if prepared_intake.PRINT_ANYWAY_LABEL in q['choices']]
    assert len(asked) == prepared_intake.COLOR_CONFIRM_ROUNDS - 1
    assert adapter.log[-1].startswith("Didn't start: printer slot 1 color is #E53935")
    assert adapter.log[-1].endswith('then press Retry.')


def test_screen_unpushed_note_names_every_slot_and_says_the_screen_is_unchanged():
    one = prepared_intake.screen_unpushed_note([{'slot': 2}])
    assert one == "Couldn't update the screen color for slot 2, so I'm printing with the screen color unchanged."
    two = prepared_intake.screen_unpushed_note([{'slot': 1}, {'slot': 3}])
    assert 'slot 1 and slot 3' in two and two.endswith('printing with the screen color unchanged.')
    three = prepared_intake.screen_unpushed_note([{'slot': 1}, {'slot': 2}, {'slot': 4}])
    assert 'slot 1, slot 2 and slot 4' in three


def test_set_screen_that_fails_for_a_slot_prints_anyway_and_tells_the_channel(env, monkeypatch):
    """#798: Set screen to Spoolman color, then print: the slot whose push failed is named, the print goes ahead."""
    seen = _color_mismatch_start(monkeypatch, TWO_COLOR_REFUSAL)
    spool_sync = external("spool_sync")
    calls = []

    def sync_slot_screen(slot, printer_id=None):
        calls.append(slot)
        if slot == 2:
            raise RuntimeError('screen offline')
        return 'pushed'

    monkeypatch.setattr(spool_sync, 'sync_slot_screen', sync_slot_screen)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])  # ... then Set screen to Spoolman color
    assert calls == [1, 2]
    assert receipt['color_mismatch']['screen'] == {'1': 'pushed', '2': 'failed'}
    notes = [m for m in adapter.log if 'screen color unchanged' in m]
    assert notes == ["Couldn't update the screen color for slot 2, so I'm printing with the screen color unchanged."]
    # Only the slot that stayed as it was is accepted; the pushed slot needs no acknowledgement.
    assert seen[1]['slots'] == [model_job.color_mismatch_key(2, '#1E88E5', '#43A047')]
    assert receipt['status'] == 'STARTED'


def test_set_screen_that_pushes_every_slot_posts_no_note(env, monkeypatch):
    _color_mismatch_start(monkeypatch)
    spool_sync = external("spool_sync")
    monkeypatch.setattr(spool_sync, 'sync_slot_screen', lambda slot, printer_id=None: 'pushed')
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])
    assert not any('screen color unchanged' in m for m in adapter.log)
    assert receipt['color_mismatch']['screen'] == {'1': 'pushed'}


MATERIAL_REFUSAL = ("REFUSED: material warning, your answer is needed: G-code filament_type 'PETG' != loaded "
                    "'PLA': the G-code was sliced for PETG and PLA is loaded [accept material|PETG|PLA]. "
                    "Nothing uploaded or started.")


def _material_mismatch_start(monkeypatch):
    """First start stops on the material warning; later starts record the answer they carried."""
    seen = []

    async def start(target):
        seen.append(list(target.get('accept_material') or []))
        if len(seen) == 1:
            return False, MATERIAL_REFUSAL
        return True, 'started'

    monkeypatch.setattr(print_next, 'start', start)
    return seen


def test_material_mismatch_asks_reslice_first_and_print_as_sliced_starts_once_1336(env, monkeypatch):
    """#1336 (fails on base 1d452a58, which showed a Retry-only refusal): the intake asks
    Reslice / Print as sliced / Cancel; Print as sliced restarts with this answer only."""
    seen = _material_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1])  # one color, slot 1, print now, Print as sliced
    question = adapter.questions[-1]
    assert question['choices'] == ['Reslice for PLA', 'Print as sliced', 'Cancel']
    assert 'PETG' in question['question'] and 'own temperatures' in question['question']
    assert seen == [[], ['material|PETG|PLA']]
    assert receipt['material_warnings']['choice'] == 1
    # The answer is this start's only: the saved Retry target does not carry it.
    assert 'accept_material' not in (receipt.get('retry_target') or {})
    assert not any(m.startswith("Didn't start:") for m in adapter.log)


def test_material_mismatch_reslice_is_the_default_and_starts_nothing_1336(env, monkeypatch):
    seen = _material_mismatch_start(monkeypatch)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])  # ... Reslice for PLA
    assert len(seen) == 1
    assert receipt['status'] == 'SLICED_NOT_STARTED'
    assert any('To reslice for PLA' in m for m in adapter.log)
