"""The old home of the chat handlers, kept for one release (#1323).

The handlers moved into the package, ``print_handy.bot.handlers``, so a
wheel install has them; the bot's Router loads them from there. This file
still loads the same handlers when something loads it by path, the way
older layouts and many tests do: it executes the handler modules' code in
this one namespace, in their dependency order, so a module loaded from here
is a fresh, self-contained copy with every name the old file had, and
patching it changes what its own handlers see. Then it registers them
exactly as before. Remove it after 2.3.x.
"""
import ast
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _make_package_importable():
    """The gateway does not run from the repo. When print_handy is not installed, use the repo
    this plugin symlink points into (plugins/print-feedback/__init__.py -> repo root)."""
    try:
        import print_handy  # noqa: F401
    except ImportError:
        root = str(Path(__file__).resolve().parents[2])
        if root not in sys.path:
            sys.path.insert(0, root)


_make_package_importable()

_PACKAGE = "print_handy.bot.handlers"
_INTERFACE_MARK = "# --- handler interface (#1323) ---"


def _handler_sources():
    """(path, source) of each handler module, in the package's dependency order."""
    import print_handy
    folder = Path(print_handy.__file__).resolve().parent / "bot" / "handlers"
    init = ast.parse((folder / "__init__.py").read_text(encoding="utf-8"))
    for node in init.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "_MODULES":
            names = [element.value for element in node.value.elts]
            break
    else:
        raise RuntimeError(f"{folder / '__init__.py'} names no _MODULES")
    return [(folder / f"{name}.py", (folder / f"{name}.py").read_text(encoding="utf-8")) for name in names]


def _code_cache():
    """Compiled handler bodies, kept on the print_handy package so they outlive this module.

    Loading this file by path makes a fresh module each time (tests and the
    timing corpus do it per case), and parsing, rewriting and compiling
    every handler module cost about 0.12 s per load (#1349). A code object
    is immutable and exec's into each fresh namespace the same way, so
    reusing it changes nothing about what a load produces.
    """
    import print_handy
    return print_handy.__dict__.setdefault("_print_feedback_shim_code", {})


def _module_body(path, source):
    """The module's own code, compiled once per (path, source); see _compile_module_body."""
    cache = _code_cache()
    key = (str(path), source)
    code = cache.get(key)
    if code is None:
        code = cache[key] = _compile_module_body(path, source)
    return code


def _compile_module_body(path, source):
    """The module's own code: no docstring, no imports from inside the handler package
    (those names are already in this namespace), no handler interface, no logger of its own."""
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines()
    mark = next((n for n, line in enumerate(lines, 1) if line.strip() == _INTERFACE_MARK), None)
    package = _PACKAGE.split(".")
    body = []
    for node in tree.body:
        if mark is not None and node.lineno > mark:
            break
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str) and node is tree.body[0]):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.level == 1:
                continue
            base = package[:len(package) - node.level + 1]
            node = ast.copy_location(ast.ImportFrom(
                module=".".join(base + ([node.module] if node.module else [])), names=node.names, level=0), node)
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["logger"]:
            continue
        body.append(node)
    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    return compile(module, str(path), "exec")


for _path, _source in _handler_sources():
    exec(_module_body(_path, _source), globals())
del _path, _source


_DISPATCH_HOOKS = (
    guided_dispatch,
    design_dispatch,
    maintenance_dispatch,
    coldpull_dispatch,
    lookup_dispatch,
    source_dispatch,
    troubleshoot_dispatch,
    macros_dispatch,
    firmware_dispatch,
    calibration_dispatch,
    typed_quantity_dispatch,
    upload_wait_dispatch,
    upload_collection_dispatch,
    model_link_dispatch,
    color_selection_dispatch,
    print_next_dispatch,
    runout_intake_dispatch,
    exclude_dispatch,
    print_stop_dispatch,
    print_resume_dispatch,
    roast_dispatch,
    print_again_dispatch,
    edit_dispatch,
    loaded_spools_dispatch,
    purchase_dispatch,
    spool_dispatch,
    print_reboot_dispatch,
    bed_down_dispatch,
    live_dispatch,
    plate_calibrate_dispatch,
    nozzle_dispatch,
)

# The dispatch class of each command hook (#1153 item 10). A hook listed
# here answers only its own slash command: its first statement tests the
# message text exactly as its gate below does and returns None otherwise,
# before it reads the source, the gateway or any state. The Router tests
# the gate once per message and skips the hook when it fails, which is the
# answer the hook would have given. Every other hook takes free text
# (typed replies, links, plain-language asks, open prompts) and always runs.
# tests/bot/test_dispatch_classes.py holds each gate to its hook.
_COMMAND_GATES = {
    maintenance_dispatch: ('/print-maintenance', True),
    coldpull_dispatch: ('/print-coldpull', False),
    lookup_dispatch: ('/print-lookup', True),
    source_dispatch: ('/print-source', False),
    troubleshoot_dispatch: ('/print-troubleshoot', True),
    macros_dispatch: ('/print-macros', True),
    firmware_dispatch: ('/print-firmware', True),
    calibration_dispatch: ('/print-calibration', True),
    print_next_dispatch: ('/print-next', True),
    exclude_dispatch: ('/exclude', True),
    print_stop_dispatch: ('/print-stop', False),
    print_resume_dispatch: ('/print-resume', False),
    bed_down_dispatch: ('/print-bed-down', False),
    live_dispatch: ('/print-live', True),
    plate_calibrate_dispatch: ('/print-plate-calibrate', True),
    nozzle_dispatch: ('/nozzle', True),
}


def _gate(command, with_args):
    def gate(text):
        return _command_matches(text, command, with_args)
    gate.command = command
    return gate


for _hook, (_command, _with_args) in _COMMAND_GATES.items():
    _hook.dispatch_gate = _gate(_command, _with_args)


def _color_selection_gate(text):
    return text.startswith('/print-colors ')


_color_selection_gate.command = '/print-colors'
color_selection_dispatch.dispatch_gate = _color_selection_gate
del _hook, _command, _with_args


def register(ctx):
    """Install the handler set on the bot's Router (the only host left)."""
    for hook in _DISPATCH_HOOKS:
        ctx.register_hook('pre_gateway_dispatch', hook)
    if hasattr(ctx, 'register_platform_handler'):
        ctx.register_platform_handler('discord', alert_button_handlers)
