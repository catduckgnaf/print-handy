"""When the two preview slices (without and with supports) may run at the same time (#1779).

Parallel previews need a host with room for two slicer runs and a slicer that tolerates them:
``[slicer] parallel_preview`` is turned on (it is off by default), ``os.cpu_count()`` is known and at least
``MIN_CPUS``, the container's CPU quota (when one is set) is not below that, and the Orca command
is not a Flatpak (concurrent Flatpak Orca runs collide, exit 243). Anything else slices one run
at a time, as before. ``MAX_SLICER_RUNS`` is the most slicer steps ever running at once across
every intake: the slicer lane takes its slot count from ``slicer_slots()``.
"""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_CPUS = 4
MAX_SLICER_RUNS = 2


def _orca_command(cfg):
    override = os.environ.get('PRINT_HANDY_ORCA_CMD')
    if override:
        return [override]
    return list(((cfg.get('orca') or {}).get('command')) or [])


def enabled(cpu_count='unset', cfg=None, budget='unset'):
    """True when this host runs the two preview slices concurrently."""
    from .. import settings
    from . import resources
    cfg = settings.current() if cfg is None else cfg
    if (cfg.get('slicer') or {}).get('parallel_preview', False) is not True:
        return False
    cpus = os.cpu_count() if cpu_count == 'unset' else cpu_count
    if not isinstance(cpus, int) or cpus < MIN_CPUS:
        return False  # unknown or small host: serial
    limit = resources.thread_budget() if budget == 'unset' else budget
    if limit is not None and limit < MIN_CPUS:
        return False
    command = _orca_command(cfg)
    return not (command and Path(command[0]).name == 'flatpak')


def slicer_slots():
    """Slicer runs allowed at once: 2 when previews run in parallel, otherwise 1."""
    try:
        return MAX_SLICER_RUNS if enabled() else 1
    except Exception:  # noqa: BLE001 - an unreadable setting keeps the safe serial lane
        logger.warning('parallel_preview setting unreadable; using one slicer run', exc_info=True)
        return 1
