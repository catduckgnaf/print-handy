"""Publish Print Handy's shared print-cost record to Moonraker (#1643)."""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
from typing import Any, Callable

from .. import moonraker as _transport

logger = logging.getLogger(__name__)
NAMESPACE = "helix-screen"
DATABASE_PATH = "/server/database/item"
SOURCE = "print-handy"


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _price_source(label: Any) -> str:
    """Map cost-engine provenance to HelixScreen's two-value wire contract."""
    text = str(label or "").lower()
    if "spoolman" in text:
        return "spoolman"
    if any(token in text for token in ("manual", "slicer", "default", "setting")):
        return "setting"
    return "setting"  # Unknown evidence is not upgraded into a spool price.


def _block(cost: dict[str, Any], *, at: float) -> dict[str, Any] | None:
    lines = cost.get("lines")
    if not isinstance(lines, list):
        return None
    filament = electricity = 0.0
    for line in lines:
        if not isinstance(line, dict):
            continue
        amount = _finite(line.get("amount"))
        if amount is None:
            return None
        if line.get("key") == "material":
            filament += amount
        elif line.get("key") == "electricity":
            electricity += amount
    tools = []
    material_lines = cost.get("material_lines")
    if isinstance(material_lines, list):
        for row in material_lines:
            if not isinstance(row, dict):
                continue
            grams = _finite(row.get("grams"))
            amount = _finite(row.get("amount"))
            if grams is None or amount is None:
                return None
            tool = row.get("tool", row.get("slot"))
            if type(tool) is not int:
                if len(material_lines) != 1:
                    continue  # Do not infer a tool number from a color name or row order.
                tool = -1  # HelixScreen's whole-file line.
            tools.append({
                "tool": tool,
                "grams": grams,
                "cost": amount,
                "price_source": _price_source(row.get("price_source")),
            })
    return {"total": round(filament + electricity, 2),
            "filament": round(filament, 2), "electricity": round(electricity, 2),
            "at": int(at), "tools": tools}


def build_record(filename: Any, file_modified: Any, cost: Any, *, at: float | None = None,
                 existing: Any = None) -> dict[str, Any] | None:
    """Build contract v1; preserve a valid other lifecycle block for this revision."""
    if not isinstance(cost, dict) or not filename:
        return None
    modified = _finite(file_modified)
    if modified is None:
        return None
    now = float(time.time() if at is None else at)
    currency = str(cost.get("currency_code") or "")
    if len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        return None
    old = existing if isinstance(existing, dict) else {}
    same_revision = (old.get("version") == 1 and old.get("source") == SOURCE
                     and old.get("filename") == str(filename) and old.get("currency") == currency
                     and _finite(old.get("file_modified")) == modified)
    record = {"version": 1, "source": SOURCE, "filename": str(filename),
              "file_modified": modified, "currency": currency}
    if same_revision:
        for state in ("estimate", "actual"):
            block = old.get(state)
            if isinstance(block, dict) and _valid_block(block):
                record[state] = block
    outcome = str(cost.get("outcome") or "estimate")
    state = "estimate" if outcome == "estimate" else "actual"
    block = _block(cost, at=now)
    if block is None:
        return None
    prior = record.get(state)
    if not prior or prior["at"] <= block["at"]:
        record[state] = block
    return record


def _valid_block(block: Any) -> bool:
    return (isinstance(block, dict) and all(_finite(block.get(k)) is not None
            for k in ("total", "filament", "electricity", "at"))
            and isinstance(block.get("tools"), list))


def record_key(filename: str) -> str:
    encoded = urllib.parse.quote(str(filename), safe="~").replace(".", "%2E")
    return "print_cost." + encoded


def _read(base_url: str, filename: str) -> Any:
    query = urllib.parse.urlencode({"namespace": NAMESPACE, "key": record_key(filename)})
    try:
        response = _transport.moonraker_json(base_url, f"{DATABASE_PATH}?{query}", timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        return None
    result = (response or {}).get("result") or {}
    return result.get("value")


def _post(base_url: str, payload: dict[str, Any]) -> Any:
    return _transport.moonraker_json(base_url, DATABASE_PATH, method="POST", data=payload, timeout=5)


def publish(base_url: Any, filename: Any, file_modified: Any, cost: Any, *,
            post: Callable[[str, dict[str, Any]], Any] | None = None,
            at: float | None = None, existing: Any = None) -> bool:
    """Best-effort publication; no cost estimate or timestamp is invented."""
    try:
        record = build_record(filename, file_modified, cost, at=at, existing=existing)
        if record is None or not base_url:
            return False
        payload = {"namespace": NAMESPACE, "key": record_key(record["filename"]), "value": record}
        (post or _post)(str(base_url), payload)
        return True
    except Exception as exc:
        logger.warning("HelixScreen cost publish failed for %s: %s", filename, exc)
        return False


_FILE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def file_lock(base_url: str, filename: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault((str(base_url), str(filename)), threading.Lock())
