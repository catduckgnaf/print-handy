"""#1417: no exception is swallowed silently anywhere in the shipped code.

Every ``except`` handler in ``print_handy/``, ``scripts/``, ``plugins/`` and
``docker/`` must do something with the failure: prevent it, recover from it,
or report it with a level and a cause. A handler whose whole body is ``pass``,
``...`` or a bare string is a silent swallow and fails this test.

``contextlib.suppress`` is the same swallow under another name, so it is held
to a per-file allowance that can only go down. Those calls predate #1417's
sweep and are tracked in #1455. A new one fails here; removing one
means lowering its count below in the same change.

The #1417 sweep's per-PR files (``tests/test_except_pass_1417_*.py``) keep
their behavior tests. Their file-scoped guards are subsumed by this one.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("print_handy", "scripts", "plugins", "docker")

# contextlib.suppress(...) calls per file, frozen at the end of the #1417 sweep.
# Only ever lower these.
SUPPRESS_ALLOWANCE = {
    "print_handy/bot/edit_chat.py": 1,
    "print_handy/bot/handlers/common.py": 4,
    "print_handy/core/prepared_intake.py": 29,
    "print_handy/runtime/family_control.py": 1,
    "print_handy/runtime/moonraker_event_listener.py": 4,
    "print_handy/runtime/print_alert_buttons.py": 4,
    "print_handy/runtime/timelapse_encode.py": 1,
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
