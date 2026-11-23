# AI fix session implementation notes (#1778)

## Connected behavior

The shared chat handler, `print_handy/bot/handlers/fix.py`, handles explicit
`/fix` plus one STL/3MF upload on Discord, Telegram, Matrix and Web UI, as well
as a model upload with a repair instruction. Session follow-ups are limited to
the authorized user, chat and thread; unrelated slash commands and new model
uploads keep their normal routing. The model uses the existing heavy-tier
`ai_routes` endpoint and gets only implemented print-repair schemas.

Each repair works on a new copy inside a private session workspace. A failed or
unchanged tool call is never a successful edit. Accept runs the *normal* model
intake with the repaired copy and only marks the session accepted after that
intake reaches its sliced/verified result; refusal or cancellation leaves the
original job unaccepted. The ordinary intake still owns its printer selection,
slicer, verification and start confirmation.

## Failed-intake offer and limits

For a rejected uploaded STL/3MF slice, the existing intake dispatcher now offers
"Work on this with AI" using the same cross-platform confirmation component.
Only the file attached to that requester is eligible. A confirmed choice starts
this same user/chat/thread-scoped session. The explicit upload-based `/fix`
path also works without a preceding failed slice.

A URL-only failed download cannot yet offer repair automatically, because it
may not have a trustworthy, locally retained original file after the refusal.
The user can upload the model with `/fix`. The handler deliberately refuses
to guess the newest archived file from another user's work.

Gusset, under-lip fill and straight-base geometry edits are not implemented.
They remain explicit unavailable dispatcher calls but are excluded from model
schemas until their geometry operations exist. The trim-by-plane guard checks
the entire below-plane polygon, including intersections at the cut plane.

Review with #1777 (tools), #978 (closed vocabulary), #903 (edit chat), and
#1759 (AI ready visibility).
