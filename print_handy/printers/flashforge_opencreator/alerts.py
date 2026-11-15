"""OpenCreator alert descriptions. No hardware action is authorized here.

Prompt choices are the printer's offers, not inferred from spool metadata.
The action gate remains closed until the macros have been hardware verified.
"""
from __future__ import annotations

import math
import re
from typing import Any

RUNOUT_PREFIX = "oc-runout-"
RECOVERY_KIND = "oc-power-loss"
NOT_VERIFIED = ("OpenCreator recovery actions are not hardware verified. Nothing sent. "
                "Use the printer's own prompt after checking the printer.")
COMMANDS = {"ocload": "_OC_RUNOUT_LOAD_CONTINUE",
            **{f"ocswitch{n}": f"_OC_RUNOUT_SWITCH TO={n}" for n in range(4)},
            "ocresume": "RECOVER_PRINT", "ocdiscard": "CLEAR_POWER_LOSS_RECOVERY"}


def is_opencreator(adapter: Any) -> bool:
    model = getattr(adapter, "model", None)
    return str(getattr(model, "key", "")).startswith("flashforge_opencreator.")


def runout_kind(commands: list[str]) -> str | None:
    allowed = list(COMMANDS.values())[:5]
    if not commands or any(command not in allowed for command in commands):
        return None
    mask = sum(1 << i for i, command in enumerate(allowed) if command in commands)
    return f"{RUNOUT_PREFIX}{mask}"


def choices(kind: str) -> list[tuple[str, str]]:
    if kind == RECOVERY_KIND:
        return [("ocresume", "Resume"), ("ocdiscard", "Discard")]
    match = re.fullmatch(r"oc-runout-([1-9]|[12][0-9]|3[01])", str(kind))
    if not match:
        return []
    mask = int(match.group(1))
    offers = [("ocload", "Load and continue"),
              *[(f"ocswitch{n}", f"Switch to T{n}") for n in range(4)]]
    return [offer for i, offer in enumerate(offers) if mask & (1 << i)]


def recovery(status: dict) -> tuple[str, str] | None:
    """Stable interruption identity and honest display from source-defined fields."""
    row = status.get("power_loss_recovery")
    if not isinstance(row, dict) or row.get("recovery_available") is not True:
        return None
    filename = row.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        filename = "unknown file"
    position, size = row.get("file_position"), row.get("file_size")
    valid = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) for v in (position, size))
    progress = "file progress unknown"
    if valid:
        position, size = float(position), float(size)
        if size > 0 and 0 <= position <= size:
            progress = f"{position / size * 100:.1f}% of the file"
    holding = row.get("holding_bed")
    bed = ("The printer reports the bed is being held hot." if holding is True else
           "The printer reports it is not holding the bed hot." if holding is False else
           "Bed hold state unknown.")
    identity = repr((filename, position, size))
    text = (f"OpenCreator power-loss recovery available: {filename}\n{progress}. {bed}\n"
            "Resume and Discard require an operator press. Actions are not hardware verified "
            "and remain blocked in Print Handy; use the printer's own controls.")
    return identity, text


def refuse_action(action: str, alert: dict, adapter: Any) -> str:
    """Final safety gate shared by every alert transport; never sends a macro."""
    offered = {key for key, _label in choices(alert.get("kind", ""))}
    if not is_opencreator(adapter) or action not in offered:
        return "That OpenCreator alert action is not recognized for this printer. Nothing sent."
    return NOT_VERIFIED
