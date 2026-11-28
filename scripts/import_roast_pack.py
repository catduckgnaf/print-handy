"""Build an offline roast pack from human-reviewed, rights-cleared JSON sources.

Run from the repository root: python -m scripts.import_roast_pack --output PACK SOURCE...
No network requests; source text and rights decisions belong to the operator.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from print_handy.core import roast


def import_pack(sources: list[Path], output: Path, minimum: int = 1000) -> dict:
    """Validate approved entries against shipped corpus and publish only if enough survive."""
    if minimum < 1 or minimum > roast.MAX_CUSTOM_ENTRIES:
        raise ValueError("minimum must be between 1 and the custom bank limit")
    rows = []
    rejected = Counter()
    for source in sources:
        payload = json.loads(source.read_text(encoding="utf-8"))
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError(f"{source}: expected an entries array")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("reviewed") is not True:
                rejected["not reviewed"] += 1
                continue
            if not all(isinstance(entry.get(key), str) and entry[key].strip()
                       for key in ("text", "source", "license")):
                rejected["missing text or provenance"] += 1
                continue
            text = entry["text"].strip()
            # Imported jokes are literal; template slots belong to house roasts.
            if "{" in text or "}" in text:
                rejected["template syntax"] += 1
                continue
            eid = roast.entry_id("external", text)
            row = {**entry, "id": eid, "family": entry.get("family") or eid, "text": text}
            rows.append(row)
    # Validate against every shipped bank using the same runtime gates.
    shipped = roast.load_corpus()
    shipped_texts = [text for rows_ in shipped["banks"].values() for _, text in rows_]
    for section in ("title_reactions", "color_roasts", "reprint_failed",
                    "history_callbacks", "material_roasts", "milestone"):
        shipped_texts.extend(text for rows_ in shipped.get(section, {}).values()
                             for _, text in rows_)
    with TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "candidate.json"
        candidate.write_text(json.dumps({"entries": rows}, ensure_ascii=False), encoding="utf-8")
        accepted, problems = roast.load_custom_bank(
            candidate, shipped_ids=set(shipped["families"]), shipped_texts=shipped_texts,
            blocklist=shipped["blocklist"])
    # Use first accepted row for each ID; a repeated input ID never overwrites
    # the original attribution.
    by_id = {}
    for row in rows:
        by_id.setdefault(row["id"], row)
    accepted_rows = [by_id[eid] for eid, _, _ in accepted]
    report = {"accepted": len(accepted_rows), "preflight_rejected": dict(rejected),
              "validation_problems": problems,
              "categories": dict(Counter(str(row.get("category", "uncategorized"))
                                         for row in accepted_rows))}
    if len(accepted_rows) < minimum:
        raise ValueError(f"Only {len(accepted_rows)} approved unique jokes survived; "
                         f"need {minimum}. Report: {json.dumps(report)}")
    result = {"_about": "Human-reviewed external joke pack",
              "_license": "Per-entry source and license; verify underlying rights",
              "entries": accepted_rows}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(import_pack(args.sources, args.output, args.minimum), indent=2))


if __name__ == "__main__":
    main()
