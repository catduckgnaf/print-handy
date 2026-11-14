import ast
from pathlib import Path


BOT_FILES = (
    "print_handy/bot/handlers/common.py",
    "print_handy/bot/matrix.py",
    "print_handy/bot/web.py",
    "print_handy/bot/client.py",
    "print_handy/bot/edit_chat.py",
    "print_handy/bot/guided.py",
    "print_handy/bot/handlers/calibration.py",
    "print_handy/bot/handlers/model_link.py",
    "print_handy/bot/handlers/print_stop.py",
)


def test_assigned_bot_files_have_no_silent_except_pass_handlers():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for relative_path in BOT_FILES:
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ExceptHandler)
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Pass)
            ):
                offenders.append(f"{relative_path}:{node.lineno}")
    assert not offenders, "silent except-pass handlers: " + ", ".join(offenders)
