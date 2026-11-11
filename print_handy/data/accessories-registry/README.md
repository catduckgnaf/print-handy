# Accessories registry

Package data index of the printer accessories Print Handy knows about (issue #1198).

The executable accessory profiles stay where they are:
`print_handy/printers/accessories.py`, one profile per key in the
`ACCESSORIES` registry, resolved by the printer adapter from that printer's
own `accessories` list (on its `[[printers]]` entry, or the top-level list of a
single-printer config). This registry is the discoverable front door
to those profiles: what each accessory is, what host it attaches to, how it
is detected, and what behavior it changes, in one place for lookups,
troubleshooting and humans browsing the repository.

## Files

One TOML file per accessory vendor, one `[[accessory]]` per registered
accessory. An accessory's entry lives in the file named for its key's vendor
prefix (`bondtech.indx` in `bondtech.toml`). The table below is the schema
for every entry, whichever file it sits in:

| Field | Meaning |
| --- | --- |
| `key` | The `vendor.accessory` key, identical to the profile's key in `printers/accessories.py` and the `[accessories]` config value. |
| `label` | Display name, identical to the profile's label. |
| `kind` | What the accessory is: `toolchanger` for a position-shaping tool changer. Sensor and thermal kinds stack alongside a position-shaping accessory instead of replacing it. |
| `confers_color_system` | The effective color system the accessory confers on its host printer, or empty when it does not shape positions. `shared_tool` (passive tools on one shared extruder and heater) exists only as an accessory-conferred system; no printer model declares it. |
| `connections` | The vendor connection types the accessory can attach over, from the profile's host requirements. |
| `hosts` | The host printers whose adapters implement the accessory, in words. Settings refuse the accessory on any other printer by name, and an adapter whose class does not list the accessory in `supported_accessories` never resolves it. |
| `detection` | The verified detection signal, or `manual only` when there is none. Nothing is detected from a guessed object name. |
| `notes` | Quirks a troubleshooter should know before trusting the record. |

Tests (`tests/test_accessories.py`) compare every entry with the code
profiles in `printers/accessories.py`, so this file fails CI instead of
drifting when a profile changes. Adding an accessory means adding its code
profile and its registry entry in the same change.
