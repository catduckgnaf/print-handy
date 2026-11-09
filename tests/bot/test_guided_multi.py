"""Guided setup, add-a-printer loop (#337): the review step's `add` reply.

The base seven steps are unchanged (tests/bot/test_guided.py pins them);
these tests drive what happens after them: a second printer is collected,
the review lists the roster, `apply` writes a [[printers]] config the real
loader resolves, `manual` spells the roster out, and an existing file that
already describes a printer refuses the roster apply byte-identical.
"""
from pathlib import Path

import yaml

from print_handy import settings
from print_handy.bot import guided

CHAT = "100000000000000001"
USER = "200000000000000002"


def store(tmp_path):
    return guided.SetupStore(tmp_path / "guided-setup.json")


def say(store_obj, text, key="k", **kwargs):
    kwargs.setdefault("report", lambda: "REPORT")
    return guided.handle_text(key, text, store=store_obj, **kwargs)


def finish_steps(store_obj, key="k"):
    say(store_obj, "/print-handy setup", key=key)
    for answer in ("flashforge.ad5x", CHAT, USER,
                   "http://ad5x.local:7125", "skip", "stored"):
        say(store_obj, answer, key=key)


def add_a_printer(store_obj, key="k"):
    """Review -> add -> first id, new id, model, its own channel, Moonraker."""
    reply = say(store_obj, "add", key=key)
    assert "Add a printer (1 of 5)" in reply
    say(store_obj, "workshop-ad5x", key=key)        # the first printer's id
    say(store_obj, "garage-a1", key=key)            # the new printer's id
    say(store_obj, "bambulab.a1", key=key)          # its model
    say(store_obj, "100000000000000003", key=key)   # its own channel (required:
    return say(store_obj, "skip", key=key)          # the main one is printer 1's)


def test_add_cycle_review_lists_the_roster(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    review = add_a_printer(store_obj)
    assert "Saved: printer garage-a1." in review
    assert "First printer id: `workshop-ad5x`" in review
    assert "Added printer: `garage-a1`" in review and "`bambulab.a1`" in review
    session = store_obj.get("k")
    assert session["status"] == "awaiting_consent"
    assert session["answers"]["printer_id"] == "workshop-ad5x"
    assert session["answers"]["printers"] == [
        {"id": "garage-a1", "model": "bambulab.a1",
         "chat_id": "100000000000000003", "moonraker_url": ""}]


def test_add_cycle_apply_writes_a_loadable_roster(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    add_a_printer(store_obj)
    local = tmp_path / "local.yaml"
    reply = say(store_obj, "apply", apply_config=lambda answers: guided._apply_answers(answers, local))
    assert "Setup complete" in reply and "[[printers]] roster (2 printers" in reply
    written = yaml.safe_load(local.read_text())
    assert [entry["id"] for entry in written["printers"]] == ["workshop-ad5x", "garage-a1"]
    assert written["printers"][0]["moonraker_url"] == "http://ad5x.local:7125"
    data = settings.load(local=local, environ={})
    assert [p["id"] for p in settings.printers(data)] == ["workshop-ad5x", "garage-a1"]


def test_add_cycle_manual_text_spells_out_the_roster(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    add_a_printer(store_obj)
    text = say(store_obj, "manual")
    assert "[[printers]]" in text
    assert "`id` = `workshop-ad5x`" in text and "`id` = `garage-a1`" in text
    assert "PRINT_HANDY__PRINTERS__<ID>__<KEY>" in text


def test_roster_apply_refuses_a_file_that_already_describes_a_printer(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    add_a_printer(store_obj)
    local = tmp_path / "local.yaml"
    local.write_text("printer:\n  model: flashforge.ad5x\n")
    before = local.read_bytes()
    reply = say(store_obj, "apply", apply_config=lambda answers: guided._apply_answers(answers, local))
    assert "The settings could not be saved" in reply
    assert "nothing was changed" in reply
    assert "checklist is unchanged" in reply
    assert local.read_bytes() == before
    assert store_obj.get("k")["status"] == "awaiting_consent"  # the checklist survives


def test_second_cycle_skips_the_first_id_question(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    add_a_printer(store_obj)
    reply = say(store_obj, "add")
    assert "Add a printer (1 of 4)" in reply  # no first_id step the second time


def test_cancel_mid_add_cycle_resumes_the_cycle(tmp_path):
    store_obj = store(tmp_path)
    finish_steps(store_obj)
    say(store_obj, "add")
    say(store_obj, "workshop-ad5x")
    say(store_obj, "/print-handy cancel")
    reply = say(store_obj, "/print-handy setup")
    assert "Add a printer (2 of 5)" in reply
