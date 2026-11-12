"""Deterministic slice refusals that chat can explain and, where safe, fix.

A slice refused for a reason that will not change on its own (a slot whose
color was never confirmed, a slot with no spool recorded) used to surface
as "Slicing this model did not work ... PB_SLICING_UNKNOWN" plus a blind
"Retry slice once" that failed identically. A :class:`SliceRefusal` carries
the plain-language reason with it, is classified ``configuration`` (so its
marker is ``PH_SLICING_CONFIGURATION``, not ``*_UNKNOWN``), and may name one
safe fix the operator can apply with a button before the retry.

The exception crosses the heavy-worker process boundary as data
(:meth:`SliceRefusal.as_dict` / :func:`from_dict`), so the fix is data too:
``{"kind": "confirm_slot_color", "slot": "2", "hex": "C2185B"}`` and
:func:`apply_fix` performs it. The only fix today confirms a slot's color
code, which affects preview colors and 3MF labels, never toolpaths.
"""
from __future__ import annotations

from typing import Any

from . import filament_color


class SliceRefusal(RuntimeError):
    """A deterministic refusal: retrying unchanged would fail the same way.

    ``str(exc)`` keeps the original technical message (logs, receipts and
    existing tests match on it); ``plain`` is what chat says.
    """

    diagnostic_category = "configuration"

    def __init__(self, message: str, *, plain: str, next_step: str | None = None,
                 fix: dict[str, Any] | None = None, fix_label: str | None = None):
        super().__init__(message)
        self.plain = plain
        self.next_step = next_step
        self.fix = fix
        self.fix_label = fix_label

    def as_dict(self) -> dict[str, Any]:
        return {"message": str(self), "plain": self.plain, "next_step": self.next_step,
                "fix": self.fix, "fix_label": self.fix_label}


def from_dict(data: dict[str, Any]) -> SliceRefusal:
    return SliceRefusal(str(data.get("message") or "slice refused"),
                        plain=str(data.get("plain") or "Slicing was refused."),
                        next_step=data.get("next_step"), fix=data.get("fix"),
                        fix_label=data.get("fix_label"))


def unconfirmed_slot_color(slot: Any, record: dict[str, Any] | None, message: str) -> SliceRefusal:
    """A slot whose color was never confirmed (or never recorded).

    Asks one question — "Is Matte white PLA loaded in slot 1?" — whose Yes
    confirms the slot with the best color code available from the record
    (its hex, else its name, else the neutral default) and slices again.
    A slot with no spool recorded has no safe one-tap fix and only explains.
    A material mismatch never reaches here: callers refuse that separately.
    """
    record = record or {}
    label = filament_color.slot_label(slot, record)
    if not record.get("spoolman_id") or not record.get("material"):
        return SliceRefusal(message, plain=f"{label} has no spool recorded, so I stopped before slicing.",
                            next_step="Use `/spool` to record which spool is loaded there, then ask again.")
    hx, source = filament_color.resolve(record)
    if record.get("hex_source") in filament_color.DERIVED_SOURCES:
        source = record["hex_source"]  # filled on load from the name or the default
    loaded = label.split("(", 1)[1].rstrip(")") if "(" in label else "this spool"
    plain = (f"{label} was never confirmed as loaded, so I have not sliced yet. "
             "The confirmation only sets the slot's color code (the preview color and the "
             "file's color label); it does not change how the part prints.")
    if source == "default":
        plain += (f" I could not work out its color from the name, so it will use neutral gray "
                  f"(#{hx}) until you set the color with `/spool`.")
    return SliceRefusal(message, plain=plain,
                        next_step=f"Is {loaded} loaded in slot {slot}?",
                        fix={"kind": "confirm_slot_color", "slot": str(slot), "hex": hx},
                        fix_label=f"Yes, slot {slot} has it: slice")


def slot_mismatch(slot: Any, record: dict[str, Any] | None, wanted: dict[str, Any] | None,
                  message: str) -> SliceRefusal:
    """The slot's recorded spool or material differs from the one chosen.

    Never waved through: slicing a slot for a material it does not hold is
    the safety case. Explains in plain words with no one-tap fix and no
    blind retry; the operator re-records the slot with `/spool`.
    """
    record = record or {}
    wanted = wanted or {}
    label = filament_color.slot_label(slot, record)
    want = (str(wanted.get("name") or "").strip()
            or " ".join(str(wanted.get(k) or "").strip() for k in ("color", "material")).strip()
            or "the chosen spool")
    plain = (f"{label} does not match the spool chosen for this job ({want}), "
             "so I stopped before slicing.")
    return SliceRefusal(message, plain=plain,
                        next_step=f"Check what is loaded in slot {slot}, record it with `/spool`, then ask again.")


def split_cause(reason: Any) -> str:
    """Why the planner split, as one of space, materials, separate, resized, other (from its reason text)."""
    low = str(reason or "").casefold()
    if "mixed materials" in low:
        return "materials"
    if "separate printing" in low or "separately was chosen" in low:
        return "separate"
    if "resized per-part" in low or "resized or turned" in low:
        return "resized"
    if "do not fit" in low or "exceed the printer height" in low:
        return "space"
    return "other"


def assembly_cannot_split(colors, size_mm, bed_mm, message: str, reason: Any = None) -> SliceRefusal:
    """An assembly (one build object holding parts of several colors) that has to be split.

    The bodies of an assembly share one coordinate frame, so they cannot be
    moved onto separate single-color plates (#820). ``reason`` is the
    planner's own split reason; the wording follows it, so a size problem is
    only claimed when the planner found one. No one-tap fix: scaling a
    per-part job slices one plate per color, which an assembly cannot do.
    """
    size = ""
    if size_mm and bed_mm:
        size = (f" ({' x '.join(f'{float(v):.0f}' for v in size_mm)} mm on a "
                f"{' x '.join(f'{float(v):.0f}' for v in bed_mm)} mm bed)")
    head = f"This model is one assembly of {colors} colors{size}."
    tail = ("An assembly's colors cannot be split onto separate plates because its parts are fixed "
            "to each other, so I stopped before slicing.")
    cause = split_cause(reason)
    one_color = "choose One color for everything to print it in one color"
    if cause == "space":
        plain = f"{head} It does not leave room to print all of its colors on one plate. {tail}"
        step = f"Make the model smaller in your slicer so all of its colors fit on one plate, or {one_color}."
    elif cause == "materials":
        plain = (f"{head} Its colors are set to materials that cannot print in one job. {tail}")
        step = f"Use colors from one material family for this model, or {one_color}."
    elif cause == "separate":
        plain = f"{head} Printing its parts separately was chosen. {tail}"
        step = f"Ask for the parts to print together, or {one_color}."
    elif cause == "resized":
        plain = f"{head} It was resized, and a resized model prints one plate per color. {tail}"
        step = f"Print it at its original size so all colors share one plate, or {one_color}."
    else:
        plain = f"{head} The planner could not keep all of its colors on one plate. {tail}"
        step = f"Try a different size or arrangement in your slicer, or {one_color}."
    return SliceRefusal(message, plain=plain, next_step=step)


def apply_fix(fix: dict[str, Any] | None, *, printer_id: str | None = None) -> str:
    """Apply one refusal's fix. Returns a plain line saying what changed.

    ``confirm_slot_color`` sets the slot's color code only when it has
    none (a valid hex is never overwritten) and marks the slot's color
    confirmed: the operator just approved it on a button.
    """
    if not fix or fix.get("kind") != "confirm_slot_color":
        raise ValueError("no fix to apply")
    from . import printer_state
    with printer_state.hardware_lock(printer_id or None):
        state = printer_state.load(printer_id) if printer_id else printer_state.load()
        slot = str(fix["slot"])
        record = (state.get("filament_slots") or {}).get(slot)
        if not isinstance(record, dict):
            raise ValueError(f"slot {slot} is not in the hardware record")
        hx = filament_color.normalize_hex(fix.get("hex")) or filament_color.NEUTRAL_DEFAULT
        if (not filament_color.normalize_hex(record.get("hex"))
                or record.get("hex_source") in filament_color.DERIVED_SOURCES):
            # Only an empty code or this module's own guess is replaced; a hex
            # the operator, Spoolman or the printer supplied stays.
            record["hex"] = hx
            record["hex_source"] = "operator"
        record["physical_color_verified"] = True
        if printer_id:
            printer_state.save(state, printer_id)
        else:
            printer_state.save(state)
    return f"Slot {slot} color confirmed as #{record['hex'].lstrip('#').upper()}."


def explain(exc: BaseException) -> SliceRefusal | None:
    """The refusal behind ``exc`` when it is one, else None."""
    return exc if isinstance(exc, SliceRefusal) else None
