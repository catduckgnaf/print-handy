# Optional Flash Studio slicer (Flashforge only)

Print Handy can use an existing, user-installed Flash Studio executable for slicing. It does not download, install, update, or configure Flash Studio. The setting is an opt-in executable path; when it is empty, Print Handy keeps its normal OrcaSlicer choice and existing fallback policy.

Set it in `config/local.yaml`:

```yaml
slicer:
  flash_studio_path: /opt/flash-studio/FlashStudio
```

Use the path to the executable itself, not its containing folder. `~` is expanded. The file must exist and be executable. This path is intended for Flashforge printers; it is rejected for other printer vendors. Keep machine-specific paths in a local config file rather than editing the tracked home defaults.

When configured, Flash Studio is an exclusive primary slicer choice for the run: Print Handy does not try OrcaSlicer or Bambu Studio if the configured file is broken, the CLI check fails, slicing returns an error, no G-code is created, or the output does not identify itself as Flash Studio. The failure is reported rather than silently producing output with another slicer. Output still passes Print Handy's normal G-code/post-slice checks.

Before slicing, Print Handy runs the configured executable with `--help` and verifies that the help text advertises the headless flags this slice needs (settings and filament loading, output directory, slicing, and any arrange/clone options in use). It then runs the resolved slice command and requires a non-empty `plate_1.gcode` whose generator header names Flash Studio. This is a conservative compatibility check, not a guarantee that every Flash Studio build supports every profile or Flashforge feature Print Handy uses. A real Flash Studio installation was not invoked by the offline test suite.

## Project and CLI references

- [Flash Studio Desktop](https://www.flashforge.com/pages/flash-studio-desktop) is Flashforge's current desktop slicer, formerly Orca-Flashforge, and is based on Orca Slicer.
- [FlashForge/Orca-Flashforge source repository](https://github.com/FlashForge/Orca-Flashforge) identifies the project as AGPL-3.0 and describes its Orca Slicer foundation.
- [`version.inc`](https://github.com/FlashForge/Orca-Flashforge/blob/main/version.inc) identifies the application as “Flash Studio” (`SLIC3R_APP_NAME`).
- [OrcaSlicer CLI mode](https://www.orcaslicer.com/wiki/cli/cli_mode) and [CLI options](https://www.orcaslicer.com/wiki/cli/cli_misc) document the upstream headless slicing and option conventions. Flash Studio's own installed `--help` remains authoritative at runtime; unsupported options are rejected before slicing.
