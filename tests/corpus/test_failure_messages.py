"""#817 corpus guard: a known-bad model never ends in the generic failure message.

"Known bad" is a corpus case the product cannot yet finish: one marked
known_failure, or one whose [expect] pins a FAILED receipt. Each runs through
the mock tier (fake Discord and Orca transport, no real slicer) and every chat
post it produced is checked. The generic "stopped before it was ready" lead is
only for a failure nothing recognizes; a corpus file the product already
knows is bad must name its stage and cause instead.
"""
import os

import pytest

from . import harness

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
GENERIC = "stopped before it was ready"
CASES = [harness.load_case(folder) for folder in harness.case_dirs()]
KNOWN_BAD = [case for case in CASES
             if case.data.get("known_failure") or case.expect.get("status") == "FAILED"]


def test_the_corpus_has_known_bad_cases_to_guard():
    assert KNOWN_BAD, "the guard would pass vacuously"


@pytest.mark.skipif(REAL, reason="the guard reads mock-tier chat posts; the real slicer runs elsewhere")
@pytest.mark.parametrize("case", KNOWN_BAD, ids=[case.name for case in KNOWN_BAD])
def test_known_bad_case_never_ends_in_the_generic_message(case, tmp_path, monkeypatch):
    posts = []
    original = harness.FakeAdapter.send

    async def recording_send(self, chat_id, content, reply_to=None, metadata=None):
        posts.append(str(content))
        return await original(self, chat_id, content, reply_to, metadata)

    monkeypatch.setattr(harness.FakeAdapter, "send", recording_send)
    try:
        harness.run_case(case, tmp_path)
    except Exception:  # a flow that stops early still posted what the operator saw
        pass
    assert not [post for post in posts if GENERIC in post], posts
    failure_posts = [post for post in posts if "Reference: [PH_" in post]
    for post in failure_posts:
        assert "Traceback" not in post, post
