"""Start claims and abandoned starts, shared by every printer listener (#1320).

print-now claims a job's start announcement in a small shared file; every
listener reads it, keyed by the content-hashed remote file name, so the
listener does not post a second "Print started" beside the intake's.
``AbandonedStartWatch`` is the pure cold-window rule for a claimed start
that never became a print; the Moonraker listener and the family launch
lease settler (family_control.LaunchLeaseSettler) both run it.

Moved out of ``moonraker_event_listener`` so the Bambu Lab and family
listeners no longer import the Moonraker listener to reach them; that
module keeps every old name as a wrapper over these.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import _config

# Start claims stay shared on purpose: print-now writes them and every
# listener reads them, keyed by the content-hashed remote file name;
# a cross-process channel, not one listener's operational state (P1-82).
START_CLAIMS = _config.state_file("PRINT_HANDY_START_CLAIMS", "print_handy_start_claims.json")
START_CLAIM_TTL_S = float(_config.cfg()["listener"]["start_claim_ttl_s"])


def _path(path):
    return START_CLAIMS if path is None else Path(path)


def _ttl(ttl_s):
    return START_CLAIM_TTL_S if ttl_s is None else float(ttl_s)


def start_claim_time(filename, now=None, *, path=None, ttl_s=None):
    """When print-now claimed this job's start announcement, or None when unclaimed/expired."""
    now = time.time() if now is None else now
    try:
        claims = json.loads(_path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    # print-now claims the uploaded name (X.gcode.3mf); the printer reports the path it runs, which
    # for a ZMod 3MF is 3mf_print/X.gcode. Matching only the basename missed that and posted a
    # second "Print started" beside the intake's (2026-10-31, Print #64).
    from ..core.launch_state import same_remote
    name = str(filename or "")
    for key in (Path(name).name, *claims):
        ts = claims.get(key)
        if (isinstance(ts, (int, float)) and 0 <= now - ts < _ttl(ttl_s)
                and (key == Path(name).name or same_remote(name, key) or same_remote(Path(name).name, key))):
            return ts
    return None


def start_claimed(filename, now=None, *, path=None, ttl_s=None):
    """True when model_job.py print-now already owns this job's start announcement."""
    return start_claim_time(filename, now, path=path, ttl_s=ttl_s) is not None


def claimed_start_filenames(now=None, *, path=None, ttl_s=None):
    """The unexpired claimed start files, as print-now recorded them (the uploaded names)."""
    now = time.time() if now is None else now
    try:
        claims = json.loads(_path(path).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(claims, dict):
        return []
    return [name for name, ts in claims.items()
            if isinstance(ts, (int, float)) and 0 <= now - ts < _ttl(ttl_s)]


def release_start_claim(filename, *, path=None):
    """Drop this file's start claim (the listener's counterpart of model_job's release).

    Matching follows start_claim_time: print-now claims the uploaded name, the printer
    reports its own path for the same file."""
    try:
        claims = json.loads(_path(path).read_text())
    except (OSError, ValueError):
        return
    if not isinstance(claims, dict):
        return
    from ..core.launch_state import same_remote
    name = str(filename or "")
    kept = {key: ts for key, ts in claims.items()
            if not (key == Path(name).name or same_remote(name, key) or same_remote(Path(name).name, key))}
    if len(kept) != len(claims):
        try:
            tmp = _path(path).with_suffix(".tmp")
            tmp.write_text(json.dumps(kept))
            os.replace(tmp, _path(path))
        except OSError:
            pass


# A claimed start that never became a print: print_stats reads standby/error with both heater
# targets at 0, sustained this long, before the listener calls it abandoned (#836). Shorter
# cold reads are just the gap between the start card and warmup.
ABANDONED_START_COLD_S = 60.0


class AbandonedStartWatch:
    """Tracks claimed starts that have not become prints, one cold window per claimed file.

    Pure observation in, one decision out: `observe` returns True exactly once per claim for
    a claimed file whose printer reads standby/error with both heater targets at 0 for
    ABANDONED_START_COLD_S. Each outstanding claim keeps its own window: the poll feeds every
    claim the same printer read (an empty reported filename matches them all), and one claim's
    reads must never reset another claim's window (#848 — with a single shared window, two
    outstanding claims alternately reset each other and neither was ever reported). Any
    heater target or an unreadable target restarts that claim's window only — neither is
    evidence a start was abandoned. A printing/paused read settles the claim for good: that
    start took, so its later idle reads (complete, then standby with cold heaters once the
    printer resets) are a finished print's story, not an abandoned start's (#1282: the old
    code dropped the window on the printing read, so the next cold read opened a fresh one
    and a print that ran to completion was reported as "never became a print"). `claimed_at`
    is the claim's own timestamp: a re-claim of the same file (a reprint) carries a new one
    and starts a fresh window. `retire` drops a claim the poll has released, and `retain`
    drops windows for claims that are no longer outstanding, so a later claim of the same
    file always starts a fresh window.
    """

    def __init__(self):
        # filename -> {"cold_since": float | None, "announced": bool, "took": bool,
        #              "claimed_at": float | None}
        self._claims = {}

    def observe(self, filename, state, extruder_target, bed_target, now=None, claimed_at=None):
        if not filename:
            return False
        now = time.time() if now is None else now
        claim = self._claims.get(filename)
        if claim is None or (claimed_at is not None and claim["claimed_at"] != claimed_at):
            claim = self._claims[filename] = {"cold_since": None, "announced": False,
                                              "took": False, "claimed_at": claimed_at}
        if state in ("printing", "paused"):
            claim["took"] = True  # this claim's start took: nothing later is an abandoned start
            claim["cold_since"] = None
            return False
        if claim["took"]:
            return False
        cold = (state in ("standby", "error")
                and extruder_target is not None and bed_target is not None
                and float(extruder_target) == 0.0 and float(bed_target) == 0.0)
        if not cold:
            claim["cold_since"] = None
            return False
        if claim["cold_since"] is None:
            claim["cold_since"] = now
            return False
        if not claim["announced"] and now - claim["cold_since"] >= ABANDONED_START_COLD_S:
            claim["announced"] = True
            return True
        return False

    def retire(self, filename):
        """Drop one claim's window (it fired, or its claim was released another way)."""
        self._claims.pop(filename, None)

    def retain(self, filenames):
        """Drop the windows of files that are no longer outstanding claims."""
        outstanding = set(filenames)
        for filename in list(self._claims):
            if filename not in outstanding:
                del self._claims[filename]
