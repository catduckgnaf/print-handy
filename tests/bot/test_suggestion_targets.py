"""Every plain-language suggestion leads to a command that exists (#1524).

``runtime._TASK_INTENT_SUGGESTIONS`` maps phrases to the slash command the
bot offers to run. Nothing held those targets to the command registry, so
three of them pointed nowhere: "pause the print" suggested ``/print-pause``
(never registered), bed-leveling phrases suggested ``/print-calibrate``
(the command is ``/print-calibration``), and camera phrases suggested
``/print-camera`` (no such command). A person who accepted the suggestion
got a command nothing answers.

This suite enumerates the table itself, so a new suggestion whose target
is not a registered command, or whose text its own handler gate would not
accept, fails here until the command exists.
"""
import pytest

from print_handy.bot import commands, handlers, runtime

# What a suggestion's placeholders stand for when the phrase supplies them.
PLACEHOLDERS = {"{q}": "thermal runaway", "{c}": "zmod"}

TARGETS = sorted({command for _pattern, command in runtime._TASK_INTENT_SUGGESTIONS})


def _filled(target):
    for placeholder, sample in PLACEHOLDERS.items():
        target = target.replace(placeholder, sample)
    return target


def _command_gates():
    """The handlers' own command gates, keyed by the command each one answers."""
    gates = {}
    for handler in handlers.HANDLERS:
        gate = getattr(handler, "match", None)
        if getattr(gate, "command", None) and hasattr(gate, "with_args"):
            gates[gate.command] = gate
    return gates


def test_the_table_is_not_empty():
    assert len(TARGETS) > 20


@pytest.mark.parametrize("target", TARGETS)
def test_suggestion_target_is_a_registered_command(target):
    text = _filled(target)
    assert "{" not in text, f"{target!r} carries a placeholder this test does not fill"
    head = text.split()[0]
    assert head.startswith("/"), f"{target!r} is not a slash command"
    assert commands.by_name(head[1:]) is not None, (
        f"{target!r} suggests {head}, which is not a registered command or alias")


@pytest.mark.parametrize("target", TARGETS)
def test_suggestion_target_passes_its_own_handler_gate(target):
    """A command with a strict gate must accept the exact text the suggestion would send."""
    text = _filled(target)
    head = text.split()[0]
    command = commands.by_name(head[1:])
    if command is None:
        pytest.skip("covered by test_suggestion_target_is_a_registered_command")
    gate = _command_gates().get("/" + command.name)
    if gate is None:
        pytest.skip(f"/{command.name} is answered by a free-text handler or the runtime itself")
    canonical = "/" + command.name + text[len(head):]
    assert gate(canonical), f"{target!r}: the /{command.name} handler gate refuses {canonical!r}"


def test_pause_is_registered_and_gated():
    assert commands.by_name("print-pause") is not None
    assert "/print-pause" in _command_gates()
    assert runtime.command_suggestion("pause the print") == "/print-pause"
