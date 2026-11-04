"""Corroborated native idle gate for explicitly mapped ZMod launches.
Not a replacement for the legacy filename-only start's slot routing checks.
"""


def _zero(value):
    if isinstance(value, bool):
        return False
    try:
        return float(value) == 0
    except (TypeError, ValueError, OverflowError):
        return False


def _native_zero(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and value == 0)


# Klipper keeps print_stats.state at the finished job's outcome (complete or
# cancelled) until the next print starts; heaters are checked separately, so
# those count as idle. "error" stays refused: it needs a human first.
IDLE_PRINT_STATES = frozenset({"standby", "complete", "cancelled"})


def _idle_failures(moonraker, native):
    """Return failed idle predicates and observed values, failing closed on malformed data."""
    from ....core.clear_job import IDLE_NATIVE

    moonraker = moonraker if isinstance(moonraker, dict) else {}
    native = native if isinstance(native, dict) else {}
    print_stats = moonraker.get("print")
    extruder = moonraker.get("extruder")
    bed = moonraker.get("bed")
    checks = (
        ("Moonraker webhooks.state == 'ready'", moonraker.get("state"), lambda value: value == "ready"),
        ("Moonraker print.state is standby/complete/cancelled", print_stats.get("state") if isinstance(print_stats, dict) else None,
         lambda value: value in IDLE_PRINT_STATES),
        ("Moonraker extruder.target == 0", extruder.get("target") if isinstance(extruder, dict) else None, _zero),
        ("Moonraker bed.target == 0", bed.get("target") if isinstance(bed, dict) else None, _zero),
        ("native status is ready/cancel/cancelled", native.get("status"), lambda value: value in IDLE_NATIVE),
        ("native leftTargetTemp == 0", native.get("leftTargetTemp"), _zero),
        ("native rightTargetTemp == 0", native.get("rightTargetTemp"), _zero),
        ("native platTargetTemp == 0", native.get("platTargetTemp"), _zero),
        ("native chamberTargetTemp == 0", native.get("chamberTargetTemp"), _zero),
        ("native errorCode == ''", native.get("errorCode"), lambda value: value == ""),
        ("native printProgress == 0", native.get("printProgress"), _native_zero),
        ("native printDuration == 0", native.get("printDuration"), _native_zero),
    )
    failures = []
    for label, observed, predicate in checks:
        try:
            passed = predicate(observed)
        except (TypeError, ValueError, OverflowError):
            passed = False
        if not passed:
            failures.append(f"{label} (observed {observed!r})")
    return failures


def require_idle(moonraker, native):
    failures = _idle_failures(moonraker, native)
    if failures:
        raise RuntimeError("REFUSED: idle guard failed: " + "; ".join(failures))
    return True


_SETTLEABLE = ("native status is ready/cancel/cancelled", "native printProgress == 0",
               "native printDuration == 0")


def require_idle_screenless(read_moonraker):
    """Idle gate for screenless ZMod (display_off=1), where the stock firmware is not running.

    The native predicates have no source there (its /detail API refuses), so
    every Moonraker predicate must pass: webhooks ready, print idle, both
    heater targets 0. Nothing is waited out: a screenless cancel leaves no
    native job residue to settle.
    """
    moonraker = read_moonraker()
    failures = [f for f in _idle_failures(moonraker, {}) if f.startswith("Moonraker")]
    if failures:
        raise RuntimeError("REFUSED: idle guard failed: " + "; ".join(failures))
    return True


def require_idle_settled(read_moonraker, read_native, timeout_s=90, interval_s=5,
                         sleep=None, clock=None):
    """Wait briefly for native firmware to settle after cancel; all predicates remain mandatory."""
    import time
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout_s
    while True:
        moonraker, native = read_moonraker(), read_native()
        m = moonraker if isinstance(moonraker, dict) else {}
        n = native if isinstance(native, dict) else {}
        if m.get('state') != 'ready' or (isinstance(m.get('print'), dict) and m['print'].get('state') not in IDLE_PRINT_STATES):
            raise RuntimeError("REFUSED: idle guard failed: Moonraker reports active execution")
        if n.get('errorCode') not in ('', None):
            raise RuntimeError("REFUSED: idle guard failed: native errorCode is non-empty")
        failures = _idle_failures(m, n)
        if not failures:
            return True
        # Only post-cancel native job residue is worth waiting out. Heater targets or any
        # Moonraker-side failure are real conditions: refuse at once with the full detail.
        if not all(f.startswith(_SETTLEABLE) for f in failures):
            raise RuntimeError("REFUSED: idle guard failed: " + "; ".join(failures))
        if clock() >= deadline:
            status = n.get('status', 'unknown')
            # A clean idle refusal like every other raise here (P1-42): the
            # launch path only converts REFUSED-prefixed messages into a
            # clean pre-upload refusal, and print-next releases the plate
            # claim only for those — an unprefixed timeout stranded the
            # claim even though nothing was sent to the printer.
            raise RuntimeError("REFUSED: idle guard failed: "
                               f"Printer screen still reports {status} while Moonraker is idle (usually lingering after a cancel). Nothing was sent to the printer. Wait a minute or clear the job on the printer screen, then ask again. Detail: " + "; ".join(failures))
        sleep(min(interval_s, max(0, deadline - clock())))
