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
the last line, as a link to the code's own catalog section below
(`Reference: [PH_...](https://catduckgnaf.github.io/print-handy-docs/diagnostics#ph-...)`;
see [Error code catalog](#error-code-catalog-one-linked-section-per-code-924));
in logs it rides in brackets on the
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

## Error code catalog: one linked section per code (#924)

Since #924, the `Reference:` line at the end of a chat failure post is a
link: `Reference: [PH_INTAKE_CONFIGURATION](https://catduckgnaf.github.io/print-handy-docs/diagnostics#ph-intake-configuration)`.
Clicking the code opens this page at the code's own section, with its
cause and recovery steps. A failure that `failure_report.py` classifies
to a known signature links the more specific signature code instead
(for example `PH_PARSE_BAD_3MF_STRUCTURE`), because that section names
the exact problem rather than the general category.

The rules that keep the links honest:

- **Slug rule.** A code's anchor is the code lowercased with every
  underscore turned into a hyphen: `PH_INTAKE_CONFIGURATION` lives at
  `#ph-intake-configuration`. Every catalog heading below carries that
  anchor explicitly, so the rule holds regardless of how the docs
  engine slugifies heading text.
- **Registry.** The codes that exist are registered in code, in one
  place per family: `print_handy.diagnostics.ERROR_CODES` for marker
  codes and `print_handy.core.failure_report.SIGNATURE_CODES` for
  signature codes. Each registry entry builds its link from the single
  base URL in `print_handy.diagnostics` (`DOCS_BASE_URL`), so no file
  hardcodes its own copy of the address.
- **Drift test.** `tests/test_error_code_docs.py` parses this page and
  asserts the registry and the catalog match both ways: every
  registered code has a section at its anchor, and every catalog
  section is a registered code. A code added without a section — or a
  section added without a code — fails CI.
- **Fallback.** A marker built from a subsystem/category pair that is
  not in the registry (a combination no current boundary emits) still
  prints its code as a link, to the [Reference codes](#reference-codes)
  section above, instead of pointing at an anchor that does not exist.
- **What did not change.** Log lines keep the bare bracket token
  (`[PH_SPOOLMAN_TRANSIENT]`) so existing log searches and alerts keep
  working, and structured receipts keep the machine-readable values
  they always carried (the `failure` record's `signature` id, stage,
  cause and recovery). Only the human-facing `Reference:` tails in chat
  posts, operator messages, the Discord startup exits and the doctor
  rows gained links. Doctor rows and the startup exits are plain text,
  so there the code is followed by the plain URL in parentheses rather
  than Markdown link syntax. On Telegram and Matrix, which carry these
  posts as plain text by design, the raw text still contains the full
  URL and the code, and clients make the URL openable; on Discord the
  Markdown link renders as a clickable code.

### Marker codes: Discord startup

#### PH_DISCORD_CONFIGURATION {#ph-discord-configuration}

**What failed:** Discord refused the bot token, or the Message Content
privileged intent is off for the bot. The bot exits instead of
retrying — retrying cannot fix either cause.
**What to do:** check that the token in the environment variable named
by `discord.token_env` is the current bot token, and turn on the
Message Content intent in the Discord developer portal (Bot >
Privileged Gateway Intents). Then restart the bot.

#### PH_DISCORD_TRANSIENT {#ph-discord-transient}

**What failed:** a Discord login at startup failed transiently (a
network or gateway hiccup) and was retried with backoff; this marker
rides the final `DISCORD_STARTUP_RETRY` log line when the retries run
out.
**What to do:** wait a moment and restart; if it keeps happening,
check the network path to Discord and Discord's own status. The
container restart policy is the outer backstop.

### Marker codes: Spoolman

#### PH_SPOOLMAN_CONFIGURATION {#ph-spoolman-configuration}

**What failed:** Spoolman is not configured — `services.spoolman_url`
is empty — so filament tracking could not talk to it.
**What to do:** set `services.spoolman_url` to your Spoolman service,
then try again. Prints are not blocked by this; tracking is skipped
until Spoolman answers.

#### PH_SPOOLMAN_TRANSIENT {#ph-spoolman-transient}

**What failed:** Spoolman did not answer (the service is down, or the
network path to it hiccuped).
**What to do:** check that the Spoolman service is running and that
`services.spoolman_url` points at it, then try again.

#### PH_SPOOLMAN_UNKNOWN {#ph-spoolman-unknown}

**What failed:** talking to Spoolman failed in a way Print Handy does
not recognize as a configuration or network problem.
**What to do:** check the Spoolman service's own log next to Print
Handy's log line carrying this code; if you report it, include the
code and a diagnostics bundle (`/print-handy bundle`).

### Marker codes: intake

#### PH_INTAKE_CONFIGURATION {#ph-intake-configuration}

**What failed:** preparing a model from a link or file stopped on a
problem intake can name — a missing or refused model-site credential,
a 3MF length unit that cannot be converted exactly, an unconfirmed
filament choice, and similar operator-fixable causes. Where a more
specific signature code exists (below), the post links that instead.
**What to do:** follow the fix the post names (for a credential, the
post names the site, the credential and the docs section that explains
setting it up), then send the link or file again. Resending unchanged
cannot help a configuration failure.

#### PH_INTAKE_TRANSIENT {#ph-intake-transient}

**What failed:** preparing a model was interrupted by network weather
— a host that could not be looked up or reached, a timeout, or a
temporary refusal (HTTP 429/5xx).
**What to do:** wait a moment and send the link or file again; if it
keeps happening, check the network connection.

#### PH_INTAKE_PROGRAMMING {#ph-intake-programming}

**What failed:** intake hit what looks like a Print Handy bug — for
example, a model whose colors cannot be split into single-color
plates.
**What to do:** this is not something a setting fixes. Run
`/print-handy bundle` and file a report with this code and the bundle.

#### PH_INTAKE_UNKNOWN {#ph-intake-unknown}

**What failed:** preparing a model failed and Print Handy has no
specific explanation for it yet. Nothing was sliced, uploaded or
started.
**What to do:** run `/print-handy bundle` and share the bundle with
this code so the failure can be looked into. Do not just resend the
same link repeatedly — an unknown failure is rarely fixed by a retry.

### Marker codes: slicing

#### PH_SLICING_CONFIGURATION {#ph-slicing-configuration}

**What failed:** slicing was refused or stopped on a deterministic
check — an unconfirmed loaded spool, a job or setting that changed
after the slice was requested, a safety guard, or another named
configuration problem. This is the code a `SliceRefusal` carries, and
it is never reported as `*_UNKNOWN`.
**What to do:** follow the fix the post names (where a safe fix
exists, it is offered as a button and applied before the one
re-slice). Re-slicing with nothing changed gives the same refusal.

#### PH_SLICING_TRANSIENT {#ph-slicing-transient}

**What failed:** the slicer hit a transient failure, such as exceeding
its time limit.
**What to do:** the post offers one retry of the slice without
printing. If the retry also fails, run `/print-handy bundle` and
share the bundle with this code.

#### PH_SLICING_PROGRAMMING {#ph-slicing-programming}

**What failed:** slicing hit an internal Print Handy error, or the
slicer produced G-code whose generator could not be verified.
**What to do:** run `/print-handy bundle` and file a report with this
code and the bundle; the saved slicer logs in the failure folder hold
the detail.

#### PH_SLICING_UNKNOWN {#ph-slicing-unknown}

**What failed:** the slice failed and Print Handy could not identify
the cause. No G-code was kept, and nothing was uploaded or started.
**What to do:** you can retry the saved model once without printing;
if it fails again, run `/print-handy bundle` and share the bundle
with this code.

### Marker codes: printer actions

#### PH_PRINTER_CONFIGURATION {#ph-printer-configuration}

**What failed:** a printer-bound action from chat (stop, resume, next
plate, print again, restart, and similar) stopped on a configuration
problem — for example a calibration authority mismatch, where the
confirmed Z offset belongs to a different screen mode than the one
the printer is using.
**What to do:** follow the step the post names (for a calibration
mismatch, confirm the current offset with `/calibration`), then try
the action again.

#### PH_PRINTER_TRANSIENT {#ph-printer-transient}

**What failed:** the printer or its listener did not answer in time
for a printer-bound action.
**What to do:** check the printer and its screen before retrying —
the post says what did and did not happen, and an action whose
outcome is unconfirmed must not be repeated blindly.

#### PH_PRINTER_HARDWARE {#ph-printer-hardware}

**What failed:** the printer or a physical subsystem did not do what
was asked. This code is in the catalog deliberately: no automatic
classifier assigns the `hardware` category today, but a boundary that
knows a failure is physical can pass the category explicitly, and
that post must land on a real section.
**What to do:** check the printer and its connection. No print was
started by the message that carries this code.

#### PH_PRINTER_PROGRAMMING {#ph-printer-programming}

**What failed:** a printer-bound action hit what looks like a Print
Handy bug.
**What to do:** check the printer screen so you know its real state,
then run `/print-handy bundle` and file a report with this code and
the bundle.

#### PH_PRINTER_UNKNOWN {#ph-printer-unknown}

**What failed:** a printer-bound action failed and Print Handy has
no specific explanation for it.
**What to do:** check the printer screen before doing anything else
(the action may or may not have taken effect), then run
`/print-handy bundle` and share the bundle with this code.

### Marker codes: debug mode, logging, listeners

#### PH_DEBUG_CONFIGURATION {#ph-debug-configuration}

**What failed:** this is the standing marker of debug log mode — it
rides the "debug mode is ON" status line while a session is live,
and names debug-mode state problems.
**What to do:** while a session is live, no action is needed; turn
the mode off with `/print-handy debug off` when the problem is
reproduced, which exports the sanitized session log. See
[Debug log mode](#debug-log-mode-and-sanitized-exports-478).

#### PH_DEBUG_TRANSIENT {#ph-debug-transient}

**What failed:** a debug-mode operation, such as building the
sanitized export when the mode is turned off, hit a transient
failure.
**What to do:** debug mode stays on and the capture is kept. Try
`/print-handy debug off` again; on the host,
`python3 -m print_handy.debug_mode off` does the same thing.

#### PH_DEBUG_PROGRAMMING {#ph-debug-programming}

**What failed:** debug mode hit what looks like a Print Handy bug.
**What to do:** the capture is kept. File a report with this code
and, if an export can be built, the sanitized export.

#### PH_DEBUG_UNKNOWN {#ph-debug-unknown}

**What failed:** a debug-mode operation failed in a way Print Handy
does not recognize — the operator message after a failed debug log
export carries this code.
**What to do:** debug mode is still on and the capture is kept. Try
`/print-handy debug off` again, or on the host run
`python3 -m print_handy.debug_mode off`. If you report it, include
this code.

#### PH_LOGGING_CONFIGURATION {#ph-logging-configuration}

**What failed:** the folder of the configured `[logging] file` does
not exist or is not writable, so the log file is not being written
(the doctor's `logging` row fails with this code; at runtime the
logger silently falls back to stderr, which is why the doctor checks
it loudly).
**What to do:** create the folder or fix its permissions, or change
`[logging] file` to a writable path, then re-run
`python3 -m print_handy.doctor`.

#### PH_LISTENER_TRANSIENT {#ph-listener-transient}

**What failed:** a required printer listener's heartbeat is unhealthy
— stale, carrying the wrong identity, or not answering (the doctor's
`listener heartbeats` warning row carries this code).
**What to do:** check that printer's listener is running; inside the
container it restarts on its own. The row is a warning, not a doctor
failure — the container healthcheck owns failing on a dead listener.

### Signature codes: parse

A signature code means `failure_report.py` recognized the failure's
pattern in the slicer's or intake's evidence. The post's cause and
recovery text comes from the same classification; this catalog is the
long-form version of each one.

#### PH_PARSE_MODEL_UNREADABLE {#ph-parse-model-unreadable}

**What failed:** the slicer could not read this 3MF file. Its model
data is damaged or laid out in a way the slicer does not understand
(the slicer reported an unreadable object, an invalid 3MF, or its
parse failure code).
**What to do:** re-export the model as a fresh 3MF (or send the STL)
from the program that made it, then send it again. If it fails again,
run `/print-handy bundle` and share the bundle.

#### PH_PARSE_NO_VOLUME {#ph-parse-no-volume}

**What failed:** this file has no solid volume. The mesh is flat,
zero-thickness or empty, so there is nothing to print.
**What to do:** re-export the model as a solid, closed mesh (give it
real thickness), then send it again.

#### PH_PARSE_BAD_3MF_STRUCTURE {#ph-parse-bad-3mf-structure}

**What failed:** the 3MF file's internal structure is inconsistent —
for example a part that refers to an object that is missing or loops
back on itself — so its parts cannot be told apart.
**What to do:** re-export the model as a fresh 3MF (or send the STL)
from the program that made it, then send it again. If it fails again,
run `/print-handy bundle` and share the bundle.

#### PH_PARSE_UNSUPPORTED_FORMAT {#ph-parse-unsupported-format}

**What failed:** that file is not a model Print Handy can slice.
Only STL and 3MF files are supported.
**What to do:** send the model as an `.stl` or `.3mf` file.

### Signature codes: intake

#### PH_INTAKE_GCODE_3MF_PACKAGE {#ph-intake-gcode-3mf-package}

**What failed:** that file is an already-sliced G-code package
(`.gcode.3mf`), not an unsliced 3MF mesh. Its build inventory is empty
because the model was already sliced, not because the file is corrupt.
**What to do:** send the original unsliced `.3mf` or `.stl` model
instead, so it can be sliced for this printer. Nothing was sliced or
started.

#### PH_INTAKE_UNCLEAR_UNITS {#ph-intake-unclear-units}

**What failed:** the model's measurement units are missing or
unclear, so its real size is not known.
**What to do:** send it again and answer the millimeters or inches
question, or re-export the model with its units set.

#### PH_INTAKE_UNTUNED_NOZZLE {#ph-intake-untuned-nozzle}

**What failed:** no tuned slicing profile exists for the selected
nozzle size.
**What to do:** select a nozzle size with a tuned profile, then send
the model again.

#### PH_INTAKE_TOO_COMPLEX {#ph-intake-too-complex}

**What failed:** the model is too large or detailed to process
safely — it passes a safety ceiling on size or element count.
**What to do:** simplify or reduce the mesh resolution in your
modeling program, then send it again.

### Signature codes: plate plan and slice

#### PH_PLATE_PLAN_OUTSIDE_BED {#ph-plate-plan-outside-bed}

**What failed:** the model does not fit inside the printer's build
volume at the size and position it was given.
**What to do:** pick a smaller size when asked, scale the model
down, or send a smaller model.

#### PH_PLATE_PLAN_EMPTY_PLATE {#ph-plate-plan-empty-plate}

**What failed:** the slicer found nothing to print on the plate. The
model is empty, hidden, or sits outside the bed.
**What to do:** check that the model has solid geometry and is not
off the plate, then send it again.

#### PH_SLICE_THIN_FEATURES {#ph-slice-thin-features}

**What failed:** the slicer could not build toolpaths for part of
the model, usually features thinner than one nozzle line.
**What to do:** thicken the thin walls or fine details in the model,
or send a different version.

#### PH_SLICE_OUT_OF_MEMORY {#ph-slice-out-of-memory}

**What failed:** the slicer ran out of memory on this model.
**What to do:** free memory on the host or send a simpler,
lower-resolution model, then retry once.

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
  or not configured, a 3MF length unit intake cannot convert exactly (#825) —
  and the chat post states that cause in plain
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

## Stage, cause and recovery for every model failure (#817)

A model failure posted to chat used to be "stopped before it was ready" or a slicer exit code. Every model failure now comes from one classification, `print_handy/core/failure_report.py`, and says four things. Since #924, a failure this classification recognizes also links its specific signature code (for example `PH_PARSE_BAD_3MF_STRUCTURE`) on the post's `Reference:` line instead of only the general subsystem marker — see the signature sections of the [Error code catalog](#error-code-catalog-one-linked-section-per-code-924):

- **Stage:** where it stopped: intake, parse, repair, color plan, plate plan, slice, upload or launch (`Stage: parse.`).
- **Cause:** in plain words, written by Print Handy from a recognized pattern. Known slicer signatures include an unreadable 3MF (Orca "can not find object id", "Invalid 3MF format", return code -6), a model outside the build volume, an empty plate, thin features Orca cannot slice, and an out-of-memory slicer.
- **Recovery:** the one thing to do next.
- **What was submitted:** "Nothing was uploaded or started." when the failure happened before anything left Print Handy, "The file is on the printer, but nothing is printing." when the printer rejected the start, and "The printer may already have the file or have started it" when the outcome is unknown (an upload or launch that was not confirmed).

The generic "Preparing that model stopped before it was ready." lead is kept only for a failure nothing recognizes, and it still names the stage and the bundle step. A slicer failure with no known signature says what the slicer reported only when that is one short sentence with no path, file name or traceback in it; otherwise nothing from the slicer's text is quoted. The raw logs stay in the archived failure folder (`<hermes_home>/state/slice-failures/<id>/`), and the Web UI status page lists the newest folders under "Recent model failures". Each receipt records the classification under `failure` (stage, cause, recovery, submitted, signature). A corpus guard (`tests/corpus/test_failure_messages.py`) fails if a known-bad corpus model ends in the generic message.

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
