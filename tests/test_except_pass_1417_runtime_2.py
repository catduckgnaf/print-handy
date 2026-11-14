import ast
from pathlib import Path

FILES = ['discord_print_summary.py', 'panic.py', 'moonraker_discord_announcements.py', 'print_control.py', 'print_records.py', 'roast_chat.py', 'starter_pings.py', 'stop_capture.py']

def test_assigned_runtime_files_have_no_except_pass():
    root = Path(__file__).parents[1] / "print_handy" / "runtime"
    found = []
    for name in FILES:
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                found.append(f"{name}:{node.lineno}")
    assert not found, found
