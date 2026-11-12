# Discord conversation mockups

Sanitized mockups of the Print Handy Discord conversation, one image per flow step, with the real
message text and the real button labels. Nothing here is a capture of a real server: names, IDs,
paths and printer details are placeholders (user "you", bot "Print Handy", printer "Workshop AD5X",
model "Cable clip"). The camera frame and the timelapse thumbnail are drawings, not photos.

Regenerate: open `src/<name>.html` in headless Chrome at 900 px wide with
`--force-device-scale-factor=2`, then quantize to 256 colors. The pages reference
`../../branding/icon-192.png` for the bot avatar.

Button colors follow Discord's styles: primary (blurple), success (green), danger (red), secondary (grey).
Plain `clarify` question buttons are primary, except labels containing cancel / don't / keep / skip / not,
which are grey (`print_handy/bot/adapter.py:105-113`). Messages are plain text (no embeds), as in the code.

| Image | Flow step | Source (file:line) |
| --- | --- | --- |
| `discord-01-link-and-colors.png` | Paste a model link; "Checking the model" with Stop; color question; the answered state | `print_handy/core/prepared_intake.py:1047` (Checking the model), `:54` (Stop label), `:1157` (color question); slot button labels `print_handy/core/model_job.py:798-814`; Stop style `plugins/print-feedback/__init__.py:2691`; answered-state edit `print_handy/bot/adapter.py:171`. A pasted link gets no "Got the link" receipt: `plugins/print-feedback/__init__.py:653-656`, `tests/hermes/test_print_messages.py:727` |
| `discord-02-copies-and-confirm.png` | Copies question, then estimate and spool projection with the bed-clear choice | `print_handy/core/prepared_intake.py:1467` (copies), `print_handy/core/quantity_selection.py:8-12` (copy labels), `prepared_intake.py:1509-1510,1525-1527` (confirm text and the four labels), `print_handy/core/model_job.py:3890-3905` (spool projection line) |
| `discord-03-print-start.png` | Click recorded; "Preparing to start" with Stop; "Print started" post with the roast line | `prepared_intake.py:1730,1739` (preparing/uploading), `print_handy/runtime/discord_print_summary.py:29,229-251` (started post layout, "Estimated total", "Approx. finish"). The roast itself is LLM-generated per print (`moonraker_event_listener.py:352`, appended to the started post at `prepared_intake.py:1764-1768`); the line shown is an illustrative example of its style and length (one sentence, under 20 words), not a captured generation |
| `discord-04-print-complete.png` | Completion post with the timelapse attached | `discord_print_summary.py:29,229-251`; completion posted from `print_handy/runtime/moonraker_discord_announcements.py:874`; timelapse attached in `print_handy/runtime/moonraker_event_listener.py:1100-1150` |
| `discord-05-camera-watchdog.png` | Camera watchdog alert (Pause / It's fine / Stop asking for this print), and the dark-camera notice | `moonraker_event_listener.py:1642` (alert text), `:1666` (dark notice), `print_handy/runtime/print_alert_buttons.py:194,202-212` (labels and styles). The reason line ("stalled_layers at layer N: verdict; detail") is built at `moonraker_event_listener.py:2045`; the detail wording here is an example |
| `discord-06-pause-and-runout.png` | Paused post with Resume print / Cancel print; filament runout variant with the spool question and its Cancel print button | `moonraker_event_listener.py:452-475` (paused text), `:638-653` (spool question), `print_alert_buttons.py:202-217,229-247` (buttons) |
| `discord-07-new-spool.png` | New spool found in Spoolman: Load into a slot, slot picker, occupied-slot question, Confirm load. From PR #493 | `print_handy/runtime/print_alert_buttons.py` (`new_spool_prompt_text`, button labels) and `plugins/print-feedback/__init__.py` (`new_spool_slot_select`, `new_spool_confirm`, `handle_new_spool`) as of PR #493 |
| `discord-08-fit-and-geometry.png` | The fit question for a model that does not fit full size (split / shrink choices), and the millimeters-or-inches question for a small STL | `prepared_intake.py:466-505` (`fit_question` text: measured size, shrink option, full-size split option, "Pick a fit option, or type split / 100% / shrink / cancel"), `prepared_intake.py:392-435` (`fit_choice_rows` labels: "100% · split N plates", "Shrink to N%", "Cancel"); units question `prepared_intake.py:1063-1070` and `print_handy/core/model_job.py:1996-2002` (choice labels). The model name, dimensions and percentages are placeholders in the same shapes |
| `discord-09-spool-change.png` | Guided spool change: "swap the filament" opens the slot picker, the shelf picker, the old spool's fate, one confirmation, and the result line | `plugins/print-feedback/__init__.py` (`_spool_guided_change`, `_spool_slot_ask` "Which slot are you changing?" with per-slot lines, the shelf question "Which spool goes into slot N? … or choose Add a new spool", `_spool_disposition_ask` "Slot N still holds spool #M. What happens to spool #M?" with `_SPOOL_DISPOSITIONS` labels Back on the shelf / Archive it / Mark it as depleted, the "Change slot N to spool #M?" confirmation) and `print_handy/runtime/spool_sync.py:437-455` (`bind_text` result line). Spool names, numbers and grams are placeholders |

## Not mocked

- Slice-only path ("Start this exact plate?" with Start next plate / Cancel): `prepared_intake.py:1701-1713`, `print_handy/runtime/print_next.py:31-32`.
- `/print-again` and the plate picker: see `print_control.py:33-38` and `print_alert_buttons.py:260-272`.
- Periodic progress posts: the code has none (only start, pause/alert, complete).
