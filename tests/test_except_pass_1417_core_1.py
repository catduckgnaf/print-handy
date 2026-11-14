import ast
from pathlib import Path

FILES = ['core/prepared_intake.py', 'core/orca_adapter.py']

def test_touched_files_have_no_except_pass_handlers():
    root = Path(__file__).resolve().parents[1] / "print_handy"
    remaining = []
    for relative in FILES:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ExceptHandler) and len(node.body) == 1
                    and isinstance(node.body[0], ast.Pass)):
                remaining.append(f"{relative}:{node.lineno}")
    assert not remaining, "silent except-pass handlers remain: " + ", ".join(remaining)
