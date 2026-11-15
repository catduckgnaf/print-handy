#!/usr/bin/env python3
"""Build firmware-feed.json: the newest release of every firmware Print Handy follows.

Issue #1280 section I. The feed is one small JSON file that
.github/workflows/firmware-feed.yml publishes on the ``firmware-feed``
branch. Every Print Handy install reads that file (no token, no GitHub
API budget on the install) to tell an owner about a new firmware release
and to fill the Web UI Firmware page.

What goes in: docs/firmware-feed.toml lists the firmware (id, name,
repository, releases or tags, channel, the printer models it runs on and
one sentence on how the update is installed). This script asks GitHub
for each one and records the newest release on the entry's channel, the
newest final release, and, where docs/firmware-baselines.toml or
docs/ZMOD.md records one under the same name, the version Print Handy's
code was audited against.

What never goes in: nothing about any printer or install. The feed is
public and identical for everyone.

A source that cannot be read keeps its entry from the previous feed
(``--previous``), marked ``stale``, so one unreachable repository never
empties the feed. The script exits 1 only when the sources file itself
is wrong, or when nothing at all could be read and there is no previous
feed to keep.

Stdlib only; it runs from a bare checkout without the package installed.

    python3 scripts/build_firmware_feed.py --output firmware-feed.json \
        [--previous old-feed.json] [--root .]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_firmware_releases as watch  # noqa: E402  (sibling script, stdlib only)

SOURCES_FILE = "docs/firmware-feed.toml"
SCHEMA = 1
CHANNELS = ("stable", "prerelease")
_FIELDS = {"id", "name", "repo", "feed", "channel", "models", "line", "update"}
_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_MODEL_RE = re.compile(r"[a-z0-9_]+\.(?:[a-z0-9_]+|\*)")
# Listing depth: enough to find the newest release on every channel and
# printer line without walking a repository's whole history.
PAGE_SIZE = 100


class FeedError(Exception):
    """The sources file is wrong, or nothing could be built."""


# ---------------------------------------------------------------------------
# Sources


def parse_sources(text: str) -> list[dict]:
    """Parse docs/firmware-feed.toml. Fails closed on anything malformed."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise FeedError(f"{SOURCES_FILE}: not valid TOML: {exc}") from exc
    records = data.get("firmware")
    if not isinstance(records, list) or not records:
        raise FeedError(f"{SOURCES_FILE}: no [[firmware]] entries")
    sources, seen_ids, seen_names = [], set(), set()
    for index, record in enumerate(records, start=1):
        where = f"{SOURCES_FILE} entry {index}"
        if not isinstance(record, dict):
            raise FeedError(f"{where}: not a table")
        unknown = set(record) - _FIELDS
        if unknown:
            raise FeedError(f"{where}: unknown field(s) {sorted(unknown)}")
        for key in ("id", "name", "repo", "feed", "channel", "update"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise FeedError(f"{where}: missing or empty '{key}'")
        where = f"{SOURCES_FILE} entry '{record['id']}'"
        if not _ID_RE.fullmatch(record["id"]):
            raise FeedError(f"{where}: id must be lowercase letters, digits and hyphens")
        if record["id"] in seen_ids or record["name"] in seen_names:
            raise FeedError(f"{where}: duplicate id or name")
        seen_ids.add(record["id"])
        seen_names.add(record["name"])
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", record["repo"]):
            raise FeedError(f"{where}: repo must be owner/name")
        if record["feed"] not in watch.FEEDS:
            raise FeedError(f"{where}: feed must be one of {watch.FEEDS}")
        if record["feed"] == "releases" and record["repo"] in watch.TAG_ONLY_REPOS:
            raise FeedError(f'{where}: {record["repo"]} publishes no usable releases; '
                            'use feed = "tags"')
        if record["channel"] not in CHANNELS:
            raise FeedError(f"{where}: channel must be one of {CHANNELS}")
        models = record.get("models", [])
        if not isinstance(models, list) or not all(
                isinstance(m, str) and _MODEL_RE.fullmatch(m) for m in models):
            raise FeedError(f"{where}: 'models' must be registry keys or vendor globs "
                            '("flashforge.ad5x", "flashforge.*")')
        line = record.get("line", [])
        if not isinstance(line, list) or not all(isinstance(n, str) and n.strip() for n in line):
            raise FeedError(f"{where}: 'line' must be a list of printer names")
        if line and record["feed"] != "releases":
            raise FeedError(f'{where}: a printer line selects releases by title; '
                            'it needs feed = "releases"')
        sources.append({
            "id": record["id"], "name": record["name"].strip(), "repo": record["repo"],
            "feed": record["feed"], "channel": record["channel"],
            "models": list(models), "line": [n.strip() for n in line],
            "update": " ".join(record["update"].split()),
        })
    return sources


def audited_versions(root: Path) -> dict[str, str]:
    """Audited baselines by name, from docs/ZMOD.md and docs/firmware-baselines.toml."""
    try:
        return {entry.name: entry.baseline for entry in watch.read_baselines(root)}
    except watch.WatchError as exc:
        raise FeedError(f"cannot read the audited baselines: {exc}") from exc


# ---------------------------------------------------------------------------
# Versions

_STAGES = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3}
_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)+)"                              # 1.4.2
    r"(?:[-._]?(dev|alpha|beta|rc|a|b)[-._]?(\d*))?"  # -beta-4, -rc.1, b3
    r"(?:-(\d+))?$",                                # -17 (a later build)
    re.IGNORECASE)


def parse_version(tag: str):
    """``(display, key)`` for a tag, or None when it carries no version.

    The version is read from the end of the tag, so ``Plus4_v1.7.1`` is
    ``1.7.1``. Unlike the weekly watch's parser this one reads a spelled
    stage with its own serial (``1.4.2-beta-4``, ``v1.1.0-rc.1``), and
    orders by that serial: beta-4 is below beta-10.
    """
    match = _VERSION_RE.search(str(tag or "").strip())
    if not match:
        return None
    numbers = [int(part) for part in match.group(1).split(".")]
    numbers += [0] * (4 - len(numbers))
    stage, serial, build = match.group(2), match.group(3), match.group(4)
    display = match.group(0)
    if stage:
        key = (*numbers[:4], 0, _STAGES[stage.lower()], int(serial or 0), 0)
    else:
        key = (*numbers[:4], 1, 0, 0, int(build or 0))
    return display, key


# ---------------------------------------------------------------------------
# Reading GitHub


def _iso(text):
    if not text:
        return None
    try:
        return watch.parse_datetime(text).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def list_candidates(client, source: dict) -> list[dict]:
    """Every readable release (or tag) of one source, newest version first."""
    repo = source["repo"]
    if source["feed"] == "tags":
        raw = client.request("GET", f"/repos/{repo}/tags?per_page={PAGE_SIZE}")
        items = [{"tag": item.get("name"), "prerelease": None, "published_at": None,
                  "url": f"https://github.com/{repo}/tree/{item.get('name')}", "title": ""}
                 for item in (raw or []) if isinstance(item, dict)]
    else:
        raw = client.request("GET", f"/repos/{repo}/releases?per_page={PAGE_SIZE}")
        items = [{"tag": item.get("tag_name"), "prerelease": bool(item.get("prerelease")),
                  "published_at": _iso(item.get("published_at")),
                  "url": item.get("html_url") or f"https://github.com/{repo}/releases",
                  "title": item.get("name") or ""}
                 for item in (raw or []) if isinstance(item, dict) and not item.get("draft")]
    if raw is None:
        raise watch.WatchError(f"{repo}: not found")
    line = set(source["line"])
    out = []
    for item in items:
        parsed = parse_version(item["tag"])
        if parsed is None:
            continue
        if line and not (watch.release_printers(item["title"]) & line):
            continue
        display, key = parsed
        staged = key[4] == 0
        out.append({
            "version": display, "tag": item["tag"], "key": key,
            # Upstream's flag, or a spelled stage in the version: some
            # projects publish a beta as a full release.
            "prerelease": bool(item["prerelease"]) or staged,
            "published_at": item["published_at"], "url": item["url"],
        })
    out.sort(key=lambda row: row["key"], reverse=True)
    return out


def _public(row):
    return None if row is None else {k: row[k] for k in ("version", "tag", "published_at", "url")}


def build_entry(source: dict, candidates: list[dict], audited: str | None) -> dict:
    stable = next((row for row in candidates if not row["prerelease"]), None)
    newest = candidates[0] if candidates else None
    latest = newest if source["channel"] == "prerelease" else stable
    return {
        "id": source["id"], "name": source["name"], "repo": source["repo"],
        "feed": source["feed"], "channel": source["channel"],
        "models": source["models"], "update": source["update"],
        "latest": _public(latest), "latest_stable": _public(stable),
        "audited": audited, "stale": False,
    }


def _content(feed: dict) -> dict:
    """The feed without its timestamp, for "did anything change"."""
    return {key: value for key, value in feed.items() if key != "generated_at"}


def build_feed(sources: list[dict], audited: dict[str, str], client, *,
               previous: dict | None = None, now: datetime | None = None,
               log=print) -> dict:
    """The feed document. A source that cannot be read keeps its previous entry."""
    kept = {}
    if isinstance(previous, dict) and isinstance(previous.get("firmware"), list):
        kept = {row.get("id"): row for row in previous["firmware"] if isinstance(row, dict)}
    entries, read = [], 0
    for source in sources:
        try:
            candidates = list_candidates(client, source)
            if not candidates:
                raise watch.WatchError(f"{source['repo']}: no release carries a version")
            entries.append(build_entry(source, candidates, audited.get(source["name"])))
            read += 1
        except watch.WatchError as exc:
            log(f"warning: {source['id']}: {exc}")
            old = kept.get(source["id"])
            if old is not None and isinstance(old.get("latest"), dict):
                # Keep what was last read, under today's metadata.
                entries.append({**build_entry(source, [], audited.get(source["name"])),
                                "latest": old.get("latest"),
                                "latest_stable": old.get("latest_stable"), "stale": True})
            else:
                entries.append({**build_entry(source, [], audited.get(source["name"])),
                                "stale": True})
    if not read and not any(row["latest"] for row in entries):
        raise FeedError("no firmware source could be read and there is no previous feed to keep")
    feed = {"schema": SCHEMA, "source": "https://github.com/catduckgnaf/print-handy",
            "firmware": entries}
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The timestamp moves only when the content does, so an unchanged
    # feed is byte-identical and the workflow has nothing to commit.
    if isinstance(previous, dict) and _content(previous) == feed:
        stamp = previous.get("generated_at") or stamp
    return {"schema": SCHEMA, "generated_at": stamp, "source": feed["source"],
            "firmware": entries}


def render(feed: dict) -> str:
    return json.dumps(feed, indent=1, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".", help="repository checkout (default: .)")
    parser.add_argument("--output", required=True, help="where to write firmware-feed.json")
    parser.add_argument("--previous", help="the feed published last time, if any")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        try:
            text = (root / SOURCES_FILE).read_text(encoding="utf-8")
        except OSError as exc:
            raise FeedError(f"cannot read {SOURCES_FILE}: {exc}") from exc
        sources = parse_sources(text)
        audited = audited_versions(root)
        previous = None
        if args.previous and Path(args.previous).is_file():
            try:
                previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"warning: ignoring an unreadable previous feed: {exc}")
        client = watch.GitHubClient(os.environ.get("GITHUB_TOKEN") or None)
        feed = build_feed(sources, audited, client, previous=previous)
    except FeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    Path(args.output).write_text(render(feed), encoding="utf-8", newline="\n")
    stale = [row["id"] for row in feed["firmware"] if row["stale"]]
    print(f"wrote {args.output}: {len(feed['firmware'])} firmware, "
          f"{len(stale)} stale{' (' + ', '.join(stale) + ')' if stale else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
