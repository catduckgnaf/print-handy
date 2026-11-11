# ZMod AD5X MVP implementation contract

## Source authority
Official AD5X documentation at ZMod commit 001703371c90e228defce41bb4230b7fafd37fea states that multiple spools assigned the same color AND material type automatically switch when the active spool runs out.
https://github.com/ghzserg/zmod/blob/001703371c90e228defce41bb4230b7fafd37fea/docs/en/AD5X.md

## Required implementation
- Use native infinite-spool handling. No AI polling/switch loop, new plugin or MCP.
- Verify same stored color and material type; additionally check compatible material variant and calibrated profile. These extra compatibility checks are local requirements, not claimed firmware capabilities.
- Keep slicer tools T0/T1 distinct from physical IFS slots and backup candidates.
- COLOR assigns spool metadata. SET_EXTRUDER_SLOT records which spool is loaded; it does not configure backups.
- Exclude unavailable/empty or unverified backup candidates. No eligible backup means native pause/error recovery, not guessed substitution.
- Preserve existing job identity/accounting and latency; no required new LLM calls or rendering.

## Acceptance
Offline tests: compatible backup, different color, different material, incompatible variant/profile, empty backup, stale metadata, exhausted candidates, remapped active slot and duplicate events. Installed-mode acceptance belongs to printer specialist. Native selection order and installed firmware/screen behavior have not been verified. No forced runout or physical print authorized by this reference phase.

## Compatibility
AD5X-specific docs govern over generic Klipper recommendations. Preserve existing start/end, purge, checksum, mesh/Z and IFS authority. Review object-exclusion crash warning before adoption. Keep existing Orca Flatpak. Bambufy is optional and officially incompatible with the native screen; no installation in MVP.

## Status
Contract delivered; runtime fallback integration and physical acceptance remain OPEN in. Existing hardening remains paused until coordinator release. Notify/Apprise audit is deferred, existing Discord behavior unchanged.

## Motion boundaries, margin contract, and multi-plate intake (1.9.1)
- **Physical sheet and motion limits**: The AD5X uses a 235x235 mm spring steel build plate. Klipper stepper travel reaches X [-20.0, 225.0] (245 mm total) and Y [-20.0, 232.0] (252 mm total).
- **Edge margins**: Single-copy prints use a 1.0 mm bed margin; multi-copy prints use a 3.0 mm margin. Parts up to ~218x218 mm fit on the nominal 220x220 mm bed area without endstop triggering or frame collision.
- **Multi-plate capacity evaluation**: Orca plate capacity tests multi-plate projects per-plate rather than across the combined canvas. If any plate exceeds the bed, binary search evaluates the minimal uniform scale ladder (e.g. 94%, 75%, 50%).
- **Plate boundary preservation**: Scaled multi-plate projects preserve plate separation during slicing, support per-plate scale overrides, and group parts by `(plate_id, slot)`.
- **Conversational continuity**: Affirmative replies ("Let's do it", "Print it", "Go ahead") automatically recover the candidate model link from conversation history. Failure messages omit prompts to resend links.

## Units, slicer fallback, and motion limits (1.10.0)
- **Units**: STL files do not record units, so an STL whose largest dimension is under 10 mm is not assumed to be in inches. Intake asks the user to confirm millimeters or inches and records the answer as source-bound unit provenance on the job (`unit_decision` / `unit_provenance`); inches print at 25.4x when the result fits the printer volume, otherwise the job fails with its measured and would-be sizes. An explicit user scale (including 1.0) settles the size and is never auto-scaled, and parts Print Handy generated itself in millimeters are never auto-scaled. Slicing a small STL with no recorded confirmation refuses with the measured size and asks for the confirmation.
- **Slicer order**: OrcaSlicer first; Bambu Studio CLI only after an Orca slicing failure. Both outputs pass the same post-slice and protected-settings gates. Bambu receives absolute line widths derived from nozzle diameter.
- **Acceleration**: Process acceleration is capped at 4,500 mm/s² (outer wall 3,200), below the measured X input shaper recommendation of 4,747 mm/s².

## Delivery format addendum
- Prints may be delivered as a `.gcode.3mf` package when the printer reports ZMod 1.7.5+ (`[printer] zmod_delivery_format`, default `auto`; see docs/ZMOD.md "Print file format"). The checksum authority above refers to the inner `.gcode`, which the package carries byte-identical; the Moonraker upload checksum covers the package bytes. Native infinite-spool behaviour is printer-side and unaffected by the container the file travels in.
