"""The chat command handlers (#1323, proposal 5 of #1160).

Each handler module declares ``match(text)`` (its dispatch gate: a slash
command, or every message for a free-text handler) and ``handle(event,
gateway)``. ``HANDLERS`` is the dispatch order: the first handler that
answers wins. ``register(ctx)`` installs them on the bot's Router, which
skips a command handler whose ``match`` fails without calling it.

Each name lives in its own module (``print_handy.bot.handlers.common``,
``print_handy.bot.handlers.print_stop`` and so on); the package itself
holds only the dispatch order and ``register``.
"""
import importlib

# Dependency order: shared code first, then each handler.
_MODULES = (
    'common',
    'fix',
    'model_link',
    'upload_wait',
    'print_again',
    'guided',
    'design',
    'color_selection',
    'slice_result',
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
    'print_status',
    'print_pause',
    'print_resume',
    'roast',
    'print_reboot',
    'bed_down',
    'typed_quantity',
    'upload_collection',
    'edit',
    'cost',
    'stats',
)
_homes = tuple(importlib.import_module(f"{__name__}.{name}") for name in _MODULES)

# Dispatch order: the first handler that answers wins.
HANDLERS = tuple(importlib.import_module(f"{__name__}.{name}") for name in (
    'guided',
    'fix',
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
    # Cost before the intake handlers (#1661): a file dropped with a
    # cost ask is priced read-only and never enters intake; a bare
    # file drop's gate does not match, so intake is untouched.
    'cost',
    'upload_collection',
    'model_link',
    'color_selection',
    'print_next',
    'runout_intake',
    'exclude',
    'print_stop',
    'print_status',
    'print_pause',
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
    'slice_result',
    'nozzle',
    'stats',
))
_DISPATCH_HOOKS = tuple(module.handle for module in HANDLERS)
_COMMAND_GATES = {module.handle: (module.match.command, module.match.with_args) for module in HANDLERS
                   if getattr(module.match, "with_args", None) is not None}

from .common import alert_button_handlers  # noqa: E402


def register(ctx):
    """Install the handler set on the bot's Router (the only host left)."""
    for hook in _DISPATCH_HOOKS:
        ctx.register_hook('pre_gateway_dispatch', hook)
    if hasattr(ctx, 'register_platform_handler'):
        ctx.register_platform_handler('discord', alert_button_handlers)
