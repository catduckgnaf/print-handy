"""Web chat is one conversation per Web UI session, never a shared outbox (#1145).

Each Web UI session (a login session, or a browser's chat cookie on an
install with no login) gets its own scope: its turns run with the
scope as the BotCore source's thread, so BotCore keys its session per
Web UI session, and every post lands in the transcript of the scope
that asked. Two sessions never see, or answer, each other's messages
and questions. The bot itself is the same BotCore for both.
"""
import asyncio
import time

from print_handy.bot import runtime, web
from print_handy.core import prompts


def wait_for(view, kind, needle="", timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in view.entries():
            if entry["kind"] == kind and needle in (entry.get("content") or ""):
                return entry
        time.sleep(0.02)
    raise AssertionError(f"no {kind} entry containing {needle!r}; "
                         f"entries: {view.entries()}")


def wait_idle(view, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline and view.busy():
        time.sleep(0.02)
    assert not view.busy()


def test_scope_is_a_one_way_digest_of_the_session():
    secret = "the-session-id-from-a-cookie"
    scope = web.scope_for(secret)
    assert scope and scope == web.scope_for(secret)
    assert secret not in scope
    assert scope != web.scope_for("another-session-id")
    assert ":" not in scope  # it is one segment of a BotCore session key
    assert web.scope_for("") == ""


def test_two_sessions_never_see_each_others_messages(cfg):
    service = web.WebChatService(cfg)
    alice = service.view(web.scope_for("session-alice"))
    bob = service.view(web.scope_for("session-bob"))
    assert alice.submit("help") is True
    wait_for(alice, "message")
    wait_idle(alice)
    assert bob.entries() == []
    assert bob.busy() is False
    assert bob.submit("status of nothing in particular") is True
    wait_for(bob, "operator", "status of nothing")
    wait_idle(bob)
    alice_text = [e.get("content") for e in alice.entries()]
    bob_text = [e.get("content") for e in bob.entries()]
    assert "help" in alice_text and "help" not in bob_text
    assert "status of nothing in particular" in bob_text
    assert "status of nothing in particular" not in alice_text
    # Every entry is in exactly one transcript: no shared reply leaked.
    alice_seqs = {e["seq"] for e in alice.entries()}
    bob_seqs = {e["seq"] for e in bob.entries()}
    assert alice_seqs.isdisjoint(bob_seqs)


def test_each_session_is_its_own_botcore_session(cfg):
    """The scope rides as the source's thread, so BotCore's session key
    (platform:chat:thread:user) differs per Web UI session while the chat
    the gateway routes on stays the web chat."""
    service = web.WebChatService(cfg)
    seen = []
    real_process = service.core.process

    async def recording_process(event, **kwargs):
        seen.append(event.source)
        return await real_process(event, **kwargs)

    service.core.process = recording_process
    alice, bob = web.scope_for("a-session"), web.scope_for("b-session")
    service.view(alice).submit("help")
    service.view(bob).submit("help")
    wait_idle(service.view(alice))
    wait_idle(service.view(bob))
    keys = {runtime.session_key(source) for source in seen}
    assert keys == {f"webui:{web.CHAT_ID}:{alice}:{web.OPERATOR_ID}",
                    f"webui:{web.CHAT_ID}:{bob}:{web.OPERATOR_ID}"}
    assert {source.chat_id for source in seen} == {web.CHAT_ID}


def test_a_question_belongs_to_the_session_that_was_asked(cfg):
    service = web.WebChatService(cfg)
    alice_scope, bob_scope = web.scope_for("asker"), web.scope_for("bystander")
    alice, bob = service.view(alice_scope), service.view(bob_scope)
    cid = "cid-web-session-1"
    prompts.register(cid, f"webui:{web.CHAT_ID}:{alice_scope}:{web.OPERATOR_ID}",
                     "Which size?", ["Small", "Large"])
    # A flow asks from inside Alice's turn, without thread metadata: the
    # running turn's scope routes it.
    asyncio.run_coroutine_threadsafe(service._in_scope(
        alice_scope, lambda: service.adapter.send_clarify(
            web.CHAT_ID, "Which size?", ["Small", "Large"], cid,
            "flow-key")), service._loop).result(timeout=10)
    assert alice.open_prompt()["clarify_id"] == cid
    assert bob.open_prompt() is None
    assert all(entry.get("clarify_id") != cid for entry in bob.entries())
    # Bob cannot press Alice's button, even knowing its id.
    assert bob.answer(cid, 0) is None
    assert prompts.is_open(cid)
    assert alice.answer(cid, 1) == "Large"


def test_a_post_with_thread_metadata_goes_to_that_session(cfg):
    service = web.WebChatService(cfg)
    alice, bob = web.scope_for("meta-a"), web.scope_for("meta-b")
    asyncio.run_coroutine_threadsafe(service.adapter.send(
        web.CHAT_ID, "for alice only", metadata={"thread_id": alice}),
        service._loop).result(timeout=10)
    assert [e["content"] for e in service.view(alice).entries()] == ["for alice only"]
    assert service.view(bob).entries() == []


def test_a_post_for_no_session_is_shared(cfg):
    """A notice that belongs to no session (a startup announcement) is
    shown to every session, and is the only kind of entry they share."""
    service = web.WebChatService(cfg)
    asyncio.run_coroutine_threadsafe(service.adapter.send(
        web.CHAT_ID, "Print Handy restarted"), service._loop).result(timeout=10)
    for name in ("one", "two"):
        contents = [e["content"] for e in service.view(web.scope_for(name)).entries()]
        assert contents == ["Print Handy restarted"]
