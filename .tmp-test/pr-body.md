## Summary and user impact

Settings restructure stage 2 of 5 (stacked on #1097): every AI surface in the Web UI settings now lives in one **AI and models** group — the chat, design and agent endpoints, the vision endpoint and model, and the [ai] edit gate. The [ai] tier tables are named in the group as a file-only subsection, so their home is discoverable from the UI instead of silent. Stacked on #1097 (stage 1 regroup); stages 3–5 follow on this branch chain.

## Problem and cause

Not applicable.

## Proposed changes

- GROUP_OF: `llm.*`, `design.*`, `agent.*` endpoint pairs move from the old endpoints group, and `services.vision_chat_url` + `vision.model` move out of the camera group, into the new `ai` group; the pages group is renamed "AI and models".
- The endpoint inheritance chain is stated once, on the chat endpoint's help (design reuses chat when empty; vision is the small tier's default route; chat is the chat tier's; design, inheriting chat, is the heavy tier's; a tier value in the settings file wins one field at a time), instead of being repeated or implied per field.
- New catalog fields for the existing [ai] edit gate (#902): `ai.allowed_users` (id_list) and `ai.edit_requests_per_hour` (number, minimum 0), with their two ratchet exemptions in tests/test_variables_contract.py removed.
- GROUP_FILE_ONLY_NOTES in catalog.py, rendered by the config page inside the group: the [ai] tier tables ([ai.small]/[ai.chat]/[ai.heavy] with their seven keys each) and the *_api_key_env settings are named as file-only. No table/repeater field kind is built.
- Named remainder, deliberately not in this PR: the vision key gap (vision has no api_key_env setting; CLIPROXY_API_KEY is read directly at the vision call sites). Wiring a `vision.api_key_env` through the settings tree, the listener and the watchers sprawls well past this stage's catalog scope, so it is left as a named follow-up rather than half-done here.

## Type of change

- [ ] Bug fix
- [x] User facing feature
- [ ] Maintenance or refactor
- [ ] Documentation
- [ ] CI or tests
- [ ] Breaking change

## Verification and test results

- Checks run: focused local suites on this worktree with a disk-backed TMPDIR — tests/test_webui_config.py, tests/test_webui_config_page.py and tests/test_variables_contract.py: 54 passed, 436 subtests passed (finished log in the lane worktree, .tmp-test/stage2-focused.log); tests/test_webui_walkthrough.py: 28 passed. CI on the exact head was read after opening and is reported with the PR's delivery note.
- Checks not run and why: the full local suite was not run — this VM is shared with parallel lanes and the guidance for it is focused local runs plus CI on the exact head as the authoritative full result.

## Tests added and regression scoping

Tests added: added
Tests added detail: tests/test_webui_config.py gains test_ai_section_fields_groups_and_gate (vision endpoint/model and both [ai] gate fields in the ai group with their kinds, the gate number's minimum, and no catalog field for any [ai] tier) and the settings-surface-fix pin table moves the six endpoint fields to the ai group; tests/test_webui_config_page.py now expects the "AI and models" heading, the rendered file-only tier note, and both gate field inputs on the page; tests/test_variables_contract.py drops the two #902 webui exemptions the new fields retire.

Regression scoping: checked
Regression scoping reason: Neighboring surfaces considered: the config page render and save flow (config page suite run locally, unchanged behavior apart from the new note paragraph), the walkthrough (suite run locally; it targets groups by field, unaffected), and the variables ratchet (run locally; removing the two exemptions is the point of the change and it passes because the fields now exist). The full offline shards run in CI on this exact head. Deliberately left out: live Web UI checks (no live instance is this lane's to use) and the vision api_key_env gap named above.

## Risks, limitations, and follow up

Stacked PR: the base is feat/settings-regroup-stage1 (#1097), not main; if #1097's head moves, this branch needs a merge-up before it can land. Stages 3 (side sub-tabs), 4 (conditional Telegram/Matrix) and 5 (Data/Wrapped split and defaults) stack on this branch in order. The catalog's field count grows from 104 to 106; an unchanged submit still round-trips (the parse set is the catalog itself).

## Related issues

- Related: #1097 (stage 1, base of this stack), #902 (AI edit gate), #818 ([ai] tiers)
- Fixes:

## Scope and acceptance criteria

- Acceptance criteria: all AI fields render in one AI and models group; the inheritance chain is stated once in the group help; both [ai] gate keys are editable fields whose ratchet exemptions are gone; the tier tables are named file-only in the rendered group; no loader, validator or default changes.
- Out of scope / remaining work: stages 3–5 (stacked follow-ups), the vision api_key_env gap (named remainder above), bot.mode and discord.clarify_ui (Hermes removal lane), the mention setting rename (#1098).

## User-facing release note

The Web UI settings page now keeps every AI setting in one AI and models group: the chat, design and agent endpoints, the vision endpoint and model, and the AI edit gate — who may use the AI edit lane and how many edit requests per hour — which can now be set from the page instead of only in the settings file. The [ai] tier tables stay in the settings file and are named as file-only in the group.

## Safety, compatibility and deployment

- Safety/compatibility impact: None. No setting is added, renamed or re-defaulted; two existing settings gain a Web UI field. The gate fields use the existing id_list and number kinds and the real settings validator on save ([ai] allowed_users list shape, edit_requests_per_hour a whole number 0 or more). No confirmation, readiness or clearance guard changes.
- Live checks or deployments performed (or "None"): None

## Documentation and attribution

- [x] Relevant setup, configuration and user documentation updated, or reason given below.
- [x] Contributor credit and third-party licenses preserved where applicable; no invented coauthors.
- [x] No credentials, private model files or personal runtime records included.
- [x] No local paths, machine or host names, private addresses or accounts appear in this PR, its commits or its comments (the Public text policy check enforces this).

## Documentation impact

Documentation: updated
Reason: docs/WEBUI.md group list and group description updated to AI and models including the gate fields and the file-only tier note; docs/SETTINGS.md [ai] rows updated — ai.allowed_users is no longer file-only and both gate rows name the Web UI group. The SETTINGS.md "Model endpoints" section heading is unchanged on purpose: it documents endpoint routing, not the Web UI group name, and anchors elsewhere link to it.
Wiki: not applicable
Wiki reason: The versioned docs updated in this PR cover the settings-page change; the wiki carries no separate settings-group page that names the old endpoints group.

## Surfaces and variables

Variables: checked
Variables reason: No variable is added or renamed; two existing variables gain the Web UI surface. ai.allowed_users — defaults tree: present ([ai] section, validator setdefault []); environment loader: PRINT_HANDY__AI__ALLOWED_USERS accepted by the generic env layer (the .env.example sample lists the sibling platform allow lists, not this one); Web UI catalog: id_list field added in this PR; consuming code: the #902 AI edit gate in the settings validator's consumers (ai_routes/edit lane); docs: SETTINGS.md row updated in this PR; Unraid templates: not a template variable today and none is added (settings are Web UI/YAML reachable, per the template-slimming rule). ai.edit_requests_per_hour — defaults tree: present (validator setdefault 20); environment loader: PRINT_HANDY__AI__EDIT_REQUESTS_PER_HOUR already in .env.example; Web UI catalog: number field (minimum 0) added in this PR; consuming code: the same #902 gate; docs: SETTINGS.md row updated; Unraid templates: same verdict as allowed_users.

## Reviewer checklist

- [x] User-visible behavior, configuration and limitations match the docs and release note.
- [x] Documentation impact and wiki impact were reviewed, including any exemption or deferral.
- [x] Tests cover the changed behavior; reported checks match actual evidence.
- [x] Tests added are named in the Tests added section, or the reason for adding none is specific and credible; added tests pin the changed behavior or regression, not only the happy path.
- [x] Regression scoping is credible: the full suite and neighboring regression areas were considered, and any exclusion or coverage gap is named with its reason.
- [x] Safety/authorization guards and contributor attribution are preserved.
- [x] Behavior changes were checked against every adapter/connection in their scope (the printer-scope rule: a change for one printer family is verified for all families it touches, not only the one at hand).
- [x] Registry/data changes keep their parity and drift tests green, or the exemption list is updated with a tracking reference.
- [ ] Required current-head CI checks pass before merge; no deployment or live action is implied.
