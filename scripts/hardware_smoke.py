#!/usr/bin/env python3
"""Pre-release hardware smoke check: documentation stub. It performs no printer I/O.

tests/replay/ replays recorded AD5X responses, so it proves the start path against what the printer
said on the day it was recorded, not against the printer in front of you. Before a release that touches
the start, material or launch-lease code, an operator runs this check by hand on an IDLE printer with the
bed clear and filament loaded. It is opt-in, never part of CI and never run by an agent.

Procedure (idle printer only; stop at the first surprise):

1. Read-only baseline. `printer_state check` reports no mismatch; Moonraker `print_stats.state` is
   `standby` and both heater targets are 0; every slot the job uses is loaded.
2. Start. Print a tiny, known-good single-slot job (a 20 mm calibration cube is enough) from chat or
   with `model_job print-now <JOB.json> --bed-clear-confirmed`. Watch for exactly one upload and one start, and a start card
   that reads STARTED or START_PENDING, never "Didn't start". Note whether the start request is held
   open past the Moonraker timeout (#616); that is expected on a screenless ZMod printer.
3. Confirm. The printer's file name appears in print_stats (ZMod may report 3mf_print/<name>.gcode
   for a .gcode.3mf upload, #691) and the launch lease settles without a second start.
4. Cancel. Cancel from the printer or chat while it is still heating, confirm both heater targets
   return to 0 and the printer reports standby or cancelled, then check `model_job reconcile-launch`
   has nothing pending.
5. Record the printer firmware, ZMod version, Print Handy version, date and result in the release
   notes; if the printer's responses changed shape, re-record tests/fixtures/printers/ad5x_zmod/
   (read-only GETs only; see the README there).

Nothing in this file talks to a printer. Wire it up only with an explicit operator opt-in and a design
review, since a script that starts and cancels a print is a script that can start a print.
"""
import sys


def main():
    print(__doc__)
    print("This is a documentation stub; it does nothing. Follow the procedure by hand.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
