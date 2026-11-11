"""The dispatch table: command hooks are classified, free-text hooks always run (#1153 item 10).

The Router skips a hook whose ``dispatch_gate`` fails for the message text.
That is only sound if the gate fails exactly when the hook would return None
without reading anything else. These tests hold every gate to its hook:
with a failing gate the real hook returns None and never uses the event's
source or the gateway; with a passing gate the hook goes on to use them. And the Router's outcome (the answer and the hooks that ran) is the
same as running every hook in order.
"""
from __future__ import annotations

import asyncio

import pytest

from print_handy.bot.router import Router, message_text

FREE_TEXT_HOOKS = {
    "guided_dispatch", "design_dispatch", "typed_quantity_dispatch", "upload_wait_dispatch",
    "model_link_dispatch", "runout_intake_dispatch", "roast_dispatch", "print_again_dispatch",
    "edit_dispatch", "loaded_spools_dispatch", "purchase_dispatch", "spool_dispatch",
    "print_reboot_dispatch",
}


class Touched(Exception):
    pass


class _Exploding:
    """Any attribute read means the hook went past its text check."""

    def __getattr__(self, name):
        raise Touched(name)


class _Event:
    """A message whose source and gateway explode on use (fetching ``event.source`` itself is harmless)."""

    def __init__(self, text):
        self.text = text
        self.source = _Exploding()


@pytest.fixture(scope="module")
def plugin():
    return Router().plugin


def _commands(plugin):
    return sorted({hook.dispatch_gate.command for hook in plugin._DISPATCH_HOOKS
                   if getattr(hook, "dispatch_gate", None)})


def _texts(plugin):
    texts = ["", "hello there", "print 57", "/", "/print", "print-stop", "  ", "/print-colors"]
    for command in _commands(plugin):
        texts += [command, f"{command} arg", f"  {command}  ", f"{command}\targ", f"{command}\narg",
                  f"{command}x", f"{command}-extra", command.upper(), f"x {command}"]
    texts += ["/print-colors /tmp/JOB.json", "/print-colors  spaced", "/print-next 1 2", "/print-stop now"]
    return texts


async def _run_hook(hook, text):
    try:
        return "returned", await hook(_Event(text), _Exploding())
    except Touched:
        return "touched", None


def test_every_dispatch_hook_is_classified(plugin):
    names = [hook.__name__ for hook in plugin._DISPATCH_HOOKS]
    gated = {hook.__name__ for hook in plugin._DISPATCH_HOOKS if getattr(hook, "dispatch_gate", None)}
    assert len(names) == len(set(names)) == 30
    assert gated | FREE_TEXT_HOOKS == set(names) and not gated & FREE_TEXT_HOOKS
    assert len(gated) == 17


GATED = sorted(h.__name__ for h in Router().plugin._DISPATCH_HOOKS if getattr(h, "dispatch_gate", None))


@pytest.mark.parametrize("name", GATED)
def test_a_gate_fails_exactly_when_its_hook_declines_without_looking(plugin, name):
    hook = next(h for h in plugin._DISPATCH_HOOKS if h.__name__ == name)
    texts = _texts(plugin) + [None]

    async def run_all():
        return [await _run_hook(hook, text) for text in texts]

    for text, (outcome, value) in zip(texts, asyncio.run(run_all())):
        if hook.dispatch_gate(message_text(_Event(text))):
            assert outcome == "touched", (name, text, value)
        else:
            assert (outcome, value) == ("returned", None), (name, text)


class _Hook:
    def __init__(self, name, answer=None, gate=None, calls=None):
        self.__name__, self.answer, self.calls = name, answer, calls
        if gate is not None:
            self.dispatch_gate = gate

    def __call__(self, event, gateway):
        self.calls.append(self.__name__)
        return self.answer(event.text) if callable(self.answer) else self.answer


class _Plugin:
    def __init__(self, hooks):
        self.hooks = hooks

    def register(self, ctx):
        for hook in self.hooks:
            ctx.register_hook("pre_gateway_dispatch", hook)


def _ungated(router, event):
    """The pre-#1153 loop: every hook in order, first answer wins."""
    for hook in router.dispatch_hooks:
        result = hook(event, None)
        if result is not None:
            return result
    return None


@pytest.mark.parametrize("text", ["hello", "/a", "/a x", "/b", "/bb", " /b ", "/c"])
def test_router_outcome_matches_running_every_hook(text):
    calls = []
    gate_a = lambda t: t == "/a" or t.startswith("/a ")  # noqa: E731
    gate_b = lambda t: t == "/b"  # noqa: E731
    hooks = [
        _Hook("free_first", calls=calls),
        _Hook("a", answer=lambda t: {"hook": "a"} if gate_a(t.strip()) else None, gate=gate_a, calls=calls),
        _Hook("free_middle", answer=lambda t: {"hook": "free"} if t == "/c" else None, calls=calls),
        _Hook("b", answer=lambda t: {"hook": "b"} if gate_b(t.strip()) else None, gate=gate_b, calls=calls),
        _Hook("free_last", calls=calls),
    ]
    router = Router(plugin=_Plugin(hooks))
    expected = _ungated(router, _Event(text))
    ungated_calls, calls[:] = list(calls), []
    assert asyncio.run(router.dispatch(_Event(text), None)) == expected
    # The same hooks answered or declined; the gated ones were skipped only when they declined.
    assert [c for c in ungated_calls if c not in calls] == [
        h.__name__ for h in hooks
        if getattr(h, "dispatch_gate", None) and not h.dispatch_gate(text.strip()) and h.__name__ in ungated_calls]
