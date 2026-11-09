"""The core prompt registry: the clarify gateway API shape, owned by Print Handy."""
import threading

from print_handy.core import prompts


def test_answer_reaches_the_waiter_once():
    prompts.register("a1", "s", "Q?", ["Yes", "No"])
    assert prompts.is_open("a1") and prompts.choices("a1") == ["Yes", "No"] and prompts.session_key("a1") == "s"
    got = []
    worker = threading.Thread(target=lambda: got.append(prompts.wait_for_response("a1", 5)))
    worker.start()
    assert prompts.resolve_gateway_clarify("a1", "Yes") is True
    assert prompts.resolve_gateway_clarify("a1", "No") is False  # a second click changes nothing
    worker.join(5)
    assert got == ["Yes"]
    assert not prompts.is_open("a1") and prompts.choices("a1") is None


def test_timeout_closes_the_prompt_and_a_late_click_finds_nothing():
    prompts.register("a2", "s", "Q?", ["Yes"])
    assert prompts.wait_for_response("a2", 0.01) is None
    assert prompts.resolve_gateway_clarify("a2", "Yes") is False


def test_a_reserved_answer_survives_the_wait_timeout_until_it_commits():
    """P2-166: reserving records the answer before slow network I/O (the
    Matrix close-out note). The waiter's timeout must not discard a reserved
    prompt: the wait holds until the reserver commits with the same answer.
    First answer wins — a second reservation or a different resolve fails."""
    import time

    prompts.register("r1", "s", "Q?", ["Yes", "No"])
    assert prompts.reserve_gateway_clarify("r1", "Yes") is True
    assert prompts.reserve_gateway_clarify("r1", "No") is False
    assert prompts.is_open("r1")  # reserved, not yet resolved: still the open prompt
    got = []
    worker = threading.Thread(target=lambda: got.append(prompts.wait_for_response("r1", 0.2)))
    worker.start()
    time.sleep(0.5)  # the nominal timeout passes while the note is posting
    assert worker.is_alive(), "the wait discarded a reserved prompt at its timeout"
    assert prompts.resolve_gateway_clarify("r1", "No") is False  # not the reserved answer
    assert prompts.resolve_gateway_clarify("r1", "Yes") is True  # the reserver commits
    worker.join(5)
    assert got == ["Yes"]
    assert not prompts.is_open("r1")


def test_cleanup_pattern_of_the_intake_leaves_nothing_behind():
    """ask(): resolve CANCELLED then wait 1 ms, after an answer or after a failed send."""
    before = prompts.open_count()
    prompts.register("a3", "s", "Q?", ["Yes"])
    prompts.resolve_gateway_clarify("a3", prompts.CANCELLED)
    assert prompts.wait_for_response("a3", 0.001) == prompts.CANCELLED
    assert prompts.resolve_gateway_clarify("a3", prompts.CANCELLED) is False
    assert prompts.wait_for_response("a3", 0.001) is None
    assert prompts.open_count() == before


def test_cancel_session_only_touches_that_session():
    prompts.register("b1", "mine", "Q?", ["Yes"])
    prompts.register("b2", "other", "Q?", ["Yes"])
    assert prompts.cancel_session("mine") == 1
    assert prompts.wait_for_response("b1", 0.01) == prompts.CANCELLED
    assert prompts.is_open("b2")
    prompts.resolve_gateway_clarify("b2", prompts.CANCELLED)
    prompts.wait_for_response("b2", 0.001)


def test_open_prompt_for_finds_the_newest_open_prompt_of_the_session():
    prompts.register("c1", "mine", "Q1?", ["Yes"])
    prompts.register("c2", "other", "Q2?", ["Yes"])
    prompts.register("c3", "mine", "Q3?", ["Yes"])
    assert prompts.open_prompt_for("mine") == "c3"
    assert prompts.open_prompt_for("other") == "c2"
    assert prompts.open_prompt_for("nobody") is None
    # An answered prompt stops being the session's open one.
    assert prompts.resolve_gateway_clarify("c3", "Yes")
    assert prompts.open_prompt_for("mine") == "c1"
    assert prompts.wait_for_response("c3", 0.01) == "Yes"
    prompts.resolve_gateway_clarify("c1", prompts.CANCELLED)
    prompts.resolve_gateway_clarify("c2", prompts.CANCELLED)
    prompts.wait_for_response("c1", 0.001)
    prompts.wait_for_response("c2", 0.001)
    assert prompts.open_prompt_for("mine") is None


def test_gateway_is_always_the_core_registry():
    """One runtime left: gateway() returns this module, even with a
    host-shaped tools.clarify_gateway importable."""
    import sys
    import types
    hermes = types.ModuleType("tools.clarify_gateway")
    package = types.ModuleType("tools")
    package.clarify_gateway = hermes
    saved = {k: sys.modules.get(k) for k in ("tools", "tools.clarify_gateway")}
    sys.modules["tools"] = package
    sys.modules["tools.clarify_gateway"] = hermes
    try:
        assert prompts.gateway() is prompts
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
