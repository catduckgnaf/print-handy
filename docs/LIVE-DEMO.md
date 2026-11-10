# Live demo

Print Handy has a static, simulated live demo: a browser chat styled
like the Discord conversation, running against a simulated printer
only. It is for answering "do I like how this talks to me?" before
any install.

The demo lives in `demo/` in this repository. It is plain HTML, CSS
and JavaScript with a scenario data file (`demo/scenarios.json`), 
no build step and no backend.

## What it is, and what it is not

- Always visible banner: "Demo, simulated printer, nothing is
  printed, sample data only."
- The visitor picks a pre-seeded sample, answers questions with
  buttons, sees a slice summary and the confirm-before-print card,
  watches compressed simulated progress, and sees a terminal post
  that mentions the visitor who started the simulated print.
- Journeys: a happy path, a too-big refusal with the shrink size
  that fits offered, a spools query, a help journey that shows the
  command list, and unmatched free text, which gets the real bot's
  short did-not-understand reply and a pointer to `/print-help` —
  not the command list.
- A Reset button restarts the session clean.
- Uploads are theatre only: a picked or dropped file shows its name
  and size, and the demo does not parse it. The playable set is the
  pre-seeded samples.
- No real printer, no chat platform account, and no credential of
  any kind is involved. The page makes no network calls beyond
  loading its own scenario file.

Scenario wording tracks the real bot's wording where the corpus
flows pin it (for example the color and copies questions, the
"Ready to slice" confirmation with its "Slice and print now (bed
is clear)" button, the fit refusal, and the loaded-filament
command). The command list, the help heading and the unmatched
reply are the production bot's own text. Some lines are shortened
or adapted for the demo; the demo is a simulation, not a recording
of a real print.

## Run it locally

```sh
cd demo
python3 -m http.server 8000
```

Then open the local address the command prints, in a browser.

## Hosting and the v2 path

Version 1's host is GitHub Pages (decided by catduck): the demo is
fully static, with no server code, so it stays within GitHub Pages
constraints at no cost. Enabling Pages is a follow-up for catduck, 
it is a repository-settings step, not a workflow in this repo (no
Pages deploy workflow exists here), and this change does not
enable Pages and adds no deployment workflow. Once Pages is
enabled, the README and docs link to the live demo as a follow-up.

Version 2, only if version 1 proves demand, is the real-logic demo
tracked separately in: the existing image in a
dedicated demo mode, talking to a fake printer through the
existing platform layer, with per-visitor ephemeral sessions,
model fetching off, and a mock slicer only.

The scenario data is checked against the real bot by the contract
tests in `tests/test_live_demo.py`: the command list and the
unmatched reply are compared with the production bot's own table
and reply text, and the confirmation and fit-refusal wording is
pinned, so wording changes in the real bot fail the demo check
instead of drifting silently.
