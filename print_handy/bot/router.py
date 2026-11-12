"""The command router: the chat handlers in their dispatch order.

The handlers live in ``print_handy.bot.handlers`` (#1323); the package's
``register(ctx)`` is called with the router itself as the context, so the
handler package stays the single place a handler is added.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path

from .. import settings

logger = logging.getLogger(__name__)

# The old handler file (plugins/print-feedback), still loadable by path for one release.
PLUGIN = settings.ROOT / "plugins" / "print-feedback" / "__init__.py"
MODULE_NAME = "print_handy_print_feedback"
HANDLERS = "print_handy.bot.handlers"


def load_plugin(path=None, name=MODULE_NAME):
    """The handler module: the ``print_handy.bot.handlers`` package, which a wheel carries.

    ``path`` loads a handler file instead (the old ``plugins/print-feedback``
    layout), under ``name``.
    """
    if path is None:
        return importlib.import_module(HANDLERS)
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


def message_text(event):
    """The text every command gate tests: the hooks' own ``str(event.text).strip()``."""
    return str(getattr(event, "text", "")).strip()


class Router:
    """Collects the handlers through their register() and runs the dispatch hooks in order.

    A hook may carry a ``dispatch_gate`` (#1153 item 10): a predicate over
    the stripped message text that is false exactly when the hook would
    return None without looking at anything else (the plugin's slash-command
    hooks). The text is classified once per message and a hook whose gate
    fails is skipped, so a plain chat message no longer walks the command
    hooks one by one. Order and the first-answer-wins rule are unchanged;
    a hook with no gate always runs.
    """

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
        try:
            text = message_text(event)
        except Exception:
            text = None  # unclassifiable: every hook runs, and reports its own failure as before
        for hook in self.dispatch_hooks:
            gate = getattr(hook, "dispatch_gate", None)
            if gate is not None and text is not None and not gate(text):
                continue
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
