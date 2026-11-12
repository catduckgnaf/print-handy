"""One start-truth function per transport (#1318, #1160 proposal 2).

Every "started", "preparing", "uncertain" and "not started" verdict a print path reaches after
its one start request comes from the function for its transport here, which compares a
pre-request baseline with what the printer reports afterwards:

- ``moonraker`` (ZMod and generic Klipper): the print_stats job identity read just before the
  request (state, file, durations) against the readback after it (``classify_start_readback``).
  A reprint reports the same file name and keeps the axes homed, so only a change counts.
- ``bambu`` (Bambu Lab LAN): the subtask transition. The package's subtask name and
  gcode_state must differ from the idle snapshot taken inside the lease.
- ``octoprint``, and ``polled`` for the other polled transports (PrusaLink, Elegoo SDCP, stock
  Flashforge): the job file and state against the idle status read inside the lease.

The functions are pure: callers take the reads, pass them in, and render the verdict with the
``*_fields`` helpers below, so the phase and outcome strings each print path has always emitted
are spelled only here. ``tests/pipeline/test_start_truth_call_sites.py`` fails when a start call
site in model_job decides a verdict without these functions.

Consolidation only: no rule here is looser than the copy it replaced.
"""
from __future__ import annotations

from typing import Any, Callable, NamedTuple

from . import launch_state

# Verdicts.
STARTED = "started"          # the printer runs this upload
PREPARING = "preparing"      # it accepted this upload and is still heating, homing or loading
UNCERTAIN = "uncertain"      # no evidence either way; never resubmitted
NOT_STARTED = "not_started"  # the printer reports this start ended without printing

# The phase and outcome strings print-now's outcome line carries (prepared_intake and print_next
# parse them). Spelled here only.
PHASE_EXECUTING = "executing"
PHASE_PREPARING = "preparing"
PHASE_AWAITING = "awaiting_confirmation"
PHASE_UNRESOLVED = "preparing_or_unresolved"
PHASE_SUBMITTED = "submitted_unconfirmed"
OUTCOME_PRINTING = "printing"
OUTCOME_PREPARING = "preparing"
OUTCOME_UNCONFIRMED = "unconfirmed"
OUTCOME_UNKNOWN = "unknown"
CONFIRMED_PHASES = (PHASE_EXECUTING, PHASE_PREPARING)
PENDING_PHASES = (PHASE_AWAITING, PHASE_UNRESOLVED, PHASE_SUBMITTED)

# What _wait_printing returns when the printer prepared for this start and then went back
# cold without printing it.
ABANDONED = "abandoned"

# Normalized states that mean "accepted, still starting" (heating the bed, running its calibration).
START_TRANSITIONAL_STATES = frozenset({"heating", "calibrate_doing", "busy", "preparing", "starting"})

# Every model_job print path that sends a start request (calls ``lease.submitting()``), and the
# start-truth function its verdict comes from. The call-site test fails when a path is added
# without an entry here, or when an entry's verdict does not reach that function.
START_CALL_SITES = {
    "print_mapped_native": "moonraker",
    "_print_now_locked": "moonraker",
    "_print_octoprint": "octoprint",
    "_print_bambu": "bambu",
    "_print_elegoo_sdcp": "polled",
    "_print_prusa": "polled",
    "_print_flashforge_stock": "polled",
}


class StartTruth(NamedTuple):
    verdict: str
    source: str = ""          # what decided it: readback, wait, heating, abandoned, live, unchanged, none
    readback: Any = None      # Moonraker: the classified readback naming this upload, else None
    heating: Any = None       # Moonraker: the heater-target read taken (True, False, None)
    readable: bool = True     # Moonraker: whether the post-request status could be read at all
    waited: Any = None        # Moonraker: the state the wait loop settled on
    live: Any = None          # polled transports: the normalized status judged

    @property
    def confirmed(self):
        """The printer took the start: printing, or accepted and still preparing."""
        return self.verdict in (STARTED, PREPARING)

    @property
    def quiet(self):
        """Readable, not naming this upload, and every heater target 0: what lets an HTTP 4xx answer
        to the start request be read as "definitely not started"."""
        return bool(self.readable) and self.readback is None and self.heating is False


def _value(x):
    return x() if callable(x) else x


# --------------------------------------------------------------------------- Moonraker

class StartReadback(NamedTuple):
    outcome: str
    evidence: Any
    message: str


def _readback_status(snapshot):
    """The Klipper object map inside a Moonraker query reply (or a bare map); {} for anything else."""
    if not isinstance(snapshot, dict):
        return {}
    if isinstance(snapshot.get("result"), dict) and isinstance(snapshot["result"].get("status"), dict):
        return snapshot["result"]["status"]
    if isinstance(snapshot.get("status"), dict):
        return snapshot["status"]
    return snapshot


def _same_job(now, before):
    """Whether two print_stats reads show the same job: a new load resets state, file and durations.
    State and file are always compared; a duration only where both reads carry it."""
    keys = ["state", "filename"] + [k for k in ("total_duration", "print_duration") if k in now and k in before]
    return all(now.get(k) == before.get(k) for k in keys)


def classify_start_readback(status_snapshot, baseline=None) -> StartReadback:
    """Pure function classifying printer status after submission.

    Outcomes:
    - 'preparing': accepted, with heating, homing or prep evidence.
    - 'printing': state printing.
    - 'failed': error or cancelled.
    - 'unconfirmed': no evidence.

    `baseline` is the status read immediately before the start request (_start_baseline). With one,
    only what CHANGED counts: a print_stats job identity (state, file, durations) equal to the
    baseline's is the previous run's leftover, so its complete/cancelled/error state and file name say
    nothing about this start; heater targets count only where the baseline proved them 0. An empty
    baseline (unreadable) proves no change at all. Without a baseline (None) the live signals count as
    before, but a file name or homed axes alone never do: a reprint of the same artifact reports the
    same name, and Klipper keeps axes homed between prints.
    """
    status = _readback_status(status_snapshot)
    has_base = baseline is not None
    base = _readback_status(baseline) if has_base else {}

    def obj(source, name):
        value = source.get(name)
        return value if isinstance(value, dict) else {}

    def fresh(name, key, value):
        """Evidence counts with no baseline (legacy callers), or when it differs from a readable one."""
        if not has_base:
            return True
        return bool(base) and obj(base, name).get(key) != value

    ps = status.get("print_stats") if isinstance(status.get("print_stats"), dict) else {}
    base_ps = obj(base, "print_stats")
    # A new job identity: the printer loaded a job since the baseline (only provable from a readable one).
    new_job = bool(base_ps) and bool(ps) and not _same_job(ps, base_ps)
    ext = status.get("extruder") if isinstance(status.get("extruder"), dict) else {}
    bed = status.get("heater_bed") if isinstance(status.get("heater_bed"), dict) else {}
    vsd = status.get("virtual_sdcard") if isinstance(status.get("virtual_sdcard"), dict) else {}
    idle = status.get("idle_timeout") if isinstance(status.get("idle_timeout"), dict) else {}
    hooks = status.get("webhooks") if isinstance(status.get("webhooks"), dict) else {}
    toolhead = status.get("toolhead") if isinstance(status.get("toolhead"), dict) else {}

    raw_state = ps.get("state") or status.get("state")
    state = raw_state.lower() if isinstance(raw_state, str) else None

    # 1. Failed. Klipper shut down is failure whatever ran before; an error or cancelled print_stats is
    # this start's only when it is not the previous job's leftover (same identity as the baseline).
    hook_state = (hooks.get("state") or "").lower() if isinstance(hooks.get("state"), str) else ""
    job_failed = state in ("error", "cancelled") and (not has_base or new_job)
    if job_failed or hook_state in ("error", "shutdown"):
        fail_state = state if job_failed else hook_state
        return StartReadback(
            outcome="failed",
            evidence={"state": fail_state},
            message=f"print failed: state is {fail_state}",
        )

    # 2. Printing
    if state == "printing":
        filament_used = None
        for k in ("filament_used", "filament_used_mm", "filament_length"):
            if k in ps:
                filament_used = ps[k]
                break
            if k in status:
                filament_used = status[k]
                break

        extruder_delta = None
        for candidate in (
            status.get("extruder_position_delta"),
            status.get("extruder_delta"),
            ext.get("position_delta"),
            ext.get("delta"),
            toolhead.get("extruder_position_delta"),
            toolhead.get("extruder_delta"),
        ):
            if candidate is not None:
                extruder_delta = candidate
                break

        extrusion_confirmed = False
        if filament_used is not None:
            try:
                if float(filament_used) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass
        if extruder_delta is not None:
            try:
                if float(extruder_delta) > 0:
                    extrusion_confirmed = True
            except (ValueError, TypeError):
                pass

        msg = "printing" if extrusion_confirmed else "printing (extrusion not yet confirmed)"
        evidence = {
            "state": "printing",
            "filament_used": filament_used,
            "extruder_position_delta": extruder_delta,
            "extrusion_confirmed": extrusion_confirmed,
            "info": ps.get("info") or status.get("info"),
        }
        return StartReadback(outcome="printing", evidence=evidence, message=msg)

    # 3. Preparing
    prep_evidence = {}
    heater_targets = {}

    def proven_off(name):
        """Without a baseline any target counts (legacy); with one, only where it read exactly 0."""
        if not has_base:
            return True
        try:
            return float(obj(base, name).get("target")) == 0
        except (TypeError, ValueError):
            return False

    for name, heater in (("extruder", ext), ("heater_bed", bed)):
        t = heater.get("target")
        if t is not None and proven_off(name):
            try:
                if float(t) > 0:
                    heater_targets[name] = float(t)
            except (ValueError, TypeError):
                pass
    for name, key in (("extruder", "extruder_target"), ("heater_bed", "heater_bed_target")):
        if key in status and proven_off(name):
            try:
                if float(status[key]) > 0:
                    heater_targets[name] = float(status[key])
            except (ValueError, TypeError):
                pass
    if heater_targets:
        prep_evidence["heating"] = heater_targets

    # Klipper keeps the axes homed between prints, so homed axes are evidence only when the baseline
    # showed them unhomed; a homing-in-progress flag is live.
    homed = toolhead.get("homed_axes") or status.get("homed_axes")
    if homed and has_base and base and not obj(base, "toolhead").get("homed_axes"):
        prep_evidence["homing"] = homed
    elif toolhead.get("homing") or status.get("homing"):
        prep_evidence["homing"] = True

    if (vsd.get("is_active") is True and fresh("virtual_sdcard", "is_active", True)) \
            or (vsd.get("file_path") and fresh("virtual_sdcard", "file_path", vsd.get("file_path"))):
        prep_evidence["virtual_sdcard"] = vsd.get("file_path") or True
    if (idle.get("state") == "Printing" and fresh("idle_timeout", "state", "Printing")) \
            or status.get("idle_state") == "Printing":
        prep_evidence["idle_timeout"] = "Printing"
    if hook_state == "printing" and fresh("webhooks", "state", hooks.get("state")):
        prep_evidence["webhooks"] = "Printing"
    if new_job and ps.get("filename") and state not in ("error", "cancelled"):
        # The printer loaded a job since the baseline (state, file or durations changed): the
        # caller has already matched the file to this upload. A name alone is never evidence.
        prep_evidence["new_job"] = ps.get("filename")
    if status.get("preparation_evidence") is True or status.get("prep") is True:
        prep_evidence["preparation_evidence"] = True
    if status.get("ifs_loading") or status.get("ifs_load"):
        prep_evidence["ifs_loading"] = True
    ifs_obj = status.get("zmod_ifs") if isinstance(status.get("zmod_ifs"), dict) else {}
    if (ifs_obj.get("state") or ifs_obj.get("feed") or ifs_obj.get("action")) \
            and (not has_base or (bool(base) and obj(base, "zmod_ifs") != ifs_obj)):
        prep_evidence["zmod_ifs"] = ifs_obj

    if prep_evidence:
        return StartReadback(
            outcome="preparing",
            evidence=prep_evidence,
            message="printer accepted the job and is preparing (heating/loading); not a failure; do not resubmit",
        )

    # 4. Unconfirmed
    return StartReadback(
        outcome="unconfirmed",
        evidence=None,
        message="submitted, awaiting confirmation",
    )


def moonraker_readback(snapshot, remote, baseline=None):
    """The classified readback of one post-request Moonraker query reply, only when print_stats
    names this exact upload (launch_state.same_remote); None when it shows another file. A reply
    that is not a query map raises, as an unreadable read does."""
    ps = (snapshot.get("result", {}).get("status", {}) or {}).get("print_stats") or {}
    if not launch_state.same_remote(ps.get("filename"), remote):
        return None
    return classify_start_readback(snapshot, baseline)


def moonraker_wait_step(state, filename, remote, leftover):
    """One wait-loop read: "printing" when it shows this upload printing, the state when this start
    ended (error or cancelled that is not the previous job's leftover), else None."""
    unchanged = leftover is not None and (state, filename) == leftover
    # A ZMod .gcode.3mf prints as 3mf_print/X.gcode; an exact compare never matched and sat out the wait.
    if state == "printing" and launch_state.same_remote(filename, remote):
        return "printing"
    if state in ("error", "cancelled") and not unchanged:
        return state
    return None


def moonraker_abandoned(prep, prep_seen, state, filename, leftover):
    """Whether native preparation ended without printing: the heaters were seen set for this start,
    are back at 0 now, and print_stats is at standby or still the previous job's leftover."""
    unchanged = leftover is not None and (state, filename) == leftover
    return prep is False and prep_seen and (state == "standby" or unchanged)


def moonraker_running(state, filename, remote, *, heaters_were_off, heating):
    """Whether the printer shows this start under way: it reports this upload printing or paused, or
    heater targets are set now that were proven off just before the request. `heating` is a value
    or a zero-argument read, taken only when it can decide."""
    if state in ("printing", "paused") and launch_state.same_remote(filename, remote):
        return True
    return bool(heaters_were_off) and _value(heating) is True


def moonraker(readback=None, *, waited=None, heating=None, heating_counts=False, readable=True) -> StartTruth:
    """The Moonraker start truth (ZMod mapped route, generic Klipper route, reprints).

    readback: moonraker_readback() of the status read after the request (baseline-compared), or a
        zero-argument read returning it; None when print_stats does not name this upload.
    waited: the state _wait_printing settled on, when the path waited.
    heating: the heater-target read (_preparation_evidence) or a zero-argument read of it; taken only
        when the start is not already proven printing.
    heating_counts: whether heater targets alone are this start's preparation. True only where the
        caller proved every heater was off before the request, or the request was answered and the
        native route heats for it.
    readable: False when the post-request status could not be read at all.

    Order: abandoned preparation, then printing (wait or readback), then preparing (readback, then
    counted heating), then a readback reporting this file's start failed, else uncertain.
    """
    if waited == ABANDONED:
        return StartTruth(NOT_STARTED, "abandoned", waited=waited, readable=readable)
    rb = _value(readback)
    if waited == "printing" or (rb is not None and rb.outcome == "printing"):
        return StartTruth(STARTED, "wait" if waited == "printing" else "readback", rb, None, readable, waited)
    heat = _value(heating)
    if rb is not None and rb.outcome == "preparing":
        return StartTruth(PREPARING, "readback", rb, heat, readable, waited)
    if heat is True and heating_counts:
        return StartTruth(PREPARING, "heating", rb, heat, readable, waited)
    if rb is not None and rb.outcome == "failed":
        return StartTruth(NOT_STARTED, "readback", rb, heat, readable, waited)
    return StartTruth(UNCERTAIN, "readback" if rb is not None else "none", rb, heat, readable, waited)


def lost_fields(truth):
    """The outcome fields after a start request whose response was lost or rejected
    (_reconcile_lost_start). A readback that is visible but not printing or preparing, including one
    reporting failure, stays unresolved: the lease is never released by guessing."""
    rb = truth.readback
    if truth.verdict == PREPARING and truth.source == "heating" and rb is None:
        # The baseline proved every heater target was 0 before this one submission, so targets set
        # now are this job's preparation (2026-10-30: the screenless SET_ZCOLOR POST held open
        # through warmup and timed out after 30 s).
        return {'phase': PHASE_PREPARING, 'outcome': OUTCOME_PREPARING, 'accepted': True, 'started': True,
                'preparation_evidence': True, 'quiet': False,
                'message': 'start response was lost; the printer is heating for this job'}
    if rb is None:
        return {'phase': PHASE_UNRESOLVED, 'outcome': OUTCOME_UNKNOWN, 'accepted': False, 'started': False,
                'quiet': truth.quiet,
                'message': 'start response was lost; printer status does not yet identify this file; do not resubmit'}
    fields = {'readback': rb._asdict(), 'quiet': False}
    if truth.verdict == STARTED:
        return {'phase': PHASE_EXECUTING, 'outcome': OUTCOME_PRINTING, 'accepted': True, 'started': True,
                'message': rb.message, **fields}
    if truth.verdict == PREPARING:
        return {'phase': PHASE_PREPARING, 'outcome': OUTCOME_PREPARING, 'accepted': True, 'started': True,
                'message': rb.message, **fields}
    return {'phase': PHASE_UNRESOLVED, 'outcome': OUTCOME_UNKNOWN, 'accepted': False, 'started': False,
            'message': 'the submitted file is visible but start progress is unresolved; do not resubmit', **fields}


def mapped_fields(truth):
    """The fields the mapped native route (_confirm_mapped_start) records once its wait ends."""
    rb = truth.readback
    if truth.verdict == STARTED:
        return dict(accepted=True, started=True, phase=PHASE_EXECUTING, moonraker_state="printing",
                    outcome=OUTCOME_PRINTING, message=(rb.message if rb else "printing"),
                    extrusion_confirmed=bool(rb and (rb.evidence or {}).get("extrusion_confirmed")))
    # The AD5X native route heats and loads the IFS slot for several minutes before print_stats
    # reports printing (2026-10-16: ~7 min), so set heater targets prove acceptance here.
    prep = truth.verdict == PREPARING
    return dict(accepted=prep, started=prep, moonraker_state=truth.waited,
                phase=PHASE_PREPARING if prep else PHASE_UNRESOLVED,
                outcome=OUTCOME_PREPARING if prep else (rb.outcome if rb else OUTCOME_UNCONFIRMED),
                message=rb.message if rb else "submitted, awaiting confirmation",
                preparation_evidence=truth.heating)


def generic_fields(truth, *, file=None, method=None):
    """The generic Moonraker route's fields (_confirm_generic_start and the printing record).

    Started: the verdict keys only (the caller adds the print details around them). Preparing and
    uncertain: the whole outcome. Not started is the caller's refusal and has no fields."""
    rb = truth.readback
    if truth.verdict == STARTED:
        return {"phase": PHASE_EXECUTING, "outcome": OUTCOME_PRINTING, "message": (rb.message if rb else "printing"),
                "extrusion_confirmed": bool(rb and (rb.evidence or {}).get("extrusion_confirmed"))}
    if truth.verdict == PREPARING:
        return {"started": True, "accepted": True, "phase": PHASE_PREPARING, "outcome": OUTCOME_PREPARING,
                "file": file, "method": method, "message": rb.message}
    return {"started": False, "accepted": True, "phase": PHASE_AWAITING, "outcome": OUTCOME_UNCONFIRMED,
            "file": file, "method": method, "message": "submitted, awaiting confirmation"}


def uncertain_fields(phase=PHASE_AWAITING):
    """The verdict keys of a start that went out and then could not be read back at all."""
    return {"phase": phase, "outcome": OUTCOME_UNKNOWN}


# --------------------------------------------------------------------------- polled transports

def _transitioned(live, baseline, keys):
    """True when there is no baseline (legacy callers), or `live` differs from it on `keys`."""
    if not isinstance(baseline, dict) or not baseline:
        return True
    return any(live.get(k) != baseline.get(k) for k in keys)


def polled(live, remote, baseline=None) -> StartTruth:
    """The start truth of a polled transport (PrusaLink, Elegoo SDCP, stock Flashforge; OctoPrint).

    Only a status naming this upload (launch_state.same_remote) and differing from the idle baseline
    read inside the lease counts: printing is started, a transitional state (heating, calibrating) is
    preparing, an error or cancelled state is not started, anything else is uncertain."""
    live = live if isinstance(live, dict) else {}
    state = live.get("state")
    if not launch_state.same_remote(live.get("filename"), remote):
        return StartTruth(UNCERTAIN, "live", live=live)
    if not _transitioned(live, baseline, ("state", "filename")):
        return StartTruth(UNCERTAIN, "unchanged", live=live)
    if state == "printing":
        return StartTruth(STARTED, "live", live=live)
    if state in START_TRANSITIONAL_STATES:
        return StartTruth(PREPARING, "live", live=live)
    if state in ("error", "cancelled"):
        return StartTruth(NOT_STARTED, "live", live=live)
    return StartTruth(UNCERTAIN, "live", live=live)


def octoprint(live, remote, baseline=None) -> StartTruth:
    """The OctoPrint start truth. Its REST status carries no job start time, so the baseline is the
    idle job file and state read inside the lease, compared by ``polled``."""
    return polled(live, remote, baseline)


BAMBU_PREPARE_STATES = frozenset({"PREPARE", "SLICING"})


def _bambu_names(reported, remote):
    """The subtask name the start command set is the package name; the bare project name (the
    printer's display spelling of the same package) is accepted too. Exact, never a prefix."""
    reported = str(reported or "")
    if not reported:
        return False
    accepted = {remote}
    for suffix in (".gcode.3mf", ".3mf"):
        if remote.endswith(suffix):
            accepted.add(remote[:-len(suffix)])
    return reported in accepted


def bambu(live, remote, baseline=None) -> StartTruth:
    """The Bambu Lab start truth: the subtask transition.

    The printer took this start when its subtask names this package and the subtask or gcode_state
    changed from the idle snapshot read inside the lease. RUNNING is started; PREPARE and SLICING (an
    A1 unpacking a package) are preparing; FAILED is not started; anything else is uncertain."""
    live = live if isinstance(live, dict) else {}
    if not _bambu_names(live.get("filename"), remote):
        return StartTruth(UNCERTAIN, "live", live=live)
    if not _transitioned(live, baseline, ("state", "gcode_state", "filename")):
        return StartTruth(UNCERTAIN, "unchanged", live=live)
    state = live.get("state")
    if state == "printing":
        gcode_state = str(live.get("gcode_state") or "").upper()
        return StartTruth(PREPARING if gcode_state in BAMBU_PREPARE_STATES else STARTED, "live", live=live)
    if state == "error":
        return StartTruth(NOT_STARTED, "live", live=live)
    return StartTruth(UNCERTAIN, "live", live=live)


def from_reports(reports: Callable[[dict, str], bool]):
    """A start-truth function from a legacy yes/no matcher: a match in a transitional state is
    preparing, any other match is started, no match is uncertain."""
    def truth(live, remote, baseline=None):
        live = live if isinstance(live, dict) else {}
        if not reports(live, remote):
            return StartTruth(UNCERTAIN, "live", live=live)
        return StartTruth(PREPARING if live.get("state") in START_TRANSITIONAL_STATES else STARTED, "live", live=live)
    return truth


def polled_fields(confirmed, preparing):
    """(phase, outcome, message) of a polled start: confirmed and still preparing, confirmed, or not.
    A polled printer reporting this start failed is not confirmed and stays uncertain: the polled
    paths never released a lease on a failure report, and consolidation does not add that."""
    if confirmed and preparing:
        return (PHASE_PREPARING, OUTCOME_PREPARING,
                "printer accepted the job and is preparing (heating/calibrating); not a failure; do not resubmit")
    if confirmed:
        return PHASE_EXECUTING, OUTCOME_PRINTING, "printing"
    return PHASE_AWAITING, OUTCOME_UNCONFIRMED, "submitted, awaiting confirmation; do not resubmit"
