"""The old name of ``print_handy.runtime``, kept for one minor version (#1323, #1157).

The listener and print-control package was called ``print_handy.hermes``
after an install method that no longer exists. It is ``print_handy.runtime``
now. Importing the old name warns (DeprecationWarning) and still works:
``print_handy.hermes.<name>`` is the very module ``print_handy.runtime.<name>``,
so ``mock.patch("print_handy.hermes.print_stop.x")`` patches the real code.
Remove this package after 2.3.x.
"""
import importlib
import importlib.abc
import importlib.util
import sys
import warnings

_OLD = __name__
_NEW = "print_handy.runtime"

warnings.warn(f"{_OLD} was renamed {_NEW}; the old name is removed after 2.3.x",
              DeprecationWarning, stacklevel=2)

# A package, so the import system looks for submodules here; the finder below answers.
__path__ = []


class _RenamedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve ``print_handy.hermes.X`` to the already-imported ``print_handy.runtime.X``."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(_OLD + "."):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        module = importlib.import_module(_NEW + spec.name[len(_OLD):])
        spec.loader_state = module.__spec__
        return module

    def exec_module(self, module):
        # The real module ran when it was imported under its new name; the import
        # system has just stamped the alias spec on it, so put its own spec back.
        module.__spec__ = module.__spec__.loader_state


if not any(isinstance(finder, _RenamedModules) for finder in sys.meta_path):
    sys.meta_path.insert(0, _RenamedModules())


def __getattr__(name):
    try:
        return importlib.import_module(f"{_OLD}.{name}")
    except ModuleNotFoundError as exc:
        raise AttributeError(f"module {_OLD!r} has no attribute {name!r}") from exc
