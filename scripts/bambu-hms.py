#!/usr/bin/env python3
"""Generate Print Handy's Bambu Lab HMS error table from Bambu Studio's own HMS resources.

    python3 scripts/bambu-hms.py --hms-dir <folder with hms_en_*.json> --commit <BambuStudio commit>
    python3 scripts/bambu-hms.py --hms-dir <folder> --commit <sha> --check

Provenance (#306, from the #298 Stage 0 audit): the code -> description mapping for Bambu HMS
faults is Bambu Lab's own data, bundled in Bambu Studio under `resources/hms/hms_en_<prefix>.json`
(one file per device prefix; each has `device_hms`, keyed by the 16-hex attr+code, and
`device_error`, keyed by the 8-hex value, every entry carrying `ecode`/`intro`). Print Handy's
table is generated from those files by THIS script — Print Handy's own generator — at a pinned
Bambu Studio commit recorded in the output. No third-party catalog is used: in particular nothing
is taken from maziggy/bambuddy (AGPL-3.0; protocol facts only, see docs/BAMBU-CONNECTIVITY.md).

What the generator does:
- Merges the per-prefix files: the common text for a code is the text most prefixes agree on
  (a tie between most-frequent texts goes to the one from the lowest sorted prefix carrying
  one of them); a prefix whose text differs keeps it as a per-model
  override. The two key spaces are kept strictly separate (16-hex HMS vs 8-hex print_error).
- Shortens each description to its first sentence: Bambu's `intro` usually leads with the fault
  description and follows with remediation steps; Print Handy's status output shows the fault.
- Preserves listed-but-empty texts as "" — a code Bambu lists with no text is listed, not a fault.

--check writes nothing: it regenerates in memory and fails (exit 1) when the shipped table differs.
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "print_handy" / "printers" / "bambulab" / "hms_data.json"


def concise(intro):
    """Print Handy's concise form of a Bambu intro text: the first sentence, whitespace-normalised."""
    text = re.sub(r"\s+", " ", (intro or "").strip())
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
    return first.strip()


def merge(files):
    """files: {prefix: studio json} -> (common, overrides) for one key space section."""
    per_code = defaultdict(dict)  # code -> {prefix: text}
    for prefix in sorted(files):
        for entry in files[prefix]:
            per_code[entry["ecode"].upper()][prefix] = concise(entry.get("intro"))
    common, overrides = {}, defaultdict(dict)
    for code, texts in per_code.items():
        counts = Counter(texts.values())
        best = counts.most_common(1)[0][0]
        if len(counts) > 1:
            top = counts.most_common()
            if top[0][1] == top[1][1]:
                # Tie: the text from the lowest sorted prefix *carrying one
                # of the tied, most frequent texts* wins (P2-102) — the
                # lowest prefix overall may carry a minority text.
                tied = {text for text, n in top if n == top[0][1]}
                best = next(texts[p] for p in sorted(texts) if texts[p] in tied)
        common[code] = best
        for prefix, text in texts.items():
            if text != best:
                overrides[prefix][code] = text
    return common, dict(overrides)


def build(hms_dir, commit):
    files = {}
    for path in sorted(Path(hms_dir).glob("hms_en_*.json")):
        prefix = path.name[len("hms_en_"):-len(".json")]
        data = json.loads(path.read_text())["data"]
        files[prefix] = data
    if not files:
        raise SystemExit(f"no hms_en_*.json files in {hms_dir}")
    hms, hms_by = merge({p: (d.get("device_hms") or {}).get("en") or [] for p, d in files.items()})
    err, err_by = merge({p: (d.get("device_error") or {}).get("en") or [] for p, d in files.items()})
    return {
        "_provenance": {
            "source": "bambulab/BambuStudio resources/hms/hms_en_<prefix>.json (device_hms + device_error, ecode/intro)",
            "bambu_studio_commit": commit,
            "generated_by": "scripts/bambu-hms.py (Print Handy's own generator; no third-party catalog used)",
            "texts": "Bambu's intro texts shortened to the first sentence by the generator; "
                     "listed-but-empty texts are preserved as empty strings",
            "prefixes": sorted(files),
        },
        "hms": hms,
        "hms_by_model": hms_by,
        "error": err,
        "error_by_model": err_by,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hms-dir", required=True, type=Path, help="folder holding Bambu Studio's hms_en_*.json files")
    ap.add_argument("--commit", required=True, help="the Bambu Studio commit the files came from (recorded as provenance)")
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    a = ap.parse_args()
    table = build(a.hms_dir, a.commit)
    rendered = json.dumps(table, ensure_ascii=False, sort_keys=True, indent=1) + "\n"
    counts = (f"hms: {len(table['hms'])} codes ({sum(len(v) for v in table['hms_by_model'].values())} per-model overrides), "
              f"error: {len(table['error'])} codes ({sum(len(v) for v in table['error_by_model'].values())} per-model overrides)")
    if a.check:
        if OUT.read_text() != rendered:
            print(f"STALE: {OUT} does not match a fresh generation ({counts})")
            return 1
        print(f"current: {OUT} matches ({counts})")
        return 0
    OUT.write_text(rendered)
    print(f"wrote {OUT} ({counts}) from Bambu Studio {a.commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
