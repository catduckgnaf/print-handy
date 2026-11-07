"""The local roast engine (#1002, test layers 2 and 3).

Bank precedence (status first), eligibility (a line needing an unknown fact
is never drawn), title scrubbing, shuffle bag + history window, one roast
per print, failure injection, the year-long repeat simulation, and the
no-LLM proofs: nothing in the roast path may reach a model or the network.
"""
import ast
import json
import random
from pathlib import Path

import pytest

from print_handy.core import roast

ROOT = Path(__file__).resolve().parents[2]


def ctx(title="Benchy", **facts):
    return roast.build_context(title, **facts)


# ---------------------------------------------------------------- context

def test_unknown_facts_are_absent_never_invented():
    c = ctx("Widget", duration_s=None, grams=0, copies=1, times=1, material="", finish_hour=None)
    assert {k for k in c if not k.startswith("_")} == {"title"}


def test_title_is_scrubbed_before_it_reaches_a_template():
    dirty = "@everyone **Big** [Dragon](https://evil.example/x) #tag\n<@123> v2_final.gcode"
    clean = roast.scrub_title(dirty)
    for bad in ("@", "*", "[", "]", "(", ")", "http", "#", "<", "\n", ".gcode"):
        assert bad not in clean, (bad, clean)
    assert "Big" in clean and len(clean) <= roast.TITLE_MAX_CHARS
    assert roast.scrub_title("   ") == ""
    assert roast.scrub_title("a" * 300) == ""  # one 300-char word: no safe title, no roast


def test_contract_rejects_long_multi_sentence_quoted_and_hashtag_lines():
    assert roast.contract_problem(" ".join(["word"] * 21) + ".") == "over 20 words"
    assert roast.contract_problem("One thing. Another thing.") == "more than one sentence"
    assert roast.contract_problem('A "quoted" line.') == "quotes"
    assert roast.contract_problem("So #blessed.") == "hashtag"
    assert roast.contract_problem("Print #57 did fine.") is None  # a print number is not a hashtag
    assert roast.contract_problem("Fine line.", ["fine"]) == "blocklisted word 'fine'"


# ---------------------------------------------------------------- precedence

def _pool_names(c, status="completed"):
    return [name for name, _ in roast.candidate_pools(c, status)]


def test_status_outranks_everything():
    c = ctx("Benchy", duration_s=10 * 3600, grams=300, times=4, finish_hour=3)
    assert _pool_names(c, "failed") == ["failed"]
    assert _pool_names(c, "cancelled") == ["cancelled"]
    assert _pool_names(c, "spaghetti") == ["spaghetti"]
    assert _pool_names(c, "nonsense") == []


def test_completed_order_is_title_then_context_then_generic():
    c = ctx("Benchy", duration_s=10 * 3600, grams=300, times=4, finish_hour=3)
    assert _pool_names(c) == ["title:benchy", "overnight", "long_print", "heavy_filament",
                              "repeat_print", "generic"]
    assert _pool_names(ctx("Widget", duration_s=600)) == ["short_print", "generic"]
    assert _pool_names(ctx("Widget")) == ["generic"]


def test_phase2_precedence_pools():
    # Reprint-failed completed print outranks title reaction
    c_reprint = ctx("Benchy", prev_fails=2)
    assert _pool_names(c_reprint)[:2] == ["reprint_failed:completed", "title:benchy"]

    # History previous print comes after context banks and before color
    c_hist = ctx("Widget", duration_s=600, last_title="Benchy", color="Galaxy Black")
    assert _pool_names(c_hist) == ["short_print", "history:previous_print", "color:black", "generic"]

    # Color streak comes before color family
    c_streak = ctx("Widget", color="Crimson", color_streak=3)
    assert _pool_names(c_streak) == ["color:streak", "color:red", "generic"]

    # Failed prints with history facts: reprint_failed:failed and history:fail_streak
    c_fail = ctx("Benchy", prev_fails=1, fail_streak=2)
    assert _pool_names(c_fail, "failed") == ["reprint_failed:failed", "history:fail_streak", "failed"]


def test_title_keywords_match_through_the_shared_fuzzy_layer():
    assert _pool_names(ctx("Benchi test"))[0] == "title:benchy"       # one-edit typo
    assert _pool_names(ctx("Dragn pack"))[0] == "title:dragon"
    assert _pool_names(ctx("Brakcet set"))[0] == "title:bracket"     # transposition
    # Two edits away without an alias stays unmatched: the shared fuzzy policy,
    # not loosened for roasts.
    assert _pool_names(ctx("Boncho test")) == ["generic"]
    assert _pool_names(ctx("Big DRAGONS set"))[0] == "title:dragon"   # case + plural
    assert _pool_names(ctx("Phone Stand v3"))[0] == "title:phone stand"
    assert _pool_names(ctx("Boxer puppy")) == ["generic"]          # "box" is a short word: exact only


def test_dictionary_alias_resolution():
    assert _pool_names(ctx("3D calibration boat"))[0] == "title:benchy"
    assert _pool_names(ctx("tugboat"))[0] == "title:benchy"
    assert _pool_names(ctx("flexi rex"))[0] == "title:rex"
    assert _pool_names(ctx("Benchie test"))[0] == "title:benchy"


def test_a_failed_benchy_never_gets_success_language():
    for seed in range(20):
        line = roast.roast(ctx("Benchy", grams=40, material="PLA"), status="failed",
                           rng=random.Random(seed))
        texts = {t for _id, t in roast.load_corpus()["banks"]["failed"]}
        assert any(roast.render(t, ctx("Benchy", grams=40, material="PLA")) == line for t in texts)


def test_a_template_needing_an_unknown_fact_is_never_drawn():
    corpus = roast.load_corpus()
    lines = {roast.roast(ctx("Widget"), rng=random.Random(seed)) for seed in range(300)}
    entries = (corpus["banks"]["generic"]
               + corpus["banks"].get("dark_humor", [])
               + corpus["banks"]["one_liners"]
               + roast._framed_entries(ctx("Widget"), corpus))
    renders = {roast.render(t, ctx("Widget")) for _id, t in entries}
    assert lines <= renders - {None}


# ---------------------------------------------------------------- selection

def test_seeded_draws_are_deterministic(tmp_path):
    a = [roast.roast(ctx(), state_path=tmp_path / "a.json", rng=random.Random(7)) for _ in range(5)]
    b = [roast.roast(ctx(), state_path=tmp_path / "b.json", rng=random.Random(7)) for _ in range(5)]
    assert a == b and all(a)


def _eligible(c):
    corpus = roast.load_corpus()
    entries = (corpus["banks"]["generic"]
               + corpus["banks"].get("dark_humor", [])
               + corpus["banks"]["one_liners"]
               + roast._framed_entries(c, corpus))
    return {line for _id, t in entries
            if (line := roast.render(t, c)) and roast.contract_problem(line, corpus["blocklist"]) is None}


def test_history_window_prevents_repeats_until_every_eligible_line_is_used(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    rng = random.Random(1)
    eligible = _eligible(ctx("Widget"))
    monkeypatch.setattr(roast, "HISTORY_WINDOW", len(eligible) + 10)
    lines = [roast.roast(ctx("Widget"), state_path=state, rng=rng) for _ in range(len(eligible))]
    assert all(lines) and len(set(lines)) == len(eligible)  # every eligible line once, no repeat
    # Past that, the least recently used line comes back first, never nothing.
    assert roast.roast(ctx("Widget"), state_path=state, rng=rng) == lines[0]


def test_one_roast_per_print_even_on_replay(tmp_path):
    state = tmp_path / "state.json"
    first = roast.roast(ctx(), key="job@100", state_path=state, rng=random.Random(3))
    history = json.loads(state.read_text())["history"]
    for _ in range(5):
        assert roast.roast(ctx(), key="job@100", state_path=state, rng=random.Random(99)) == first
    assert json.loads(state.read_text())["history"] == history  # the bag did not advance


@pytest.mark.parametrize("payload", ["{broken", "[]", json.dumps({"bags": "x", "history": 5})])
def test_a_broken_state_file_still_roasts(tmp_path, payload):
    state = tmp_path / "state.json"
    state.write_text(payload)
    assert roast.roast(ctx(), state_path=state, rng=random.Random(1))


def test_failures_return_empty_never_raise(monkeypatch):
    monkeypatch.setattr(roast, "load_corpus", lambda: (_ for _ in ()).throw(roast.CorpusError("gone")))
    assert roast.roast(ctx()) == ""


def test_framed_assembly_produces_contract_passing_lines():
    corpus = roast.load_corpus()
    c = ctx("Benchy")
    framed = roast._framed_entries(c, corpus)
    assert len(framed) == len(corpus["fragments"]["openers"]) * 2
    passing = [tmpl for _eid, tmpl in framed
               if (line := roast.render(tmpl, c)) and roast.contract_problem(line, corpus["blocklist"]) is None]
    assert len(passing) >= 80


def test_color_roasts_family_matching_and_synonyms():
    assert roast.color_family_keys("Midnight Black") == ["black"]
    assert roast.color_family_keys("Charcoal Grey") == ["gray"]
    assert roast.color_family_keys("Crimson Red") == ["red"]
    assert roast.color_family_keys("Cyan") == ["blue"]
    assert roast.color_family_keys("Lime Green") == ["green"]
    assert roast.color_family_keys("Translucent Clear") == ["clear"]


def test_scrub_last_title():
    assert roast.scrub_last_title("Benchy v2_final.gcode") == "Benchy v2 final"
    assert roast.scrub_last_title("@everyone **Secret** print") == "Secret print"
    assert roast.scrub_last_title("a" * 100) == ""


def test_a_year_at_three_roasts_a_day_has_no_exact_repeat_inside_the_window(tmp_path):
    """The headline acceptance simulation: 3/day for 365 days of ordinary
    completed prints. No line repeats within the history window, and when
    eligible lines run out the engine reuses the least recently used one
    rather than failing (bounded work, measured here)."""
    state = tmp_path / "state.json"
    rng = random.Random(20261107)
    titles = ["Widget", "Benchy", "Cable Clip", "Drawer Organizer", "Hook", "Vase", "Bracket"]
    out = []
    for day in range(365):
        for n in range(3):
            c = ctx(titles[(day * 3 + n) % len(titles)], duration_s=rng.choice([900, 3600, 7200, 30000]),
                    material="PLA", color="Black", grams=rng.choice([12, 60, 220]),
                    printer="AD5X", finish_hour=rng.choice([2, 10, 15, 21]))
            out.append(roast.roast(c, state_path=state, rng=rng))
    assert all(out)
    window = 150
    for i in range(len(out)):
        assert out[i] not in out[max(0, i - window):i], (i, out[i])


# ---------------------------------------------------------------- no-LLM proofs

ROAST_MODULES = ["print_handy/core/roast.py", "print_handy/hermes/roast_chat.py"]
FORBIDDEN_IMPORTS = {"urllib", "http", "socket", "requests", "httpx", "aiohttp", "ssl",
                     "print_handy.ai_routes", "ai_routes", "print_handy.core.design"}


@pytest.mark.parametrize("path", ROAST_MODULES)
def test_roast_modules_import_no_network_or_model_client(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    hits = {n for n in names for bad in FORBIDDEN_IMPORTS
            if n == bad or n.startswith(bad + ".") or n.split(".")[-1] == bad}
    assert not hits, hits
    source = (ROOT / path).read_text(encoding="utf-8")
    assert "API_KEY" not in source and "os.environ" not in source


def test_generate_roast_has_no_model_call():
    source = (ROOT / "print_handy/hermes/moonraker_event_listener.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "generate_roast")
    body = ast.get_source_segment(source, fn)
    for bad in ("ai_routes", "urlopen", "CLIPROXY", "roast_model", "chat_url"):
        assert bad not in body, bad
