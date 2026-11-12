"""The ``match`` predicates the chat handlers declare (#1323).

A command handler answers only its own slash command: its ``match`` is the
dispatch gate the Router tests once per message (#1153 item 10), and the
handler's first statement tests the text the same way. A free-text handler
(typed replies, links, plain-language asks, open prompts) matches every
message and always runs.
"""
from .common import _command_matches


def command_gate(command, with_args):
    """Match ``command`` alone, or with arguments when ``with_args``."""
    def gate(text):
        return _command_matches(text, command, with_args)
    gate.command = command
    gate.with_args = with_args
    return gate


def prefix_gate(prefix, command):
    """Match a command that always carries arguments (``/print-colors <...>``)."""
    def gate(text):
        return text.startswith(prefix)
    gate.command = command
    return gate


def free_text(text):
    """Free-text handlers look at every message."""
    return True
