"""Post-reboot printer clock watch (#807): warn while a rebooted printer's
clock is still wrong, and say when it catches up.

A ZMod printer (the AD5X that prompted this) has no clock that survives a
power cycle: it comes back at 31 Dec 2025 and only recovers when its NTP
clients get an answer, which can take a long time. While the clock is
wrong, Moonraker history is stamped with wrong dates, the printer's own
TLS connections can fail with "certificate not yet valid", and Print
Handy can fail to match a finished job to its launch.

This watch is deliberately read-only. It never sets the printer's clock
and never edits printer files; it compares the printer's clock with the
host's, posts one "printer clock has not synced yet" message when a
far-off clock is first seen, re-checks on the listener's poll cadence
until the clock catches up, posts one reminder if it stays wrong, and
posts one "printer clock synced" message when it recovers. Nothing here
blocks a print: a wrong clock is a warning, never a refusal.

The skew is read from the HTTP ``Date`` header Moonraker already sends
on ``/server/info``, so no extra credential, port or printer-side change
is needed. An unreadable clock is unknown — never treated as synced and
never treated as wrong.
"""

import time
import urllib.request
from email.utils import parsedate_to_datetime

# A printer clock this far from the host's is "off": ordinary drift is
# seconds, a rebooted ZMod clock is months. The same 5-minute threshold
# launch_state (PR #804, on main) uses for its warning and idle sync.
CLOCK_SKEW_WARN_S = 300.0

# How often the listener re-checks while it polls, and how long a clock
# may stay wrong before the single reminder post.
CHECK_INTERVAL_S = 60.0
REMIND_AFTER_S = 1800.0


def read_clock_skew(moonraker_url, timeout=6, now=None):
    """Seconds the host clock is ahead of the printer's, or None when unknown.

    Positive means the printer is behind real time. ``now`` is injectable
    for tests and defaults to the host wall clock.
    """
    base = str(moonraker_url or "").rstrip("/")
    if not base:
        return None
    try:
        req = urllib.request.Request(base + "/server/info", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            stamp = resp.headers.get("Date")
        if not stamp:
            return None
        host_now = (now() if now is not None else time.time())
        return host_now - parsedate_to_datetime(stamp).timestamp()
    except Exception:
        return None


def describe_skew(skew):
    """A short operator-facing size and direction for a skew in seconds."""
    seconds = abs(float(skew))
    if seconds >= 86400:
        size = f"about {seconds / 86400:.0f} day(s)"
    elif seconds >= 3600:
        size = f"about {seconds / 3600:.0f} hour(s)"
    else:
        size = f"about {max(1, round(seconds / 60))} minute(s)"
    side = "behind" if skew > 0 else "ahead of"
    return f"{size} {side} real time"


def _describe_duration(seconds):
    seconds = max(0.0, float(seconds))
    if seconds >= 3600:
        return f"about {seconds / 3600:.0f} hour(s)"
    return f"about {max(1, round(seconds / 60))} minute(s)"


def not_synced_message(skew):
    return (
        "**Printer clock has not synced yet:** the printer's clock is "
        f"{describe_skew(skew)}. This is what a printer that rebooted and is still "
        "waiting on its time sync looks like. Print Handy will keep checking and "
        "will say when it catches up. Prints are not blocked, but until it syncs: "
        "job-history dates and \"started at\" times will be wrong, and the "
        "printer's own update checks can fail (its certificates can look not yet "
        "valid). If it does not catch up, check the printer can reach an NTP "
        "server (outbound UDP port 123)."
    )


def still_wrong_message(skew, elapsed_s):
    return (
        "**Printer clock still has not synced:** the printer's clock is "
        f"{describe_skew(skew)}, and it has been wrong for {_describe_duration(elapsed_s)}. "
        "Print Handy is still checking and will say when it catches up. If it "
        "stays wrong, check the printer can reach an NTP server (outbound UDP "
        "port 123) and reboot the printer."
    )


def synced_message(elapsed_s):
    return (
        "**Printer clock synced:** the printer's clock has caught up "
        f"after {_describe_duration(elapsed_s)}. Times the printer recorded "
        "while its clock was wrong can still be off."
    )


class PrinterClockWatch:
    """One wrong-clock episode at a time: announce, remind once, announce recovery.

    ``skew_reader`` returns seconds the host is ahead of the printer, or
    None when the clock cannot be read; a reader that raises is treated
    the same as returning None. ``clock`` is a monotonic-style seconds
    callable, injected in tests. ``poll()`` returns the messages to post
    now (usually none).
    """

    def __init__(self, skew_reader, clock=time.monotonic):
        self.skew_reader = skew_reader
        self.clock = clock
        self._wrong_since = None
        self._reminded = False

    def poll(self):
        try:
            skew = self.skew_reader()
        except Exception:
            skew = None
        if skew is None:
            # Unknown is not synced and not wrong: hold the episode open
            # so a printer that drops off the network mid-episode does
            # not get a false "synced" when it answers again in range,
            # and a silent printer never earns a warning either.
            return []
        now = self.clock()
        if abs(skew) < CLOCK_SKEW_WARN_S:
            if self._wrong_since is None:
                return []
            elapsed = now - self._wrong_since
            self._wrong_since = None
            self._reminded = False
            return [synced_message(elapsed)]
        if self._wrong_since is None:
            self._wrong_since = now
            self._reminded = False
            return [not_synced_message(skew)]
        if not self._reminded and now - self._wrong_since >= REMIND_AFTER_S:
            self._reminded = True
            return [still_wrong_message(skew, now - self._wrong_since)]
        return []
