# Print Handy 2.3.3: just @ it

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.3.3, also tagged latest.

2.3.2 printed what the designer drew, then took 26 seconds to admit the lantern was ready, yelled at a healthy print over a 12% hunch, and told the owner "I didn't understand that" when he pasted a link to his own message. Twice. 2.3.3 is faster, calmer, and finally answers when you call it by name.

## What you can do

### Talk to the bot by name
- Mention the bot instead of typing the slash: "@Print Handy spool list", "@Print Handy /spool list" or "@Print Handy stop" run the command on Discord, Telegram, Matrix and web chat. Punctuation after the mention is fine ("@Print Handy, spool list"), and a bare mention gets the help. (#1394)
- The bot learns its own name from its account at run time: Discord mention, name and nickname; Telegram username and display name; Matrix pill, display name and localpart; plus `bot_name` if you set one. Rename the account to "Shop Bot" and it answers to "@Shop Bot" and replies as "Shop Bot". (#1394)
- On Telegram, a command for another bot in a shared group (`/status@SomeOtherBot`) is no longer run here. (#1394)

### Re-run an earlier message
- On Discord, paste the link to an earlier message in the print channel and the bot runs that message's model link or file again, as you. (#1391)

### Keep printing while a design runs
- A model link or file pasted while an AI design is working, waiting at "What next for this design?", or waiting for a change now starts a normal print intake instead of being swallowed. "print 57" works mid-design, and another person's design runs alongside yours. (#1398)

### Ready sooner on big jobs
- A large job reaches its Ready question about 11 seconds sooner: on a 25 MB, 1.16 million line G-code the passes after OrcaSlicer take 3.6 s instead of about 15 s, with the same verdicts. The bed check runs in Rust now, with the Python version kept as the fallback. (#1393, #1396)

### Home Assistant gets its fields
- The server now reports `temperature_limits_c`, `controls_enabled`, a persistent random `instance_id`, and `GET /api/printers` (config and state only) for the ha-print-handy integration. (#1401)

## Fixes
- New guide: install Print Handy on Home Assistant OS as an app from **Settings > Apps > App store**, and it updates with each release. (#1405)
- A finished print with a numbered timelapse posts "Print complete" once. On 2.3.2 the same completion could post a second time a few minutes later. (#1406)
- Release housekeeping, no change to what Print Handy does: the version bump (#1403), a test that leaked a background thread into later tests (#1407), and release-notes corrections (#1408, #1410, #1411).
- A single weak camera detection (one box at 12% on a healthy print) no longer posts an alert. A lone weak detection is logged; two in a row post one plainly worded, low-confidence alert with no ping; three make it a failure, as two did before. Strong detections behave exactly as before, on Moonraker and Bambu Lab alike. (#1395)
- A stall alert keeps its starter ping even when a weak detection shows up on the same frame. (#1395)
- Open questions (design, colors, Stop buttons, approvals) no longer hold the bot's worker threads, so a few of them can't stall a print start on a small container anymore. (#1398)
- The ZMod finalize no longer trusts a file that only looks finished: a hand-edited G-code with Print Handy's map lines now gets the full heater, material and used-tools checks. The full pass is fast enough that skipping it bought nothing. (#1396)
- A mention followed by a newline ("@Print Handy design" then the description on the next line) runs `/design`. The help stays one Discord message for a `bot_name` up to 32 characters. (#1394)

## Upgrade and compatibility
- **Mentions are commands now.** A message that starts by mentioning the bot is routed as a command for it. A message that mentions nobody routes exactly as before. (#1394)
- **Telegram privacy mode.** With BotFather's privacy mode on (the default) a group only delivers commands and replies, so there use `/spool@YourBot list` or reply to the bot. A plain "@YourBot spool list" needs privacy mode off or the bot as a group admin. A private chat always works. (#1394)
- **Weak detections stay quiet.** If you relied on every weak camera detection posting an alert, you now get one only after two in a row. (#1395)
- **Claim the Web UI at setup.** The install guides now make claiming the Web UI a setup step; nothing changes for an install that already claimed it. (#1401)
- No setting was added, renamed or removed.

## Technical details and contributors
- A performance regression ledger and procedure (`docs/history/PERFORMANCE-REGRESSIONS.md`): measure the real job, profile a copy, fix the work instead of the threshold, add a work-counting test. (#1392, #1393, #1396)
- `tests/hermes/` is now `tests/runtime/`. The 2.3.x compatibility shims (`print_handy.hermes` and friends) are scheduled for removal in 2.4.0, and a test fails at 2.4.0 if any remain. (#1401)
- `config/docker.yaml` and `config/docker.toml` are held to identical settings by a test; issue text is spell-checked like PR text. (#1401)
- The 2.3.2 image digest is pinned in docs/DOCKER.md. (#1390)
- Slicing-speed baselines re-recorded from the release commit's CI run.

## Verification and known limitations
- Offline suites and CI on every merged head.
- Not seen live yet: a mention on Telegram and Matrix, a weak-detection streak on a real print, a print started during a running design, the Home Assistant integration reading the new fields, and a single "Print complete" for a finished print with a numbered timelapse.
