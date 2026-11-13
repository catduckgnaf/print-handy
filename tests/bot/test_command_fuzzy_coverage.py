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

Expected values are the suggestion's command form. Two established
forms differ from the registry spelling and are pinned as they are:
calibration phrases suggest the legacy ``/print-calibrate`` and the
nozzle phrases suggest ``/nozzle change``.
"""
import pytest

from print_handy.bot import commands, runtime
from print_handy.core.fuzzy_vocabulary import TYPO_FORMS

COMMAND_PHRASES = {
    "print-next": [("next plate", "/print-next")],
    "print-stop": [("stop the print", "/print-stop")],
    "print-resume": [("resume the print", "/print-resume")],
    "print-again": [("print again", "/print-again")],
    "print-reboot": [("reboot the printer", "/print-reboot")],
    "print-coldpull": [("do a cold pull", "/print-coldpull")],
    "print-bed-down": [("prepare to clean", "/print-bed-down"),
                       ("lower the bed", "/print-bed-down")],
    "nozzle": [("change the nozzle", "/nozzle change")],
    "print-maintenance": [("run maintenance", "/print-maintenance"),
                          ("what maintenance is due", "/print-maintenance")],
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
                       ("upgrade moonraker", "/print-firmware update moonraker")],
    "print-calibration": [("bed leveling", "/print-calibrate"),
                          ("calibrate the bed", "/print-calibrate")],
    "print-live": [("start the live camera", "/print-live"),
                   ("watch the print live", "/print-live")],
    "print-plate-calibrate": [("calibrate the plate", "/print-plate-calibrate"),
                              ("capture the plate reference", "/print-plate-calibrate")],
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
