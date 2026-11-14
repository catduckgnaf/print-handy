"""The prompt registry never carries an open prompt from one test into the next.

The two tests run in file order: the first leaves a prompt open on purpose,
the second must still see an empty registry. Before the isolation fixture in
tests/conftest.py, the second failed (and so did
tests/bot/test_firmware_alert_transports.py::
test_alert_prompt_closes_when_the_waiter_is_cancelled_before_starting
whenever tests/bot/test_multipart_prompt_parity_1204.py ran first).
"""
from print_handy.core import prompts


def test_a_test_may_leave_a_prompt_open():
    prompts.register("isolation-leak", "isolation:flow", "Question?", ["Yes", "No"], approval=True)
    assert prompts.open_count() == 1
    assert prompts.is_approval("isolation-leak")


def test_the_next_test_starts_with_no_open_prompt():
    assert prompts.open_count() == 0
    assert not prompts.is_approval("isolation-leak")
