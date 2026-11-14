#!/usr/bin/env python3
"""Bug report completeness: sanitized logs and evidence must be attached; stdlib only.

Reads the markdown GitHub renders from the bug_report issue form (``### Label``
sections, ``_No response_`` for an empty field). Only bug reports are checked.
Problems are reported by name and never echo report content.
"""
import argparse
import json
import re
import sys
from pathlib import Path

EMPTY = {"", "_no response_", "no response", "n/a", "none", "todo", "tbd"}
ATTACHMENT = re.compile(r"https://github\.com/(?:user-attachments|[^/\s]+/[^/\s]+/files)/\S+")
# One pasted Doctor row: the doctor prints "ok" / "FAIL" / "WARN", two or
# more spaces, then a label ending in a colon ("ok    settings: ...").
DOCTOR_ROW = re.compile(r"(?m)^(ok|FAIL|WARN)\s{2,}\S[^:]*:")


def sections(body):
    parts = re.split(r"^### +(.+?)\s*$", body or "", flags=re.M)
    return {parts[i].strip().lower(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def filled(text, minimum=15):
    value = re.sub(r"<!--.*?-->", "", text or "", flags=re.S).strip()
    return value.casefold() not in EMPTY and len(value) >= minimum


def is_bug(body, labels):
    return "bug" in {str(label).lower() for label in labels or ()} or "### what happened" in (body or "").lower()


def check(body, labels=()):
    if not is_bug(body, labels):
        return []
    found = sections(body)
    errors = []
    for name, minimum in (("what happened", 40), ("evidence", 40), ("print handy version", 3)):
        if not filled(found.get(name, ""), minimum):
            errors.append(f"Fill in '{name}' with real content.")
    bundle = found.get("diagnostics bundle", "")
    report = found.get("sanitization report", "")
    # A bundle counts as present as an attachment, as pasted content that
    # names the Doctor, or — the #442 shape (#1404) — as pasted Doctor
    # output itself: at least two Doctor rows in a filled section, with
    # no attachment and no "doctor" word anywhere in it.
    has_bundle = (bool(ATTACHMENT.search(bundle))
                  or ("doctor" in bundle.lower() and filled(bundle, 80))
                  or (len(DOCTOR_ROW.findall(bundle)) >= 2 and filled(bundle, 80)))
    has_report = report.lower().lstrip().startswith("sanitization report")
    if not (has_bundle or has_report):
        errors.append("Attach the sanitized diagnostics bundle (`/print-handy bundle`, upload the file or paste its "
                      "Doctor section) or paste the sanitization report from a debug export.")
    if not filled(found.get("printer and firmware", ""), 3) and "not applicable" not in (body or "").lower():
        errors.append("Name the printer and firmware, or write 'Not applicable' for an offline problem.")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True, help="JSON with body and labels; never shell code")
    data = json.loads(parser.parse_args().metadata.read_text())
    errors = check(data.get("body", ""), data.get("labels", []))
    for error in errors:
        print(error)
    print(f"Issue policy: {len(errors)} problem(s).")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
