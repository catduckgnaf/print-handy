# Firmware registry

Package index of firmware commands, macros and error codes for Print Handy
(issue #169), read by `print_handy/knowledge/` and surfaced through
`/print-lookup`. Entries are reference data: a lookup returns the text, it never
sends the command. Sending anything to a printer stays behind the existing
guarded print-control flows and their confirmation buttons.

## Files

One file per firmware family in this directory (issue #644): `zmod.toml`,
`klipper.toml`, `marlin.toml` and `prusa.toml`. An entry lives in the file
named for its `firmware` value — the loader loads every `*.toml` in
this directory in firmware-family order (the sequence the single file used,
so name resolution behaves exactly as it did before the split), merges
them into one registry, and rejects an entry whose `firmware` field
disagrees with its file, so the partition cannot silently drift. Splitting by brand instead was rejected:
an entry applies along its firmware, not its brand (the AD5X entries are
ZMod macros, shared Klipper entries span brands, and single brands span
firmwares).

Each file holds one `[[entry]]` per command, macro or error:

| Field | Meaning |
| --- | --- |
| `id` | Stable slug prefixed with the family: `zmod.`, `klipper.`, `marlin.` or `prusa.`. |
| `name` | The command or macro name as the firmware knows it, or the error text. |
| `aliases` | Phrasings lookups should match (for example "ifs load"). |
| `firmware` | `zmod`, `klipper`, `marlin` or `prusa`. ZMod is listed separately from stock Klipper because the AD5X macro set and authority rules differ. Prusa is listed separately from Marlin because Buddy firmware is Prusa's own fork of Marlin 2 with a published Prusa-specific command layer (the M862 print checks and friends, issue #647); its file describes only that layer, not the generic Marlin-compatible codes. Must match the file the entry sits in. |
| `kind` | `command`, `macro` or `error`. |
| `printers` | Registered printer model keys (see `../printers-registry/<vendor>.toml`) the entry is known to apply to, or `["*"]` for every printer on that firmware. |
| `pack` | Optional. Names a macro pack that rides on the firmware (for example a vendor or community macro pack layered over stock Klipper); absent means the firmware's stock set. A lowercase slug. Lookups can filter on it alongside firmware and printer. No entry carries one yet — the field exists so a justified pack entry has a scope of its own instead of borrowing a printer key or the stock set's name. |
| `command` | The literal command line, with `<placeholders>` for values. Empty for errors. |
| `description` | What it does, when to use it, and the traps. |
| `safety` | What it can do physically (heat, motion, extrusion, restart) and the guard that applies. |
| `notes` | Optional extra context. |

Only add an entry you can stand behind: prefer commands the repository already
uses or documents, and say where an entry is generic firmware knowledge rather
than verified on a Print Handy printer. The loader validates the schema
(`print_handy/knowledge/registry.py`) and the tests pin the ZMod, Klipper,
Marlin and Prusa coverage the issues ask for, including the decided state of
every non-Moonraker family (issue #647; the verdicts themselves live in
`docs/MACROS.md`).
