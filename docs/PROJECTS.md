# Multi-part print projects

A model can be made of several printable components. Print Handy already
supports selecting parts/versions from a model-site link, combining them into
a geometry-only 3MF where appropriate, and making a multi-plate print plan.
The same behavior now applies to **several STL/3MF files attached to one
Discord message**. This feature needs no configured AI provider.

## Upload a multi-part model

Attach the STL/3MF files together in one message. For example, select the
center tray, left arm and right arm from a rack mount. Print Handy inventories
each as a separate source rather than ignoring the message because there are
too many attachments. You may choose all required parts or a subset. It
recognizes common filename alternatives such as with/without logo, without
silently discarding left/right pairs.

The chosen files are copied separately to the archived job, even if two
original attachments share the same filename. The existing multi-file
combiner then tries to place them together at original scale; the normal
printer fit, slot/color, slice and approval questions still apply. The
combined 3MF is an intermediate print input, **not an assembled CAD model**.

A single STL/3MF upload works as before. A model link in the same message
still takes precedence over attached files.

## Collect a project over several messages

When the parts do not fit in one message, collect them:

1. Send the first part(s) with the words **add to project** (or type
   **start a project** first). Print Handy answers with the project's file
   list and two choices: **Done adding** and **Cancel project**.
2. Send the other parts in later messages in the same chat or thread. Each
   upload is asked **Add to project** or **Print it on its own**; nothing
   joins the project unless you choose Add to project (or the upload's own
   text says "add to project"). Print it on its own runs the normal
   single-upload intake for that file, as if no project were open.
3. Press **Done adding** (or type **done adding**). The collected files then
   go through the same part chooser, combiner, fit, color, slice and
   approval steps as files attached to one message. **Cancel project** (or
   typing **cancel project**) discards the collected files.

A collection belongs to you, in this chat or thread, for the printer the
channel serves. Another person's upload, an upload in another thread or
another printer's channel, and any upload while no project is open never
join it. Two files with the same name are kept as two parts. A file whose
bytes equal a part already collected is not added again (the reply says
so): print more copies with the copies question at print time instead.

On Discord the choices are buttons; on Telegram, Matrix and the Web UI chat
they use the same choice prompt every other question uses (Matrix answers
by number). The collection is saved in the bot's state folder
(`upload_collections.json`, with the files under `upload-collections/`), so a
restart keeps it; buttons posted before a restart stop answering, so type
**done adding** or send the next part to continue. An unfinished collection
expires after 7 days. Nothing is sliced or started while a project is
collecting.

## Upload a ZIP of model sources

A `.zip` sent in its own message is opened as a source archive. Print Handy
reads its directory first and refuses the whole archive, before anything is
written, when an entry is encrypted, a symbolic link, an absolute path or a
path that climbs out of the archive (ZIP-slip). It also refuses archives
over these bounds:

| Bound | Limit |
| --- | --- |
| Entries | 1,000 |
| Printable STL/3MF files | 64 |
| One printable file, uncompressed | 200 MiB (then the usual [intake limits](INTAKE-LIMITS.md)) |
| All entries, uncompressed | 512 MiB |
| Compression ratio of an entry over 1 MiB | 100:1 (a ZIP bomb) |

Only `.stl` and `.3mf` entries are extracted, each into its own folder, so
equal names in different folders stay two parts. Everything else is listed
in the chat as left out and never extracted: STEP and other CAD sources,
images, READMEs and other documents, scripts and programs (never run),
already-sliced G-code, nested archives, operating-system metadata, and
reference envelopes such as `REFERENCE_MAC_MINI_ENVELOPE_NOT_FOR_PRINTING.step`
(a name starting with `REFERENCE_` or saying "not for printing" or "do not
print"). An archive with no printable part is refused with the list of what
it holds.

The extracted parts then take the same multi-file path as attachments:
the same chooser, combiner and project manifest. A 3MF is extracted byte for
byte, so a single 3MF inside a ZIP keeps its authored colors, plates and
object names exactly as an uploaded 3MF does, and a 3MF among STLs is offered
first as the laid-out model. Combining several files is still geometry only,
as above. Each part's path inside the ZIP is kept on its source row and
project component (`archive_path`), and `JOB.json` records the archive
report (`source_archive`: kept parts with their SHA-256, ignored entries with
the reason). A `.3mf` upload is not treated as a ZIP: it keeps its own path.
A message with STL/3MF attachments and a ZIP prints the attachments and says
the ZIP was left out; send the ZIP in its own message. A ZIP is never
collected into a multi-message project: "add to project" with a ZIP is
refused, and a ZIP sent while a project is collecting is its own project.

## A multi-part AI design

A design made with `/print-design` that declares several parts (see
[Designs with several parts](DESIGN.md#designs-with-several-parts)) enters
this same flow. **Slice it** hands its part files to intake together, as
if they had been dropped in one message, so the parts get the same
questions, the same combiner and the same project manifest. The manifest's
`source_kind` is `ai_design`, its title is the design's name, each
component also records its declared part (`design_part`), and `design`
records the design session and turn it came from. That record is
provenance only; it approves nothing. A one-piece design is handed over
as a single file, exactly as before.

## Project identity in JSON

For multiple selected source files, `JOB.json` now records a `project`
object with `schema: project_v1`, stable `project_id`, original source
kind (`uploaded_files`, `uploaded_collection`, `uploaded_archive` or
`linked_files`), and a list of required source components. A collected
project also records `upload_collection` (the collection id and each file's
name, SHA-256 and message). A project is not a plate:
three separate STL files may fit on **one physical print plate**.

When one project is sliced into multiple G-code plates, `multipart_plan`
preserves a plate id and alphabetic suffix (A, B, ... Z, AA, AB).

Every print still gets its own permanent `print_number` (since #988 a
number names one print, not a job), so the plates of one project have
different print numbers: the center tray might be print #82 and an arm
#83. The project gets **one** readable number, the print number of the
first plate that starts. It is stored in the plan (`project_number`) and
never changes afterwards, so every plate's record carries the same project
label: `project_plate_ref` is `82A` for the tray and `82B` for the arm,
even though the arm's own print number is 83. A reprint of a plate is a new
print with a new print number and keeps its plate label (`82A`);
`print_attempt_id` tells the attempts apart. The label is for people: the
project id, the plate id and the print number stay the real identifiers.

Print records keep their usual names, one file per print number
(`print-0082.json`, `print-0083.json`), so every tool that finds a record
by its number keeps working; the plate label is a field inside the record.

These fields are additive. Old `JOB.json` and print records without
`project_id` still load; their integer IDs are never renumbered.

## Completing the project

When a project produces two or more sliced G-code files, it uses Print
Handy's existing [next-plate flow](PROJECT-REFERENCE.md): after a verified
print finishes, the completion message shows remaining plates and the
Discord next-plate selector offers a fresh bed-clear confirmation.
Nothing starts automatically. Without camera data, the normal
post-completion picker still works, while the optional camera empty-bed
offer requires an actual empty-bed observation.

Typing `/print-next <plan id>` without naming a plate, when two or more
plates are left, first asks **Which plate is next?** with one choice per
remaining plate and **Later**. The question is the same on Discord,
Telegram, Matrix and web chat: it goes through the shared chat prompt
layer, so the platforms without the Discord plate picker can pick a plate
out of plan order too. Choice labels are cut once to the shortest button
label any platform keeps whole, so every platform shows the same labels.
Picking a plate is not an approval: the picked plate still asks for its
own bed-clear confirmation, and Later starts nothing.

## Reprinting a project

`/print-again` understands the plate labels. `/print-again 82B` reprints
exactly that plate. Picking a project plate by its print number or its
title first asks its scope, **This plate only**, **Entire project**,
**Choose a plate**, or **Remaining unprinted plates** when some plates are
not finished yet, because one plate of a project is not the project.
An exact label already names its scope, so it skips the question.

The entire-project and remaining-plates scopes queue the other plates as
one new reprint run: the first plate goes through the normal bed-clear
confirmation, each following plate is offered by the completion message
and started with `/print-again next` behind its own fresh bed-clear
confirmation, and `/print-again stop` drops the rest. No approval is
inherited between plates, and a reprint run never changes the original
project's completion counts.

A reprint is its own logical run. An entire-project reprint gets a new
`project_run_id` (for example `run-3f9c2a7b1d04`), shared by all of its
plates, and so does a reprint of one plate; the original plates keep
`project_run_id: original`. The new run id is recorded on the reprint's
start and completion records, every physical attempt still has its own
`print_attempt_id`, and a completion on a reprint run never credits the
original plan: its `completed_quantity` stays where the original run
left it. **Remaining unprinted plates** is the exception on purpose: it
finishes the original run, so those plates still count toward it. Each
start uses the approval asked for that start; an approval or a queued
plate's start claim is never reused for another plate. Only plates that were actually printed can
be reprinted; a plate that never printed stays on the next-plate flow
above. Legacy print numbers keep their exact old meaning: `/print-again 82`
still resolves print #82 first, and the scope question is what turns it
into a project reprint.

`/print-again list` groups a project's prints: a standalone print stays
one line, while the prints of one project are listed together under one
project line (its number, title, plate count and how many plates are
finished), each with its print number and exact plate alias such as
`plate 82B (Left arm)`. A project counts as one entry toward the list's
length, so grouping never pushes a standalone print off the list.

## Project readout in the Web UI

The Web UI's [Projects page](WEBUI.md#projects-page) (`/projects`, and
`/api/projects` as JSON) shows each project's components, plates,
quantities, every attempt per plate and its run, and whether the project
is complete or can be resumed. It reads the job's manifest, the live plan
file and the print records; it keeps no store of its own and starts
nothing.

**Not implemented yet:** an AI design's parts enter the project flow above,
but the design does not yet declare assembly roles or placements for them,
and the engineering-grade assembly generation described in
[#998](https://github.com/catduckgnaf/print-handy/issues/998) (research,
fit and collision checks across parts) is not claimed here.

## Safety and verification

The standard intake checks (actual bed/part footprint, G-code, printer and
spool compatibility, per-plate permissions and bed-clear confirmation)
are unchanged. Automatically generating a project manifest is **not**
permission to start a print. The tests for this feature run offline with
fake printer/chat endpoints. No live multi-plate print or mechanical
load validation is claimed by this documentation.
