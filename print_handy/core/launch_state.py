"""Transactional launch state: consumed bed-clear approvals and one submission lease per printer.

One small SQLite database per printer, under <state>/printers/<id>/jobs.db (#334, design §2.4),
so the check and the write happen in one transaction across processes (the bot, print_next
children, a CLI print-now) and one printer's lease or approval table can never leak into
another's. JOB.json keeps its human-readable approvals list; model archives stay ordinary files.

Upgrade adoption: the first time the default printer's database is opened and it does not
exist yet, the legacy single database (<state>/print_handy_launches.sqlite3) is copied into
place — held lease and consumed approvals intact — and the legacy file is left untouched for
one release so a rollback still finds it. $PRINT_HANDY_LAUNCH_DB keeps naming one explicit
database and disables the per-printer layout (tests and operator overrides).

Lease phases: ``leased`` (checks and upload, nothing started), ``submitting`` (persisted before the
start request), ``uncertain`` (sent, outcome unknown). A confirmed or failed start deletes the row.
An unfinished ``leased`` row whose process is gone, or that is older than LEASED_TTL_S (a container
restart leaves a foreign hostname or a recycled PID that looks alive), is released: nothing was sent. A ``submitting``
or ``uncertain`` row is never released by guessing and never replayed. It is reconciled when the
printer reports that exact file, now or in its job history since the lease, or by the operator with
``model_job reconcile-launch``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .. import settings

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS approvals (job_json TEXT NOT NULL, gcode_sha256 TEXT NOT NULL, "
    "kind TEXT NOT NULL, consumed_at TEXT NOT NULL, pid INTEGER)",
    "CREATE TABLE IF NOT EXISTS leases (printer TEXT PRIMARY KEY, token TEXT NOT NULL, phase TEXT NOT NULL, "
    "job_json TEXT, gcode_sha256 TEXT, remote TEXT, pid INTEGER, host TEXT, leased_at TEXT, updated_at TEXT, "
    "detail TEXT)",
    # The history job keys that already existed when a lease was taken (P1-121):
    # a run that predates a submission can never confirm that submission.
    "CREATE TABLE IF NOT EXISTS lease_baselines (printer TEXT PRIMARY KEY, baseline TEXT NOT NULL)",
)


def _adopt_legacy_db(target, printer_id):
    """Copy the legacy single launch database into the default printer's folder, once.

    Only the default printer adopts it: the legacy database was the singleton
    printer's, and copying it to a second printer would duplicate its held
    lease and consumed approvals. The legacy file (and any WAL sidecars) is
    left in place for one release.
    """
    if printer_id != settings.default_printer_id():
        return
    legacy = settings.state_dir() / "print_handy_launches.sqlite3"
    if target.exists() or not legacy.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".adopting")
    shutil.copy2(legacy, tmp)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(legacy) + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, Path(str(tmp) + suffix))
    os.replace(tmp, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(tmp) + suffix)
        if sidecar.is_file():
            os.replace(sidecar, Path(str(target) + suffix))
    _rekey_adopted_db(target, printer_id)


def _rekey_adopted_db(target, printer_id):
    """Rekey an adopted legacy database's rows to the roster printer id (P1-74).

    The legacy database keyed its lease (and any history baseline) by the old
    Moonraker URL / model key, while every lookup in the per-printer layout
    uses the roster id: copied verbatim, a held submitting/uncertain lease
    would never be found and a second launch could start over it. ``printer``
    is each table's PRIMARY KEY, so several distinct legacy keys cannot all
    survive the rekey: the newest lease row (by ``updated_at``) wins, exactly
    the one row a singleton database can hold.
    """
    con = sqlite3.connect(target)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        with con:
            survivor = None
            if "leases" in tables:
                stale = con.execute(
                    "SELECT rowid, printer FROM leases ORDER BY updated_at DESC").fetchall()
                survivor = stale[0][1] if stale else None
                for (rowid, _key) in stale[1:]:
                    con.execute("DELETE FROM leases WHERE rowid=?", (rowid,))
                con.execute("UPDATE leases SET printer=? WHERE printer!=?",
                            (printer_id, printer_id))
            if "lease_baselines" in tables:
                # The baseline that goes with the surviving lease, not just the first row: a
                # baseline from another legacy key would let a run it predates confirm it (2.2.5 review).
                stale = con.execute("SELECT rowid, printer FROM lease_baselines").fetchall()
                keep = next((rowid for (rowid, key) in stale if key == survivor),
                            stale[0][0] if stale else None)
                for (rowid, _key) in stale:
                    if rowid != keep:
                        con.execute("DELETE FROM lease_baselines WHERE rowid=?", (rowid,))
                con.execute("UPDATE lease_baselines SET printer=? WHERE printer!=?",
                            (printer_id, printer_id))
    finally:
        con.close()


def db_path(printer_id=None):
    """The launch database for one printer: $PRINT_HANDY_LAUNCH_DB, else its per-printer jobs.db."""
    override = os.environ.get("PRINT_HANDY_LAUNCH_DB")
    if override:
        return Path(override)
    printer_id = printer_id or settings.default_printer_id()
    target = settings.printer_state_dir(printer_id) / "jobs.db"
    _adopt_legacy_db(target, printer_id)
    return target


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


@contextmanager
def transaction(printer_id=None):
    """One exclusive write transaction on one printer's database.

    Every launch-state check-then-write runs inside one.
    """
    path = db_path(printer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN IMMEDIATE")
        for statement in SCHEMA:
            con.execute(statement)
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def consumed(con, job_json, gcode_sha256):
    return [dict(r) for r in con.execute("SELECT * FROM approvals WHERE job_json=? AND gcode_sha256=? ORDER BY rowid",
                                         (str(job_json), gcode_sha256))]


def record_consumed(con, job_json, gcode_sha256, kind, when):
    con.execute("INSERT INTO approvals VALUES (?,?,?,?,?)", (str(job_json), gcode_sha256, kind, when, os.getpid()))


# A ``leased`` row is pre-submission only: nothing was sent to the printer. Its owner is judged by
# hostname and PID, which a container restart defeats (the hostname may be unchanged or foreign, and
# a recycled PID answers kill(pid, 0)), so a leased row also expires after this long. Real launches
# spend seconds to a few minutes in that phase (the upload has a 300 s cap). ``submitting`` and
# ``uncertain`` rows never expire: something may have been sent.
LEASED_TTL_S = 15 * 60


def _leased_abandoned(row, unknown=True, now=None):
    """Whether a ``leased`` row can be released: its owner is gone, or it cannot be checked (another
    host or container) and outlived LEASED_TTL_S. A live process on this host never expires."""
    if not _owner_alive(row, unknown):
        return True
    if row["host"] == socket.gethostname():
        return False  # a live process on this host is the owner, however long it has been
    try:
        updated = datetime.strptime(row["updated_at"] or row["leased_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        return False  # an unreadable stamp cannot prove expiry
    return (time.time() if now is None else now) - updated > LEASED_TTL_S


def _owner_alive(row, unknown=True):
    """Whether the lease's process still runs. Another host or container cannot be checked: unknown."""
    if row["host"] != socket.gethostname():
        return unknown
    try:
        os.kill(int(row["pid"]), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, TypeError, ValueError):
        return True
    return True


def same_remote(reported, remote):
    """Whether a file name the printer reports is this upload: the exact name or a documented
    Klipper path for it, including ZMod's 3MF unpack path (X.gcode.3mf -> 3mf_print/X.gcode) and
    the bare project name a Bambu printer shows for a package (X.gcode.3mf -> X)."""
    reported = str(reported or "").replace("\\\\", "/").lstrip("/")
    remote = str(remote or "").replace("\\\\", "/").lstrip("/")
    if not remote:
        return False
    expected = {remote}
    if remote.endswith(".gcode.3mf"):
        unpacked = remote[:-4]
        # Bambu reports the bare project name (subtask_name) for the package it was sent.
        expected.update({unpacked, "3mf_print/" + unpacked, "gcodes/" + unpacked, remote[:-len(".gcode.3mf")]})
    elif remote.endswith(".3mf"):
        expected.add(remote[:-len(".3mf")])
    elif remote.endswith(".gcode"):
        expected.update({"gcodes/" + remote, "3mf_print/" + remote})
    return reported in expected


# Printer and host clocks drift; a history start this far before the lease still counts. An
# older run of the same file (a reprint) started well before its lease and never matches.
HISTORY_SLACK_S = 120


def history_key(entry):
    """One history entry's identity: its job id when the printer reports one, else the
    (filename, start) pair, which a real job's history record never changes."""
    if len(entry) >= 3 and entry[0]:
        return f"id:{entry[0]}"
    return f"pair:{entry[-2]}@{entry[-1]}"


def reached_printer(row, observed_file=None, started_files=(), baseline=()):
    """Whether a submitting/uncertain lease's exact file reached the printer: it is the current
    file, or the printer's job history shows it starting after the lease was taken.

    started_files is (filename, start_epoch) pairs or (job_id, filename, start_epoch)
    triples, or a callable returning them, read only when the current file does not
    already settle it. baseline holds history_key() values captured before this
    lease's submission (see acquire): those runs predate the submission, so they
    can never confirm it — not even a same-name run inside the clock slack, which
    is exactly the cancelled-then-retried case (P1-121). The slack still applies
    to runs that are not in the baseline, including the submission's own.
    """
    remote = row["remote"]
    if not remote:
        return False
    if same_remote(observed_file, remote):
        return True
    try:
        since = datetime.strptime(row["leased_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp() - HISTORY_SLACK_S
    except (TypeError, ValueError):
        return False
    try:
        known = set(baseline or ())
        history = started_files() if callable(started_files) else started_files
        for entry in history or ():
            if known and history_key(entry) in known:
                continue  # already in history before this submission: not its confirmation
            filename, started = entry[-2], entry[-1]
            if same_remote(filename, remote) and float(started) >= since:
                return True
    except Exception:
        return False  # unreadable history settles nothing; the lease stays uncertain
    return False


def _stored_baseline(con, printer):
    """The history baseline captured when the printer's held lease was taken, if any."""
    row = con.execute("SELECT baseline FROM lease_baselines WHERE printer=?", (printer,)).fetchone()
    if row is None:
        return ()
    try:
        return tuple(json.loads(row["baseline"]))
    except Exception:
        return ()


def _capture_baseline(started_files):
    """history_key() for every job already in history, read before submission.

    An unreadable history captures an empty baseline: reconciliation then falls
    back to the clock-slack rule alone, never worse than before baselines existed.
    """
    try:
        history = started_files() if callable(started_files) else started_files
        return sorted({history_key(entry) for entry in history or () if entry})
    except Exception:
        return []


def _describe(row):
    return f"job {row['job_json'] or 'unknown'}, file {row['remote'] or 'unknown'}, since {row['leased_at']}"


class Lease:
    """The one launch allowed on a printer right now. Every update is scoped to this lease's token."""

    def __init__(self, printer, token, history_source=None):
        self.printer, self.token = printer, token
        self._history_source = history_source

    def _set(self, phase, detail=None, required=False):
        with transaction(self.printer) as con:
            changed = con.execute("UPDATE leases SET phase=?, detail=?, updated_at=? WHERE printer=? AND token=?",
                                  (phase, detail, _now(), self.printer, self.token)).rowcount
        if required and not changed:
            raise SystemExit("REFUSED: this launch lost its printer lease before sending; nothing started.")

    def submitting(self):
        """Persist before the external start request: from here a crash is uncertain, not retried."""
        global _SUBMITTED
        self._set("submitting", required=True)  # a stolen lease must stop the launch before the request
        _SUBMITTED = True
        if self._history_source is not None:
            # The pre-submission history baseline (P1-121), captured at the last
            # moment before the start request: every job in history now predates
            # this submission, so none of them can later confirm it. Capturing
            # here (not at acquire) keeps pre-submission refusal paths free of
            # history reads they never needed.
            # Read the history before the write transaction opens: BEGIN IMMEDIATE must not be held
            # across a Moonraker request (2.2.5 review).
            baseline = json.dumps(_capture_baseline(self._history_source))
            with transaction(self.printer) as con:
                con.execute("INSERT INTO lease_baselines VALUES (?,?) "
                            "ON CONFLICT(printer) DO UPDATE SET baseline=excluded.baseline",
                            (self.printer, baseline))


    def uncertain(self, detail):
        self._set("uncertain", str(detail)[-500:])

    def release(self):
        with transaction(self.printer) as con:
            con.execute("DELETE FROM leases WHERE printer=? AND token=?", (self.printer, self.token))
            con.execute("DELETE FROM lease_baselines WHERE printer=?", (self.printer,))


# Whether this process has reached any lease's submitting() (print-now runs one launch per
# process). Before it, nothing was asked of the printer, so a failure is a clean refusal.
_SUBMITTED = False


def submitted():
    return _SUBMITTED


def reset_submitted():
    global _SUBMITTED
    _SUBMITTED = False


def acquire(printer, *, job_json=None, gcode_sha256=None, remote=None, observed_file=None, started_files=()):
    """Take the printer's lease or refuse. observed_file is the file the printer reports right now;
    started_files is its job history as (job_id, filename, start_epoch) triples, or a callable
    read when an earlier submission is held (see reached_printer). The new lease keeps it as
    its history source: at submitting() it captures the pre-submission baseline — the jobs
    already in history at that moment predate anything this lease submits, so they can never
    confirm it later (P1-121).

    `printer` is the roster printer id (#334): the lease lives in that
    printer's own database and its row is keyed by the id.
    """
    with transaction(printer) as con:
        row = con.execute("SELECT * FROM leases WHERE printer=?", (printer,)).fetchone()
        if row is not None:
            if row["phase"] == "leased" and _leased_abandoned(row):
                print(f"LAUNCH LEASE RELEASED: an earlier launch stopped before submission ({_describe(row)}).", flush=True)
            elif row["phase"] != "leased" and reached_printer(row, observed_file, started_files,
                                                              baseline=_stored_baseline(con, printer)):
                print(f"LAUNCH RECONCILED: the printer reports {row['remote']}, so the earlier {row['phase']} "
                      "submission reached it. Not resubmitted.", flush=True)
            elif row["phase"] == "leased" or (row["phase"] == "submitting" and _owner_alive(row)):
                raise SystemExit(f"REFUSED: another launch on this printer is in progress ({_describe(row)}). "
                                 "Nothing uploaded or started.")
            else:
                raise SystemExit(f"REFUSED: an earlier start on this printer is uncertain ({_describe(row)}). "
                                 "Check the printer, then run `python3 -m print_handy.core.model_job reconcile-launch "
                                 "--started` or `--not-started`. It is never resubmitted automatically. "
                                 "Nothing uploaded or started.")
            con.execute("DELETE FROM leases WHERE printer=?", (printer,))
        token = uuid.uuid4().hex
        now = _now()
        con.execute("INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (printer, token, "leased", str(job_json) if job_json else None, gcode_sha256, remote,
                     os.getpid(), socket.gethostname(), now, now, None))
        # The previous lease's baseline was consumed judging its row above; the
        # new lease captures its own at submitting() (Lease._history_source).
        con.execute("DELETE FROM lease_baselines WHERE printer=?", (printer,))
    history_source = started_files if (callable(started_files) or started_files) else None
    return Lease(printer, token, history_source=history_source)


def pending(printer=None):
    """Held leases: one printer's, or every roster printer's when none is named."""
    if printer is not None or os.environ.get("PRINT_HANDY_LAUNCH_DB"):
        with transaction(printer) as con:
            rows = con.execute("SELECT * FROM leases" + (" WHERE printer=?" if printer else ""),
                               (printer,) if printer else ()).fetchall()
        return [dict(r) for r in rows]
    found = []
    for entry in settings.printers():
        path = settings.printer_state_dir(entry["id"]) / "jobs.db"
        if not path.is_file():
            continue  # a printer that never launched has no database yet; reading must not create one
        with transaction(entry["id"]) as con:
            found += [dict(r) for r in con.execute("SELECT * FROM leases").fetchall()]
    return found


def resolve(printer, outcome):
    """Operator reconciliation of an uncertain start. Clears the lease; consumed approvals stay consumed."""
    if outcome not in ("started", "not_started"):
        raise ValueError("outcome must be started or not_started")
    with transaction(printer) as con:
        row = con.execute("SELECT * FROM leases WHERE printer=?", (printer,)).fetchone()
        if row is None:
            raise SystemExit("No launch is pending on this printer; nothing to reconcile.")
        if row["phase"] == "leased" and not _leased_abandoned(row, unknown=False):  # the operator vouches for other hosts
            raise SystemExit(f"REFUSED: that launch is still running ({_describe(row)}); nothing changed.")
        con.execute("DELETE FROM leases WHERE printer=?", (printer,))
        con.execute("DELETE FROM lease_baselines WHERE printer=?", (printer,))
    out = dict(row, reconciled=outcome, reconciled_at=_now())
    print("LAUNCH RECONCILED " + json.dumps(out), flush=True)
    return out


# A printer clock this far from the host's is "off": history start times can no longer be compared
# with the lease time directly, and the operator is warned (never blocked).
CLOCK_SKEW_WARN_S = 300


def printer_clock_skew(moonraker_url, timeout=6):
    """Seconds the host clock is ahead of the printer's (positive: printer behind), or None.

    Read from the HTTP Date header Moonraker already sends, so no extra credential or port is
    needed. Unreadable means unknown, never zero.
    """
    import time
    import urllib.request
    from email.utils import parsedate_to_datetime
    try:
        req = urllib.request.Request(str(moonraker_url).rstrip("/") + "/server/info", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            stamp = resp.headers.get("Date")
        return time.time() - parsedate_to_datetime(stamp).timestamp() if stamp else None
    except Exception:
        return None


def clock_warning(skew):
    """The operator-facing warning for a printer whose clock is off, or None. Informational only."""
    if skew is None or abs(skew) < CLOCK_SKEW_WARN_S:
        return None
    days = abs(skew) / 86400
    size = f"{days:.0f} day(s)" if days >= 1 else f"{abs(skew) / 60:.0f} minute(s)"
    side = "behind" if skew > 0 else "ahead of"
    return (f"WARNING: the printer's clock is {size} {side} real time. The print is not affected, but until the "
            "printer syncs its time: its job history records wrong dates, so Print Handy can fail to match this "
            "print to its start and leave the printer's launch lock stuck as uncertain (blocking the next print "
            "until it is reconciled); timelapse, duration and \"started at\" times can be wrong; and TLS "
            "connections the printer makes can fail. Fix: let the printer reach an NTP server (UDP 123) and "
            "reboot it.")
