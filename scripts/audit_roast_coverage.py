"""Audit title-roast coverage against extracted corpus filenames and local print history.

Run locally; no upload, network calls, or modification of print records.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from print_handy.core import print_stats, roast


def coverage(titles, counts=None):
    counts = counts or {}
    corpus = roast.load_corpus()
    results = []
    for title in sorted(set(titles), key=lambda t: (-counts.get(t, 0), t.casefold())):
        ctx = roast.build_context(title)
        success = [(k, v) for k, v in roast.candidate_pools(ctx, corpus=corpus)
                   if k.startswith("title:")]
        failure = [(k, v) for k, v in roast.candidate_pools(ctx, "failed", corpus)
                   if k.startswith("failure-title:")]
        results.append({"title": title, "prints": counts.get(title, 0),
                        "success_topics": [k for k, _ in success],
                        "success_jokes": len({eid for _, rows in success for eid, _ in rows}),
                        "failure_topics": [k for k, _ in failure],
                        "failure_jokes": len({eid for _, rows in failure for eid, _ in rows})})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-vault", type=Path, help="Local records vault containing Prints/")
    parser.add_argument("--corpus-root", type=Path, help="Extracted regression archive")
    args = parser.parse_args()
    if not args.records_vault and not args.corpus_root:
        parser.error("provide --records-vault and/or --corpus-root")
    titles = set()
    counts = Counter()
    skipped = []
    if args.records_vault:
        records, skipped = print_stats.load_records(args.records_vault)
        for record in records:
            title = print_stats.display_title(record.get("title"), record.get("gcode_file"))
            if title:
                counts[title] += 1
                titles.add(title)
    if args.corpus_root:
        for path in args.corpus_root.rglob("*"):
            if path.is_file() and path.suffix.casefold() in (".stl", ".3mf", ".obj", ".step", ".stp"):
                # Archive layouts use models/<case-id>/model.stl, so a generic
                # filename alone is insufficient to identify the print.
                titles.add(f"{path.parent.name} {path.stem}")
    rows = coverage(titles, counts)
    print(json.dumps({"models": len(rows), "skipped_records": len(skipped),
                      "missing_success": sum(not row["success_jokes"] for row in rows),
                      "missing_failure": sum(not row["failure_jokes"] for row in rows),
                      "rows": rows}, indent=2))


if __name__ == "__main__":
    main()
