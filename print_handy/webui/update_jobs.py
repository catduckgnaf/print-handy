"""Background firmware update jobs for the Web UI (#1610).

Applying an update blocks for as long as the printer takes — Moonraker
awaits the update before answering the upgrade POST, minutes at a
time — so the Web UI never holds an HTTP request open for it. A
confirmed update becomes a job here: ``firmware_update.send_update``
runs on a daemon thread (it re-checks the printer is idle, sends the
one POST, and verifies by re-reading), and the status page reads the
job back until it finishes. Jobs live in memory on purpose: the update
itself is the printer's, not this process's, so a Web UI restart loses
only the progress view — the Firmware page's installed-version read
shows the true state — never the update.

One running job per printer and component: a second start for the same
pair returns the running job instead of sending anything twice.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .. import firmware_update as fw

logger = logging.getLogger(__name__)

_JOBS: dict = {}
_LOCK = threading.Lock()


@dataclass
class UpdateJob:
    """One update, running or finished, as the status page shows it."""

    id: str
    printer_id: str
    printer_name: str
    component: str
    current: str | None
    remote: str | None
    estimate_text: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    outcome: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


def get_job(job_id):
    with _LOCK:
        return _JOBS.get(str(job_id))


def running_job_for(printer_id, component_name):
    """The running job for this printer + component, or None."""
    with _LOCK:
        for job in _JOBS.values():
            if (job.running and job.printer_id == str(printer_id)
                    and job.component == component_name):
                return job
    return None


def start_job(data, *, printer_id, printer_name, transport, prepared, on_finish=None):
    """Start ``prepared`` (a PreparedUpdate) in the background; return the job.

    If a job for the same printer and component is already running it
    is returned unchanged and nothing new is sent. ``on_finish(job)``
    runs after the outcome is recorded (the handler uses it to clear
    the applied offer from the notification observations).
    """
    component = prepared.component
    # The running-job check and the insert happen under one lock hold
    # (#1614 review): two concurrent confirmations for the same printer
    # and component cannot both pass the check and send two updates.
    with _LOCK:
        for existing in _JOBS.values():
            if (existing.running and existing.printer_id == str(printer_id)
                    and existing.component == component.name):
                return existing
        job = UpdateJob(
            id=secrets.token_urlsafe(9), printer_id=str(printer_id),
            printer_name=str(printer_name or printer_id),
            component=component.name, current=component.current,
            remote=component.remote,
            estimate_text=fw.update_estimate_text(component.name))
        _JOBS[job.id] = job

    def run():
        try:
            job.outcome = fw.send_update(transport, prepared)
        except (fw.FirmwareRefusal, fw.FirmwareError) as exc:
            job.outcome = str(exc)
        except Exception:
            logger.exception("Web UI firmware update job failed")
            job.outcome = ("The update stopped on an unexpected error. Check the "
                           "printer's own update screen before trying again.")
        finally:
            if on_finish is not None:
                try:
                    on_finish(job)
                except Exception:
                    logger.warning("update job finish hook failed", exc_info=True)
            # finished_at is the done signal the status page and callers
            # read, so it is set last, after the hook has run.
            job.finished_at = time.time()

    threading.Thread(target=run, name=f"webui-update-{job.id}",
                     daemon=True).start()
    return job


def _reset_for_tests():
    with _LOCK:
        _JOBS.clear()


__all__ = ["UpdateJob", "get_job", "running_job_for", "start_job"]
