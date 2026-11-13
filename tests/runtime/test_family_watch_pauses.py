"""#460: every family's launched-print monitor posts its pause alerts.

One rig per family, the same script it always ran — printing, paused,
resumed at the printer, paused again, complete — driven through the
real monitor class with a real family_control.PauseAlerts wired to
recording posters. Before #460 "paused" was a silent active state in
all five monitors: the pause post list was empty by construction.
"""
from print_handy.runtime import (bambu_watch, elegoo_watch, family_control as fc,
                                octo_watch, prusa_watch, stock_watch)

REMOTE = "benchy.gcode"
SCRIPT = ["printing", "paused", "paused", "printing", "paused", "complete"]


def st(state, filename=REMOTE):
    status = {"state": state, "filename": filename, "progress": 0.5}
    if state == "complete":
        status["progress"] = 1.0
    return status


def scripted(values):
    items = list(values)

    def next_value():
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return next_value


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Recorder:
    def __init__(self):
        self.posts = []
        self.retires = []

    def post(self, filename, connection, printer_id, status):
        self.posts.append((filename, connection))
        return True

    def retire(self, filename, headline):
        self.retires.append((filename, headline))
        return 1


def run_monitor(monitor, rec):
    outcome = monitor.run()
    # Two pause episodes -> exactly two pause alerts; the terminal poll
    # retires them under the completion headline.
    assert [p[0] for p in rec.posts] == [REMOTE, REMOTE]
    assert rec.retires == [(REMOTE, "Print complete.")]
    return outcome


def alerts(rec, connection):
    return fc.PauseAlerts(REMOTE, connection, poster=rec.post, retirer=rec.retire)


def statuses(script=SCRIPT):
    return scripted([st(state) for state in script])


def test_prusa_monitor_posts_pause_alerts():
    rec, clock = Recorder(), FakeClock()
    monitor = prusa_watch.PrusaPrintMonitor(
        REMOTE, status_fn=statuses(), camera_source_fn=lambda: (None, None),
        post=lambda _m: True, pause_alerts=alerts(rec, "prusalink"),
        clock=clock, sleep=clock.sleep)
    assert run_monitor(monitor, rec) == "complete"
    assert {p[1] for p in rec.posts} == {"prusalink"}


def test_elegoo_monitor_posts_pause_alerts():
    rec, clock = Recorder(), FakeClock()
    monitor = elegoo_watch.ElegooPrintMonitor(
        REMOTE, status_fn=statuses(), camera_source_fn=lambda: (None, None),
        post=lambda _m: True, pause_alerts=alerts(rec, "elegoo_sdcp"),
        clock=clock, sleep=clock.sleep)
    assert run_monitor(monitor, rec) == "complete"
    assert {p[1] for p in rec.posts} == {"elegoo_sdcp"}


def test_stock_monitor_posts_pause_alerts():
    rec, clock = Recorder(), FakeClock()
    monitor = stock_watch.StockPrintMonitor(
        REMOTE, status_fn=statuses(), camera_url_fn=lambda: None,
        stream_fn=lambda _a: None,
        post=lambda _m: True, pause_alerts=alerts(rec, "flashforge_stock"),
        clock=clock, sleep=clock.sleep)
    assert run_monitor(monitor, rec) == "complete"
    assert {p[1] for p in rec.posts} == {"flashforge_stock"}


def test_bambu_monitor_posts_pause_alerts():
    rec, clock = Recorder(), FakeClock()
    monitor = bambu_watch.BambuPrintMonitor(
        REMOTE, status_fn=statuses(), camera_fn=lambda: None,
        post=lambda _m: True, pause_alerts=alerts(rec, "bambulab_lan"),
        clock=clock, sleep=clock.sleep)
    assert run_monitor(monitor, rec) == "complete"
    assert {p[1] for p in rec.posts} == {"bambulab_lan"}


def test_octo_monitor_posts_pause_alerts():
    rec, clock = Recorder(), FakeClock()
    # OctoPrint has no "complete" state: standby at full progress is the end.
    script = [st("printing"), st("paused"), st("paused"), st("printing"),
              st("paused"), {**st("standby"), "progress": 1.0}]
    monitor = octo_watch.OctoPrintMonitor(
        REMOTE, status_fn=scripted(script), movies_fn=lambda: [],
        video_fn=lambda _m: None,
        post=lambda _m: True, pause_alerts=alerts(rec, "octoprint"),
        clock=clock, sleep=clock.sleep)
    assert run_monitor(monitor, rec) == "complete"
    assert {p[1] for p in rec.posts} == {"octoprint"}


def test_monitor_without_pause_alerts_is_the_old_behaviour():
    # pause_alerts=None (every pre-#460 construction) posts no pause
    # alert and retires nothing: the terminal post is the only post.
    clock = FakeClock()
    posts = []
    monitor = prusa_watch.PrusaPrintMonitor(
        REMOTE, status_fn=statuses(), camera_source_fn=lambda: (None, None),
        post=posts.append, clock=clock, sleep=clock.sleep)
    assert monitor.run() == "complete"
    assert len(posts) == 1
