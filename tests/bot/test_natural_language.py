"""The #1524 natural-language contract: gold corpus, mood, facade, context.

The corpus under tests/corpus/natural_language/ is the definition of
the layer's behavior: every positive utterance parses to exactly one
intent (pairwise ambiguity), negatives never allow an effect, follow-ups
resolve only inside their own scope, and a typo may cause a miss but
never a mutation.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from print_handy.bot import natural_language as nl

CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "natural_language"

RAW_SPOOLS = [
    {"id": 1, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Polymaker"}}, "remaining_weight": 800},
    {"id": 2, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": 450},
    {"id": 3, "filament": {"name": "Purple", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": None},
    {"id": 4, "filament": {"name": "Black", "material": "PETG",
                           "vendor": {"name": "Generic"}}, "remaining_weight": 300},
    {"id": 5, "filament": {"name": "Lilac", "material": "PLA",
                           "vendor": {"name": "Generic"}}, "remaining_weight": 200},
]
VOCAB = nl.build_vocabulary(RAW_SPOOLS, ["AD5X"])
RECORDS = [nl.normalize_spool(r) for r in RAW_SPOOLS]


def load(name):
    return json.loads((CORPUS / f"{name}.json").read_text())["cases"]


def effects_of(parsed):
    if parsed is None:
        return []
    if parsed.blocked and not parsed.routed_to:
        return []
    return [parsed.effect]


def check_case(case):
    parsed = nl.parse(case["text"], VOCAB)
    expected = case.get("intent")
    got = None if parsed is None else (parsed.routed_to or parsed.capability_id)
    assert got == expected, f"{case['text']!r}: intent {got} != {expected}"
    if case.get("mood") and parsed is not None:
        assert parsed.mood == case["mood"], f"{case['text']!r}: mood {parsed.mood}"
    for key, value in (case.get("slots") or {}).items():
        assert parsed is not None and parsed.slots.get(key) == value, \
            f"{case['text']!r}: slot {key}={parsed and parsed.slots.get(key)} != {value}"
    if case.get("allowed_effects") is not None:
        assert effects_of(parsed) == case["allowed_effects"], \
            f"{case['text']!r}: effects {effects_of(parsed)} != {case['allowed_effects']}"
    return parsed


@pytest.mark.parametrize("case", load("positive"), ids=lambda c: c["text"])
def test_positive_corpus(case):
    check_case(case)


@pytest.mark.parametrize("case", load("negative"), ids=lambda c: c["text"])
def test_negative_corpus(case):
    parsed = check_case(case)
    assert "mutating" not in effects_of(parsed)


@pytest.mark.parametrize("case", load("ambiguity"), ids=lambda c: c["text"])
def test_ambiguity_corpus(case):
    parsed = check_case(case)
    assert effects_of(parsed) == []


@pytest.mark.parametrize("case", load("typos"), ids=lambda c: c["text"])
def test_typo_corpus(case):
    parsed = check_case(case)
    assert "mutating" not in effects_of(parsed)


@pytest.mark.parametrize("case", load("positive"), ids=lambda c: c["text"])
def test_pairwise_ambiguity(case):
    """Each positive utterance matches exactly one intent (plan point 4)."""
    assert nl.matching_intents(case["text"]) == [case["intent"]]


def test_followups_share_scope_only():
    nl.CONTEXT.clear()
    key = ("user-1", "chan-1", "thread-1")
    for case in load("followups"):
        parsed = nl.parse_with_context(case["text"], key, VOCAB)
        got = None if parsed is None else (parsed.routed_to or parsed.capability_id)
        assert got == case["intent"], f"{case['text']!r}: {got} != {case['intent']}"
        for slot, value in (case.get("slots") or {}).items():
            assert parsed.slots.get(slot) == value, \
                f"{case['text']!r}: slot {slot}={parsed.slots.get(slot)} != {value}"
    # A different user/channel sees none of it.
    other = nl.parse_with_context("Only Polymaker", ("user-2", "chan-1", "thread-1"), VOCAB)
    assert other is None


def test_context_expires():
    store = nl.ContextStore(ttl_seconds=10, clock=lambda: 100.0)
    store.put("k", {"color": "purple"})
    assert store.get("k") == {"color": "purple"}
    store.clock = lambda: 111.0
    assert store.get("k") is None


def test_platform_parity():
    for case in load("platform"):
        parsed = nl.parse(case["text"], VOCAB)
        got = None if parsed is None else (parsed.routed_to or parsed.capability_id)
        assert got == case["intent"], f"{case['platform']}: {case['text']!r}"
        assert effects_of(parsed) == case["allowed_effects"]


# ---------------------------------------------------------------- inventory facts

def test_inventory_corpus_facts():
    for case in load("inventory"):
        parsed = nl.parse(case["text"], VOCAB)
        text = nl.inventory_answer_text(RECORDS, parsed.slots,
                                        capability_id=parsed.capability_id)
        facts = case["facts"]
        if facts.get("no_match"):
            assert "no" in text and "spools recorded" in text
            continue
        assert f"{facts['total_grams']:.0f} g" in text, text
        if facts.get("incomplete"):
            assert "incomplete" in text
        if facts.get("tier") == "exact":
            assert "broader color family" not in text


def test_inventory_unknown_is_not_zero():
    text = nl.inventory_answer_text(RECORDS, {"color": "purple", "material": "pla"})
    assert "1250 g" in text and "incomplete" in text
    only_unknown = [dict(r, remaining=None) for r in RECORDS[:1]]
    text = nl.inventory_answer_text(only_unknown, {"color": "purple"})
    assert "no total can be given" in text


def test_inventory_dedupes_and_skips_archived():
    dup = RECORDS + [dict(RECORDS[0])]
    text = nl.inventory_answer_text(dup, {"color": "purple", "material": "pla"})
    assert "1250 g" in text
    archived = [dict(RECORDS[3], archived=True)]
    text = nl.inventory_answer_text(archived, {"color": "black"})
    assert "no" in text and "spools recorded" in text


def test_spool_detail_only_real_fields():
    text = nl.spool_detail_text(RECORDS[0])
    assert "#1" in text and "800 g" in text
    assert "cost" not in text and "$" not in text and "purchased" not in text
    assert nl.spool_detail_text(None) == "That spool is not in Spoolman."


# ---------------------------------------------------------------- mood + facade

@pytest.mark.parametrize("text,mood", [
    ("Update ZMod", "imperative"),
    ("Is there an update?", "interrogative"),
    ("I updated ZMod yesterday", "past"),
    ("Don't stop the print", "negated"),
    ("Why did the print stop?", "interrogative"),
    ("Do not cancel anything", "negated"),
])
def test_mood(text, mood):
    assert nl.classify_mood(text) == mood


def test_mutating_intent_needs_imperative():
    asked = nl.parse("Is there an update?", VOCAB)
    assert asked.capability_id == "firmware_updates"
    applied = nl.parse("Update ZMod", VOCAB)
    assert applied.allowed_to_mutate()


def test_read_only_facade_has_no_writes():
    facade = nl.ReadOnlyFacade({"spools": lambda: [1, 2]})
    assert facade.spools() == [1, 2]
    for name in ("set_spool", "update", "firmware_report", "anything"):
        with pytest.raises(AttributeError):
            getattr(facade, name)
    with pytest.raises(AttributeError):
        facade.spools = lambda: []


# ---------------------------------------------------------------- firmware states

@pytest.mark.parametrize("kwargs,state", [
    (dict(installed="1.0", available="1.1"), "update_available"),
    (dict(installed="1.1", available="1.1"), "current"),
    (dict(feed_latest="2.0"), "release_unverified"),
    (dict(), "unknown"),
    (dict(reachable=False), "unavailable"),
    (dict(installed="1.0", available="1.1", apply_blocked_reason="busy"), "blocked"),
])
def test_firmware_states(kwargs, state):
    assert nl.classify_firmware_state("zmod", **kwargs) == state


def test_firmware_unknown_family_and_no_false_current():
    assert nl.classify_firmware_state("mysteryos") == "unsupported"
    text = nl.firmware_state_text("ZMod", "zmod", "unknown")
    assert "up to date" not in text and "unknown" in text


# ---------------------------------------------------------------- registry contract

def test_registry_is_complete():
    for cap in nl.CAPABILITIES:
        assert cap.description and cap.examples, cap.id
        assert cap.effect in (nl.READ_ONLY, nl.MUTATING, nl.EXTERNAL)
        if cap.id in nl.LIVE_CAPABILITIES:
            assert cap.handler and getattr(nl, cap.handler, None), cap.id
    for template_intent in {t.intent for t in nl.TEMPLATES}:
        assert template_intent in nl.REGISTRY, template_intent


def test_help_is_generated_from_the_registry():
    text = nl.help_text()
    for cap in nl.CAPABILITIES:
        assert cap.examples[0] in text


# ---------------------------------------------------------------- shadow + live answer

def _core(cfg=None):
    return SimpleNamespace(cfg=cfg or {})


def _event(text):
    return SimpleNamespace(
        text=text, source={"platform": "discord", "user_id": "u1", "channel_id": "c1"})


def test_shadow_mode_logs_and_answers_nothing(monkeypatch):
    monkeypatch.setattr(nl, "_fetch_spool_records", lambda core: (RECORDS, set()))
    nl._shadow_disagreements.clear()
    core = _core({"natural_language": {"shadow_mode": True}})
    assert nl.answer(core, _event("How much purple PLA do I have?"), {}) is None
    assert nl._shadow_disagreements and nl._shadow_disagreements[-1]["intent"] == "inventory_total"


def test_live_inventory_answer(monkeypatch):
    monkeypatch.setattr(nl, "_fetch_spool_records", lambda core: (RECORDS, set()))
    reply = nl.answer(_core(), _event("How much purple PLA do I have?"), {})
    assert reply is not None and "1250 g" in reply and "incomplete" in reply


def test_live_answer_never_comes_from_a_mutating_intent(monkeypatch):
    monkeypatch.setattr(nl, "_fetch_spool_records", lambda core: (RECORDS, set()))
    assert nl.answer(_core(), _event("Update ZMod"), {}) is None
    assert nl.answer(_core(), _event("Load spool 1 into slot 2"), {}) is None


def test_unavailable_spoolman_says_unavailable(monkeypatch):
    def boom(core):
        raise RuntimeError("down")
    monkeypatch.setattr(nl, "_fetch_spool_records", boom)
    reply = nl.answer(_core(), _event("How much purple PLA do I have?"), {})
    assert reply is not None and "unavailable" in reply


def test_typo_never_mutates():
    clean = nl.parse("Load spool 12 into slot 2", VOCAB)
    assert clean.allowed_to_mutate()
    text = "Load spool 12 into slot 2"
    for i in range(len(text)):
        variant = text[:i] + text[i + 1:]
        parsed = nl.parse(variant, VOCAB)
        if parsed is not None and parsed.allowed_to_mutate():
            assert parsed.capability_id == clean.capability_id


def test_route_counters_and_unmatched_log():
    before = nl.route_counters()
    nl.record_route("deterministic")
    assert nl.route_counters()["deterministic"] == before["deterministic"] + 1
    nl.log_unmatched("mail me at a@b.com about https://x.example/1", enabled=True)
    logged = nl.export_unmatched()
    assert "a@b.com" not in logged[-1]["phrase"] and "https" not in logged[-1]["phrase"]
    nl.log_unmatched("not logged", enabled=False)
    assert nl.export_unmatched()[-1]["phrase"] != "not logged"
