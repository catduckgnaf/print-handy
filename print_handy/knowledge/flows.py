"""Guided troubleshooting flows as a pure state machine.

A flow (print_handy/knowledge/data/troubleshooting.toml) is a graph of question nodes that branch
on the operator's answers and advice nodes that state one bounded physical check.
This module only walks that graph. It imports nothing that can reach a printer,
opens no socket and runs no command: following a flow can never heat, move or
extrude anything. Acting on the advice stays with the person at the printer, or
with the existing guarded flows (for example /print-coldpull) that a step may
point to and that ask for their own confirmation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core import fuzzy


def _norm(text):
    """The shared fold — ``core.fuzzy.norm`` is the one implementation."""
    return fuzzy.norm(text)


@dataclass(frozen=True)
class Option:
    label: str
    next: str


@dataclass(frozen=True)
class Node:
    id: str
    kind: str  # "question" or "advice"
    prompt: str
    options: tuple[Option, ...] = ()
    next: str | None = None


@dataclass(frozen=True)
class Flow:
    id: str
    title: str
    aliases: tuple[str, ...] = ()
    safety: tuple[str, ...] = ()
    start: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    """What the bot shows at one point of a flow."""

    flow_id: str
    node_id: str
    kind: str  # "question", "advice", or "done"
    text: str
    choices: tuple[str, ...] = ()
    safety: tuple[str, ...] = ()
    done: bool = False


class FlowError(ValueError):
    """A flow cannot be started, or an answer does not match its question."""


class FlowSession:
    """One walk through one flow. Deterministic: same answers, same path."""

    def __init__(self, flow: Flow):
        if flow.start not in flow.nodes:
            raise FlowError(f"flow {flow.id!r} starts at unknown node {flow.start!r}")
        self.flow = flow
        self._node = flow.nodes[flow.start]
        self._done = False
        self._safety_shown = False
        self.path: list[str] = [self._node.id]

    def step(self) -> Step:
        """The current step. Safety warnings ride along exactly once, on the first step."""
        if self._done:
            return Step(self.flow.id, "", "done", "", done=True)
        safety = ()
        if not self._safety_shown:
            safety = self.flow.safety
            self._safety_shown = True
        choices = tuple(o.label for o in self._node.options) if self._node.kind == "question" else ()
        return Step(self.flow.id, self._node.id, self._node.kind, self._node.prompt,
                    choices=choices, safety=safety)

    def answer(self, choice) -> Step:
        """Answer the current question by option label (exact, then unambiguous prefix/token match)."""
        if self._done:
            raise FlowError("this troubleshooting flow has already ended")
        if self._node.kind != "question":
            raise FlowError("the current step is advice, not a question; advance it instead")
        wanted = _norm(choice)
        matches = []
        for option in self._node.options:
            label = _norm(option.label)
            if wanted == label or label.startswith(wanted) or wanted.startswith(label):
                matches.append(option)
            elif wanted and set(wanted.split()) <= set(label.split()):
                matches.append(option)
        if len(matches) != 1:
            labels = ", ".join(o.label for o in self._node.options)
            raise FlowError(f"answer {choice!r} does not pick one of: {labels}")
        return self._go(matches[0].next)

    def advance(self) -> Step:
        """Move past the current advice step; ends the flow when the advice is terminal."""
        if self._done:
            raise FlowError("this troubleshooting flow has already ended")
        if self._node.kind != "advice":
            raise FlowError("the current step is a question; answer it instead")
        if self._node.next is None:
            self._done = True
            return self.step()
        return self._go(self._node.next)

    def _go(self, node_id) -> Step:
        node = self.flow.nodes.get(node_id)
        if node is None:
            raise FlowError(f"flow {self.flow.id!r} points at unknown node {node_id!r}")
        self._node = node
        self.path.append(node.id)
        return self.step()
