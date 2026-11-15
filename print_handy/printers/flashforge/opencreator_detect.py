"""Firmware identity detection for FlashForge Creator 5 / Creator 5 Pro (#1490).

Distinguishes ZMod (``zmod_color``) from OpenCreator (``creator5_toolchanger``)
over a Moonraker ``/printer/objects/list`` payload. Secondary evidence from
Moonraker update-manager component names and ``/server/info`` components is
used only in diagnostic wording and never overrides the object-list verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


ZMOD_OBJECT = "zmod_color"
OPENCREATOR_OBJECT = "creator5_toolchanger"

OPENCREATOR_UPDATE_COMPONENTS = ("opencreator", "opencreator-updater")
ZMOD_UPDATE_COMPONENTS = ("zmod",)

CREATOR_ZMOD_MODELS = frozenset({"flashforge.creator5", "flashforge.creator5_pro"})


@dataclass(frozen=True)
class FirmwareDetection:
    """Result of inspecting a Moonraker objects list for Creator firmware."""

    firmware: str  # "opencreator" | "zmod" | "both" | "unknown"
    has_opencreator: bool
    has_zmod: bool
    secondary_evidence: tuple[str, ...] = ()

    @property
    def evidence_note(self) -> str:
        if not self.secondary_evidence:
            return ""
        return " (" + "; ".join(self.secondary_evidence) + ")"


def _normalize_names(values: Iterable[str] | None) -> set[str]:
    if not values:
        return set()
    return {str(v).strip() for v in values if isinstance(v, str) and v.strip()}


def detect_creator_firmware(
    objects: Iterable[str] | None,
    *,
    update_components: Iterable[str] | None = None,
    server_components: Iterable[str] | None = None,
) -> FirmwareDetection:
    """Classify a Creator 5 / 5 Pro printer's firmware from its Moonraker objects."""
    obj_set = _normalize_names(objects)
    has_opencreator = OPENCREATOR_OBJECT in obj_set
    has_zmod = ZMOD_OBJECT in obj_set

    if has_opencreator and has_zmod:
        firmware = "both"
    elif has_opencreator:
        firmware = "opencreator"
    elif has_zmod:
        firmware = "zmod"
    else:
        firmware = "unknown"

    evidence: list[str] = []
    upd_set = _normalize_names(update_components)
    srv_set = _normalize_names(server_components)

    oc_upd = [name for name in OPENCREATOR_UPDATE_COMPONENTS if name in upd_set]
    if oc_upd:
        evidence.append(f"update manager lists {', '.join(oc_upd)}")
    zmod_upd = [name for name in ZMOD_UPDATE_COMPONENTS if name in upd_set]
    if zmod_upd:
        evidence.append(f"update manager lists {', '.join(zmod_upd)}")
    if "afc" in srv_set or "AFC" in obj_set:
        evidence.append("AFC present")

    return FirmwareDetection(
        firmware=firmware,
        has_opencreator=has_opencreator,
        has_zmod=has_zmod,
        secondary_evidence=tuple(evidence),
    )


def zmod_opencreator_mismatch_message(detection: FirmwareDetection) -> str | None:
    """Return a one-sentence mismatch message when a ZMod Creator profile sees OpenCreator."""
    if detection.firmware == "opencreator":
        return (
            f"this printer runs OpenCreator ({OPENCREATOR_OBJECT} is present and "
            f"{ZMOD_OBJECT} is absent){detection.evidence_note}, while this profile is "
            "for ZMod; set [printer] model to the OpenCreator model key once supported"
        )
    if detection.firmware == "both":
        return (
            f"this printer reports both {OPENCREATOR_OBJECT} and {ZMOD_OBJECT}"
            f"{detection.evidence_note}, while {ZMOD_OBJECT} is not reporting; "
            "verify the printer firmware and [printer] model"
        )
    return None
