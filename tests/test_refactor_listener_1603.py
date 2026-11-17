"""Pin tests for #1603 items 4 and 5: the listener transition extractions.

Each test pins one extracted helper through the entry point its callers
use, or pins the order ``_apply_status`` calls the helpers in, so the
behavior-preserving move cannot reorder, drop or unguard a side effect.
The lazy-attribute pins check the declared ``__init__`` defaults the
former ``getattr(self, ...)`` reads used to fall back to.
"""
import asyncio

from print_handy.runtime import moonraker_event_listener as mel


# -- item 5: lazy attributes are declared in __init__ -------------------------

def test_lazy_attributes_are_declared_with_their_former_defaults():
    listener = mel.Listener()
    assert listener._job_last_run == {}
    assert listener.visual_timeouts == 0
    assert listener._next_plate_task is None
    assert listener._watch_context_seen is False
    assert listener.oc_recovery_identity is None
    assert listener.oc_prompt is None
    assert listener.shutdown_alerted is None
    assert listener._heartbeat_thread is None
    assert listener.failover_watch is not None
    assert listener.print_seen_started_at is None


def test_run_due_jobs_uses_the_declared_job_table():
    listener = mel.Listener()  # no explicit _job_last_run assignment
    ran = []

    async def good():
        ran.append("good")

    asyncio.run(listener._run_due_jobs([("good job", 60, good)], now=1000.0))
    assert ran == ["good"]
    asyncio.run(listener._run_due_jobs([("good job", 60, good)], now=1001.0))
    assert ran == ["good"]  # inside the interval: not due again


# -- item 4: the extracted transition side effects ----------------------------

def test_launch_lease_settle_only_fires_when_printing_and_is_guarded(monkeypatch):
    listener = mel.Listener()
    settled = []
    listener._settle_launch_lease = lambda: settled.append(True)
    asyncio.run(listener._apply_launch_lease_settle("paused"))
    assert settled == []
    asyncio.run(listener._apply_launch_lease_settle("printing"))
    assert settled == [True]

    warnings = []
    monkeypatch.setattr(mel, "warn", warnings.append)

    def _raise():
        raise RuntimeError("lease store gone")

    listener._settle_launch_lease = _raise
    asyncio.run(listener._apply_launch_lease_settle("printing"))  # must not raise
    assert len(warnings) == 1 and "launch lease settle failed" in warnings[0]


def test_run_announcer_guarded_warns_instead_of_raising(monkeypatch):
    listener = mel.Listener()
    seen = []
    monkeypatch.setattr(mel, "run_announcer", seen.append)
    asyncio.run(listener._run_announcer_guarded("printing", "paused"))
    assert seen == ["printing->paused"]

    warnings = []
    monkeypatch.setattr(mel, "warn", warnings.append)

    def _raise(reason):
        raise RuntimeError("announcer gone")

    monkeypatch.setattr(mel, "run_announcer", _raise)
    asyncio.run(listener._run_announcer_guarded("printing", "paused"))  # must not raise
    assert len(warnings) == 1 and "announcer after printing->paused failed" in warnings[0]


def test_ended_bookkeeping_only_runs_for_an_ended_print(monkeypatch):
    listener = mel.Listener()
    charges = []
    captures = []

    async def _charge(new_state):
        charges.append(new_state)

    listener._charge_multislot_usage = _charge
    listener._capture_end = captures.append
    monkeypatch.setattr(mel, "load_runout_pause", lambda: None)

    asyncio.run(listener._apply_ended_bookkeeping("printing", "paused", False))
    assert charges == [] and captures == []
    asyncio.run(listener._apply_ended_bookkeeping("paused", "standby", True))
    assert charges == ["standby"] and captures == ["standby"]


def test_bind_start_correlation_is_guarded_on_its_own(monkeypatch):
    listener = mel.Listener()
    listener.filename = "charm.gcode"
    warnings = []
    monkeypatch.setattr(mel, "warn", warnings.append)

    def _raise(context):
        raise RuntimeError("no correlation")

    monkeypatch.setattr(mel.job_correlation, "from_context", _raise)
    listener._bind_start_correlation()  # must not raise
    assert len(warnings) == 1 and "correlation id binding failed" in warnings[0]


def _recording_listener(monkeypatch):
    """A listener whose extracted side effects record their call order."""
    listener = mel.Listener()
    listener.filename = "charm.gcode"
    order = []

    def _recorder(name):
        async def _record(*args):
            order.append(name)
        return _record

    for name in ("_apply_launch_lease_settle", "_apply_fresh_start",
                 "_apply_reconnect_effects", "_apply_visual_due_check",
                 "_apply_resume_or_start", "_apply_pause_transition",
                 "_apply_terminal_notice", "_apply_ended_bookkeeping",
                 "_run_announcer_guarded", "_print_ended"):
        setattr(listener, name, _recorder(name))
    monkeypatch.setattr(mel, "log", lambda line: None)
    monkeypatch.setattr(mel, "_record_transition_markers", lambda *a: None)
    return listener, order


def test_apply_status_resume_calls_side_effects_in_order(monkeypatch):
    listener, order = _recording_listener(monkeypatch)
    listener.state = "paused"
    asyncio.run(listener._apply_status({"state": "printing"}, "test"))
    assert order == ["_apply_launch_lease_settle", "_apply_reconnect_effects",
                     "_apply_visual_due_check", "_apply_resume_or_start",
                     "_apply_ended_bookkeeping", "_run_announcer_guarded"]
    assert listener.state == "printing"
    assert listener._print_ended_done is False


def test_apply_status_terminal_calls_side_effects_in_order_and_ends_once(monkeypatch):
    listener, order = _recording_listener(monkeypatch)
    listener.state = "printing"
    asyncio.run(listener._apply_status({"state": "cancelled"}, "test"))
    assert order == ["_apply_launch_lease_settle", "_apply_reconnect_effects",
                     "_apply_visual_due_check", "_apply_terminal_notice",
                     "_apply_ended_bookkeeping", "_run_announcer_guarded",
                     "_print_ended"]
    assert listener.state == "cancelled"
    assert listener._ended_cleanup_armed is True
    assert listener._print_ended_done is True
