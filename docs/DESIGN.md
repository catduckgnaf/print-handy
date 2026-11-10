# AI design with OpenSCAD (`/print-design`, alias `/design`)

Describe a part in chat — Discord, Telegram or Matrix — or start from a model you already have — and Print Handy
designs it for you: a model writes
OpenSCAD, Print Handy renders it with the OpenSCAD binary already in the
install, measures the result, and shows you a preview with the real
dimensions. You then choose:

- **Slice it** — the STL goes through the normal prepared intake (filament,
  fit, slicing and the usual print confirmation). Design never slices or
  prints anything itself, and nothing reaches the printer without the same
  approvals as a model link.
- **Change it** — your next message in the chat is the change ("make the
  holes 5 mm", "add a lip around the edge"); the design is edited and
  re-rendered. Up to 10 turns per design. A plain parameter change —
  "make plate_z to 5" or "make it 5 mm taller" — is applied to the
  named variable directly, with no model call, and the reply names the
  change ("plate_z: 3 → 5"); after a model-written turn the reply lists
  any parameters the rewrite changed, so silent drift is visible.
  "Undo" (or "go back", "go back to the first one", "go back to version
  2") returns to an earlier version of the same design without a model
  call, reusing that version's render. A model-written change is shown the
  last few change requests, so "the other side" or "like before" resolve
  against the same conversation.
- **Cancel** — the design session ends.

You do not have to press **Change it** first: while the question is open,
just type the change ("make the holes 5 mm") and it runs as the next turn
of the same design. Typing a choice (or its number on Matrix) answers the
question the same as the button.

## Turning it on

Design is **on automatically whenever a model endpoint is configured** —
the same posture as roasts. The `[design] enabled` switch is tri-state:

- **Unset (the default): auto.** Design works when an endpoint resolves
  (below); with no endpoint configured anywhere, `/print-design`, `/design`
  and the design phrases answer "design is not set up".
- **`enabled = false`:** off in all cases, whatever is configured.
- **`enabled = true`:** forced on; an unconfigured endpoint is then a
  visible "no design model is configured" failure, not a silent off.

Where the model comes from:

- **The `[design]` settings pick the model.**
  Leave `chat_url` / `model` / `api_key_env` empty to reuse the `[llm]`
  endpoint, or point them at a stronger model — design turns are the ones
  worth spending a bigger model on, while everyday chat stays on the small
  one. This is also how Telegram and Matrix get design: an explicitly
  configured `[design]` endpoint (or the `[llm]` values it inherits)
  serves design there, and nothing else changes — ordinary chat on those
  platforms still has no LLM fallback and unmatched text still gets the
  short “didn't understand” reply.
`bot check` prints a `design` row naming the mode (`off`, `auto-off`,
`auto-on` or `on`), the model in effect, and whether OpenSCAD is present.

Standalone installs outside the container need the `openscad` binary on the
machine (the container image includes it); with design on and no
`openscad` on PATH, `bot check` fails that row and design turns report it.

## The sandbox, in plain language

The model writes the part, but it never gets to run anything general:

- The generated file may not use `import`, `include`, `use` or `surface` —
  the statements that could read files off the machine. A file containing
  one is refused before OpenSCAD ever runs. (Mesh edits are the one place an
  `import` appears in a render: the engine's own fixed header imports the
  session-local copy of the model being edited — the model's text is still
  scanned and still may not contain one.)
- Rendering runs with `--hardwarnings` (any warning is a failure, so a
  design that silently lost a piece cannot slip through), in the design's
  own folder, with a near-empty environment, a CPU/memory limit and a
  wall-clock timeout (`[design] timeout_s`, 180 s by default).
- If OpenSCAD rejects the file, the exact error goes back to the model for
  a repair, up to `[design] repair_attempts` times (3 by default). If it
  still fails, the turn says so and shows the last error — nothing is
  handed to intake.

## How a design is made and checked

The model is asked for a parametric, print-aware file: every dimension a
named variable with a comment, millimeters, one solid lying flat on the
bed, chamfers instead of overhangs past 45 degrees, walls of at least
1.2 mm, 0.2-0.3 mm clearance on holes and mating parts, and a size that
fits the configured printer. The file starts with three comment lines the
engine reads back: `// Part:` (what it is), `// Size:` (the intended
bounding box) and `// Assumes:` (each assumption worth knowing).

Before you see anything, the rendered part is measured and checked: it
must be a watertight, manifold solid with volume; it should be one piece,
rest on the bed at Z=0, fit the printer's bed and build height
(`[printer] bed_mm` and `max_print_height_mm`), and measure what its
`Size` line says (within 0.5 mm or 3 %, in any axis order). A failed check
goes back to the model with the measured numbers ("it measures 30 x 28 x
45 mm, not the 30 x 31 x 45 mm on the Size line") and the model fixes it.
Render errors and failed checks share the same `repair_attempts` budget,
so a turn never makes more than `1 + repair_attempts` model calls; every
attempt is logged. When the budget runs out, the attempt with the fewest
problems is shown and the reply lists what is still off under
"Heads-up"; a mesh that is not a printable solid is never shown or handed
to intake.

The reply says what was made, its measured size, what was assumed, the key
named parameters and how to change it. The numbers are always measured;
only the description and the assumptions are the model's words.

Design proceeds on sensible assumptions instead of asking. It asks at most
one question per design, and only on the first turn of a new part, when a
missing fact would change the part and cannot be assumed (the size of the
thing it must fit, with no standard size). Your next message is the
answer; "you pick" lets it choose and say what it assumed. Edits never ask.

A turn that asks the model starts with a short "Designing it now" note,
and the chat shows the bot typing until the part is posted, because
checking and repairing can take a minute or two. Undo and plain parameter
changes are quick and skip the note.

## Honest limits

- Each turn is a real model call plus a real render: expect seconds to a
  couple of minutes per turn, and model cost per turn. Sessions are kept
  under the cache directory (`design-sessions/`) for 14 days.
- The preview and measurements are of the rendered STL: dimensions, volume,
  shell count and whether the mesh is watertight and manifold. A design
  that renders to nothing is rejected, not posted.
- The preview is a four-view sheet (iso, front, top, right) with the
  measured dimensions drawn on it; from the second turn on, the previous
  turn's sheet is shown beside the new one for comparison. If the OpenSCAD
  preview render fails, a built-in rasterizer draws the preview instead;
  if no image can be produced at all, the reply says why ("No preview:
  …") next to the measured dimensions instead of staying silent.
- Dropping a model with `/print-design` and no instruction previews the
  model exactly as dropped — no model call is made for that first look.
- Mesh edits (below) can only do what a mesh allows; a change that needs the
  original design's parameters is refused in plain words, not faked.

## Editing an existing model

Attach the model to the design message — drop the file in the same message as
`/print-design <the change>`, or paste a supported model-site link in it
(MakerWorld, Printables, Thingiverse, MyMiniFactory, Cults3D, Creality Cloud, Thangs,
Nexprint, MakerOnline). The
original is never modified: design works on a copy inside its session folder.

- **`.scad`** — edited in source form. The model returns the complete updated
  file, so parameters change directly (there is no separate parameter-override
  mechanism; the session file always shows the real design). If the file
  references other files (`import`/`include`/`use`/`surface`), the first reply
  says so: a design session holds one self-contained file, so referenced parts
  cannot render here.
- **`.stl`, and `.3mf` after conversion to STL** — edited as a mesh. The engine
  composes the render: its own header defines `base()` (the model being
  edited) and the model's edit body calls it. Scaling, mirroring, moving,
  adding shapes and cutting holes work this way. Changes that need the
  original design's parameters or generated structure (a thread pitch, a
  pattern) do not — the design says so in plain words instead of guessing. A
  `.3mf` is converted through the same geometry loader intake uses.
- **A model link** — the file is fetched through the ordinary link machinery
  first (MakerWorld picks its default profile; Printables, Thingiverse and
  Creality Cloud must offer one STL/3MF file — a Creality Cloud link's print
  profile names its file when it has one; Thangs, Nexprint and
  MyMiniFactory must offer one STL/3MF file too; MakerOnline and Cults3D links
  stop with their download gaps named), then edited like a dropped file.

## Saying it instead of the command

With design on and a model configured, these phrases start design
without the command (case-insensitive):

- "design me …" / "help me design …" — a new part; the rest of the message is
  the description.
- "design this", "design it", "redesign this" — edit the model attached to
  the message, linked in it, or the channel's recent model; with none of
  those, design asks for one.
- "edit this", "modify this", "change this" — the same edits, but only when a
  model is actually there (attached, linked or recent); a bare "change this"
  with no model anywhere is left to normal chat.

Anything without a design phrase — a plain link, a plain file drop, "print
it", every existing command — behaves exactly as before, and while a design
is waiting for a revision, your next plain message is the revision, phrases
or not. When design cannot run — switched off, or auto with no endpoint
configured — a message with a design phrase gets the same
"design is not set up" reply as the command — and with design forced on but
no model configured, the same "no design model is configured" reply — while
messages without a design phrase are untouched either way. The phrases work
on Telegram and Matrix exactly as on Discord; on Matrix the Slice it /
Change it / Cancel choices arrive as a numbered list you answer by number
or by typing the choice, like every other Matrix prompt.

## Browsing designs in the Web UI

The [Web UI](WEBUI.md) has a read-only **Designs** page (`/designs`, behind
the same access token as every other page) listing the design sessions on
the machine, newest first: what each one is (created from a description, or
an edit with its source named), its state, how many turns it took, when it
was made and last changed, and its preview. Opening a design shows its
final `.scad` source and offers downloads of the `.scad`, the final `.stl`
and the preview PNG.

The page is a catalog of what exists on disk — the same sessions chat works
with, kept for the same 14 days — so it lists sessions even while
`[design]` is switched off. It has no actions: nothing there deletes,
slices or prints. Slicing a design still happens from chat, through the
normal confirmation flow.

## Multi-part engineering checks

`print_handy/core/assembly_check.py` measures a multi-part project's exported STLs and judges them against stated rules, never against a model's own claims. Every finding carries its measured evidence:

- **Each part:** watertight, manifold, consistent winding, one shell, positive volume, measured extents.
- **Bed fit on the real printer:** both flat orientations are tried. An edge margin under 5 mm is a *warning*: it is unproven until a real slice shows the skirt, brim, purge and bed exclusion zones fit.
- **Rack ears:** checked against the EIA-310 panel clearance, not just the unit pitch. A 2U ear may be 88.106 mm, not 88.9 mm.
- **Mirrored left/right pairs** must match.
- **Project shape:** at least two structural printable parts. Test coupons and geometry marked not-for-printing never count.
- **Declared roles:** a part's role no longer has to be guessed from its file name. A `project_v1` component may declare `role` — `structural`, `coupon`, `reference`, or `rack_ear` — and `assembly_check.review` takes the manifest (or the same declarations directly) and lets the declaration win, labeled `declared` in the project-shape evidence; undeclared parts keep the file-name inference, labeled `inferred`. A part declared `rack_ear` gets the panel-clearance check because it was declared, at any height, and a declared non-ear is no longer ear-checked by the height heuristic. A declared ear reviewed without a rack size is a warning, not a pass.
- **Assembly transforms and collisions:** a component may also declare its placement as `assembly_transform` (`translate_mm` and `rotate_deg`, degrees about X, then Y, then Z — for example the reference tray's +10 mm Z offset, which loose STLs cannot carry). Placed parts are reported with their placed bounds and checked pairwise: disjoint placed bounds prove no collision; overlapping bounds get an exact interpenetration test, where flush contact is not a collision. Parts without a declared placement are named as unchecked — a review where only some parts are placed says so, and a review where none are placed still states transforms and collisions as not checked.

Against the reference design (the fixture in `tests/corpus/design/mac_mini_rack_issue_998/`), it fails both 88.5 mm ears on panel clearance and warns on the 214 mm tray's 3 mm bed margin.

Not checked (and said so in every summary): fastener fit, load and heat, and slicer toolpaths. The design engine does not call these checks yet; the manifest declarations above are the surface a multi-part design session or the AI design handoff declares on, and `review(..., project=manifest)` is the call it makes.
