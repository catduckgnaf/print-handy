"""OctoPrint launched-print monitor contract (#373 audit).

Before this monitor existed, an OctoPrint print launched by Print Handy
got a start confirmation and then nothing: no completion post at all, and
the timelapse OctoPrint itself renders never reached the chat. These
tests pin the contract the monitor establishes — a completed job with a
rendered timelapse available attaches the media; every failure route
attaches, explains in the post, or logs; nothing is silent. Against the
pre-change code this module doesn't even import (there was no octo_watch
and no monitor), which is the missing-route defect stated as a test.
"""
import itertools

import pytest

from print_handy.runtime import octo_watch

SHA = "aabbccddeeff00112233445566778899" * 2
REMOTE = f"Benchy_plate_1_{SHA}.gcode"
MOVIE = f"Benchy_plate_1_{SHA}_20261026171200.mp4"


def _status(state, progress=None, filename=REMOTE):
    return {"state": state, "filename": filename, "progress": progress}


class Harness:
    def __init__(self, statuses, movies=(), video="/cache/movie.mp4"):
        self.posts, self.logs = [], []
        self.movies_calls = 0
        self._statuses = iter(statuses)
        self._movies = list(movies)
        self._video = video

    def status(self):
        return next(self._statuses)

    def movies(self):
        self.movies_calls += 1
        return list(self._movies)

    def video(self, movie):
        if isinstance(self._video, Exception):
            raise self._video
        return self._video

    def monitor(self, filename=REMOTE):
        ticks = itertools.count(0, 30)
        return octo_watch.OctoPrintMonitor(
            filename, status_fn=self.status, movies_fn=self.movies,
            video_fn=self.video, post=self.posts.append,
            clock=lambda: next(ticks), sleep=lambda s: None, poll_s=30)


@pytest.fixture(autouse=True)
def _capture_logs(monkeypatch):
    Harness.logs = []
    monkeypatch.setattr(octo_watch, "log", lambda line: Harness.logs.append(line)); monkeypatch.setattr(octo_watch, "warn", lambda line: Harness.logs.append(line))


COMPLETE_SEQ = [_status("printing", 0.5), _status("standby", 1.0)]


def test_completed_job_with_rendered_timelapse_attaches_media():
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": f"/downloads/timelapse/{MOVIE}"}])
    assert h.monitor().run() == "complete"
    assert len(h.posts) == 1
    assert "Print complete" in h.posts[0]
    assert "MEDIA:/cache/movie.mp4" in h.posts[0]


def test_movie_download_failure_is_explained_in_the_post():
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": "/x"}],
                video=RuntimeError("boom"))
    assert h.monitor().run() == "complete"
    assert len(h.posts) == 1
    assert "Print complete" in h.posts[0]
    assert "Timelapse not attached" in h.posts[0]
    assert "MEDIA:" not in h.posts[0]
    assert any("timelapse fetch failed" in line for line in Harness.logs)


def test_unsizable_movie_posts_the_too_big_line():
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": "/x"}], video=None)
    h._video = None
    assert h.monitor().run() == "complete"
    assert len(h.posts) == 1
    assert "Timelapse" in h.posts[0]
    assert "MEDIA:" not in h.posts[0]


def test_movie_listing_failure_is_explained_in_the_post():
    h = Harness(COMPLETE_SEQ)
    def boom():
        raise RuntimeError("api down")
    m = h.monitor()
    m._movies_fn = boom
    assert m.run() == "complete"
    assert len(h.posts) == 1
    assert "Timelapse not attached" in h.posts[0]


def test_no_rendered_movie_posts_text_and_logs_the_reason():
    h = Harness(COMPLETE_SEQ, movies=[])
    assert h.monitor().run() == "complete"
    assert len(h.posts) == 1
    assert "Print complete" in h.posts[0]
    assert "Timelapse not attached" not in h.posts[0]
    assert "MEDIA:" not in h.posts[0]
    assert any("no rendered timelapse listed" in line for line in Harness.logs)


def test_cancelled_job_posts_without_a_timelapse_lookup():
    h = Harness([_status("printing", 0.4), _status("standby", 0.4)],
                movies=[{"name": MOVIE, "url": "/x"}])
    assert h.monitor().run() == "cancelled"
    assert len(h.posts) == 1
    assert "cancel" in h.posts[0].lower()
    assert h.movies_calls == 0


def test_superseded_job_posts_nothing():
    h = Harness([_status("printing", 0.5, filename="someone-else.gcode")])
    assert h.monitor().run() is None
    assert h.posts == []


def test_sustained_unknown_state_after_printing_posts_error():
    seq = [_status("printing", 0.5)] + [_status("unknown")] * 12
    h = Harness(seq)
    assert h.monitor().run() == "error"
    assert len(h.posts) == 1


def test_status_read_failures_cost_polls_not_the_monitor():
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": "/x"}])
    m = h.monitor()
    real_status = m._status_fn
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("reconnecting")
        return real_status()
    m._status_fn = flaky
    assert m.run() == "complete"
    assert any("MEDIA:" in p for p in h.posts)
    assert any("status read failed" in line for line in Harness.logs)


def test_movie_for_job_matching():
    other = {"name": f"Other_plate_1_{SHA}_20261026171200.mp4"}
    older = {"name": f"Benchy_plate_1_{SHA}_20261025090000.mp4"}
    drifted = {"name": f"Benchy_plate_1_{SHA}_final.mp4"}
    wrong_ext = {"name": f"Benchy_plate_1_{SHA}_20261026171200.mov"}
    movies = [other, older, drifted, wrong_ext, {"name": MOVIE}]
    assert octo_watch.movie_for_job(movies, REMOTE)["name"] == MOVIE
    assert octo_watch.movie_for_job([other, drifted, wrong_ext], REMOTE) is None
    assert octo_watch.movie_for_job([], REMOTE) is None


def test_start_monitor_needs_a_post_target(monkeypatch):
    from print_handy.runtime import bambu_watch
    monkeypatch.setattr(bambu_watch, "_post_target_configured", lambda: False)
    assert octo_watch.start_monitor(object(), REMOTE) is None
    assert any("no chat is configured" in line for line in Harness.logs)


def test_start_monitor_wires_the_adapter(monkeypatch):
    class Client:
        def status(self):
            return {"printer": {"state": {"flags": {"printing": True}}},
                    "job": {"job": {"file": {"path": REMOTE}}, "progress": {"completion": 50.0}}}
        def timelapse_movies(self):
            return []
        def timelapse_unrendered(self):
            return []
    class Adapter:
        def client(self):
            return Client()
        def normalize_status(self, raw):
            return _status("printing", 0.5)
    started = {}
    def factory(target):
        class T:
            def start(self):
                started["target"] = target
        return T()
    thread = octo_watch.start_monitor(Adapter(), REMOTE, post=lambda m: None,
                                      thread_factory=factory)
    assert thread is not None
    assert "target" in started


def test_media_post_failure_falls_back_to_the_text_completion():
    """#391 (#373 expansion): the platform refusing the MEDIA post must not
    lose the completion — the monitor retries with the text alone and
    logs why, the same attach-or-explain contract as the Moonraker route."""
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": "/x"}])
    posts = []

    def flaky_post(message):
        posts.append(message)
        if "MEDIA:" in message:
            raise RuntimeError("platform refused the attachment")

    m = h.monitor()
    m._post = flaky_post
    assert m.run() == "complete"
    assert len(posts) == 2
    assert "MEDIA:" in posts[0]
    assert "Print complete" in posts[1] and "MEDIA:" not in posts[1]
    assert any("post with timelapse failed" in line for line in Harness.logs)


def test_start_monitor_tolerates_a_client_without_the_render_queue(monkeypatch):
    """P2-147 wiring: an adapter client with no timelapse_unrendered
    (older doubles, partial clients) must still start the monitor —
    pending_fn degrades to None (single lookup), never to no monitor."""
    class Client:
        def status(self):
            return {}
        def timelapse_movies(self):
            return []
    class Adapter:
        def client(self):
            return Client()
        def normalize_status(self, raw):
            return _status("printing", 0.5)
    started = {}
    def factory(target):
        class T:
            def start(self):
                started["target"] = target
        return T()
    thread = octo_watch.start_monitor(Adapter(), REMOTE, post=lambda m: None,
                                      thread_factory=factory)
    assert thread is not None
    assert "target" in started


def test_a_pending_render_is_waited_for_and_then_attached():
    """P2-147: OctoPrint renders in a background queue after the print
    ends, so the movie legitimately appears after the terminal poll.
    While the queue says this job is still rendering, the monitor keeps
    polling the listing instead of posting without the video."""
    h = Harness(COMPLETE_SEQ)
    m = h.monitor()
    calls = {"n": 0}

    def movies():
        calls["n"] += 1
        return [] if calls["n"] < 3 else [{"name": MOVIE, "url": "/x"}]

    m._movies_fn = movies
    m._pending_fn = lambda: [{"name": MOVIE}]
    assert m.run() == "complete"
    assert calls["n"] == 3
    assert len(h.posts) == 1 and "MEDIA:" in h.posts[0]


def test_a_render_that_never_lands_gives_up_bounded():
    h = Harness(COMPLETE_SEQ, movies=[])
    m = h.monitor()
    calls = {"n": 0}

    def movies():
        calls["n"] += 1
        return []

    m._movies_fn = movies
    m._pending_fn = lambda: [{"name": MOVIE}]
    assert m.run() == "complete"
    assert len(h.posts) == 1 and "MEDIA:" not in h.posts[0]
    assert 1 < calls["n"] <= 12  # it waited, but the wait is bounded


def test_no_pending_render_for_this_job_means_no_wait():
    h = Harness(COMPLETE_SEQ, movies=[])
    m = h.monitor()
    calls = {"n": 0}

    def movies():
        calls["n"] += 1
        return []

    m._movies_fn = movies
    m._pending_fn = lambda: [{"name": "Other_job_20261026171200.mp4"}]
    assert m.run() == "complete"
    assert calls["n"] == 1
    assert any("no rendered timelapse listed" in line for line in Harness.logs)


def test_movie_for_job_edge_shapes():
    """#391 (#373 expansion): the matcher pins — a non-.gcode job name is
    used whole, a movie with no timestamp digits is refused, a missing
    name is skipped, and the newest valid render wins."""
    plain = {"name": "widget_20261026120000.mp4"}
    assert octo_watch.movie_for_job([plain], "widget")["name"] == plain["name"]
    assert octo_watch.movie_for_job([{"name": "widget_.mp4"}], "widget") is None
    assert octo_watch.movie_for_job([{"url": "/x"}, {"name": ""}, None], "widget") is None
    separated = {"name": "widget_2026-10-26_171200.mp4"}
    assert octo_watch.movie_for_job([separated], "widget")["name"] == separated["name"]
    older = {"name": "widget_20261025090000.mp4"}
    newer = {"name": "widget_20261026171200.mp4"}
    assert octo_watch.movie_for_job([older, newer], "widget")["name"] == newer["name"]
    assert octo_watch.movie_for_job(None, "widget") is None


def test_a_refused_media_post_falls_back_to_the_text_completion():
    """The production sender returns False on a refusal instead of raising
    (the P2-169/P2-173 contract): a refused MEDIA post still owes the
    chat the plain completion text."""
    h = Harness(COMPLETE_SEQ, movies=[{"name": MOVIE, "url": "/x"}])
    attempts = []

    def post(message):
        attempts.append(message)
        return "MEDIA:" not in message

    m = h.monitor()
    m._post = post
    assert m.run() == "complete"
    assert len(attempts) == 2
    assert "MEDIA:" in attempts[0]
    assert "MEDIA:" not in attempts[1]
