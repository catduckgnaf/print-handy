# Diagnostics: error taxonomy, markers and the audit inventory (#390)

Print Handy's diagnostics overhaul (issue #390, milestone 2.1.6) gives every
failure one vocabulary, one searchable marker, and one operator-facing bar:
a message says **what failed**, the **most likely cause**, and **what the
operator can do about it**. No bare exception text, no silent drops, and no
telemetry — diagnostics leave the machine only when the operator explicitly
exports a bundle (Stage 3).

## Error taxonomy

Every failure at a main boundary is classified by
`print_handy.diagnostics.classify` into exactly one category:

| Category | Meaning | Typical remedy shape |
| --- | --- | --- |
| `transient` | Network weather: DNS, refused/reset connections, timeouts, 429/5xx | Wait and retry; then check the network path |
| `configuration` | A setting, profile or credential is wrong or missing | Fix the named setting; re-run the doctor |
| `hardware` | The printer or a physical subsystem did not do what was asked | Check the printer; nothing was started by the message |
| `programming` | A Print Handy bug | File a report with the marker and a diagnostics bundle |
| `unknown` | Nothing honest can be said yet | Run the doctor; include the marker in any report |

An explicit category from a boundary that knows better always wins over the
conservative heuristic fallback; an unrecognised failure is `unknown`,
never a guessed cause.

## Greppable markers

Each (subsystem, category) pair has one stable marker,
`PH_<SUBSYSTEM>_<CATEGORY>` — for example `PH_SPOOLMAN_TRANSIENT`.
Markers are append-only within a naming generation: once one has shipped
in a log line or a message it is not renamed again, so support threads
stay searchable. The one rename that has happened is #590: the prefix was
`PB_` (Print Buddy, the old product name) until 2.1.9 and is `PH_` (Print
Handy) from this change on; the alias table under
[Reference codes](#reference-codes) maps every old code to its new one so
old reports stay searchable. The pre-#390
marker `DISCORD_STARTUP_RETRY` (#364/#369) is registered as a legacy marker
in `print_handy.diagnostics.LEGACY_MARKERS` and likewise never changes.

## Reference codes

A reference code (marker) is `PH_<SUBSYSTEM>_<CATEGORY>`: the subsystem
says which part of Print Handy failed, and the category says what kind of
failure it was — see the taxonomy table above for what each category
means and the remedy shape it carries. A code in a chat post appears as
the last line, `Reference: PH_...`; in logs it rides in brackets on the
failure's WARNING line. Codes are how a support conversation starts:
search the code in this repo's issues, or quote it in a new report with
the diagnostics bundle.

Subsystems in use:

| Subsystem | What it covers |
| --- | --- |
| `DISCORD` | Discord startup and login (bot token, gateway intents) |
| `SPOOLMAN` | Talking to the Spoolman service (unreachable, refused, not configured) |
| `INTAKE` | Preparing a model from a link or file (fetch, fit and color steps) |
| `SLICING` | Slicing a prepared model |
| `PRINTER` | Printer-bound actions from chat (stop, resume, next plate, print again, restart) |
| `DEBUG` | Debug log mode and its export |
| `LOGGING` | The configured log file (doctor row) |
| `LISTENER` | Per-printer listener heartbeats (doctor warning row) |

Fixed codes that are not built from a subsystem/category pair:

| Code | What it means |
| --- | --- |
| `PH_DISCORD_CONFIGURATION` | Discord refused the bot token, or the Message Content intent is off: check the token named by `discord.token_env` / turn the intent on in the developer portal. The bot exits instead of retrying. |
| `PH_DEBUG_CONFIGURATION` | A debug-mode state problem, or the marker carried on the "debug mode: ON" line while a session is live. |
| `PH_LOGGING_CONFIGURATION` | The folder of the configured `[logging] file` does not exist or is not writable, so the log file is not being written. |
| `PH_LISTENER_TRANSIENT` | A required printer listener's heartbeat is unhealthy (stale, wrong identity, or not answering). |
| `DISCORD_STARTUP_RETRY` | Legacy log marker (never had a prefix): a Discord login failed transiently at startup and is being retried with backoff (#364/#369). Unchanged by #590. |

Old codes (`PB_`, before #590) and their replacements — search an old
report's code here to find the code current versions emit:

| Before #590 | From #590 |
| --- | --- |
| `PB_<SUBSYSTEM>_<CATEGORY>` (any pair) | `PH_<SUBSYSTEM>_<CATEGORY>` (same pair) |
| `PB_SPOOLMAN_TRANSIENT` | `PH_SPOOLMAN_TRANSIENT` |
| `PB_INTAKE_UNKNOWN` | `PH_INTAKE_UNKNOWN` |
| `PB_DISCORD_CONFIGURATION` | `PH_DISCORD_CONFIGURATION` |
| `PB_DEBUG_CONFIGURATION` | `PH_DEBUG_CONFIGURATION` |
| `PB_DEBUG_UNKNOWN` | `PH_DEBUG_UNKNOWN` |
| `PB_LOGGING_CONFIGURATION` | `PH_LOGGING_CONFIGURATION` |
| `PB_LISTENER_TRANSIENT` | `PH_LISTENER_TRANSIENT` |

## Readable errors: the real cause, and `PH_` not `PB_` (#590)

Two live failures after the 2.1.9 deploy drove this pass. A MakerWorld
link failed with a clear, operator-fixable cause in the container log —
no MakerWorld account token set up — while chat got only a generic
"stopped before it was ready", advice to resend the link (which could
not help), and a `PB_INTAKE_UNKNOWN` reference. The rules now are:

- **A known cause is named.** `print_handy.diagnostics.known_cause`
  recognizes the curated, operator-phrased failures Print Handy raises
  at its model-site boundaries — a missing, empty or refused site
  credential (MakerWorld, Thingiverse, MyMiniFactory, Cults3D), a site
  robot check, a site's built-in download gap, Spoolman not answering
  or not configured — and the chat post states that cause in plain
  words with its remedy (for a token: the site, the credential, and the
  docs/PROJECT-REFERENCE.md section that explains setting it up). The
  wording is built in `diagnostics.py` from the recognized pattern,
  never copied from raw exception text, so no internal path, secret or
  traceback can ride along — the #467 no-raw-text pin still holds.
- **A known cause is classified, not `UNKNOWN`.** The same recognition
  feeds `classify`, so these failures carry a real category
  (`CONFIGURATION` for credentials, `TRANSIENT` for a robot check or an
  unreachable Spoolman) and the marker says so. HTTP 401/403 responses
  classify as `configuration` and 429/5xx as `transient` for the same
  reason. An unrecognised failure is still honestly `unknown`.
- **Resending is only suggested when it can help.** Intake and
  color-selection posts offer "send the link or file again" only for a
  transient failure; a known cause gets its remedy, and a genuinely
  unknown failure keeps the diagnostics bundle hint
  (``/print-handy bundle``) with no resend advice.
- **Reference codes say `PH_`.** Every user-facing marker — the
  `marker()` builder, the Discord startup exits, the doctor rows, the
  debug-mode marker and the Spoolman notice — uses the `PH_` prefix.
  See the alias table above for the old codes.

Two boundaries folded into the same allowlist after #590 (the #605 and
#608 follow-on, once both had landed). Creality Cloud's credential
failures were already recognized through the site list above; the new
rows are its two remaining operator-fixable failures. Direct file links
(#607) are recognized by the `DirectLinkError` type name over the
provider's curated phrasing, with the wording built in
`diagnostics.py` — the original message can name the operator's host
or carry a credential-bearing URL, and neither reaches chat.

| Failure | Category | The post names |
| --- | --- | --- |
| Creality Cloud download answer with no findable file URL | `configuration` | The answer gap, with the browser-download remedy |
| Creality Cloud model does not exist / not found | `configuration` | The model is gone or the link is wrong; check the link |
| Direct link points at a non-public address | `configuration` | Direct links must be public links anyone can open |
| Direct link scheme is not http/https | `configuration` | Send an http:// or https:// link |
| Direct link carries a user name or password | `configuration` | Credential-carrying links are refused; share a public link |
| Direct link has no host name / no address to fetch | `configuration` | Send the complete link |
| Direct link host cannot be looked up (DNS) | `transient` | Check the link or the network, then send it again |
| Direct link host cannot be reached | `transient` | Check the link or the network, then send it again |
| Direct link host answered HTTP 401/403 (or another non-retry status) | `configuration` | The status; check the link is public and the file is still there |
| Direct link host answered HTTP 429/5xx | `transient` | The status; check the link, then send it again |
| Direct link redirected too many times | `configuration` | Send a link that points at the file more directly |
| Direct link returned a web page instead of a print file | `configuration` | Use the host's direct download link; a Dropbox share link works as-is |
| Direct link file is not a 3MF / not a print file / empty | `configuration` | Check the link points at the model's .stl or .3mf file itself |

The direct-link HTTP row parses the status out of the curated message
text: `DirectLinkError` discards the `HTTPError`, so the `code`
attribute path in `classify` cannot see it. Creality Cloud's unexpected
answer and its bare envelope refusal codes deliberately stay
`unknown` — neither is operator-fixable, and the allowlist only claims
causes an operator can act on.

What did not change: logging (full tracebacks stay in the logs), safety
refusals and confirmation gates, and the curated refusal texts that
already named their reason (`IntakeLimitExceeded`, the multi-slot
ValueError, `NextPlateError`, `DesignError`).

## Staged plan

1. **Stage 1 (this page's PR):** taxonomy + markers + the operator-message
   builder (`print_handy/diagnostics.py`), first wiring in the Spoolman
   client's failure warning, and this audit inventory.
2. **Stage 2:** Discord message audit fixes — apply the operator-message
   bar to the surfaces the inventory below marks as failing it.
3. **Stage 3 (done):** diagnostics bundle — `/print-handy bundle` in the bot
   and `python3 -m print_handy.diagnostics_bundle` on the host assemble
   versions, image digest (when the environment records one), a redacted
   config summary (env-name indirection only; secret values never
   included), printer/roster summary, doctor output and a bounded
   recent-log tail. The bug-report issue form asks for the bundle. The
   bundle is only ever written to a local file the operator chooses to
   attach — nothing is uploaded.
4. **Stage 4 (done):** structured logging consistency — per-subsystem
   verbosity via `[logging] levels` (see [Settings](SETTINGS.md)),
   `printer_id` bound on every log line of a per-printer listener
   process, and the container supervisor naming the required child that
   exited (`Required child exited: bot (status 1); stopping container.`)
   instead of a bare exit status. The container healthcheck already named
   each subsystem with its reason and is unchanged.
5. **Stage 5 (done):** doctor / `bot check` tripwires — the doctor gained
   a `logging` row (a configured log file whose folder is missing or
   unwritable now fails loudly with the remedy and
   `PH_LOGGING_CONFIGURATION`, instead of silently falling back to
   stderr) and a `listener heartbeats` warning row naming each required
   printer whose heartbeat is unhealthy, with `PH_LISTENER_TRANSIENT`.
   Both rows also appear in the Web UI status page's doctor section,
   which reuses the same check functions. The heartbeat row is a warning,
   never a doctor failure: the container healthcheck owns failing on a
   dead listener, and a doctor run on a host where listeners are
   legitimately stopped must not fail.
6. **Stage 6 (done):** per-post audit of the Discord action handlers in
   `plugins/print-feedback` — the generic failure posts (color selection,
   next plate, stop, resume, print again, restart, plate picker, alert
   buttons) now go through one helper that keeps the operator detail and
   the state/remedy note and appends the greppable `PH_*` reference
   marker, redacted. The deliberate refusal posts (`NextPlateError`,
   `REFUSED: ... Nothing uploaded or started.`) already met the bar and
   are unchanged; `model_job`'s CLI exits are those same curated refusal
   texts. The design turn's `DesignError` post is the same class —
   a curated, operator-phrased error type — and is likewise unchanged.

## Plain-language pass (#467)

#390's bar was written for the operator debugging the system. #467 holds
the messages a person in chat actually reads to a plainer bar:

1. **First line in plain words** — what you asked for, what happened. No
   exception class names, no raw exception or stderr text, no ALL-CAPS
   failure prefix, no internal jargon.
2. **What did *not* happen**, where it matters — nothing started /
   nothing uploaded / nothing changed / your print is still running.
3. **One clear next step** — the button, command or check that fixes or
   retries it. If there is no user fix (it looks like a bug), the post
   says so and points at the diagnostics bundle (`/print-handy bundle`).
4. **Technical detail demoted, not deleted** — the `PH_*` marker rides as
   a secondary `Reference:` line, and the full detail stays in the log,
   the intake receipt and the bundle.
5. **Never silent, never a dead end.**

The shared builder is `print_handy.diagnostics.plain_post`; the Discord
handlers' `_failure_post` (#390) is built on it, so there is one wording
system. `tests/test_friendly_errors.py` pins the rewritten corpus,
including the negative pin: no inventoried chat post may contain a raw
exception string, a traceback fragment, or an ALL-CAPS failure prefix.

What changed, surface by surface:

| Surface | Before → after |
| --- | --- |
| Slicing failure (prepared intake) | Names the failed slicer (including a failed fallback), timeout, process limit, copy placement, changed job, or safety check and gives a specific next step. An unidentified cause says so and points to `/print-handy bundle`. Every failed retry retains its reason, reference and diagnostics step. Raw errors stay in the receipt and logs. |
| Deterministic slice refusal (prepared intake) | A `SliceRefusal` (`print_handy/core/slice_refusal.py`) says the reason in plain words — e.g. "Slot 1 (Matte white PLA) was never confirmed as loaded, so I have not sliced yet … Is Matte white PLA loaded in slot 1?" — classified `configuration` (`Reference: PH_SLICING_CONFIGURATION`, never `*_UNKNOWN`). Where a safe fix exists it is the button ("Yes, slot 1 has it: slice"), applied before the one re-slice; with no safe fix the post explains and offers no retry. An exception may declare its own category with a `diagnostic_category` attribute, which `classify` honors |
| Backup-slicer notice | `Bambu Studio sliced it because OrcaSlicer failed: {reason}` → "Sliced with the backup slicer (Bambu Studio) because the main slicer could not slice this model."; the reason is logged |
| Fit / resize checks unverified | trailing raw exception → plain unverified wording + resume step + marker |
| Preview failure | `Preview unavailable: {exc}` → "I could not make a preview image, so no image was sent…" |
| Slot/toolhead map problem | `Slot map problem: {exc}` → "Heads-up on the filament positions: …" (the adapter's own phrased reason) |
| Intake preparation failure | `New model preparation stopped: {exc}` → plain post + "Send the link or file again; … `/print-handy bundle`" + marker |
| Colour-selection failure (link intake) | `New model color selection stopped: {exc}` → plain post + retry step + marker |
| Delivery refusal | `Not delivered: …` → "The files were not sent here: …" (the `DeliveryRefused` reason is curated and kept) |
| Start failures (plugin next-plate / print-again / plate picker) | the raw print-now tail (`REFUSED:` / upload / start lines) was posted verbatim → `prepared_intake.plain_start_failure` translates it: a confirmed pre-upload refusal gets the specific plain guidance; anything else gets the honest "did not confirm whether the print started" wording |
| `model_job` pipeline exits (CLI) | `UPLOAD FAILED: {stderr tail}`, `START FAILED: …`, `REFUSED: live hardware check failed: …`, `G-CODE REFUSED: …` → plain first lines ("The file could not be uploaded to the printer, so nothing started…") with the raw detail demoted to a secondary `Detail:` line |
| Guided setup apply | `Applying the settings failed: {raw}` → "The settings could not be saved, so nothing was changed…" + bundle pointer; checklist and retry/manual/cancel replies unchanged |
| Guided debugging findings | `login check failed: {exc}` / `health probe failed: {exc}` → "the login check did not pass" / "the health check did not answer", with the existing Next step kept |
| `printer_state` warnings/problems | `Spoolman lookup failed: {e}`, `Moonraker active spool lookup failed: {e}`, `Moonraker unreachable: {e}`, `Calibration authority read failed: {e}` → plain wording naming the fallback ("the recorded spool was used; printing can continue") or the check to make; #235 fail-soft unchanged |
| Design turns | `DesignError` texts that embedded the raw service error → plain reasons ("the design service did not answer — wait a moment and try again"); the post leads "That design change did not work: … Nothing was sliced or started." |
| Print-control / spool / cold-pull / maintenance generic failures | `…: {exc}` posts → plain posts naming the state ("nothing was changed", "usage is still paused; answer again", "do not retry until the printer state is checked") |
| Vision-watch alerts | `vision check failed: {exc!r}` in the alert post → "the vision check could not be completed, so treat this as unreviewed…"; the raw error stays in the log |
| Spool sync results | `NOT updated ({exc})` / per-slot raw errors → plain "not updated — run `/spool sync`…" wording |
| Access refusal notice | "You are not on this bot's allowed list." → adds why and the next step ("…ask the person who runs Print Handy to add you"); the house "Not allowed. Nothing changed." posts are unchanged |
| Unmatched message | the full command list posted as an error response → a short reply: "I didn't understand that, so nothing happened." + the one common next step (post a link / drop a file) + the list on request via `/print-help`. Explicit `/print-help` / `help` still returns the full list, and the #437 LLM-first order is unchanged |

Deliberately unchanged, and why:

- The `REFUSED: …` lines inside `model_job` / `print_next` /
  `launch_state` remain the **transport contract** between the pipeline
  and its callers (`print_next` treats a trailing `REFUSED:` line as a
  confirmed pre-upload refusal; prepared intake extracts it). They are
  never posted raw any more — every chat path translates them — and the
  receipts keep them verbatim for support.
- Curated error types whose text is already operator-phrased —
  `NextPlateError` ("No next plate: …"), `DesignError`, guided-setup
  validation answers ("That answer does not work: reply with …") — keep
  their text; #467 removed the *raw machinery* interpolation under them.
- Logs, the diagnostics bundle, doctor/`bot check` rows and Web UI
  status keep full technical detail; only chat posts were rewritten.

The fail-soft posture is preserved throughout (#235): warnings never block
a print, and no print path changes behaviour beyond reporting.

## Debug log mode and sanitized exports (#478)

On top of the bundle and the static `[logging] levels` above, Print Handy
has a temporary **debug log mode** for reproducing a problem:

- **Surfaces.** `/print-handy debug on [minutes]` / `off` / `status` in the
  bot, the Debug log mode form on the Web UI status page, and
  `python3 -m print_handy.debug_mode on|off|status` on the host are all
  equivalent — they read and write one shared state file
  (`debug-state.json`) in the state folder. No config edit, no restart.
- **Duration.** `on` lasts **30 minutes by default** (an explicit number
  of minutes may follow, up to 24 hours), then the mode turns itself off.
  Starting it again ends the old session cleanly and starts a new one.
- **While on.** Print Handy's own loggers run at DEBUG; noisy library
  loggers (discord, websockets, asyncio) stay at INFO. Records are also
  written to a dedicated, size-bounded capture file
  (`debug-capture.log`, rotating, in the state folder), delimited by
  `DEBUG_SESSION_START` / `DEBUG_SESSION_END` markers carrying the
  session id. The capture exists whether or not a `[logging] file` is
  configured, so a Docker (stderr-only) install still produces logs.
  **Listener processes are covered**: the mode is shared state, not a
  per-process switch — every process applies it when it configures
  logging, the standalone bot re-checks it on its heartbeat, and the
  Moonraker, family and Bambu listeners poll it in their run loops.
  `bot check`, the doctor and the Web UI status page show a
  "debug mode: ON — session …, off in …" row (marker
  `PH_DEBUG_CONFIGURATION`) while a session is live.
- **Logging only.** Turning the mode on or off, and exporting, never
  changes print behaviour, confirmation gates or readiness guards.

**Sanitized export.** Turning the mode off in the bot builds the export
for that session's window (the capture lines between its markers) and
delivers the file **in chat**, after showing its sanitization report;
the CLI's `off` writes the same export to `state/debug-exports/`. The
file is named `print-handy-debug-<date>-<session>.md`. With no session
window, the export (and the #390 bundle, which uses the same path) falls
back to the existing bounded log tail. Nothing is uploaded anywhere:
the operator reviews the export and attaches it to the issue themselves,
and the bug-report form asks for the debug session ID and the
sanitization report next to the bundle field.

Exports are sanitized by `print_handy/sanitize_logs.py`, layered on
`logs.redact` (which already strips tokens, credentials and secret env
values). The export pass additionally removes:

- IP addresses and internal hostnames, in URLs and bare text — the URL
  scheme, port and path shape stay, so the line remains diagnosable;
- Discord/Telegram/Matrix user and channel IDs, plus IDs the config
  itself names — replaced with **consistent pseudonyms** within one
  export (`user-1`, `channel-1`, …) so lines still correlate;
- local file paths, email addresses, MAC addresses, serial-number-shaped
  values, and URL query strings.

A **verification pass** then re-runs the whole sanitizer over the result;
anything the first pass missed is removed there and counted too. Every
export carries the **sanitization report** — counts by category, never
the removed values (for example: "removed: 4 secrets/tokens, 6 IP
addresses, 3 user IDs. Verification pass found nothing further.") — so
the operator can see at a glance what was taken out before attaching it
to a public issue. The diagnostics bundle's config summary and log
sections use the same sanitizer and carry the same report.

## Audit inventory (Stage 1)

User-facing failure/warning surfaces, audited against the bar above.
"Meets bar" is a judgement from reading the code; the Stage 2 PR re-checks
each row as it changes it.

| Surface | What it does today | Bar verdict | Stage |
| --- | --- | --- | --- |
| Discord startup (`bot/client.py` `run_bot`) | `DISCORD_STARTUP_RETRY` log marker with attempt/backoff; token/intent failures print an actionable stderr line and exit 3 | Meets bar after Stage 2: the exhausted-retry line carries `PH_DISCORD_*`, and the token/intent exits name the remedy and `PH_DISCORD_CONFIGURATION` | 2 (done) |
| Spoolman unavailable (pipeline notice) | One-line notice that tracking was skipped; failure detail only in the WARNING log | Meets bar after Stage 2: the notice states the print continues and what to check; the log warning carries the exact `PH_SPOOLMAN_*` marker (Stage 1) | 2 (done) |
| Timelapse completion posts (#383) | Every degraded outcome posts a reason line instead of dropping the video silently | Meets bar — the reference pattern for Stage 2 | — |
| Intake / slicing / delivery posts (`core/model_job.py` + plugin handlers) | Curated refusal texts carry the reason and the state; generic handler failures were built from raw exception text with no marker | Meets bar after Stage 6: generic Discord handler posts carry the detail, the state/remedy note and the `PH_*` marker via `_failure_post`; deliberate refusals unchanged | 6 (done) |
| Listener heartbeat (`healthcheck.py`, `bot/health.py`) | `(ok, reason)` strings name staleness/identity/connection states | Names what failed; no remedy, no marker | 5 |
| Container supervisor (`docker/supervise.sh`) | A required child's exit is reported as a bare status; listener exits name the printer and the restart budget | Required-child exit does not name the child | 4 |
| Container healthcheck (`docker/healthcheck.py`) | One row per subsystem with a reason string | Mostly meets bar; rows get markers in Stage 5 | 5 |
| Doctor (`doctor.py`) | `FAIL <check>: <detail>`; several details already include the remedy (e.g. the #384 platform row) | Mostly meets bar; new detectable modes get rows in Stage 5 | 5 |
| Web UI status (`webui/status.py`) | Status/health JSON for the same checks | Stage 5 adds equivalents for new doctor rows where a surface exists | 5 |
| Web UI control API (`webui/control.py`, #708) | Every refusal and failure is an explicit `{ok, reason}` answer with a distinct status per cause: 404 controls disabled or unknown printer, 401 bad token, 400 malformed target, 409 state does not allow the action (nothing sent), 422 the connection does not support it, 502 the printer could not be read or the send failed. A sent action reports the read-back state in the chat paths' wording | Meets bar: a control failure always names what happened and whether anything was sent; no silent success, no raw exception text beyond the typed failure the read routes already expose | — |
