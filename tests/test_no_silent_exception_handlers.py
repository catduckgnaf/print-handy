"""#1417: no exception is swallowed silently anywhere in the shipped code.

Every ``except`` handler in ``print_handy/``, ``scripts/``, ``plugins/`` and
``docker/`` must do something with the failure: prevent it, recover from it,
or report it with a level and a cause. A handler whose whole body is ``pass``,
``...`` or a bare string is a silent swallow and fails this test.

``contextlib.suppress`` is the same swallow under another name, so it is held
to a per-file allowance that can only go down. Those calls predate #1417's
sweep and are tracked in #1455. A new one fails here; removing one
means lowering its count below in the same change. The #1455 sweep removed
or narrowed most of them; the few that remain are reasoned exceptions, each
with its written reason in ``SUPPRESS_REASONS``, and the guard fails when an
allowance entry has no reason.

The #1417 sweep's per-PR files (``tests/test_except_pass_1417_*.py``) keep
their behavior tests. Their file-scoped guards are subsumed by this one.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("print_handy", "scripts", "plugins", "docker")

# contextlib.suppress(...) calls per file, frozen at the end of the #1417 sweep
# and lowered by the #1455 sweep. Only ever lower these. Every file still
# listed here carries its written reason in SUPPRESS_REASONS below (#1455):
# the done state is this dict empty, or reasoned-only, and the guard enforces
# the pairing.
SUPPRESS_ALLOWANCE = {
    "print_handy/core/background_slice.py": 7,
    "print_handy/core/owned_worker.py": 4,
    "print_handy/core/prepared_intake.py": 4,
    "print_handy/intake/fit_flow.py": 1,
    "print_handy/runtime/timelapse_encode.py": 1,
}

# Why each file above may keep its suppress calls (#1455). A file with an
# allowance but no reason here fails the guard, and so does a reason left
# behind for a file whose calls are all gone.
SUPPRESS_REASONS = {
    "print_handy/core/background_slice.py": (
        "Awaiting this runner's own slice tasks: _run records failures in "
        "self.error / self.supports_error, so the await can only surface "
        "CancelledError — an expected end state (a discarded or superseded "
        "slice) whose outcome is read from the fields right after."),
    "print_handy/core/owned_worker.py": (
        "Signaling a process group that may already be dead (callers verify "
        "death separately), the _gone poll whose timeout is the 'not yet' "
        "signal, feeding a worker that died mid-exchange (the read side of "
        "the gather surfaces the failure), and settle(), whose contract is "
        "to absorb cancels while a future finishes."),
    "print_handy/core/prepared_intake.py": (
        "A never-raises TIMING.jsonl tail parse (a torn last line from the "
        "concurrent writer is expected and skipped), awaiting watchers and "
        "tasks this code cancelled itself one line above (their real "
        "failures are retrieved and logged right after), and one bounded "
        "wait whose timeout is an expected outcome, read from the slice "
        "record next."),
    "print_handy/intake/fit_flow.py": (
        "Awaiting the fit task this finally block cancelled itself just "
        "above; a real failure is retrieved and logged right after."),
    "print_handy/runtime/timelapse_encode.py": (
        "flock(LOCK_UN) in a finally: an unlock failure is moot because "
        "closing the handle on the next line releases the lock either way."),
}


def _sources():
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8"), str(path))


def _is_silent(handler: ast.ExceptHandler) -> bool:
    body = handler.body
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    # `...` or a lone string: a comment dressed as a statement, still a swallow.
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
        and (stmt.value.value is Ellipsis or isinstance(stmt.value.value, str))


def _suppress_calls(tree) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "suppress"
                and isinstance(func.value, ast.Name) and func.value.id == "contextlib") \
                or (isinstance(func, ast.Name) and func.id == "suppress"):
            count += 1
    return count


def test_no_silent_exception_handler_anywhere():
    silent = [f"{name}:{node.lineno}"
              for name, tree in _sources()
              for node in ast.walk(tree)
              if isinstance(node, ast.ExceptHandler) and _is_silent(node)]
    assert silent == [], (
        "silent exception handlers (prevent the error, recover narrowly, or log the "
        f"level and cause; see #1417): {silent}")


def test_contextlib_suppress_never_grows():
    found = {name: n for name, tree in _sources() if (n := _suppress_calls(tree))}
    grown = {name: (n, SUPPRESS_ALLOWANCE.get(name, 0)) for name, n in found.items()
             if n > SUPPRESS_ALLOWANCE.get(name, 0)}
    assert grown == {}, (
        f"new contextlib.suppress calls (found, allowed): {grown}. suppress() swallows "
        "silently like `except: pass`; handle or log the failure instead (#1417).")
    stale = {name: (found.get(name, 0), allowed) for name, allowed in SUPPRESS_ALLOWANCE.items()
             if found.get(name, 0) < allowed}
    assert stale == {}, (
        f"contextlib.suppress allowance is higher than the code (found, allowed): {stale}. "
        "Lower SUPPRESS_ALLOWANCE in this file so the removed calls cannot come back.")


def test_contextlib_suppress_allowance_is_reasoned():
    """#1455's done state: the allowance is empty, or every entry in it has a written reason."""
    unreasoned = {name for name in SUPPRESS_ALLOWANCE if not SUPPRESS_REASONS.get(name, "").strip()}
    assert unreasoned == set(), (
        f"suppress allowance entries without a written reason (#1455): {unreasoned}. "
        "Remove the calls, or record why they may stay in SUPPRESS_REASONS.")
    orphaned = set(SUPPRESS_REASONS) - set(SUPPRESS_ALLOWANCE)
    assert orphaned == set(), (
        f"suppress reasons for files with no allowance left: {orphaned}. "
        "Drop the reason together with the allowance entry.")


def test_the_guard_recognizes_every_silent_shape():
    src = (
        "try:\n    a()\nexcept OSError:\n    pass\n"
        "try:\n    a()\nexcept OSError:\n    ...\n"
        "try:\n    a()\nexcept OSError:\n    'ignored'\n"
        "try:\n    a()\nexcept OSError:\n    log()\n"
        "import contextlib\nwith contextlib.suppress(OSError):\n    a()\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert [_is_silent(h) for h in handlers] == [True, True, True, False]
    assert _suppress_calls(tree) == 1
