"""Printer profiles, vendor first, then printer. See base.py for the contract and docs/PRINTER-PROFILES.md.

    from print_handy import printers
    printers.current().build_volume()
    printers.MODELS["klipper.generic"].defaults()
    printers.VENDORS["bambulab"].delivery

Every vendor package (flashforge/, klipper/, sovol/, octoprint/, bambulab/) is scanned. Its __init__ defines
VENDOR; each module in it that defines MODEL is registered under its "vendor.printer" key and inherits VENDOR.
Adding a printer is one new file in its vendor package plus its templates.
"""
from __future__ import annotations

import importlib
import pkgutil
import threading
from collections.abc import MutableMapping
from dataclasses import replace

from .base import MultiColorUnsupported, PrinterAdapter, PrinterModel, VendorProfile

# Legacy-job provenance only — NOT an install default. Jobs written before the
# model was recorded were sliced for the AD5X (the only printer supported then),
# so a job that records no model reads as an AD5X job. The default model for an
# install that names none lives in the shipped config trees (config/home.yaml,
# config/docker.yaml); see #649.
LEGACY_JOB_MODEL = "flashforge.ad5x"


# Other spellings of a vendor key that config may use. "bambu" reads as "bambulab".
VENDOR_ALIASES = {"bambu": "bambulab", "bambu_lab": "bambulab", "bambulabs": "bambulab"}


def _vendor(name):
    """Import and check one vendor package; None when ``name`` is not one."""
    package = importlib.import_module(f"{__name__}.{name}")
    if not hasattr(package, "__path__"):
        return None
    vendor = getattr(package, "VENDOR", None)
    if not isinstance(vendor, VendorProfile):
        raise RuntimeError(f"{package.__name__}: a vendor package must define VENDOR = VendorProfile(...)")
    if vendor.key != name:
        raise RuntimeError(f"{package.__name__}: VENDOR key {vendor.key!r} must be {name}")
    return vendor


def _model(vendor, name):
    """Import and check one printer module of a vendor package; None when it defines no MODEL."""
    module = importlib.import_module(f"{__name__}.{vendor.key}.{name}")
    if hasattr(module, "__path__"):
        return None
    model = getattr(module, "MODEL", None)
    if model is None:
        return None
    if model.key != f"{vendor.key}.{name}":
        raise RuntimeError(f"{module.__name__}: MODEL key {model.key!r} must be {vendor.key}.{name}")
    if model.vendor is None:
        model = replace(model, vendor=vendor)  # a printer module that does not name its vendor
    elif model.vendor != vendor:
        raise RuntimeError(f"{module.__name__}: MODEL vendor {model.vendor.key!r} must be "
                           f"the {vendor.key} package VENDOR")
    return model


class _Registry:
    """The vendor and model tables, filled on first use instead of at import.

    Importing every vendor family costs a few tenths of a second, and most
    processes (each heavy intake worker among them) need one printer. A
    lookup by full key loads just that vendor package and printer module;
    iterating, measuring or editing a table runs the full scan once, with
    the same checks and the same sorted order the eager scan had. Objects
    loaded by key are reused by the full scan, so identity is stable.
    """

    def __init__(self):
        self.vendors: dict[str, VendorProfile] = {}
        self.models: dict[str, PrinterModel] = {}
        self.complete = False
        self.lock = threading.RLock()

    def scan(self):
        with self.lock:
            if self.complete:
                return
            vendors, found = dict(self.vendors), dict(self.models)
            for brand in pkgutil.iter_modules(__path__):
                if not brand.ispkg:
                    continue
                vendor = vendors.get(brand.name) or _vendor(brand.name)
                vendors[vendor.key] = vendor
                package = importlib.import_module(f"{__name__}.{brand.name}")
                for module in pkgutil.iter_modules(package.__path__):
                    if module.ispkg:
                        continue
                    key = f"{brand.name}.{module.name}"
                    model = found.get(key) or _model(vendor, module.name)
                    if model is not None:
                        found[key] = model
            self.vendors = dict(sorted(vendors.items()))
            self.models = dict(sorted(found.items()))
            self.complete = True

    def vendor(self, name):
        """One vendor by key, loading only its package. None when unknown (after a full scan)."""
        with self.lock:
            if name in self.vendors or self.complete:
                return self.vendors.get(name)
            if _plain_name(name) and _is_child(__path__, name, package=True):
                vendor = _vendor(name)
                if vendor is not None:
                    self.vendors[name] = vendor
                    return vendor
            self.scan()
            return self.vendors.get(name)

    def model(self, key):
        """One model by "vendor.printer" key, loading only its module. None when unknown."""
        with self.lock:
            if key in self.models or self.complete:
                return self.models.get(key)
            head, dot, tail = str(key).partition(".") if isinstance(key, str) else ("", "", "")
            if dot and _plain_name(tail):
                vendor = self.vendor(head)
                if vendor is not None and not self.complete:
                    package = importlib.import_module(f"{__name__}.{head}")
                    if _is_child(package.__path__, tail, package=False):
                        model = _model(vendor, tail)
                        if model is not None:
                            self.models[key] = model
                            return model
            self.scan()
            return self.models.get(key)


def _plain_name(name):
    return isinstance(name, str) and name.isidentifier() and not name.startswith("__")


def _is_child(path, name, *, package):
    """True when ``name`` is a module (or package) the full scan would list under ``path``."""
    return any(info.name == name and info.ispkg == package for info in pkgutil.iter_modules(path))


_REGISTRY = _Registry()


def _discover():
    """Compatibility hook returning the fully validated vendor and model tables."""
    _REGISTRY.scan()
    return _REGISTRY.vendors, _REGISTRY.models


class _Table(MutableMapping):
    """A dict-like view of one registry table (VENDORS or MODELS)."""

    def __init__(self, table, one):
        self._table, self._one = table, one

    def _all(self):
        _REGISTRY.scan()
        return getattr(_REGISTRY, self._table)

    def __getitem__(self, key):
        found = self._one(key)
        if found is None:
            raise KeyError(key)
        return found

    def __contains__(self, key):
        return self._one(key) is not None

    def __iter__(self):
        return iter(list(self._all()))

    def __len__(self):
        return len(self._all())

    def __setitem__(self, key, value):
        self._all()[key] = value

    def __delitem__(self, key):
        del self._all()[key]

    def __repr__(self):
        return repr(self._all())


VENDORS: MutableMapping[str, VendorProfile] = _Table("vendors", _REGISTRY.vendor)
MODELS: MutableMapping[str, PrinterModel] = _Table("models", _REGISTRY.model)


def vendor_key(name):
    """Canonical vendor key for a configured vendor name or alias."""
    name = str(name or "").strip().lower()
    return VENDOR_ALIASES.get(name, name)


# Bare printer names that resolved to one model before another vendor added the
# same name (#638 added sovol_marlin.sv06 beside sovol.sv06).
_LEGACY_BARE_NAMES = {"sv06": "sovol.sv06"}


def resolve_key(model, vendor=None):
    """The registered "vendor.printer" key for a [printer] model value, with an optional [printer] vendor.

    Accepts the full key ("flashforge.ad5x", what every existing config has), a vendor alias in it
    ("bambu.a1"), or the printer part alone ("ad5x"), taken with `vendor` or, when it names exactly one
    printer, on its own. A vendor that disagrees with a full key is refused rather than guessed.
    """
    raw = str(model or "").strip()
    want_vendor = vendor_key(vendor) if vendor else None
    if want_vendor and want_vendor not in VENDORS:
        raise ValueError(f"[printer] vendor must be one of {', '.join(VENDORS)}, got {vendor!r}")
    if "." in raw:
        head, tail = raw.split(".", 1)
        key = f"{vendor_key(head)}.{tail}"
        if want_vendor and vendor_key(head) != want_vendor:
            raise ValueError(f"[printer] vendor {vendor!r} does not match model {raw!r}")
    elif want_vendor:
        key = f"{want_vendor}.{raw}"
    else:
        matches = [k for k, m in MODELS.items() if m.printer_key == raw]
        if len(matches) > 1 and raw in _LEGACY_BARE_NAMES:
            # A bare name that was unique before a later vendor reused it keeps
            # resolving the way existing configs were written against.
            matches = [_LEGACY_BARE_NAMES[raw]]
        if len(matches) > 1:
            raise ValueError(f"[printer] model {raw!r} exists for several vendors ({', '.join(matches)}); "
                             "set [printer] vendor or use the full vendor.printer key")
        key = matches[0] if matches else raw
    if key not in MODELS:
        raise ValueError(f"[printer] model must be one of {', '.join(MODELS)}, got {model!r}")
    return key


def model(key):
    """The PrinterModel registered under a "brand.model" key."""
    if key not in MODELS:
        raise ValueError(f"unknown printer model {key!r}; expected one of {', '.join(MODELS)}")
    return MODELS[key]


def get(key, config=None):
    """Adapter for a model key, built on `config` (default: the process settings)."""
    selected = model(key)
    return selected.adapter(config, selected)


def current(config=None):
    """The adapter selected by [printer] model."""
    from .. import settings
    config = config or settings.current()
    return get(config["printer"]["model"], config)


class JobModelMismatch(RuntimeError):
    """A job was prepared for a different printer model than the one configured."""


# JOB.json written by 2.0.2 and 2.0.3 records printer_family instead of printer_model.
LEGACY_FAMILY = {"flashforge_zmod": "flashforge.ad5x", "sovol_klipper": "sovol.sv06",
                 "generic_klipper": "klipper.generic"}


def job_model(job):
    """Model a JOB.json was prepared for. Jobs older than either field are flashforge.ad5x."""
    if not isinstance(job, dict):
        return LEGACY_JOB_MODEL
    return job.get("printer_model") or LEGACY_FAMILY.get(job.get("printer_family")) or LEGACY_JOB_MODEL


def check_job_model(job, config=None):
    """Raise JobModelMismatch unless the job's model is the configured one."""
    want = current(config).key
    have = job_model(job)
    if have != want:
        raise JobModelMismatch(f"this job was prepared for {have}, but the configured printer model is {want}")
