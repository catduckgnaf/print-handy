"""Auto light-off for the family printers (#516 follow-up): the driver,
its listener/sink wiring, and the per-family availability reporting.

The Moonraker half shipped with #519; this file pins the Bambu Lab and
Elegoo SDCP half. Everything is offline: sends, snapshots, verdicts,
probes and clocks are fakes, and the policy under test is the same
light_control.AutoLightPolicy the Moonraker listener feeds.
"""
import pytest

from print_handy.runtime import family_control as fc
from print_handy.runtime import family_event_listener as fel
from print_handy.runtime import light_control as lc
from print_handy.runtime import visual_watch

T0 = 1_000_000.0
MIN = 60.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def make_driver(clock, *, minutes=30, send=None, snapshot=None, verdict=None,
                available=None, vision_ready=None):
    sends = []
    driver = fc.FamilyAutoLight(
        "elegoo_sdcp",
        send=send or (lambda on: sends.append(on)),
        snapshot=snapshot if snapshot is not None else (lambda: b"frame"),
        verdict=verdict or (lambda image, prompt: ("empty_idle", "bed empty")),
        available=available if available is not None else (lambda: True),
        vision_ready=vision_ready if vision_ready is not None else (lambda: True),
        policy=lc.AutoLightPolicy(minutes, check_interval_s=300),
        clock=clock)
    return driver, sends


# ------------------------------------------------------------------ driver

def test_disabled_policy_never_sends():
    clock = Clock()
    driver, sends = make_driver(clock, minutes=0)
    driver.print_started()
    driver.print_finished()
    clock.now += 99999
    driver.idle_tick("complete")
    assert sends == []


def test_print_start_sends_on_once(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()
    driver, sends = make_driver(clock)
    driver.print_started()
    driver.print_started()  # already printing: no repeat
    assert sends == [True]


def test_print_start_without_a_vision_endpoint_sends_nothing():
    clock = Clock()
    driver, sends = make_driver(clock, vision_ready=lambda: False)
    driver.print_started()
    assert sends == []


def test_reported_absent_light_is_never_switched_and_sticks():
    clock = Clock()
    probes = []

    def probe():
        probes.append(1)
        return False

    driver, sends = make_driver(clock, available=probe)
    driver.print_started()
    driver.print_started()
    assert sends == []
    assert len(probes) == 1  # the absence is cached, not re-probed per start


def test_failed_probe_is_unknown_and_never_blocks_a_send():
    clock = Clock()

    def probe():
        raise OSError("printer unreachable")

    driver, sends = make_driver(clock, available=probe)
    driver.print_started()
    assert sends == [True]


def test_failed_send_at_start_is_swallowed():
    clock = Clock()

    def send(on):
        raise OSError("transport down")

    driver, _sends = make_driver(clock, send=send)
    driver.print_started()  # must not raise
    driver.print_finished()
    driver.idle_tick("complete")


def test_full_cycle_turns_off_after_a_sustained_empty_streak(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()
    driver, sends = make_driver(clock)
    driver.print_started()
    driver.print_finished()
    driver.idle_tick("complete")              # first empty verdict: streak opens
    clock.now += 29 * MIN
    driver.idle_tick("complete")              # 29 minutes in: not yet
    assert sends == [True]
    clock.now += 1 * MIN
    driver.idle_tick("complete")              # 30 minutes: lights out, once
    clock.now += 60 * MIN
    driver.idle_tick("complete")              # window closed: nothing more
    assert sends == [True, False]


def test_non_empty_verdict_resets_the_streak(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()
    verdicts = iter([("empty_idle", ""), ("ok", "part on plate"),
                     ("empty_idle", ""), ("empty_idle", "")])
    driver, sends = make_driver(clock, verdict=lambda i, p: next(verdicts))
    driver.print_started()
    driver.print_finished()
    driver.idle_tick("complete")              # streak opens
    clock.now += 20 * MIN
    driver.idle_tick("complete")              # "ok": reset
    clock.now += 20 * MIN
    driver.idle_tick("complete")              # new streak opens here
    clock.now += 29 * MIN
    driver.idle_tick("complete")              # 29 minutes into the new streak
    assert sends == [True]


def test_dark_frame_is_uncertain_and_resets_the_streak(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: True)
    clock = Clock()
    judged = []
    driver, sends = make_driver(
        clock, verdict=lambda i, p: judged.append(i) or ("empty_idle", ""))
    driver.print_started()
    driver.print_finished()
    clock.now += 60 * MIN
    driver.idle_tick("complete")
    assert sends == [True]      # darkness never turns the light off
    assert judged == []         # and the endpoint is never even asked


def test_no_frame_source_never_turns_off_and_never_asks_for_a_verdict():
    clock = Clock()
    judged = []
    driver, sends = make_driver(
        clock, snapshot=lambda: None,
        verdict=lambda i, p: judged.append(i) or ("empty_idle", ""))
    driver.print_started()
    driver.print_finished()
    clock.now += 24 * 3600
    driver.idle_tick("complete")
    driver.idle_tick("standby")
    assert sends == [True]      # SDCP today: on at start, off never — reported, not silent
    assert judged == []


def test_idle_tick_ignores_active_and_unknown_states(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()
    driver, sends = make_driver(clock)
    driver.print_finished()
    driver.idle_tick("printing")
    driver.idle_tick("unknown")
    assert sends == []


def test_verdict_failure_is_swallowed_and_the_cadence_advances(monkeypatch):
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()

    def verdict(image, prompt):
        raise RuntimeError("endpoint down")

    driver, sends = make_driver(clock, verdict=verdict)
    driver.print_started()
    driver.print_finished()
    driver.idle_tick("complete")   # raises inside; swallowed
    assert driver._policy.last_check_at == T0
    assert sends == [True]


def test_a_failed_check_resets_the_empty_streak(monkeypatch):
    """A check that raises (endpoint or camera down) is an uncertain
    observation, not a skipped one — the same rule the Moonraker listener
    follows (P2-201), mirrored here: the pre-outage streak cannot carry
    the sustained-empty requirement."""
    monkeypatch.setattr(visual_watch, "frame_dark", lambda image: False)
    clock = Clock()
    calls = {"n": 0}

    def verdict(image, prompt):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("endpoint down")
        return "empty_idle", ""

    driver, sends = make_driver(clock, verdict=verdict)
    driver.print_started()
    driver.print_finished()
    driver.idle_tick("complete")       # streak opens at T0
    clock.now += 20 * MIN
    driver.idle_tick("complete")       # raises: the streak must reset
    assert driver._policy.empty_since is None
    clock.now += 20 * MIN
    driver.idle_tick("complete")       # a new streak opens here
    clock.now += 29 * MIN
    driver.idle_tick("complete")       # 29 minutes into the new streak
    assert sends == [True]


# ------------------------------------------------------- listener wiring

class FakeLight:
    def __init__(self):
        self.started = 0
        self.finished = 0
        self.ticks = []

    def print_started(self):
        self.started += 1

    def print_finished(self):
        self.finished += 1

    def idle_tick(self, state):
        self.ticks.append(state)


def st(state, filename="benchy.gcode"):
    return {"state": state, "filename": filename, "progress": 0.5}


def scripted(values):
    items = list(values)

    def next_value():
        return items.pop(0) if len(items) > 1 else items[0]

    return next_value


def sdcp_listener(tmp_path, statuses, light):
    return fel.FamilyListener(
        "elegoo_sdcp", scripted(statuses),
        claim_fn=lambda _f: False,
        start_poster=lambda kind, fn: True,
        pause_poster=lambda fn, conn, pid, status: True,
        terminal_poster=lambda kind, fn: True,
        retirer=lambda fn, headline: 1,
        beat=lambda **details: None,
        sleep=lambda _s: None, clock=lambda: 0.0,
        state_path=tmp_path / "fam.json", light=light)


def test_family_listener_feeds_the_light(tmp_path):
    light = FakeLight()
    rig = sdcp_listener(tmp_path, [st("standby"), st("printing"), st("complete"),
                                   st("complete")], light)
    rig.poll_once()   # standby: idle observations are forwarded (the policy
    # itself no-ops without an open window), nothing else happens
    assert (light.started, light.finished) == (0, 0)
    assert light.ticks == ["standby"]
    rig.poll_once()   # witnessed start
    assert light.started == 1
    rig.poll_once()   # terminal: window opens (and this poll is itself an idle tick)
    assert light.finished == 1
    rig.poll_once()   # still idle: another tick
    assert light.ticks == ["standby", "complete", "complete"]


def test_family_listener_baselined_print_never_turns_on_but_its_end_opens_the_window(tmp_path):
    light = FakeLight()
    rig = sdcp_listener(tmp_path, [st("printing"), st("complete")], light)
    rig.poll_once()   # first seen mid-print: baselined — no turn-on
    rig.poll_once()   # but a tracked print ending still opens the idle window
    assert light.started == 0
    assert light.finished == 1


def test_family_listener_without_a_light_is_unchanged(tmp_path):
    rig = sdcp_listener(tmp_path, [st("standby"), st("printing"), st("complete")], None)
    for _ in range(3):
        rig.poll_once()  # simply must not raise


# ------------------------------------------------------------ Bambu sink

def sink_with_light(light, claimed=True):
    return fc.BambuEventSink(
        claim_fn=lambda _f: claimed,
        start_poster=lambda kind, fn: True,
        pause_poster=lambda fn, conn, pid, status: True,
        terminal_poster=lambda kind, fn: True,
        retirer=lambda fn, headline: 1,
        light=light)


def test_bambu_sink_feeds_the_light_for_claimed_and_external_prints():
    for claimed in (True, False):
        light = FakeLight()
        sink = sink_with_light(light, claimed=claimed)
        sink("status", {"status": st("standby"), "transition": None})
        sink("status", {"status": st("printing"), "transition": "started"})
        assert light.started == 1   # the light follows the printer, not the claim split
        sink("completion", {"status": st("complete"), "transition": "finished"})
        assert light.finished == 1
        sink("status", {"status": st("complete"), "transition": None})
        assert light.ticks == ["standby", "complete"]


def test_bambu_sink_without_a_light_is_unchanged():
    sink = sink_with_light(None)
    sink("status", {"status": st("standby"), "transition": None})
    sink("status", {"status": st("printing"), "transition": "started"})
    sink("completion", {"status": st("complete"), "transition": "finished"})


# ------------------------------------------------------------- factories

class StubClient:
    def __init__(self, status):
        self._status = status

    def status(self):
        return self._status


class StubSdcpAdapter:
    connection = "elegoo_sdcp"
    label = "Elegoo Centauri Carbon"

    def __init__(self):
        self.sent = []
        self._client = StubClient({"Status": {"CurrentStatus": [0],
                                               "LightStatus": {"SecondLight": 1}}})

    def client(self):
        return self._client

    def set_light(self, on):
        self.sent.append(on)


class StubBambuAdapter:
    connection = "bambulab_lan"
    label = "Bambu Lab A1"

    def __init__(self, lights=True):
        self.sent = []
        report = [{"node": "chamber_light", "mode": "on"}] if lights else []
        self._snapshot = {"print": {"lights_report": report}, "info": None}

    def client(self):
        return self

    def snapshot(self):
        return self._snapshot

    def set_chamber_light(self, on):
        self.sent.append(on)

    def camera_snapshot(self):
        return b"frame"


def enabled_config(monkeypatch):
    from print_handy.runtime import _config
    monkeypatch.setattr(_config, "cfg",
                        lambda: {"vision": {"auto_light_off_minutes": 30}})


def test_auto_light_for_sdcp_builds_a_working_driver(monkeypatch):
    enabled_config(monkeypatch)
    monkeypatch.setattr(fc, "_mel", lambda: type("M", (), {"_vision_ready": staticmethod(lambda: True)})())
    adapter = StubSdcpAdapter()
    driver = fc.auto_light_for(adapter)
    assert driver is not None and driver.enabled
    driver.print_started()
    assert adapter.sent == [True]      # probe saw LightStatus; Cmd 403 send recorded
    driver.print_finished()


def test_auto_light_for_bambu_builds_a_working_driver(monkeypatch):
    enabled_config(monkeypatch)
    monkeypatch.setattr(fc, "_mel", lambda: type("M", (), {"_vision_ready": staticmethod(lambda: True)})())
    adapter = StubBambuAdapter()
    driver = fc.auto_light_for(adapter)
    assert driver is not None and driver.enabled
    driver.print_started()
    assert adapter.sent == [True]


def test_auto_light_for_bambu_without_a_chamber_light_never_sends(monkeypatch):
    enabled_config(monkeypatch)
    monkeypatch.setattr(fc, "_mel", lambda: type("M", (), {"_vision_ready": staticmethod(lambda: True)})())
    adapter = StubBambuAdapter(lights=False)
    driver = fc.auto_light_for(adapter)
    driver.print_started()
    assert adapter.sent == []          # lights_report present, no chamber light: unavailable


def test_auto_light_for_other_families_is_none():
    class StubOcto:
        connection = "octoprint"

    class StubOffline:
        connection = "offline"

    assert fc.auto_light_for(StubOcto()) is None
    assert fc.auto_light_for(StubOffline()) is None
    assert fc.auto_light_for(object()) is None
