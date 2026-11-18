"""Every registered command has a fuzzy equivalent — the coverage guard.

The rule (catduck, 2026-11-11): when a command is added, its fuzzy
equivalent ships with it, not after it. A fuzzy equivalent here means
plain-language phrases a person would actually type in chat suggest the
command through the deterministic suggestion layer
(``runtime.command_suggestion`` — no LLM involved), and those phrases
keep suggesting it when a word in them is typed with a known corpus
typo.

This suite enumerates ``commands.COMMANDS`` itself, so a new command
with no entry in ``COMMAND_PHRASES`` below fails here until its phrases
exist in ``runtime._TASK_INTENT_SUGGESTIONS`` and are pinned here. The
table is the audit of every current command, kept honest by the two
routing tests: an entry whose phrases stop routing fails, and a phrase
whose corpus-typo variant stops routing fails.

Since #1496 the same rule covers subcommands: fuzzy stays parallel
with the whole command table, not just its top level. A second table,
``SUBCOMMAND_PHRASES``, is enumerated mechanically from
``Command.subcommands`` — every subcommand with a typed word (the bare
forms, whose typed word is empty, have no word to phrase or mistype)
needs pinned phrases suggesting its typed form, and every typed word
gets a generated near-miss case (one adjacent-letter swap) that
``runtime.subcommand_suggestion`` must correct back. A new subcommand
without fuzzy phrases fails here until they ship with it.

Expected values are the suggestion's command form. One established
form differs from the bare registry name and is pinned as it is: the
nozzle phrases suggest ``/nozzle change``. Every suggested command is a
registered one (#1524, tests/bot/test_suggestion_targets.py).
"""
import pytest

from print_handy.bot import commands, runtime
from print_handy.core.fuzzy_vocabulary import TYPO_FORMS

COMMAND_PHRASES = {
    "print-next": [("next plate", "/print-next")],
    "print-stop": [("stop the print", "/print-stop")],
    "print-pause": [("pause the print", "/print-pause")],
    "print-resume": [("resume the print", "/print-resume")],
    "print-status": [("how is the print doing", "/print-status"),
                     ("how long left", "/print-status"),
                     ("printer status", "/print-status"),
                     ("what layer is it on", "/print-status")],
    "print-again": [("print again", "/print-again")],
    "print-reboot": [("reboot the printer", "/print-reboot")],
    "print-coldpull": [("do a cold pull", "/print-coldpull")],
    "print-bed-down": [("prepare to clean", "/print-bed-down"),
                       ("lower the bed", "/print-bed-down")],
    "nozzle": [("change the nozzle", "/nozzle change")],
    "print-maintenance": [("run maintenance", "/print-maintenance"),
                          ("what maintenance is due", "/print-maintenance"),
                          ("quick maintenance", "/print-maintenance quick")],
    "print-slice-result": [("show the slicing result", "/print-slice-result"),
                           ("how much filament will it use", "/print-slice-result")],
    "print-colors": [("choose the colors for this print", "/print-colors"),
                     ("pick colors for the job", "/print-colors")],
    "spool": [("change the filament", "/spool"),
              ("change spool", "/spool"),
              ("fix the spool weight", "/spool weight"),
              ("the spool weight is wrong", "/spool weight")],
    "print-spools": [("what filaments are loaded", "/print-spools"),
                     ("running low on filament", "/print-spools")],
    "print-lookup": [("what does thermal runaway mean", "/print-lookup"),
                     ("thermal runaway", "/print-lookup")],
    "print-troubleshoot": [("the nozzle is clogged", "/print-troubleshoot clog"),
                           ("heat creep", "/print-troubleshoot heat-creep")],
    "print-macros": [("list the macros", "/print-macros"),
                     ("show me the macros", "/print-macros")],
    "print-firmware": [("update the firmware", "/print-firmware"),
                       ("can you check for firmware updates", "/print-firmware"),
                       ("can you check for zmod updates?", "/print-firmware"),
                       ("check for helixscreen updates", "/print-firmware"),
                       ("check for guppy updates", "/print-firmware"),
                       ("is there a helix update", "/print-firmware"),
                       ("helix update", "/print-firmware"),
                       ("update zmod", "/print-firmware update zmod"),
                       ("update z-mod", "/print-firmware update zmod"),
                       ("update helix", "/print-firmware update helixscreen"),
                       ("update helix screen", "/print-firmware update helixscreen"),
                       ("update helixscreen", "/print-firmware update helixscreen"),
                       ("update guppy", "/print-firmware update guppyscreen"),
                       ("update guppy screen", "/print-firmware update guppyscreen"),
                       ("update klipper", "/print-firmware update klipper"),
                       ("upgrade moonraker", "/print-firmware update moonraker"),
                       ("check for forge-x updates", "/print-firmware"),
                       ("is there a new opencreator release", "/print-firmware"),
                       ("forgex update", "/print-firmware"),
                       ("open creator version", "/print-firmware"),
                       ("show the firmware feed", "/print-firmware")],
    "print-calibration": [("bed leveling", "/print-calibration"),
                          ("calibrate the bed", "/print-calibration")],
    "print-live": [("start the live camera", "/print-live"),
                   ("watch the print live", "/print-live"),
                   ("stop the live camera", "/print-live stop"),
                   ("live camera status", "/print-live status")],
    "print-plate-calibrate": [("calibrate the plate", "/print-plate-calibrate"),
                              ("capture the plate reference", "/print-plate-calibrate"),
                              ("capture the plate", "/print-plate-calibrate")],
    "print-design": [("design a bracket", "/print-design")],
    "print-handy": [("add a printer", "/print-handy add-printer"),
                    ("set up print handy", "/print-handy setup")],
    "print-source": [("what sites can you print from", "/print-source")],
    "print-help": [("what can you do", "/print-help"),
                   ("list the commands", "/print-help")],
}

# Reverse index of the typo corpus: correct word -> one known typo of it.
_TYPO_OF = {}
for _typo, _word in sorted(TYPO_FORMS.items()):
    _TYPO_OF.setdefault(_word, _typo)


def _cases():
    return [(name, phrase, expected)
            for name, entries in sorted(COMMAND_PHRASES.items())
            for phrase, expected in entries]


def test_every_registered_command_has_fuzzy_phrases():
    registered = {c.name for c in commands.COMMANDS}
    assert registered == set(COMMAND_PHRASES), (
        "commands without fuzzy phrases: "
        f"{sorted(registered - set(COMMAND_PHRASES))}; "
        "stale entries: "
        f"{sorted(set(COMMAND_PHRASES) - registered)}")


@pytest.mark.parametrize("name,phrase,expected", _cases())
def test_command_phrases_suggest_their_command(name, phrase, expected):
    found = runtime.command_suggestion(phrase)
    assert found is not None and found.startswith(expected), (
        f"{phrase!r} suggested {found!r}, expected {expected!r} for {name}")


def _typo_cases():
    cases = []
    for name, phrase, expected in _cases():
        words = phrase.strip("?").split()
        for i, word in enumerate(words):
            typo = _TYPO_OF.get(word)
            if typo:
                mutated = " ".join(words[:i] + [typo] + words[i + 1:])
                cases.append((name, mutated, expected.split()[0]))
    return cases


@pytest.mark.parametrize("name,phrase,base", _typo_cases())
def test_command_phrases_survive_corpus_typos(name, phrase, base):
    found = runtime.command_suggestion(phrase)
    assert found is not None and found.startswith(base), (
        f"typo phrase {phrase!r} suggested {found!r}, expected {base!r} for {name}")


# The firmware phrases pick a form, not just the command: a question
# ("check for", "is there", "X update") gets the read-only report, an
# instruction ("update X") the update form for that component, its name
# through firmware_update's alias table. Either way the phrase only
# offers the did-you-mean prompt and the command asks before it sends.
@pytest.mark.parametrize("phrase,expected", COMMAND_PHRASES["print-firmware"])
def test_firmware_phrases_pick_the_report_or_the_update_form(phrase, expected):
    assert runtime.command_suggestion(phrase) == expected


# ----------------------------------------------- subcommands (#1496)
# Plain-language phrases per typed subcommand word, keyed by command
# then by the subcommand's typed word. The bare forms (guided spool,
# macros list, firmware versions) carry an empty typed word: they are
# the command's own bare form, already covered by COMMAND_PHRASES, and
# have no second word to phrase or mistype.
SUBCOMMAND_PHRASES = {
    "spool": {
        "list": [("list the spools", "/spool list"),
                 ("show the spools", "/spool list")],
        "change": [("change spool", "/spool")],
        "new": [("add a new spool", "/spool new"),
                ("new spool", "/spool new")],
        "weight": [("fix the spool weight", "/spool weight"),
                   ("the spool weight is wrong", "/spool weight")],
        "shelve": [("put the spool back on the shelf", "/spool shelve"),
                   ("unload the spool", "/spool shelve")],
        "rename": [("rename the spool", "/spool rename"),
                   ("rename a spool", "/spool rename")],
        "archive": [("archive the spool", "/spool archive"),
                    ("archive an old spool", "/spool archive")],
        "unarchive": [("unarchive the spool", "/spool unarchive"),
                      ("restore a spool", "/spool unarchive")],
        "sync": [("sync the spools", "/spool sync"),
                 ("sync spools with spoolman", "/spool sync"),
                 ("sync with spoolman", "/spool sync")],
    },
    "print-macros": {
        "run": [("run a macro", "/print-macros run"),
                ("run the macro", "/print-macros run")],
    },
    "print-firmware": {
        "update": [("update zmod", "/print-firmware update zmod")],
        "channel": [("switch helixscreen to beta",
                     "/print-firmware channel helixscreen beta"),
                    ("change the helixscreen channel to beta",
                     "/print-firmware channel helixscreen beta"),
                    ("switch moonraker to dev",
                     "/print-firmware channel moonraker dev"),
                    ("put helixscreen on stable",
                     "/print-firmware channel helixscreen stable")],
        "guide": [("firmware guide", "/print-firmware guide"),
                  ("show me the firmware guide", "/print-firmware guide")],
    },
    "print-calibration": {
        "mesh": [("bed mesh", "/print-calibration mesh"),
                 ("calibrate the bed mesh", "/print-calibration mesh")],
        "screws": [("bed screws", "/print-calibration screws"),
                   ("calibrate the bed screws", "/print-calibration screws")],
        "pid": [("pid tuning", "/print-calibration pid"),
                ("tune the pid", "/print-calibration pid")],
        "shaper": [("input shaper", "/print-calibration shaper"),
                   ("calibrate input shaper", "/print-calibration shaper")],
        "belts": [("belt tension", "/print-calibration belts"),
                  ("calibrate the belts", "/print-calibration belts")],
        "zoffset": [("z offset", "/print-calibration zoffset"),
                    ("calibrate z offset", "/print-calibration zoffset")],
        "records": [("calibration records", "/print-calibration records"),
                    ("show calibration records", "/print-calibration records")],
    },
    "nozzle": {
        "change": [("change the nozzle", "/nozzle change"),
                   ("nozzle change", "/nozzle change")],
    },
}

# The firmware versions form is a subcommand with an empty typed word,
# so it is pinned here instead: its phrases suggest the bare command,
# which is the versions report.
FIRMWARE_VERSIONS_PHRASES = [
    ("firmware versions", "/print-firmware"),
    ("show firmware versions", "/print-firmware"),
    ("what firmware version is installed", "/print-firmware"),
    ("what version is the firmware", "/print-firmware"),
]


def _typed_subcommand_words():
    """(command name, typed word) for every subcommand that has one."""
    return {(c.name, sub.word) for c in commands.COMMANDS
            for sub in c.subcommands if sub.word}


def test_every_typed_subcommand_has_fuzzy_phrases():
    pinned = {(name, word) for name, by_word in SUBCOMMAND_PHRASES.items()
              for word in by_word}
    assert _typed_subcommand_words() == pinned, (
        "subcommands without fuzzy phrases: "
        f"{sorted(_typed_subcommand_words() - pinned)}; "
        "stale entries: "
        f"{sorted(pinned - _typed_subcommand_words())}")


def _subcommand_cases():
    return [(name, word, phrase, expected)
            for name, by_word in sorted(SUBCOMMAND_PHRASES.items())
            for word, entries in sorted(by_word.items())
            for phrase, expected in entries]


@pytest.mark.parametrize("name,word,phrase,expected", _subcommand_cases())
def test_subcommand_phrases_suggest_their_form(name, word, phrase, expected):
    found = runtime.command_suggestion(phrase)
    assert found is not None and found.startswith(expected), (
        f"{phrase!r} suggested {found!r}, expected {expected!r} "
        f"for {name} {word}")


@pytest.mark.parametrize("phrase,expected", FIRMWARE_VERSIONS_PHRASES)
def test_firmware_versions_phrases_suggest_the_report(phrase, expected):
    assert runtime.command_suggestion(phrase) == expected


def _subcommand_typo_cases():
    cases = []
    for name, by_word in sorted(SUBCOMMAND_PHRASES.items()):
        for word in sorted(by_word):
            for phrase, expected in by_word[word]:
                words = phrase.strip("?").split()
                for i, token in enumerate(words):
                    typo = _TYPO_OF.get(token)
                    if typo:
                        mutated = " ".join(words[:i] + [typo] + words[i + 1:])
                        cases.append((name, mutated, expected.split()[0]))
    return cases


@pytest.mark.parametrize("name,phrase,base", _subcommand_typo_cases())
def test_subcommand_phrases_survive_corpus_typos(name, phrase, base):
    found = runtime.command_suggestion(phrase)
    assert found is not None and found.startswith(base), (
        f"typo phrase {phrase!r} suggested {found!r}, expected {base!r} for {name}")


def _swapped(word):
    """The word with its second and third letters swapped: one typo."""
    return word[0] + word[2] + word[1] + word[3:]


@pytest.mark.parametrize("name,word", sorted(_typed_subcommand_words()))
def test_subcommand_words_survive_a_letter_swap(name, word):
    typed = f"/{name} {_swapped(word)}"
    assert runtime.subcommand_suggestion(typed) == f"/{name} {word}", (
        f"{typed!r} did not correct to /{name} {word}")
    # The same correction rides the full suggestion entry point.
    assert runtime.command_suggestion(typed) == f"/{name} {word}"


@pytest.mark.parametrize("text", [
    "/spool ls",        # the handler's own alias for list
    "/spool set 1 12",  # the handler's own alias for change
    "/spool load 1 12",
    "/spool add",
    "/spool 1",         # not a word at all
    "/spool list",      # exact forms are not near misses
    "/spool change 1 12",
    "/print-firmware versions",  # the bare form's name, not a typed word
    "/print-firmware zmod",      # a component name is an argument, not a verb
    "/print-macros list",        # the bare form's name, not a typed word
    "/print-calibration extruder",  # an argument word, far from every form
    "/print-lookup clog",        # a command without subcommands
])
def test_subcommand_lookalikes_get_no_suggestion(text):
    assert runtime.subcommand_suggestion(text) is None
