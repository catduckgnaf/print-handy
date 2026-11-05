# Editing a prepared model from chat

After a model has been prepared in a channel, you can change the job in
chat instead of starting over. Every change — typed, tapped or proposed
by the AI lane — becomes one typed **edit plan** that is confirmed,
applied to the job, verified against the re-sliced result, and recorded
on the job. Nothing edits silently, and no edit starts a print: the
re-sliced job comes back through the normal intake summary, and every
approval is asked again against the new G-code.

## The vocabulary

Say any of these about the current job in the channel (or thread):

- **Supports:** "add supports", "add tree supports", "normal supports",
  "supports on the build plate only", "no supports". A bare "supports
  please" works too.
- **Rotate:** "rotate 90", "rotate 45", "rotate 45 degrees around X". A bare number is the angle. Default is 90°
  around Z.
- **Lay flat:** "lay flat", "flat side down", "largest face down" — the
  model is set on its largest face.
- **Scale:** "scale to 80%", "make it 80%", "make it 50 mm wide" (or
  "tall" by axis: "scale to 40 mm on Z"; plain "to 50 mm" scales the
  largest side).
- **Relative size:** "shrink it by 20%", "20% smaller", "10% bigger",
  "make it half the size", "double it". These compose with the current
  scale: a job already at 80% that is shrunk by 20% goes to 64%. "Shrink
  this" or "make it bigger" with no amount gets a pick list of amounts.
  Size verbs tolerate typos ("shrnik it by 20%").
- **Mirror:** "mirror it", "flip it on Y".
- **Copies:** "print 3 copies", "two copies" — single-plate,
  single-color jobs only, and only as many as fit the bed.
- **Brim:** "add a brim", "brim 8 mm", "no brim".
- **Layer height:** "layer height 0.12", "fine layers", "draft quality".
- **Infill:** "infill 25%".

Print Handy resolves the phrase itself, with the same fuzzy matching
used for its other chat understanding. A clear match gets a confirmation
naming the exact edit ("re-slice with tree supports"); a near miss gets
a pick list of the closest edits; nothing is ever applied on a guess.
"Undo" (or "undo the last edit") restores the state before the most
recent edit and re-slices that.

## The AI lane

When a request names an edit concept but the vocabulary cannot resolve
it, the AI lane may be asked to translate it — into a plan from the
*same* vocabulary, which the same validator checks and the same
confirmation names. The AI never applies anything itself, and an
out-of-range or malformed proposal is rejected with the reason. A
request that changes the model's geometry itself (a hole, a taller
wall) is routed to the design engine instead; requests about slicer
settings (supports, brim, infill, layer height, scale, rotation,
copies) are never turned into mesh edits.

The AI lane uses the design endpoint ([design], inheriting [llm]).
`ai.allowed_users` limits it to listed chat user IDs (empty keeps it open
to everyone the gateway already authorizes), and
`ai.edit_requests_per_hour` (default 20, 0 for none) limits each user over
a rolling hour. A refused request, or one sent with no AI configured at
all, gets the vocabulary list above; nothing reaches the endpoint. Discord
mentions are stripped from the request before it is sent.

The AI lane keeps a short memory per user and channel: the last three
exchanges, forgotten after 30 minutes or a restart, so a follow-up such
as "thicker than that" has context. It is held in memory only.

Model names, filenames and 3MF metadata are passed to the AI as quoted
data, never as instructions, and the reply is only ever parsed as a
plan — instruction-like text inside a model file cannot change the plan
schema or skip the confirmation.

## Verification before the print buttons

An edit is test-sliced on a copy of the job first, and the result is
checked against the plan before the real job changes:

- scale, rotate, mirror and lay-flat edits compare the sliced G-code's
  motion bounds with the expected size, within tolerance;
- supports edits check the G-code actually contains support toolpaths
  and the sliced config matches (support on/off, tree vs normal,
  build-plate-only);
- copies check the plate's object count (the probe slice turns Orca object labels on, since the shipped AD5X profiles ship with them off);
- brim, layer height and infill check the values in the sliced config.

A failed check is reported with its reason and the job is left exactly
as it was — a failed verification is never printed. (The slicer's own
floating/cantilever warnings are not detected yet; that is separate
work.)

## Multi-part models

Job edits never touch the source file, so a multi-part 3MF keeps its
per-part color/material assignments through every edit above. Two
honest exceptions: mirroring is refused for per-part color jobs (the
per-part export has no mirror channel, and flattening would drop the
assignments), and a *geometry* edit on a multi-part model is refused
with the reason — edit one part at a time instead. Neither is ever done
silently.

## Provenance and undo

Each edit is recorded on the job: who asked (platform and user), which
lane served it (fuzzy or AI), the plan, the verification result, and
the source and output G-code hashes. "Undo" walks that record back one
edit at a time. Because approvals are bound to the G-code hash, any
edit — including an undo — means the print approvals are asked again
for the new G-code.

## Questions about the model

A question about the current job is answered, never turned into an edit:

- "Does this need supports?" reports whether this slice has supports on,
  what the slicer flagged (floating regions, cantilevers) and, when the
  file ships its own supports as a named object such as "REMOVABLE
  SUPPORTS", a measured report: each overhang's height and area, the gap
  down to the support tops, and roughly how much of the overhang they
  touch. These numbers come from the mesh, never from AI, and are
  approximate (a 0.25 mm grid). When the slicer flagged something and
  supports are off, it offers **Re-slice with supports** or **Print
  without supports**; nothing changes until you pick. The same measured
  report is added to the slicer-warning question asked during intake.
- "How big is it?" gives the job's X x Y x Z size in millimeters.
- "Will it fit?" compares that size with the printer's bed.

A question in a channel with no current job is left to the chat.

A polite request is a command, not a question. "Can you add supports?",
"could you shrink it", "please add a brim" and "can you make it smaller"
go to the edit path and get the usual confirmation; "do I need
supports?" and "does this need supports?" stay questions.

## A link or file plus an instruction

A model link or attached file can carry the edit or question in the same
message: "<printables link> shrink this by 20%" or a 3MF with "does this
have supports?". Print Handy runs the normal intake first, then applies
the edit (with its confirmation and re-slice) or answers the question
against the job that intake just prepared. Plain intake text such as
"print this" is not treated as an instruction.

## Edit corpus tests

The edit pipeline is covered by a dedicated 3-shard test suite in `tests/corpus/edit/` and weekly external corpus sweeps in `tests/corpus/run_external_edit_corpus.py`. See [Discord flow corpus in PROJECT-REFERENCE.md](PROJECT-REFERENCE.md#edit-corpus-journeys) for runner details and case authoring conventions.
