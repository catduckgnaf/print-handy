"""Hardening-pass pins for the shared scorer (post-2.2.1 audit, toward 2.2.2).

Four groups, each measured against the pre-change scorer while the pass
was developed (numbers in the PR body):

- the Unicode fold (F5): diacritics fold consistently, CJK text survives;
- the window-pass caps (F2): over-cap inputs fall back to the
  whole-string score instead of the quadratic window scan;
- canonical forms: number words == digits, domain abbreviations and
  compounds, plurals/verb forms, and the phonetic rescue pass for
  spelling-by-ear pairs the plain pass scores 0;
- the confidence/conflict policy (``fuzzy.decide``).
"""
import pytest

from print_handy.core import fuzzy, fuzzy_vocabulary


# ------------------------------------------------------------------- fold

@pytest.mark.parametrize("query,target", [
    ("café", "cafe"),
    ("naïve", "naive"),       # scored 0 before the fold: the ï split the word
    ("résumé", "resume"),
    ("ＡＢＣ", "abc"),          # fullwidth forms folded to nothing before
    ("喷嘴堵塞", "喷嘴堵塞"),     # identical CJK scored 0 before: fold erased it
    ("bed adhesion", "bed adhesion"),
])
def test_fold_equates_unicode_variants(query, target):
    assert fuzzy.norm(query) == fuzzy.norm(target)
    assert fuzzy.fuzzy_score(query, target) == 99


def test_fold_keeps_scripts_distinct():
    # Folding must not collapse different CJK strings into each other.
    assert fuzzy.norm("喷嘴堵塞") != fuzzy.norm("喷嘴畅通")
    assert fuzzy.fuzzy_score("喷嘴堵塞", "加热座") == 0


# ------------------------------------------------------------- window caps

_STUFFED = " ".join(["Benchy", "3D", "Printer", "Test", "Model", "Calibration",
                     "Print", "Quality", "Check", "Best", "Settings", "PLA",
                     "PETG", "ABS", "Fast", "Easy", "Cute", "Gift", "Toy",
                     "Display", "Shelf", "Desk", "Office", "Home", "Decor",
                     "Miniature", "Collectible", "Vase", "Bracket", "Holder",
                     "Mount", "Stand", "Box", "Case", "Lid", "Gear", "Wheel",
                     "Puzzle", "Dragon"])


def test_window_pass_is_capped_for_stuffed_titles():
    # 38 tokens: over MAX_WINDOW_TARGET_TOKENS, so no window scan happens
    # and the whole-string score stands (the "desk stand" window inside
    # would otherwise score ~90 and cost the quadratic pass).
    assert len(_STUFFED.split()) > fuzzy.MAX_WINDOW_TARGET_TOKENS
    assert fuzzy.windowed_fuzzy_score("desk stand", _STUFFED) == \
        fuzzy.fuzzy_score("desk stand", _STUFFED)


def test_window_pass_is_capped_for_very_long_queries():
    query = "desk stand " + " ".join(f"filler{i}" for i in range(20))
    assert len(query.split()) > fuzzy.MAX_WINDOW_QUERY_TOKENS
    assert fuzzy.windowed_fuzzy_score(query, "G4 Instant Desk Stand") == \
        fuzzy.fuzzy_score(query, "G4 Instant Desk Stand")


def test_window_pass_still_applies_within_the_caps():
    # The existing behaviour for ordinary titles is untouched by the caps.
    assert fuzzy.windowed_fuzzy_score("desk stadn", "G4 Instant Desk Stand") == 90


# ------------------------------------------------------- canonical: numbers

@pytest.mark.parametrize("query,target", [
    ("mark 2", "Mark Two"),
    ("mark two", "Mark 2"),
    ("2nd layer", "second layer"),
    ("second layer", "2nd Layer"),
    ("3rd", "third"),
    ("first", "1st"),
    ("twenty one pieces", "21 pieces"),
    ("21 pieces", "twenty-one pieces"),
    ("twentyone pieces", "21 pieces"),
    ("version three", "V3"),
    ("v3", "Version 3"),
    ("one hundred", "100"),
    ("two hundred five", "205"),
    ("ninety nine", "99"),
    ("twelve mm", "12 mm"),
    ("zero clearance", "0 clearance"),
    ("firstlayer", "first layer"),
    ("one hundred and five", "105"),
])
def test_number_forms_match(query, target):
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR


def test_first_layer_keeps_ordinal_normalization():
    # Regression (PR #812 review): folding ("first", "layer") to the
    # opaque token "firstlayer" ran before number parsing, so the word
    # form stopped equating with the digit form — "first layer" scored
    # 0 against "1st layer" and /print-again "1st layer" no longer
    # offered an archived "First Layer Calibration". The joined token
    # still equates: canonical() splits a leading number word back out
    # of "firstlayer" before the number parser runs.
    assert fuzzy.canonical("first layer") == "1 layer"
    assert fuzzy.canonical("1st layer") == "1 layer"
    assert fuzzy.canonical("firstlayer") == "1 layer"
    assert fuzzy.fuzzy_score("first layer", "1st layer") >= fuzzy.FUZZY_FLOOR
    assert fuzzy.windowed_fuzzy_score(
        "1st layer", "First Layer Calibration") >= fuzzy.FUZZY_FLOOR


def test_number_forms_do_not_equate_different_numbers():
    # The canonical forms stay distinct, and a different number scores
    # strictly below the same number written the other way (83 vs 99
    # here) — the residual similarity is the shared "mark", which is
    # what suggestion-band pick-lists are for; it never resolves alone.
    assert fuzzy.canonical("mark 2") != fuzzy.canonical("mark three")
    assert fuzzy.fuzzy_score("21", "22") == 0
    assert fuzzy.fuzzy_score("mark 2", "Mark Three") < fuzzy.fuzzy_score("mark 2", "Mark Two")


# ----------------------------------------------------- canonical: variants

@pytest.mark.parametrize("query,target", [
    ("cfg", "config"),
    ("config", "configuration file"),
    ("var", "variable"),
    ("vars", "variables"),
    ("param", "parameter"),
    ("params", "parameters"),
    ("dim", "dimension"),
    ("dims", "dimensions"),
    ("accel", "acceleration"),
    ("fw", "firmware"),
    ("sw", "software"),
    ("calib", "calibration"),
    ("diag", "diagnostic"),
    ("temp", "temperature"),
    ("temperature", "temp"),
    ("12 millimeter", "12 mm"),
    ("millimeters", "mm"),
    ("centimeter", "cm"),
    ("centimetre", "cm"),
    ("meter", "m"),
    ("metre", "m"),
    ("micron", "um"),
    ("micrometer", "um"),
    ("inches", "in"),
    ("kilogram", "kg"),
    ("grams", "g"),
    ("seconds", "s"),
    ("10 sec", "10 s"),
    ("minute", "min"),
    ("hour", "hr"),
    ("celsius", "c"),
    ("degrees", "deg"),
    ("percent", "pct"),
    ("hot end", "hotend"),
    ("cold end", "coldend"),
    ("z offset", "zoffset"),
    ("build plate", "buildplate"),
    ("heat bed", "heatbed"),
    ("heated bed", "heatbed"),
    ("print bed", "printbed"),
    ("lead screw", "leadscrew"),
    ("bowden tube", "bowdentube"),
    ("input shaper", "inputshaper"),
    ("pressure advance", "pressureadvance"),
    ("layer height", "layerheight"),
    ("flow rate", "flowrate"),
    ("fan speed", "fanspeed"),
    ("cold pull", "coldpull"),
    ("spool holder", "spoolholder"),
    ("bed mesh", "bedmesh"),
    ("run out", "runout"),
    ("time lapse", "timelapse"),
    ("power supply", "psu"),
    ("under extrusion", "underextrusion"),
    ("nozzles", "nozzle"),
    ("colour", "color"),
    ("colours", "colors"),
    ("grey", "gray"),
    ("Silk Grey", "silk gray"),
    ("aluminium", "aluminum"),
    ("fibre", "fiber"),
    ("carbon fibre", "carbon fiber"),
    ("centre", "center"),
    ("calibre", "caliber"),
    ("matt", "matte"),
    ("matt black", "matte black"),
    ("customise", "customize"),
    ("optimise", "optimize"),
    ("behaviour", "behavior"),
    ("favourite", "favorite"),
    ("polyamide", "nylon"),
    ("polycarbonate", "pc"),
    ("orca slicer", "orcaslicer"),
    ("bambu studio", "bambustudio"),
    ("bambu lab", "bambulab"),
    ("prusa slicer", "prusaslicer"),
    ("creality cloud", "crealitycloud"),
    ("flash forge", "flashforge"),
    ("home assistant", "homeassistant"),
    ("web ui", "webui"),
    ("mmf", "myminifactory"),
    ("cults", "cults3d"),
    ("octo", "octoprint"),
    ("z seam", "zseam"),
    ("tree support", "treesupport"),
    ("bed leveling", "bedleveling"),
    ("direct drive", "directdrive"),
    ("thermal runaway", "thermalrunaway"),
    ("layer shift", "layershift"),
    ("heat creep", "heatcreep"),
    ("bed adhesion", "bedadhesion"),
    ("shut down", "shutdown"),
    ("slicing", "slice"),
    ("sliced", "slice"),
    ("stringy", "stringing"),
    ("oozing", "ooze"),
    ("delaminated", "delamination"),
    ("golden", "gold"),
    ("boxes", "box"),
    ("warped", "warp"),
    ("leveling", "level"),
    ("heating", "heat"),
    ("jammed", "jam"),
])
def test_variant_forms_match(query, target):
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR


def test_distinct_concepts_are_not_merged():
    # Brim/skirt/raft and nozzle/hotend/extruder are different things, not
    # variants — the canonical fold must not equate them.
    assert fuzzy.canonical("brim") != fuzzy.canonical("skirt")
    assert fuzzy.canonical("skirt") != fuzzy.canonical("raft")
    assert fuzzy.canonical("nozzle") != fuzzy.canonical("hotend")
    assert fuzzy.canonical("hotend") != fuzzy.canonical("extruder")
    assert fuzzy.canonical("petg") != fuzzy.canonical("pctg")
    assert fuzzy.canonical("asa") != fuzzy.canonical("abs")
    assert fuzzy.canonical("tpe") != fuzzy.canonical("tpu")
    # And the abbreviation fold must not rescue the pinned false friend:
    # "temp" (→ temperature) against "temple" scored 80 before, 0 now.
    assert fuzzy.fuzzy_score("temp", "temple") == 0
    assert fuzzy.fuzzy_score("var", "vat") == 0


# ------------------------------------------------------- canonical: typos

@pytest.mark.parametrize("query,target", [
    # doubled / dropped letters
    ("nozzlle", "nozzle"), ("adheesion", "adhesion"), ("suppport", "support"),
    ("nozzel", "nozzle"), ("calbration", "calibration"), ("temperture", "temperature"),
    # ie/ei transposition
    ("recieve", "receive"), ("hieght", "height"), ("wieght", "weight"),
    # silent-letter drops
    ("nozel", "nozzle"), ("lisen", "listen"),
    # missing/extra spaces and hyphenation
    ("bedleveling", "bed leveling"), ("firstlayer", "first layer"),
])
def test_misspelling_classes_match(query, target):
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR


@pytest.mark.parametrize("query,target", [
    ("enuf", "enough"),               # pure sound spelling: plain pass scores 0
    ("retrakshun", "retraction"),
    ("adhezhun", "adhesion"),
    ("wat", "what"),
    ("lite", "light"),
    ("brite", "bright"),
    ("clok", "clock"),
    ("wring", "ring"),
])
def test_phonetic_rescue_lifts_sound_spellings(query, target):
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR


def test_phonetic_rescue_never_lifts_false_friends():
    # The rescue pass runs only when the plain pass scored 0, and the
    # pinned unrelated pairs must stay 0 through it.
    for query, target in [("pizza", "prusa mk4s"), ("weather", "heat bed"),
                          ("banana", "bambu lab a1"), ("fan", "first layer"),
                          ("warp drive calibration", "probe calibrate")]:
        assert fuzzy.fuzzy_score(query, target) == 0


# ------------------------------------------------------------------ policy

def test_decide_grid():
    none = fuzzy.decide([])
    assert (none.kind, none.best, none.contenders) == ("none", None, ())
    # Below the floor is not a candidate at all.
    assert fuzzy.decide([(0, "a"), (fuzzy.FUZZY_FLOOR - 1, "b")]).kind == "none"
    # High confidence with a clear margin → direct.
    direct = fuzzy.decide([(99, "a"), (70, "b")])
    assert (direct.kind, direct.best, direct.contenders) == ("direct", "a", ("a",))
    # Exactly the margin below is still clear (gap >= CLEAR_MARGIN).
    assert fuzzy.decide([(85, "a"), (75, "b")]).kind == "direct"
    # Grey zone (floor..clear) with a single contender → suggest.
    grey = fuzzy.decide([(70, "a")])
    assert (grey.kind, grey.best, grey.contenders) == ("suggest", "a", ("a",))
    # High score but a close runner-up → conflict, both contenders listed.
    close = fuzzy.decide([(90, "a"), (85, "b")])
    assert (close.kind, close.contenders) == ("suggest", ("a", "b"))
    # A tie is the extreme conflict and is never direct.
    tie = fuzzy.decide([(99, "a"), (99, "b")])
    assert (tie.kind, tie.contenders) == ("suggest", ("a", "b"))
    # Ranking is stable: equal scores keep caller order (no coin flips).
    assert fuzzy.decide([(80, "b"), (80, "a")]).contenders == ("b", "a")


# --------------------------------------------------- expanded vocabulary (1,000+)

def test_expanded_vocabulary_scale_and_integrity():
    total_entries = len(fuzzy_vocabulary.PHRASE_FORMS) + len(fuzzy_vocabulary.TOKEN_FORMS)
    assert total_entries >= 1000, f"Expected at least 1,000 entries, found {total_entries}"
    # Verify all phrase keys are lowercased tuples of non-empty strings
    for (w1, w2), targets in fuzzy_vocabulary.PHRASE_FORMS.items():
        assert w1 == w1.lower() and w1.strip() == w1 and w1
        assert w2 == w2.lower() and w2.strip() == w2 and w2
        assert isinstance(targets, tuple) and len(targets) >= 1
    # Verify all token keys are lowercased strings
    for k, v in fuzzy_vocabulary.TOKEN_FORMS.items():
        assert k == k.lower() and k.strip() == k and k
        assert isinstance(v, str) and v.strip() == v and v


@pytest.mark.parametrize("query,target", [
    # Brands and printer lines
    ("bamboo lab", "bambu"),
    ("bambu lab", "bambu"),
    ("ender3v2", "ender 3 v2"),
    ("k1max", "k1 max"),
    ("coreone", "core one"),
    ("prusaxl", "prusa xl"),
    ("neptune4pro", "neptune 4 pro"),
    ("saturn3", "saturn 3"),
    ("kobra2", "kobra 2"),
    ("sv08", "sv08"),
    ("q1pro", "q1 pro"),
    # Materials and filament brands
    ("colorfabb", "colorfabb"),
    ("polyflex", "polyflex"),
    ("polymax", "polymax"),
    ("toughpla", "tough pla"),
    ("silkpla", "silk pla"),
    ("carbonfiber", "carbon fiber"),
    # Common mobile autocorrects and fat-finger typos
    ("rebot", "reboot"),
    ("restrt", "restart"),
    ("resum", "resume"),
    ("cancle", "cancel"),
    ("pauze", "pause"),
    ("staus", "status"),
    ("slise", "slice"),
    ("halp", "help"),
    ("camra", "camera"),
    ("disply", "display"),
    ("connet", "connect"),
    # Mechanical and slicing phrases
    ("all metal", "allmetal"),
    ("hardened steel", "hardenedsteel"),
    ("direct drive", "directdrive"),
    ("anti backlash", "antibacklash"),
    ("treesupport", "tree support"),
    ("bed temp", "bedtemperature"),
    ("infilloverlap", "infill overlap"),
])
def test_expanded_vocabulary_matches(query, target):
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR



# ------------------------------------------- expanded vocabulary: review guards

@pytest.mark.parametrize("query,target", [
    ("two trees", "2 trees"),
    ("two trees", "second tree"),
    ("first layer", "1st layer"),
])
def test_vocabulary_never_breaks_number_equivalence(query, target):
    # A phrase containing a number word, joined before number parsing,
    # would turn "two trees" into an opaque token that "2 trees" never reaches.
    assert fuzzy.canonical(query) == fuzzy.canonical(target)
    assert fuzzy.fuzzy_score(query, target) >= fuzzy.FUZZY_FLOOR


def test_no_vocabulary_phrase_contains_a_number_word():
    for pair in fuzzy_vocabulary.PHRASE_FORMS:
        assert not set(pair) & set(fuzzy._NUMBER_WORDS), pair


def test_every_vocabulary_key_is_reachable_after_norm():
    # norm() turns '-' and '_' into spaces, so such keys could never match.
    for key in fuzzy_vocabulary.TOKEN_FORMS:
        assert fuzzy.norm(key) == key, key
    for pair in fuzzy_vocabulary.PHRASE_FORMS:
        assert all(fuzzy.norm(word) == word for word in pair), pair


@pytest.mark.parametrize("word,folded,phrase,target", [
    # Bamboo is a filament and a print subject, not a misspelled Bambu.
    ("bamboo", "bambu", "bamboo planter", "bambu lab a1"),
    # Statues are print subjects, not a misspelled status.
    ("statue", "status", "statue of david", "printer status"),
])
def test_common_words_are_not_folded_into_brands_or_commands(word, folded, phrase, target):
    assert fuzzy.canonical(word) != fuzzy.canonical(folded)
    assert fuzzy.fuzzy_score(phrase, target) < fuzzy.FUZZY_FLOOR


@pytest.mark.parametrize("spaced,joined", [
    ("thermal runaway", "thermalrunaway"), ("bed adhesion", "bedadhesion"),
    ("shut down", "shutdown"), ("creality print", "crealityprint"),
    ("creality cloud", "crealitycloud"), ("flash print", "flashprint"),
    ("home assistant", "homeassistant"), ("web ui", "webui"),
])
def test_compound_phrases_keep_their_joined_form(spaced, joined):
    assert fuzzy.canonical(spaced) == fuzzy.canonical(joined)


def test_sensorless_homing_typo_still_finds_the_archived_print():
    # Codex on #855: 'sensorless' -> 'sensorless homing' was expanded twice by the windowed
    # scorer while the target folded to one token, so this archive match fell from 90 to 0.
    assert fuzzy.windowed_fuzzy_score("sensorless homming", "Sensorless Homing Test") >= 80
