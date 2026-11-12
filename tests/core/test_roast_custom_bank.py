"""The custom roast bank (#1002 Phase 4): an operator's own house roasts.

An operator points ``vision.roast_custom_bank`` at a local JSON file and
its entries join the generic draw pool, additively, after passing the
same gates the shipped banks pass (schema, stable ids, known slots, the
filled contract, the blocklist, no duplicates or near-duplicates against
the shipped corpus or each other). A missing, unreadable or invalid file
— or an invalid entry inside a valid file — degrades to no custom
roasts, with the problem named, never a crash and never a blocked post.
No LLM anywhere: the custom path is data and validation only.
"""
import json
import logging
import random

import pytest

from print_handy import settings
from print_handy.core import roast

VALID = {"id": "custom-workshop-motto", "family": "custom-house",
         "text": "The workshop gremlin union has certified {title} as officially finished."}
VALID_BARE = "House rule number nine: {title} gets a bow before it gets a shelf."


def write_bank(tmp_path, payload, name="house-roasts.json"):
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def ctx(title="Widget", **facts):
    return roast.build_context(title, **facts)


# ---------------------------------------------------------------- loading

def test_a_custom_bank_loads_and_joins_the_generic_pool(tmp_path):
    path = write_bank(tmp_path, {"entries": [VALID, VALID_BARE]})
    corpus = roast.load_corpus(custom_path=str(path))
    custom = corpus["banks"]["custom"]
    assert [text for _id, text in custom] == [VALID["text"], VALID_BARE]
    assert corpus["custom_problems"] == []
    # Additive: the shipped generic bank is untouched, and the custom
    # entries ride in the same generic pool a completed print draws from.
    shipped = roast.load_corpus()
    assert corpus["banks"]["generic"] == shipped["banks"]["generic"]
    pools = dict(roast.candidate_pools(ctx(), corpus=corpus))
    generic_texts = {text for _id, text in pools["generic"]}
    assert VALID["text"] in generic_texts and VALID_BARE in generic_texts
    assert roast.corpus_size(custom_path=str(path)) == roast.corpus_size() + 2


def test_a_custom_line_is_actually_drawn_by_the_engine(tmp_path):
    path = write_bank(tmp_path, {"entries": [VALID]})
    corpus = roast.load_corpus(custom_path=str(path))
    # Seed the generic pool's bag with only the custom entry: the draw
    # must come from the custom bank, through the engine's own selection.
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"bags": {"generic": [VALID["id"]]},
                                 "history": [], "chosen": {}}), encoding="utf-8")
    line = roast.roast(ctx(), state_path=state, rng=random.Random(0),
                       custom_path=str(path))
    rendered = roast.render(VALID["text"], ctx())
    framed = roast.framing_options(rendered, "generic", ctx(), corpus)
    assert line == rendered or line in framed


def test_no_custom_bank_means_no_custom_entries_and_no_problems():
    corpus = roast.load_corpus()
    assert corpus["banks"].get("custom") in (None, [])
    assert corpus["custom_problems"] == []


# ------------------------------------------------------- named degradation

def test_a_missing_custom_file_is_a_named_noop(tmp_path, caplog):
    missing = tmp_path / "not-there.json"
    with caplog.at_level(logging.WARNING, logger="print_handy.core.roast"):
        corpus = roast.load_corpus(custom_path=str(missing))
    assert corpus["banks"]["custom"] == []
    assert len(corpus["custom_problems"]) == 1
    assert str(missing) in corpus["custom_problems"][0]
    assert "could not be read" in corpus["custom_problems"][0]
    assert any(str(missing) in record.getMessage() for record in caplog.records), \
        "the degradation is logged by name, not silent"
    # Roasts still work from the shipped corpus alone.
    assert roast.roast(ctx(), rng=random.Random(1), custom_path=str(missing))


def test_a_custom_file_that_is_not_json_is_a_named_noop(tmp_path):
    path = write_bank(tmp_path, "{not json")
    corpus = roast.load_corpus(custom_path=str(path))
    assert corpus["banks"]["custom"] == []
    assert len(corpus["custom_problems"]) == 1
    assert "not valid JSON" in corpus["custom_problems"][0]


def test_a_custom_file_with_the_wrong_shape_is_a_named_noop(tmp_path):
    path = write_bank(tmp_path, {"roasts": ["A fine house line about {title}."]})
    corpus = roast.load_corpus(custom_path=str(path))
    assert corpus["banks"]["custom"] == []
    assert any("entries" in problem for problem in corpus["custom_problems"])


def test_an_unreadable_custom_path_never_raises(tmp_path):
    # A directory is not a bank file: the draw degrades, the post goes out.
    corpus = roast.load_corpus(custom_path=str(tmp_path))
    assert corpus["banks"]["custom"] == []
    assert corpus["custom_problems"]
    assert roast.roast(ctx(), rng=random.Random(2), custom_path=str(tmp_path))


# ------------------------------------------------------------ entry gates

def test_invalid_entries_are_refused_by_name_and_valid_ones_still_load(tmp_path):
    shipped = roast.load_corpus()
    shipped_id, shipped_text = shipped["banks"]["generic"][0]
    blockword = shipped["blocklist"][0]
    entries = [
        VALID,
        {"id": "custom-unknown-slot", "family": "custom-house",
         "text": "The {owner} personally approved {title}."},
        {"id": "custom-too-long", "family": "custom-house",
         "text": " ".join(["word"] * 25) + " {title}."},
        {"id": "custom-blocked", "family": "custom-house",
         "text": f"Only a {blockword} would doubt {{title}} finished."},
        {"id": "custom-exact-dupe", "family": "custom-house", "text": shipped_text},
        {"id": shipped_id, "family": "custom-house",
         "text": "A perfectly original house line about {title} and its shelf."},
        {"id": "custom-near-dupe", "family": "custom-house",
         "text": shipped_text.rstrip(".") + "!"},
        {"id": "custom-self-dupe", "family": "custom-house", "text": VALID["text"]},
        {"text": "An entry with no stable id at all."},
    ]
    path = write_bank(tmp_path, {"entries": entries})
    corpus = roast.load_corpus(custom_path=str(path))
    loaded_ids = {eid for eid, _text in corpus["banks"]["custom"]}
    assert loaded_ids == {VALID["id"]}
    problems = "\n".join(corpus["custom_problems"])
    assert "custom-unknown-slot" in problems and "owner" in problems
    assert "custom-too-long" in problems and "over 20 words" in problems
    assert "custom-blocked" in problems and "blocklisted" in problems
    assert "custom-exact-dupe" in problems and "duplicate" in problems
    assert shipped_id in problems and "already used" in problems
    assert "custom-near-dupe" in problems and "near-duplicate" in problems
    assert "custom-self-dupe" in problems and "duplicate" in problems
    assert "no stable id" in problems


def _house_lines(count):
    words = ["anvil", "badger", "copper", "dynamo", "ember", "falcon", "garnet", "harbor",
             "indigo", "jasper", "kettle", "lantern", "marble", "nickel", "orchid", "pepper"]
    lines = []
    for n in range(count):
        a, b, c = words[n % 16], words[(n // 16) % 16], words[(n // 256) % 16]
        lines.append({"id": f"custom-house-{n}", "family": "custom-house",
                      "text": f"House {a} {b} {c} number {n} salutes {{title}} today."})
    return lines


def test_a_large_custom_bank_validates_quickly(tmp_path):
    """300 entries took about 11 s with the all-pairs difflib check, inside
    the print-completion post. The indexed check must stay well under that."""
    import time
    path = write_bank(tmp_path, {"entries": _house_lines(300)})
    started = time.monotonic()
    entries, _problems = roast.load_custom_bank(
        path, shipped_texts=[t for rows in roast.load_corpus()["banks"].values() for _i, t in rows])
    assert time.monotonic() - started < 3
    assert entries  # distinct lines load (near-duplicates of each other are refused)


def test_the_near_duplicate_index_agrees_with_the_plain_rule():
    """The length and word prefilters never change the verdict of the
    corpus gate's difflib rule, on shipped lines and close variants."""
    import difflib
    shipped = [t for rows in roast.load_corpus()["banks"].values() for _i, t in rows][:120]
    index = roast._NearDuplicateIndex(shipped)
    variants = []
    for text in shipped[:30]:
        variants += [text.rstrip(".") + "!", text.upper(), text[:-6], "Totally new " + text[10:],
                     text.replace(" ", "  ", 1), text[::-1]]

    def plain(candidate):
        folded = candidate.casefold()
        return any(difflib.SequenceMatcher(None, folded, other.casefold()).ratio()
                   > roast.NEAR_DUPLICATE_RATIO for other in shipped)

    for candidate in variants:
        assert index.matches(candidate) == plain(candidate), candidate


def test_an_oversized_custom_file_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(roast, "MAX_CUSTOM_BANK_BYTES", 1024)
    path = write_bank(tmp_path, {"entries": _house_lines(40)})
    entries, problems = roast.load_custom_bank(path)
    assert entries == []
    assert len(problems) == 1 and "KiB limit" in problems[0] and str(path) in problems[0]


def test_a_custom_bank_over_the_entry_limit_is_truncated_with_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(roast, "MAX_CUSTOM_ENTRIES", 5)
    path = write_bank(tmp_path, {"entries": _house_lines(12)})
    entries, problems = roast.load_custom_bank(path)
    assert len(entries) <= 5
    assert any("12 entries is over the 5-entry limit" in problem for problem in problems)
    assert not any(eid == "custom-house-5" for eid, _f, _t in entries)


def test_custom_entries_render_against_a_full_print(tmp_path):
    # A template that only fills when every fact is known still loads:
    # the gates fill it against a fully populated sample print, exactly
    # like the shipped corpus gates do.
    entry = {"id": "custom-full-facts", "family": "custom-house",
             "text": "{title} took {duration} and {grams} grams of {material} on {printer}."}
    path = write_bank(tmp_path, {"entries": [entry]})
    corpus = roast.load_corpus(custom_path=str(path))
    assert [text for _id, text in corpus["banks"]["custom"]] == [entry["text"]]
    # And it renders only when those facts are known, so at draw time it
    # is eligible only for prints that carry them (the engine skips a
    # template whose render comes back None).
    full = roast.build_context("Widget", duration_s=7200, grams=200, material="PLA", printer="AD5X")
    line = roast.render(entry["text"], full)
    assert line == "Widget took 2h and 200 grams of PLA on AD5X."
    assert roast.render(entry["text"], ctx("Widget")) is None


# ---------------------------------------------------------------- setting

def test_roast_custom_bank_setting_defaults_empty_and_loads(tmp_path):
    assert settings.load(local=False, environ={})["vision"]["roast_custom_bank"] == ""
    local = tmp_path / "local.yaml"
    local.write_text("vision:\n  roast_custom_bank: /data/house-roasts.json\n", encoding="utf-8")
    data = settings.load(local=local, environ={})
    assert data["vision"]["roast_custom_bank"] == "/data/house-roasts.json"
    # The environment form names the same setting.
    data = settings.load(local=False,
                         environ={"PRINT_HANDY__VISION__ROAST_CUSTOM_BANK": "/env/bank.json"})
    assert data["vision"]["roast_custom_bank"] == "/env/bank.json"


def test_roast_custom_bank_setting_refuses_a_non_string(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("vision:\n  roast_custom_bank: 42\n", encoding="utf-8")
    with pytest.raises(settings.SettingsError, match="roast_custom_bank"):
        settings.load(local=local, environ={})


# ------------------------------------------------------------- live wiring

def test_the_listener_draws_from_the_configured_custom_bank(tmp_path, monkeypatch):
    """generate_roast reads vision.roast_custom_bank from the live config:
    a configured bank's lines can be drawn, and a bank that vanishes is a
    named no-op — a roast still comes out of the shipped corpus."""
    from print_handy.runtime import moonraker_event_listener as mel

    path = write_bank(tmp_path, {"entries": [VALID]})
    state = tmp_path / "roast-state.json"
    state.write_text(json.dumps({"bags": {"generic": [VALID["id"]]},
                                 "history": [], "chosen": {}}), encoding="utf-8")
    monkeypatch.setattr(mel, "ROAST_STATE", state)
    monkeypatch.setitem(mel._config.cfg()["vision"], "roast_custom_bank", str(path))
    line = mel.generate_roast("Widget")
    rendered = roast.render(VALID["text"], roast.build_context("Widget"))
    corpus = roast.load_corpus(custom_path=str(path))
    assert line == rendered or line in roast.framing_options(
        rendered, "generic", roast.build_context("Widget"), corpus)

    monkeypatch.setitem(mel._config.cfg()["vision"], "roast_custom_bank",
                        str(tmp_path / "gone.json"))
    assert mel.generate_roast("Widget")
