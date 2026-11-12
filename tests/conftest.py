"""Test settings for the Hermes helpers, installed before any test module imports them.

print_handy.runtime reads its hosts and paths through print_handy.runtime._config. Here that is replaced by
a copy of the checked-in home config with fake `.test` hosts and temp directories, so no suite (hermes or pipeline)
can read a live profile or reach a live service through those modules.
"""
import os
import tempfile
from pathlib import Path

import pytest

# The geometry fixtures across the suites were authored against the Standard part
# gaps (merge spacing 10 mm, edge 8 mm, tower clearance 8/10 mm), which are now
# the "medium" preset; the shipped default is "minimum". Pin the suites to
# medium so those fixtures keep testing the algorithms they were written for.
# The presets themselves are covered by tests/core/test_spacing_presets.py, the
# minimum values by tests/slicer/test_spacing_minimums_real.py (real Orca), and
# fit behavior at the minimum preset by tests/pipeline/test_fit_auto_rearrange.py.
os.environ.setdefault("PRINT_HANDY__LAYOUT__SPACING", "medium")
# Likewise the temperature fixtures (fake slicer G-code, recorded starts, "stock" nozzles that resolve to
# steel) were authored while the shipped config zeroed the #513 steel offsets. Pin them to 0 so those
# fixtures keep their literal temperatures. The offsets are covered by tests/test_nozzle.py, the shipped
# values by test_shipped_offsets_match_the_nozzle_data_defaults and the shipped behavior end to end by
# test_shipped_offsets_raise_a_steel_nozzle_by_five.
for _steel in ("HARDENED_STEEL", "STAINLESS_STEEL"):
    os.environ.setdefault(f"PRINT_HANDY__NOZZLE_TEMP_OFFSETS__{_steel}", "0")

from print_handy import settings  # noqa: E402
from print_handy.runtime import _config

_tmp = Path(tempfile.mkdtemp(prefix="3dp-test-state-"))
os.environ.setdefault("PRINT_BOT_CANCEL_MARKER", str(_tmp / "last_bot_cancel.json"))
os.environ.setdefault("PRINT_ALERT_STATE", str(_tmp / "print_alert_buttons.json"))
os.environ.setdefault("PRINT_WATCH_FRAME_DIR", str(_tmp / "print-watch"))


def _test_settings():
    data = settings.load(local=Path(os.devnull), environ={})
    data["paths"].update(state_home=_tmp / "profile", archive=_tmp / "archive", records_vault=_tmp / "vault",
                         cache=_tmp / "cache")
    data["services"].update(
        moonraker_url="http://printer.test:7125", moonraker_ws_url="ws://printer.test:7125/websocket",
        camera_snapshot_url="http://printer.test/webcam/?action=snapshot", spoolman_url="http://spoolman.test:7912",
        vision_chat_url="http://vision.test:8317/v1/chat/completions")
    data["discord"]["chat_id"] = "100000000000000001"
    data["timelapse"].update(archive_host="archive.test", archive_dir="/archive/timelapses")
    # The Moonraker URL above is patched in after the load, so the mode
    # that load derived ("off", from the shipped home config's empty URL)
    # is stale: a load that saw this URL derives "rendered". Since 2.2.2
    # the mode gates the timelapse paths (go2rtc.capture_active /
    # rendered_active), so the fixture carries the mode its own services
    # imply, exactly as a real load of the same config would resolve.
    data["timelapse"]["mode"] = "rendered"
    return data


TEST_SETTINGS = _test_settings()
_config.set_override(TEST_SETTINGS)



@pytest.fixture
def vision_ai_on(monkeypatch):
    """Turn AI camera checks on for a test of the vision endpoint routes.

    ``vision.ai_enabled`` is off by default (#1376), and while it is off
    no endpoint route counts. Tests that exercise those routes (the
    verdict and extract calls, PRINT_VISION_URL, [ai.small], label and
    purchase reads) opt in with ``pytestmark = pytest.mark.usefixtures(
    "vision_ai_on")``. The switch itself is pinned in
    tests/bot/test_ai_design_only_defaults.py.
    """
    from print_handy.runtime import visual_watch
    monkeypatch.setattr(visual_watch, "vision_ai_enabled", lambda cfg=None: True)


@pytest.fixture(autouse=True)
def _quiet_status_voice(request, monkeypatch):
    """Status quips are random by design, so suites that assert exact progress text see none.

    A test module that sets STATUS_VOICE_LIVE = True keeps the real selector
    (the corpus and selection tests); wiring tests seed their own quip.
    """
    if getattr(request.module, "STATUS_VOICE_LIVE", False):
        return
    from print_handy.core import status_voice
    monkeypatch.setattr(status_voice, "quip", lambda *args, **kwargs: "")


@pytest.fixture(autouse=True)
def _offline_purge_detection(monkeypatch):
    """ZMod purge/plugin detection (#832) never reaches a network in tests: every read fails like an
    unreachable printer. Tests of the detection itself patch purge._fetch with their own fake."""
    from print_handy.core import purge

    def offline(url, timeout):
        raise OSError(f"network disabled in tests: {url}")
    monkeypatch.setattr(purge, "_fetch", offline)


@pytest.fixture(autouse=True)
def _offline_stop_capture(tmp_path, monkeypatch):
    """Stop-reason capture never reaches a printer in tests and writes to the test's own dir.
    Tests of the capture itself pass their own fake fetch."""
    from print_handy.runtime import stop_capture

    def offline(path, range_bytes=None):
        raise OSError(f"network disabled in tests: {path}")
    monkeypatch.setattr(stop_capture, "_default_fetch", offline)
    monkeypatch.setattr(stop_capture, "stops_dir", lambda printer_id=None: tmp_path / "stops")
    monkeypatch.setattr(stop_capture, "_archive_folder", lambda name: None)


@pytest.fixture(autouse=True)
def _isolated_launch_state(tmp_path, monkeypatch):
    """Printer leases and consumed approvals live in a per-test database, never the profile's."""
    monkeypatch.setenv("PRINT_HANDY_LAUNCH_DB", str(tmp_path / "print_handy_launches.sqlite3"))


@pytest.fixture(autouse=True)
def _isolated_credential_bindings(tmp_path, monkeypatch):
    """Each test's service-key host bindings (#1192) live in its own file, starting empty."""
    from print_handy import credential_binding
    monkeypatch.setenv(credential_binding.PATH_ENV, str(tmp_path / "credential-bindings.json"))
    credential_binding._warned.clear()


@pytest.fixture(autouse=True)
def _head_sensor_unknown(request, monkeypatch):
    """Resume's toolhead-sensor gate reads 'unknown' (no extra printer calls) unless a test opts in
    with @pytest.mark.real_head_sensor to exercise the sensor read itself."""
    if request.node.get_closest_marker("real_head_sensor"):
        return
    from print_handy.runtime import print_control
    monkeypatch.setattr(print_control, "head_filament_loaded", lambda printer_id=None: None)


@pytest.fixture(autouse=True)
def _bed_clear_confirmed(request, monkeypatch):
    """The launch tests drive print-now as if the operator had confirmed the bed clear (#1147).

    print_now refuses without that confirmation; the suites written before it test what happens
    after it, so they start with it given. Tests of the refusal itself opt out with
    @pytest.mark.bed_clear_unconfirmed and get the production default (no confirmation).
    """
    if request.node.get_closest_marker("bed_clear_unconfirmed"):
        return
    import contextvars

    from print_handy.core import model_job
    monkeypatch.setattr(model_job, "_BED_CLEAR", contextvars.ContextVar("test_bed_clear", default=True))


def pytest_configure(config):
    config.addinivalue_line("markers", "real_head_sensor: run print_control's real toolhead sensor read")
    config.addinivalue_line("markers", "bed_clear_unconfirmed: print-now gets no bed-clear confirmation "
                                       "(the production default; see _bed_clear_confirmed)")


class _FakeStartClock:
    """A start-confirmation window that costs no wall time: each sleep advances the clock."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _instant_start_confirmation(monkeypatch):
    """Every transport polls its start readback over a window (model_job._poll_start_confirmation).

    Tests drive that window on a fake clock instead of sleeping; one that wants to assert on the clock
    patches _confirm_clock and _confirm_sleep itself, which wins over this default.
    """
    from print_handy.core import model_job
    clock = _FakeStartClock()
    monkeypatch.setattr(model_job, "_confirm_clock", clock)
    monkeypatch.setattr(model_job, "_confirm_sleep", clock.sleep)
    return clock


def pytest_ignore_collect(collection_path, config):
    """Keep the dedicated edit corpus out of the existing test shards."""
    edit_dir = Path(__file__).parent / "corpus" / "edit"
    if collection_path == edit_dir and os.environ.get("PRINT_HANDY_EDIT_CORPUS") != "1":
        return True
    return None


# #971: tests marked `container` need the shipped container image (its
# entrypoint, state volumes or runtime layout). Outside the image they are
# environment-only failures, so they skip instead of failing; inside the
# image (the Dockerfile sets PRINT_HANDY_IN_CONTAINER=1) and in CI (the
# offline-tests job in ci.yml sets it explicitly) they run as before.
CONTAINER_ENV = "PRINT_HANDY_IN_CONTAINER"


def in_container(environ=None):
    """True when running inside the shipped image (or a CI job that opts in)."""
    return (environ if environ is not None else os.environ).get(CONTAINER_ENV) == "1"


# CI splits a long suite across parallel jobs with PRINT_HANDY_TEST_SHARD="<index>/<count>" (0 based).
# Items are dealt round robin in collection order, so every shard gets a slice of each module and the slow
# corpus cases spread evenly instead of landing in one shard. Unset (local runs) collects everything.
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if not in_container():
        skip_container = pytest.mark.skip(
            reason=f"needs the shipped container image: {CONTAINER_ENV} is not 1 "
                   "(set in the Dockerfile; the CI offline-tests job sets it explicitly)")
        for item in items:
            if "container" in item.keywords:
                item.add_marker(skip_container)
    spec = os.environ.get("PRINT_HANDY_TEST_SHARD", "").strip()
    if not spec:
        return
    index, count = (int(part) for part in spec.split("/"))
    if not 0 <= index < count:
        raise pytest.UsageError(f"PRINT_HANDY_TEST_SHARD={spec!r}: index must be in 0..{count - 1}")
    keep = [item for n, item in enumerate(items) if n % count == index]
    drop = [item for n, item in enumerate(items) if n % count != index]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep
