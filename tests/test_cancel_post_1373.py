"""#1373: an operator cancel posts the title and the roast line, nothing else.

Live: a cancel the operator asked for (and confirmed) repeated the start
summary, temperatures, Z height and console lines. The roast also cut the
title mid-phrase: "Cancelling Pinecone Cottage Lantern - Hanging or spared ...".
"""
from print_handy.core import roast
from print_handy.runtime import klipper_report
from print_handy.runtime import moonraker_event_listener as listener

LANTERN = "Pinecone Cottage Lantern - Hanging or Tabletop"


def test_long_title_keeps_its_name_not_a_subtitle_fragment():
    assert roast.scrub_title(LANTERN) == "Pinecone Cottage Lantern"
    assert roast.scrub_title("Rocket Planter: self watering, tall version for herbs") == "Rocket Planter"
    # A long single-part title is still cut, and never ends on a joiner.
    cut = roast.scrub_title("Big box with a lid and a hinge for all of the things")
    assert cut.split()[-1].casefold() not in {"or", "and", "with", "a", "for", "of", "the"}
    # Short titles are unchanged.
    assert roast.scrub_title("Cable clip") == "Cable clip"
    assert roast.scrub_title("Clip - v2") == "Clip - v2"


def test_operator_cancel_post_is_title_plus_roast(monkeypatch):
    sent = {}
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda now=None: True)
    monkeypatch.setattr(klipper_report, "report",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no printer report")))
    monkeypatch.setattr(listener, "capture_stop", lambda filename, trigger: "")
    monkeypatch.setattr(listener.print_summary, "find_job", lambda filename: ({"title": LANTERN}, None))
    monkeypatch.setattr(listener, "generate_roast",
                        lambda title, **kw: f"Cancelling {roast.scrub_title(title)} spared the plate.")
    monkeypatch.setattr(listener.starter_pings, "ping_for", lambda *a, **k: None)
    monkeypatch.setattr(listener, "post", lambda text, **kw: sent.update(text=text, **kw) or True)
    listener.post_cancel_report("Pinecone-Cottage-Lantern-Hanging-or-Tabl_plate_1.gcode")
    assert sent["text"] == (f"Print cancelled: {LANTERN}\n\n"
                            "Cancelling Pinecone Cottage Lantern spared the plate.")


def test_a_cancel_nobody_asked_for_keeps_the_full_report(monkeypatch):
    sent = {}
    monkeypatch.setattr(klipper_report, "cancelled_by_bot", lambda now=None: False)
    monkeypatch.setattr(klipper_report, "report", lambda *a, **k: "Nozzle 204°C (off)")
    monkeypatch.setattr(listener, "capture_stop", lambda filename, trigger: "")
    monkeypatch.setattr(listener, "_with_status_roast", lambda text, *a, **k: text)
    monkeypatch.setattr(listener.starter_pings, "ping_for", lambda *a, **k: None)
    monkeypatch.setattr(listener, "post", lambda text, **kw: sent.update(text=text) or True)
    listener.post_cancel_report("a.gcode")
    assert "Nozzle 204°C (off)" in sent["text"]
