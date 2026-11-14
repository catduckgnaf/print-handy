"""Per-job correlation identity stays stable and isolated across concurrent slices."""
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
    import asyncio

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
