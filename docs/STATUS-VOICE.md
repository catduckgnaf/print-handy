# Print Handy status voice

Print Handy now ships 1,000 locally selected, stage-specific sarcastic status lines,
organized as **25 banks of 40**. They require **no LLM**, no external service, and no
printer connection to choose. The original factual status message remains the
authoritative account of what Print Handy is doing.

## Stage map

A quip is added only where Print Handy already sends a neutral progress or
status line for that stage, as one trailing sentence after the unchanged
factual text. When the line ends with a Stop instruction, the quip goes just
before it, so the instruction stays last. Every quip carries a stable event
key (the intake id and stage, or the print and milestone), so a re-render,
an edited status line, or a retry shows the same line. If the voice fails or
returns nothing, the message is byte-identical to the one without the
feature. No stage adds a message of its own, except the quantity line below,
which is sent only when there is a quip to send.

| Stage | Status | Where, or why not |
| :--- | :--- | :--- |
| `link_intake` | wired | `prepared_intake.send_preview`: the "That looks like ..." preview after a link lookup (not for uploads) |
| `file_download` | unwired | the download runs without any progress message, so there is no line to carry a quip |
| `file_validation` | unwired | the source checks only speak when they refuse a file, and refusals never carry a quip |
| `model_audit` | wired | `prepared_intake._prepare_link`: the large-model notice shown while the model check runs |
| `geometry_inspection` | unwired | the clearance and geometry analysis posts nothing while it runs; its findings belong to the support decision |
| `mesh_repair` | wired | `prepared_intake._prepare_link`: the "Extended underside ..." and "Flattened model to plate ..." notices after the chosen fix |
| `color_detection` | wired | `prepared_intake._prepare_link`: "Checking the model.", only when the color manifest has to be read |
| `spool_matching` | unwired | the spool projection is only shown inside the Ready to slice approval question |
| `bed_fit` | wired | `prepared_intake.FitStatus.run`, phase "trying rotations and smaller sizes", on the first line and its refreshes |
| `plate_capacity` | wired | `prepared_intake._prepare_link`: "Checking fit and calculating plate capacity." (formerly "Checking whether it fits the bed", which undersold the capacity work) |
| `plate_arrangement` | wired | `prepared_intake.FitStatus.run`, phase "trying the parts rearranged on one plate at 100%" |
| `quantity_selected` | wired | `prepared_intake._prepare_link`: "N copies selected.", after the count is checked against verified capacity and saved |
| `unit_check` | unwired | the millimeters or inches check is a question, and its answer posts nothing |
| `scaling` | unwired | the only scaling report ("Scaled to ...") is part of the Ready to slice approval question; the other scaling texts are refusals |
| `rotation` | wired | `prepared_intake._prepare_link`: "Reoriented: laid the largest face flat. ..." |
| `split` | wired | `prepared_intake.FitStatus.run`, phase "trying a split onto separate plates at 100%" (both split checks) |
| `supports` | wired | `prepared_intake._prepare_link`: "Slicing with supports." and "Re-slicing with supports." |
| `slice` | wired | `prepared_intake._prepare_link`: "Slicing.", "Waiting for the background slice to finish." and "Retrying the slice." (never the re-slice after accepting filament warnings or an AI fix) |
| `gcode_validation` | unwired | the G-code checks run inside print-now and only speak when they refuse |
| `approval` | unwired | the lines sent before the start question are slicer findings, refused-plate notices and the estimate with thermal facts, which the operator reads to decide; nothing informational follows the answer except the Stop prompt |
| `upload` | wired | `prepared_intake._watch_start_progress`: "Uploaded to the printer. Sending the start now; ..." |
| `print_start` | wired | `prepared_intake._watch_start_progress`: "The printer accepted the start. ..." |
| `print_monitor` | wired | `moonraker_event_listener.announce_warmup_done`: the "Warmup complete, now printing." edit of the one start card, keyed per print and milestone |
| `print_complete` | wired, narrow | `family_control.post_family_event`: the plain completion post for prints started outside Print Handy on a family printer. Moonraker completion posts already carry the completion roast, so they get no second joke; the Bambu launched-print completion post is left alone because its caption and media assembly is shared with the camera watch |
| `print_failure` | wired, narrow | `family_control.post_family_event`: the plain "stopped with an error" post on a family printer. Moonraker cancel and error posts already carry a status roast; pause and stop alerts that offer actions never get a quip; a cancel is not a failure |

The roast engine (`print_handy/core/roast.py`) and its completion, cancel,
error and spaghetti posts are unchanged. Approval and confirmation
questions, their buttons, refusals, errors, filament, clearance and slicer
warnings, safety gates and firmware text never carry a quip.

In tests, `tests/conftest.py` silences the voice so exact progress text
assertions stay deterministic; the wiring tests seed their own quip, and the
corpus tests (`STATUS_VOICE_LIVE = True`) use the real selector.

## Copy count Easter eggs

Three entries live in `planning.json` as exact-copy, first-time messages:

* **2**: "You know what I would do with a million dollars? Two prints at the same time man."
* **10**: "There are 10 types of people in this world, those who understand binary and those who don't."
* **11**: "Eleven copies? These go to eleven. Nigel Tufnel would be proud."

The first verified choice of each exact number triggers its respective line
**guaranteed**, not via a probability roll. They are independent per user and
platform. Later choices of the same count use ordinary status lines. A stable
job key preserves an already chosen line on message replay.

When user identity is unavailable the bot scopes the first-time flag to the
platform and chat. First-time flags are persisted as hashes in
`status_voice_state.json`, in the configured profile state directory.
Selection uses a thread lock and a Linux advisory file lock around atomic
writes, so simultaneous requests from the same bot installation do not
silently both win the first-time flag.

## Corpus contract

Files: `print_handy/data/status_voice/{intake,preflight,planning,slicing,printing}.json`.

Each entry has an `id`, `family`, and `text`. Entries requiring
`{copies}` are eligible only when a verified positive integer is supplied.
First-time entries additionally carry `exact_copies` and `first_time`.
New entries must be tagged with the **real** stage, not a plausible-sounding
future outcome. The bank loader checks exact text and ID uniqueness, known
placeholders, bounds, and exactly 1,000 entries across 25 stages.

The selector has a persisted per-stage shuffle bag, global recent-entry
history, and family spacing to reduce repetitive wording. User-provided
content is not interpolated into these lines.

## Relationship to the existing print roasts

The ordinary completed-print/failure/spaghetti roast engine remains separate
(`print_handy/core/roast.py`). It already supports stable IDs, custom
roast banks, a global history window, and fact-bound templates. The only
behavior change to that engine here is **fairer selection among eligible
factual banks**. A long, heavy print, for example, can receive both long-print
and heavy-filament roasts over multiple jobs instead of always taking the
first fixed-priority factual bank. Reprint-after-failure keeps its
special precedence; status-first selection and title/generic fallback remain.

No approval gate, model geometry, slicing, capacity number, startup command,
or printer readiness rule is changed by this feature. If the optional status
voice fails to load, the factual message still goes out without its joke.

## Testing

Run:

```sh
pytest -q tests/core/test_status_voice.py tests/core/test_roast_engine.py tests/pipeline/test_prepared_intake.py   tests/pipeline/test_status_voice_wiring.py tests/pipeline/test_status_voice_wiring_support.py   tests/hermes/test_status_voice_listeners.py
```

The copy-count tests cover first-time guarantees, independence, replay,
concurrent requests, and quantity values greater than verified capacity.
The content tests assert 1,000 unique entries. The wiring tests drive each
wired stage's real message path with a seeded quip and check that the quip
follows the factual text, that the factual text is unchanged, and that an
empty quip leaves the message byte-identical. This PR does **not**
authorize a live printer test or deployment.
