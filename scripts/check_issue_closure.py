#!/usr/bin/env python3
"""Issue closure guard: an issue closed as completed has no unticked checklist item; stdlib only.

Issues here carry their work as a checklist. Closing one with unticked items
hides unfinished work, which is how several 2.2.9 issues were closed too early.
Reads the issue body from a JSON file (never interpolated into a command), lists
every unticked item that is not marked as moved to another issue, and exits 1
when there is any. The workflow then reopens the issue and says which items.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_contribution import open_items  # noqa: E402  (one checklist reader for PRs and issues)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True, help="JSON {body} from the GitHub API")
    args = parser.parse_args(argv)
    data = json.loads(args.metadata.read_text(encoding="utf-8"))
    items = open_items(data.get("body", ""))
    for item in items:
        print(f"- [ ] {item}")
    print(f"Issue closure: {len(items)} unticked checklist item(s).")
    return 1 if items else 0


if __name__ == "__main__":
    raise SystemExit(main())
