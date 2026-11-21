#!/usr/bin/env python3
"""Issue closure guard: an issue closed as completed has no unticked checklist item; stdlib only.

Issues here carry their work as a checklist. Closing one with unticked items
hides unfinished work, which is how several 2.2.9 issues were closed too early.
Reads the issue body from a JSON file (never interpolated into a command), lists
every unticked item that is not marked as moved to another issue, and exits 1
when there is any. The workflow then reopens the issue and says which items.
Any failure to read the issue exits 2, so a crash fails the job instead of
reopening the issue (Python's own uncaught-exception exit code is also 1).

Run it before closing, too: ``--issue N`` reads the live issue through ``gh``
and gives the same answer the guard will. The 2.3.6 release checklist was
closed with boxes unticked, the guard reopened it, and the publication
dispatched in the meantime failed its checklist check and had to be re-run.

    check_issue_closure.py --metadata issue.json     # the workflow
    check_issue_closure.py --issue 1729 [--repo o/r] # before closing by hand
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_contribution import open_items  # noqa: E402  (one checklist reader for PRs and issues)


def issue_body(number, repo=None):
    """The live issue body, read through ``gh`` (argument list, never a shell string)."""
    cmd = ["gh", "issue", "view", str(int(number)), "--json", "body"]
    if repo:
        cmd += ["--repo", repo]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--metadata", type=Path, help="JSON {body} from the GitHub API")
    source.add_argument("--issue", type=int, help="issue number, read live through gh")
    parser.add_argument("--repo", default=None, help="OWNER/REPO for --issue (default: the checkout's)")
    args = parser.parse_args(argv)
    try:
        if args.issue is not None:
            data = issue_body(args.issue, args.repo)
        else:
            data = json.loads(args.metadata.read_text(encoding="utf-8"))
        items = open_items(data.get("body") or "")
    except Exception as exc:  # noqa: BLE001 - a broken run must never read as "items open"
        print(f"Issue closure: could not read the issue ({type(exc).__name__}: {exc}).", file=sys.stderr)
        return 2
    for item in items:
        print(f"- [ ] {item}")
    print(f"Issue closure: {len(items)} unticked checklist item(s).")
    return 1 if items else 0


if __name__ == "__main__":
    raise SystemExit(main())
