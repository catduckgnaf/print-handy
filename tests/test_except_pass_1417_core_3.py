import ast
from pathlib import Path

FILES = ['core/roast.py', 'core/purge.py', 'core/site_accounts.py', 'core/printer_geometry.py', 'core/resources.py', 'core/status_voice.py', 'core/support_clearance.py', 'core/thermal_probe.py', 'core/wrapped_joins.py', 'gcode/verify.py', 'intake/fit_flow.py', 'jobs/audit.py', 'jobs/prompts.py', 'jobs/slice.py']

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
