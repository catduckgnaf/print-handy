# Draft notes for #1778 AI fix session

## Implemented in this draft
- Session lifecycle (start, get, end, timeout, accept/cancel)
- Offer label when AI ready
- Routing helper for mention + file + fix instruction
- Placeholder process_turn that records history

## Still needed for acceptance criteria
- Wire offer into problem intake questions (prepared_intake / slice_result)
- Capture follow-up messages in adapters (discord, telegram, matrix, web) without mention
- Full tool loop: AI function calls → deterministic execution → re-slice → report
- Geometry tools from #1777 (island_scan, overhang_scan, trim_below_plane, gusset_45, etc.)
- Accept path: repoint job at copy, re-enter intake, confirm-before-print
- Tests covering the acceptance criteria
- Web UI chat parity

Related: #1777 (tools), #978 (closed vocabulary), #903 (edit chat), #1759 (AI ready visibility).
