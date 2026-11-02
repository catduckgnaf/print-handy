"""Offline slice verification keeps LAN write-readiness out (P2-73).

printer_state.check(live=False) is what model_job.verify_gcode uses for the
offline/LAN families: the record-only readiness checks still run, but no
live snapshot is fetched, so a busy printer or Developer Mode being off
cannot fail a slice-time verify. The print paths re-check live readiness
before any write, and the default check() stays live.
"""
from print_handy.core import printer_state

STATE = {"bed_mm": [220, 220], "spool": {}}


class _BusyClient:
    def snapshot(self):
        raise AssertionError("an offline check must not fetch a live snapshot")

    def status(self):
        raise AssertionError("an offline check must not fetch a live status")

    def detail(self):
        raise AssertionError("an offline check must not fetch a live detail")


class _LanAdapter:
    """The shared shape of the Bambu/Prusa/Elegoo LAN branches in check()."""

    connection = "bambulab_lan"

    def __init__(self, transport):
        self.transport = transport
        self.readiness_statuses = []

    def lan_configured(self):
        return True

    def link_configured(self):
        return True

    def sdcp_configured(self):
        return True

    def client(self):
        return _BusyClient()

    def normalize_status(self, snapshot):
        return {"snapshot": snapshot}

    def readiness_problems(self, state, status):
        self.readiness_statuses.append(status)
        return ["printer is busy"] if status else []


class _FlashforgeAdapter:
    transport = "flashforge_stock"
    connection = "flashforge_stock"

    def __init__(self):
        self.readiness_calls = 0

    def client(self):
        return _BusyClient()

    def normalize_status(self, detail):
        return {"detail": detail}

    def readiness_problems(self, state, detail):
        self.readiness_calls += 1
        return [] if detail else ["no live detail: not ready"]


def test_lan_families_skip_the_live_snapshot_when_not_live():
    for transport in ("bambulab_lan", "prusalink", "elegoo_sdcp"):
        adapter = _LanAdapter(transport)
        ok, probs, _facts = printer_state.check(dict(STATE), adapter=adapter, live=False)
        assert ok and probs == []
        assert adapter.readiness_statuses == [{}]  # record-only checks still ran


def test_lan_family_live_check_still_fetches_the_snapshot():
    adapter = _LanAdapter("bambulab_lan")

    class LiveClient:
        def snapshot(self):
            return {"gcode_state": "RUNNING"}

    adapter.client = lambda: LiveClient()
    ok, probs, _facts = printer_state.check(dict(STATE), adapter=adapter)
    assert not ok and probs == ["printer is busy"]
    assert adapter.readiness_statuses == [{"gcode_state": "RUNNING"}]


def test_flashforge_offline_check_never_judges_an_empty_detail():
    adapter = _FlashforgeAdapter()
    ok, probs, _facts = printer_state.check(dict(STATE), adapter=adapter, live=False)
    assert ok and probs == []
    assert adapter.readiness_calls == 0
