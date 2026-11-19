"""Bounded disk cache for pure, source-bound intake inspections."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
ALGORITHM_VERSION = "inspection-v1"
_MAX_ENTRY_BYTES = 16 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False).encode()).hexdigest()


def _scope_key(scope):
    # Do not put requester/channel identifiers in filenames or cache contents.
    return _digest({str(k): str(v or "") for k, v in sorted((scope or {}).items())})


def _path(root, kind, source_sha, scope, inputs=None):
    key = _digest({"source_sha256": source_sha, "scope": _scope_key(scope),
                   "kind": kind, "inputs": inputs or {},
                   "schema": SCHEMA_VERSION, "algorithm": ALGORITHM_VERSION})
    return Path(root) / "intake-inspections" / key[:2] / f"{key}.json"


def _read(path, source_sha, kind, inputs):
    try:
        if path.stat().st_size > _MAX_ENTRY_BYTES:
            return None
        envelope = json.loads(path.read_text(encoding="utf-8"))
        payload = envelope["payload"]
        if (envelope.get("schema") != SCHEMA_VERSION
                or envelope.get("algorithm") != ALGORITHM_VERSION
                or envelope.get("source_sha256") != source_sha
                or envelope.get("kind") != kind
                or envelope.get("inputs") != (inputs or {})
                or envelope.get("payload_sha256") != _digest(payload)
                or not isinstance(payload, dict)):
            return None
        return payload
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _write(path, source_sha, kind, inputs, payload):
    try:
        envelope = {"schema": SCHEMA_VERSION, "algorithm": ALGORITHM_VERSION,
                    "source_sha256": source_sha, "kind": kind, "inputs": inputs or {},
                    "payload": payload, "payload_sha256": _digest(payload)}
        data = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if len(data.encode()) > _MAX_ENTRY_BYTES:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".inspection-", suffix=".partial", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
    except (OSError, TypeError, ValueError):
        return


_MAX_ENTRIES = 256


def get(root, kind, source_sha, scope, *, inputs=None):
    """Return a validated cache hit, or None."""
    path = _path(root, kind, source_sha, scope, inputs)
    payload = _read(path, source_sha, kind, inputs)
    if payload is None:
        return None
    try:
        os.utime(path, None)
    except OSError:
        pass
    return payload


def _prune(root):
    """Bound total retained inspection files, evicting least recently used first."""
    try:
        files = list((Path(root) / "intake-inspections").glob("*/*.json"))
        if len(files) <= _MAX_ENTRIES:
            return
        files.sort(key=lambda item: item.stat().st_mtime)
        for item in files[:len(files) - _MAX_ENTRIES]:
            try:
                item.unlink()
            except OSError:
                pass
    except OSError:
        return


def put(root, kind, source_sha, scope, payload, *, inputs=None):
    """Store complete, source-bound JSON inspection; unsafe outputs are skipped."""
    if not isinstance(payload, dict):
        return
    if kind == "audit" and not (payload.get("sha256") == source_sha
                                and isinstance(payload.get("color_manifest"), dict)
                                and payload["color_manifest"].get("source_sha256") == source_sha):
        return
    path = _path(root, kind, source_sha, scope, inputs)
    _write(path, source_sha, kind, inputs, payload)
    _prune(root)


def bind_audit_to_path(payload, current_path):
    """Rehydrate path metadata omitted from shared cache payloads."""
    result = dict(payload)
    result["path"] = str(current_path)
    return result
