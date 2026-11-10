"""The printer registry loads vendor families on first use, with the eager scan's results."""
import json
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

from print_handy import printers

ROOT = Path(__file__).resolve().parents[2]


def _fresh(code):
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


FAMILIES = ("import json, sys; print(json.dumps(sorted({m.split('.')[2] for m in sys.modules "
            "if m.startswith('print_handy.printers.') and m.split('.')[2] not in ('base', 'job_checks')})))")


def test_importing_the_package_loads_no_vendor_family():
    assert _fresh("import print_handy.printers\n" + FAMILIES) == []


def test_a_lookup_by_key_loads_only_that_family():
    code = ("from print_handy import printers\n"
            "m = printers.MODELS['klipper.generic']\n"
            "assert printers.model('klipper.generic') is m and 'klipper.generic' in printers.MODELS\n"
            "assert printers.VENDORS['klipper'] is m.vendor\n" + FAMILIES)
    assert _fresh(code) == ["klipper"]


def _eager_scan():
    """The pre-lazy discovery, kept as the oracle: every vendor package, every MODEL module."""
    import importlib
    from dataclasses import replace
    vendors, found = {}, {}
    for brand in pkgutil.iter_modules(printers.__path__):
        if not brand.ispkg:
            continue
        package = importlib.import_module(f"print_handy.printers.{brand.name}")
        vendors[brand.name] = package.VENDOR
        for module in pkgutil.iter_modules(package.__path__):
            if module.ispkg:
                continue
            model = getattr(importlib.import_module(f"{package.__name__}.{module.name}"), "MODEL", None)
            if model is not None:
                found[model.key] = model if model.vendor is not None else replace(model, vendor=package.VENDOR)
    return dict(sorted(vendors.items())), dict(sorted(found.items()))


def test_the_full_tables_match_the_eager_scan_in_content_and_order():
    vendors, models = _eager_scan()
    assert list(printers.VENDORS) == list(vendors)
    assert list(printers.MODELS) == list(models)
    assert dict(printers.MODELS) == models and dict(printers.VENDORS) == vendors
    assert len(printers.MODELS) == len(models)
    # Objects are stable: a lookup after the full scan returns the scanned object.
    key = next(iter(models))
    assert printers.MODELS[key] is printers.model(key) is dict(printers.MODELS.items())[key]


@pytest.mark.parametrize("key", ["nope.nothing", "klipper.nothing", "base.x", "flashforge", "", None,
                                 "klipper.__init__", "flashforge.__init__"])
def test_unknown_keys_are_refused_as_before(key):
    assert key not in printers.MODELS
    with pytest.raises(KeyError):
        printers.MODELS[key]
    assert printers.MODELS.get(key) is None


def test_unknown_vendor_and_resolve_key_errors_are_unchanged():
    assert "base" not in printers.VENDORS and "nope" not in printers.VENDORS
    with pytest.raises(ValueError, match="model must be one of"):
        printers.resolve_key("nope.nothing")
    with pytest.raises(ValueError, match="vendor must be one of"):
        printers.resolve_key("ad5x", vendor="nope")
    assert printers.resolve_key("ad5x", vendor="flashforge") == "flashforge.ad5x"


def test_tables_can_still_be_patched_by_tests(monkeypatch):
    model = printers.MODELS["klipper.generic"]
    monkeypatch.setitem(printers.MODELS, "klipper.test_only", model)
    assert printers.MODELS["klipper.test_only"] is model and "klipper.test_only" in list(printers.MODELS)
    monkeypatch.undo()
    assert "klipper.test_only" not in printers.MODELS
