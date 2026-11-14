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
    left, right = "a" * 32, "b" * 32
    context = contextvars.ContextVar("test_correlation", default="")
    token_a = context.set(left)
    try:
        assert context.get() == left
        token_b = context.set(right)
        try:
            assert context.get() == right
        finally:
            context.reset(token_b)
        assert context.get() == left
    finally:
        context.reset(token_a)
