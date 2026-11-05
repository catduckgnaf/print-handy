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
- **Rotate:** "rotate 90", "rotate 45 degrees around X". Default is 90°
  around Z.
- **Lay flat:** "lay flat", "flat side down", "largest face down" — the
  model is set on its largest face.
- **Scale:** "scale to 80%", "make it 50 mm wide" (or "tall" by axis:
  "scale to 40 mm on Z"; plain "to 50 mm" scales the largest side).
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

Who the AI lane serves follows the agent identity layer (see
SETTINGS.md, "Agent lane identities"): when that layer is active, only
mapped or allowlisted users get AI-proposed plans — everyone else keeps
the deterministic vocabulary and the vocabulary list. When the layer is
not configured, the design endpoint ([design], inheriting [llm]) serves
edit proposals. With no AI configured at all, an unresolvable edit
request simply gets the vocabulary list above.

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
- copies check the plate's object count;
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
