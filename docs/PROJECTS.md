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
still takes precedence over attached files. Several individual messages do
**not yet** automatically accumulate into one project: send the files in
one message. ZIP file collection and AI multi-output CAD remain part of
the tracked feature issue, not claims made for this increment.

## Project identity in JSON

For multiple selected source files, `JOB.json` now records a `project`
object with `schema: project_v1`, stable `project_id`, original source
kind, and a list of required source components. A project is not a plate:
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
project's completion counts. Only plates that were actually printed can
be reprinted; a plate that never printed stays on the next-plate flow
above. Legacy print numbers keep their exact old meaning: `/print-again 82`
still resolves print #82 first, and the scope question is what turns it
into a project reprint.

**Not implemented yet:** a dedicated project run id recorded on reprint
records, collecting across multiple chat messages, ZIP importing, project
management in Web UI, and full AI-produced multi-part assemblies. Those
remain acceptance criteria of [#1008](https://github.com/catduckgnaf/print-handy/issues/1008);
the CAD generator itself is [#998](https://github.com/catduckgnaf/print-handy/issues/998).

## Safety and verification

The standard intake checks (actual bed/part footprint, G-code, printer and
spool compatibility, per-plate permissions and bed-clear confirmation)
are unchanged. Automatically generating a project manifest is **not**
permission to start a print. The tests for this feature run offline with
fake printer/chat endpoints. No live multi-plate print or mechanical
load validation is claimed by this documentation.
