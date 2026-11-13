"""The local failure detector over real camera frames, through the replay harness.

The repo's real frames (fixtures/timelapse_real: a healthy print's camera
JPEGs) are replayed as one healthy session at the watch's schedule, and
the session must produce no *posted* alert under the weak-detection rules
(camera_watch.weak_noted): a lone weak box is noted, not posted.

* The real-weights check is opt-in: set ``PRINT_HANDY_REAL_DETECTOR`` to
  the path of the real 202 MB ``obico-failure-detector.onnx``. Without it
  the test skips; nothing is ever downloaded.
* The plumbing (a plain frame folder as a session, the posted-alert
  accounting, the ``python -m tests.hermes.vision_eval`` entry point) runs
  always, against the tiny synthetic stand-in model.
"""
import copy
import hashlib
import os
from pathlib import Path

import pytest

from print_handy.runtime import _config, local_vision as lv
from tests.hermes.vision_eval import harness
from tests.hermes.vision_eval import __main__ as vision_eval_cli

HERE = Path(__file__).parent
REAL_FRAMES = HERE / "fixtures" / "timelapse_real"
TINY_MODEL = HERE / "fixtures" / "tiny_failure_detector.onnx"
REAL_MODEL = os.environ.get("PRINT_HANDY_REAL_DETECTOR", "").strip()


@pytest.fixture
def detector_model(monkeypatch):
    """Point the process config's local detector at a model file, with clean detector caches."""
    for name, value in (("_sessions", {}), ("_active_providers", {}), ("_failures", {}),
                        ("_prefetching", {}), ("_verified_files", set()),
                        ("_announced", set()), ("_notices", [])):
        monkeypatch.setattr(lv, name, value)
    previous = copy.deepcopy(_config.cfg())

    def use(path):
        data = copy.deepcopy(previous)
        data.setdefault("vision", {})
        data["vision"]["local_detector_enabled"] = True
        data["vision"]["local_model_path"] = str(path)
        _config.set_override(data)

    yield use
    _config.set_override(previous)


@pytest.mark.skipif(not REAL_MODEL, reason="set PRINT_HANDY_REAL_DETECTOR to the real "
                                           "obico-failure-detector.onnx to run this check")
@pytest.mark.parametrize("risky", [True, False], ids=["early-window", "plain"])
def test_real_detector_posts_no_alert_on_real_healthy_frames(detector_model, risky):
    model = Path(REAL_MODEL).expanduser()
    assert model.is_file(), f"PRINT_HANDY_REAL_DETECTOR={REAL_MODEL}: no such file"
    if model.stat().st_size == lv.MODEL_SIZE_BYTES:
        digest = hashlib.sha256(model.read_bytes()).hexdigest()
        assert digest == lv.MODEL_SHA256, "the model file is not the pinned Obico weights"
    detector_model(model)
    report = harness.evaluate(harness.local_detector_producer,
                              sessions=[harness.folder_session(REAL_FRAMES, cadence_s=600)],
                              risky=risky)
    print("\n" + harness.format_report(report))
    row = report["sessions"][0]
    assert row["checks"], "the schedule made no check"
    assert all(c["verdict"] != "unavailable" for c in row["checks"]), row["checks"]
    assert harness.posted_alerts(row) == [], harness.posted_alerts(row)
    assert row["producer_outcome"] == "quiet"


def test_real_frames_replay_as_a_healthy_session_with_the_stand_in_model(detector_model):
    detector_model(TINY_MODEL)
    session = harness.folder_session(REAL_FRAMES, cadence_s=600)
    manifest = session["manifest"]
    assert manifest["label"] == "ok" and manifest["synthetic"] is False
    assert len(session["frames"]) == len(list(REAL_FRAMES.glob("frame-*.jpg"))) == 12
    row = harness.replay_session(session, harness.local_detector_producer)
    assert row["checks"] and all(c["verdict"] in {"ok", "uncertain", "spaghetti", "dark"}
                                 for c in row["checks"])
    assert all("posted" in c for c in row["checks"])


def test_folder_session_refuses_an_empty_folder(tmp_path):
    with pytest.raises(harness.EvalSetError, match="no frames"):
        harness.folder_session(tmp_path)


def test_the_cli_reports_a_frames_folder_with_a_model(detector_model, capsys):
    assert vision_eval_cli.main(["--model", str(TINY_MODEL), "--frames", str(REAL_FRAMES)]) == 0
    out = capsys.readouterr().out
    assert "session timelapse_real (ok):" in out and "posted alerts" in out
    assert "healthy false alerts:" in out


def test_the_cli_refuses_a_missing_model_instead_of_downloading(tmp_path, capsys):
    with pytest.raises(SystemExit):
        vision_eval_cli.main(["--model", str(tmp_path / "missing.onnx"),
                              "--frames", str(REAL_FRAMES)])
    assert "no such file" in capsys.readouterr().err
