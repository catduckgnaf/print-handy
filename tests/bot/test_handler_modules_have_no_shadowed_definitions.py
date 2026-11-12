"""A chat handler module defines each top-level function once.

color_selection.py carried two identical ``color_selection_dispatch`` bodies;
the second silently replaced the first, so an edit to the first would never run.
"""
import ast
from collections import Counter
from pathlib import Path

HANDLERS = Path(__file__).resolve().parents[2] / "print_handy" / "bot" / "handlers"


def test_no_handler_module_redefines_a_top_level_function():
    repeated = {}
    for path in sorted(HANDLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = Counter(node.name for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        twice = sorted(name for name, count in names.items() if count > 1)
        if twice:
            repeated[path.name] = twice
    assert not repeated, repeated
