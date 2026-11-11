# Print Handy branding kit

One place for the name, the pitch, the voice and the assets. When a page, a
store listing or a link preview describes Print Handy, it copies from here, so
the README, the docs site, the Unraid template, GitHub and anyone else's
listing all say the same thing.

## The pitch

**Bambu Handy for every printer.**

Bambu owners get a slick phone app: find a model, tap print, check on it from
anywhere. Everyone else gets a slicer chained to a desktop, a USB stick, and a
walk to the garage to see if the first layer stuck. Print Handy is that phone
experience for everybody else, Bambu included. It lives in the chat app you
already have open, so "print this" is one pasted link instead of a sit-down
session with a slicer.

What it is, in facts: drop a model link or file in Discord, Telegram or
Matrix (the Web UI covers status, settings and spools); Print Handy slices it for your printer, matches your Spoolman
spools, asks before anything prints, follows the print, watches the camera for
spaghetti, posts the timelapse, and roasts your model on the way out. One
Docker container.

Local-first is part of the pitch, not a footnote: Print Handy was made as a
local-only solution that runs on the owner's machine and talks to printers on
the local network, with no Print Handy account and no Print Handy cloud. AI
is optional, and Discord is optional; neither is needed for the core flows.
Say so wherever there is room, in those words or close to them, and never
stretch it into "nothing leaves the house": the chat provider the owner picks
carries the messages, files and photos sent through it, like any other
message on that platform.

Always pair the Bambu comparison with the not-affiliated line wherever there is
room for it:

> Not affiliated with Bambu Lab. We just liked their app enough to want it on
> every other printer.

## Copy by length

Copy these as written. Each one is sized for where it goes.

| Use | Length | Copy |
|---|---|---|
| Tagline | 5 words | Bambu Handy for every printer. |
| Link preview, GitHub repo description | under 160 characters | Bambu Handy for every printer. Drop a model link in Discord, Telegram or Matrix, approve it, walk away. It slices, watches for spaghetti and roasts your model. |
| Store listing or plugin list, one line | one sentence | Print from your phone on any printer: paste a model link in chat, approve it, and Print Handy slices, prints, watches the camera and roasts your model. |
| Short paragraph | about 60 words | Print Handy is Bambu Handy for every printer. Paste a MakerWorld or Printables link in Discord, Telegram or Matrix, pick colors with buttons, and approve the sliced job. It matches your Spoolman spools, follows the print, watches the camera for spaghetti, sends the timelapse, and roasts your model choices. One Docker container, nothing prints without your OK. |

## Voice

- **Funny about the model, never about safety.** Keep approval prompts, heat and motion warnings, refusals and errors plain and exact. Put roasts only where they belong: one per print on the completion post, the failure and cancel posts, and a confirmed-spaghetti camera alert, drawn from the local roast corpus (no LLM, no endpoint, #1002); never in safety copy.
- **Short, concrete and task-first.** Say what the reader can do, then link to details. Avoid repetitive reassurance and exhaustive inventories on entry pages.
- **Local-first, stated honestly.** Print Handy itself runs on the owner's machine and has no Print Handy cloud. Chat providers carry messages and files; model downloads and optional hosted AI use the network. Never claim that nothing leaves the house.
- **Mark optional features clearly.** Camera monitoring, AI, live viewing and printer controls are optional or separately enabled. Do not imply camera analysis guarantees safe unattended printing.
- **Describe supported paths, not every printer.** Name connection families in short copy and link to the current support guide. Distinguish code coverage from hardware verification. Never imply certification.
- **Preserve approvals.** State that the exact sliced job needs human approval before printing. Do not suggest a model estimate or bounding box proves a safe physical print.
- **American spelling** (color, behavior, gray) and avoid em dashes.

Approved short copy:

- "Your slicer lives on a desk. You don’t."
- "Another articulated dragon? Bold choice."
- "Sometimes it really is fine. Sometimes it's modern art."
- "Spoolman, minus the spreadsheet."
- "Not affiliated with Bambu Lab. We just liked their app enough to want it on every other printer."

## The mascot

The mascot is the catduck: a yellow, fluffy cat with duck feet and a nozzle
thumbs-up, on a black rounded tile with a green ring. It is named after the
maintainer's handle. Use it as is: no recoloring, no cropping off the feet, no
stretching.

## Colors

| Name | Hex | Where it comes from |
|---|---|---|
| Catduck yellow | `#F5C247` | Mascot fur |
| Duck-foot yellow | `#FED32C` | Mascot feet, highlights |
| Tile black | `#181818` | Icon tile, wordmark |
| Ring green | `#4B985C` | Icon ring, success accents |
| Eye blue | `#1286E3` | Mascot eyes, links |
| Docs orange | `#F97316` | Docs site theme color |

## Assets

All in [`assets/branding/`](../assets/branding/):

| File | Size | Use it for |
|---|---|---|
| `social-preview.png` | 1280 x 640 | GitHub social preview, link preview cards (`og:image`) |
| `logo-lockup.png` / `logo-lockup-dark.png` | 2240 x 1120 | README header and docs (light and dark backgrounds) |
| `avatar-1024.png` | 1024 x 1024 | Bot avatars (Discord, Telegram, Matrix), Unraid and store icons |
| `icon-master.png`, `icon-16` to `icon-512.png`, `favicon.ico` | various | Favicons, app icons, the Web UI |

## Where the copy lives

Update these together when the pitch changes:

| Surface | Where |
|---|---|
| README | `README.md` (synced to the docs site's Guide page on release) |
| Docs home page and link preview | `print-handy-docs`: `docs/index.md` and `docs/.vitepress/config.mjs` |
| Unraid template | `docker/unraid/print-handy.xml` here and `catduck-unraid-templates/templates/print-handy.xml` |
| GitHub repo description and social preview | Repository settings |
| Third-party listings | ZMod's plugin list (`ghzserg/zmod` `docs/*/Plugin.md`) |
