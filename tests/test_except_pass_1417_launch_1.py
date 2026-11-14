import ast
from pathlib import Path


FILES = (
    "print_handy/launch/print_now.py",
    "print_handy/launch/gates.py",
    "print_handy/printers/bambulab/client.py",
    "print_handy/printers/bambulab/camera.py",
    "print_handy/printers/bambulab/listener.py",
    "print_handy/printers/elegoo_sdcp/client.py",
)


def test_assigned_files_have_no_except_pass_handlers():
    root = Path(__file__).resolve().parents[1]
    remaining = []
    for filename in FILES:
        tree = ast.parse((root / filename).read_text(encoding="utf-8"), filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                for handler in node.handlers:
                    if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                        remaining.append(f"{filename}:{handler.lineno}")
    assert remaining == []
