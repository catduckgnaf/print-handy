"""#1455: every rewritten `contextlib.suppress` keeps the original control flow.

``with contextlib.suppress(asyncio.CancelledError): await task`` never re-raises. The
first rewrite added ``if asyncio.current_task().cancelling(): raise``, which changes
behavior twice over:
- inside a ``finally`` it skips the rest of the cleanup (an open Stop prompt stayed open);
- ``cancelling()`` stays nonzero after an inner coroutine has already handled a
  cancellation, so it re-raised an old cancel. That turned a Stop during a live start
  from "start outcome uncertain" into "cancelled by operator": tests/pipeline/
  test_prepared_intake.py::test_stop_during_live_start_reaps_child_and_reports_uncertain_outcome.

The handled form is a plain ``except`` that logs and falls through, exactly as suppress did.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "print_handy"


def test_no_cancel_handler_reraises_on_a_cancelling_count():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
                if isinstance(node, ast.If) and "cancelling" in ast.unparse(node.test) \
                        and any(isinstance(s, ast.Raise) for s in node.body):
                    found.append(f"{path.relative_to(ROOT.parent)}:{node.lineno}")
    assert found == [], (
        "a cancel handler re-raises on asyncio.current_task().cancelling(); the suppress it "
        f"replaced never re-raised (#1455): {found}")
