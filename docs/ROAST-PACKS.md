# Roast Engine 2.0: externally sourced packs

The existing corpus already has a floor of 1,500 entries. This project targets
1,000 additional distinct, reviewed external jokes, not generated variants.

## Import and use

Prepare local JSON files containing an array (or an object with an entries array).
Each entry must have text, source (canonical URL or source work), license
(verified rights basis), reviewed=true, category, and optionally keywords
and family. Review the actual joke rights separately from the repository's
software license. The tool trusts your review; it cannot establish copyright
ownership. A keyword list (up to 12 explicit aliases or phrases) makes a
joke title-only; omit it for a general joke. The existing one-sentence,
20-word contract remains in force. Do not truncate a setup or punchline.

From the repo root:

    python -m scripts.import_roast_pack --output /path/roast-pack.json approved-dad.json approved-science.json approved-satire.json

The command defaults to 1,000 accepted unique entries, checks shipped and
imported duplicates and the existing blocklist, and reports categories and
rejected rows. It does not overwrite the output when the threshold fails.
Configure vision.roast_custom_bank to the resulting file accessible to Print
Handy, then restart. The existing local pack setting accepts up to 5,000
entries and 2 MiB. There are no runtime network calls or AI dependencies.

The selector preserves first-draw fact preference and gives title matches
a turn after three prior selections without one. Explicit multiword tags
beat single-word tags; failed/cancelled/spaghetti still use their own banks.

## Research and remaining work

- https://icanhazdadjoke.com/api : searchable dad jokes; check https://icanhazdadjoke.com/tos.
- https://jokeapi.dev/ : programming/puns/dark categories and content flags.
- https://github.com/jonas-werner/dad-joke-api : curated corpus and stable IDs.
- https://huggingface.co/datasets/yoonholee/humor-greats-public-domain : public domain candidate; check original works.
- https://github.com/taivop/joke-dataset and https://github.com/amoudgl/short-jokes-dataset : research corpora, not blanket redistribution permission.

No third-party jokes are bundled by this first PR. Follow-up issue #1955
tracks rights-cleared acquisition, the 1,000-entry curated pack, two-part
jokes, adjustable intensity, topical packs with expiry, celebrity/political
and pop culture satire, and human quality review. Keep satire aimed at the
print and avoid treating jokes as factual allegations.
