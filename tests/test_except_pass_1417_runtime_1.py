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
