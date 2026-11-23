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

## Still outside this incremental change

A context-specific "Work on this with AI" button on a *failed intake* needs to
be attached to the persisted failed intake/job and its original authenticated
uploader, then routed back into this same handler. The explicit upload-based
`/fix` path works without that automatic button. This draft does not claim
the automatic offer is already wired.

Gusset, under-lip fill and straight-base geometry edits are not implemented.
They remain explicit unavailable dispatcher calls but are excluded from model
schemas until their geometry operations exist. The trim-by-plane guard checks
the entire below-plane polygon, including intersections at the cut plane.

Review with #1777 (tools), #978 (closed vocabulary), #903 (edit chat), and
#1759 (AI ready visibility).
