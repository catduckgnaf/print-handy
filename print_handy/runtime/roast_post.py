"""The per-print roast for a terminal post, family-neutral (#1672).

The roast step used to live only in the Moonraker listener
(``moonraker_event_listener._with_status_roast``), so a print that ended
on a PrusaLink, OctoPrint, Elegoo SDCP, stock Flashforge or Bambu
printer got a plain terminal post with at most a status quip, while a
Moonraker print got its roast. This module is the shared step: given a
print's filename, how it ended, the roster id of the printer it ran on
and a stable per-print key, it returns the one roast line the terminal
post should carry — or "" when there is none (roasts off, no title, a
failed draw), in which case the post stays exactly what it was.

``printer_id`` is explicit by contract: the family listener and the
Bambu sink already hold their printer's id and pass it down, so the
screen mirror inside ``generate_roast`` writes to the posting printer's
own screen. This module never guesses a printer. The one caller that
passes None is the Moonraker listener's own wrapper, where the roast
generator's existing fallback (this listener process's own printer) is
the posting printer by construction.

No LLM anywhere in this path (#1002): the line comes from the local
corpus through ``generate_roast``, under the same ``vision.roast_enabled``
switch. Best effort throughout — any failure is "" and the post is
unchanged.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from . import discord_print_summary as print_summary
from . import moonraker_discord_announcements as announcer

logger = logging.getLogger(__name__)


def roast_for_print(filename: str, status: str, *, printer_id: str | None,
                    key: str, facts: Mapping[str, Any] | None = None) -> str:
    """One roast line for a print that ended as ``status``, or "".

    ``status`` is a roast bank name: "completed", "failed", "cancelled"
    or "spaghetti" (spaghetti stays Moonraker-only — only the Moonraker
    camera watch raises it, and only its wrapper passes it here).
    ``key`` is stable per print (terminal kind plus filename, the shape
    the Moonraker paths use), so a reposted terminal notice repeats its
    line instead of drawing a new one. Caller ``facts`` win over the job
    record's; a cancelled print's grams are dropped from both, because
    they were never used.
    """
    try:
        title, known = print_summary.safe_label(filename), {}
        try:
            job, plate = print_summary.find_job(filename)
            title = (job or {}).get("title") or title
            known = dict(announcer.roast_facts(job, plate, {}) or {})
        except Exception as exc:
            logger.info("%s roast facts unavailable: %r", status, exc)
        known.update(facts or {})
        if status == "cancelled":
            known.pop("grams", None)
        from . import moonraker_event_listener as mel
        return mel.generate_roast(title, facts=known, key=key, status=status,
                                  printer_id=printer_id) or ""
    except Exception as exc:
        logger.info("%s roast skipped: %r", status, exc)
        return ""


__all__ = ("roast_for_print",)
