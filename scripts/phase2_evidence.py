#!/usr/bin/env python3
"""Record and verify offline evidence artifacts for evidence-bound phase-two issues.

This does NOT perform a live hardware check, infer a result from a filename, or
upload material to GitHub. An observed entry means an operator supplied a file;
the report's hash verifies integrity, not the truth of the observation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "print-handy-phase2-evidence-v1"
CASES = {
    1067: (
        "nvenc-encode", "intel-encode", "amd-encode", "camera-detector",
        "ort-accelerated", "classifier-real-frames", "cloudflare-live",
    ),
    1068: (
        "renumber-plan", "renumber-applied", "rollback-amd64",
        "rollback-arm64", "assembly-production-caller",
    ),
    1069: (
        "scad-cgal-manifold", "cad-split", "preview-dri", "warning-deltas",
        "orca-headless-gpu", "packaging-amd64-arm64",
    ),
    1070: ("paused-command-layer", "recovery-path-decision", "runout-diagnostics"),
    1072: (
        "release-candidate", "publish-image", "docs-site", "release-digest",
        "live-check-disposition",
    ),
}
STATUSES = ("observed", "blocked", "deferred")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def make_record(*, issue: int, case: str, status: str, context: str,
                note: str, artifact: Path | None = None) -> dict:
    if issue not in CASES or case not in CASES[issue]:
        raise ValueError("unknown issue/case; use 'cases' for accepted evidence types")
    if status not in STATUSES:
        raise ValueError("status must be observed, blocked or deferred")
    if len(context.strip()) < 10:
        raise ValueError("context must identify the actual setup or release without private hostnames")
    if status != "observed" and len(note.strip()) < 10:
        raise ValueError("blocked/deferred entries need an explanatory note")
    if status == "observed" and artifact is None:
        raise ValueError("observed claims require an actual evidence file")
    if artifact is not None and (not artifact.is_file() or artifact.is_symlink()):
        raise ValueError("evidence must be an existing regular, non-symlink file")
    attachment = None
    if artifact is not None:
        attachment = {"name": artifact.name, "sha256": sha256(artifact), "bytes": artifact.stat().st_size}
    return {
        "schema": SCHEMA,
        "issue": issue,
        "case": case,
        "status": status,
        "context": context.strip(),
        "note": note.strip(),
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "attachment": attachment,
        "limitation": "Evidence file supplied by an operator; this is not independent hardware verification.",
    }


def verify_record(record: dict, artifact_dir: Path) -> list[str]:
    errors = []
    if record.get("schema") != SCHEMA:
        errors.append("unsupported evidence schema")
    issue, case, status = record.get("issue"), record.get("case"), record.get("status")
    if type(issue) is not int or case not in CASES.get(issue, ()):
        errors.append("unknown issue/case")
    if status not in STATUSES:
        errors.append("invalid status")
    if not isinstance(record.get("context"), str) or len(record["context"].strip()) < 10:
        errors.append("missing environment/release context")
    if status in ("blocked", "deferred") and (
        not isinstance(record.get("note"), str) or len(record["note"].strip()) < 10
    ):
        errors.append("blocked/deferred status requires a reason")
    attachment = record.get("attachment")
    if status == "observed" and not isinstance(attachment, dict):
        errors.append("observed entry has no evidence file")
    if isinstance(attachment, dict):
        name = attachment.get("name")
        expected = attachment.get("sha256")
        if not isinstance(name, str) or not name or name in (".", "..") or (
            Path(name).name != name
        ) or "/" in name or "\\" in name:
            errors.append("evidence filename must be a safe basename")
        elif not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            errors.append("invalid evidence sha256")
        else:
            path = artifact_dir / name
            if not path.is_file() or path.is_symlink():
                errors.append(f"missing or unsafe evidence: {name}")
            elif sha256(path) != expected or path.stat().st_size != attachment.get("bytes"):
                errors.append(f"evidence changed after capture: {name}")
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="command", required=True)
    cmds.add_parser("cases", help="List evidence kinds; #1071 is already merged")
    capture = cmds.add_parser("record", help="Record evidence locally; no uploads")
    capture.add_argument("--issue", type=int, required=True)
    capture.add_argument("--case", required=True)
    capture.add_argument("--status", choices=STATUSES, required=True)
    capture.add_argument("--context", required=True)
    capture.add_argument("--note", default="")
    capture.add_argument("--artifact", type=Path)
    capture.add_argument("--out", type=Path, required=True)
    verify = cmds.add_parser("verify", help="Verify hashes and case identifiers")
    verify.add_argument("--record", type=Path, required=True)
    verify.add_argument("--artifact-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "cases":
        for issue, cases in CASES.items():
            print(f"#{issue}: " + ", ".join(cases))
        print("#1071: already merged; no new evidence required")
        return 0
    try:
        if args.command == "record":
            item = make_record(issue=args.issue, case=args.case,
                               status=args.status, context=args.context,
                               note=args.note, artifact=args.artifact)
            if args.out.exists():
                raise ValueError("refusing to overwrite an existing evidence record")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")
            print(f"Recorded #{item['issue']} {item['case']} as {item['status']} locally.")
            print("Review and sanitize the attachment before posting it anywhere.")
            return 0
        record = json.loads(args.record.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("evidence record must be a JSON object")
        errors = verify_record(record, args.artifact_dir)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"Evidence error: {exc}\n")
    for issue in errors:
        print(f"FAIL: {issue}")
    if errors:
        return 1
    print("Evidence format and attachment SHA-256 verified; no live hardware claims inferred.")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
