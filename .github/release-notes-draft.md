# Print Handy 2.2.7 — your prints, wrapped; roasts with no AI; a watchdog that can judge locally

One Docker image for amd64 + arm64: ghcr.io/catduckgnaf/print-handy:2.2.7, also tagged latest.

## What you can do

### See your printing, wrapped
- Wrapped collects per-print stats as you go — time patterns, repeat counts, colors, materials, and pasta, failed and stopped prints as three separate headline numbers — and presents them as a scrolling page with the video embedded, plus a 9:16 story version. Sharing is opt-in (public or link-only). (#986) (#1001)

### Print multi-part projects like one job
- Upload several files and they stay linked as one multi-part project; `/print-again` asks which scope you mean and reprints the right plates, with a bed-clear confirmation per plate. (#1009) (#1038)
- Design sessions gained deterministic multi-part engineering checks to build on. (#1016)
- A project component can declare its role and its assembly placement; declared parts get placed-bounds reporting, pairwise collision checks and a rack-ear clearance check by declaration instead of by filename guessing. (#1055)

### Get roasted by a corpus, not a cloud
- Roasts are written by a local corpus and engine now — no LLM is called, at all, and there is no AI fallback. Custom roast banks and a community corpus path are in. (#1014) (#1022) (#1054)
- Ask in plain language, typos and all: the fuzzy vocabulary grew across errors, questions, software, brands, colors and slicing terms, and fuzzy still only ever suggests — it never answers by itself. (#1027)

### Only get pinged for your own prints
- When a print ends, the Discord post mentions only the person who started that print — not everyone who shares the channel. Button follow-up posts keep the same rule. (#1029) (#1033)

### Let the watchdog judge where you want
- New `vision.watch_source` setting (`auto` / `endpoint` / `local`): pin the camera watchdog to your vision endpoint, or judge frames locally on your own machine even when an endpoint is configured. (#1034)
- Teach it your empty plate: `/print-plate-calibrate` captures a reference photo (you confirm it first), and the idle checks can then judge the plate locally. Off by default. (#1065)
- A stall alert now names what changed in the frame, not just that something stalled. (#1064)

### More new tricks
- Timelapses can encode on the GPU (Intel, AMD and NVIDIA paths) with a named CPU fallback when no GPU is usable. (#1018)
- Ran out of filament mid-print? The bot asks what happened; "same spool — I fixed the feed" settles the question without touching your Spoolman records, and the print is never auto-resumed by answering. (#1066)
- If an AI mesh repair sealed holes to fix your model, the confirmation now says so — with the count — before you apply it, so a part that is supposed to have openings does not get silently plugged. (#1071)
- Outage and jam follow-through: outage notices escalate instead of repeating themselves, files can be bundled right in chat, spool slot names are clearer, and jam posts carry the supports-decision evidence with them. (#1025)
- Live camera groundwork: the Discord Activity now serves a sign-in bootstrap so it can authenticate with the bot's allowlist. The live sitting itself is still an operator-run test. (#1053)
- Try the static live demo of the chat flow, and find the official Print-Handy support Discord linked from the README and docs. (#1035) (#1041)

## Fixes

- A config typo no longer kills startup: unknown settings now warn and are ignored instead of failing the bot (see Upgrade for the exceptions that still fail closed). (#1061)
- Print numbers: per-print follow-ups after the numbering rework — reprints, mentions and timelapse names resolve to the right print. (#1013)
- The Moonraker listener hang, the supervisor watchdog and outage notices were fixed, along with two follow-up batches closing the gaps they found. (#1021) (#1019) (#1023)
- Print-now with Moonraker unreachable now raises the named offline refusal instead of failing quietly. (#1057)
- Design renders keep the first WARNING line in the error text, so the actual problem is the first thing you see. (#1056)
- The Docker image build creates the site-packages core directory before the bounds copy, and CI now builds the full image on pull requests so a broken image fails before merge, not after. (#1024) (#1015)

## Upgrade and compatibility

- **Unknown settings behavior changed (#1061):** settings Print Handy does not know — in a config file or a `PRINT_HANDY__*` environment variable — now produce a warning and are ignored, instead of stopping startup. Four cases still fail closed on purpose: credential-shaped keys in settings files, malformed `PRINT_HANDY__PRINTERS__` entries, unknown keys inside AI tier settings, and the retired `[printer]` family key.
- **Roasts no longer use an LLM (#1022):** if you had AI roasts configured, roast text now comes from the local corpus. Nothing to change; the jokes are homegrown now.
- **Print-end posts mention the starter (#1029):** on Discord, a finished, canceled or errored print now mentions the user who started it. Set `discord.mention_starter_on_complete` to `false` if you do not want the ping.
- **New settings, all safe by default:** `vision.watch_source` (default `auto`, the old behavior), `vision.local_empty_enabled` (default off) and `discord.mention_starter_on_complete` (default on, above). (#1034) (#1065) (#1029)

## Technical details and contributors

- Motion bounds got a native Rust/PyO3 parser with the Python fallback kept, then a hardening phase: G20 refused, the GIL released during parses, a pinned rustup, and a `.so`-only runtime. (#1010) (#1015)
- Slicing-speed work: the timing gate now splits slicer time from Python time, Bambu zip output is at parity, the bounds cache is validated and capped, and a cpuset budget applies; the real-Orca timing leg gained a large-plate case (reporting only). Slicing baselines were re-recorded for this release. (#993) (#997) (#1073)
- GPU inference flavors (`ORT_FLAVOR`) for the local failure detector, for hosts that want the accelerated build. (#1036)
- Testing: a hardening pass (version parity, upstream step-size policy, invariant suites), a required tests-added and regression-scoping answer on every PR, dead-test cleanup with shipped-binary and native tests in offline CI, and deep-dive expansions across Spoolman (5 to 38 real-server tests), the Web UI (real-HTTP coverage), OpenSCAD (real-binary coverage), fuzzy search and a Stage 0 vision-verdict evaluation harness with reproducible CAD assets. (#999) (#1004) (#1026) (#1030) (#1043) (#1044) (#1045) (#1047) (#1048) (#1052) (#1062) (#1000) (#1040)
- Docs: the 2.2.6 image digest is pinned in the Docker docs and the in-repo Unraid template. (#1012)

## Verification and known limitations

- Every change above passed the offline suites and CI on its exact head before merging; the release candidate image was built from the tag and checked before publishing (the provenance block on the GitHub Release lists the runs and digests).
- Not live-verified tonight, stated plainly: no real-printer acceptance run, no per-vendor GPU encode proof, no real-camera accuracy pass for the local detector or the empty-plate reference, no live Discord Activity sitting, and no live runout recovery on a real printer. Those stay open as operator follow-ups on the release checklist (#1072), the same disposition as 2.2.6.
- Wrapped sharing is public or link-only; encrypted private sharing is not in this release.
- One harness note for operators who follow the release checks: the upgrade lifecycle check cannot pass against the shipped 2.2.6 image, because 2.2.6 hard-exits on the newer settings keys the check seeds. That is the bug #1061 fixed — from 2.2.7 onward the check can pass end to end.
