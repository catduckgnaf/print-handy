# Knowledge registry

Package troubleshooting knowledge for Print Handy (issue #169). These files are
data, read by `print_handy/knowledge/` and surfaced through `/print-lookup` and
`/print-troubleshoot`. They are advisory only: nothing here is ever sent to a
printer by the lookup or troubleshooting code.

## Files

- `symptoms.toml` — the symptom index: what a user reports, the flow that works
  it, and the firmware entries that belong with it.
- `troubleshooting.toml` — the guided physical troubleshooting flows.

## Schema

### symptoms.toml

One `[[symptom]]` per entry:

| Field | Meaning |
| --- | --- |
| `id` | Stable slug, unique across the file. |
| `title` | The symptom as a person would state it. |
| `aliases` | Other phrasings lookups should match. |
| `flow` | A flow `id` from `troubleshooting.toml`, or `""` when no flow should walk the user through it (heater errors stop at advice on purpose). |
| `firmware_entries` | Entry ids from the per-firmware files in `../../data/firmware-commands/`. |
| `summary` | One or two sentences: the likely shape of the problem. |
| `first_checks` | Read-only observations to make first. Never printer actions. |

### troubleshooting.toml

One `[[flow]]` per workflow, with `[[flow.node]]` graph nodes under it:

| Field | Meaning |
| --- | --- |
| `id` | Stable slug; this is the `/print-troubleshoot <id>` argument. |
| `title`, `aliases` | Display name and lookup phrasings. |
| `safety` | Warnings shown before the first step, every time. Physical hazards first. |
| `start` | The `id` of the first node. |
| node `id` | Unique inside the flow. |
| node `kind` | `question` (branches) or `advice` (one bounded check or fix). |
| node `prompt` | The question, or the check/fix text. |
| node `option` | Question only: `label` (the button text) and `next` (a node id). |
| node `next` | Advice only, optional: continue to this node after showing the advice. No `next` ends the flow. |

Rules the loader enforces (`print_handy/knowledge/registry.py`): unique ids,
every `next`/`start` resolves inside the flow, every question has at least two
options, every symptom's `flow` and `firmware_entries` exist, and no flow text
may present a printer action as something the bot will do for the user.

## Adding knowledge

Add the symptom, then the flow, then check both with
`python3 -m pytest -q tests/test_knowledge.py`. Keep steps bounded and physical:
one observation or one small change per node, with the safety warning that
belongs with it.
