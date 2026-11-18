"""Background Moonraker channel-switch jobs for the Web UI (#1634).

A confirmed channel switch restarts Moonraker and then waits to verify
the new channel from the update status, so the Web UI never holds an
HTTP request open for it. A confirmed switch becomes a job here:
``firmware_update.send_channel_switch`` runs on a daemon thread (it
re-checks the printer is idle, saves the backup, writes the one line,
restarts Moonraker and verifies, restoring the backup on failure), and
the status page reads the job back until it finishes. Jobs live in
memory on purpose, like the update jobs: the switch itself is the
printer's, so a Web UI restart loses only the progress view — the
Firmware page's installed-version read shows the true state.

One running job per printer and component: a second start for the same
pair returns the running job instead of writing anything twice.
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
class ChannelJob:
    """One channel switch, running or finished, as the status page shows it."""

    id: str
    printer_id: str
    printer_name: str
    component: str
    current_channel: str
    target_channel: str
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


def start_job(*, printer_id, printer_name, transport, files, prepared, restart,
              on_finish=None):
    """Start ``prepared`` (a PreparedChannelSwitch) in the background; return the job.

    ``restart`` is the printer's Moonraker restart, routed through
    print_handy.printer_control by the caller (#1317). If a job for the
    same printer and component is already running it is returned
    unchanged and nothing new is sent. ``on_finish(job)`` runs after
    the outcome is recorded.
    """
    with _LOCK:
        for existing in _JOBS.values():
            if (existing.running and existing.printer_id == str(printer_id)
                    and existing.component == prepared.component.name):
                return existing
        job = ChannelJob(
            id=secrets.token_urlsafe(9), printer_id=str(printer_id),
            printer_name=str(printer_name or printer_id),
            component=prepared.component.name,
            current_channel=prepared.current_channel,
            target_channel=prepared.target_channel)
        _JOBS[job.id] = job

    def run():
        try:
            job.outcome = fw.send_channel_switch(transport, files, prepared,
                                                 restart=restart)
        except (fw.FirmwareRefusal, fw.FirmwareError) as exc:
            job.outcome = str(exc)
        except Exception:
            logger.exception("Web UI firmware channel switch job failed")
            job.outcome = ("The channel switch stopped on an unexpected error. "
                           "Check the printer's Moonraker config before trying "
                           "again — the job's own restore may not have run.")
        finally:
            if on_finish is not None:
                try:
                    on_finish(job)
                except Exception:
                    logger.warning("channel job finish hook failed", exc_info=True)
            # finished_at is the done signal the status page reads, so
            # it is set last, after the hook has run.
            job.finished_at = time.time()

    threading.Thread(target=run, name=f"webui-channel-{job.id}",
                     daemon=True).start()
    return job


def _reset_for_tests():
    with _LOCK:
        _JOBS.clear()


__all__ = ["ChannelJob", "get_job", "running_job_for", "start_job"]
