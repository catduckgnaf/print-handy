import ast
from pathlib import Path

FILES = ['moonraker_event_listener.py', '_config.py', 'camera_watch.py', 'family_event_listener.py', 'nozzle_change.py', 'print_alert_buttons.py', 'print_stop.py', 'start_claims.py', 'timelapse_encode.py', 'visual_watch.py', 'watchdog_events.py']

def test_assigned_runtime_files_have_no_except_pass():
    root = Path(__file__).parents[1] / "print_handy" / "runtime"
    found = []
    for name in FILES:
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                found.append(f"{name}:{node.lineno}")
    assert not found, found
