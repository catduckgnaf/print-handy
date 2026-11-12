"""Local, factual-stage status quips. No LLM, printer calls, or automatic actions.

The authoritative progress/error text stays separate. This engine only supplies
optional jokes after a stage is actually entered. It borrows roast.py's stable
ids, family spacing, persisted history, and replay-stable selections, without
mixing in the completion roast corpus or altering its existing API.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from importlib import resources
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import tempfile
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)

BANK_FILES = ("intake", "preflight", "planning", "slicing", "printing")
STATE_NAME = "status_voice_state.json"
EXPECTED_COUNT = 1000
MAX_HISTORY = 200
MAX_EVENT_KEYS = 500
MAX_USERS = 3000
_MUTEX = RLock()
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

try:
    import fcntl
except ImportError:  # Windows development; in production Docker uses flock
    fcntl = None


@lru_cache(maxsize=1)
def load_bank() -> dict[str, tuple[dict[str, Any], ...]]:
    """Read and verify the shipped corpus, returning stages with stable ids."""
    stages: dict[str, tuple[dict[str, Any], ...]] = {}
    ids: set[str] = set()
    texts: set[str] = set()
    for filename in BANK_FILES:
        ref = resources.files("print_handy").joinpath(
            "data", "status_voice", filename + ".json")
        payload = json.loads(ref.read_text(encoding="utf-8"))
        for stage, rows in payload["stages"].items():
            if stage in stages or not isinstance(rows, list):
                raise ValueError(f"duplicate or invalid status voice stage {stage}")
            checked = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"invalid status voice entry in {stage}")
                eid, family, message = row.get("id"), row.get("family"), row.get("text")
                if not all(isinstance(x, str) and x.strip() for x in (eid, family, message)):
                    raise ValueError(f"invalid status voice fields in {stage}")
                if eid in ids or message.casefold() in texts:
                    raise ValueError(f"duplicate status voice entry {eid}")
                if len(message) > 300 or len(message.split()) > 55:
                    raise ValueError(f"status voice entry too long: {eid}")
                if set(_PLACEHOLDER.findall(message)) - {"copies"}:
                    raise ValueError(f"unknown status voice placeholder: {eid}")
                if row.get("first_time") and (
                    stage != "quantity_selected" or
                    type(row.get("exact_copies")) is not int or
                    row["exact_copies"] < 1
                ):
                    raise ValueError(f"unscoped first time message: {eid}")
                ids.add(eid)
                texts.add(message.casefold())
                checked.append(row)
            stages[stage] = tuple(checked)
    if len(ids) != EXPECTED_COUNT or len(stages) != 25:
        raise ValueError(f"status voice corpus has {len(ids)} entries in {len(stages)} stages")
    return stages


def _empty_state() -> dict[str, Any]:
    return {"bags": {}, "history": [], "chosen": {}, "first_seen": {}}


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return {**_empty_state(), **value}
    except (OSError, ValueError, TypeError):
        pass
    return _empty_state()


def _save(path: Path, state: dict[str, Any]) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".status_voice_", suffix=".tmp", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(state, handle, separators=(",", ":"), ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


@contextmanager
def _locked(path: Path):
    """Atomic selection across threads and, on Linux, across bot processes."""
    with _MUTEX:
        path.parent.mkdir(parents=True, exist_ok=True)
        with (path.parent / (path.name + ".lock")).open("a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _render(row: dict[str, Any], copies: int | None) -> str | None:
    required = set(_PLACEHOLDER.findall(row["text"]))
    if required and ("copies" not in required or copies is None):
        return None
    if row.get("exact_copies") is not None and copies != row["exact_copies"]:
        return None
    return row["text"].replace("{copies}", str(copies)) if copies is not None else row["text"]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _choose(
    stage: str, *, copies: int | None, identity: str, event_key: str | None,
    path: Path, rng: random.Random
) -> str:
    rows = load_bank().get(stage, ())
    if not rows:
        return ""
    identity_key = _fingerprint(identity) if identity else ""
    replay_key = _fingerprint(f"{identity_key}:{stage}:{event_key}") if event_key else ""

    with _locked(path):
        state = _read(path)
        chosen = state["chosen"] if isinstance(state.get("chosen"), dict) else {}
        if replay_key and isinstance(chosen.get(replay_key), dict):
            return str(chosen[replay_key].get("line") or "")

        seen = state["first_seen"] if isinstance(state.get("first_seen"), dict) else {}
        first_for_user = set(seen.get(identity_key) or []) if identity_key else set()
        eligible_specials = [
            row for row in rows
            if row.get("first_time") and identity_key
            and row["id"] not in first_for_user
            and _render(row, copies) is not None
        ]
        if eligible_specials:
            # No probability: a verified first 2, 10 or 11 selection gets the
            # Easter egg before ordinary random selection.
            selected = eligible_specials[0]
            first_for_user.add(selected["id"])
            seen[identity_key] = sorted(first_for_user)
            if len(seen) > MAX_USERS:
                seen.pop(next(iter(seen)))
            state["first_seen"] = seen
        else:
            ordinary = [
                row for row in rows
                if not row.get("first_time") and _render(row, copies) is not None
            ]
            if not ordinary:
                return ""
            history = state["history"] if isinstance(state.get("history"), list) else []
            bags = state["bags"] if isinstance(state.get("bags"), dict) else {}
            valid_ids = {row["id"] for row in ordinary}
            bag = [eid for eid in bags.get(stage, []) if eid in valid_ids]
            if not bag:
                bag = sorted(valid_ids)
            options = [row for row in ordinary if row["id"] in bag]
            recent = set(history[-min(24, len(history)):])
            fresh = [row for row in options if row["id"] not in recent]
            if fresh:
                options = fresh
            else:
                # Avoid a tiny eligible set getting stuck behind old history.
                positions = {eid: index for index, eid in enumerate(history)}
                earliest = min(positions.get(row["id"], -1) for row in options)
                options = [row for row in options
                           if positions.get(row["id"], -1) == earliest]
            by_id = {row["id"]: row for row in ordinary}
            recent_families = {
                by_id[eid]["family"] for eid in history[-8:] if eid in by_id
            }
            different_families = [
                row for row in options if row["family"] not in recent_families
            ]
            if different_families:
                options = different_families
            selected = rng.choice(options)
            bag.remove(selected["id"])
            bags[stage] = bag
            state["bags"] = bags

        line = _render(selected, copies)
        history = state["history"] if isinstance(state.get("history"), list) else []
        state["history"] = (history + [selected["id"]])[-MAX_HISTORY:]
        if replay_key:
            chosen[replay_key] = {"line": line, "id": selected["id"]}
            while len(chosen) > MAX_EVENT_KEYS:
                chosen.pop(next(iter(chosen)))
            state["chosen"] = chosen
        _save(path, state)
        return line or ""


def quip(
    stage: str, *, copies: int | None = None, identity: str = "",
    event_key: str | None = None, state_path: str | Path | None = None,
    rng: random.Random | None = None,
) -> str:
    """One optional joke, only after the caller has entered the factual stage.

    A caller must pass a verified copy count for quantity_selected, and a
    stable identity + event key for the first-time Easter eggs and replays.
    Missing files/state/permissions simply return an empty string so the
    important workflow messages still go out.
    """
    try:
        if copies is not None and (type(copies) is not int or copies < 1):
            return ""
        if stage == "quantity_selected" and copies is None:
            return ""
        if state_path is None:
            from .. import settings
            state_path = settings.state_dir() / STATE_NAME
        return _choose(
            stage, copies=copies, identity=identity, event_key=event_key,
            path=Path(state_path), rng=rng or random.Random()
        )
    except Exception:
        logger.exception("status voice unavailable for stage %s", stage)
        return ""


__all__ = ("EXPECTED_COUNT", "STATE_NAME", "load_bank", "quip")
