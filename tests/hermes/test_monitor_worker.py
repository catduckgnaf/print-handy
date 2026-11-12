"""P1-85 / P1-98: launched-print monitors must outlive the print-now process.

For the stock Flashforge, Elegoo SDCP, OctoPrint, PrusaLink and Bambu
families, the print path started the monitor on a daemon thread inside
print-now. But print-now is a short-lived subprocess — print_next spawns
it per launch, and a direct CLI run is no longer-lived — so the thread
died with the submission and the print never got its completion post or
timelapse. The monitor now runs in a detached worker process
(print_handy.runtime.monitor_worker) that rebuilds the job printer's
adapter, starts the family's watch monitor there, and stays alive until
the monitor's poll loop ends.
"""
import subprocess
import sys
import types
from unittest import mock

import pytest

from print_handy import listeners, logs, settings
from print_handy.runtime import monitor_worker


# -- spawn: the detached worker -------------------------------------------------


class _FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return mock.Mock(pid=4242)


def test_spawn_builds_a_detached_worker_command(monkeypatch):
    monkeypatch.setattr(monitor_worker, "_log_sink", lambda: None)
    popen = _FakePopen()
    proc = monitor_worker.spawn("print_handy.runtime.stock_watch", "plate.gcode",
                                popen=popen)
    assert proc.pid == 4242
    (argv, kwargs), = popen.calls
    assert argv == [sys.executable, "-m", "print_handy.runtime.monitor_worker",
                    "--watch", "stock", "--filename", "plate.gcode"]
    # Detached: its own session, and no inherited stdio — above all not
    # print-now's stdout pipe, which print_next reads to EOF.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_spawn_passes_the_job_printer_and_redirects_to_the_monitor_log(
        monkeypatch, tmp_path):
    sink = (tmp_path / "monitor.log").open("ab")
    monkeypatch.setattr(monitor_worker, "_log_sink", lambda: sink)
    popen = _FakePopen()
    monitor_worker.spawn("print_handy.runtime.elegoo_watch", "benchy.gcode",
                         printer_id="elegoo-two", popen=popen)
    (argv, kwargs), = popen.calls
    assert argv[-4:] == ["--filename", "benchy.gcode", "--printer", "elegoo-two"]
    assert argv[3:5] == ["--watch", "elegoo"]
    assert kwargs["stdout"] is sink
    assert kwargs["stderr"] == subprocess.STDOUT


def test_watch_key_derives_from_the_watch_module_name():
    assert monitor_worker.watch_key("print_handy.runtime.stock_watch") == "stock"
    assert monitor_worker.watch_key("print_handy.runtime.bambu_watch") == "bambu"
    assert monitor_worker.watch_key("print_handy.runtime.octo_watch") == "octo"


# -- run: the worker side ---------------------------------------------------------


class _FakeThread:
    def __init__(self):
        self.joined = False

    def join(self):
        self.joined = True


def _fake_watch(monkeypatch, thread):
    seen = {}
    module = types.ModuleType("fake_watch_module")

    def start_monitor(adapter, filename, **kwargs):
        seen.update(adapter=adapter, filename=filename, kwargs=kwargs)
        return thread

    module.start_monitor = start_monitor
    sys.modules["fake_watch_module"] = module
    monkeypatch.setitem(monitor_worker.WATCH_MODULES, "fake", "fake_watch_module")
    monkeypatch.setattr(monitor_worker, "_adapter_for", lambda pid: ("adapter", pid))
    return seen


def test_run_starts_the_monitor_and_waits_for_it(monkeypatch):
    thread = _FakeThread()
    seen = _fake_watch(monkeypatch, thread)
    assert monitor_worker.run("fake", "remote.gcode") == 0
    assert seen == {"adapter": ("adapter", None), "filename": "remote.gcode",
                    "kwargs": {}}
    assert thread.joined  # the worker lives exactly as long as the monitor


def test_run_threads_the_job_printer_through(monkeypatch):
    thread = _FakeThread()
    seen = _fake_watch(monkeypatch, thread)
    assert monitor_worker.run("fake", "remote.gcode", printer_id="elegoo-two") == 0
    assert seen["adapter"] == ("adapter", "elegoo-two")
    assert seen["kwargs"] == {"printer_id": "elegoo-two"}


def test_run_a_declined_monitor_is_a_quiet_success(monkeypatch):
    _fake_watch(monkeypatch, None)  # start_monitor declines (no chat configured)
    assert monitor_worker.run("fake", "remote.gcode") == 0


# -- the adapter the worker builds -------------------------------------------------


ROSTER_YAML = (
    "discord:\n  chat_id: \"111\"\n"
    "printers:\n"
    "  - id: moon-two\n    model: klipper.generic\n    chat_id: \"chat-two\"\n"
    "  - id: bambu-two\n    model: bambulab.a1\n")


def test_adapter_for_a_roster_printer_installs_its_view_and_identity(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(ROSTER_YAML)
    roster = settings.load(home=settings.HOME, local=local, environ={})
    built = {}

    def fake_get(model, view):
        built.update(model=model, view=view)
        return "the-adapter"

    try:
        with mock.patch.object(settings, "current", return_value=roster), \
                mock.patch("print_handy.printers.get", side_effect=fake_get):
            adapter = monitor_worker._adapter_for("moon-two")
        assert adapter == "the-adapter"
        assert built["model"] == "klipper.generic"
        # The instance identity and the printer's claimed chat are the
        # worker's settings, exactly as the listener launcher installs
        # them, so the monitor posts as that printer.
        assert listeners.instance_printer_id() == "moon-two"
        assert settings.current()["discord"]["chat_id"] == "chat-two"
    finally:
        settings.set_override(None)
        listeners._set_instance_printer_id(None)
        logs.clear_context()


# -- model_job's routing ------------------------------------------------------------


def test_model_job_starts_family_monitors_through_the_worker(monkeypatch):
    from print_handy.core import model_job
    from print_handy.runtime import stock_watch

    spawned = []
    monkeypatch.setattr(
        monitor_worker, "spawn",
        lambda watch, remote, printer_id=None: spawned.append((watch, remote, printer_id)))
    model_job._start_family_monitor(stock_watch, object(), "plate.gcode",
                                    {"printer_id": "stock-two"})
    model_job._start_family_monitor(stock_watch, object(), "plate.gcode", {})
    assert spawned == [("print_handy.runtime.stock_watch", "plate.gcode", "stock-two"),
                       ("print_handy.runtime.stock_watch", "plate.gcode", None)]
