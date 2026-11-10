# Multi-printer design: one instance, many printers

[Documentation home](../README.md) · [More than one printer today](../MULTI-INSTANCE.md) · [Listener domains](../LISTENERS.md) · Issue [#257](https://github.com/catduckgnaf/print-handy/issues/257)

**Status: design, not implemented.** This page is the output of the #257
Stage 2 design spike (2026-10-26). It records an audit of every place the
code assumes exactly one printer, the design decisions for true
multi-printer support in a single instance, and the staged implementation
plan. The stage issues linked from §5 are the implementation work; this
page changes no behaviour. Nothing here is a live-hardware claim — the
audit is of the code at the revision this page was written against, and
the design is verified by reading, not by running printers.

Where things stand:

- **Stage 0** (the multi-instance recipe, [MULTI-INSTANCE.md](../MULTI-INSTANCE.md))
  is shipped and stays valid forever as the alternative deployment:
  separate processes, separate bots, hard isolation.
- **Stage 1** (#256, merged) made one instance fully correct for one
  non-Moonraker printer: the supervisor starts only the listener children
  the configured connection needs.
- **Listener domains** (#299) landed the per-domain listener registry
  (`print_handy/listeners.py`) and the normalised status/event contract,
  and already committed to the shape this design builds on: *the domain
  selects the listener type; each configured printer gets its own
  listener instance, supervised and healthchecked independently*
  ([LISTENERS.md](../LISTENERS.md)).

## 1. The audit: where "one printer" lives

### 1.1 Settings and configuration

Every integration consumes one resolved settings object, and that is a
deliberate design: *"The resolved settings dictionary is the only data
structure that most integrations consume. There is no global state lookup
in those integrations."* (`print_handy/settings.py:604-606`). The printer
identity inside it is singular end to end:

- `[printer]` is one table: `model`, `name`, `vendor`, `firmware`,
  `slicer`, nozzle and bed geometry, IFS slots, start/end macros
  (defaults in `config/home.yaml:18-34`). `validate()` resolves exactly
  one model (`print_handy/settings.py:570-588`); when `model` is empty it
  silently adopts the first registry model (`settings.py:43-45, 582-584`).
- One adapter hangs off the resolution: `printers.current()` reads a
  single dict key (`print_handy/printers/__init__.py:154-166`), and
  `validate()` bundles one model + one vendor + one adapter into every
  consumer's hands (`settings.py:586-588, 610-622`). Callers with no
  printer argument are everywhere: the bot core, the wizard, the Web UI,
  the CLI doctor (`print_handy/doctor.py:18-26`).
- Connection settings are per-printer facts wearing global clothes:
  `services.moonraker_url`, `services.octoprint_url` and the camera URL
  (`config/home.yaml:65-72`), plus one section per vendor — `[bambu]`
  `lan_host`/`lan_serial` (`settings.py:96-99, 269-299`), `[prusa]`
  `link_host` (`settings.py:316-332`), `[elegoo_sdcp]` `host`
  (`settings.py:334-348`), `[flashforge_stock]` `host`
  (`settings.py:100, 301-314`). Each vendor validator takes the one
  resolved model and refuses to apply to any other vendor; the
  cross-check that URL-based services stay empty for offline / Bambu LAN
  / stock / PrusaLink / SDCP connections is likewise written against the
  single model (`settings.py:507-509`). The validators already accept a
  `model` argument — they generalise to *(entry, model)* without logic
  changes.
- The environment layer is global and applied last:
  `load()` is `_merge(defaults, _merge(home, _merge(local, env)))` and
  `_from_env` runs over the merged result (`settings.py:625-643`).
  Environment names are exactly two-part
  (`PRINT_HANDY__SECTION__KEY`); anything else is an error. Lists cannot
  come from the environment at all (`settings.py:193-195`), and a config
  file is mandatory whenever a list setting exists (`settings.py:638-641`).
  Consequence: per-printer values cannot ride the existing env mechanism
  — a printers list is file-born, and per-entry env overrides need their
  own addressing pass.
- Parity is enforced mechanically: `tests/test_docker_release.py`'s
  `SettingsParityTests` builds the settings inventory by walking the
  *defaults tree* (`config/home.yaml`) and then requires a
  `docs/SETTINGS.md` row and a `.env.example` line (or an explicit
  "file only" ruling) for every key. Anything added to the defaults tree
  silently inherits those obligations; a section that never appears in
  the defaults is invisible to the inventory and needs explicit test
  treatment instead.
- The setup surfaces hardcode the singleton: the setup wizard
  writes exactly four printer answers, one of them the model
  (`.env.example:26`, the wizard's `_ask_printer_model`), the guided
  setup asks one model / one Moonraker URL and names the file for "the
  printer" (`print_handy/bot/guided.py:139, 145, 162`), and the Docker
  variable tests pin the singular placeholder spelling
  (`tests/test_docker_release.py`, DockerFeatureVariablesTests). The Web
  UI catalog addresses settings by dotted *path*
  (`print_handy/webui/catalog.py:171-180`); a path that resolves into a
  list entry does not resolve at all today — its field model has no way
  to name "the second printer's host".

### 1.2 Core pipeline: identity, leases, records

- **A job does not know its printer.** `JOB.json` stores `source_model`
  and `model` — the same value twice (`print_handy/core/model_job.py:1550-1551`)
  — and no printer identity anywhere. Everything downstream re-derives
  "the printer" from ambient settings: the adapter is
  `printers.current()` in the state check, the guards, the delivery
  branches (`model_job.py:970, 980`) and the spool guard.
  One honourable exception, and the precedent this design follows: the
  uploaded-G-code print path resolves its adapter *from the job record's
  own model* (`model_job.py:904`), not from settings.
- **The launch lease is one row in one database.** The launch state
  lives in a single SQLite database at the state root,
  `print_buddy_launches.sqlite3` (the #157 layout; renamed
  `print_handy_launches.sqlite3` with the project). Its `leases` table
  is keyed by `_printer_key`: the printer's Moonraker URL, or its model
  key when no URL applies — there is no literal `"printer"` key and no
  `model_key` column. `LaunchState` is constructed at five
  sites that would all need the printer identity: the print path
  (`model_job.py:961`), the listener process
  (`print_handy/hermes/listener_process.py:505`), two watcher paths
  (`print_handy/hermes/printer_events.py:1108, 1339`) and a bot command
  (`print_handy/bot/core_commands.py:385`). Load-filament claims the same
  single lease (`print_handy/core/load_filament.py:141-151`).
- **Approvals are one row per instance.** The pending-approval table
  holds a single row, replaced wholesale (`print_handy/core/print_claims.py:74-79`);
  a claim is already bound to `job_id` + approval event id, so a stale
  or cross-job approval is refused — but two *concurrent* approvals for
  two printers would collide in the table before leases are even
  consulted. (Print start claims, by contrast, are already keyed per
  `(job_id, ident)` — `print_claims.py:143-153`.)
- **The hardware record is a singleton file with process-wide caches.**
  One `printer_hardware.json` per state directory
  (`print_handy/core/printer_hardware.py:24, 324`); its loader caches by
  path+mtime process-wide (`:27-40`) and the last-good snapshot `_LAST_GOOD`
  is one global dict (`:192-215`). Today the only thing keeping two
  printers' records apart is that they live in different processes
  (Stage 0 rule 1). Filament/IFS runtime state is even less visible: it
  exists only in bot-process memory (`print_handy/hermes/filament.py:33-61`),
  rebuilt at startup from the hardware record.
- **Storage is closer than it looks.** Job folders already live at
  `archive/<job_id>` with instance-unique ids
  (`print_handy/state_files.py:20-24`); spool bindings are stored per
  spool id (`state_files.py:98-117`); the Orca profile cache is keyed by
  content hash (`print_handy/core/orca_profiles.py:15-38`) and the slicer
  runs with the job folder as its working directory — all safe to share.
  The singleton files are: `printer_hardware.json`, the launch database
  `print_buddy_launches.sqlite3`,
  `selected_spools.json` (`model_job.py:968-973`), `maintenance.json`,
  the listener state/heartbeat files (§1.3), and the platform pairing
  database.
- **Spoolman is one URL, constructed per call.** There is no shared
  client object: the print path, the hardware record and the listener
  snapshot each build a client from the one `services.spoolman_url`
  (`model_job.py:970`, `printer_hardware.py:335-346`). Which spool is
  loaded is *device* state inside Spoolman plus the per-printer fields
  of the hardware record (`printer_hardware.py:356-359`). Concurrent
  prints on different printers with different spools need no new lock;
  the hazard to keep refusing is the *same* spool recorded as loaded on
  two printers.

### 1.3 Runtime: bot, listener, supervisor

- **Discord routing is one integer.** The bot holds one `chat_id`
  (`print_handy/bot/runtime.py:208-209`); a message in any other channel
  is dropped (`runtime.py:160-161`), and so is an interaction
  (`runtime.py:245-255`). Authorisation is the configured allowed-user
  set (`print_handy/bot/platform.py:270-279`). But the conversation
  layer underneath is already multi-channel: sessions are keyed
  `(chat_id, user_id)` (`print_handy/bot/sessions.py:19-20`) and
  conversation records are per `chat_id`
  (`print_handy/core/conversation.py:45-62`). Two latent issues for a
  multi-channel world: platform replies are keyed by *message id only*,
  chat not included (`platform.py:301`), and one approval prompt's
  buttons resolve through the singleton claims table (§1.2).
- **The listener process is structurally single-connection.** The
  Moonraker listener keeps module-level mutable globals for its socket,
  heartbeat thread and "one Moonraker WS URL"
  (`print_handy/hermes/moonraker_event_listener.py:128-163`); its `main()`
  takes no printer identity (`:1676`); the process wrapper keeps exactly
  one status slot (`print_handy/hermes/listener_process.py:78-79, 157, 185`).
  There is no event queue: consumers read the heartbeat file
  (`print_handy/healthcheck.py:12-21`) — one fixed filename,
  `listener_health.json` (`healthcheck.py:23`) — and the container
  supervisor creates exactly one heartbeat touch file
  (`docker/supervise.sh:19`). The listener is also not just an observer:
  its print watcher consumes approvals and claims the lease
  (`printer_events.py:1041-1046, 1108`).
- **The #299 registry is the ready-made seam.** `print_handy/listeners.py`
  maps every connection to a `ListenerDomain` with a three-valued
  availability (`:40-55`), enforces completeness against the printer
  registry at import (`:83-89`), and its launcher resolves the domain for
  *the* configured adapter with the gate's 0/3/2 exit codes
  (`:148-181`). The gate (`print_handy/listener_gate.py:17-29`) and the
  supervisor's consumption of it (`docker/supervise.sh:29-46`) derive
  from the registry, so per-printer instances plug into an existing
  contract instead of a new one.
- **Supervision is all-or-nothing.** Today's children are: bundled
  Spoolman (flag-gated), at most one listener (gate-gated), and the bot;
  `wait -n` means *any* child exit stops the container
  (`docker/supervise.sh:79-83`), and the healthcheck aggregates bot +
  Spoolman + the one listener row (`docker/healthcheck.py:113-116`).
  With N listener children, the #256 lesson — one printer's failure must
  never take down the others — requires listener children to leave the
  `wait -n` contract.
- **The camera watchdog reads the one heartbeat**
  (`print_handy/hermes/visual_watch.py:149-150`), and bot status answers
  by checking the one current printer live
  (`print_handy/bot/status_text.py:52-53, 118-146`). Timelapse work is
  already per job folder, so completions generalise once the events that
  trigger them carry a printer identity; completion/start-claim
  callbacks are already keyed by `(job_id, source_message_id)` with an
  opaque `ident` (`print_handy/hermes/listener_process.py:834-896`) —
  the existing seam for printer routing.

### 1.4 What already generalises

Adapters are stateless and re-instantiable from any model key
(`print_handy/printers/__init__.py:47-51, 137-151`); the registry knows
every model's identity (`print_handy/printers/base.py:43-106`); per-vendor
settings validators already take a model argument; sessions and
conversations are keyed by chat; job folders, spool bindings and the
Orca cache are content- or id-keyed. The singleton is concentrated in
four places: the resolved-settings bundle, the lease/claims database,
the hardware record, and the chat/listener runtime wiring. The staged
plan in §5 follows that concentration.

## 2. Decisions

Each decision is stated with the alternative that was rejected. These
are the spike's recommendations; the stage issues in §5 implement them
unless catduck directs otherwise on #257.

### 2.1 Config shape: one `[[printers]]` list

**Decision.** A config file declares printers as a **list of entries**.
Each entry carries:

| Key | Meaning |
| --- | --- |
| `id` | Required. Stable slug (`a-z`, `0-9`, `-`, `_`; starts alphanumeric). Unique in the list. This is the printer's identity everywhere in §2.2. |
| `model` | Required. A registry model key, resolved exactly as `[printer] model` is today (including the refusal of unknown keys). |
| `name` | Optional display name (approvals, status, logs); defaults to the model label. |
| `chat_id` | Optional. This printer's channel; falls back to the global `discord.chat_id` (see §2.3). |
| connection keys | The keys that belong to this printer's vendor, spelled as today but nested in the entry: `moonraker_url`, `octoprint_url`, `camera_snapshot_url` (from `[services]`); `lan_host`, `lan_serial`, `lan_access_code_env`, `start_confirm_s` (from `[bambu]`); `link_host`, `api_key_env` (from `[prusa]`); `host` (from `[elegoo_sdcp]` and `[flashforge_stock]`, disambiguated by the entry's model, exactly as the vendor sections are today). |
| `nozzle_type`, `tool_count` | Optional per-printer overrides, as today in `[printer]`. |

Everything else stays global and singular: `[discord]` (token env,
allowed users, timeouts), `services.spoolman_url` and the other service
URLs that are not per-printer, `[orca]`, `[slicer]`, `[delivery]`
methods, `[paths]`, `[listener]` tuning, `[bot]`, `[llm]`, `[vision]`,
`[timelapse]`, `[visual_watch]`, `[maintenance]`, `[logging]`,
`[webui]`, and the material/temperature tables. The remaining `[printer]`
keys (firmware, vendor, slicer, geometry defaults, macros) stay global
defaults — they are either derived from the model or install-wide
preferences; if a per-printer override is ever needed it joins the entry
without a shape change.

Validation follows the code that exists: each entry is validated by the
same per-vendor validators that run today, now called with the entry's
own model (they already take a model argument); an entry may not carry
another vendor's keys; ids must be unique; two entries may not claim the
same `chat_id` (§2.3). When a `[[printers]]` list exists, the legacy
per-printer locations (`[printer]`, `[bambu]`, `[prusa]`,
`[elegoo_sdcp]`, `[flashforge_stock]`, and the per-printer keys of
`[services]`) must be absent or empty — the same refusal style as
today's cross-checks — so a config can never describe two different
rosters.

**Migration.** Migration is at load time, in memory; config files are
never rewritten by loading. If `[printers]` is absent, `validate()`
synthesises exactly one entry from today's shape: id = slug of
`[printer] name` when set, else `"printer"`; model = the resolved model
*including today's first-model fallback*; the vendor-section and
`[services]` connection keys fold into that entry. The resolved settings
then expose both the roster (`settings.printers()` / `by_id(id)`) and
the **legacy singleton view** — `data["printer"]`, the `[services]`
URLs, and `printers.current()` — populated from the single entry, or
from the first entry when a list exists. Untouched consumers therefore
behave identically for every existing install, and a one-entry
`[[printers]]` config takes the same path. The Web UI catalog keeps
addressing global fields only in v1: its dotted-path field model cannot
name a list entry (`webui/catalog.py:171-180`), so per-printer fields
are read-only there until a per-printer addressing scheme is designed
(stage 2e decides or explicitly defers it — **decided in stage 2e
(#337): explicitly deferred**. The catalog keeps addressing global
fields only; the status page lists the roster read-only, and roster
edits stay in the config file and the setup surfaces. Recorded in
docs/WEBUI.md). The settings-parity tests
need one deliberate change: the inventory walk must treat `[printers]`
explicitly (validate its shape in tests) rather than silently ignoring
a table that is not in the defaults tree; `docs/SETTINGS.md` gains a
hand-written `[printers]` section.

**Environment.** The list itself is **file-only**, by the loader's own
rule: lists cannot come from the environment
(`settings.py:193-195, 638-641`). Scalar keys of a *declared* entry may
be overridden from the environment with an indexed spelling,
`PRINT_HANDY__PRINTERS__<ID>__<KEY>` (the id uppercased), validated like
any other env setting. The environment can never *create* an entry —
mirroring `_from_env`'s refusal of unknown keys — because Docker and
Unraid operators, the very users who configure by environment, still
need one small config file that names the roster; everything about a
printer that is not a scalar (nothing in v1) stays file-only regardless.

**Rejected alternative: named sections** (`[printers.<id>]`, a table of
tables). The codebase has a precedent for name-chosen tables
(`settings.py`'s `OPEN_SECTIONS`), and the difference is honestly
modest. The list wins on three points: roster order is explicit (the
first entry is the default, §2.6); a printer id is *data* with one slug
rule, not a key whose spelling two parsers (YAML-first, TOML legacy)
treat differently; and every consumer iterates a list naturally. The
cost — slightly more verbose YAML — is accepted.

### 2.2 Printer identity: the id, end to end

**Decision.** The entry `id` is the printer's stable identity. It is
immutable in practice: renaming an id makes the new id a *different*
printer as far as state is concerned (§2.4), and the docs say so.
Renaming the display `name` is free.

A job learns its printer at intake and never re-derives it:
`JOB.json` gains `printer_id` next to the existing `source_model` /
`model` pair, stamped when the job is created (the channel determines
the printer, §2.3). Slicing resolves the adapter from the entry's model
(`adapter_for`), delivery and the print paths resolve the adapter from
the *job record's* printer — generalising the one path that already
works this way (the uploaded-G-code path, `model_job.py:904`). Start
claims, approval claims, completion callbacks and watchdog events carry
the printer id in their existing keyed payloads (the opaque `ident` of
the completion callbacks gains it). Everything user-visible names the
printer by display name: preview, approval prompt, status answer,
completion post — the pattern `status_text.py:52-53` already uses.

**Rejected alternative: identify printers by model key.** Two printers
of the same model is a normal household (two A1s); model is a *type*,
not an identity. Name-as-identity was also rejected: names are display
text, edited freely, and may collide or contain spaces.

### 2.3 Discord routing: one bot, one channel per printer

**Decision.** One bot serves every printer. Routing generalises the
mechanism that already keeps instances apart:

- At startup the bot builds a **channel → printer map** from the roster:
  each entry's `chat_id` (falling back to the global `discord.chat_id`)
  claims that channel for that printer. Two entries claiming the same
  channel is a startup configuration error — the Stage 0 rule "never
  share an approval channel" stops being a deployment discipline and
  becomes a validated invariant. *(Amended by #536: a duplicate claim is
  no longer an error; it makes the channel shared — see the supersession
  note below.)*
- A message or interaction in a mapped channel is handled for that
  channel's printer: intake stamps the job with that printer, questions
  and approvals post in that channel, and the approval claim binds
  `(job_id, printer_id, approval_event_id)`. The existing drops
  generalise from "not my channel" to "not a mapped channel"
  (`runtime.py:160-161, 245-255`).
- A message in an **unmapped** channel is dropped silently, exactly as
  today. The bot gains no new surface in channels nobody configured.
- Sessions and conversations need no redesign: they are already keyed
  by `(chat_id, user_id)` / `chat_id`, so per-channel means per-printer
  by construction. The platform reply map must be re-keyed to include
  the chat (today it is message-id only, `platform.py:301`) so replies
  in two channels can never cross.
- Telegram chats and Matrix rooms follow the same map through their own
  chat ids; the rule is platform-agnostic, as Stage 0 already documents
  it for instances.

**Rejected alternative: a shared channel with explicit targeting**
("print this on the garage printer"). Out of scope for v1, by design:
an approval is a button press, and a button carries no typed target —
the entire safety model of the approval flow is that the channel *is*
the target. A targeting design would have to bind every prompt,
question and button to an explicit printer and prove the binding under
concurrency; that is a separate proposal, not a flag on this one.
Per-printer bots (one token each) were also rejected as the *required*
shape: the channel scoping already isolates printers within one bot
(the Stage 0 finding), and one bot is the simpler household deployment.
Nothing in the map prevents a user from still running separate
instances instead (§2.6).

> **Superseded in part by [#536](https://github.com/catduckgnaf/print-handy/issues/536)
> (2026-10-28).** The separate proposal this section called for exists and
> was adopted: a channel claimed by two or more roster entries is a
> **shared channel**. It is served, but the channel decides no printer —
> dropping a model link asks *which printer* before any printer state
> loads, job-bound commands resolve the printer from the job record,
> "current print" commands follow which printer is actually printing
> (asking when more than one is), and a shared channel never falls
> through to the default printer. The binding proof the rejection asked
> for is the prompt keying: a decided flow's questions register under a
> printer-scoped session key, and typed answers resolve nothing when two
> printers' prompts are open. The rest of this section stands unchanged:
> a channel claimed by exactly one entry still routes to that printer
> end to end, an unmapped channel still gets silence, and the duplicate
> claim is no longer a startup error — it is precisely how a shared
> channel is configured.

### 2.4 State and concurrency: per-printer state, per-printer leases

**Decision.** One state root (the `[paths]` layout is unchanged), with
a per-printer subdirectory `printers/<id>/` holding everything that is
about one machine:

- `printer_hardware.json` — that printer's hardware record. Its loader
  caches and the last-good snapshot become keyed by printer (today they
  are process-wide globals, §1.2).
- `jobs.db` — that printer's launch lease and approval claims. For the
  default printer it is seeded by copying the legacy launch database
  (`print_handy_launches.sqlite3`) whole — approval rows and any held
  lease row intact, under their existing keys — rather than by rekeying
  rows to the printer id; leases taken after the move are keyed by the
  job's printer id. Every existing query is otherwise unchanged,
  including busy detection.
- `selected_spools.json`, `maintenance.json` — per printer, as their
  contents are per machine.
- Listener state and heartbeats (`listener_health.json`,
  `listener_state.json`, the bot heartbeat) — §2.5.

Job folders stay in the shared archive at `archive/<job_id>`: ids are
instance-unique, `printer_id` inside `JOB.json` is the key, and the
records/vault tooling walks one flat archive. Spool bindings stay per
spool in the shared store. Platform pairing state stays global.

Compatibility for existing installs: on first start, a printer entry
whose per-printer files do not exist yet adopts the legacy root-level
files (read from the legacy location; new writes go to the per-printer
location; the legacy files are left in place for one release so a
rollback still finds them). The launch database is the exception in
mechanism, not in spirit: only the default printer adopts it, by
copying `print_handy_launches.sqlite3` whole into its `jobs.db` —
copying it to a second printer would duplicate its held lease and
consumed approvals. For the migrated single entry this is a
pure relocation; nothing about the *contents* changes.

**Concurrency.** The lease is per printer, so prints on different
printers proceed in parallel and a busy printer refuses only its own
starts — today's semantics, scoped correctly. An approval can only be
consumed by the job and printer it was issued for. Load-filament claims
the target printer's lease only. Slicing is already isolated per job
folder, the Orca cache is content-keyed, and timelapse rendering per
completion is CPU contention to note for operators, not a correctness
issue. Filament/IFS in-memory state becomes a per-printer map in the
bot process, restored at startup from each printer's hardware record.

**Rejected alternative: one shared `jobs.db` with a printer column.**
Every lease/claim query site (five `LaunchState` constructions, the
claims module, the listener's watcher) would need a new filter
discipline, and a mistake leaks across printers. Per-printer files keep
every query as written, keep one printer's state resettable without
touching another's — the strongest property of the Stage 0 recipe —
and bound the blast radius of a corrupt state file to one printer.

### 2.5 Event sources: one listener instance per printer

**Decision.** This layer follows #299, which already decided it: the
domain selects the listener *type*; **each printer gets its own
listener instance**, launched through the existing registry launcher
with a printer selector (`python3 -m print_handy.listeners --printer
<id>`), keeping the 0/3/2 gate exit codes per instance. A printer whose
domain is `planned` or `none` in the registry simply has no listener
child — the same normal state as a single-printer install today.
Multi-printer adds no new domain listeners; Bambu, PrusaLink and SDCP
listeners arrive on #299's own staging (Stages B and C) and light up
per printer when they land.

Heartbeats and listener state move under `printers/<id>/`, so the
healthcheck rule generalises per printer: a heartbeat is required
exactly when that printer's domain is implemented. Consumers iterate
the roster: bot status aggregates per printer, the camera watchdog runs
per printer against that printer's heartbeat, and completion events
post to the printer's channel carrying its id (§2.2).

Supervision changes in one deliberate way. The supervisor enumerates
the roster: bundled Spoolman (unchanged, global), the one bot, and one
listener child per printer whose domain is implemented. The bot and
Spoolman remain *required* children — their exit stops the container,
as today. **Listener children leave the `wait -n` contract**: a
listener that exits is restarted for its printer only (its own
reconnect discipline applies), and its healthcheck row reports that
printer unhealthy in the meantime. One printer's listener can never
stop the container or another printer's listener — the #256
failure-isolation lesson, applied at N printers. The container
healthcheck aggregates: bot + Spoolman + one row per printer, healthy
iff every required row is healthy.

**Rejected alternative: one listener process hosting all connections.**
The Moonraker listener is module-global, single-connection code
(§1.3); hosting N connections in it means refactoring every domain
listener into a multi-tenant event loop and giving up failure
isolation between printers — the opposite of the direction #256 and
#299 established. Per-printer processes cost a few megabytes each and
keep every existing single-connection implementation valid unchanged.

### 2.6 Spoolman: one inventory, confirmed

**Decision.** Nothing about Spoolman becomes per-printer. There is one
`services.spoolman_url`, one inventory, and the clients keep being
constructed per call from that URL (there is no shared client object to
split). Spool assignment stays per job: the job record names its slots,
the per-printer hardware record names what is physically loaded on that
printer, and Spoolman's own device assignment is the arbiter when the
two are compared at readiness. Two printers printing concurrently from
different spools is the normal case and needs no new lock; the one
invariant to keep enforcing is that the same spool is never recorded as
loaded on two printers at once. This is the Stage 0 sharing model,
unchanged — multi-printer in one instance gets it for free because the
inventory was never the singleton problem.

### 2.7 Compatibility and rollout

**Decision.** Existing installs are the design constraint, not an
afterthought:

- Every config that loads today loads after Stage 2a with a resolved
  settings object that is identical in every value a consumer can read
  (the legacy singleton view of §2.1). The golden settings snapshot
  tests are extended to prove this, not weakened to pass.
- Single-printer behaviour is byte-identical where behaviour is
  observable: same prompts in the same channel, same job records plus
  one additive `printer_id` field, same gates in the same order.
- [MULTI-INSTANCE.md](../MULTI-INSTANCE.md) remains the supported
  alternative deployment. One instance is a *choice* (one bot, one
  config, one Spoolman client view, combined status), not a migration
  users are pushed into; households that want hard process isolation
  keep it.
- Rollout is the staged plan in §5: each stage is independently
  mergeable, keeps single-printer installs working, and can stop after
  any stage with a coherent product.

## 3. A two-printer configuration

The motivating household — a Bambu A1 next to a Flashforge AD5X — in the
Stage 2 shape. One config file, one container, one bot, two channels,
one shared Spoolman:

```yaml
# Global sections are written once, exactly as today.
discord:
  chat_id: 100000000000000001   # default channel; the AD5X entry uses it
  allowed_user_ids: [200000000000000002]
services:
  spoolman_url: http://spoolman.example:7912
delivery:
  methods: [discord, printer]

printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    chat_id: 100000000000000003  # #a1-prints
    lan_host: 192.0.2.60
    lan_serial: "00M00A000000000"
    lan_access_code_env: BAMBU_LAN_PASSWORD
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    # no chat_id: falls back to the global discord.chat_id (#ad5x-prints)
    moonraker_url: http://192.0.2.61:7125
```

What this buys over the Stage 0 recipe: one container and one bot
token instead of two of everything; approvals in `#a1-prints` can only
ever start the A1 because the channel is bound to `garage-a1` at
startup; both printers draw on the one Spoolman inventory; the
container runs a listener child only for the AD5X (the A1's
`bambulab_lan` domain has no implemented listener yet, per the #299
registry — it gains one automatically when #299 Stage B lands); and
state lives at `printers/garage-a1/…` and `printers/workshop-ad5x/…`
under the one data directory. A pre-Stage-2 config for either printer
alone loads unchanged and behaves as the one-entry roster.

## 4. Non-goals for this design

- **A shared channel with explicit printer targeting** — rejected in
  §2.3; needs its own proposal with a binding proof. *(That proposal is
  [#536](https://github.com/catduckgnaf/print-handy/issues/536), adopted
  2026-10-28 — see the supersession note in §2.3.)*
- **Cross-printer job failover** (a job started on one printer is never
  silently re-targeted to another; the printers differ physically).
- **A combined "print to whichever is free" queue.** Jobs belong to a
  printer from intake (§2.2).
- **New listener domains.** Bambu/PrusaLink/SDCP listeners are #299
  Stages B/C; this design consumes them per printer as they land.
- **Per-printer Spoolman, per-printer bots as a requirement, and
  per-printer delivery-method overrides** — delivery methods stay
  global in v1 (§6, question 2).

## 5. Staged implementation plan

Five stages, each an independently mergeable PR series with its own
issue. Order is dependency order; no stage changes user-visible
behaviour for a single-printer install.

### Stage 2a — Config model and migration (no behaviour change) — [#333](https://github.com/catduckgnaf/print-handy/issues/333)

- **Scope.** `[[printers]]` parsing and per-entry validation in
  `print_handy/settings.py` (entry slug rule, uniqueness, per-vendor
  validators generalised to *(entry, model)*, legacy-shape refusal when
  both shapes are present); in-memory migration synthesising the
  one-entry roster from today's config; the roster API
  (`settings.printers()`, `by_id`) and the legacy singleton view
  (`printers.current()` and friends read the single/first entry);
  indexed env overrides for declared-entry scalars
  (`PRINT_HANDY__PRINTERS__<ID>__<KEY>`), file-only roster; the parity
  test treatment for `[printers]` and a hand-written `[printers]`
  section in `docs/SETTINGS.md`.
- **Key files.** `print_handy/settings.py`, `print_handy/printers/__init__.py`,
  `config/home.yaml`, `config/docker.yaml`, `.env.example`,
  `docs/SETTINGS.md`, `tests/test_docker_release.py`,
  `tests/test_settings.py` (golden snapshots).
- **Risks.** The golden settings snapshot and the deliberate
  one-settings-object contract (`settings.py:604-622`) — the legacy view
  must be *provably* identical, or every later stage inherits a
  behaviour change nobody chose. Env spelling collisions with the
  two-part parser must be refused loudly, not misread.
- **Acceptance.** Every config that loads today resolves to identical
  values (golden tests extended with a two-entry fixture and a
  legacy-shape fixture, none weakened); a two-entry config validates
  and each entry refuses another vendor's keys; mixed legacy+list input
  is refused with a named error; env can override a declared entry's
  scalar and cannot create an entry; `printers.current()` is unchanged
  for all existing callers.

### Stage 2b — Per-printer state, records and leases — [#334](https://github.com/catduckgnaf/print-handy/issues/334)

- **Scope.** The `printers/<id>/` state layout (§2.4): hardware record
  per printer with per-printer loader/last-good caches; per-printer
  `jobs.db` with the lease row keyed by the real id; approval claims
  bound to `(job_id, printer_id, event_id)`; all five `LaunchState`
  construction sites pass the identity; load-filament targets a
  printer's lease; filament/IFS in-memory state keyed by printer;
  `selected_spools` and `maintenance` per printer; `JOB.json` gains
  `printer_id` at creation and the delivery/print paths resolve their
  adapter from the record (the `model_job.py:904` precedent);
  `printer_state.check` takes entry+adapter and reports the printer id;
  legacy state adoption (read legacy location, write per-printer,
  legacy files left in place for one release).
- **Key files.** `print_handy/core/launch_state.py`,
  `print_handy/core/print_claims.py`, `print_handy/core/printer_hardware.py`,
  `print_handy/core/model_job.py`, `print_handy/core/printer_state.py`,
  `print_handy/core/load_filament.py`, `print_handy/hermes/filament.py`,
  `print_handy/hermes/printer_events.py`,
  `print_handy/hermes/listener_process.py`, `print_handy/state_files.py`.
- **Risks.** The listener process is a print *actor* (it consumes
  approvals and claims the lease), so identity plumbing must reach
  inside it, not just the bot paths. State adoption must never lose a
  held lease or a hardware record on upgrade.
- **Acceptance.** Offline tests print concurrently on two fake printers
  (independent leases; a second start on the *same* printer is refused
  busy exactly as today); an approval issued for printer A cannot be
  consumed by printer B's job (explicit test); a job record created
  before the upgrade (no `printer_id`) still prints on the single
  configured printer; an existing single-printer state directory is
  adopted with lease and hardware record intact.

### Stage 2c — Discord routing — [#335](https://github.com/catduckgnaf/print-handy/issues/335)

- **Scope.** The channel → printer map built at bot startup from the
  roster (duplicate claim = startup error — *superseded by #536: a
  duplicate claim makes the channel shared, not an error*);
  `runtime.py` routing
  generalises from one `chat_id` to the map (unmapped = dropped, as
  today); the platform reply map re-keyed with the chat
  (`platform.py:301`); intake stamps the channel's printer into the
  job; questions/approvals/completions post in the printer's channel
  and name the printer; status commands answer for their channel's
  printer (a roster-wide status answer is additive, in the default
  channel only). Telegram/Matrix ride the same map.
- **Key files.** `print_handy/bot/runtime.py`,
  `print_handy/bot/platform.py`, `print_handy/bot/status_text.py`,
  `print_handy/bot/core_commands.py`, `print_handy/core/conversation.py`.
- **Depends on.** 2a (roster) for routing; the print-*starting* half
  also needs 2b (claims/leases keyed by printer).
- **Risks.** Any path that silently falls back to "the current printer"
  instead of the channel's printer is a wrong-printer safety bug; the
  audit's no-arg `printers.current()` callers must be swept, not
  sampled.
- **Acceptance.** Two channels drive two fake printers end-to-end
  offline (intake → approval → start → completion post, each in its own
  channel); a button press in channel A can never start a job on
  printer B (explicit test); silence in unmapped channels; the full
  existing bot suites pass unmodified for the single-channel case.

### Stage 2d — Event sources, supervisor and healthcheck — [#336](https://github.com/catduckgnaf/print-handy/issues/336)

- **Scope.** The registry launcher gains a printer selector
  (`python3 -m print_handy.listeners --printer <id>`); heartbeat and
  listener-state paths move under `printers/<id>/`; bot status
  aggregation and the camera watchdog iterate the roster;
  `docker/supervise.sh` enumerates listener children per printer with
  restart-not-fatal semantics (bot and Spoolman remain the required
  children); `docker/healthcheck.py` aggregates one row per printer.
- **Key files.** `print_handy/listeners.py`,
  `print_handy/listener_gate.py`, `print_handy/healthcheck.py`,
  `print_handy/hermes/moonraker_event_listener.py` (identity argument),
  `print_handy/hermes/visual_watch.py`, `docker/supervise.sh`,
  `docker/healthcheck.py`, `tests/test_listener_gate.py`,
  `tests/test_combined_supervisor.py`.
- **Depends on.** 2a + 2b. Non-Moonraker domains light up per printer
  whenever their #299 stage lands; this stage is not blocked on them.
- **Risks.** Supervisor semantics are safety-adjacent: a listener
  restart loop must degrade to "that printer unhealthy", never to a
  container restart loop (the #256 failure shape) — backoff and a
  restart budget are part of the stage, not polish.
- **Acceptance.** A container-shaped test with two Moonraker printers:
  both heartbeats required and checked; killing one listener restarts
  only that child and the container stays up; a printer whose domain
  has no listener adds no health requirement; single-printer container
  behaviour and health output are unchanged.

### Stage 2e — Setup, Web UI decision and docs — [#337](https://github.com/catduckgnaf/print-handy/issues/337)

- **Scope.** `docs/DOCKER.md` gains the one-instance multi-printer
  shape; [MULTI-INSTANCE.md](../MULTI-INSTANCE.md) is reframed as the
  alternative deployment (hard isolation) rather than the only answer;
  `docs/SETTINGS.md` completed for the roster; setup wizard and guided
  setup learn an add-a-printer loop that writes `[[printers]]` entries
  (and the env write-back spelling for them); the Web UI catalog either
  gains a per-printer addressing scheme or the stage lands an explicit,
  documented deferral with a read-only roster view; the two-printer
  example of §3 is load-checked offline as a walkthrough, mirroring
  Stage 0's verification.
- **Key files.** `docs/DOCKER.md`, `docs/MULTI-INSTANCE.md`,
  `docs/SETTINGS.md`, `print_handy/setup_wizard.py`,
  `print_handy/bot/guided.py`, `print_handy/webui/catalog.py`.
- **Depends on.** 2a–2d.
- **Risks.** Setup write-back is the one place configs are *written*;
  a bad write-back breaks a working install, so the wizard must refuse
  to rewrite a config it cannot round-trip.
- **Acceptance.** The §3 example config loads with both entries
  resolved, distinct channels and the shared Spoolman (offline
  walkthrough, as Stage 0 did for instances); wizard round-trip tests
  for one-printer and two-printer configs; docs index and cross-links
  updated.

## 6. Open questions for catduck

The decisions in §2 are recommendations the stage issues will implement
as written unless directed otherwise. Four points are genuinely
catduck's call:

1. **The default printer.** Where a singleton is unavoidable (the CLI
   doctor, a Web UI landing view, `printers.current()` during the
   transition), the design uses the **first entry** and names it in
   output. Alternative: refuse and require an explicit printer choice
   everywhere. *Recommendation: first entry, named — it is what makes
   migration invisible to existing installs.*
2. **Per-printer delivery methods.** V1 keeps `[delivery] methods`
   global (a household that enables printer delivery enables it for
   every printer, each still behind its own approval). *Recommendation:
   global for v1; per-entry override is a small later addition if a
   mixed-trust household appears.*
3. **Shared-channel targeting stays out.** §2.3 rejects one channel
   serving two printers even with explicit targeting, on approval-safety
   grounds. *Recommendation: confirm as a standing non-goal, so no stage
   designs toward it.* *(Resolved the other way by
   [#536](https://github.com/catduckgnaf/print-handy/issues/536)
   (2026-10-28): shared channels were adopted with the ask-first model —
   see §2.3's supersession note.)*
4. **All platforms at once.** The routing map is platform-agnostic, so
   Telegram and Matrix get channel-per-printer in the same stage as
   Discord. *Recommendation: yes, one rule for every platform — the
   Stage 0 recipe already documents the rule that way for instances.*
