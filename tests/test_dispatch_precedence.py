"""Single-claim dispatcher precedence matrix (testing-hardening pass).

Issue #979's two causes were both precedence/selection bugs that no test
watched:

* H1 — "print it again" matched the affirmative-followup pattern inside
  ``model_link_dispatch`` (``_is_affirmative_followup`` search-matches
  "print it" inside "print it again"), which prepends the channel's
  remembered link BEFORE the reprint check runs, so the phrase re-sliced
  the remembered link instead of reprinting the last printed job.
* H2 — two "last print" resolvers disagreed: ``prepared_intake.last_job``
  picks the newest JOB.json by mtime (printed or not), while the Hermes
  ``resolve_last_print`` reads launch records only.

This file pins the claim surface three ways:

1. A phrase corpus over the pure claim predicates (affirmative followup,
   reprint request, repair classification). Overlapping claims are
   recorded AS overlaps — the corpus is the map a precedence fix must
   update deliberately, never a claim that today's overlaps are right.
   Known gaps are labeled in the corpus, not hidden.
2. The two last-print resolvers over one synthetic archive: since #988
   (#979 decision D2, launch-record selection) the natural-language
   resolver ``last_printed_job`` and ``/print-again``'s
   ``resolve_last_print`` name the SAME target, while
   ``newest_archived_job`` (record recency, the renamed ``last_job``)
   still names the newest record — the divergence can never return
   unnoticed.
3. The dispatch outcome for "print it again": with a remembered link AND
   an archived job, the reprint route wins (was a strict xfail until #988
   landed).
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import ai_fix, prepared_intake

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dispatch_precedence_plugin", ROOT / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

URL = "https://www.printables.com/model/111-remembered-widget"


def claims(text):
    """The set of deterministic routes whose text predicate fires."""
    out = set()
    if plugin._is_affirmative_followup(text):
        out.add("affirmative")
    if prepared_intake.reprint_request(text):
        out.add("reprint")
    if ai_fix.classify_request(text) == "repair":
        out.add("repair")
    return out


# phrase -> expected claim set TODAY. Entries marked KNOWN GAP / OVERLAP
# describe current behavior so a change in either direction is a
# deliberate, reviewed edit to this map — see the module docstring.
CORPUS = {
    "print it": {"affirmative"},
    "start it": {"affirmative"},
    "do it": {"affirmative"},
    "yes, print it": {"affirmative"},
    "lets print this": {"affirmative"},
    # OVERLAP (#979 H1): both predicates fire; dispatch order decides, and
    # today the link route decides wrong — see the strict xfail below.
    "print it again": {"affirmative", "reprint"},
    "print this again": {"affirmative", "reprint"},
    "reprint that": {"reprint"},
    "print another": {"reprint"},
    "print the previous one in black": {"reprint"},
    # Bare "print again" is claimed deterministically (#979 decision D6,
    # landed in #988).
    "print again": {"reprint"},
    # A link in the message suppresses the reprint reading entirely.
    f"print it again {URL}": {"affirmative"},
    "fix this": {"repair"},
    "fix this for my printer": {"repair"},
    # Questions ABOUT a defect are not repair requests (the #978 hijack
    # shape): the anchored repair pattern must not fire on them.
    "how do I fix this layer shift?": set(),
    "what causes a layer shift?": set(),
    "make it 20% smaller": set(),   # an edit, claimed by the edit route
    "print 57": set(),              # exact-number route, not a text claim
}


@pytest.mark.parametrize("phrase,expected", sorted(CORPUS.items()))
def test_phrase_claims_match_the_recorded_map(phrase, expected):
    assert claims(phrase) == expected


def test_combined_fix_and_edit_phrases_follow_the_lead_verb():
    """Combined phrases classify by their lead verb (#987 widened the
    repair reading): a fix-led combination is a repair request — the
    repair flow owns it and the edit rides along — while an edit-led
    combination stays an edit. Neither is silently dropped, and a
    question about a defect (the corpus rows above) is neither."""
    assert ai_fix.classify_request("fix this and make it smaller") == "repair"
    assert ai_fix.classify_request("fix this, then shrink it 20%") == "repair"
    assert ai_fix.classify_request("make it smaller and fix this") == "edit"
    assert "repair" in claims("fix this and make it smaller")
    assert "repair" not in claims("make it smaller and fix this")


def test_dispatch_hook_order_is_the_recorded_one():
    """The standalone Router registers the plugin hooks in list order and
    the first claim wins, so the relative order of the intake, reprint
    and edit hooks is load-bearing and pinned."""
    from print_handy.bot.router import Router

    names = Router().handler_names()
    assert names.index("model_link_dispatch") < names.index("print_again_dispatch")
    assert names.index("print_again_dispatch") < names.index("edit_dispatch")
    assert names.index("typed_quantity_dispatch") < names.index("model_link_dispatch")


# ------------------------------------------------------- resolver split

def _write_job(folder: Path, payload: dict, mtime: float):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "JOB.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_the_two_last_print_resolvers_name_different_targets(tmp_path):
    """One archive: the newest JOB.json was prepared but NEVER printed;
    an older job was launched. last_job (mtime) names the never-printed
    job; resolve_last_print (launch records) names the printed one.
    #979 H2 — decision D2 moves last_job to launch-record selection."""
    from print_handy.runtime import print_control

    archive = tmp_path / "archive"
    shield_src = tmp_path / "shield.stl"
    shield_src.write_bytes(b"solid shield")
    panel_src = tmp_path / "panel.stl"
    panel_src.write_bytes(b"solid panel")

    plate_dir = archive / "panel-job" / "sliced" / "2026" / "1107"
    plate_dir.mkdir(parents=True)
    plate = plate_dir / "plate_1.gcode"
    plate.write_text("; panel\nG1 X1\n", encoding="utf-8")
    (plate_dir / "plate_1.launch.json").write_text("{}", encoding="utf-8")

    _write_job(archive / "panel-job", {
        "title": "Panel A", "gcode": str(plate),
        "selected_source": {"site": "printables", "title": "Panel A"},
        "downloaded_file": str(panel_src), "intake_id": "panel",
    }, mtime=1_700_000_000)
    _write_job(archive / "shield-job", {
        "title": "Shield",  # prepared later, never launched
        "selected_source": {"site": "printables", "title": "Shield"},
        "downloaded_file": str(shield_src), "intake_id": "shield",
    }, mtime=1_800_000_000)

    # Record recency still names the newest record (it is not "last print").
    assert prepared_intake.newest_archived_job(archive)["title"] == "Shield"
    # Both last-PRINT resolvers agree on the job that actually printed.
    assert prepared_intake.last_printed_job(archive)["title"] == "Panel A"
    target = print_control.resolve_last_print(archive)
    assert target["title"] == "Panel A"
    assert target["reprint"] is True


# -------------------------------------------- the desired dispatch win

def test_print_it_again_reprints_instead_of_refetching_the_link(monkeypatch):
    """Full dispatch outcome: a channel with a remembered link and an
    archived last job. "print it again" must reach the intake as a REUSE
    of the archived job (``reuse`` set), not as a fresh fetch of the
    remembered link."""
    from print_handy.core import printer_state

    calls = {}

    async def fake_prepare_link(event, adapter, state, root, reuse=None, **kw):
        calls["reuse"] = reuse
        calls["text"] = event.text
        return {"action": "skip", "reason": "recorded"}

    monkeypatch.setattr(prepared_intake, "prepare_link", fake_prepare_link)
    monkeypatch.setattr(prepared_intake, "last_printed_job",
                        lambda archive=None, printer_id=None: {"title": "Panel A",
                                                               "intake_id": "panel"})
    monkeypatch.setattr(printer_state, "load", lambda **kw: object())
    monkeypatch.setattr(plugin, "_is_hosting_profile", lambda value: True)
    monkeypatch.setattr(plugin, "_recent_model_link", lambda source: URL)
    monkeypatch.setattr(plugin, "_routed_printer_id", lambda source, gateway=None: None)
    monkeypatch.setattr(plugin, "_undecided_printer_ids", lambda source, gateway=None: [])
    monkeypatch.setattr(plugin, "_model_instruction", lambda text, url: None)
    monkeypatch.setattr(plugin, "_adapter_for",
                        lambda gateway, source: S(send=_unused_send))
    from print_handy.bot import recent_links
    monkeypatch.setattr(recent_links, "remember", lambda *a, **kw: None)
    from print_handy.core import yolo as _yolo
    monkeypatch.setattr(_yolo, "gcode_file", lambda event: None)

    source = S(platform=S(value="discord"), chat_id="chan", thread_id=None,
               user_id="u1", profile="host", is_bot=False)
    event = S(text="print it again", source=source, metadata={},
              media_urls=[], attachments=[], message_id="m1", internal=False)
    gateway = S(_is_user_authorized_for_source=lambda src: True,
                _session_key_for_source=lambda src: "sess")
    result = asyncio.run(plugin.model_link_dispatch(event, gateway))
    assert result == {"action": "skip", "reason": "recorded"}
    assert calls["reuse"] is not None, (
        "the remembered link won: 'print it again' re-fetched the link "
        "instead of reprinting the archived job")


async def _unused_send(**kwargs):  # pragma: no cover - tripwire only
    raise AssertionError("the adapter must not be asked to send here")
