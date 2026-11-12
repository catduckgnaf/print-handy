"""Status voice wiring (#1326) in the listeners: print_monitor, print_complete, print_failure.

The quip rides only on posts that already exist and are informational: the
claimed start's warmup-done edit, and the plain family-printer terminal posts
that carry no completion or status roast. An empty quip leaves each post
byte-identical.
"""
import pytest

from print_handy.core import status_voice
from print_handy.runtime import family_control
from print_handy.runtime import moonraker_event_listener as mel
from .test_print_messages import (  # noqa: F401  (pytest fixtures)
    _claimed_listener, _drive_listener, _fix_finish_text_on_windows, job)


def seed(monkeypatch, speak=True):
    calls = []

    def fake(stage, **kwargs):
        calls.append((stage, kwargs))
        return f'[{stage} quip]' if speak else ''
    monkeypatch.setattr(status_voice, 'quip', fake)
    return calls


def _warmup_edit(monkeypatch, tmp_path, job, speak):
    calls = seed(monkeypatch, speak)
    monkeypatch.setattr(mel, "generate_roast", lambda *a, **kw: "")
    edits = _claimed_listener(monkeypatch, tmp_path, job)
    posted = _drive_listener(monkeypatch, job.hashed, ["standby", "printing"], claimed=True)
    assert posted == [] and len(edits) == 1  # no new message: the one start card is edited
    return edits[0][1], calls


def test_warmup_done_edit_carries_one_monitor_quip_after_the_facts(job, tmp_path, monkeypatch):
    voiced, calls = _warmup_edit(monkeypatch, tmp_path, job, True)
    assert voiced.endswith("\nWarmup complete, now printing. [print_monitor quip]")
    assert [(stage, kw.get("event_key")) for stage, kw in calls] == [
        ("print_monitor", f"{job.hashed}:warmup_done")]  # one key per print and milestone


def test_warmup_done_edit_without_a_quip_is_byte_identical(job, tmp_path, monkeypatch):
    voiced, _ = _warmup_edit(monkeypatch, tmp_path, job, True)
    plain, _ = _warmup_edit(monkeypatch, tmp_path / "again", job, False)
    assert plain.endswith("\nWarmup complete, now printing.")
    assert voiced == plain + " [print_monitor quip]"


@pytest.mark.parametrize("kind,stage", [("complete", "print_complete"), ("error", "print_failure"),
                                        ("cancelled", None), ("standby", None), ("started", None)])
@pytest.mark.parametrize("speak", [True, False])
def test_family_terminal_post_quip_follows_the_facts(monkeypatch, kind, stage, speak):
    calls = seed(monkeypatch, speak)
    posts = []

    class Mel:
        @staticmethod
        def post(text, **kwargs):
            posts.append((text, kwargs))
            return True
    monkeypatch.setattr(family_control, "_mel", lambda: Mel)
    facts = f"Print {kind}: Stand\nPrinter: A1"
    monkeypatch.setattr(family_control.print_summary, "format_event", lambda k, f: facts)
    assert family_control.post_family_event(kind, "stand.gcode") is True
    expected = facts + (f"\n[{stage} quip]" if stage and speak else "")
    assert posts == [(expected, {"ping_kind": kind, "ping_job_key": "stand.gcode"})]
    assert [c[0] for c in calls] == ([stage] if stage else [])
