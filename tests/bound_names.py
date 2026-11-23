"""Patch a name everywhere a module group binds it.

Code split across modules binds a shared name in each module that uses it:
``from ..jobs.common import _mr`` in ``launch/gates.py`` and
``launch/print_now.py`` gives each its own ``_mr``. Patching only the
defining module leaves the others calling the real one. These helpers patch
the name in the given module and in every other module of its group that
binds the same object, so a test fakes a helper once for the whole group:

* the job code: ``print_handy.core.model_job`` and ``print_handy.{gcode,jobs,launch}.*``;
* the intake: ``print_handy.core.prepared_intake`` and ``print_handy.intake.*``;
* the chat handlers: every ``print_handy.bot.handlers.*`` module.

``patch_bound`` takes ``mock.patch.object``'s arguments and works as a
context manager (``with patch_bound(...) as fake:``) or with ``start()`` and
``stop()``. ``setattr_bound`` is the ``monkeypatch.setattr`` form.
"""
import importlib
import pkgutil
import sys
from unittest import mock

_UNSET = object()

GROUPS = {
    "jobs": ("print_handy.core.model_job", "print_handy.gcode.", "print_handy.jobs.", "print_handy.launch."),
    "intake": ("print_handy.core.prepared_intake", "print_handy.intake."),
    "handlers": ("print_handy.bot.handlers.",),
}
_PACKAGES = {"jobs": ("print_handy.gcode", "print_handy.jobs", "print_handy.launch"),
             "intake": ("print_handy.intake",), "handlers": ("print_handy.bot.handlers",)}
_loaded = set()


def _group(module):
    name = module.__name__
    for group, prefixes in GROUPS.items():
        if any(name == p or (p.endswith(".") and name.startswith(p)) for p in prefixes):
            return group
    raise ValueError(f"{name} is in no patch group; use mock.patch.object")


def _members(group):
    """Every imported module of ``group`` (its packages' modules are imported once, so none is missed)."""
    if group not in _loaded:
        for package in _PACKAGES[group]:
            pkg = importlib.import_module(package)
            for info in pkgutil.iter_modules(pkg.__path__, package + "."):
                importlib.import_module(info.name)
        if group == "jobs":
            importlib.import_module("print_handy.core.model_job")
        if group == "intake":
            importlib.import_module("print_handy.core.prepared_intake")
        _loaded.add(group)
    prefixes = GROUPS[group]
    return [m for n, m in sorted(sys.modules.items()) if m is not None
            and any(n == p or (p.endswith(".") and n.startswith(p)) for p in prefixes)]


def binders(module, name):
    """``module`` first, then every other module of its group whose ``name`` is the same object."""
    value = module.__dict__.get(name, _UNSET)
    if value is _UNSET:
        return [module]
    return [module] + [m for m in _members(_group(module))
                       if m is not module and m.__dict__.get(name, _UNSET) is value]


class patch_bound:
    """``mock.patch.object(module, name, ...)`` applied to every binder of the name in the group."""

    def __init__(self, module, name, *args, **kwargs):
        self.module, self.name, self.args, self.kwargs = module, name, args, kwargs
        self._patches = []

    def start(self):
        first = mock.patch.object(self.module, self.name, *self.args, **self.kwargs)
        others = binders(self.module, self.name)[1:]
        new = first.start()
        self._patches = [first]
        for other in others:
            patch = mock.patch.object(other, self.name, new)
            patch.start()
            self._patches.append(patch)
        return new

    def stop(self):
        while self._patches:
            self._patches.pop().stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def setattr_bound(monkeypatch, module, name, value, raising=True):
    """``monkeypatch.setattr(module, name, value)`` for every binder of the name in the group."""
    for target in binders(module, name):
        monkeypatch.setattr(target, name, value, raising=raising)
