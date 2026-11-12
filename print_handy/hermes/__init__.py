"""The old name of ``print_handy.runtime``, kept for one minor version (#1323, #1157).

The listener and print-control package was called ``print_handy.hermes``
after an install method that no longer exists. It is ``print_handy.runtime``
now. Importing the old name warns (DeprecationWarning) and still works:
``print_handy.hermes.<name>`` is the very module ``print_handy.runtime.<name>``,
so ``mock.patch("print_handy.hermes.print_stop.x")`` patches the real code.
``python -m print_handy.hermes.<name>`` (service files written before 2.3.0)
runs ``print_handy.runtime.<name>`` and warns that the service file is stale.
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


def _real_name(fullname):
    return _NEW + fullname[len(_OLD):]


class _RenamedModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve ``print_handy.hermes.X`` to the already-imported ``print_handy.runtime.X``."""

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_OLD + "."):
            return None
        real = importlib.util.find_spec(_real_name(fullname))
        if real is None:
            return None
        return importlib.util.spec_from_loader(
            fullname, self, origin=real.origin, is_package=real.submodule_search_locations is not None)

    # ``python -m print_handy.hermes.X`` goes through runpy, which never imports
    # the module: it asks the loader for the code and runs it as ``__main__``.
    # Hand it the real module's code so old service files keep starting.
    def is_package(self, fullname):
        real = importlib.util.find_spec(_real_name(fullname))
        return real is not None and real.submodule_search_locations is not None

    def get_code(self, fullname):
        new = _real_name(fullname)
        warnings.warn(f"python -m {fullname} is deprecated; run python -m {new} instead "
                      "(re-run the installer to update service files); the old name is removed after 2.3.x",
                      FutureWarning, stacklevel=2)
        real = importlib.util.find_spec(new)
        return real.loader.get_code(new)

    def get_source(self, fullname):
        new = _real_name(fullname)
        return importlib.util.find_spec(new).loader.get_source(new)

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
