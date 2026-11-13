"""Old module paths for code that moved into smaller modules (#1323).

When code moves out of a large module (``print_handy.core.model_job``,
``print_handy.core.prepared_intake``), the old module re-exports every
moved name for one release, so ``from print_handy.core.model_job import
gcode_motion_bounds`` keeps working. A plain re-export is not enough for
callers that *assign* to the old module, which is what ``mock.patch`` and
``monkeypatch.setattr`` do: before the move, assigning
``model_job.BOUNDS_CACHE`` changed what every function in that module saw;
after it, the function lives elsewhere and would keep its own binding.

``keep_old_path`` makes the old module forward such assignments, so the
two stay one name:

* setting or deleting a name on the old module also sets or deletes it
  on every new home whose binding is the same object (the name was
  re-exported, or both modules imported the same thing);
* names the new home rebinds at run time (``global`` state) are not
  copied into the old module at all; reading them on the old module
  returns the new home's current value, and writing them writes there.

The forwarding is the only behavior; every function still runs from its
new home. This module and every ``keep_old_path`` call are removed in
2.4.0 (docs/PROJECT-REFERENCE.md, Scheduled removals in 2.4.0).
"""
import sys
import types

_UNSET = object()


class _OldPath(types.ModuleType):
    """Module type for an old path: assignments reach the moved code too."""

    def __getattr__(self, name):
        home = self.__dict__.get("_relocated_live", {}).get(name)
        if home is not None and name in home.__dict__:
            return home.__dict__[name]
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name, value):
        mine = self.__dict__
        home = mine.get("_relocated_live", {}).get(name)
        if home is not None:
            setattr(home, name, value)
            return
        current = mine.get(name, _UNSET)
        for other in mine.get("_relocated_homes", ()):
            if current is not _UNSET and other.__dict__.get(name, _UNSET) is current:
                setattr(other, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name):
        mine = self.__dict__
        home = mine.get("_relocated_live", {}).get(name)
        if home is not None:
            delattr(home, name)
            return
        current = mine.get(name, _UNSET)
        for other in mine.get("_relocated_homes", ()):
            if current is not _UNSET and other.__dict__.get(name, _UNSET) is current:
                delattr(other, name)
        super().__delattr__(name)


def keep_old_path(old_name, *homes, live=()):
    """Make module ``old_name`` forward assignments to ``homes``.

    ``homes`` are the module objects that received moved code. ``live``
    names state that a home rebinds at run time (``global``); the old
    module must not hold its own copy of those, so do not re-export them.
    Each live name is looked up in the home that defines it.
    """
    module = sys.modules[old_name]
    known = list(module.__dict__.get("_relocated_homes", ()))
    for home in homes:
        if home not in known:
            known.append(home)
    owners = dict(module.__dict__.get("_relocated_live", {}))
    for name in live:
        if name in module.__dict__:
            raise ValueError(f"{old_name}.{name} is live state; do not re-export it")
        found = [home for home in known if name in home.__dict__]
        if len(found) != 1:
            raise ValueError(f"live name {name!r} must be defined by exactly one home")
        owners[name] = found[0]
    module.__class__ = _OldPath
    module.__dict__["_relocated_homes"] = tuple(known)
    module.__dict__["_relocated_live"] = owners
    return module
