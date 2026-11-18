"""Object-based firmware evidence for the three Creator firmwares (#1490).

ZMod, OpenCreator and Reforge run on the same Creator 5 and Creator 5 Pro
hardware and all speak Moonraker, so a configured model can easily point
at a printer running another firmware. The firmwares publish different
Moonraker objects, and those objects are the honest evidence available
without a live probe:

- ZMod publishes a ``zmod_color`` object (its active-tool record).
  Neither OpenCreator nor Reforge publishes one.
- OpenCreator's tool authority is its ``creator5_toolchanger`` Kalico
  module, it runs AFC in standalone toolchanger mode, and its Moonraker
  update manager carries a component named ``opencreator``. A status
  sample that carries objects or components under those names is
  OpenCreator evidence.
- Reforge's tool authority is its ``ff_toolchange`` Klipper extra and
  its print start is its ``ff_print`` extra (Reforge
  ``pkgs/klipper/payload/klipper/klippy/extras/ff_toolchange.py`` and
  ``ff_print.py``). Stock FlashForge Klipper has neither: its tool
  changes are done by the touchscreen application, not Klipper, and its
  own ``ff_*`` sections (``ff_eddy``) are not evidence. Reforge also
  runs AFC, so an AFC object is OpenCreator evidence only when no
  Reforge object is present.

The OpenCreator and Reforge object shapes are source-verified, not
hardware verified: these helpers only ever read the names of keys a
status sample already carries, never their inner fields, and absence of
evidence is never treated as evidence of any firmware.
"""
from __future__ import annotations


def looks_like_zmod(status) -> bool:
    """True when a status sample carries ZMod's active-tool object."""
    status = status if isinstance(status, dict) else {}
    return isinstance(status.get("zmod_color"), dict)


# Objects only Reforge's own Klipper extras publish.
REFORGE_OBJECTS = ("ff_toolchange", "ff_print")


def _names(status):
    """Key names of a status sample or an objects list; empty for anything else."""
    if isinstance(status, (dict, list, tuple)):
        return [name for name in status if isinstance(name, str)]
    return []


def reforge_evidence(status) -> list[str]:
    """Names in a status sample (or objects list) that only Reforge publishes."""
    return [name for name in _names(status) if name in REFORGE_OBJECTS]


def opencreator_evidence(status) -> list[str]:
    """Names in a status sample (or objects list) that only OpenCreator publishes.

    Matches the creator5_toolchanger object, AFC objects and any
    opencreator-named object or update component key, in sample order.
    AFC objects count only when no Reforge object is present: Reforge
    runs AFC too.
    """
    reforge = bool(reforge_evidence(status))
    found = []
    for name in _names(status):
        afc = name == "AFC" or name.startswith("AFC ") or name.startswith("AFC_")
        if (name == "creator5_toolchanger" or name.startswith("creator5_toolchanger ")
                or (afc and not reforge) or "opencreator" in name.lower()):
            found.append(name)
    return found


def identify(status) -> str | None:
    """The Creator firmware a status sample or objects list shows, or None.

    "zmod", "opencreator" or "reforge" when exactly one firmware's
    evidence is present; None when there is none (a stock printer, or a
    sample that did not ask for the right objects) or when evidence for
    more than one firmware conflicts.
    """
    names = _names(status)
    zmod, reforge = "zmod_color" in names, bool(reforge_evidence(names))
    # ZMod's AFC shim (zmod_afclite) and Reforge both publish AFC objects,
    # so AFC alone points at OpenCreator only when neither is present.
    opencreator = any(name.startswith("creator5_toolchanger") or "opencreator" in name.lower()
                      for name in names) or (bool(opencreator_evidence(names)) and not zmod)
    found = {family for family, present in (
        ("zmod", zmod), ("opencreator", opencreator), ("reforge", reforge)) if present}
    return found.pop() if len(found) == 1 else None


def zmod_mismatch_problem(status) -> str | None:
    """The warning for an OpenCreator model pointed at a ZMod printer."""
    if not looks_like_zmod(status):
        return None
    return ("this Creator printer appears to be running ZMod, not OpenCreator "
            "(it reports zmod_color, which OpenCreator does not publish); "
            "select the ZMod Creator model (flashforge.creator5 or "
            "flashforge.creator5_pro) for this printer, or install OpenCreator")


def opencreator_mismatch_problem(status) -> str | None:
    """The warning for a ZMod Creator model pointed at an OpenCreator printer."""
    evidence = opencreator_evidence(status)
    if not evidence or looks_like_zmod(status) or reforge_evidence(status):
        return None
    return ("this Creator printer appears to be running OpenCreator, not ZMod "
            f"(it reports {', '.join(evidence)} and no zmod_color); "
            "select the OpenCreator Creator model (flashforge_opencreator.creator5 or "
            "flashforge_opencreator.creator5_pro) for this printer, or install ZMod")


def reforge_mismatch_problem(status, configured: str) -> str | None:
    """The warning for a ZMod or OpenCreator Creator model pointed at a Reforge printer.

    ``configured`` names the firmware the configured model expects
    ("ZMod" or "OpenCreator").
    """
    evidence = reforge_evidence(status)
    if not evidence or looks_like_zmod(status):
        return None
    return (f"this Creator printer appears to be running Reforge, not {configured} "
            f"(it reports {', '.join(evidence)}); select the Reforge Creator model "
            "(flashforge_reforge.creator5 or flashforge_reforge.creator5_pro) for this "
            f"printer, or install {configured}")


def not_reforge_problem(status) -> str | None:
    """The warning for a Reforge model pointed at a ZMod or OpenCreator printer."""
    if reforge_evidence(status):
        return None
    if looks_like_zmod(status):
        return ("this Creator printer appears to be running ZMod, not Reforge "
                "(it reports zmod_color, which Reforge does not publish); select the "
                "ZMod Creator model (flashforge.creator5 or flashforge.creator5_pro) "
                "for this printer, or install Reforge")
    evidence = [name for name in opencreator_evidence(status)
                if name.startswith("creator5_toolchanger") or "opencreator" in name.lower()]
    if evidence:
        return ("this Creator printer appears to be running OpenCreator, not Reforge "
                f"(it reports {', '.join(evidence)}); select the OpenCreator Creator model "
                "(flashforge_opencreator.creator5 or flashforge_opencreator.creator5_pro) "
                "for this printer, or install Reforge")
    return None
