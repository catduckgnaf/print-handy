"""Object-based firmware evidence for the two AD5M firmwares (#1506).

ZMod and Forge-X run on the same Adventurer 5M and Adventurer 5M Pro
hardware and both speak Moonraker, so a configured model can easily
point at a printer running the other firmware. This pair is a more
dangerous near-miss than most: both firmwares use a start macro named
START_PRINT, so a ZMod-shaped job can start on a Forge-X printer and
only then meet the different parameter contract. The firmwares publish
different Moonraker surfaces, and those names are the honest evidence
available without a live probe:

- Forge-X publishes a ``mod_params`` object (its SET_MOD settings
  system) and its Moonraker update manager carries a component named
  ``forge-x``. ZMod publishes neither.
- ZMod evidence is its own objects and update component: ``zmod_color``
  or ``zmod_ifs`` objects, or a ``zmod`` update component. (The ZMod
  AD5M models do not require zmod_color for readiness the way the
  multi-tool models do; this helper only names ZMod when a sample
  positively carries one of these names.)

The Forge-X object shapes are documented and config verified only,
not hardware verified (issue #1506): these helpers only ever read the
names of keys a status sample already carries, never their inner
fields, and absence of evidence is never treated as evidence of
either firmware.
"""
from __future__ import annotations


def forgex_evidence(status) -> list[str]:
    """Names in a status sample that only Forge-X publishes.

    Matches the mod_params object and any forge-x-named object or
    update component key, in sample order.
    """
    status = status if isinstance(status, dict) else {}
    found = []
    for name in status:
        if not isinstance(name, str):
            continue
        if (name == "mod_params" or name.startswith("mod_params ")
                or "forge-x" in name.lower() or "forge_x" in name.lower()):
            found.append(name)
    return found


def looks_like_zmod(status) -> bool:
    """True when a status sample positively carries a ZMod-only name."""
    status = status if isinstance(status, dict) else {}
    for name in status:
        if not isinstance(name, str):
            continue
        if name in ("zmod_color", "zmod_ifs") or name.startswith(("zmod_color ", "zmod_ifs ")):
            if isinstance(status.get(name), dict):
                return True
        if name.strip().lower() == "zmod":
            return True
    return False


def zmod_mismatch_problem(status) -> str | None:
    """The warning for a Forge-X model pointed at a ZMod printer."""
    if not looks_like_zmod(status):
        return None
    return ("this AD5M printer appears to be running ZMod, not Forge-X "
            "(it reports ZMod firmware surfaces, which Forge-X does not publish); "
            "select the ZMod AD5M model (flashforge.ad5m or "
            "flashforge.ad5m_pro) for this printer, or install Forge-X")


def forgex_mismatch_problem(status) -> str | None:
    """The warning for a ZMod AD5M model pointed at a Forge-X printer."""
    evidence = forgex_evidence(status)
    if not evidence or looks_like_zmod(status):
        return None
    return ("this AD5M printer appears to be running Forge-X, not ZMod "
            f"(it reports {', '.join(evidence)}); its START_PRINT takes different "
            "parameters than ZMod's and rejects some of ZMod's outright, so a "
            "ZMod-shaped job is not safe to start here; "
            "select the Forge-X AD5M model (flashforge_forgex.ad5m or "
            "flashforge_forgex.ad5m_pro) for this printer, or install ZMod")
