"""Focused regression coverage for the #1417 recovery paths."""
from __future__ import annotations

import ast
from pathlib import Path


def test_targeted_except_pass_handlers_are_removed():
    paths = [
        Path("print_handy/jobs/audit.py"),
        Path("print_handy/jobs/prompts.py"),
        Path("print_handy/gcode/verify.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text())
        assert not any(
            isinstance(node, ast.ExceptHandler)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Pass)
            for node in ast.walk(tree)
        ), path
