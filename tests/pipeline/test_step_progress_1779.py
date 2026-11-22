"""#1779: a slow intake step says how long it has run, in place.

A step whose Stop prompt is posted (download, combine, slicing, ...) edits
that same prompt with its elapsed time ("Slicing… 18 s") and, once the step
finished, with "done" and the total. Never a new message per tick. The
multi-file intake also writes its stage records to TIMING.jsonl as each
stage finishes, so a slow combine no longer hides the finished downloads.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import prepared_intake, running_jobs
from tests.bound_names import setattr_bound
from print_handy.intake import worker as intake_worker


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch):
    running_jobs.clear()
    monkeypatch.setattr(prepared_intake, "STEP_PROGRESS_TICK_S", 0.05, raising=False)
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    yield
    running_jobs.clear()


def _source():
    return S(platform="discord", chat_id="c1", thread_id=None, user_id="u1")


class _EditingAdapter:
    """Stop prompts stay open; prompt edits and message edits are recorded."""

    def __init__(self):
        self.sent, self.clarifies, self.prompt_edits, self.message_edits = [], [], [], []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True, message_id="m-sent")

    async def send_clarify(self, **kw):
        self.clarifies.append(kw)
        return S(success=True, message_id="m-stop")

    async def edit_prompt(self, clarify_id, question):
        self.prompt_edits.append(question)
        return True

    async def edit_message(self, chat_id, message_id, content, metadata=None):
        self.message_edits.append((message_id, content))
        return S(success=True, message_id=message_id)


async def _slow(result="done", delay=0.4):
    await asyncio.sleep(delay)
    return result


def test_elapsed_and_done_text():
    text = prepared_intake._step_progress_text
    assert text("Slicing. Press Stop to cancel.", 18.2) == "Slicing… 18 s. Press Stop to cancel."
    assert text("Putting the 2 files together as one project.", 125) == (
        "Putting the 2 files together as one project… 2 min 5 s.")
    assert text("Slicing. Press Stop to cancel.", 180, done=True) == "Slicing: done, 3 min."
    assert text("Downloading the model.", 0.4, done=True) == "Downloading the model: done, 0 s."


def test_a_slow_step_edits_its_one_prompt_with_the_elapsed_time():
    adapter = _EditingAdapter()

    async def main():
        return await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Slicing. Press Stop to cancel.",
            _slow("sliced"), allow_stop=True)

    assert asyncio.run(main()) == "sliced"
    # One prompt, no new message per tick.
    assert [c["question"] for c in adapter.clarifies] == ["Slicing. Press Stop to cancel."]
    assert adapter.sent == []
    assert adapter.prompt_edits, "the prompt never showed its elapsed time"
    assert all(edit.startswith("Slicing… ") and edit.endswith(" s. Press Stop to cancel.")
               for edit in adapter.prompt_edits)
    # The finished step reads done, on the same message.
    [(message_id, done)] = adapter.message_edits
    # The step sleeps 0.4 s; a loaded CI runner can take past 0.5 s, which rounds to 1 s.
    assert message_id == "m-stop" and done in ("Slicing: done, 0 s.", "Slicing: done, 1 s.")


def test_a_step_inside_its_quiet_window_still_posts_and_edits_nothing():
    adapter = _EditingAdapter()

    async def main():
        return await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Downloading the model.",
            _slow("file.stl", delay=0.01), allow_stop=True, quiet_s=1.0)

    assert asyncio.run(main()) == "file.stl"
    assert adapter.clarifies == adapter.sent == adapter.prompt_edits == adapter.message_edits == []


def test_a_stopped_step_is_not_marked_done():
    adapter = _EditingAdapter()
    job = running_jobs.register(running_jobs.RunningJob(
        platform="discord", chat_id="c1", thread_id=None, owner_user_id="u1", intake_id="i1"))

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Slicing. Press Stop to cancel.",
            _slow(delay=30), allow_stop=True))
        for _ in range(100):
            if adapter.clarifies:
                break
            await asyncio.sleep(0.02)
        job.request_cancel()
        with pytest.raises(prepared_intake.Stopped):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(main())
    assert adapter.message_edits == []


def test_a_failed_start_result_is_not_marked_done():
    adapter = _EditingAdapter()

    async def refused():
        await asyncio.sleep(0.15)
        return False, "REFUSED: printer busy."

    async def main():
        return await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Uploading. Press Stop to cancel.",
            refused(), allow_stop=True)

    assert asyncio.run(main())[0] is False
    assert adapter.message_edits == []


def test_the_discord_stop_view_message_ticks_and_ends_done(monkeypatch):
    edits = []

    class StopMessage:
        id = "77"

        async def edit(self, **kwargs):
            edits.append(kwargs)

    async def stop_sender(_source, _text, _cid):
        return StopMessage()

    monkeypatch.setattr(prepared_intake, "STOP_SENDER", stop_sender)
    adapter = _EditingAdapter()

    async def main():
        return await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Downloading file 1 of 2.",
            _slow("a.stl"), allow_stop=True)

    assert asyncio.run(main()) == "a.stl"
    ticks, last = edits[:-1], edits[-1]
    assert ticks and all(set(e) == {"content"} and e["content"].startswith("Downloading file 1 of 2… ")
                         for e in ticks)
    # Same 0.4 s step as above: a loaded runner past 0.5 s rounds to 1 s.
    assert last in ({"content": "Downloading file 1 of 2: done, 0 s.", "view": None},
                    {"content": "Downloading file 1 of 2: done, 1 s.", "view": None})
    assert adapter.clarifies == [] and adapter.sent == []


def test_parts_intake_records_download_before_the_combine_finishes(tmp_path, monkeypatch):
    """2026-11-21: four minutes into a two-file intake TIMING.jsonl held only
    message_received, because download_done was stamped after the combine."""
    from print_handy.core import model_parts
    from print_handy.jobs import common as jobs_common
    from print_handy.jobs import sources as jobs_sources
    from print_handy.runtime import _config as runtime_config
    from .test_color_intake import state
    from .test_running_job_cancel import _IntakeAdapter, _grid_stl

    setattr_bound(monkeypatch, jobs_common, "ARCHIVE", tmp_path / "archive")
    monkeypatch.setattr(runtime_config, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(jobs_sources, "printables_inventory", lambda url: {
        "site": "printables", "slug": "two-parts", "title": "Two Parts", "model_id": 1,
        "sources": [{"id": 1, "name": "part_a.stl", "kind": "stls"},
                    {"id": 2, "name": "part_b.stl", "kind": "stls"}]})
    monkeypatch.setattr(jobs_sources, "printables_download",
                        lambda job, chosen, dest: str(_grid_stl(Path(dest) / chosen["name"], 20, 20)))
    seen = {}

    def combine(target, paths, bed, **kw):
        timing = next((tmp_path / "archive").rglob("TIMING.jsonl"))
        seen["during_combine"] = [json.loads(line)["stage"] for line in timing.read_text().splitlines()]
        raise RuntimeError("stop the intake here")

    monkeypatch.setattr(model_parts, "combine", combine)
    setattr_bound(monkeypatch, intake_worker, "_heavy",
                        lambda fn, *a, **k: asyncio.to_thread(fn, *a, **k))
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="parts-timing", text="https://www.printables.com/model/1-two-parts")

    asyncio.run(prepared_intake.prepare_link(event, _IntakeAdapter([0]), state(), tmp_path / "receipts",
                                             timeout=30))
    stages = seen["during_combine"]
    assert stages[0] == "message_received"
    assert {"download_done", "source_checked", "combine_begin"} <= set(stages)
    assert "combine_done" not in stages
