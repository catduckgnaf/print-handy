"""The chat handlers' source text, for the static checks that read it.

The handlers moved from plugins/print-feedback/__init__.py into the
print_handy.bot.handlers package (#1323). A check that read the old file
reads every handler module here instead, in the package's dependency
order (shared code first), so text that sat together in the old file
still sits together.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "print_handy" / "bot" / "handlers"


def handler_paths():
    init = ast.parse((HANDLERS / "__init__.py").read_text(encoding="utf-8"))
    for node in init.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_MODULES":
            return [HANDLERS / f"{element.value}.py" for element in node.value.elts]
    raise AssertionError("print_handy/bot/handlers/__init__.py names no _MODULES")


def handler_source():
    """Every handler module's source, joined in dependency order."""
    paths = handler_paths()
    assert len(paths) > 20, paths
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)
