## Upstream issue tracker watches

The weekly upstream issue watch checks Forge-X (`DrA1ex/ff5m`) and OpenCreator (`FlashForge-C5-Modding-Group/OpenCreator`) for issues updated in the last week that match the editable keywords in `docs/upstream-issue-watch.toml` as whole words or phrases. It opens one labeled, deduplicated Print Handy issue per match, keyed by the upstream repository and issue number, so a renamed upstream issue is not tracked twice and a tracking issue closed here stays closed. The watch is notify-only and never writes to upstream trackers. The separate firmware watch continues to track new firmware releases against audited baselines.

To catch up on every open upstream issue rather than the last week's, run the workflow by hand with **all_open** set, or run `python3 scripts/check_upstream_issues.py --all-open --dry-run` to preview.
