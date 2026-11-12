"""Offline tests for the one start-truth function per transport (#1318, #1160 proposal 2).

Each transport's function compares a pre-request baseline with what the printer reports after
the start request, and every verdict (started, preparing, uncertain, not started) comes from it.
No printer is contacted: every status here is a recorded shape.
"""
from print_handy.core import start_truth as T

REMOTE = "part_abc.gcode"


# --------------------------------------------------------------------------- Moonraker

def _mr(state="standby", filename=REMOTE, *, ext_target=0, bed_target=0, homed="", total=0.0, **extra):
    status = {"print_stats": {"state": state, "filename": filename, "total_duration": total},
              "extruder": {"target": ext_target}, "heater_bed": {"target": bed_target},
              "toolhead": {"homed_axes": homed}, **extra}
    return {"result": {"status": status}}


def _baseline(snapshot):
    return snapshot["result"]["status"]


def _moonraker(after, baseline, **kw):
    return T.moonraker(T.moonraker_readback(after, REMOTE, baseline), **kw)


def test_moonraker_started_when_this_upload_prints():
    base = _baseline(_mr("standby", ""))
    truth = _moonraker(_mr("printing", REMOTE, ext_target=215, total=3.0), base)
    assert truth.verdict == T.STARTED and truth.confirmed
    assert T.generic_fields(truth)["phase"] == T.PHASE_EXECUTING


def test_moonraker_started_from_the_wait_loop_alone():
    truth = T.moonraker(None, waited="printing")
    assert truth.verdict == T.STARTED and truth.source == "wait"
    assert T.mapped_fields(truth)["moonraker_state"] == "printing"


def test_moonraker_preparing_when_heaters_proven_off_come_on():
    base = _baseline(_mr("standby", ""))
    truth = _moonraker(_mr("standby", REMOTE, ext_target=215), base)
    assert truth.verdict == T.PREPARING and truth.source == "readback"
    assert T.lost_fields(truth)["phase"] == T.PHASE_PREPARING


def test_moonraker_preparing_from_counted_heating_with_no_file_named():
    truth = T.moonraker(None, heating=True, heating_counts=True)
    assert truth.verdict == T.PREPARING and truth.source == "heating"
    fields = T.lost_fields(truth)
    assert fields["started"] is True and fields["preparation_evidence"] is True


def test_moonraker_heating_alone_does_not_count_unless_proven():
    truth = T.moonraker(None, heating=True, heating_counts=False)
    assert truth.verdict == T.UNCERTAIN and not truth.quiet
    assert T.lost_fields(truth)["phase"] == T.PHASE_UNRESOLVED


def test_moonraker_reprint_leftover_is_uncertain_not_started():
    # The previous run of the same artifact: state complete, same file, axes still homed.
    leftover = _mr("complete", REMOTE, homed="xyz", total=1200.0)
    truth = _moonraker(leftover, _baseline(leftover))
    assert truth.verdict == T.UNCERTAIN
    assert T.generic_fields(truth, file=REMOTE, method="print/start")["outcome"] == T.OUTCOME_UNCONFIRMED


def test_moonraker_reprint_leftover_error_is_not_this_starts_failure():
    leftover = _mr("error", REMOTE, total=50.0)
    assert _moonraker(leftover, _baseline(leftover)).verdict == T.UNCERTAIN


def test_moonraker_not_started_when_this_upload_fails():
    base = _baseline(_mr("complete", "older.gcode", total=900.0))
    truth = _moonraker(_mr("error", REMOTE, total=2.0), base)
    assert truth.verdict == T.NOT_STARTED and not truth.confirmed
    # After a lost response the lease is never released by guessing: a failure stays unresolved.
    assert T.lost_fields(truth)["phase"] == T.PHASE_UNRESOLVED


def test_moonraker_not_started_when_preparation_is_abandoned():
    truth = T.moonraker(lambda: (_ for _ in ()).throw(AssertionError("not read")), waited=T.ABANDONED)
    assert truth.verdict == T.NOT_STARTED and truth.source == "abandoned"


def test_moonraker_another_file_is_never_this_start():
    assert T.moonraker_readback(_mr("printing", "someone_else.gcode"), REMOTE) is None
    truth = T.moonraker(None, heating=False)
    assert truth.verdict == T.UNCERTAIN and truth.quiet  # what lets a 4xx read as "not started"


def test_moonraker_wait_step_and_abandoned_rule():
    leftover = ("complete", REMOTE)
    assert T.moonraker_wait_step("printing", "3mf_print/part_abc.gcode", "part_abc.gcode.3mf", None) == "printing"
    assert T.moonraker_wait_step("error", REMOTE, REMOTE, leftover) == "error"
    assert T.moonraker_wait_step("complete", REMOTE, REMOTE, leftover) is None
    assert T.moonraker_abandoned(False, True, "standby", "", leftover)
    assert T.moonraker_abandoned(False, True, "complete", REMOTE, leftover)
    assert not T.moonraker_abandoned(False, False, "standby", "", leftover)
    assert not T.moonraker_abandoned(True, True, "standby", "", leftover)


def test_moonraker_running_reads_heating_only_when_it_can_decide():
    def boom():
        raise AssertionError("heating read when it could not decide")
    assert T.moonraker_running("paused", REMOTE, REMOTE, heaters_were_off=True, heating=boom)
    assert not T.moonraker_running("standby", "", REMOTE, heaters_were_off=False, heating=boom)
    assert T.moonraker_running("standby", "", REMOTE, heaters_were_off=True, heating=lambda: True)


# --------------------------------------------------------------------------- Bambu Lab

PACKAGE = "plate_1.gcode.3mf"
BAMBU_IDLE = {"state": "complete", "gcode_state": "FINISH", "filename": "plate_1"}  # a reprint's leftover


def test_bambu_started_on_a_subtask_transition_to_running():
    truth = T.bambu({"state": "printing", "gcode_state": "RUNNING", "filename": "plate_1"}, PACKAGE, BAMBU_IDLE)
    assert truth.verdict == T.STARTED


def test_bambu_preparing_while_the_package_unpacks():
    for word in ("PREPARE", "SLICING"):
        truth = T.bambu({"state": "printing", "gcode_state": word, "filename": PACKAGE}, PACKAGE, BAMBU_IDLE)
        assert truth.verdict == T.PREPARING and truth.confirmed


def test_bambu_uncertain_when_the_subtask_did_not_change():
    assert T.bambu(dict(BAMBU_IDLE), PACKAGE, BAMBU_IDLE).verdict == T.UNCERTAIN
    assert T.bambu({"state": "printing", "gcode_state": "RUNNING", "filename": "other"}, PACKAGE,
                   BAMBU_IDLE).verdict == T.UNCERTAIN
    assert T.bambu({}, PACKAGE).verdict == T.UNCERTAIN


def test_bambu_not_started_when_this_subtask_fails():
    truth = T.bambu({"state": "error", "gcode_state": "FAILED", "filename": "plate_1"}, PACKAGE, BAMBU_IDLE)
    assert truth.verdict == T.NOT_STARTED and not truth.confirmed


# --------------------------------------------------------------------------- OctoPrint and polled

OCTO_IDLE = {"state": "standby", "filename": REMOTE}  # the same file left selected by the previous run


def test_octoprint_started_when_the_job_moves_to_printing():
    assert T.octoprint({"state": "printing", "filename": REMOTE}, REMOTE, OCTO_IDLE).verdict == T.STARTED


def test_octoprint_preparing_in_a_transitional_state():
    assert T.octoprint({"state": "heating", "filename": REMOTE}, REMOTE, OCTO_IDLE).verdict == T.PREPARING


def test_octoprint_uncertain_when_nothing_changed_or_another_file():
    assert T.octoprint(dict(OCTO_IDLE), REMOTE, OCTO_IDLE).verdict == T.UNCERTAIN
    assert T.octoprint({"state": "printing", "filename": "other.gcode"}, REMOTE, OCTO_IDLE).verdict == T.UNCERTAIN
    assert T.octoprint(None, REMOTE).verdict == T.UNCERTAIN


def test_octoprint_not_started_when_this_job_errors():
    assert T.octoprint({"state": "error", "filename": REMOTE}, REMOTE, OCTO_IDLE).verdict == T.NOT_STARTED


def test_polled_unchanged_status_is_never_evidence():
    printing = {"state": "printing", "filename": REMOTE}
    assert T.polled(printing, REMOTE, printing).verdict == T.UNCERTAIN
    assert T.polled(printing, REMOTE).verdict == T.STARTED  # no baseline: the legacy rule


def test_polled_fields_keep_a_reported_failure_uncertain():
    assert T.polled_fields(True, True)[0] == T.PHASE_PREPARING
    assert T.polled_fields(True, False)[:2] == (T.PHASE_EXECUTING, T.OUTCOME_PRINTING)
    assert T.polled_fields(False, False)[:2] == (T.PHASE_AWAITING, T.OUTCOME_UNCONFIRMED)


def test_legacy_matcher_keeps_its_old_preparing_rule():
    truth = T.from_reports(lambda live, remote: True)
    assert truth({"state": "heating"}, REMOTE).verdict == T.PREPARING
    assert truth({"state": "printing"}, REMOTE).verdict == T.STARTED
    assert T.from_reports(lambda live, remote: False)({}, REMOTE).verdict == T.UNCERTAIN
