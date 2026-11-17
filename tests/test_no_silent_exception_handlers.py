"""#1417: no exception is swallowed silently anywhere in the shipped code.

Every ``except`` handler in ``print_handy/``, ``scripts/``, ``plugins/`` and
``docker/`` must do something with the failure: prevent it, recover from it,
or report it with a level and a cause. A handler whose whole body is ``pass``,
``...`` or a bare string is a silent swallow and fails this test.

``contextlib.suppress`` is the same swallow under another name, so it is held
to a per-file allowance that can only go down. Those calls predate #1417's
sweep and are tracked in #1455. A new one fails here; removing one
means lowering its count below in the same change. The #1455 sweep removed
or narrowed most of them; the few that remain are reasoned exceptions, each
with its written reason in ``SUPPRESS_REASONS``, and the guard fails when an
allowance entry has no reason.

#1467 extends the same freeze to the next shape of the same swallow: a
handler that catches ``Exception`` (or is bare), binds no exception name,
and neither logs, re-raises nor otherwise reports the failure — it just
returns a default. Counting handlers that way on ``main`` at 623aa6bb
reproduces the count in #1467 exactly: 448 in ``print_handy/`` (450 across
every root scanned here; ``docker/healthcheck.py`` holds the other 2).
Narrow typed catches are not counted: an ``except ValueError: return None``
around a parse is a parse, not a swallow. The handlers #1467 listed on the
launch, purge and gate decision paths were fixed (they log the cause, or
catch only the transport and payload errors they mean to catch), which
lowered the frozen total to 441. Every remaining allowance entry carries
its written reason; the burn-down beyond those decision paths is future
work frozen by this guard, not done.

The #1417 sweep's per-PR files (``tests/test_except_pass_1417_*.py``) keep
their behavior tests. Their file-scoped guards are subsumed by this one.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("print_handy", "scripts", "plugins", "docker")

# contextlib.suppress(...) calls per file, frozen at the end of the #1417 sweep
# and lowered by the #1455 sweep. Only ever lower these. Every file still
# listed here carries its written reason in SUPPRESS_REASONS below (#1455):
# the done state is this dict empty, or reasoned-only, and the guard enforces
# the pairing.
SUPPRESS_ALLOWANCE = {
    "print_handy/core/background_slice.py": 7,
    "print_handy/core/owned_worker.py": 4,
    "print_handy/core/prepared_intake.py": 4,
    "print_handy/intake/fit_flow.py": 1,
    "print_handy/runtime/timelapse_encode.py": 1,
}

# Why each file above may keep its suppress calls (#1455). A file with an
# allowance but no reason here fails the guard, and so does a reason left
# behind for a file whose calls are all gone.
SUPPRESS_REASONS = {
    "print_handy/core/background_slice.py": (
        "Awaiting this runner's own slice tasks: _run records failures in "
        "self.error / self.supports_error, so the await can only surface "
        "CancelledError — an expected end state (a discarded or superseded "
        "slice) whose outcome is read from the fields right after."),
    "print_handy/core/owned_worker.py": (
        "Signaling a process group that may already be dead (callers verify "
        "death separately), the _gone poll whose timeout is the 'not yet' "
        "signal, feeding a worker that died mid-exchange (the read side of "
        "the gather surfaces the failure), and settle(), whose contract is "
        "to absorb cancels while a future finishes."),
    "print_handy/core/prepared_intake.py": (
        "A never-raises TIMING.jsonl tail parse (a torn last line from the "
        "concurrent writer is expected and skipped), awaiting watchers and "
        "tasks this code cancelled itself one line above (their real "
        "failures are retrieved and logged right after), and one bounded "
        "wait whose timeout is an expected outcome, read from the slice "
        "record next."),
    "print_handy/intake/fit_flow.py": (
        "Awaiting the fit task this finally block cancelled itself just "
        "above; a real failure is retrieved and logged right after."),
    "print_handy/runtime/timelapse_encode.py": (
        "flock(LOCK_UN) in a finally: an unlock failure is moot because "
        "closing the handle on the next line releases the lock either way."),
}


# Broad (``except Exception`` or bare) handlers that bind no exception name and
# neither log, re-raise nor otherwise report the failure, per file (#1467):
# (frozen count, written reason the count remains). Only ever lower a count,
# and only with the handler that went away; an entry whose count reaches 0 is
# removed, not kept at 0.
BROAD_NO_LOG_ALLOWANCE = {
    "docker/healthcheck.py": (2,
        "Pre-existing broad no-log handlers in container healthcheck code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/ai_routes.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/__main__.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/edit_chat.py": (3,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/firmware_alert.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/guided.py": (6,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/bed_down.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/calibration.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/color_selection.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/common.py": (13,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/design.py": (2,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/exclude.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/guided.py": (3,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/model_link.py": (2,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/nozzle.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/plate_calibrate.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/print_again.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/print_reboot.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/handlers/spool.py": (2,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/matrix_rest.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/outage_alert.py": (2,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/platform.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/router.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/runtime.py": (2,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/web.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/bot/webui_access.py": (1,
        "Pre-existing broad no-log handlers in chat bot handlers, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/calibration.py": (6,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/ai_fix.py": (4,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/connection_probe.py": (3,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/control_lock.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/edit_plan.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/geometry_layout.py": (4,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/heater_ceiling.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/launch_state.py": (2,
        "Two pre-existing broad no-log handlers remain (the stored-baseline parse and the clock-sync path); the three handlers listed in #1467 now log or catch narrowly. Frozen so a new one fails; burning these down is future work."),
    "print_handy/core/link_intake.py": (2,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/modeled_supports.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/nozzle.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/orca_adapter.py": (8,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/owned_worker.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/prepared_intake.py": (11,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/printer_clock.py": (2,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/printer_state.py": (7,
        "Seven pre-existing broad no-log handlers remain in best-effort hardware-record reads; read_purge_mode, the one listed in #1467, now logs and catches narrowly. Frozen so a new one fails; burning the rest down is future work."),
    "print_handy/core/purge.py": (1,
        "One pre-existing broad no-log handler remains (the adapter fallback in resolve); the four decision-path handlers listed in #1467 now log or catch narrowly. Frozen so a new one fails; burning this last one down is future work."),
    "print_handy/core/roast.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/source_colors.py": (13,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/split_fit.py": (2,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/spool_lifecycle.py": (3,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/start_recorder.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/support_clearance.py": (7,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/thermal_probe.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/upload_collection.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/wrapped_joins.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/core/yolo.py": (1,
        "Pre-existing broad no-log handlers in core decision and slicing code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/crash_report.py": (7,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/credential_binding.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/diagnostics_bundle.py": (5,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/firmware_alerts.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/firmware_update.py": (4,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/gcode/verify.py": (2,
        "Pre-existing broad no-log handlers in G-code handling code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/go2rtc.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/healthcheck.py": (3,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/intake/fit_flow.py": (4,
        "Pre-existing broad no-log handlers in intake flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/intake/questions.py": (1,
        "Pre-existing broad no-log handlers in intake flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/jobs/cli.py": (1,
        "Pre-existing broad no-log handlers in job flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/jobs/prompts.py": (1,
        "Pre-existing broad no-log handlers in job flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/jobs/slice.py": (3,
        "Pre-existing broad no-log handlers in job flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/jobs/sources.py": (3,
        "Pre-existing broad no-log handlers in job flow code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/knowledge/thermal.py": (1,
        "Pre-existing broad no-log handlers in knowledge lookup code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/launch/print_now.py": (16,
        "Pre-existing broad no-log handlers in launch path code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/listeners.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/live_activity.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/live_stream.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/logs.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/notify.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printer_control.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/anycubic/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/bambulab/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/bambulab/listener.py": (4,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/creality/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/elegoo/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/elegoo_sdcp/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/prusa/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/printers/qidi/adapter.py": (1,
        "Pre-existing broad no-log handlers in printer family adapters, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/_config.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/bambu_watch.py": (5,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/bed_separate.py": (4,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/camera_watch.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/discord_print_summary.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/elegoo_watch.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/exclude_chat.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/family_control.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/family_event_listener.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/lane_data_sync.py": (10,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/local_empty.py": (4,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/local_vision.py": (5,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/maintenance.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/monitor_worker.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/moonraker_discord_announcements.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/moonraker_event_listener.py": (22,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/multislot_usage.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/next_plate_offer.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/nozzle_change.py": (5,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/octo_watch.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/panic.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/plan_timelapse.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/print_alert_buttons.py": (17,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/print_control.py": (6,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here. Lowered 7 -> 6 in #1587: the status fallback's broad handler became a narrow connection-error catch that re-raises HTTP and other errors."),
    "print_handy/runtime/print_next.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/print_records.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/print_stop.py": (7,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/prusa_watch.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/purchase_intake.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/roast_chat.py": (3,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/spool_codes.py": (1,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/spool_sync.py": (32,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/starter_pings.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/stock_watch.py": (4,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/stop_capture.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/timelapse_capture.py": (2,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/timelapse_encode.py": (8,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/runtime/visual_watch.py": (6,
        "Pre-existing broad no-log handlers in runtime watchers and listeners, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/setup_state.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/setup_wizard.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/config.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/controls.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/data.py": (6,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/instance.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/chat_handlers.py": (2,
        "Pre-existing broad no-log handlers moved unchanged out of webui/server.py in the #1603 item 3 handler split; the allowance followed the code from server.py (whose count dropped by the same total), so the overall frozen #1467 total did not grow."),
    "print_handy/webui/server.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here. Six of the original seven moved with their handlers to webui/sessions.py, webui/status_handlers.py and webui/chat_handlers.py in the #1603 item 3 split, and their allowances moved with them."),
    "print_handy/webui/sessions.py": (2,
        "Pre-existing broad no-log handlers moved unchanged out of webui/server.py in the #1603 item 3 handler split; the allowance followed the code from server.py (whose count dropped by the same total), so the overall frozen #1467 total did not grow."),
    "print_handy/webui/status_handlers.py": (2,
        "Pre-existing broad no-log handlers moved unchanged out of webui/server.py in the #1603 item 3 handler split; the allowance followed the code from server.py (whose count dropped by the same total), so the overall frozen #1467 total did not grow."),
    "print_handy/webui/share_export.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/shell.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/spools.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/status.py": (5,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/upload.py": (1,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/walkthrough.py": (6,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui/wrapped_video.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
    "print_handy/webui_gate.py": (2,
        "Pre-existing broad no-log handlers in shipped code, frozen at the #1467 count so a new one fails CI; burning them down beyond the #1467 decision paths is future work, not done here."),
}


def _sources():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"), str(path))


def _is_silent(handler: ast.ExceptHandler) -> bool:
    body = handler.body
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    # `...` or a lone string: a comment dressed as a statement, still a swallow.
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
        and (stmt.value.value is Ellipsis or isinstance(stmt.value.value, str))


def _suppress_calls(tree) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "suppress"
                and isinstance(func.value, ast.Name) and func.value.id == "contextlib") \
                or (isinstance(func, ast.Name) and func.id == "suppress"):
            count += 1
    return count


# Call names that report a failure with a level: logger-style methods
# (``logger.warning(...)``, ``log.exception(...)``, ``self._log.error(...)``)
# and ``print`` for the CLI paths that report to the operator that way.
_LOG_METHODS = frozenset({"critical", "debug", "error", "exception", "info", "log", "warn", "warning"})


def _is_broad(handler: ast.ExceptHandler) -> bool:
    """Catches ``Exception`` or is bare — the broad shapes #1467 counts.

    A tuple catch or a ``BaseException`` catch is a different, deliberate
    shape and is not part of the #1467 count this guard freezes.
    """
    return handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception")


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_same_scope(node):
    """``ast.walk`` that does not enter nested functions, lambdas or classes.

    A log call or a raise inside a function the handler only defines does
    not run when the handler does, so it must not count as the handler
    reporting the failure (review of #1479)."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        yield from _walk_same_scope(child)


def _is_broad_no_log(handler: ast.ExceptHandler) -> bool:
    """A broad handler that discards the failure: no log, no raise, no exception name.

    This is the #1467 counting definition, and it reproduces that issue's AST
    count (448 on ``main`` at 623aa6bb in ``print_handy/``): a handler that
    binds the exception (``except Exception as exc``) counts as referencing
    it, and a narrow typed catch never counts at all.
    """
    if not _is_broad(handler) or handler.name is not None:
        return False
    for stmt in handler.body:
        for node in _walk_same_scope(stmt):
            if isinstance(node, ast.Raise):
                return False
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
                    return False
                if isinstance(func, ast.Name) and func.id == "print":
                    return False
    return True


def _broad_no_log_count(tree) -> int:
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.ExceptHandler) and _is_broad_no_log(node))


def test_no_silent_exception_handler_anywhere():
    silent = [f"{name}:{node.lineno}"
              for name, tree in _sources()
              for node in ast.walk(tree)
              if isinstance(node, ast.ExceptHandler) and _is_silent(node)]
    assert silent == [], (
        "silent exception handlers (prevent the error, recover narrowly, or log the "
        f"level and cause; see #1417): {silent}")


def test_contextlib_suppress_never_grows():
    found = {name: n for name, tree in _sources() if (n := _suppress_calls(tree))}
    grown = {name: (n, SUPPRESS_ALLOWANCE.get(name, 0)) for name, n in found.items()
             if n > SUPPRESS_ALLOWANCE.get(name, 0)}
    assert grown == {}, (
        f"new contextlib.suppress calls (found, allowed): {grown}. suppress() swallows "
        "silently like `except: pass`; handle or log the failure instead (#1417).")
    stale = {name: (found.get(name, 0), allowed) for name, allowed in SUPPRESS_ALLOWANCE.items()
             if found.get(name, 0) < allowed}
    assert stale == {}, (
        f"contextlib.suppress allowance is higher than the code (found, allowed): {stale}. "
        "Lower SUPPRESS_ALLOWANCE in this file so the removed calls cannot come back.")


def test_broad_no_log_handlers_never_grow():
    found = {name: n for name, tree in _sources() if (n := _broad_no_log_count(tree))}
    allowed = {name: count for name, (count, _reason) in BROAD_NO_LOG_ALLOWANCE.items()}
    grown = {name: (n, allowed.get(name, 0)) for name, n in found.items()
             if n > allowed.get(name, 0)}
    assert grown == {}, (
        f"new broad no-log exception handlers (found, allowed): {grown}. A handler that "
        "catches Exception (or is bare) and returns a default without logging the cause "
        "is the #1417 swallow with one more token; log the cause at a level, catch only "
        "the errors meant, or re-raise (#1467).")
    stale = {name: (found.get(name, 0), count) for name, count in allowed.items()
             if found.get(name, 0) < count}
    assert stale == {}, (
        f"broad no-log allowance is higher than the code (found, allowed): {stale}. "
        "Lower BROAD_NO_LOG_ALLOWANCE in this file so the removed handlers cannot come back.")
    reasonless = [name for name, (count, reason) in BROAD_NO_LOG_ALLOWANCE.items()
                  if count <= 0 or not str(reason).strip()]
    assert reasonless == [], (
        f"broad no-log allowance entries with no written reason (or a non-positive count): "
        f"{reasonless}. Every remaining entry needs its reason, per #1467.")


def test_contextlib_suppress_allowance_is_reasoned():
    """#1455's done state: the allowance is empty, or every entry in it has a written reason."""
    unreasoned = {name for name in SUPPRESS_ALLOWANCE if not SUPPRESS_REASONS.get(name, "").strip()}
    assert unreasoned == set(), (
        f"suppress allowance entries without a written reason (#1455): {unreasoned}. "
        "Remove the calls, or record why they may stay in SUPPRESS_REASONS.")
    orphaned = set(SUPPRESS_REASONS) - set(SUPPRESS_ALLOWANCE)
    assert orphaned == set(), (
        f"suppress reasons for files with no allowance left: {orphaned}. "
        "Drop the reason together with the allowance entry.")


def test_the_guard_recognizes_every_silent_shape():
    src = (
        "try:\n    a()\nexcept OSError:\n    pass\n"
        "try:\n    a()\nexcept OSError:\n    ...\n"
        "try:\n    a()\nexcept OSError:\n    'ignored'\n"
        "try:\n    a()\nexcept OSError:\n    log()\n"
        "import contextlib\nwith contextlib.suppress(OSError):\n    a()\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert [_is_silent(h) for h in handlers] == [True, True, True, False]
    assert _suppress_calls(tree) == 1


def test_the_guard_recognizes_every_broad_no_log_shape():
    src = (
        # Broad, no log, no raise, no name: counted.
        "try:\n    a()\nexcept Exception:\n    return None\n"
        "try:\n    a()\nexcept:\n    return {}\n"
        # Binds the exception: counts as referencing it, not counted.
        "try:\n    a()\nexcept Exception as exc:\n    return None\n"
        # Logs the cause: not counted.
        "try:\n    a()\nexcept Exception:\n    logger.warning('failed')\n    return None\n"
        # Re-raises: not counted.
        "try:\n    a()\nexcept Exception:\n    raise\n"
        # Narrow typed catch returning a default: a parse, never counted.
        "try:\n    a()\nexcept ValueError:\n    return None\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert [_is_broad_no_log(h) for h in handlers] == [True, True, False, False, False, False]
    assert _broad_no_log_count(tree) == 2
