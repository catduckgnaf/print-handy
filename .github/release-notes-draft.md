# Print Handy 2.3.1: the bot actually watched a print this time

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.1, also tagged latest.

2.3.0 rebuilt the engine room. Then a real two-color print went through it, and the bot left the start message unedited, forgot the bronze slot, asked about supports for a gap thinner than one layer, and offered the same broken AI fix twice. 2.3.1 is what we fixed after watching that print and reading a week of the print channel. If you run Print Handy in Docker, upgrading is just pulling the new image. If you run it natively with systemd, read "Upgrade and compatibility".

## What you can do

### Multicolor prints get charged, every spool
- A print that uses more than one slot now charges each slot's spool in Spoolman when it ends, from the slicer's own per-tool grams, to the spool that was in the slot at launch. A cancelled print is charged for what it actually used. A restart or a repeated end event can't double charge, and a Spoolman outage retries later. Before this, multicolor prints charged nothing at all. The finish post now ends with what each spool was charged. (#1362)
- A print that finished while the bot was down is posted once ("finished at 9:44 while the bot was offline") and charged once. (#1359) (#1362)
- Earlier multicolor prints are not backfilled. If you print in more than one color, check the remaining weight of those spools in Spoolman once.

### Starts that tell the truth
- The start post lists every slot a multicolor print uses, with each slot's grams, instead of only the first part's slot. (#1353)
- Not enough filament on any slot the print uses is now a warning you accept or cancel before anything is uploaded, on every chat platform, never a hard block. A spool Spoolman says is at 0 g says so, in case its weight was never set. (#1354)
- "Uploaded to the printer... This is a transfer, not a launch" is gone. The start notes say what is true at that moment, and the "printer accepted the start" note no longer shows up a second before "Print started". (#1366)
- Uploaded files no longer get "the model page gives no estimate" when there is no model page. (#1366)

### Fewer questions you didn't need
- A gap under the part thinner than the first layer is contact, not a low-clearance underside: no supports question, no risk prompt after slicing, no extend or sink offer. A job's own first layer height edit counts. (#1355)
- When supports are not recommended, "without supports" is the first choice instead of the second. (#1366)
- AI fix can now extend a sloped underside down to the plate (it used to "move nothing" on any slope), and a fix that failed is not offered again for that model. (#1356)
- Reorient on a model that already lies flat says so and changes nothing. Turning or resizing a multicolor model keeps it on one multicolor plate instead of quietly printing each color on its own plate. (#1367)
- The same clearance warning shows once with a count, not ten times; thermal debug lines show only for materials that need an enclosure; part grams follow what each part actually extrudes. (#1366)

### Pause, resume and stop that believe the printer
- Resume and Stop no longer report failure when the printer simply took longer than 15 seconds to answer. Print Handy watches the printer for up to 90 seconds and reports what it actually did, in chat and in the Web UI. (#1364)
- Asking to stop in the first minutes, while a start is still in flight, offers to stop it as soon as it starts instead of saying there is nothing to stop. (#1364)
- A runout that pauses again right after you resume (filament not at the toolhead yet) updates the original alert instead of reposting everything and asking about the spool again. (#1363)
- A runout spool question left over from an earlier print closes itself when the same spool is back in the slot, and stops reminding you every 10 minutes once its print is over. While it stayed open, filament usage was paused for every spool. (#1357)

### Camera watch and restarts
- No more "this file has no layer numbers" on files that do. Camera alerts use plain words instead of internal codes, and a low-confidence result posts without pinging anyone. (#1365)
- A listener that restarts mid-print continues the same timelapse, posts its "listener is down" notice once with the print's name, and does not re-run first-layer checks at layer 211. (#1361)

## Fixes
- 2.3.0 in Docker: the Moonraker listener's helpers looked themselves up in the wrong module, so the start message edit, camera watch notices and spool reminders failed and the printer connection could drop. (#1351)
- `python -m print_handy.hermes.<module>` runs again, as 2.3.0 promised for this release, with a warning to update. Listener and chat handler logs carry their own names, so per-module log levels and debug mode reach them. (#1358)
- The Docker image runs the real package from any working directory, and the detached print monitor uses its own printer's chat. (#1360)
- The test harness no longer recompiles every chat handler each time it loads the plugin, which was the whole 2.3.0 timing regression (#1349); the 2.3.0 timing baselines from an unusually fast runner were reverted. (#1352) (#1350)
- docs/DOCKER.md pins the 2.3.0 image digest and documents rolling back to 2.2.9. (#1348)
- Release housekeeping: two reorient tests that looked menus up by position were corrected after the supports menu order changed, and the slicing-speed baselines were re-recorded from the release code's CI run on the usual runner class. (#1370) (#1371)

## Upgrade and compatibility
- **Native installs with systemd:** if your listener unit still runs `print_handy.hermes.moonraker_event_listener`, it works again in 2.3.1, but re-run `scripts/install-standalone.sh` to move it to `print_handy.runtime`. The old path goes away after 2.3.x.
- **Log names changed:** listener lines log as `print_handy.runtime.moonraker_event_listener` (and `family_event_listener`) instead of `__main__`, and chat handlers as `print_handy.bot.handlers` instead of `print_handy_print_feedback`. Update any log filter that matched the old names.
- **Spoolman weights:** multicolor prints before 2.3.1 were never charged. Correct those spools once.
- Still using `[paths] hermes_home`? It works through 2.3.x with a warning; rename it to `state_home`.

## Not verified on a printer yet
Everything above is covered by offline tests and CI, including the real-slicer jobs. Not yet seen on hardware: multicolor charging through a real print, the runout question closing on its own, the 90-second resume and stop readback, and the reoriented multicolor plate through a real print.
