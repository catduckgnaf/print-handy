"""Every printer-control entry point goes through print_handy.printer_control (#1317).

Proposal 1 of #1160: chat, the listener alert buttons, the Web UI control
routes and the CLI used to reach the printer transports directly, which is
how ``/print-stop`` and the alert buttons once fell back to "default
printer, Moonraker only" while the Web UI got it right (#1147). This file
holds the line two ways:

- ``ENTRY_POINTS`` (in the service) names each entry point; each must exist
  and call the service.
- A scan of every module in the package and the handler plugin finds any
  reach into a transport driver (the stop, pause, resume, reboot,
  emergency-stop and family-control primitives, the Moonraker control
  endpoints, an adapter's ``control("stop"|"pause"|"resume")``) outside the
  service and the drivers themselves, and fails naming the file, line and
  function.

A few runtime checks then drive real entry points with the service's
methods replaced, to prove the call lands in the service and not only that
the source names it.

Fails on 0b0ebac1 (the base commit): ``print_handy.printer_control`` does not
exist there, and the scan finds the direct driver calls this change routed.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import printer_control
from print_handy.hermes import panic
from print_handy.hermes import print_alert_buttons as pab

ROOT = Path(__file__).resolve().parents[1]
SCANNED = (ROOT / "print_handy", ROOT / "plugins")

# The driver modules, by the name they are imported as (any alias resolves to these).
DRIVERS = {"print_stop", "print_control", "family_control", "panic", "print_alert_buttons"}
# Driver attributes that send a control command (or build the object that does).
PRIMITIVES = {
    "print_stop": {"cancel", "pause", "_lan_adapter"},
    "print_control": {"resume", "force_reboot"},
    "family_control": {"control_for", "FamilyControl"},
    "panic": {"force_restart", "purge"},
    "print_alert_buttons": {"clear_shutdown"},
}
# Moonraker endpoints that stop, pause, resume or restart a printer.
ENDPOINTS = ("/printer/print/cancel", "/printer/print/pause", "/printer/print/resume",
             "/printer/emergency_stop", "/printer/firmware_restart", "/server/restart")
ADAPTER_VERBS = {"stop", "pause", "resume"}

# Where the primitives may appear: the service, the drivers' own modules, the
# family adapters and clients that implement control(), and the named driver
# functions living in modules that also hold entry points.
ALLOWED_FILES = {
    "print_handy/printer_control.py",
    "print_handy/hermes/print_stop.py",
    "print_handy/hermes/print_control.py",
    "print_handy/hermes/family_control.py",
}
ALLOWED_PREFIXES = ("print_handy/printers/",)
ALLOWED_FUNCTIONS = {
    ("print_handy/hermes/panic.py", "force_restart"),
    ("print_handy/hermes/panic.py", "purge"),
    ("print_handy/hermes/print_alert_buttons.py", "clear_shutdown"),
    ("print_handy/webui/controls.py", "default_send"),
    # The Web UI driver's endpoint table (module level, read by default_send).
    ("print_handy/webui/controls.py", "<module>"),
}


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _sources():
    for base in SCANNED:
        for path in sorted(base.rglob("*.py")):
            yield _rel(path), path.read_text(encoding="utf-8")


def _driver_aliases(tree: ast.AST, rel: str) -> dict[str, str]:
    """{local name: driver module} for every way a module brings a driver in."""
    aliases: dict[str, str] = {}
    own = Path(rel).stem
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name in DRIVERS:
                    aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                tail = item.name.rsplit(".", 1)[-1]
                if tail in DRIVERS and item.asname:
                    aliases[item.asname] = tail
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            args = node.value.args
            if name in ("load", "lazy") and args and isinstance(args[0], ast.Constant) \
                    and args[0].value in DRIVERS:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases[target.id] = args[0].value
    if own in DRIVERS:
        aliases.setdefault("<self>", own)
    return aliases


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _bypasses(rel: str, source: str) -> list[str]:
    if rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES):
        return []
    tree = ast.parse(source)
    aliases = _driver_aliases(tree, rel)
    own = aliases.get("<self>")
    docstrings = _docstring_nodes(tree)
    found: list[str] = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
            # A nested def keeps its outermost function's name for the allowlist.
            here = func if func != "<module>" or name is None else name
            if (rel, here) not in ALLOWED_FUNCTIONS:
                problem = _problem(child)
                if problem:
                    found.append(f"{rel}:{child.lineno} in {here}: {problem}")
            visit(child, here)

    def _problem(node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = aliases.get(node.value.id)
            if module and node.attr in PRIMITIVES[module]:
                return f"{node.value.id}.{node.attr} (a {module} driver primitive)"
        if own and isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id in PRIMITIVES[own]:
            return f"{node.id} (this module's own {own} driver primitive)"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for endpoint in ENDPOINTS:
                if endpoint in node.value:
                    return f"the Moonraker control endpoint {endpoint}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "control" and node.args \
                and isinstance(node.args[0], ast.Constant) and node.args[0].value in ADAPTER_VERBS:
            return f'an adapter control("{node.args[0].value}")'
        return None

    visit(tree, "<module>")
    return found


def _function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


# ------------------------------------------------------------- enumeration


def test_entry_point_table_covers_every_surface():
    files = {path for path, _ in printer_control.ENTRY_POINTS}
    assert "print_handy/bot/handlers/print_stop.py" in files  # chat commands (#1323 moved them from plugins/)
    assert "print_handy/hermes/print_alert_buttons.py" in files  # every platform's alert buttons
    assert "print_handy/webui/controls.py" in files            # Web UI control routes
    assert "print_handy/hermes/panic.py" in files              # the CLI
    assert len(set(printer_control.ENTRY_POINTS)) == len(printer_control.ENTRY_POINTS)


@pytest.mark.parametrize("path,name", printer_control.ENTRY_POINTS,
                         ids=[f"{p}::{n}" for p, n in printer_control.ENTRY_POINTS])
def test_each_entry_point_calls_the_service(path, name):
    node = _function(ast.parse((ROOT / path).read_text(encoding="utf-8")), name)
    assert node is not None, f"entry point {path}::{name} is gone; update ENTRY_POINTS"
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert "printer_control" in names, f"{path}::{name} does not call print_handy.printer_control"


def test_no_entry_point_bypasses_the_service():
    found = [line for rel, source in _sources() for line in _bypasses(rel, source)]
    assert not found, ("control commands sent around print_handy.printer_control "
                       "(route them through the service):\n" + "\n".join(found))


def test_the_scan_catches_a_bypass():
    """The scan is not vacuous: each kind of bypass it guards is reported."""
    source = (
        "from print_handy.hermes import print_stop, family_control\n"
        "from print_handy.hermes import print_control as pc\n"
        "def stop(adapter):\n"
        "    print_stop.cancel('a.gcode')\n"
        "    pc.resume('a.gcode')\n"
        "    family_control.control_for(None)\n"
        "    pc._send('/printer/print/pause', method='POST')\n"
        "    adapter.control('stop')\n"
    )
    found = _bypasses("plugins/example.py", source)
    assert len(found) == 5, found
    assert all(" in stop: " in line for line in found)


# ------------------------------------------------------- runtime routing


def _spy(**returns):
    calls = []

    def method(name):
        def call(self, *args, **kwargs):
            calls.append((name, self.printer_id, args, kwargs))
            return returns[name]
        return call
    return calls, {name: method(name) for name in returns}


def test_alert_cancel_and_resume_land_in_the_service():
    calls, methods = _spy(cancel=(True, "Stopped."), resume=(True, "Resumed."))
    ready = {"klippy": "ready", "state": "paused", "filename": "f.gcode"}
    with mock.patch.multiple(printer_control.PrinterControl, **methods), \
            mock.patch("print_handy.hermes.print_control.status", return_value=ready):
        assert pab.handle_click("cancel", {"kind": "paused", "filename": "f.gcode"}) == "Stopped."
        assert pab.handle_click("resume", {"kind": "paused", "filename": "f.gcode"}) == "Resumed."
    assert [(name, pid, args) for name, pid, args, _ in calls] == [
        ("cancel", None, ("f.gcode",)), ("resume", None, ("f.gcode",))]


def test_family_alert_press_lands_in_the_service_with_its_connection():
    calls, methods = _spy(cancel=(True, "Stopped."), accepts_alert=True)
    alert = {"kind": "paused", "filename": "f.gcode", "connection": "octoprint"}
    with mock.patch.multiple(printer_control.PrinterControl, **methods):
        assert pab.handle_click("cancel", alert) == "Stopped."
    assert calls[-1][0] == "cancel" and calls[-1][3] == {"alert_connection": "octoprint"}


def test_family_alert_for_a_changed_printer_sends_nothing():
    adapter = S(connection="moonraker", transport="moonraker")
    with mock.patch("print_handy.hermes.family_control.adapter_for", return_value=adapter):
        out = pab.handle_click("cancel", {"kind": "paused", "filename": "f.gcode",
                                          "connection": "octoprint"})
    assert out == printer_control.FOREIGN_ALERT_TEXT


def test_visual_pause_on_moonraker_sends_through_the_service():
    calls, methods = _spy(is_bambu_lan=False, send_pause=None)
    reads = iter([{"klippy": "ready", "state": "printing", "filename": "f.gcode"},
                  {"klippy": "ready", "state": "paused", "filename": "f.gcode"}])
    with mock.patch.multiple(printer_control.PrinterControl, **methods), \
            mock.patch("print_handy.hermes.print_control.status", side_effect=lambda **k: next(reads)), \
            mock.patch("print_handy.hermes.print_alert_buttons.time.sleep"):
        out = pab.handle_click("pause", {"kind": "visual", "filename": "f.gcode"},
                               pause_wait_s=5, pause_poll_s=0)
    assert out == "Paused. Printer now reports paused."
    assert [c[0] for c in calls] == ["is_bambu_lan", "send_pause"]


def test_web_ui_control_route_sends_through_the_service():
    data = {"webui": {"controls_enabled": True},
            "printers": [{"id": "p1", "model": "klipper-generic"}]}
    sent = []
    calls, methods = _spy(send=None)

    def send(self, command, *, sender):
        sent.append((self.printer_id, command, sender))
    methods["send"] = send
    adapter = S(connection="moonraker")
    with mock.patch.multiple(printer_control.PrinterControl, **methods), \
            mock.patch("print_handy.settings.by_id", return_value={"id": "p1", "model": "m"}), \
            mock.patch("print_handy.settings.view_for", return_value={}):
        from print_handy.webui import controls
        code, body = controls.control(data, "p1", "pause", adapter=adapter,
                                      read=lambda a, v: {"state": "printing"}, send=lambda *a: None)
    assert code == 200, body
    assert sent and sent[0][:2] == ("p1", ("control", "pause"))


def test_cli_force_restart_is_the_service_emergency_stop(capsys):
    calls, methods = _spy(emergency_stop={"ok": True, "steps": ["emergency_stop"]})
    with mock.patch.multiple(printer_control.PrinterControl, **methods):
        assert panic.main(["force-restart", "garage"]) == 0
    assert calls == [("emergency_stop", "garage", (), {})]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_service_forwards_the_driver_call_shape():
    """None stays the drivers' own default printer: no printer_id keyword is added."""
    with mock.patch("print_handy.hermes.print_stop.cancel", return_value=(True, "x")) as cancel, \
            mock.patch("print_handy.hermes.print_control.resume", return_value=(True, "y")) as resume:
        printer_control.for_printer().cancel("a.gcode")
        printer_control.for_printer("p2").resume("b.gcode", wait_s=1)
    cancel.assert_called_once_with("a.gcode")
    resume.assert_called_once_with("b.gcode", printer_id="p2", wait_s=1)


def test_capabilities_follow_the_transport():
    moonraker = printer_control.for_printer(adapter=S(connection="moonraker"), view={})
    assert moonraker.capabilities().supports("emergency_stop")
    family = printer_control.for_printer(adapter=S(connection="prusalink", control=lambda a: None), view={})
    caps = family.capabilities()
    assert caps.supports("cancel") and not caps.supports("emergency_stop")
    offline = printer_control.for_printer(adapter=S(connection="offline"), view={})
    assert offline.capabilities().verbs == frozenset()
