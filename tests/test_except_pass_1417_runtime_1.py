import ast
from pathlib import Path

FILES = ['spool_sync.py', 'timelapse_capture.py', 'print_next.py', 'klipper_report.py']

def test_assigned_runtime_files_have_no_except_pass():
    root = Path(__file__).parents[1] / "print_handy" / "runtime"
    found = []
    for name in FILES:
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                found.append(f"{name}:{node.lineno}")
    assert not found, found


def test_stop_and_reap_already_exited_child_logs_debug_not_warning(caplog):
    """A child that exits before terminate() is the expected race (duckcatgnaf #1431)."""
    import asyncio
    import logging

    from print_handy.runtime import print_next

    class Proc:
        returncode = None

        def terminate(self):
            self.returncode = 0
            raise ProcessLookupError

        def kill(self):
            raise AssertionError("kill must not run once the child is gone")

        async def wait(self):
            return self.returncode

    caplog.set_level(logging.DEBUG, logger=print_next.__name__)
    assert asyncio.run(print_next._stop_and_reap(Proc())) is True
    records = [r for r in caplog.records if r.name == print_next.__name__]
    assert any(r.levelno == logging.DEBUG and "already exited" in r.getMessage() for r in records)
    assert not any(r.levelno >= logging.WARNING for r in records)


def test_stop_and_reap_unreaped_child_still_warns(caplog, monkeypatch):
    """A child that survives SIGKILL is still a warning, and reaped stays False."""
    import asyncio
    import logging

    from print_handy.runtime import print_next

    monkeypatch.setattr(print_next, "STOP_GRACE_S", 0.01)
    monkeypatch.setattr(print_next, "STOP_REAP_TIMEOUT_S", 0.01)

    class Proc:
        returncode = None

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            await asyncio.sleep(1)

    caplog.set_level(logging.DEBUG, logger=print_next.__name__)
    assert asyncio.run(print_next._stop_and_reap(Proc())) is False
    assert any(r.levelno == logging.WARNING and "not reaped" in r.getMessage()
               for r in caplog.records if r.name == print_next.__name__)
