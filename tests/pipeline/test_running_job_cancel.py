"""A typed cancel reaches a running intake and stops it before upload (#1789)."""
import asyncio
import json
import struct
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import prepared_intake, running_jobs
from print_handy.core.prepared_intake import Stopped


@pytest.fixture(autouse=True)
def _clean_registry():
    running_jobs.clear()
    yield
    running_jobs.clear()


def _source(user="u1"):
    return S(platform="discord", chat_id="c1", thread_id=None, user_id=user)


def _job():
    return running_jobs.register(running_jobs.RunningJob(
        platform="discord", chat_id="c1", thread_id=None,
        owner_user_id="u1", intake_id="i1"))


class _StopAdapter:
    """A minimal adapter with no picks/clicks: Stop prompts stay open."""

    def __init__(self):
        self.sent = []
        self.clarifies = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)

    async def send_clarify(self, **kw):
        self.clarifies.append(kw)
        return S(success=True)


async def _slow(result="done", delay=30.0):
    await asyncio.sleep(delay)
    return result


def test_typed_cancel_stops_a_slow_step_with_a_stop_button():
    adapter = _StopAdapter()
    job = _job()
    job.attach_job({"title": "Snacking Tongs"})

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Doing the slow thing.",
            _slow(), allow_stop=True))
        for _ in range(100):
            if adapter.clarifies:
                break
            await asyncio.sleep(0.05)
        assert adapter.clarifies
        assert adapter.clarifies[0]["choices"] == ["Stop"]
        assert job.step == "doing the slow thing"
        job.request_cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(main())


def test_typed_cancel_stops_a_slow_step_without_a_stop_button():
    adapter = _StopAdapter()
    job = _job()

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Doing the slow thing.", _slow()))
        await asyncio.sleep(0.2)
        assert adapter.sent == ["Doing the slow thing."]
        job.request_cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=10)

    asyncio.run(main())


def test_cancel_requested_before_a_step_stops_it_before_it_starts():
    adapter = _StopAdapter()
    job = _job()
    job.request_cancel()
    ran = []

    async def work():
        ran.append(True)
        return "done"

    coro = work()
    try:
        with pytest.raises(Stopped):
            asyncio.run(prepared_intake._with_stop(
                adapter, _source(), "model-link:i1", None, "Doing the slow thing.", coro))
    finally:
        coro.close()
    assert not ran


def test_a_step_runs_on_when_no_job_is_registered_or_it_is_past_upload():
    adapter = _StopAdapter()

    async def main():
        assert await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Doing the slow thing.",
            _slow(result="done", delay=0.05)) == "done"
        job = _job()
        job.mark_past_cancel_point()
        job.request_cancel()
        assert await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Doing the slow thing.",
            _slow(result="done", delay=0.05), allow_stop=True) == "done"

    asyncio.run(main())


def test_a_stop_step_without_progress_text_posts_no_prompt_but_still_cancels():
    """A small model's audit has no notice line: no Stop button is posted,
    but a typed cancel still stops it (#1789)."""
    adapter = _StopAdapter()
    job = _job()

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, None, _slow(), allow_stop=True))
        await asyncio.sleep(0.2)
        assert adapter.clarifies == []
        job.request_cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=10)
        running_jobs.unregister(job)
        _job()
        assert await prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, None,
            _slow(result="done", delay=0.05), allow_stop=True) == "done"
        assert adapter.clarifies == []

    asyncio.run(main())


def test_cancel_of_the_newest_job_does_not_stop_an_older_job():
    """Two intakes in one channel are separate jobs: cancelling the newest
    leaves the older one's steps running (#1789)."""
    adapter = _StopAdapter()
    older = running_jobs.register(running_jobs.RunningJob(
        platform="discord", chat_id="c1", thread_id=None,
        owner_user_id="u1", intake_id="i-old"))
    newer = running_jobs.register(running_jobs.RunningJob(
        platform="discord", chat_id="c1", thread_id=None,
        owner_user_id="u1", intake_id="i-new"))

    async def main():
        newer.request_cancel()
        assert running_jobs.lookup_for_source(_source()) is newer
        assert await prepared_intake._with_stop(
            adapter, _source(), "model-link:i-old", None, "Doing the slow thing.",
            _slow(result="done", delay=0.05)) == "done"
        with pytest.raises(Stopped):
            await prepared_intake._with_stop(
                adapter, _source(), "model-link:i-new", None, "Doing the slow thing.",
                _slow(), allow_stop=True)

    try:
        asyncio.run(main())
    finally:
        running_jobs.unregister(older)
        running_jobs.unregister(newer)


def test_cancel_during_a_threaded_step_waits_for_the_worker_thread():
    """#1793 review: cancelling a thread-backed step waits the thread out —
    the intake must not report cancellation while the worker still writes
    or reads the step's files."""
    import threading
    import time as _time

    from print_handy.core import owned_worker

    adapter = _StopAdapter()
    job = _job()
    started = threading.Event()
    finished = threading.Event()
    written = []

    def slow_write():
        started.set()
        _time.sleep(1.0)
        written.append("done")
        finished.set()

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None, "Downloading the model.",
            owned_worker.in_thread(slow_write), allow_stop=True))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            raise AssertionError("thread never started")
        job.request_cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=10)
        # Stopped was raised only after the thread finished writing.
        assert finished.is_set()
        assert written == ["done"]

    asyncio.run(main())


def _grid_stl(path, cols=250, rows=200, size=1.0):
    """A flat subdivided plane: many triangles, one object, bed-sized."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = cols * rows * 2
    with path.open("wb") as out:
        out.write(b"\0" * 80 + struct.pack("<I", count))
        for r in range(rows):
            for c in range(cols):
                x0, y0 = c * size, r * size
                x1, y1 = x0 + size, y0 + size
                for tri in (((x0, y0, 0.0), (x1, y0, 0.0), (x1, y1, 0.0)),
                            ((x0, y0, 0.0), (x1, y1, 0.0), (x0, y1, 0.0))):
                    out.write(struct.pack("<12fH", 0, 0, 1,
                                          *tri[0], *tri[1], *tri[2], 0))
    return path


def test_cancel_during_heavy_combine_kills_the_worker(tmp_path):
    from print_handy.core import model_parts, owned_worker
    from print_handy.intake.worker import _heavy

    first = _grid_stl(tmp_path / "a.stl")
    second = _grid_stl(tmp_path / "b.stl")
    target = tmp_path / "combined.3mf"
    adapter = _StopAdapter()
    job = _job()

    async def main():
        task = asyncio.create_task(prepared_intake._with_stop(
            adapter, _source(), "model-link:i1", None,
            "Putting the 2 files together as one project.",
            _heavy(model_parts.combine, target, [str(first), str(second)],
                   [220.0, 220.0, 250.0]),
            allow_stop=True))
        for _ in range(200):
            if owned_worker._LIVE:
                break
            await asyncio.sleep(0.05)
        assert owned_worker._LIVE, "the combine worker never started"
        job.request_cancel()
        with pytest.raises(Stopped):
            await asyncio.wait_for(task, timeout=120)

    asyncio.run(main())
    assert not target.exists()
    assert not owned_worker._LIVE


# ------------------------------------------------------------------ full intake


class _IntakeAdapter:
    """Answers the file-choice question, leaves every Stop prompt open."""

    def __init__(self, answers):
        self.answers = iter(answers)
        self.questions = []
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        if list(kw.get("choices") or []) == ["Stop"]:
            return S(success=True)
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw["clarify_id"], kw["choices"][next(self.answers)])
        return S(success=True)


def test_owner_cancel_during_audit_ends_the_intake_cleanly(tmp_path, monkeypatch):
    """#1789: a typed cancel lands during the audit step, even when the
    model is small enough that the audit shows no Stop prompt of its own."""
    import threading
    import time as _time

    from print_handy.core import model_job
    from .test_color_intake import state

    monkeypatch.setattr(model_job, "ARCHIVE", tmp_path / "archive")
    from print_handy.runtime import _config as runtime_config
    monkeypatch.setattr(runtime_config, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(model_job, "printables_inventory", lambda url: {
        "site": "printables", "slug": "snacking-tongs", "title": "Snacking Tongs",
        "model_id": 1,
        "sources": [{"id": 1, "name": "tongs_a.stl", "kind": "stls"},
                    {"id": 2, "name": "tongs_b.stl", "kind": "stls"}]})

    def download(job, chosen, dest):
        return str(_grid_stl(Path(dest) / chosen["name"]))

    audit_started = threading.Event()

    def slow_audit(path):
        audit_started.set()
        _time.sleep(5)
        return {"plates": 1}

    monkeypatch.setattr(model_job, "printables_download", download)
    monkeypatch.setattr(model_job, "audit", slow_audit)
    monkeypatch.setattr(model_job, "slice_job", lambda *a, **k: pytest.fail("slice ran after a cancel"))

    adapter = _IntakeAdapter([0])
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="tongs",
              text="https://www.printables.com/model/1-snacking-tongs")
    receipts = tmp_path / "receipts"

    async def main():
        task = asyncio.create_task(prepared_intake.prepare_link(
            event, adapter, state(), receipts, timeout=60))
        for _ in range(900):
            if audit_started.is_set():
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            raise AssertionError(
                f"intake never reached audit: {[q['question'] for q in adapter.questions]}")
        kind = await running_jobs.handle_owner_text(adapter, event.source, "Cancel", None)
        assert kind == "cancel"
        return await asyncio.wait_for(task, timeout=120)

    result = asyncio.run(main())
    assert result == {"action": "skip", "reason": "cancelled by operator"}
    receipt = json.loads(next(receipts.glob("*.json")).read_text())
    assert receipt["status"] == "CANCELLED"
    assert not receipt["sliced"] and not receipt["uploaded"] and not receipt["started"]
    assert "Cancelling the Snacking Tongs job. Nothing was uploaded or started." in adapter.sent
    assert "Cancelled. Nothing sliced, uploaded or started." in adapter.sent
    assert "Traceback" not in "\n".join(adapter.sent)
    assert running_jobs.lookup_for_source(event.source) is None


def test_owner_cancel_during_combine_ends_the_intake_cleanly(tmp_path, monkeypatch):
    from print_handy.core import model_job
    from .test_color_intake import state

    monkeypatch.setattr(model_job, "ARCHIVE", tmp_path / "archive")
    from print_handy.runtime import _config as runtime_config
    monkeypatch.setattr(runtime_config, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(model_job, "printables_inventory", lambda url: {
        "site": "printables", "slug": "snacking-tongs", "title": "Snacking Tongs",
        "model_id": 1,
        "sources": [{"id": 1, "name": "tongs_a.stl", "kind": "stls"},
                    {"id": 2, "name": "tongs_b.stl", "kind": "stls"}]})

    def download(job, chosen, dest):
        return str(_grid_stl(Path(dest) / chosen["name"]))

    monkeypatch.setattr(model_job, "printables_download", download)
    monkeypatch.setattr(model_job, "audit", lambda path: pytest.fail("audit ran after a cancel"))
    monkeypatch.setattr(model_job, "slice_job", lambda *a, **k: pytest.fail("slice ran after a cancel"))

    adapter = _IntakeAdapter([0])
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="tongs",
              text="https://www.printables.com/model/1-snacking-tongs")
    receipts = tmp_path / "receipts"

    async def main():
        task = asyncio.create_task(prepared_intake.prepare_link(
            event, adapter, state(), receipts, timeout=30))
        for _ in range(600):
            if any(q["question"].startswith("Putting the 2 files together")
                   for q in adapter.questions):
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            raise AssertionError(
                f"intake never reached combine: {[q['question'] for q in adapter.questions]}")
        kind = await running_jobs.handle_owner_text(adapter, event.source, "Cancel", None)
        assert kind == "cancel"
        return await asyncio.wait_for(task, timeout=120)

    result = asyncio.run(main())
    assert result == {"action": "skip", "reason": "cancelled by operator"}
    receipt = json.loads(next(receipts.glob("*.json")).read_text())
    assert receipt["status"] == "CANCELLED"
    assert not receipt["sliced"] and not receipt["uploaded"] and not receipt["started"]
    assert "Cancelling the Snacking Tongs job. Nothing was uploaded or started." in adapter.sent
    assert "Cancelled. Nothing sliced, uploaded or started." in adapter.sent
    assert "Traceback" not in "\n".join(adapter.sent)
    assert not list((tmp_path / "archive").rglob("*-parts.3mf"))
    assert running_jobs.lookup_for_source(event.source) is None
    # Every long pre-upload step the intake reached showed a Stop button.
    stop_texts = [q["question"] for q in adapter.questions
                  if list(q.get("choices") or []) == ["Stop"]]
    assert "Reading the model page." in stop_texts
    assert "Downloading file 1 of 2." in stop_texts
    assert "Checking the downloaded files." in stop_texts
    assert "Putting the 2 files together as one project." in stop_texts


def test_owner_cancel_during_download_waits_for_the_download_thread(tmp_path, monkeypatch):
    """#1793 review: cancelling during a threaded download waits the thread
    out — the downloaded file is complete when the intake reports cancelled,
    not still being written."""
    import time as _time

    from print_handy.core import model_job
    from .test_color_intake import state

    monkeypatch.setattr(model_job, "ARCHIVE", tmp_path / "archive")
    from print_handy.runtime import _config as runtime_config
    monkeypatch.setattr(runtime_config, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(model_job, "printables_inventory", lambda url: {
        "site": "printables", "slug": "snacking-tongs", "title": "Snacking Tongs",
        "model_id": 1,
        "sources": [{"id": 1, "name": "tongs_a.stl", "kind": "stls"},
                    {"id": 2, "name": "tongs_b.stl", "kind": "stls"}]})

    written = {}

    def download(job, chosen, dest):
        # Write the file in chunks with sleeps between them, so a cancel
        # landing mid-download catches a partial file if the thread were
        # abandoned instead of waited out.
        src = _grid_stl(Path(dest).parent / ("src-" + chosen["name"]), cols=120, rows=100)
        target = Path(dest) / chosen["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        chunk = len(data) // 4
        with open(target, "wb") as out:
            for i in range(4):
                out.write(data[i * chunk:(i + 1) * chunk if i < 3 else len(data)])
                out.flush()
                _time.sleep(0.3)
        written[target] = len(data)
        return str(target)

    monkeypatch.setattr(model_job, "printables_download", download)
    monkeypatch.setattr(model_job, "audit", lambda path: pytest.fail("audit ran after a cancel"))
    monkeypatch.setattr(model_job, "slice_job", lambda *a, **k: pytest.fail("slice ran after a cancel"))

    adapter = _IntakeAdapter([0])
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="tongs",
              text="https://www.printables.com/model/1-snacking-tongs")
    receipts = tmp_path / "receipts"

    async def main():
        task = asyncio.create_task(prepared_intake.prepare_link(
            event, adapter, state(), receipts, timeout=30))
        for _ in range(600):
            if any(q["question"].startswith("Downloading file 1 of 2.")
                   for q in adapter.questions):
                break
            await asyncio.sleep(0.05)
        else:
            task.cancel()
            raise AssertionError(
                f"intake never reached the download: {[q['question'] for q in adapter.questions]}")
        # Land the cancel mid-download, after the thread has started writing.
        await asyncio.sleep(0.3)
        kind = await running_jobs.handle_owner_text(adapter, event.source, "Cancel", None)
        assert kind == "cancel"
        result = await asyncio.wait_for(task, timeout=120)
        # The intake has reported cancellation: the download thread must
        # already be finished. This check runs inside the loop, before
        # asyncio.run's executor shutdown would wait the thread out for us.
        assert written, "the download never wrote a file"
        for target, size in written.items():
            assert target.exists()
            assert target.stat().st_size == size
        return result

    result = asyncio.run(main())
    assert result == {"action": "skip", "reason": "cancelled by operator"}
    receipt = json.loads(next(receipts.glob("*.json")).read_text())
    assert receipt["status"] == "CANCELLED"
    assert not receipt["sliced"] and not receipt["uploaded"] and not receipt["started"]
    assert "Cancelling the Snacking Tongs job. Nothing was uploaded or started." in adapter.sent
    assert "Cancelled. Nothing sliced, uploaded or started." in adapter.sent
    assert "Traceback" not in "\n".join(adapter.sent)
    assert running_jobs.lookup_for_source(event.source) is None
