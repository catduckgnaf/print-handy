"""The shared camera-watch core and the listener import boundary (#1320).

Three parts:

* The import guard: no printer listener other than the Moonraker
  listener itself imports ``moonraker_event_listener``, statically or
  lazily, for helpers. Before #1320 the family listener imported it for
  ``start_claimed`` and ``family_control`` (which both the Bambu Lab and
  family listeners run on) for the post path, the pause classes and the
  vision route. ``test_no_listener_imports_the_moonraker_listener_0b0ebac1``
  fails on the base commit 0b0ebac1.
* The per-check rules every watched family runs (camera_watch), including
  the two family rules that still differ, recorded in FAMILY_RULES.
* The per-printer watch coverage: active, or off and why, in each
  listener's heartbeat and in ``bot check`` and the diagnostics bundle.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from print_handy import listeners
from print_handy.hermes import camera_watch as cw
from print_handy.hermes import visual_watch as vw

ROOT = Path(__file__).resolve().parents[2]
MEL = "print_handy.hermes.moonraker_event_listener"

# The modules every non-Moonraker listener runs on: the registry's own
# listener modules plus the shared family module both the Bambu Lab sink
# and the polled families' listener call into.
SHARED_LISTENER_MODULES = ("print_handy.hermes.family_control",)


def _listener_modules():
    modules = {domain.module for domain in listeners.DOMAINS.values()
               if getattr(domain, "module", None)}
    assert MEL in modules, "the registry no longer names the Moonraker listener"
    modules.discard(MEL)
    assert modules, "no non-Moonraker listener in the registry"
    return sorted(modules | set(SHARED_LISTENER_MODULES))


def _imports_mel(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [alias.name for alias in node.names if alias.name.endswith("moonraker_event_listener")]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("moonraker_event_listener"):
                hits.append(node.module)
            hits += [alias.name for alias in node.names if alias.name == "moonraker_event_listener"]
    return hits


@pytest.mark.parametrize("module", _listener_modules())
def test_no_listener_imports_the_moonraker_listener_0b0ebac1(module):
    path = ROOT / Path(*module.split(".")).with_suffix(".py")
    assert path.is_file(), path
    assert _imports_mel(path) == [], (
        f"{module} imports moonraker_event_listener; shared listener helpers live in "
        "listener_common, start_claims and camera_watch (#1320)")


def test_listener_helper_paths_never_load_the_moonraker_listener():
    """Importing the listeners and reaching every moved helper leaves mel unloaded."""
    code = (
        "import sys\n"
        "from print_handy.hermes import family_event_listener, family_control, start_claims, camera_watch\n"
        "from print_handy.printers.bambulab import listener\n"
        "helpers = family_control._mel()\n"
        "helpers.classify_pause('Filament runout'); helpers._buttonless_standalone(); helpers._vision_chat_url\n"
        "start_claims.start_claimed('x.gcode'); start_claims.AbandonedStartWatch()\n"
        "family_control.LaunchLeaseSettler('p1')._watch_for()\n"
        "camera_watch.coverage('prusalink')\n"
        "print('loaded' if 'print_handy.hermes.moonraker_event_listener' in sys.modules else 'clean')\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "clean"


def test_the_moonraker_listener_keeps_every_moved_name():
    from print_handy.hermes import listener_common, moonraker_event_listener as mel, start_claims
    for name in ("post", "_post_rest", "_post_rest_discord", "_bot_token", "_buttonless_standalone",
                 "_telegram_standalone", "_matrix_standalone", "_profile_env_value", "_vision_ready",
                 "_vision_chat_url", "start_claim_time", "start_claimed", "claimed_start_filenames",
                 "release_start_claim"):
        assert callable(getattr(mel, name)), name
    assert mel.classify_pause is listener_common.classify_pause
    assert mel.AbandonedStartWatch is start_claims.AbandonedStartWatch
    assert mel.ABANDONED_START_COLD_S == start_claims.ABANDONED_START_COLD_S == 60.0
    assert mel._VISION_MODEL == listener_common._VISION_MODEL


def test_moonraker_seams_still_steer_the_shared_post_path(monkeypatch):
    """listener_common resolves through the Moonraker module, so its old patches still apply."""
    from print_handy.hermes import moonraker_event_listener as mel
    sent = []
    monkeypatch.setattr(mel, "_platform_name", lambda: "telegram")
    monkeypatch.setattr(mel, "_telegram_chat_id", lambda: "")
    monkeypatch.setattr(mel, "log", sent.append)
    assert mel.post("hello") is False
    assert sent == ["post skipped: telegram.chat_id is not configured"]


def test_start_claim_wrappers_read_the_listener_path(monkeypatch, tmp_path):
    from print_handy.hermes import moonraker_event_listener as mel, start_claims
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps({"a.gcode": 1000.0}))
    monkeypatch.setattr(mel, "START_CLAIMS", claims)
    assert mel.start_claimed("a.gcode", now=1001)
    assert start_claims.start_claimed("a.gcode", now=1001, path=claims)
    mel.release_start_claim("a.gcode")
    assert json.loads(claims.read_text()) == {}


# -- the per-check rules ---------------------------------------------------------

MOONRAKER = cw.FAMILY_RULES["moonraker"]
BAMBU = cw.FAMILY_RULES["bambulab_lan"]
NO_VETO = {"vision_veto": False}
VETO = {"vision_veto": True}


@pytest.mark.parametrize("verdict", ["air", "spaghetti", "detached", "uncertain", "no", "unsafe"])
def test_suspicious_verdicts_alert_for_every_family(verdict):
    for rules in (MOONRAKER, BAMBU):
        assert cw.is_suspicious(verdict, reason="periodic", changed=True, profile=NO_VETO, rules=rules)


def test_healthy_verdicts_do_not_alert():
    for rules in (MOONRAKER, BAMBU):
        assert not cw.is_suspicious("ok", reason="periodic", changed=True, profile=NO_VETO, rules=rules)


def test_empty_bed_while_printing_follows_each_familys_recorded_rule():
    # Moonraker re-checks first; its re-check alerts. Bambu Lab alerts at once.
    assert not cw.is_suspicious("empty_idle", reason="periodic", changed=None, profile=NO_VETO,
                                rules=MOONRAKER)
    assert cw.is_suspicious("empty_idle", reason="empty_idle_recheck", changed=None, profile=NO_VETO,
                            rules=MOONRAKER)
    assert not cw.is_suspicious("empty_idle", reason="empty_idle_recheck", changed=None, profile=NO_VETO,
                                rules=MOONRAKER, printing=False)
    assert cw.is_suspicious("empty_idle", reason="periodic", changed=None, profile=NO_VETO, rules=BAMBU)


def test_stall_veto_follows_each_familys_recorded_rule():
    # A healthy verdict vetoes a stall: always on Moonraker, by profile on Bambu Lab.
    assert not cw.is_suspicious("ok", reason="stalled_layers", changed=False, profile=NO_VETO,
                                rules=MOONRAKER)
    assert cw.is_suspicious("ok", reason="stalled_layers", changed=False, profile=NO_VETO, rules=BAMBU)
    assert not cw.is_suspicious("ok", reason="stalled_layers", changed=False, profile=VETO, rules=BAMBU)
    for rules in (MOONRAKER, BAMBU):
        assert cw.is_suspicious("uncertain", reason="stalled_layers", changed=False, profile=VETO,
                                rules=rules)


def test_promote_stall_and_keep_frame():
    watch = vw.VisualPrintWatch(sensitivity=3)
    stall_layers = watch.profile["stall_layers"]
    watch.record(5, 100.0, b"frame")
    reason, stall = cw.promote_stall(watch, "layer_progress", False, 5 + stall_layers)
    assert (reason, stall) == ("stalled_layers", True)
    assert watch.alerted_stall_layer == 5 + stall_layers
    # Only one stall per print.
    assert cw.promote_stall(watch, "periodic", False, 6 + stall_layers) == ("periodic", False)
    # A changed frame is no stall.
    fresh = vw.VisualPrintWatch(sensitivity=3)
    fresh.record(5, 100.0, b"frame")
    assert cw.promote_stall(fresh, "layer_progress", True, 5 + stall_layers) == ("layer_progress", False)
    # A layer-progress probe adopts the frame without moving the clock.
    assert cw.keep_frame(fresh, "layer_progress", 9, b"new", 500.0) is True
    assert (fresh.last_snapshot, fresh.last_snapshot_layer, fresh.last_check_at) == (b"new", 9, 100.0)
    assert cw.keep_frame(fresh, "periodic", 10, b"newer", 600.0) is False
    assert (fresh.last_snapshot_layer, fresh.last_check_at) == (10, 600.0)


def test_alert_texts():
    assert cw.alert_text("layer2", 2, "spaghetti", "tangle") == "layer2 at layer 2: spaghetti; tangle"
    assert cw.alert_text("periodic", None, "uncertain", "") == (
        "periodic at layer unknown: uncertain; visual check needs operator review")
    paused = cw.alert_text("layer2", 2, "spaghetti", "tangle", pause_why="two views")
    assert paused.endswith("\nAuto-pause (visual_watch.auto_pause_early): two views, "
                           "so I am pausing the print. It is not cancelled.")
    assert cw.alert_text("stalled_layers", 12, "ok", "fine") == vw.stall_alert_text(12, "ok", "fine")
    assert cw.vision_failed_text("periodic", 3).startswith(
        "periodic at layer 3: the vision check could not be completed")
    assert cw.camera_failed_text("periodic", None).startswith(
        "periodic at layer unknown: the camera snapshot could not be captured")
    assert cw.watch_failed("uncertain", vw.VISION_FAILED_DETAIL)
    assert not cw.watch_failed("uncertain", "blurry")


def test_decide_auto_pause_reserves_once(monkeypatch):
    seen = []

    def decision(members, **kwargs):
        seen.append(kwargs["already_paused"])
        return (not kwargs["already_paused"]), "two independent views"

    monkeypatch.setattr(vw, "auto_pause_decision", decision)
    watch = vw.VisualPrintWatch(sensitivity=3)
    kwargs = dict(enabled=True, printer_id="p1", identity="i", state="printing",
                  verdict="spaghetti", frame_age_s=1.0, early=True)
    assert cw.decide_auto_pause(watch, **kwargs) == (True, "two independent views")
    assert watch.auto_paused is True
    assert cw.decide_auto_pause(watch, **kwargs)[0] is False
    assert seen == [False, True]


def test_both_watches_share_one_deadline_and_prompt():
    from print_handy.hermes import bambu_watch, moonraker_event_listener as mel
    assert mel.VISUAL_CHECK_DEADLINE_S == bambu_watch.CHECK_DEADLINE_S == cw.CHECK_DEADLINE_S
    assert bambu_watch.WATCH_PROMPT == cw.WATCH_PROMPT


# -- coverage --------------------------------------------------------------------

def test_coverage_records_active_or_off_and_why():
    assert cw.coverage("moonraker") == {"family": "Moonraker (Klipper)", "watched": True,
                                        "active": True, "why": ""}
    off = cw.coverage("bambulab_lan", unavailable_reason="no vision endpoint")
    assert (off["active"], off["why"]) == (False, "no vision endpoint")
    assert cw.coverage("moonraker", enabled=False)["why"] == "visual_watch.enabled is false"
    family = cw.coverage("prusalink")
    assert family["watched"] is False and family["active"] is False
    assert "no in-print watch" in family["why"]
    assert cw.coverage_text({"active": True}) == "active"
    assert cw.coverage_text(off) == "off (no vision endpoint)"
    assert cw.coverage_text(None) == ""


def test_listener_coverage_uses_the_camera_reason_first_and_never_prefetches(monkeypatch):
    calls = []
    monkeypatch.setattr(vw, "watch_unavailable_reason",
                        lambda api_key, prefetch=True: calls.append(prefetch) or "no route")
    cfg = {"visual_watch": {"enabled": True}}
    assert cw.listener_coverage("moonraker", cfg, lambda: "", camera_reason="no camera")["why"] == "no camera"
    assert cw.listener_coverage("moonraker", cfg, lambda: "")["why"] == "no route"
    assert calls == [False]
    assert cw.listener_coverage("moonraker", {"visual_watch": {"enabled": False}}, lambda: "")["why"] == (
        "visual_watch.enabled is false")


def test_coverage_reporter_caches_and_survives_a_failing_compute():
    now = [0.0]
    results = [{"active": True}, RuntimeError("boom")]

    def compute():
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    reporter = cw.CoverageReporter(compute, ttl_s=10, clock=lambda: now[0])
    assert reporter.current() == {"active": True}
    now[0] = 5
    assert reporter.current() == {"active": True} and results  # cached, not recomputed
    now[0] = 11
    assert reporter.current() == {"active": True}  # compute raised: last good record kept


def test_coverage_report_adds_what_each_listener_reported(monkeypatch):
    monkeypatch.setattr(vw, "watch_unavailable_reason", lambda api_key, prefetch=True: "")
    cfg = {"visual_watch": {"enabled": True},
           "printers": [{"id": "k1", "model": _model_for("moonraker")},
                        {"id": "mk4", "model": _model_for("prusalink")}]}
    live = {"k1": cw.coverage("moonraker", unavailable_reason="no camera snapshot URL is configured"),
            "mk4": None}
    rows = cw.coverage_report(cfg, lambda: "", live=live.get)
    assert rows[0][1].endswith("; listener reports: off (no camera snapshot URL is configured)")
    assert rows[1][1] == "mk4: PrusaLink: no in-print watch (idle plate checks only)"
    assert vw.coverage_report(cfg, lambda: "")[1][1] == rows[1][1]


def test_live_coverage_reads_the_heartbeat(monkeypatch, tmp_path):
    from print_handy import healthcheck
    path = tmp_path / "listener_health.json"
    monkeypatch.setattr(healthcheck, "heartbeat_path", lambda data=None, printer_id=None: path)
    assert cw.live_coverage({}, "p1") is None
    path.write_text(json.dumps({"time": 1, "camera_watch": cw.coverage("prusalink")}))
    assert cw.live_coverage({}, "p1")["family"] == "PrusaLink"


def test_family_listener_heartbeat_reports_its_coverage(tmp_path):
    from print_handy.hermes import family_event_listener as fel
    beats = []
    listener = fel.FamilyListener("prusalink", lambda: {"state": "standby"}, printer_id="mk4",
                                  beat=lambda **details: beats.append(details),
                                  state_path=tmp_path / "state.json", clock=lambda: 0.0)
    listener._beat(True, "standby")
    assert beats[-1]["camera_watch"] == cw.coverage("prusalink")


def test_moonraker_heartbeat_reports_its_coverage(monkeypatch):
    from unittest import mock
    from print_handy.hermes import moonraker_event_listener as mel
    monkeypatch.setattr(mel, "_WATCH_COVERAGE",
                        cw.CoverageReporter(lambda: cw.coverage("moonraker")))
    listener = mel.Listener.__new__(mel.Listener)
    listener.state = "standby"
    listener.moonraker_connected = True
    captured = {}
    with mock.patch.object(mel.healthcheck, "beat", side_effect=lambda **kw: captured.update(kw)):
        listener._beat_once()
    assert captured["camera_watch"]["active"] is True


def test_bambu_listener_heartbeat_reports_its_coverage(monkeypatch):
    from print_handy.printers.bambulab import listener as bl
    beats = []
    listener = bl.BambuListener.__new__(bl.BambuListener)
    listener._clock = lambda: 0.0
    listener._connected = False
    listener._last_message_at = None
    listener._status = None
    listener._beat_fn = lambda **details: beats.append(details)
    listener._coverage = cw.CoverageReporter(
        lambda: cw.coverage("bambulab_lan", unavailable_reason="no vision endpoint"))
    listener._beat_now()
    assert beats[-1]["camera_watch"]["why"] == "no vision endpoint"


def test_diagnostics_bundle_names_the_reported_coverage(monkeypatch, tmp_path):
    from print_handy import diagnostics_bundle, healthcheck, settings, supervisor_state
    path = tmp_path / "listener_health.json"
    monkeypatch.setattr(healthcheck, "heartbeat_path", lambda data=None, printer_id=None: path)
    monkeypatch.setattr(supervisor_state, "read_all", lambda data: {})
    monkeypatch.setattr(settings, "printers", lambda data: [{"id": "mk4"}])
    path.write_text(json.dumps({"time": 100.0, "camera_watch": cw.coverage("prusalink")}))
    text = diagnostics_bundle.runtime_section({"paths": {}}, now=110.0)
    assert "camera watch off (this printer family has no in-print watch" in text


def _model_for(connection):
    from print_handy import printers
    for key in printers.MODELS:
        model = printers.MODELS.get(key)
        if getattr(getattr(model, "vendor", None), "connection", None) == connection:
            return key
    pytest.skip(f"no model with connection {connection}")
