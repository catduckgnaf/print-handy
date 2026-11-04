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

    def leg(key, message):
        started.append(key)
        listener._post_completion(message, message)  # the real leg delivers, then acknowledges

    monkeypatch.setattr(listener, "_timelapse_post", leg)
    monkeypatch.setattr(listener, "post", lambda text: True)
    listener.run_announcer("restart")
    for thread in __import__("threading").enumerate():
        if thread.name == "timelapse-postprocess-retry":
            thread.join(5)
    assert started == ["stand"]
    assert not announcer.pending_timelapse_job_key(announcer.load_state())
    assert not listener._POSTPROCESS_ACTIVE


def _join_postprocess_threads():
    import threading
    for thread in threading.enumerate():
        if thread.name.startswith("timelapse-postprocess"):
            thread.join(5)


def test_restart_retry_keeps_the_persisted_key_until_delivery(monkeypatch, tmp_path):
    """P2-145: a retry that dies before delivering must not lose the video
    leg — the persisted key survives until _post_completion acknowledges
    delivery, and a later pass re-drives it."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand", "pending_completion_message": "Print complete: Stand",
                          "pending_timelapse_job_key": "stand"})
    legs = []
    monkeypatch.setattr(listener, "_timelapse_post", lambda key, message: legs.append(key))
    listener.run_announcer("restart")
    _join_postprocess_threads()
    assert legs == ["stand"]
    # The leg ended without delivering: the key (and the pending text) survive.
    assert announcer.pending_timelapse_job_key(announcer.load_state()) == "stand"
    assert announcer.pending_completion_message(announcer.load_state()) == "Print complete: Stand"
    assert not listener._POSTPROCESS_ACTIVE
    # A later pass can therefore re-drive the same leg.
    listener.run_announcer("retry")
    _join_postprocess_threads()
    assert legs == ["stand", "stand"]
    assert announcer.pending_timelapse_job_key(announcer.load_state()) == "stand"


def test_restart_retry_restores_the_multipart_routing(monkeypatch, tmp_path):
    """P2-146: the retry replays the persisted plan routing and picker,
    not the bare single-plate post."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: None)
    monkeypatch.setattr(listener, "log", lambda text: None)
    state = {"job_key": "stand", "pending_completion_message": "Print complete: Stand",
             "pending_timelapse_job_key": "stand"}
    announcer.set_pending_timelapse_routing(state, "plan9", ("plan9", "more", 1))
    announcer.save_state(state)
    plan_posts = []
    pickers = []
    monkeypatch.setattr(listener, "_plan_timelapse_post",
                        lambda key, message, tl_plan: plan_posts.append((key, message, tl_plan)))
    monkeypatch.setattr(listener, "post_plate_picker", lambda plan_id: pickers.append(plan_id))
    listener.run_announcer("restart")
    _join_postprocess_threads()
    assert plan_posts == [("stand", "Print complete: Stand", ("plan9", "more", 1))]
    assert pickers == ["plan9"]


def test_completion_pass_persists_the_routing_beside_the_key(monkeypatch, tmp_path):
    """P2-146: a fresh completion's parsed routing is stored with its key."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: print(
        "TIMELAPSE_POSTPROCESS:stand.gcode\n"
        "Print complete: Stand\n"
        "TIMELAPSE_PLAN:plan9:more:1\n"
        "PLATE_PICKER:plan9"))
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand.gcode"})
    monkeypatch.setattr(listener, "_plan_timelapse_post", lambda key, message, tl_plan: None)
    monkeypatch.setattr(listener, "post_plate_picker", lambda plan_id: None)
    listener.run_announcer("completion")
    _join_postprocess_threads()
    state = announcer.load_state()
    assert announcer.pending_timelapse_routing(state) == ("plan9", ("plan9", "more", 1))


def test_pending_pass_processes_a_new_completions_markers(monkeypatch, tmp_path):
    """P2-38: while an old completion sits undelivered, a reprint of the
    same file completes and the announcer pass consumes its one-shot
    markers. The markers must be processed (the new print's video leg
    starts) even though the pass's text post is suppressed as a duplicate
    of the pending delivery."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(announcer, "main", lambda: print(
        "TIMELAPSE_POSTPROCESS:stand.gcode\n"
        "Print complete: Stand again"))
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"job_key": "stand.gcode",
                          "pending_completion_message": "Print complete: Stand",
                          "pending_timelapse_job_key": "stand"})
    legs = []
    monkeypatch.setattr(listener, "_timelapse_post", lambda key, message: legs.append((key, message)))
    posts = []
    monkeypatch.setattr(listener, "post", lambda text: posts.append(text) or True)
    listener.run_announcer("tick")
    _join_postprocess_threads()
    # The restart retry for the old key AND the new completion's own leg both ran.
    assert ("stand", "Print complete: Stand") in legs
    assert ("stand.gcode", "Print complete: Stand again") in legs
    # No bare text post of the announcer's message happened in this pass.
    assert posts == []


def test_completion_post_carries_exactly_one_roast_before_the_video(monkeypatch, tmp_path):
    """The roast rides the completion post that carries the timelapse, sits under the
    completion text, keeps MEDIA: lines on their own lines, and is cleared with the
    pending notice so a retry or replay cannot roast twice."""
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"pending_completion_message": "Print complete: Stand"})
    state = announcer.load_state()
    announcer.set_pending_roast_subject(state, "Stand", "a stand")
    announcer.save_state(state)
    calls = []
    monkeypatch.setattr(listener, "generate_roast", lambda title, summary="": calls.append((title, summary)) or "Roast for Stand")
    posted = []
    monkeypatch.setattr(listener, "post", lambda text: posted.append(text) or True)
    assert listener._post_completion("Print complete: Stand\nMEDIA:/cache/stand.mp4", "Print complete: Stand")
    assert posted == ["Print complete: Stand\n\nRoast for Stand\nMEDIA:/cache/stand.mp4"]
    assert calls == [("Stand", "a stand")]
    assert announcer.pending_roast_subject(announcer.load_state()) == ("", "")
    assert listener._post_completion("Print complete: Stand", "Print complete: Stand")
    assert len(calls) == 1 and posted[1] == "Print complete: Stand"


def test_completion_roast_failure_leaves_the_post_untouched(monkeypatch, tmp_path):
    announcer = listener.announcer
    monkeypatch.setattr(announcer, "STATE_PATH", tmp_path / "announcement.json")
    monkeypatch.setattr(listener, "log", lambda text: None)
    announcer.save_state({"pending_completion_message": "Print complete: Stand"})
    state = announcer.load_state()
    announcer.set_pending_roast_subject(state, "Stand", "")
    announcer.save_state(state)

    def boom(*a, **kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(listener, "generate_roast", boom)
    posted = []
    monkeypatch.setattr(listener, "post", lambda text: posted.append(text) or True)
    assert listener._post_completion("Print complete: Stand", "Print complete: Stand")
    assert posted == ["Print complete: Stand"]
