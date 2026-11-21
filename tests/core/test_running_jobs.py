"""The owner's plain text reaches their running job (#1789)."""
import asyncio
import re
from types import SimpleNamespace as S

import pytest

from print_handy.core import running_jobs


@pytest.fixture(autouse=True)
def _clean_registry():
    running_jobs.clear()
    yield
    running_jobs.clear()


def _job(owner="u1", **kw):
    args = dict(platform="discord", chat_id="c1", thread_id=None,
                owner_user_id=owner, intake_id="i1")
    args.update(kw)
    return running_jobs.register(running_jobs.RunningJob(**args))


def _source(user="u1", chat="c1", thread=None):
    return S(platform="discord", chat_id=chat, thread_id=thread, user_id=user)


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        return S(success=True)


@pytest.mark.parametrize("text", [
    "cancel", "Cancel", "CANCEL", "cancel.", "cancel!", "stop", "Stop",
    "never mind", "Never Mind", "nevermind", "abort", "don't print it",
    "dont print it", "do not print it", "cancel this", "cancel the job",
    "stop the print", "cancel please", "please cancel",
])
def test_cancel_words(text):
    assert running_jobs.classify_owner_text(text) == "cancel"


@pytest.mark.parametrize("text", [
    "status", "Status?", "what's happening", "whats happening",
    "is it stuck", "is it stuck?", "?", "how is it going",
    "how's it going", "what is it doing", "where is it up to",
    "what's the status", "is it done yet",
])
def test_status_words(text):
    assert running_jobs.classify_owner_text(text) == "status"


@pytest.mark.parametrize("text", [
    "only the first file", "make it bigger", "hello", "cancel the printer",
    "stop the music", "what time is it", "2",
])
def test_other_text_is_not_a_stop_or_status_word(text):
    assert running_jobs.classify_owner_text(text) == "other"


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_text_classifies_to_nothing(text):
    assert running_jobs.classify_owner_text(text) is None


@pytest.mark.parametrize("seconds,expected", [
    (0, "0 s"), (45, "45 s"), (59, "59 s"), (60, "1 min"), (90, "1 min 30 s"),
    (158, "2 min 38 s"), (3600, "1 h 0 min"), (3720, "1 h 2 min"),
])
def test_format_elapsed(seconds, expected):
    assert running_jobs.format_elapsed(seconds) == expected


def test_registry_newest_wins_and_unregister_drops_it():
    first = _job(intake_id="i1")
    second = _job(intake_id="i2")
    assert running_jobs.lookup("discord", "c1", None) is second
    assert running_jobs.lookup_all("discord", "c1", None) == [first, second]
    running_jobs.unregister(second)
    assert running_jobs.lookup("discord", "c1", None) is first
    running_jobs.unregister(first)
    assert running_jobs.lookup("discord", "c1", None) is None
    running_jobs.unregister(first)  # unknown jobs never raise


def test_lookup_for_source_matches_platform_chat_and_thread():
    job = _job(thread_id="t1")
    assert running_jobs.lookup_for_source(_source(thread="t1")) is job
    assert running_jobs.lookup_for_source(_source(thread="t2")) is None
    assert running_jobs.lookup_for_source(_source(chat="other")) is None


def test_lookup_by_intake_finds_a_job_regardless_of_channel():
    first = _job(intake_id="i1")
    second = _job(intake_id="i2")
    assert running_jobs.lookup_by_intake("i1") is first
    assert running_jobs.lookup_by_intake("i2") is second
    assert running_jobs.lookup_by_intake("i3") is None


def test_label_is_the_job_title_once_attached():
    job = _job()
    assert job.label == "print"
    job.attach_job({"title": "Snacking Tongs", "slug": "snacking-tongs"})
    assert job.label == "Snacking Tongs"


def test_set_step_names_the_step_for_status_replies():
    job = _job()
    job.set_step("Putting the 2 files together as one project.")
    assert job.step == "putting the 2 files together as one project"
    job.set_step("Checking the model. Press Stop to cancel.")
    assert job.step == "checking the model"


def test_cancel_ack_status_and_job_aware_replies():
    job = _job()
    job.attach_job({"title": "Snacking Tongs"})
    job.set_step("combining the files")
    assert running_jobs.cancel_ack(job) == (
        "Cancelling the Snacking Tongs job. Nothing was uploaded or started.")
    assert re.match(r"Still combining the files \(\d+ s so far\)\.",
                    running_jobs.status_reply(job))
    assert re.match(r"Your Snacking Tongs job is still preparing "
                    r"\(combining the files, \d+ s\)\. "
                    r"Say \*cancel\* to stop it, or wait for its next question\.",
                    running_jobs.job_aware_reply(job))
    assert "newest of 2 jobs" in running_jobs.status_reply(job, count=2)


def test_handle_owner_text_cancel_acknowledges_and_sets_the_event():
    job = _job()
    adapter = _Adapter()
    kind = asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "cancel", None))
    assert kind == "cancel"
    assert job.cancel_event.is_set()
    assert adapter.sent == ["Cancelling the print job. Nothing was uploaded or started."]
    kind = asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "stop", None))
    assert kind == "cancel"
    assert adapter.sent[-1] == "Already cancelling the print job. Nothing was uploaded or started."


def test_handle_owner_text_status_and_other_replies():
    job = _job()
    job.attach_job({"title": "Snacking Tongs"})
    job.set_step("combining the files")
    adapter = _Adapter()
    kind = asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "status", {"thread_id": None}))
    assert kind == "status"
    assert re.match(r"Still combining the files \(\d+ s so far\)\.", adapter.sent[-1])
    kind = asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "only the first file", None))
    assert kind == "other"
    assert adapter.sent[-1].startswith("Your Snacking Tongs job is still preparing")


def test_handle_owner_text_ignores_another_user_a_finished_job_and_empty_text():
    job = _job()
    adapter = _Adapter()
    assert asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(user="u2"), "cancel", None)) is None
    assert not job.cancel_event.is_set()
    assert adapter.sent == []
    job.mark_past_cancel_point()
    assert asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "cancel", None)) is None
    assert not job.cancel_event.is_set()
    job.cancellable = True
    assert asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "   ", None)) is None


def test_handle_owner_text_with_no_running_job_falls_through():
    adapter = _Adapter()
    assert asyncio.run(running_jobs.handle_owner_text(
        adapter, _source(), "cancel", None)) is None
    assert adapter.sent == []
