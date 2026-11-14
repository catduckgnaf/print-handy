"""Per-job correlation identity stays stable and isolated across concurrent slices."""
import asyncio
import contextvars

from print_handy import logs
from print_handy.core import job_correlation


def test_intake_id_is_reused_and_random_id_is_path_independent():
    known = "a" * 32
    job = {"intake_id": known}
    assert job_correlation.ensure(job) == known
    assert job["job_correlation_id"] == known
    first, second = {}, {}
    assert job_correlation.ensure(first) == job_correlation.ensure(first)
    assert job_correlation.ensure(second) != first["job_correlation_id"]
    assert job_correlation.from_context({"job_correlation_id": "../private/path"}) is None


def test_concurrent_log_contexts_keep_separate_correlation_ids():
    async def read_id(job):
        token = logs.bind(job_correlation_id=job_correlation.ensure(job))
        try:
            await asyncio.sleep(0)
            return logs.bound_context()["job_correlation_id"]
        finally:
            logs.unbind(token)

    async def run():
        left, right = {"intake_id": "a" * 32}, {"intake_id": "b" * 32}
        return await asyncio.gather(read_id(left), read_id(right))

    assert asyncio.run(run()) == ["a" * 32, "b" * 32]


# --- #1414 rework: the id is persisted by merging one field ------------------

import json
import logging


def test_persist_adds_only_the_id_and_keeps_fields_written_since(tmp_path):
    # The plate-A project_number claim lands in JOB.json after the caller read
    # it; persisting the id must not write the stale copy back over it.
    job_json = tmp_path / "JOB.json"
    job_json.write_text(json.dumps({"title": "Rack"}))
    stale = json.loads(job_json.read_text())
    job_json.write_text(json.dumps({"title": "Rack", "project_number": 82}))
    value = job_correlation.ensure(stale)
    job_correlation.persist(job_json, value)
    on_disk = json.loads(job_json.read_text())
    assert on_disk == {"title": "Rack", "project_number": 82, "job_correlation_id": value}


def test_persist_keeps_an_existing_id_and_survives_an_unwritable_file(tmp_path, caplog):
    job_json = tmp_path / "JOB.json"
    job_json.write_text(json.dumps({"job_correlation_id": "c" * 32}))
    job_correlation.persist(job_json, "d" * 32)
    assert json.loads(job_json.read_text())["job_correlation_id"] == "c" * 32
    missing = tmp_path / "gone" / "JOB.json"
    with caplog.at_level(logging.WARNING, logger=job_correlation.logger.name):
        job_correlation.persist(missing, "d" * 32)  # must not raise
    assert any("could not record the job correlation id" in r.getMessage() for r in caplog.records)


def test_listener_binds_the_id_process_wide_and_clears_it(monkeypatch):
    # A process-wide binding reaches the listener's worker threads, which a
    # task ContextVar never does; the end-of-print path clears it.
    import threading
    logs.clear_context()
    logs.bind_process(job_correlation_id="e" * 32)
    seen = []
    t = threading.Thread(target=lambda: seen.append(logs.bound_context().get("job_correlation_id")))
    t.start(); t.join()
    assert seen == ["e" * 32]
    logs.unbind_process("job_correlation_id")
    assert "job_correlation_id" not in logs.bound_context()


def test_bundle_finds_ids_older_than_the_newest_fifty_jobs(tmp_path, monkeypatch):
    # #1424 review: 60 newer jobs without an id must not hide older ones with ids.
    import os
    from print_handy import diagnostics_bundle
    from print_handy.jobs import common
    archive = tmp_path / "archive"
    for i in range(70):
        folder = archive / f"job{i:02d}"
        folder.mkdir(parents=True)
        job = {"title": f"job{i}"}
        if i < 3:
            job["job_correlation_id"] = f"{i:x}" * 32
        (folder / "JOB.json").write_text(json.dumps(job))
        os.utime(folder / "JOB.json", (1_000_000 + i, 1_000_000 + i))  # job69 newest
    monkeypatch.setattr(common, "ARCHIVE", archive)
    section = diagnostics_bundle.job_correlation_section(None)
    assert section.splitlines() == ["- `" + "2" * 32 + "`", "- `" + "1" * 32 + "`", "- `" + "0" * 32 + "`"]


def test_bundle_skips_a_job_removed_mid_scan(tmp_path, monkeypatch):
    from print_handy import diagnostics_bundle
    from print_handy.jobs import common
    archive = tmp_path / "archive"
    (archive / "kept").mkdir(parents=True)
    (archive / "kept" / "JOB.json").write_text(json.dumps({"job_correlation_id": "f" * 32}))
    gone = archive / "gone" / "JOB.json"
    real_glob = type(archive).glob
    monkeypatch.setattr(common, "ARCHIVE", archive)
    monkeypatch.setattr(type(archive), "glob",
                        lambda self, pattern: [gone, *real_glob(self, pattern)] if self == archive else real_glob(self, pattern))
    assert diagnostics_bundle.job_correlation_section(None) == "- `" + "f" * 32 + "`"
