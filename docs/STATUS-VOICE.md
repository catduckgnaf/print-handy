# Print Handy status voice

Print Handy now ships 1,000 locally selected, stage-specific sarcastic status lines,
organized as **25 banks of 40**. They require **no LLM**, no external service, and no
printer connection to choose. The original factual status message remains the
authoritative account of what Print Handy is doing.

## Live intake integration

The intake flow currently adds optional humor to three actual moments:

1. **Model color inspection**: only when the model color manifest needs reading.
2. **Build plate copy capacity**: while the fit/capacity probe is underway. The
   previous wording, "Checking whether it fits the bed", undersold the actual
   work. The new factual heading says "Checking fit and calculating plate
   capacity" without claiming an answer before the probe completes.
3. **Quantity selection**: after the user selects a number of copies, after the
   quantity has been checked against the actual verified plate capacity and saved.

The other 22 banks are ready for follow-on wiring to the matching factual
lifecycle events, not falsely advertised as active integrations. For example,
a completed-print message may only be selected after a real completion event;
a failure message must never be used for an ongoing job.

## Copy count Easter eggs

Three entries live in `planning.json` as exact-copy, first-time messages:

* **2**: "You know what I would do with a million dollars? Two prints at the same time man."
* **10**: "There are 10 types of people in this world, those who understand binary and those who don't."
* **11**: "Eleven copies? These go to eleven. Nigel Tufnel would be proud."

The first verified choice of each exact number triggers its respective line
**guaranteed**, not via a probability roll. They are independent per user and
platform. Later choices of the same count use ordinary status lines. A stable
job key preserves an already chosen line on message replay.

When user identity is unavailable the bot scopes the first-time flag to the
platform and chat. First-time flags are persisted as hashes in
`status_voice_state.json`, in the configured profile state directory.
Selection uses a thread lock and a Linux advisory file lock around atomic
writes, so simultaneous requests from the same bot installation do not
silently both win the first-time flag.

## Corpus contract

Files: `print_handy/data/status_voice/{intake,preflight,planning,slicing,printing}.json`.

Each entry has an `id`, `family`, and `text`. Entries requiring
`{copies}` are eligible only when a verified positive integer is supplied.
First-time entries additionally carry `exact_copies` and `first_time`.
New entries must be tagged with the **real** stage, not a plausible-sounding
future outcome. The bank loader checks exact text and ID uniqueness, known
placeholders, bounds, and exactly 1,000 entries across 25 stages.

The selector has a persisted per-stage shuffle bag, global recent-entry
history, and family spacing to reduce repetitive wording. User-provided
content is not interpolated into these lines.

## Relationship to the existing print roasts

The ordinary completed-print/failure/spaghetti roast engine remains separate
(`print_handy/core/roast.py`). It already supports stable IDs, custom
roast banks, a global history window, and fact-bound templates. The only
behavior change to that engine here is **fairer selection among eligible
factual banks**. A long, heavy print, for example, can receive both long-print
and heavy-filament roasts over multiple jobs instead of always taking the
first fixed-priority factual bank. Reprint-after-failure keeps its
special precedence; status-first selection and title/generic fallback remain.

No approval gate, model geometry, slicing, capacity number, startup command,
or printer readiness rule is changed by this feature. If the optional status
voice fails to load, the factual message still goes out without its joke.

## Testing

Run:

```sh
pytest -q tests/core/test_status_voice.py tests/core/test_roast_engine.py tests/pipeline/test_prepared_intake.py
```

The copy-count tests cover first-time guarantees, independence, replay,
concurrent requests, and quantity values greater than verified capacity.
The content tests assert 1,000 unique entries. This PR does **not**
authorize a live printer test or deployment.
