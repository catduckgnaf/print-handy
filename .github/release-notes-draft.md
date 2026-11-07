# Print Handy 2.2.6 — your year in plastic, plus a bot that fixes its own homework

This one is a double feature. Print Handy Wrapped lands: a Data tab that turns your print history into charts, and a once-a-year recap of everything you printed — the triumphs, the spaghetti, the 2 a.m. decisions. And when a slice goes wrong, the bot can now look at the failure and offer to fix the model for your printer instead of just delivering the bad news. One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.2.6, also tagged latest.

## What you can do

### See your year in prints: Wrapped and the Data tab

Turn on `timelapse.wrapped_enabled` and the Web UI grows a Data tab: read-only statistics over your print records — hours printed, filament used, failures, and breakdowns by printer, material, color, vendor, spool and nozzle — plus Print Handy Wrapped, the once-a-year recap built from the same records. Flip on `timelapse.wrapped_ai_video` as well and the Wrapped page can compile a local video of the year (stat cards plus your timelapses) when you ask for it; it is generated on your machine and never sent anywhere. Both switches are off by default, and while they are off the pages are not in the navigation and their routes refuse by name. (#958, building on #956)

### "Fix this for my printer"

When a slice fails, a slice is refused, or a post-slice gate fails, intake now offers an AI fix next to the existing choices — and an uploaded file followed by "fix this", "repair this" or "make this printable" starts the same flow. The fix proposes changes from the edit vocabulary, a bounded geometry repair, and/or a mesh repair (welded vertices, filled holes, fixed winding — with designed openings listed and kept open when you say so). Every proposal is shown first, applying it works on a copy (your source file is never overwritten), and the fixed job re-slices through the full intake so every gate runs again. A fix is never a waiver: nothing is recorded as Print anyway, and starting the print still needs the normal approvals. With no AI configured, the fix simply is not offered and intake behaves as before. A bare G-code file sliced for a different printer is refused plainly, with a request for the source model — G-code cannot be safely retargeted and is never rewritten. (#980, hardened by #987)

### Yolo mode, for files you vouch for

Say `yolo` with a file and Print Handy prints it without the usual questions — including a sliced `.gcode` or `.gcode.3mf` drop, which prints as sent. The checks still run and are reported; they just do not hold the print hostage. This is deliberately loud, deliberately named, and entirely your call: the file is treated as operator-vouched. (#965)

### Honest support gates, no more dead ends

The post-slice support gates grew up. Coverage is now graded on the underside footprint a region actually occupies, per copy and per plate (#966), validated against fixtures reproducing the documented intake scenarios (#974). When a check cannot run at all — a part that matches no object, an unreadable plate file — intake says so and asks Print anyway / Cancel instead of leaving you at a dead end (#976), and the with-supports path got the same real Print anyway choice instead of a loop back to "choose supports" (#964).

### The bot understands more typos

The fuzzy vocabulary learned a few hundred more of the ways humans actually type: 54 more misspellings mined from real chat history and everyday slips (#955), then 441 more in phase 3 (#992) — including the one everyone was waiting for: spegatti now finds the spaghetti. Fuzzy still only ever suggests; it never answers by itself.

### New printer on the roster: Bambu Lab H3C (initial support)

The Bambu Lab H3C joins with initial support: verified machine facts (330×320×325 bed, 9 tools, 0.2–0.8 nozzles), its 32X error-code prefix wired into the HMS table, and docs. It is deliberately not registered for slicing yet — the bundled Orca build carries no H3C profiles, and support that cannot slice is not support. That lands when Orca does. (#994)

### Stay signed in

The Web UI's "Keep me signed in" now means it: remembered sessions last 30 days and survive restarts, so the dashboard stops asking for your token every time the container breathes. (#953)

### Fresh paint

The icon set is black everywhere now — one consistent set across the app, the docs and the bot surfaces. (#957)

## Fixes

- "Print it again" now reprints the model that actually printed last. It used to resolve to whatever link was last shared in the channel, or whichever archived job was touched most recently — including jobs that were prepared but never printed. Reprints now resolve from launch records, scoped to the channel's printer, and a follow-up-born intake asks "Print {title}?" first. (#988, fixing #979)
- Print-now starts now reconcile the intake receipt: a confirmed start marks the receipt started instead of leaving it pending, and an accepted-but-unconfirmed start is marked pending rather than lost. (#975)
- The container image now sets `PRINT_HANDY_ORCA_CMD` itself, so `docker exec` sessions and anything spawned outside the entrypoint find the bundled slicer without being told. (#973)
- Container-only tests are marked as such, so a local test run outside the image shows honest skips instead of failures. (#977)
- The upstream bump bot got better at explaining itself: identity failures are diagnosed, secret shapes are validated before use, and it authenticates with the app's client ID. (#991)

## Upgrade and compatibility

- Nothing to rename, move or re-enter. Wrapped and its video are off by default; yolo changes nothing unless you say the word.
- Bundled components moved: Spoolman 0.27.1 (#989) and Bambu Studio 02.09.01.70 (#990).
- The 2.2.5 digest pin in the docs and the in-repo Unraid template was refreshed as part of this range (#954), and the repo root finished its tidy-up: registry, knowledge and firmware data now live inside the package (#962), Python packaging is consolidated in `pyproject.toml` (#961), the stray root files moved out (#959), and the in-repo Unraid template ships with empty ExtraParams — the hardening lives in Compose and the image, where it belongs (#960).

## Technical details and contributors

- Report-only Codecov patch coverage on the offline test legs; project coverage stays off on purpose, because a single percentage would misstate a suite that also drives real slicers and corpus fixtures (#967).
- Release engineering: this release ships with an automated release check (version surfaces, ancestry-based notes coverage, full-notes spelling scan, parity suites) and a written release checklist built from an audit of every past release's catches and misses — because the first draft of these notes was missing most of this list, again, and now a machine says so. (#1006) The slicing-speed baselines were re-recorded for this release in their own PR before tagging, with one heavy case deliberately recorded from a representative runner after the release run landed on the fast runner group twice. (#1007)

## Verification and known limitations

- Offline test suite and CI on the release head; the slicing-speed baselines were re-recorded from a green CI run on main before tagging (one heavy case recorded from a representative runner, documented in its baseline entry).
- No live printer, live chat or Docker-on-this-machine pass was run for this release — there is no Docker daemon or printer reachable from the build machine, and offline results are not live proof. The owed live checks are listed on the release checklist issue (#1005).
- Uranium (the maintainer's Unraid host) pulls the new image at the machine, as with past releases.
