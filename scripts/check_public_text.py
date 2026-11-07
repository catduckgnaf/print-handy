#!/usr/bin/env python3
"""Public-text hygiene for issues, pull requests and their comments; stdlib only.

Everything written into a public issue or PR is public forever, including the
edit history. This check refuses text that points at the author's own
machines: home or NAS paths, private IP addresses, local hostnames, MAC
addresses, email addresses, chat IDs, SSH targets, tokens, and any operator
supplied machine names (``PUBLIC_TEXT_DENYLIST``, kept in a repository secret
so the names are not themselves published in this file).

Findings are reported as ``line N: <category>`` and NEVER echo the matched
value, so the check cannot republish what it caught. A line that carries the
marker ``public-text-ok`` is skipped (for a documented example). Documentation
IP ranges (RFC 5737) are never flagged.
"""
import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

MARKER = "public-text-ok"

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_HOST = re.compile(r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:local|lan|home|internal|localdomain|home\.arpa)\b")
_RULES = (
    ("home directory path", re.compile(r"(?i)(?:(?<![\w.])/home/[A-Za-z0-9._-]+|(?<![\w.])/Users/[A-Za-z0-9._-]+|\b[A-Z]:\\Users\\[^\\\s]+)")),
    ("root home path", re.compile(r"(?<![\w.])/root(?:/|\b)")),
    ("NAS or Unraid path", re.compile(r"(?<![\w.])/(?:mnt/(?:user0?|disk\d+|cache|disks|remotes)|boot/config|volume\d+)(?:/|\b)")),
    ("local hostname", _HOST),
    ("MAC address", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("SSH or copy target", re.compile(r"\b(?:ssh|scp|rsync|sftp)\b[^\n]*?\b[a-z_][a-z0-9_-]*@[A-Za-z0-9._-]+")),
    ("chat or account ID", re.compile(r"(?<![0-9A-Za-z])\d{17,20}(?![0-9A-Za-z])")),
    ("token", re.compile(r"(?:ghp_|gho_|ghs_|github_pat_|xox[bapr]-|sk-[A-Za-z0-9]{0,6}-?)[A-Za-z0-9_-]{16,}|[MNO][A-Za-z\d_-]{23,25}\.[\w-]{6}\.[\w-]{27,}")),
)


def _private_ip(match):
    try:
        ip = ipaddress.ip_address(match.group(0))
    except ValueError:
        return False
    documentation = any(ip in ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
    return (ip.is_private or ip.is_link_local) and not documentation and not ip.is_loopback and not ip.is_unspecified


def denylist(raw=None):
    raw = os.environ.get("PUBLIC_TEXT_DENYLIST", "") if raw is None else raw
    terms = [t.strip() for t in re.split(r"[,\n]", raw) if t.strip()]
    return [re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])") for t in terms]


def scan(text, terms=None):
    """[(line number, category)] for every line that points at a private place."""
    terms = denylist() if terms is None else terms
    findings = []
    for number, line in enumerate(str(text or "").splitlines(), 1):
        if MARKER in line:
            continue
        seen = set()
        for category, pattern in _RULES:
            if pattern.search(line):
                seen.add(category)
        if any(_private_ip(m) for m in _IPV4.finditer(line)):
            seen.add("private IP address")
        if any(t.search(line) for t in terms):
            seen.add("operator machine name")
        findings.extend((number, category) for category in sorted(seen))
    return findings


def check(data, terms=None):
    """Problem strings for one metadata record: title, body and extra texts (comments, commit messages)."""
    errors = []
    parts = [("title", data.get("title") or ""), ("body", data.get("body") or "")]
    parts += [(f"text {i}", t) for i, t in enumerate(data.get("extra") or [], 1)]
    for label, text in parts:
        for number, category in scan(text, terms):
            errors.append(f"{label} line {number}: {category}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True,
                        help="JSON with title, body and optional extra[] texts; never shell code")
    args = parser.parse_args()
    data = json.loads(args.metadata.read_text())
    errors = check(data)
    for error in errors:
        print(error)
    print(f"Public text policy: {len(errors)} problem(s). Values are never printed; "
          f"edit the text, or add '{MARKER}' to a line that is a deliberate example.")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
