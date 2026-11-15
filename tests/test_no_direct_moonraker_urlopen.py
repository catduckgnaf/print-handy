"""#1466 guard: no direct ``urlopen`` against a Moonraker URL outside the helper.

In the style of the #1417 silent-handler guard: every module that talks to
Moonraker must go through ``print_handy.moonraker`` (the one transport that
adds ``X-Api-Key`` and classifies 401/403 as an auth failure). A direct
``urllib.request.urlopen`` in any of these modules is how the key gets
forgotten again, so it fails here.

The one allowance is ``print_alert_buttons``' Discord REST posts: those
``urlopen`` calls aim at Discord's API, never at Moonraker, and are listed
by function name so a new one has to be named here deliberately.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MOONRAKER_MODULES = (
    "print_handy/runtime/print_control.py",
    "print_handy/runtime/print_stop.py",
    "print_handy/webui/controls.py",
    "print_handy/core/launch_state.py",
    "print_handy/core/start_recorder.py",
    "print_handy/jobs/common.py",
    "print_handy/core/printer_state.py",
    "print_handy/core/purge.py",
    "print_handy/core/spool_lifecycle.py",
    "print_handy/runtime/spool_sync.py",
    "print_handy/runtime/print_alert_buttons.py",
    "print_handy/runtime/klipper_report.py",
    "print_handy/webui/status.py",
    "print_handy/doctor.py",
    "print_handy/bot/runtime.py",
    "print_handy/go2rtc.py",
    # Modules that already sent the key, routed through the helper too:
    "print_handy/core/printer_clock.py",
    "print_handy/core/model_job.py",
    "print_handy/diagnostics_bundle.py",
    "print_handy/runtime/moonraker_discord_announcements.py",
)

# (module, function) pairs whose urlopen calls are NOT Moonraker calls:
# print_alert_buttons posts to Discord's REST API with the bot token.
NON_MOONRAKER_URLOPEN = {
    ("print_handy/runtime/print_alert_buttons.py", "post_plate_picker"),
    ("print_handy/runtime/print_alert_buttons.py", "post_plate_confirm"),
    ("print_handy/runtime/print_alert_buttons.py", "post_spool_question"),
    ("print_handy/runtime/print_alert_buttons.py", "edit_message"),
    ("print_handy/runtime/print_alert_buttons.py", "post_insertion_confirmation"),
    ("print_handy/runtime/print_alert_buttons.py", "post_new_spool"),
    ("print_handy/runtime/print_alert_buttons.py", "post_with_buttons"),
    ("print_handy/runtime/print_alert_buttons.py", "retire_alerts"),
    ("print_handy/runtime/print_alert_buttons.py", "post_visual_alert"),
    ("print_handy/runtime/print_alert_buttons.py", "post_visual_text_alert"),
    ("print_handy/runtime/print_alert_buttons.py", "post_visual_dark_notice"),
}

HELPER = "print_handy/moonraker.py"


def _urlopen_calls(path):
    """(function_name, lineno) for every urlopen call in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []

    def visit(node, func_name):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func_name
            if isinstance(child, ast.Call):
                func = child.func
                if (isinstance(func, ast.Attribute) and func.attr == "urlopen") or (
                        isinstance(func, ast.Name) and func.id == "urlopen"):
                    found.append((name, child.lineno))
            visit(child, name)

    visit(tree, "<module>")
    return found


def test_no_direct_urlopen_in_moonraker_modules():
    offenders = []
    for rel in MOONRAKER_MODULES:
        for func_name, lineno in _urlopen_calls(ROOT / rel):
            if (rel, func_name) in NON_MOONRAKER_URLOPEN:
                continue
            offenders.append(f"{rel}:{lineno} in {func_name}()")
    assert offenders == [], (
        "direct urlopen in a Moonraker module (route it through "
        f"print_handy.moonraker, #1466): {offenders}")


def test_the_helper_is_the_one_place_that_opens_moonraker_urls():
    calls = _urlopen_calls(ROOT / HELPER)
    assert calls, "print_handy/moonraker.py must own the urlopen call"


def test_non_moonraker_allowance_is_exact():
    """The Discord allowance cannot quietly grow or go stale."""
    found = {(rel, func) for rel in MOONRAKER_MODULES
             for func, _lineno in _urlopen_calls(ROOT / rel)}
    assert found & {(rel, func) for rel, func in NON_MOONRAKER_URLOPEN} == NON_MOONRAKER_URLOPEN
    assert {rel for rel, _f in NON_MOONRAKER_URLOPEN} == {"print_handy/runtime/print_alert_buttons.py"}
