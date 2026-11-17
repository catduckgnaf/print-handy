"""Web UI update notifications, click-to-update and update jobs (#1610).

Covers the toast store and source (print_handy.webui.notifications),
the Firmware page's update offers, the confirm/apply/status routes
(print_handy.webui.firmware_handlers), the background jobs
(print_handy.webui.update_jobs), the server's toast injection, and the
per-component duration estimates. Fake transports only — nothing here
touches a printer.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from print_handy import firmware_update as fw
from print_handy import settings
from print_handy.webui import firmware_handlers, firmware_page, notifications
from print_handy.webui import server as server_module
from print_handy.webui import update_jobs

GENERIC = fw.printer_entry("klipper.generic")

STATUS_BEHIND = {
    "result": {
        "version_info": {
            "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-310"},
            "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"},
            "system": {"package_count": 0, "package_list": []},
        },
        "busy": False,
    }
}

STATUS_CURRENT = {
    "result": {
        "version_info": {
            "klipper": {"version": "v0.12.0-310", "remote_version": "v0.12.0-310"},
            "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"},
            "system": {"package_count": 0, "package_list": []},
        },
        "busy": False,
    }
}


class StatefulTransport:
    """Moonraker call shape: the upgrade POST flips klipper to current."""

    def __init__(self, states=("standby",)):
        self.states = list(states)
        self.posts = []
        self.upgraded = False

    def __call__(self, method, path, body=None, timeout=None):
        if method == "POST":
            self.posts.append((path, body))
            if path == fw.UPDATE_UPGRADE_PATH:
                self.upgraded = True
            return {}
        if path == fw.UPDATE_STATUS_PATH:
            return STATUS_CURRENT if self.upgraded else STATUS_BEHIND
        if path == fw.PRINT_STATE_PATH:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        raise OSError(f"no such read: {path}")


def _data(tmp_path, *, controls=True, alerts=True):
    return {"paths": {"state_home": tmp_path},
            "webui": {"controls_enabled": controls},
            "firmware": {"update_alerts": alerts},
            "printers": [{"id": "kl1", "model": "klipper.generic",
                          "name": "Garage Klipper"}]}


@pytest.fixture(autouse=True)
def _clean_jobs():
    update_jobs._reset_for_tests()
    yield
    update_jobs._reset_for_tests()


# ------------------------------------------------------------------ estimates

def test_estimates_zmod_runs_longer_than_helixscreen():
    assert fw.update_estimate("zmod") == (300, 900)
    assert fw.update_estimate("Z-Mod") == (300, 900)
    assert fw.update_estimate("helixscreen") == (120, 300)
    assert fw.update_estimate("something-else") == fw.DEFAULT_UPDATE_ESTIMATE_S
    assert fw.update_estimate_text("zmod") == "roughly 5-15 minutes"
    assert fw.update_estimate_text("helixscreen") == "roughly 2-5 minutes"


# ------------------------------------------------------------------ the store

def _status(*components):
    return fw.UpdateStatus(tuple(components))


def test_recorded_observation_becomes_a_toast(tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="v0.12.0-289",
                             remote="v0.12.0-310")))
    found = notifications.current_notifications(data)
    assert len(found) == 1
    notif = found[0]
    assert notif.title == "Update available"
    assert "klipper v0.12.0-310 is available for Garage Klipper" in notif.body
    assert notif.action_url == "/firmware?printer=kl1"
    html = notifications.render_toasts(found, back="/", form_token="tok")
    assert 'class="toasts"' in html and 'class="toast"' in html
    # Dismiss is a CSRF-carrying POST form, never a GET link (#1614 review).
    assert 'method="post" action="/notifications/dismiss"' in html
    assert 'name="_csrf" value="tok"' in html and 'name="back" value="/"' in html
    assert "/notifications/dismiss?id=" not in html


def test_dismissal_is_per_offer_and_a_newer_release_notifies_again(tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2")))
    notif_id = notifications.current_notifications(data)[0].id
    notifications.dismiss(data, notif_id)
    assert notifications.current_notifications(data) == []
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="2", remote="3")))
    assert len(notifications.current_notifications(data)) == 1


def test_observation_replacement_and_alerts_switch(tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2")))
    assert len(notifications.current_notifications(data)) == 1
    # A later read with nothing available clears the toast.
    notifications.record_observation(data, "kl1", "Garage Klipper", _status())
    assert notifications.current_notifications(data) == []
    # [firmware] update_alerts off silences the Web UI source too.
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2")))
    assert notifications.current_notifications(
        _data(tmp_path, alerts=False)) == []


def test_clear_observed_update_drops_only_that_component(tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2"),
                fw.Component("moonraker", current="1", remote="2")))
    notifications.clear_observed_update(data, "kl1", "klipper")
    found = notifications.current_notifications(data)
    assert [n.id for n in found] == ["update:kl1:moonraker:2"]


# ------------------------------------------------------------------ the page

def _page(monkeypatch, tmp_path, *, controls):
    data = _data(tmp_path, controls=controls)
    monkeypatch.setattr(firmware_page.settings, "printers",
                        lambda d: d["printers"])
    observed = []
    transport = StatefulTransport()
    page = firmware_page.render_firmware_page(
        data, feed=None, checked="kl1",
        transport_for=lambda pid: transport,
        controls=controls,
        observe=lambda pid, name, status: observed.append((pid, status)))
    return page, observed


def test_checked_page_offers_update_with_estimate_when_controls_on(monkeypatch, tmp_path):
    page, observed = _page(monkeypatch, tmp_path, controls=True)
    assert "Updates available" in page
    assert 'href="/firmware/update?printer=kl1&amp;component=klipper"' in page
    assert "roughly 5-10 minutes" in page
    assert len(observed) == 1
    assert observed[0][1].updates_available[0].name == "klipper"


def test_checked_page_names_the_controls_gate_when_off(monkeypatch, tmp_path):
    page, _observed = _page(monkeypatch, tmp_path, controls=False)
    assert "Updates available" in page
    assert "/firmware/update?printer=" not in page
    assert "controls_enabled" in page


# ------------------------------------------------------------------ handlers

class FakeHandler:
    """The slice of the request handler the firmware routes use."""

    def __init__(self, data, path="/firmware/update"):
        self.server = SimpleNamespace(settings_data=data)
        self.path = path
        self.sent = []
        self.redirects = []
        self.refusals = []

    def _theme(self):
        return "dark"

    def _form_token(self):
        return "csrf-value"

    def _send(self, code, body, content_type, headers=()):
        self.sent.append((code, body))

    def _redirect(self, location, headers=()):
        self.redirects.append(location)

    def _html_refusal(self, code, message, back="/"):
        self.refusals.append((code, message, back))


def _wire(monkeypatch, transport):
    monkeypatch.setattr(settings, "printers", lambda d: d["printers"])
    monkeypatch.setattr(firmware_page, "printer_transport",
                        lambda data, pid: transport)


def test_confirm_page_shows_the_change_estimate_and_form(monkeypatch, tmp_path):
    _wire(monkeypatch, StatefulTransport())
    handler = FakeHandler(_data(tmp_path),
                          "/firmware/update?printer=kl1&component=klipper")
    firmware_handlers.update_confirm_route(handler)
    code, body = handler.sent[-1]
    assert code == 200
    assert "v0.12.0-289" in body and "v0.12.0-310" in body
    assert "roughly 5-10 minutes" in body
    assert '<form method="post" action="/firmware/update">' in body
    assert 'name="_csrf" value="csrf-value"' in body
    assert 'name="component" value="klipper"' in body


def test_confirm_page_with_controls_off_explains_and_offers_no_form(monkeypatch, tmp_path):
    _wire(monkeypatch, StatefulTransport())
    handler = FakeHandler(_data(tmp_path, controls=False),
                          "/firmware/update?printer=kl1&component=klipper")
    firmware_handlers.update_confirm_route(handler)
    _code, body = handler.sent[-1]
    assert "controls_enabled" in body and "<form" not in body


def test_confirm_page_for_a_busy_printer_refuses(monkeypatch, tmp_path):
    _wire(monkeypatch, StatefulTransport(states=("printing",)))
    handler = FakeHandler(_data(tmp_path),
                          "/firmware/update?printer=kl1&component=klipper")
    firmware_handlers.update_confirm_route(handler)
    _code, body = handler.sent[-1]
    assert "must not update" in body and "<form" not in body


def test_apply_with_controls_off_refuses_and_sends_nothing(monkeypatch, tmp_path):
    transport = StatefulTransport()
    _wire(monkeypatch, transport)
    handler = FakeHandler(_data(tmp_path, controls=False))
    firmware_handlers.update_apply_route(
        handler, {"printer": ["kl1"], "component": ["klipper"]})
    assert handler.refusals and handler.refusals[0][0] == 403
    assert transport.posts == []


def test_apply_starts_a_job_and_the_status_page_follows_it(monkeypatch, tmp_path):
    transport = StatefulTransport()
    _wire(monkeypatch, transport)
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="v0.12.0-289",
                             remote="v0.12.0-310")))
    handler = FakeHandler(data)
    firmware_handlers.update_apply_route(
        handler, {"printer": ["kl1"], "component": ["klipper"]})
    assert len(handler.redirects) == 1
    job_id = handler.redirects[0].split("job=")[1]
    job = update_jobs.get_job(job_id)
    assert job is not None
    deadline = time.time() + 10
    while job.running and time.time() < deadline:
        time.sleep(0.05)
    assert not job.running
    assert transport.posts and transport.posts[0][0] == fw.UPDATE_UPGRADE_PATH
    assert "Updated" in (job.outcome or "") and "verified" in job.outcome
    # The applied offer stops notifying.
    assert notifications.current_notifications(data) == []
    status_handler = FakeHandler(data, f"/firmware/update/status?job={job_id}")
    firmware_handlers.update_status_route(status_handler)
    _code, body = status_handler.sent[-1]
    assert "Updated" in body and "refresh" not in body


def test_status_page_for_a_running_job_refreshes(monkeypatch, tmp_path):
    transport = StatefulTransport()
    prepared = fw.prepare_update(transport, GENERIC, "klipper", None)
    job = update_jobs.start_job({}, printer_id="kl1", printer_name="Garage",
                                transport=transport, prepared=prepared)
    job.finished_at = None  # still running for this render
    handler = FakeHandler(_data(tmp_path),
                          f"/firmware/update/status?job={job.id}")
    firmware_handlers.update_status_route(handler)
    _code, body = handler.sent[-1]
    assert "Updating" in body and "roughly 5-10 minutes" in body
    assert '<meta http-equiv="refresh" content="15">' in body
    missing = FakeHandler(_data(tmp_path), "/firmware/update/status?job=nope")
    firmware_handlers.update_status_route(missing)
    assert missing.refusals and missing.refusals[0][0] == 404


def test_dismiss_route_records_and_redirects_back(monkeypatch, tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2")))
    notif_id = notifications.current_notifications(data)[0].id
    handler = FakeHandler(data, "/notifications/dismiss")
    firmware_handlers.dismiss_route(handler, {"id": [notif_id], "back": ["/firmware"]})
    assert handler.redirects == ["/firmware"]
    assert notifications.current_notifications(data) == []
    evil = FakeHandler(data, "/notifications/dismiss")
    firmware_handlers.dismiss_route(evil, {"id": ["x"], "back": ["//evil.example"]})
    assert evil.redirects == ["/"]


def test_dismissal_is_a_csrf_post_never_a_get(monkeypatch, tmp_path):
    """#1614 review: a crafted GET link cannot dismiss a notification."""
    from print_handy.webui import routes
    gets = [r for r in routes.ROUTES if r.method == "GET" and r.pattern == "/notifications/dismiss"]
    posts = [r for r in routes.ROUTES if r.method == "POST" and r.pattern == "/notifications/dismiss"]
    assert gets == []
    assert len(posts) == 1 and posts[0].policy == routes.SESSION_CSRF


def test_concurrent_confirmations_start_one_update_job(monkeypatch, tmp_path):
    """#1614 review: two confirmations racing for one printer and component start one job."""
    import threading
    transport = StatefulTransport()
    prepared = fw.prepare_update(transport, GENERIC, "klipper", None)
    sent = []
    gate = threading.Event()

    def slow_send(_transport, _prepared):
        sent.append(1)
        gate.wait(2)
        return "done"
    monkeypatch.setattr(fw, "send_update", slow_send)
    real_lock = update_jobs._LOCK
    barrier = threading.Barrier(2)

    class RacingLock:
        # Both threads reach the job check together before either inserts.
        def __enter__(self):
            try:
                barrier.wait(0.5)
            except threading.BrokenBarrierError:
                pass
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)
    monkeypatch.setattr(update_jobs, "_LOCK", RacingLock())
    jobs = []

    def confirm():
        jobs.append(update_jobs.start_job({}, printer_id="kl1", printer_name="Garage",
                                          transport=transport, prepared=prepared))
    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(3)
    time.sleep(0.2)
    gate.set()
    assert len(jobs) == 2 and jobs[0] is jobs[1]
    assert len(sent) == 1


# ------------------------------------------------------------------ injection

def test_server_injects_toasts_into_shell_pages_only(tmp_path):
    data = _data(tmp_path)
    notifications.record_observation(
        data, "kl1", "Garage Klipper",
        _status(fw.Component("klipper", current="1", remote="2")))
    handler = server_module._Handler.__new__(server_module._Handler)
    handler.server = SimpleNamespace(settings_data=data, form_token="proc-token",
                                     access=SimpleNamespace(required=False))
    handler.path = "/"
    page = b"<html><body><header class=\"site\">x</header><main>y</main></body></html>"
    out = handler._inject_toasts(page)
    assert b'class="toasts"' in out and b"Update available" in out
    # The Dismiss form carries this request's form token (#1614 review).
    assert b'name="_csrf" value="proc-token"' in out
    assert out.index(b"</header>") < out.index(b'class="toasts"')
    bare = b"<html><body><main>login</main></body></html>"
    assert handler._inject_toasts(bare) == bare
