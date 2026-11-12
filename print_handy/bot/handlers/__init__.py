"""The chat command handlers (#1323, proposal 5 of #1160).

Each handler module declares ``match(text)`` (its dispatch gate: a slash
command, or every message for a free-text handler) and ``handle(event,
gateway)``. ``HANDLERS`` is the dispatch order: the first handler that
answers wins. ``register(ctx)`` installs them on the bot's Router, which
skips a command handler whose ``match`` fails without calling it.

The handlers moved here from ``plugins/print-feedback/__init__.py`` so a
wheel install has them. That file still loads the same handlers for one
release. This package re-exports every name the plugin module had, and
assignments to it (``mock.patch``, ``monkeypatch``) reach the handler that
uses the name (``print_handy/_relocated.py``).
"""
import importlib

from ... import _relocated

# Dependency order: shared code first, then each handler.
_MODULES = (
    'common',
    'model_link',
    'upload_wait',
    'print_again',
    'guided',
    'design',
    'color_selection',
    'print_next',
    'runout_intake',
    'coldpull',
    'nozzle',
    'maintenance',
    'spool',
    'purchase',
    'loaded_spools',
    'lookup',
    'source',
    'macros',
    'firmware',
    'calibration',
    'plate_calibrate',
    'troubleshoot',
    'exclude',
    'print_stop',
    'print_resume',
    'roast',
    'print_reboot',
    'bed_down',
    'typed_quantity',
    'upload_collection',
    'edit',
)
_homes = tuple(importlib.import_module(f"{__name__}.{name}") for name in _MODULES)

# Dispatch order (the plugin's _DISPATCH_HOOKS).
HANDLERS = tuple(importlib.import_module(f"{__name__}.{name}") for name in (
    'guided',
    'design',
    'maintenance',
    'coldpull',
    'lookup',
    'source',
    'troubleshoot',
    'macros',
    'firmware',
    'calibration',
    'typed_quantity',
    'upload_wait',
    'upload_collection',
    'model_link',
    'color_selection',
    'print_next',
    'runout_intake',
    'exclude',
    'print_stop',
    'print_resume',
    'roast',
    'print_again',
    'edit',
    'loaded_spools',
    'purchase',
    'spool',
    'print_reboot',
    'bed_down',
    'live',
    'plate_calibrate',
    'nozzle',
))
_DISPATCH_HOOKS = tuple(module.handle for module in HANDLERS)
_COMMAND_GATES = {module.handle: (module.match.command, module.match.with_args) for module in HANDLERS
                   if getattr(module.match, "with_args", None) is not None}

_INTERFACE = {"match", "handle", "logger", "_LIVE_SESSION"}
for _home in _homes:
    for _name, _value in vars(_home).items():
        if not _name.startswith("__") and _name not in _INTERFACE:
            globals().setdefault(_name, _value)
del _home, _name, _value
logger = _homes[0].logger


def register(ctx):
    """Install the handler set on the bot's Router (the only host left)."""
    for hook in _DISPATCH_HOOKS:
        ctx.register_hook('pre_gateway_dispatch', hook)
    if hasattr(ctx, 'register_platform_handler'):
        ctx.register_platform_handler('discord', alert_button_handlers)  # noqa: F821


_relocated.keep_old_path(__name__, *_homes, live=("_LIVE_SESSION",))
