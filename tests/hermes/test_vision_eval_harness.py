"""Stage 0 evaluation harness (#455 item 4): manifest contract, cadence
replay, per-print outcome accounting, and the box-geometry experiment.

All offline and synthetic: the sessions under fixtures/vision_eval are
generated format fixtures (labeled synthetic in their manifests), and the
producers under test are scripted or box-scripted — no real camera frame
and no model file is involved. What is pinned here is the harness's
accounting: a healthy session that alerts is a false alert, a healthy
session with two consecutive auto-pause-set verdicts in the early window
is a false confirmation, and a labeled failure session is scored detected
only when the producer actually names or alerts it.
"""
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from tests.hermes.vision_eval import harness

ROOT = harness.FIXTURES_ROOT


def _jpeg(value=128, size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, (value, value, value)).save(buf, "JPEG")
    return buf.getvalue()


def _write_session(tmp_path, name="s1", label="ok", frames=6, synthetic=True):
    sdir = tmp_path / name
    sdir.mkdir(parents=True)
    entries = []
    for i in range(frames):
        fn = f"frame-{i + 1:03d}.jpg"
        (sdir / fn).write_bytes(_jpeg(100 + i))
        entries.append({"file": fn, "label": label, "source": "synthetic",
                        "license": "test", "synthetic": synthetic})
    manifest = {"session": name, "label": label, "source": "synthetic",
                "license": "test", "synthetic": synthetic, "cadence_s": 60,
                "layers": list(range(1, frames + 1)), "frames": entries}
    (sdir / "manifest.json").write_text(json.dumps(manifest))
    return sdir


# --------------------------------------------------------------- manifest contract

def test_shipped_fixtures_load_and_are_labeled_synthetic():
    sessions = harness.load_eval_set()
    assert len(sessions) == 3
    for session in sessions:
        manifest = session["manifest"]
        assert manifest["synthetic"] is True
        assert manifest["source"] == "synthetic"
        assert len(session["frames"]) == len(manifest["frames"])
        for entry in manifest["frames"]:
            assert entry["synthetic"] is True and entry["source"] == "synthetic"


def test_manifest_missing_field_is_refused(tmp_path):
    sdir = _write_session(tmp_path)
    manifest = json.loads((sdir / "manifest.json").read_text())
    del manifest["frames"][0]["license"]
    (sdir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(harness.EvalSetError, match="license"):
        harness.load_session(sdir)


def test_frame_synthetic_mismatch_is_refused(tmp_path):
    sdir = _write_session(tmp_path)
    manifest = json.loads((sdir / "manifest.json").read_text())
    manifest["frames"][0]["synthetic"] = False
    (sdir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(harness.EvalSetError, match="disagrees"):
        harness.load_session(sdir)


def test_unknown_label_is_refused(tmp_path):
    sdir = _write_session(tmp_path, label="bogus")
    with pytest.raises(harness.EvalSetError, match="label"):
        harness.load_session(sdir)


# --------------------------------------------------------------- per-print accounting

def test_healthy_session_quiet_under_ok_producer(tmp_path):
    session = harness.load_session(_write_session(tmp_path))
    row = harness.replay_session(session, harness.scripted_producer({}))
    assert row["label"] == "ok" and row["producer_outcome"] == "quiet"
    assert row["alert_fired"] is False and row["streak_confirmed"] == ""


def test_healthy_session_false_alert_is_counted(tmp_path):
    session = harness.load_session(_write_session(tmp_path))
    producer = harness.scripted_producer({0: ("uncertain", "unsure")})
    report = harness.evaluate(producer, sessions=[session])
    assert report["false_alert_sessions"] == 1
    assert report["false_confirmation_sessions"] == 0
    assert report["sessions"][0]["producer_outcome"] == "false_alert"


def test_a_weak_detection_that_did_not_persist_is_not_a_false_alert(tmp_path):
    # The watches only note a lone weak local detection (camera_watch.weak_noted);
    # the replay's alert policy must not score it as an alert either.
    from print_handy.runtime import local_vision as lv
    session = harness.load_session(_write_session(tmp_path))
    noted = harness.replay_session(session, harness.scripted_producer(
        {0: ("uncertain", lv._weak_detail(0.12, 1))}))
    assert noted["producer_outcome"] == "quiet" and harness.posted_alerts(noted) == []
    posted = harness.replay_session(session, harness.scripted_producer(
        {0: ("uncertain", lv._weak_detail(0.12, 1)), 1: ("uncertain", lv._weak_detail(0.12, 2))}))
    assert posted["producer_outcome"] == "false_alert"
    assert [c["detail"] for c in harness.posted_alerts(posted)] == [lv._weak_detail(0.12, 2)]


def test_healthy_session_false_confirmation_needs_the_streak(tmp_path):
    # One early suspicious verdict alerts but does not confirm; two
    # consecutive ones inside the early window are the false confirmation
    # the #913 auto-pause consumer would have acted on.
    session = harness.load_session(_write_session(tmp_path, frames=8))
    single = harness.replay_session(
        session, harness.scripted_producer({0: ("air", "no plastic")}))
    assert single["producer_outcome"] == "false_alert"
    double = harness.replay_session(
        session, harness.scripted_producer(
            {0: ("air", "no plastic"), 1: ("air", "still none")}))
    assert double["producer_outcome"] == "false_confirmation"
    assert double["streak_verdict"] == "air"


def test_labeled_session_detected_only_when_named_or_alerted(tmp_path):
    session = harness.load_session(_write_session(tmp_path, label="detached"))
    named = harness.replay_session(
        session, harness.scripted_producer({0: ("detached", "part moved")}))
    assert named["producer_outcome"] == "detected_named"
    alert_only = harness.replay_session(
        session, harness.scripted_producer({0: ("uncertain", "unsure")}))
    assert alert_only["producer_outcome"] == "detected_alert_only"
    missed = harness.replay_session(session, harness.scripted_producer({}))
    assert missed["producer_outcome"] == "missed"


def test_replay_uses_the_watch_schedule_not_every_frame(tmp_path):
    # A 60 s-cadence session of 6 frames at sensitivity 3 is checked only
    # where the schedule owes a look — the layer milestones and the
    # 120 s early-window rhythm — never on a plain per-frame loop with
    # the print clock standing still. Give the session 30 frames: the
    # schedule checks a strict subset of them.
    session = harness.load_session(_write_session(tmp_path, frames=30))
    row = harness.replay_session(session, harness.scripted_producer({}))
    assert 0 < len(row["checks"]) < 30
    checked = [c["frame_index"] for c in row["checks"]]
    assert checked == sorted(set(checked))  # in order, no repeats


# --------------------------------------------------------------- box-geometry experiment

def _boxes_producer(schedule_script):
    """Boxes keyed by *frame index*; the producer answers the schedule."""
    def produce(frame, ctx):
        return "ok", "scripted boxes", schedule_script.get(ctx["frame_index"], [])
    return produce


def test_geometry_heuristic_fires_on_vanish_and_relocation():
    box = [(0.9, (100.0, 100.0, 160.0, 160.0))]
    moved = [(0.9, (420.0, 100.0, 480.0, 160.0))]
    state = None
    step = harness.box_geometry_step(state, box, "spaghetti")
    state = step["state"]
    assert step["candidate"] is False  # first sighting is a baseline, not a signal
    step = harness.box_geometry_step(state, [], "ok")
    assert step["candidate"] is True and step["kind"] == "vanished"
    step = harness.box_geometry_step(state, box, "spaghetti")
    state = step["state"]
    step = harness.box_geometry_step(state, moved, "spaghetti")
    assert step["candidate"] is True and step["kind"] == "relocated"
    state = step["state"]
    step = harness.box_geometry_step(state, moved, "spaghetti")
    assert step["candidate"] is False  # steady box: no discontinuity


def test_geometry_experiment_scores_against_labels(tmp_path):
    healthy = harness.load_session(_write_session(tmp_path / "h", name="h"))
    detached = harness.load_session(
        _write_session(tmp_path / "d", name="d", label="detached"))
    # A producer whose strong box vanishes mid-session: the heuristic
    # fires on both sessions, so the report shows it cannot separate
    # them — the shape of the negative result the scope predicts. The
    # schedule checks frames 1 and 4 (layers 2 and 5) in a 6-frame
    # 60 s-cadence session, so the box is present at frame 1 and gone
    # by frame 4.
    box = [(0.9, (10.0, 10.0, 40.0, 40.0))]
    script = {1: box}
    report = harness.evaluate(_boxes_producer(script), sessions=[healthy, detached])
    geo = report["geometry_experiment"]
    assert geo["healthy_sessions"] == 1 and geo["detached_sessions"] == 1
    assert geo["sessions_fired_healthy"] == geo["sessions_fired_detached"]


def test_box_geometry_state_does_not_carry_across_steps():
    # box_geometry_step is a pure transition: the same (state, boxes)
    # pair always yields the same answer, so a replay cannot leak state
    # between sessions through it.
    box = [(0.9, (100.0, 100.0, 160.0, 160.0))]
    first = harness.box_geometry_step(None, box, "spaghetti")
    again = harness.box_geometry_step(first["state"], box, "spaghetti")
    assert again["candidate"] is False
    fresh = harness.box_geometry_step(None, box, "spaghetti")
    assert fresh["candidate"] is False and fresh["state"] == first["state"]


def test_shipped_fixtures_replay_end_to_end():
    report = harness.evaluate(harness.scripted_producer({}))
    assert report["session_count"] == 3 and report["checks_total"] > 0
    text = harness.format_report(report)
    assert "false confirmation" in text and "box-geometry" in text
