#!/usr/bin/env python3
"""Regenerate (or check) the committed OrcaSlicer profile database (#1816).

    python3 scripts/build_orca_profile_db.py                  # fetch the pinned Orca, regenerate the data
    python3 scripts/build_orca_profile_db.py --orca-root DIR  # use an existing OrcaSlicer checkout instead
    python3 scripts/build_orca_profile_db.py --check          # fail when the data does not match the pin
    python3 scripts/build_orca_profile_db.py --diff-to-markdown OUT.md   # also write the bump report

The pin is the ``version=`` / ``revision=`` pair in docker/orca-prebuilt.sh, the one OrcaSlicer build Print
Handy ships. Only ``resources/profiles`` is fetched (a blob-less sparse checkout), the same shape as
scripts/bump_upstream.py's fetch. The output is print_handy/data/orca_profiles/ (see
print_handy/core/orca_profile_db.py). Regenerating at the same pin is byte-identical.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from print_handy.core import orca_profile_db as db  # noqa: E402

ORCA_URL = "https://github.com/OrcaSlicer/OrcaSlicer.git"
PIN_FILE = ROOT / "docker" / "orca-prebuilt.sh"


def pinned_orca(pin_file: Path = PIN_FILE) -> tuple[str, str]:
    """(version, revision) of the stable OrcaSlicer pin."""
    text = pin_file.read_text(encoding="utf-8")
    block = re.search(r"^\s*stable\)\s*$(.*?)^\s*;;\s*$", text, re.M | re.S)
    if not block:
        raise SystemExit(f"{pin_file}: no stable) block")
    version = re.search(r"^\s*version=(\S+)\s*$", block.group(1), re.M)
    revision = re.search(r"^\s*revision=([0-9a-f]{40})\s*$", block.group(1), re.M)
    if not version or not revision:
        raise SystemExit(f"{pin_file}: the stable block has no version= / revision= pin")
    return version.group(1), revision.group(1)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def fetch_profiles(revision: str, dest: Path) -> Path:
    """OrcaSlicer's resources/profiles at exactly ``revision``, sparsely fetched into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "."], dest)
    _git(["remote", "add", "origin", ORCA_URL], dest)
    _git(["sparse-checkout", "init", "--cone"], dest)
    _git(["sparse-checkout", "set", "resources/profiles"], dest)
    _git(["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", revision], dest)
    _git(["checkout", "-q", "--detach", "FETCH_HEAD"], dest)
    head = _git(["rev-parse", "HEAD"], dest)
    if head != revision:
        raise SystemExit(f"fetched OrcaSlicer HEAD {head} is not the pin {revision}")
    return dest / "resources" / "profiles"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orca-root", type=Path, help="an OrcaSlicer checkout at the pinned revision")
    parser.add_argument("--version", help="Orca version to record (default: the pin)")
    parser.add_argument("--revision", help="Orca commit to fetch and record (default: the pin)")
    parser.add_argument("--out", type=Path, default=db.DATA_DIR, help="output folder")
    parser.add_argument("--check", action="store_true",
                        help="regenerate into a temporary folder and fail if it differs from --out")
    parser.add_argument("--diff-to-markdown", type=Path, help="write the change report against the current data")
    args = parser.parse_args(argv)
    pin_version, pin_revision = pinned_orca()
    version, revision = args.version or pin_version, args.revision or pin_revision
    with tempfile.TemporaryDirectory(prefix="orca-profiles-") as tmp:
        if args.orca_root:
            profiles = args.orca_root / "resources" / "profiles"
            if (args.orca_root / ".git").exists():
                head = _git(["rev-parse", "HEAD"], args.orca_root)
                if head != revision:
                    raise SystemExit(f"{args.orca_root} is at {head}, not {revision}")
        else:
            profiles = fetch_profiles(revision, Path(tmp) / "orca")
        fresh = db.build(profiles, version, revision)
        if args.check:
            out = Path(tmp) / "check"
            db.write(fresh, out)
            mismatched = [p.name for p in sorted(out.iterdir())
                          if not (args.out / p.name).is_file() or (args.out / p.name).read_bytes() != p.read_bytes()]
            extra = [p.name for p in sorted(args.out.glob("*.jsonl")) if not (out / p.name).exists()]
            if mismatched or extra:
                print("Orca profile database is stale: " + ", ".join(mismatched + extra), file=sys.stderr)
                print("Regenerate: python3 scripts/build_orca_profile_db.py", file=sys.stderr)
                return 1
            print(f"Orca profile database matches OrcaSlicer {version} ({revision[:12]}).")
            return 0
        if args.diff_to_markdown:
            try:
                previous = db.load(args.out)
            except db.ProfileDatabaseError:
                previous = {"meta": {"orca_version": None, "orca_revision": None}, "printers": {}, "filaments": {}}
            args.diff_to_markdown.write_text(db.diff_markdown(db.diff(previous, fresh)), encoding="utf-8")
        db.write(fresh, args.out)
        db.index.cache_clear()
        db.vendor_filaments.cache_clear()
    meta = fresh["meta"]
    print(f"Wrote {args.out}: OrcaSlicer {version} ({revision[:12]}), {meta['printer_presets']} printer presets, "
          f"{meta['filament_presets']} filament presets, {len(meta['vendors'])} vendors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
