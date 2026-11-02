"""Opt-in semantic layer behind the local fuzzy matcher (#468 stage 4).

Typos and synonyms are different failure classes. The local matcher
(``core.fuzzy``) handles character-level near-misses deterministically;
it cannot know that "bed won't stick" means "bed adhesion". Stage 4 lets
an operator point Print Handy at a semantic-search endpoint — the
reference shape is a small Cloudflare Worker over an embeddings model,
but anything answering the contract below works — and have *meaning*
matches offered when, and only when, the local matcher found nothing.

The posture is the #468 amendment's, and it is strict:

- **Opt-in and off by default.** ``PRINT_FUZZY_URL`` is a plain
  environment variable (no ``PRINT_HANDY__`` config form), read at call
  time. Unset, this module is never consulted and lookup is byte-identical
  to the local-only behaviour.
- **Stateless.** Only the query text and the caller's candidate list
  (public registry entries: id + display text) are sent. Nothing is
  stored, here or — per the contract the endpoint is asked to honour —
  there. Print numbers, spool IDs and per-install data (print history
  titles in particular) are never candidates.
- **Behind the local matcher.** Local exact/substring/fuzzy results are
  computed first and are never merged with or re-ranked by this layer;
  callers consult it only on a local no-match, and its hits are presented
  as suggestions, labelled as coming from the endpoint.
- **Silent degradation.** Any failure — unreachable, timeout, HTTP error,
  malformed body, or the endpoint answering ``degraded: true`` — returns
  ``[]`` and the local behaviour stands. The error is logged, the lookup
  is not.
- **No Authorization header.** The endpoint URL carries its secret in
  the path, the same posture as ``PRINT_VISION_URL``.

Contract (from the #468 spike, repo ``cf-fuzzy-search``): request
``POST {query, candidates: [{id, text}], threshold?, limit?}``; response
``{matches: [{id, text, score}], degraded: false}`` or
``{matches: [], degraded: true, reason}``. Scores are cosine similarities
in 0..1. ``DEFAULT_THRESHOLD`` is the spike's 0.55 — a placeholder chosen
before any live corpus existed, to be tuned against real lookups; it is
sent explicitly so the behaviour an install gets is the one documented
here, not whatever an endpoint defaults to.

Hardening pass (post-2.2.1 audit, toward 2.2.2) — the client no longer
takes the endpoint's word for anything it can check itself:

- The ``matches`` field must be a list; any other JSON shape (and any
  surprise while processing the entries) degrades to the local result
  instead of raising out of the lookup.
- Scores must be real numbers (a JSON ``NaN``/``Infinity`` literal, a
  string, a bool — all rejected), finite, and are clamped to 0..1.
  Before this, a NaN score crashed the caller's ``round(score * 100)``
  and the lookup answered "Lookup failed" instead of degrading.
- ``DEFAULT_THRESHOLD`` is enforced here, not just sent: matches below
  it are dropped, duplicate ids collapse to their best score, and the
  result is sorted by score (ties by id), never left in endpoint order.
- The response body is capped (``MAX_RESPONSE_BYTES``) and the whole
  exchange — connect, send, read — runs against one total deadline of
  ``TIMEOUT_S`` seconds, not a per-read socket timeout, so a slow-drip
  endpoint cannot hold the lookup thread.
- The URL must be http or https; anything else is a configuration
  error, logged once, and the layer stays off. Plain http to a
  non-local host earns a one-time warning: query text and candidate
  texts would travel in cleartext. Private/loopback endpoints over
  http are a supported pattern and are not warned about or blocked.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import math
import os
import time
import urllib.parse

logger = logging.getLogger(__name__)

TIMEOUT_S = 10
DEFAULT_THRESHOLD = 0.55
MAX_CANDIDATES = 1000
MAX_CANDIDATE_CHARS = 200
MAX_RESPONSE_BYTES = 256 * 1024
_READ_CHUNK = 64 * 1024

_warned_urls: set = set()


def _endpoint_url():
    """The configured PRINT_FUZZY_URL, validated, or None when unusable.

    A non-http(s) value is an operator configuration error: it is logged
    (once per distinct value) and the layer reports itself unconfigured
    rather than failing every lookup. A plain-http URL whose host is not
    local gets the same one-time treatment as a cleartext warning.
    """
    url = os.environ.get("PRINT_FUZZY_URL")
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        if url not in _warned_urls:
            _warned_urls.add(url)
            logger.error("PRINT_FUZZY_URL must be an http or https URL, got %r; "
                         "the semantic layer stays off", url)
        return None
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        if url not in _warned_urls:
            _warned_urls.add(url)
            logger.warning("PRINT_FUZZY_URL uses plain http to a non-local host (%s); "
                           "lookup queries and candidate texts travel in cleartext",
                           parts.hostname)
    return url


def _is_local_host(hostname):
    """True for loopback / private / .local names — the supported local-endpoint pattern."""
    host = (hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback or addr.is_link_local or addr.is_private
    except ValueError:
        return False


def configured() -> bool:
    """True when an operator has set a usable PRINT_FUZZY_URL — the layer's only switch."""
    return _endpoint_url() is not None


def _fetch(url, payload, deadline):
    """POST ``payload`` and return the raw response body, capped in size and time.

    Raises on any failure (the caller degrades). The socket timeout is
    reset to the remaining total budget before every read, so the whole
    exchange is bounded by ``deadline`` rather than by a per-read timeout.
    """
    parts = urllib.parse.urlsplit(url)
    port = parts.port
    timeout = max(0.1, deadline - time.monotonic())
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError(f"endpoint answered HTTP {response.status}")
        body = bytearray()
        # read1, not read: read(n) blocks until n bytes or EOF, which
        # would defeat the deadline check between iterations on a
        # slow-drip response; read1 returns after a single raw read.
        read_chunk = getattr(response, "read1", response.read)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"endpoint exceeded the {TIMEOUT_S} s total deadline")
            if conn.sock is not None:
                conn.sock.settimeout(remaining)
            chunk = read_chunk(_READ_CHUNK)
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError(f"endpoint response exceeds {MAX_RESPONSE_BYTES} bytes")
        return bytes(body)
    finally:
        conn.close()


def semantic_matches(query, candidates, *, limit=5):
    """Meaning-level matches for ``query`` from the configured endpoint, or [].

    ``candidates`` is a list of ``{"id", "text"}`` dicts. Returns a list of
    ``{"id", "text", "score"}`` dicts sorted by score descending (ties by
    id), capped at ``limit``: only entries with a finite numeric score at
    or above ``DEFAULT_THRESHOLD`` survive, scores are clamped to 0..1,
    and a duplicated id keeps its best score. Returns [] when
    PRINT_FUZZY_URL is unset/unusable or on any failure — callers treat
    [] as "the local answer stands".
    """
    url = _endpoint_url()
    query = str(query or "").strip()
    if not url or not query or not candidates:
        return []
    payload = json.dumps({
        "query": query[:MAX_CANDIDATE_CHARS],
        "candidates": [{"id": str(c["id"]), "text": str(c["text"])[:MAX_CANDIDATE_CHARS]}
                       for c in candidates[:MAX_CANDIDATES]],
        "threshold": DEFAULT_THRESHOLD,
        "limit": limit,
    }).encode()
    try:
        raw = _fetch(url, payload, time.monotonic() + TIMEOUT_S)
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("semantic lookup via PRINT_FUZZY_URL failed, keeping the local result: %s: %s",
                       type(exc).__name__, exc)
        return []
    if not isinstance(data, dict) or data.get("degraded"):
        if isinstance(data, dict) and data.get("degraded"):
            logger.warning("semantic lookup endpoint reports degraded: %s", data.get("reason") or "no reason given")
        return []
    raw_matches = data.get("matches") or []
    if not isinstance(raw_matches, list):
        # Valid JSON, invalid shape (e.g. "matches": 42): iterating it used
        # to raise TypeError here, outside the failure boundary, and the
        # caller answered "the registries could not be read" instead of
        # keeping the local result (P2-223).
        logger.warning("semantic lookup endpoint returned a non-list matches field (%s), "
                       "keeping the local result", type(raw_matches).__name__)
        return []
    try:
        best = {}
        for entry in raw_matches:
            if not isinstance(entry, dict) or entry.get("id") is None:
                continue
            raw_score = entry.get("score", 0)
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                continue  # a string/bool "score" is not a score; hostile or buggy payload
            score = float(raw_score)
            if not math.isfinite(score):
                continue  # NaN / ±Infinity must degrade, never reach the caller's arithmetic
            score = min(1.0, max(0.0, score))
            if score < DEFAULT_THRESHOLD:
                continue
            ident = str(entry["id"])
            if ident not in best or score > best[ident]["score"]:
                best[ident] = {"id": ident, "text": str(entry.get("text", "")), "score": score}
        matches = sorted(best.values(), key=lambda m: (-m["score"], m["id"]))
    except Exception as exc:
        logger.warning("semantic lookup response could not be processed, keeping the local "
                       "result: %s: %s", type(exc).__name__, exc)
        return []
    return matches[:limit]
