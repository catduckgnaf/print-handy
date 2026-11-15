"""The firmware feed: the newest release of every firmware Print Handy follows.

Issue #1280 section I. One public JSON file, built by
scripts/build_firmware_feed.py from docs/firmware-feed.toml and published
by .github/workflows/firmware-feed.yml on the repository's
``firmware-feed`` branch. This module is the reading half: fetch it,
check its shape, and answer two questions for the rest of Print Handy:

- which firmware does this printer model run, and what is its newest
  release (:meth:`Feed.for_model`);
- what is the newest release of one firmware by id (:meth:`Feed.get`).

Why a feed and not a GitHub API call per firmware: an install has no
token, GitHub allows an unauthenticated address 60 API requests an hour,
and a roster with several firmware families would spend that on its own.
The feed is one plain file read, the same for every install, and it
carries what the API does not: which printer models a firmware belongs
to, the version Print Handy was audited against, and how the update is
installed.

The read sends nothing about the install: no printer, model, version or
identity, only a request for a public file. It fails soft everywhere: an
unreachable, malformed or unknown-schema feed is ``None``, never an
exception, and nothing that depends on the feed blocks a print.

The feed says what upstream has published. It never says a printer is
out of date (Print Handy reads that from the printer where it can) and
it never authorizes an update: applying one stays the confirmed
``/print-firmware update`` path, for the families that have one.
"""
from __future__ import annotations

import fnmatch
import http.client
import json
import time
import urllib.request
from dataclasses import dataclass

FEED_URL = ("https://raw.githubusercontent.com/catduckgnaf/print-handy/"
            "firmware-feed/firmware-feed.json")
SCHEMA = 1
TIMEOUT_S = 10.0
# One read an hour for the whole install, a failure included.
CACHE_S = 3600.0
MAX_BYTES = 512 * 1024


@dataclass(frozen=True)
class Release:
    version: str
    tag: str = ""
    published_at: str | None = None
    url: str = ""


@dataclass(frozen=True)
class Firmware:
    """One firmware line in the feed."""

    id: str
    name: str
    repo: str = ""
    channel: str = "stable"
    models: tuple = ()
    update: str = ""
    latest: Release | None = None
    latest_stable: Release | None = None
    audited: str | None = None
    stale: bool = False

    def covers(self, model_key) -> bool:
        key = str(model_key or "")
        return bool(key) and any(fnmatch.fnmatchcase(key, pattern) for pattern in self.models)


@dataclass(frozen=True)
class Feed:
    generated_at: str | None
    firmware: tuple

    def get(self, firmware_id) -> Firmware | None:
        wanted = str(firmware_id or "")
        return next((row for row in self.firmware if row.id == wanted), None)

    def for_model(self, model_key) -> list:
        """Every firmware line that runs on one printer model, in feed order."""
        return [row for row in self.firmware if row.covers(model_key)]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _release(raw) -> Release | None:
    if not isinstance(raw, dict) or not _text(raw.get("version")):
        return None
    url = _text(raw.get("url"))
    return Release(version=_text(raw.get("version")), tag=_text(raw.get("tag")),
                   published_at=_text(raw.get("published_at")) or None,
                   # Only a GitHub link is ever shown; anything else is dropped.
                   url=url if url.startswith("https://github.com/") else "")


def parse(payload) -> Feed | None:
    """A Feed from the decoded JSON, or None when it is not one this build reads.

    Tolerant of extra keys and of single malformed entries (they are
    skipped); strict about the schema number, because a newer schema may
    change what a field means.
    """
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    rows = payload.get("firmware")
    if not isinstance(rows, list):
        return None
    out, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        ident, name = _text(raw.get("id")), _text(raw.get("name"))
        if not ident or not name or ident in seen:
            continue
        seen.add(ident)
        models = raw.get("models")
        out.append(Firmware(
            id=ident, name=name, repo=_text(raw.get("repo")),
            channel=_text(raw.get("channel")) or "stable",
            models=tuple(m for m in models if isinstance(m, str)) if isinstance(models, list) else (),
            update=_text(raw.get("update")),
            latest=_release(raw.get("latest")),
            latest_stable=_release(raw.get("latest_stable")),
            audited=_text(raw.get("audited")) or None,
            stale=bool(raw.get("stale"))))
    return Feed(generated_at=_text(payload.get("generated_at")) or None, firmware=tuple(out))


def fetch(opener=None, timeout=TIMEOUT_S, url=FEED_URL) -> Feed | None:
    """Read and parse the published feed. None on any failure; never raises."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "print-handy"})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            return None
        return parse(json.loads(body or b"null"))
    except (OSError, ValueError, http.client.HTTPException):
        # Unreachable, timed out, cut short or not JSON: the feed is
        # never required, so every reader treats None as "not known".
        return None


class FeedCache:
    """One feed read per CACHE_S for the whole process; a failure is cached too.

    After a failed read the last good feed keeps answering, so one bad
    hour does not blank the Firmware page.
    """

    def __init__(self, fetcher=fetch, clock=time.monotonic):
        self._fetch, self._clock = fetcher, clock
        self._at, self._value = None, None

    def get(self) -> Feed | None:
        now = self._clock()
        if self._at is None or now - self._at >= CACHE_S:
            fresh = self._fetch()
            self._at = now
            if fresh is not None:
                self._value = fresh
        return self._value


_shared = FeedCache()


def current() -> Feed | None:
    """The process-wide cached feed (None until one read has succeeded)."""
    return _shared.get()


__all__ = ["CACHE_S", "FEED_URL", "Feed", "FeedCache", "Firmware", "Release",
           "SCHEMA", "current", "fetch", "parse"]
