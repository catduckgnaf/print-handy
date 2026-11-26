# Klipper 0.14.0 contract audit

**Audit scope:** Print Handy's current Klipper and Moonraker contracts against upstream tag [`v0.14.0`](https://github.com/Klipper3d/klipper/tree/v0.14.0), released November 20, 2026. This is a source and offline-fixture audit only; no printer hardware was queried or tested. Generic Klipper-derived forks may diverge from upstream and are not certified by this audit. public-text-ok

## Upstream changes relevant to Print Handy

- Klipper's release notes describe multi-axis `G1` support and runtime-defined extra axes, and the configuration change log explains that coordinate status arrays can now have more than four components, with ordering and count varying at runtime. The API status reference instructs clients to use `gcode_move.axis_map` or `toolhead.extra_axes` for non-XYZ components. Print Handy's inspected consumers of status positions use the first three entries for X/Y/Z or Z only; no production code assumes a fixed fourth entry is E. `tests/fixtures/klipper/v0.14.0-dynamic-coordinates.json` records a five-element coordinate and a non-fourth E index; the offline test feeds that fixture through `klipper_report.build`, which reads Z from `gcode_move.gcode_position` or falls back to `toolhead.position`, and asserts the Z value is still read correctly.
- Moonraker's configfile response still exposes raw `config` values as strings. Klipper's stepper `position_min` remains a valid setting and may be negative. Print Handy's geometry reader incorrectly clamped negative minima to zero, understating travel for a negative minimum. It now computes `position_max - position_min` directly, with a regression case of `-5..245` yielding 250 mm.
- Klipper 0.14 does not document the previously removed `max_accel_to_decel`, `ACCEL_TO_DECEL`, or `max_stepper_error` settings as restored. Print Handy's production Python contracts contain no references to these settings. The firmware update report's MCU currency warning intentionally remains specific to ZMod's Klipper 0.13.x `UPDATE_MCU` behavior; it does not claim an MCU mismatch for 0.14 hosts.
- The reviewed contracts continue to rely on Moonraker's update-manager status for a host component version and Klipper's `mcu.mcu_version` object for MCU firmware. The latter is a firmware build version, not a reliable host source version for arbitrary forks. Version-based host/MCU equality checks must remain fork- and installer-specific; this code only evaluates ZMod 0.13.x and otherwise reports unknown.

## Baseline decision

The weekly watch baseline advances from 0.13.0 to 0.14.0 because the upstream release, status reference and configuration change log were audited against the affected contracts here. The reference AD5X's tested baseline remains independently documented as Klipper 0.13; a global watch baseline is not a claim that the printer or every vendor fork runs 0.14.

## Evidence

- [Klipper 0.14 release notes](https://www.klipper3d.org/Releases.html#klipper-0140)
- [Klipper configuration changes](https://www.klipper3d.org/Config_Changes.html)
- [Klipper status reference: accessing coordinates](https://www.klipper3d.org/Status_Reference.html#accessing-coordinates)
- [Klipper config reference: stepper positions and generic Cartesian](https://www.klipper3d.org/Config_Reference.html)
- [Klipper source tag v0.14.0](https://github.com/Klipper3d/klipper/tree/v0.14.0)
- [Moonraker API: printer objects and configfile](https://moonraker.readthedocs.io/en/latest/web_api/)

## Limits

Offline tests establish Print Handy's parsing and fallback behavior against recorded response shapes, not that a particular printer's fork, Moonraker build, macros, or physical geometry matches upstream. Coordinate arrays are only interpreted for XYZ where inspected; consumer behavior for arbitrary extra axes is deliberately not inferred. No hardware compatibility, firmware update safety, or MCU flashing behavior was validated.
