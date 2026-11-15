"""Object-based firmware evidence for the two Creator firmwares (#1490).

ZMod and OpenCreator run on the same Creator 5 and Creator 5 Pro hardware
and both speak Moonraker, so a configured model can easily point at a
printer running the other firmware. The firmwares publish different
Moonraker objects, and those objects are the honest evidence available
without a live probe:

- ZMod publishes a ``zmod_color`` object (its active-tool record).
  OpenCreator never publishes one.
- OpenCreator's tool authority is its ``creator5_toolchanger`` Kalico
  module, it runs AFC in standalone toolchanger mode, and its Moonraker
  update manager carries a component named ``opencreator``. A status
  sample that carries objects or components under those names is
  OpenCreator evidence.

The OpenCreator object shapes are source-verified against the fork,
not hardware verified (issue #1490): these helpers only ever read the
names of keys a status sample already carries, never their inner fields,
and absence of evidence is never treated as evidence of either firmware.
"""
from __future__ import annotations


def looks_like_zmod(status) -> bool:
    """True when a status sample carries ZMod's active-tool object."""
    status = status if isinstance(status, dict) else {}
    return isinstance(status.get("zmod_color"), dict)


def opencreator_evidence(status) -> list[str]:
    """Names in a status sample that only OpenCreator publishes.

    Matches the creator5_toolchanger object, AFC objects and any
    opencreator-named object or update component key, in sample order.
    """
    status = status if isinstance(status, dict) else {}
    found = []
    for name in status:
        if not isinstance(name, str):
            continue
        if (name == "creator5_toolchanger" or name.startswith("creator5_toolchanger ")
                or name == "AFC" or name.startswith("AFC ") or name.startswith("AFC_")
                or "opencreator" in name.lower()):
            found.append(name)
    return found


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
    if not evidence or looks_like_zmod(status):
        return None
    return ("this Creator printer appears to be running OpenCreator, not ZMod "
            f"(it reports {', '.join(evidence)} and no zmod_color); "
            "select the OpenCreator Creator model (flashforge_opencreator.creator5 or "
            "flashforge_opencreator.creator5_pro) for this printer, or install ZMod")
