# Listener domains

[Documentation home](README.md) · [Docker](DOCKER.md)

A **listener** watches one printer continuously and reports what happens, 
status changes, progress, completion, errors, so the bot can post about a
print without anyone asking. Print Handy splits listeners by **domain**: the
configured printer's connection / firmware type. The domain selects the
listener *type*; each configured printer gets its own listener *instance*,
supervised and healthchecked independently, so one printer's listener
failing can never take down the bot, Spoolman, or another printer's
listener.

## The registry

`print_handy/listeners.py` is the single registry: connection → listener
domain. The supervisor's gate (`python3 -m print_handy.listener_gate`), the
container healthcheck and the registry launcher (`python3 -m
print_handy.listeners`) all consult it, adding a domain listener means
registering the module and implementing the contract below, with no
supervisor or healthcheck edits.

| Domain (connection) | Listener | Transport |
|---|---|---|
| `moonraker` | implemented: `print_handy.runtime.moonraker_event_listener` | Moonraker websocket |
| `bambulab_lan` | implemented: `print_handy.printers.bambulab.listener` | Bambu LAN MQTT, long-lived |
| `prusalink` | implemented: `print_handy.runtime.family_event_listener` | PrusaLink HTTP status polling |
| `elegoo_sdcp` | implemented: `print_handy.runtime.family_event_listener` | Elegoo SDCP status polling |
| `octoprint` | implemented: `print_handy.runtime.family_event_listener` | OctoPrint HTTP status polling |
| `flashforge_stock` | implemented: `print_handy.runtime.family_event_listener` | Stock Flashforge LAN HTTP status polling |
| `offline` | none, by definition | Print Handy never contacts the printer |

Every connection except `offline` now has a delivered listener. A domain with no listener yet is a normal
state, not an error: the supervisor starts nothing for it and a
configuration with no children at all idles instead of exiting. A
configuration that cannot be resolved at all is different; the supervisor
stops with the settings error rather than guessing which children to run.

## The normalised contract

Every domain listener reports through the one shape the printer adapters'
`normalize_status` already returns. Consumers, Discord status and
completion posts, the camera watchdog, timelapse, subscribe to this
shape and never need to know which domain produced it.

**Status fields**

| Field | Meaning |
|---|---|
| `state` | Normalised state string (`printing`, `paused`, `complete`, `error`, `unknown`, …) |
| `filename` | Current job's file name, or `""` |
| `progress` | 0..1, or `None` when the domain cannot know yet |
| `hotend_c`, `bed_c` | Current temperatures, or `None` |
| `hotend_target_c`, `bed_target_c` | Target temperatures when the protocol reports them |
| `remaining_min`, `layer`, `total_layer` | Optional, when the protocol reports them |

A domain may add its own extra fields (for example SDCP's `status_code`);
core consumers read only the fields above. `print_handy.listeners.validate_status`
pins the core shape in tests.

**Event kinds**

| Kind | Meaning |
|---|---|
| `status` | A fresh status snapshot in the shape above |
| `completion` | A print finished |
| `error` | The printer reports an error. The payload keeps the domain's raw representation alongside the normalised state, for Bambu that includes its HMS entries, so no diagnostic detail is lost in normalisation |
| `snapshot_available` | A camera snapshot can be fetched for this printer now |

**Liveness.** A running listener writes the listener heartbeat
(`listener_health.json`, see `print_handy.healthcheck`) under its domain's
registered runtime identity. Since #336 the heartbeat lives in the
printer's own state directory (`state/printers/<id>/listener_health.json`)
and the record names its printer; a legacy single-printer configuration
keeps the historical root-level path. The container healthcheck requires
that heartbeat exactly when the registry says the printer's domain is
implemented, and reports "not required for this printer connection"
otherwise.

## The Bambu domain listener

`print_handy/printers/bambulab/listener.py` holds one Bambu LAN MQTT
session open and merges the printer's delta pushes into a persistent
state, normalising through the Bambu adapter. Its discipline comes from
the #298 audit of Bambuddy (protocol facts only, no code copied):

- `get_version` + `pushall` on every (re)connect, keepalive 30 s, and no
  periodic pushall; the one extra request is at layer 1 when the layer
  total is still unknown.
- Only `push_status` frames (or frames with no `print.command`) are
  telemetry; command acknowledgements never update state.
- 60 s without a message is stale: the session is torn down and rebuilt
  with a fresh MQTT client (hard reset, so a publish queued on the dead
  client can never replay) after a 30 s reconnect cooldown.
- Read-only: the only payloads it publishes are its own
  `get_version`/`pushall` requests.
- Events: gcode_state transitions become `status` events
  (started/paused/resumed/cancelled), FINISH becomes `completion`, FAILED
  becomes `error` with the decoded HMS entries attached, and the first
  complete snapshot of a session emits `snapshot_available` when the
  model has a verified camera protocol.

With `[bambu] lan_host`/`lan_serial` unset, the Bambu listener starts but
idles, heartbeat alive, `printer_connected=False`, no network contact, 
so an offline Bambu configuration behaves exactly as it did before the
domain had a listener. See [Bambu Lab printers](BAMBU-LAB.md#lan-status-read-only-opt-in).

Since #460 the listener's `main()` wires its events to a posting sink
(`family_control.BambuEventSink`): the transitions it always detected, 
started / paused / cancelled / completion / error, now post for prints
started outside Print Handy. Claimed prints (below) stay silent here;
they belong to their launched-print monitor.

## Start/pause parity: the polling families

Moonraker's listener has always posted the full lifecycle, starts
(launched and external), pauses with Resume/Cancel buttons, completions.
When that G-code contains `EXCLUDE_OBJECT_DEFINE` names, the Moonraker
start post lists them (`Objects still printing: ...`) and offers a cancel
control. Choosing one object asks once. Confirm sends one Moonraker
`EXCLUDE_OBJECT NAME=...` script for that name. The last remaining object
uses the existing `/print-stop` confirm instead of an exclude. Objects
Klipper already excluded count as gone, so after one exclude the other
of two is the last. Just before the send the printer is read again: the
same file must still be printing or paused, or nothing is sent and the
reply says the print changed. If that read fails, nothing is sent. A file
without that metadata does not offer the control. A failed exclude is
reported and is not described as cancelled. The camera watchdog does not
exclude or stop a print. Bambu, Prusa, OctoPrint, and stock Flashforge
are not given an exclude. On Telegram and Matrix the same choice is
`/exclude <name>`, which asks before anything is sent.
#460 gives every other family the same contract through one shared
mechanism (`print_handy/runtime/family_control.py`), not five copies.
The helpers every listener needs live outside the Moonraker listener
(#1320): the chat post path, bot tokens, pause classes and the vision
route in `listener_common.py`, start claims and the abandoned-start rule
in `start_claims.py`, and the camera watch rules and coverage in
`camera_watch.py`. No other listener imports
`moonraker_event_listener`; `tests/hermes/test_camera_watch_core.py`
enforces it.

- **Launched prints** are claimed by the print path
  (`model_job.claim_start_announcement`, the same claim the Moonraker
  listener honours) before the family's start command is sent. Their
  pause alerts come from their launched-print monitor: each `*_watch`
  monitor feeds its polls to `family_control.PauseAlerts`, which posts
  exactly one pause alert per printing → paused episode and retires the
  print's alerts when it ends.
- **External prints** (started at the printer or from another app) are
  the listener domain's. The four polling families share one listener
  (`print_handy/runtime/family_event_listener.py`): it polls the family's
  normalised status every 15 s, the same read its monitor uses, and
  runs the Moonraker state machine over it. The Bambu domain does the
  same through its event sink.
- **Resume/Cancel** on a family pause alert reach the family's *own*
  adapter `control()` operation (`family_control.FamilyControl`), routed
  by the alert record's connection marker in
  `print_alert_buttons.handle_click`, never the Moonraker transport.
  The press re-reads the family's state first, refuses when the state no
  longer matches or a different file is loaded, sends at most one
  command, and reports the read-back; the Moonraker discipline. Every
  family verifies state before resuming this way, so no family is
  offered a blind Resume. Nothing actuates without an operator press:
  posters and listeners hold no control callable.
- **No stale buttons.** When a print ends, its pause alerts are retired
  (buttons stripped, headline updated) by whichever owner sees the end
  first; the monitor or the listener; retiring is idempotent. A pause
  resolved at the printer leaves the alert live, exactly as Moonraker's
  does: its buttons re-read state on a press and refuse truthfully.
- **Baseline rule.** A print already active at a listener's first
  successful read is never classified: no start, pause or terminal post
  for it. Only a witnessed inactive → active transition classifies a
  print, via the start-claim file. The polling listener persists its
  classification, so a restart mid-print restores it without re-posting
  the start. A launched-print monitor that dies mid-print is not
  restarted (the pre-existing monitor limitation); the listener still
  retires that print's pause alerts when it ends.

Per-family event coverage (all rows: pause post with Resume + Cancel,
external start post, completion attaches as before #460):

| Family | Pause post source (launched) | External prints | Resume / Cancel transport |
|---|---|---|---|
| `bambulab_lan` | `bambu_watch` monitor | Bambu listener sink | adapter `control()` over Bambu LAN MQTT |
| `prusalink` | `prusa_watch` monitor | polling listener | adapter `control()` over PrusaLink HTTP (`/api/v1/job`) |
| `elegoo_sdcp` | `elegoo_watch` monitor | polling listener | adapter `control()` over SDCP (pause/resume/stop commands) |
| `flashforge_stock` | `stock_watch` monitor | polling listener | adapter `control()` over stock LAN HTTP (`jobCtl_cmd`; resume is spelled `continue`) |
| `octoprint` | `octo_watch` monitor | polling listener | adapter `control()` over OctoPrint HTTP (`/api/job`; stop is spelled `cancel`) |

On standalone Telegram and Matrix the family pause alert is plain
text; the inline-keyboard and reaction forms belong to the Moonraker
listener's own pause and stop alerts.

## Instances

Since #336 (multi-printer Stage 2d) the supervisor runs **one listener
instance per roster printer** whose domain has an implementation:

- The supervisor enumerates the roster
  (`python3 -m print_handy.listener_gate --roster`), gates each printer
  (`--printer <id>`: 0 = start, 3 = the domain has no listener, anything
  else = unresolved configuration, which stops the container with the
  settings error), and starts
  `python3 -m print_handy.listeners --printer <id>` for each printer
  that passes.
- An instance runs against its printer's settings view (the Stage 2b
  `view_for` projection) with the roster entry's channel claim applied,
  so its completion posts and questions land in the **owning printer's
  channel**; deferred screen updates queued for that printer (Stage 2c)
  flush through its listener. Its heartbeat and state live under
  `state/printers/<id>/`.
- Listener instances are **not required children**. When one exits, the
  supervisor restarts only that printer's listener, with backoff
  (5 s doubling to a 60 s cap) and a restart budget (5 restarts per
  10 minutes). A listener that exhausts its budget stays down and the
  healthcheck reports that printer unhealthy; a listener failure can
  never restart the container or disturb the bot, Spoolman, or another
  printer's listener. The bot and bundled Spoolman remain the required
  children whose exit ends the container.
- A Moonraker print that finished or was cancelled while its listener was
  down or stuck is not lost: when the listener comes back and sees the
  printer idle or on a different job, it reads Moonraker's job history and posts
  each missed finish once, with the time it ended ("Finished at 14:05
  while the bot was offline."). Such a post has no timelapse or roast.
- The container healthcheck prints one listener row per roster printer.
  A single-printer (or legacy) configuration keeps exactly the
  historical process tree and the single health row.

Running several containers, one per printer, remains available, see
[More than one printer](MULTI-INSTANCE.md).
