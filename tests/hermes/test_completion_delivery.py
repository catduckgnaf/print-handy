"""Offline delivery regression coverage. Never contacts Discord or a printer."""
from print_handy.hermes import moonraker_event_listener as listener


def test_failed_completion_retries_and_success_survives_restart(monkeypatch, tmp_path):
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand", "pending_completion_message": "Print complete: Stand"})
    attempts = []
    results = iter([False, True])
    monkeypatch.setattr(listener, "post", lambda text: attempts.append(text) or next(results))
    listener.run_announcer("failure")
    assert announcer.load_state()["pending_completion_message"]
    listener.run_announcer("retry")
    assert not announcer.load_state().get("pending_completion_message")
    assert announcer.load_state()["announced_completion"] is True
    listener.run_announcer("restart")
    assert attempts == ["Print complete: Stand"] * 2


def test_failed_pending_retry_does_not_attempt_a_second_delivery(monkeypatch, tmp_path):
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: print("Print complete: Stand"))
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand", "pending_completion_message": "Print complete: Stand"})
    attempts = []
    monkeypatch.setattr(listener, "post", lambda text: attempts.append(text) or False)
    listener.run_announcer("retry")
    assert attempts == ["Print complete: Stand"]
    assert announcer.load_state()["pending_completion_message"] == "Print complete: Stand"


def test_video_success_acknowledges_pending_completion(monkeypatch, tmp_path):
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    announcer.save_state({"pending_completion_message": "Print complete: Stand"})
    monkeypatch.setattr(announcer, "archive_completed_timelapse",
                        lambda key: announcer.ArchiveOutcome(announcer.ARCHIVED, "/archive/stand.mp4"))
    monkeypatch.setattr(announcer, "materialize_archived_timelapse", lambda path: "/cache/stand.mp4")
    monkeypatch.setattr(listener, "platform_sized_video", lambda path, max_bytes=None: path)
    monkeypatch.setattr(listener, "post", lambda text: True)
    listener._timelapse_post("stand", "Print complete: Stand")
    assert not announcer.load_state().get("pending_completion_message")
    assert announcer.load_state()["announced_completion"] is True


def test_standby_pass_does_not_rerun_a_running_video_leg(monkeypatch, tmp_path):
    """complete -> standby re-ran the announcer while the first render was
    still running; it re-drove the pending video key and posted twice."""
    import threading
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand", "pending_completion_message": "Print complete: Stand",
                          "pending_timelapse_job_key": "stand"})
    release = threading.Event()
    legs = []

    def leg(job_key, message):
        legs.append(job_key)
        release.wait(5)
        listener._post_completion(message, message)

    posts = []
    monkeypatch.setattr(listener, "_timelapse_post", leg)
    monkeypatch.setattr(listener, "post", lambda text: posts.append(text) or True)
    assert listener._start_postprocess("stand", "Print complete: Stand", None, None, "first")
    listener.run_announcer("standby")
    assert posts == []
    assert announcer.pending_timelapse_job_key(announcer.load_state()) == "stand"
    release.set()
    for thread in threading.enumerate():
        if thread.name == "first":
            thread.join(5)
    assert legs == ["stand"]
    assert posts == ["Print complete: Stand"]
    assert not listener._POSTPROCESS_ACTIVE


def test_restart_still_redrives_a_pending_video_leg(monkeypatch, tmp_path):
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand", "pending_completion_message": "Print complete: Stand",
                          "pending_timelapse_job_key": "stand"})
    started = []
    monkeypatch.setattr(listener, "_timelapse_post", lambda key, message: started.append(key))
    listener.run_announcer("restart")
    for thread in __import__("threading").enumerate():
        if thread.name == "timelapse-postprocess-retry":
            thread.join(5)
    assert started == ["stand"]
    assert not announcer.pending_timelapse_job_key(announcer.load_state())
    assert not listener._POSTPROCESS_ACTIVE
