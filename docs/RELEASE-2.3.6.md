# Print Handy 2.3.6 (Draft / Unreleased)

Status: Draft / Unreleased. This is a documentation draft for the changes on `main` after 2.3.5; it is not a published release, and has no release date or version tag.

## What you can do

### Choose the intended part or plate

- When a model link has multiple authored plates, make the choice in the request: for example, send `https://www.printables.com/model/123-example print plate 2` to select authored plate 2. An unambiguous phrase such as `print only the second part` can identify its authored plate when that part maps to exactly one plate. Ambiguous or unavailable selections do not silently choose another plate. (#1704)
- You can also reply in the same Discord channel to the original model-link message with `print plate 2` or `print only the second part`; the replyer's selection takes precedence over instructions written by the original poster. A message link can likewise carry the requester's selection. (#1710)
- For an existing sliced project, `/print-next <plan id>` asks which remaining plate to print when there is more than one, including on chat transports that do not have Discord's plate picker. Choosing a plate is not print approval: its normal bed-clear confirmation remains required. `Later` starts nothing. When a requester has several eligible sliced projects, Print Handy identifies the right project rather than assuming the first one. (#1706)
- For archived source checkpoints, `/resume-model` can ask which project to resume if several eligible archives match. `print part 2` can narrow the request to a part, and a picker distinguishes multiple matching projects/plates. The candidates are scoped to the requester, chat and printer; this resumes an archived source workflow, not an automatic print. (#1711)

### See and share cost estimates

- Recorded print costs are now published to Moonraker's `helix-screen` database namespace for HelixScreen to show the same filament-plus-electricity amount. Labor and other cost lines are excluded from that shared value. Publishing is best-effort and does not block starting or completing a print; HelixScreen can calculate its own cost if a matching shared value is unavailable. (#1652)

### Follow firmware update notices

- Eligible Moonraker component updates are grouped into one printer update alert rather than separate messages. The alert offers a seven-day deferral; deferral postpones the digest and does not install anything. Update actions still require explicit confirmation and the existing eligibility, idle and verification checks. Bundled component updates are hidden from reports by default; setting `[firmware] hide_bundled_updates = false` shows them for information without enabling separate installation. (#1705, #1707)

### Explore Web, Data and Pasta Wrapped

- The Data page and Pasta Wrapped now include per-user/per-printer statistics and costs, and the Web UI's Data page has a more question-focused layout. The Web UI now has six grouped navigation tabs; its Designs page is a design conversation, and its Status page includes the printer screen. Pasta Wrapped is the year-in-review view (configured to open December 1). (#1640, #1642, #1647, #1657)
- The built-in cost calculator accepts supported slicer/file inputs and manual estimates, uses configured prices and available spool pricing, and contributes frozen job costs to Data and Wrapped. See [Cost calculator](COST.md) and [Web UI](WEBUI.md) for scope, defaults and limitations. (#1632)

### Move projects between slicers

- Cross-slicer 3MF intake translates a validated subset of foreign slicer settings into the destination slicer, while the destination printer profile and its machine-specific settings remain authoritative. The conversion report distinguishes preserved, translated, replaced, dropped and unknown settings. You can export an editable 3MF and inspect per-plate statistics, external-intake drafts and the read-only Snapmaker U1 feasibility report. U1 feasibility is not a print plan, and slicing does not deduct inventory. See [Cross-slicer 3MF](CROSS-SLICER-3MF.md). (#1578, #1621)

## Fixes and refinements

- Firmware release notices no longer imply that a printer is out of date when Print Handy cannot compare its installed version. Bundled components are hidden by default, and the existing apply guards remain in place. (#1707)
- The cost calculator now handles cost-only historical Data records even where a material breakdown is unavailable, hours-only manual estimates ahead of attached-file estimates, costs for files dropped directly into chat, and binary ChiTuBox `.cxdlpv4` resin containers. (#1663, #1668, #1693, #1694)
- Slicer and project handling received fixes for Cura stack/instance selection, translated foreign-project settings, painted-fit tool maps and purge plans, safe package reuse, and ZMod upload slot choices. (#1653, #1665, #1685, #1686, #1689, #1670)
- Print Handy now records terminal roasts to MQTT with Home Assistant discovery, expands roast/fuzzy-match content, and includes more roast-fact banks. (#1672, #1673, #1674, #1654)

## Upgrade and compatibility

- No mandatory configuration migration is identified in the reviewed changes. The firmware visibility setting `[firmware] hide_bundled_updates` defaults to `true`; setting it to `false` changes report visibility only, not update eligibility.

## Technical details and references

- Notable changes: [cross-slicer 3MF interoperability #1578](https://github.com/catduckgnaf/print-handy/pull/1578), [cost calculator #1632](https://github.com/catduckgnaf/print-handy/pull/1632), [six Web UI tabs #1642](https://github.com/catduckgnaf/print-handy/pull/1642), [Data and Pasta Wrapped #1657](https://github.com/catduckgnaf/print-handy/pull/1657), [grouped firmware alerts #1705](https://github.com/catduckgnaf/print-handy/pull/1705), and the plate/project selection changes [#1704](https://github.com/catduckgnaf/print-handy/pull/1704), [#1706](https://github.com/catduckgnaf/print-handy/pull/1706), [#1710](https://github.com/catduckgnaf/print-handy/pull/1710), [#1711](https://github.com/catduckgnaf/print-handy/pull/1711).
- This draft summarizes code already merged to `main` through `94e75463`. It does not include pending, unmerged #1712–#1714.

## Verification and known limitations

- This document was checked against the code and current guides at `94e75463`; this is documentation review, not a test run or hardware validation.
- Relevant tests in the merged commits are part of the repository's CI. No CI result for this documentation branch is claimed here.
- Cross-slicer coverage includes offline fixtures and real-slicer acceptance described in the feature documentation; no new printer-family live verification is claimed by this draft. In particular, the Snapmaker U1 feasibility report is read-only and has not established that a U1 print works.
- HelixScreen cost sharing follows its documented Moonraker contract and is best-effort. No live HelixScreen or printer check is claimed.
- Firmware update alerts and actions are distinct: alerts/deferrals do not install updates; only eligible component actions use the explicit guarded update flow. No live firmware update was performed for this draft.
