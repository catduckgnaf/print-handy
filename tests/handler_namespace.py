"""Names the removed print-feedback plugin file used to expose.

The handlers live in ``print_handy.bot.handlers``, one module each. ``plugin``
resolves each name to the module that defines it. Assigning a name patches
every handler module that bound the same object, which is what the old
single namespace did when a test replaced a helper.

The names also sit on this object's ``__dict__``. ``mock.patch`` only
restores an attribute it found there; a lookup that exists only via
``__getattr__`` is deleted on the way out.
"""
from __future__ import annotations

import importlib
import pkgutil

from tests.bound_names import binders

_modules = None
_index = None


def _load():
    global _modules, _index
    if _modules is not None:
        return
    import print_handy.bot.handlers as pkg
    mods = [pkg]
    for info in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
        mods.append(importlib.import_module(info.name))
    index = {}
    for mod in mods:
        for name, obj in mod.__dict__.items():
            if name.startswith("__"):
                continue
            if getattr(obj, "__module__", None) == mod.__name__:
                index[name] = mod
            else:
                index.setdefault(name, mod)
    _modules = mods
    _index = index


def _owner(name):
    _load()
    mod = _index.get(name)
    if mod is not None and name in getattr(mod, "__dict__", {}):
        return mod
    for mod in _modules:
        if name in mod.__dict__:
            _index[name] = mod
            return mod
    return None


def _assign(mod, name, value):
    try:
        targets = binders(mod, name)
    except ValueError:
        targets = [mod]
    for target in targets:
        setattr(target, name, value)


class HandlerPlugin:
    """Attribute access over ``print_handy.bot.handlers``."""

    def __init__(self):
        _load()
        for name, mod in _index.items():
            if name in mod.__dict__:
                self.__dict__[name] = mod.__dict__[name]

    def __getattr__(self, name):
        mod = _owner(name)
        if mod is None:
            raise AttributeError(name)
        value = mod.__dict__[name]
        self.__dict__[name] = value
        return value

    def __setattr__(self, name, value):
        self.__dict__[name] = value
        mod = _owner(name)
        if mod is None:
            return
        _assign(mod, name, value)

    def __delattr__(self, name):
        self.__dict__.pop(name, None)
        mod = _owner(name)
        if mod is None:
            return
        try:
            targets = binders(mod, name)
        except ValueError:
            targets = [mod]
        for target in targets:
            if name in target.__dict__:
                delattr(target, name)

    def __dir__(self):
        _load()
        return sorted(set(_index) | set(self.__dict__))


plugin = HandlerPlugin()
