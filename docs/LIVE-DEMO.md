# Live demo

Print Handy has a static, simulated live demo: a browser chat styled
like the Discord conversation, running against a simulated printer
only. It is for answering "do I like how this talks to me?" before
any install.

The demo lives in `demo/` in this repository. It is plain HTML, CSS
and JavaScript with a scenario data file (`demo/scenarios.json`) —
no build step and no backend.

## What it is, and what it is not

- Always visible banner: "Demo — simulated printer, nothing is
  printed, sample data only."
- The visitor picks a pre-seeded sample, answers questions with
  buttons, sees a slice summary and the confirm-before-print card,
  watches compressed simulated progress, and sees a terminal post
  that mentions the visitor who started the simulated print.
- Journeys: a happy path, a too-big refusal with the biggest size
  that fits offered, a spools query, and unmatched free text, which
  gets the command list only.
- A Reset button restarts the session clean.
- Uploads are theatre only: a picked or dropped file shows its name
  and size, and the demo does not parse it. The playable set is the
  pre-seeded samples.
- No real printer, no chat platform account, and no credential of
  any kind is involved. The page makes no network calls beyond
  loading its own scenario file.

Scenario wording tracks the real bot's wording where the corpus
flows pin it (for example the color, copies and "Slice and print"
confirmation questions, and the loaded-filament command). Some lines
are shortened or adapted for the demo; the demo is a simulation, not
a recording of a real print.

## Run it locally

```sh
cd demo
python3 -m http.server 8000
```

Then open the local address the command prints, in a browser.

## Hosting and the v2 path

Version 1 is designed for any static host at no cost, including
GitHub Pages. Enabling a host is a follow-up after catduck chooses
it; this change does not enable Pages and adds no deployment
workflow.

Version 2, only if version 1 proves demand, is the real-logic demo
described on issue #1031: the existing image in a dedicated demo
mode on a small paid container, talking to a fake printer through
the existing platform layer, with per-visitor ephemeral sessions,
model fetching off, and a mock slicer only.

A follow-up can generate or check the scenario data from corpus
harness snapshots of the real bot's messages, so wording changes in
the real bot fail the demo check instead of drifting silently.
