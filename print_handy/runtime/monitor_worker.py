"""Detached launched-print monitors: a worker process that outlives print-now.

For the non-Moonraker families, the print path starts a monitor that
watches the launched print, posts its completion (with the timelapse)
and raises its pause alerts. That monitor used to run on a daemon
thread inside the print-now process — but print-now is a short-lived
subprocess (print_next spawns it per launch, and a direct CLI run is
no longer-lived), so the thread died with the submission and the print
lost its completion post and timelapse entirely (P1-85, P1-98).

model_job therefore starts the monitor here instead: a detached
process — its own session, stdio redirected to the monitor log in the
state folder, never print-now's pipes, which print_next reads to EOF —
that rebuilds the job printer's adapter exactly as print_now did,
starts the family's watch monitor, and stays alive until the monitor's
poll loop ends. The print itself never waits on this process, and a
worker failure never looks like a print failure.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys

WATCH_MODULES = {
    "octo": "print_handy.runtime.octo_watch",
    "bambu": "print_handy.runtime.bambu_watch",
    "elegoo": "print_handy.runtime.elegoo_watch",
    "prusa": "print_handy.runtime.prusa_watch",
    "stock": "print_handy.runtime.stock_watch",
}

LOG_NAME = "monitor_worker.log"


def watch_key(watch_module_name):
    """'print_handy.runtime.stock_watch' -> 'stock'."""
    short = watch_module_name.rsplit(".", 1)[-1]
    return short[:-len("_watch")] if short.endswith("_watch") else short


def _log_sink():
    """An append-mode handle for the worker's stdio, or None for /dev/null."""
    try:
        from print_handy import settings

        path = settings.state_dir() / LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("ab")
    except Exception:
        return None


def spawn(watch_module_name, filename, printer_id=None, *, popen=None):
    """Start the detached monitor worker for a just-launched print.

    Returns the Popen object. The worker is fully detached (new session,
    stdio redirected): print-now exits immediately, and the worker —
    holding no inherited pipe — cannot keep the caller's stdout open.
    """
    argv = [sys.executable, "-m", "print_handy.runtime.monitor_worker",
            "--watch", watch_key(watch_module_name), "--filename", str(filename)]
    if printer_id:
        argv += ["--printer", str(printer_id)]
    sink = _log_sink()
    try:
        return (popen or subprocess.Popen)(
            argv, stdin=subprocess.DEVNULL,
            stdout=sink if sink is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if sink is not None else subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
    finally:
        if sink is not None:
            sink.close()


def _adapter_for(printer_id):
    """The adapter print_now built for this launch, with its settings view.

    For a roster printer, the instance view and identity are installed
    first — exactly as the listener launcher installs them — so the
    monitor's posts and state resolve to that printer; the adapter
    itself is built from the same per-printer view print_now used. A
    launch with no recorded printer keeps the configured printer.
    """
    from print_handy import listeners, logs, printers, settings

    if not printer_id:
        return printers.current()
    data = settings.current()
    entry = settings.by_id(printer_id, data)
    settings.set_override(listeners._instance_view(printer_id, data))
    listeners._set_instance_printer_id(printer_id)
    # Like the launcher: every log line from this process carries its printer_id.
    logs.bind(printer_id=printer_id)
    logs.bind_process(printer_id=printer_id)  # worker threads inherit no ContextVar (P2-161)
    return printers.get(entry["model"], settings.view_for(printer_id, data))


def run(watch, filename, printer_id=None):
    """Start the family's monitor in this process and wait for it to end.

    Returns a process exit code. A monitor that declines to start (no
    chat configured to post to) is a quiet success: there is nobody to
    post the completion to.
    """
    module = importlib.import_module(WATCH_MODULES[watch])
    adapter = _adapter_for(printer_id)
    if printer_id:
        thread = module.start_monitor(adapter, filename, printer_id=printer_id)
    else:
        thread = module.start_monitor(adapter, filename)
    if thread is None:
        return 0
    thread.join()
    return 0


def main(argv=None):
    from print_handy.core import resources

    resources.apply_to_environment()  # before numpy/onnxruntime load: pools follow the CPU quota
    parser = argparse.ArgumentParser(prog="print_handy.runtime.monitor_worker")
    parser.add_argument("--watch", choices=sorted(WATCH_MODULES), required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--printer", default=None)
    args = parser.parse_args(argv)
    from print_handy import logs

    logs.configure()
    try:
        return run(args.watch, args.filename, args.printer)
    except Exception as exc:  # a monitor must never look like a print failure
        print(f"monitor worker for {args.filename!r} failed: {exc!r}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
