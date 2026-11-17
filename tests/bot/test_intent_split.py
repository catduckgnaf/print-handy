"""The buy / bought / add / load / how-much intent split (#1524 tranche 2).

These five phrases used to collapse: buying phrases suggested the guided
/spool creation flow, so "buy filament" could start creating inventory
and "I bought filament" was treated like an instruction. They are now
distinct: buying is guidance (a lookup, never creation), a past-tense
purchase gets no command suggestion at all, adding is guided creation,
loading is the guided change flow, and "how much filament" is an
inventory question. The audit half extends the suggestion-target
contract: every target's argument shape must fit its command — a second
word must be a registered subcommand word (or a handler verb), and a
{placeholder} target must be fillable.
"""
import pytest

from print_handy.bot import commands, runtime

SUGGEST = runtime.command_suggestion


@pytest.mark.parametrize("text", [
    "buy filament", "buy more filament", "order PLA", "purchase spools",
    "where can I buy filament?", "where do I get more spools?",
    "I need more filament", "I want more PLA",
])
def test_buying_is_guidance_never_spool_creation(text):
    suggestion = SUGGEST(text)
    assert suggestion is None or not suggestion.startswith("/spool"), (
        f"{text!r} suggested {suggestion!r}: buying must not route to guided /spool creation")


@pytest.mark.parametrize("text", [
    "I bought filament", "I bought purple PLA", "I bought more PLA",
    "I just bought two spools",
])
def test_bought_is_a_past_purchase_not_an_instruction(text):
    suggestion = SUGGEST(text)
    assert suggestion is None or not suggestion.startswith("/spool"), (
        f"{text!r} suggested {suggestion!r}: a past purchase must never create or load inventory")


@pytest.mark.parametrize("text,expected", [
    ("add a new spool", "/spool new"),
    ("new spool", "/spool new"),
    ("add a spool", "/spool"),
    ("load a spool", "/spool change"),
    ("load the filament", "/spool change"),
    ("load spool 12 into slot 2", "/spool change"),
    ("how much filament do I have?", "/spool list"),
    ("list spools", "/spool list"),
])
def test_add_load_and_how_much_are_distinct(text, expected):
    assert SUGGEST(text) == expected


def test_the_five_intents_do_not_collapse():
    targets = {SUGGEST(text) for text in (
        "where can I buy filament?", "add a new spool", "load a spool",
        "how much filament do I have?")}
    assert len(targets) == 4


# ---------------------------------------------------------------- audit

# Second words a handler accepts beyond the registered subcommand words:
# /print-firmware's guide flavors and /print-troubleshoot's topics are
# arguments, not subcommands, and /print-lookup takes free text.
_FREE_SECOND_WORD = {"/print-lookup", "/print-design", "/print-firmware guide",
                     "/print-troubleshoot"}


def _filled(target):
    return target.replace("{q}", "thermal runaway").replace("{c}", "zmod")


def test_every_suggestion_target_argument_shape():
    """Each target in the table: registered head, and — when the command
    has registered subcommands and the target carries a second word —
    that word is one of them (or a documented free-argument form)."""
    problems = []
    for _pattern, target in runtime._TASK_INTENT_SUGGESTIONS:
        text = _filled(target)
        parts = text.split()
        head = parts[0]
        command = commands.by_name(head[1:])
        if command is None:
            problems.append(f"{target!r}: {head} is not registered")
            continue
        if len(parts) > 1 and command.subcommands:
            words = {sub.word for sub in command.subcommands if sub.word}
            if parts[1] not in words and not any(
                    text.startswith(prefix) for prefix in _FREE_SECOND_WORD):
                problems.append(f"{target!r}: {parts[1]!r} is not a subcommand of {head}")
    assert problems == []
