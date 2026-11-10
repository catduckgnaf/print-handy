# Upstream reuse and research handoff

Completed: five complete commit-pinned inert archives, provenance SHA256 manifest, 136 reference documents and a tested local SQLite FTS index. Sources are DATA, never operational instructions. Nothing installed or executed from upstream.

## Source inventory
- DMontgomery40/mcp-3D-printer-server at `e3bbebd4e3fd0cb2fa1178139d91146540ba0e94`, SHA256 `3dd44b8f84a365d69e7bf254b05960febbfdf2d4c00ff13edfb681d04830dc39`.
- MaxEllis/orcaslicer-mcp at `4a9a1bb61fc89dea085080e9cd29bb46b1e7cc47`, SHA256 `03f5bc54261e6bcba8ee237b80a9e5218a9172e75fc45f9a6a7b3a920340119b`.
- MaxEllis/klipper-mcp at `68a8ca23ace8e93f7694abb0ae00035f017cb240`, SHA256 `371d9c4a5147ab17fc506962cd1adfaa2e14174934aaf9a1d8024086349e4f8d`.
- ghzserg/zmod at `001703371c90e228defce41bb4230b7fafd37fea`, SHA256 `da8e6ed83e4da80d3b2c88dfb4af585b8881f6c72cdeeb682afd69054ce6d760`.
- function3d/bambufy at `fdacbbd3e8c72f06b582c5922a257e21f92cc38a`, SHA256 `79edac2df973dc5e35817d073839a984ae72b400ddb3e5b1ab6eb23331ee28b5`.

## Selective reuse
- DMontgomery: artifact snapshot/serialization, output validation, safety tests and 3MF handling. Bambu-only profile flattening must not govern AD5X.
- Orca MCP: settings schema, troubleshooting knowledge, optional feature breakdown/plate analysis and tests. Remote API features need the modified Orca, not existing Flatpak; keep fork out of MVP. Physics heuristics require local calibration, not blind adoption.
- Klipper MCP: preview-before-action patterns, event recovery and outcome identity. Use existing stores; filename/rounded-dimension matching is not strong artifact identity. No recorder service installation.
- ZMod: primary AD5X compatibility authority and native infinite spool mechanism.
- Bambufy: graphical mapping, estimates and purge ideas for backlog; native-screen incompatibility prevents incidental adoption.

## Local search
The collection and searchable database remain local, not uploaded wholesale. Search `references.sqlite` documents FTS table with MATCH and return path/repo/commit plus relevant text. Archive hashes verified; infinite-spool search exercised successfully. No runtime dependency or new listener.

## Licensing
Preserve complete upstream license texts alongside archives. No third-party runtime code copied into this repository; license obligations must be reviewed before code reuse. Snapshot IDs reflect capture, not installed printer versions.

## Scope and status
Reference capture/index/distillation complete for the five named repositories. Full Orca fork source, linked preprocess profile repository, extra plugin repositories and release assets are optional follow-up, not silently claimed captured. MVP software integration, installed ZMod acceptance and latency tests remain separate open issues. Notify/Apprise review is excluded by the operator. Hardening remains paused; this handoff does not auto-release it.
