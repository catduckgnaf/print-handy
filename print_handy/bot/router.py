"""The command router: the print-feedback module's own handlers, in its own order.

The module's ``register(ctx)`` is called with the router itself as the context, so the
handler module stays the single place a handler is added.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from .. import settings

logger = logging.getLogger(__name__)

PLUGIN = settings.ROOT / "plugins" / "print-feedback" / "__init__.py"
MODULE_NAME = "print_handy_print_feedback"


def load_plugin(path=PLUGIN, name=MODULE_NAME):
    if name in sys.modules:
        return sys.modules[name]
    if not Path(path).is_file():
        # The handlers live outside the package (plugins/print-feedback), so
        # an install that ships only the wheel has no command handlers at
        # all. Fail loudly and name what is missing (#1146) instead of
        # surfacing a bare FileNotFoundError from the import machinery.
        raise RuntimeError(
            f"the print-feedback command handlers were not found at {path}: "
            "this install is missing plugins/print-feedback/__init__.py, so "
            "no chat commands can be served. Run from a source checkout or "
            "an image that includes the plugins folder.")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class Router:
    """Collects the plugin's hooks through its register() and runs the dispatch hooks in order."""

    def __init__(self, plugin=None):
        self.plugin = plugin if plugin is not None else load_plugin()
        self.hooks: dict[str, list] = {}
        self.platform_handlers: dict[str, list] = {}
        self.plugin.register(self)

    # The plugin context API (the subset Print Handy's plugin uses).
    def register_hook(self, name, func):
        self.hooks.setdefault(name, []).append(func)

    def register_platform_handler(self, platform, func):
        self.platform_handlers.setdefault(platform, []).append(func)

    @property
    def dispatch_hooks(self):
        return list(self.hooks.get("pre_gateway_dispatch", []))

    def handler_names(self):
        return [getattr(h, "__name__", repr(h)) for h in self.dispatch_hooks]

    def attach_platform(self, platform, client, adapter):
        """Run the plugin's platform handlers (alert, insertion and plate buttons) against our client."""
        for func in self.platform_handlers.get(platform, []):
            func(client, adapter)

    async def dispatch(self, event, gateway):
        """The first hook that answers wins, as in the gateway. None: no handler took the message."""
        for hook in self.dispatch_hooks:
            try:
                result = hook(event, gateway)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                logger.exception("handler %s failed", getattr(hook, "__name__", hook))
                return {"action": "skip", "reason": f"{getattr(hook, '__name__', 'handler')} failed"}
            if result is not None:
                return result
        return None
