"""The post-slice bed check: the first G0-G3 move that leaves the bed (#1392).

``_first_off_bed_python`` is the contract and stays forever as the fallback
and test oracle. On a 25 MB G-code it costs about 5 s, so the dispatcher
uses the Rust port in the bounds extension (``print_handy.core._bounds_native``,
``native/bounds/src/bed_scan.rs``) under the same rules as the bounds parser
(``print_handy.gcode.bounds``): only for pure-ASCII text, only when the
extension imports, reports the expected ``BED_SCAN_VERSION`` and passes a
golden self-check, and never when ``PRINT_HANDY_BOUNDS_IMPL=python``. Any
native error falls back to Python, is logged, and disables the native path
for the rest of the process; ``PRINT_HANDY_BOUNDS_IMPL=native`` makes a
missing, mismatched or failing extension an error instead. Non-ASCII text
takes the Python loop under every setting, ``native`` included, exactly as
the bounds parser does: the Rust port implements the ASCII subset of the
patterns, so this is how the two stay equal, not a fallback from a fault.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# The bounds parser's setting and rules (print_handy.gcode.bounds), repeated here
# so importing the bed check never imports bounds, which reads settings at import.
_BOUNDS_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _bounds_impl_setting():
    return os.environ.get("PRINT_HANDY_BOUNDS_IMPL", "auto").strip().lower()

MOVE = re.compile(r"^\s*G[0-3]\b([^;]*)", re.I)
XY_WORD = re.compile(r"\b([XY])\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)

# The bed-scan semantics version; bump with any change to the contract.
BED_SCAN_VERSION = 1
_SELF_CHECK_BED = (220.0, 220.0)
_SELF_CHECK = (
    ("G1 X10 Y20 E1\nG0 X220 Y0\n", None),
    ("G1 X10 Y20\nG1 Y-1\nG1 X999\n", (10.0, -1.0)),
    ("G10 X-5\nG92 X-5\n; G1 X-5\nG1X-5\nG1 E1X-5\nG1 X5 ; X-5\n", None),
    ("M117 a\rG1 X-.5\n", (-0.5, 0.0)),
)
_native_module = None
_native_broken = False


def _first_off_bed_python(text, bed):
    """The first (x, y) a G0-G3 move puts off the bed, or None.

    X and Y start at 0 and only X/Y words on move lines change them, read as
    absolute (the check is a slice sanity gate, not a motion model; the toolpath
    motion checks in slice_guard handle printers whose start code leaves the bed).
    """
    x = y = 0.0
    for line in text.splitlines():
        m = MOVE.match(line)
        if not m:
            continue
        moved = False
        for axis, value in XY_WORD.findall(m.group(1)):
            if axis in "Xx":
                x = float(value)
            else:
                y = float(value)
            moved = True
        if moved and (x < 0 or y < 0 or x > bed[0] or y > bed[1]):
            return x, y
    return None


def _load_native():
    global _native_module, _native_broken
    if _native_module is not None or _native_broken:
        return _native_module
    try:
        from ..core import _bounds_native as module
        if getattr(module, "BED_SCAN_VERSION", None) != BED_SCAN_VERSION:
            raise ValueError(f"native bed scan version {getattr(module, 'BED_SCAN_VERSION', None)!r} "
                             f"!= expected {BED_SCAN_VERSION}")
        for text, expected in _SELF_CHECK:
            got = module.first_off_bed(text, *_SELF_CHECK_BED)
            if (None if got is None else tuple(got)) != expected:
                raise ValueError(f"native bed scan self-check mismatch for {text!r}: {got!r}")
    except BaseException as exc:
        if isinstance(exc, _BOUNDS_NEVER_SWALLOW):
            raise
        _native_broken = True
        if _bounds_impl_setting() == "native":
            raise RuntimeError(f"native bed scan required but unavailable: {exc}") from exc
        logger.debug("native bed scan unavailable, using Python: %s", exc)
        return None
    _native_module = module
    return module


def _reset_native_cache():
    """Forget the cached native module (tests only)."""
    global _native_module, _native_broken
    _native_module = None
    _native_broken = False


def first_off_bed(text, bed):
    """The first (x, y) a G0-G3 move puts off a bed[0] x bed[1] mm bed, or None."""
    global _native_broken
    setting = _bounds_impl_setting()
    required = setting == "native"
    if setting != "python" and text.isascii():
        if _native_broken and required:
            raise RuntimeError("native bed scan required but unavailable: "
                               "disabled after an earlier failure in this process")
        module = None if _native_broken else _load_native()
        if module is not None:
            try:
                result = module.first_off_bed(text, float(bed[0]), float(bed[1]))
                if result is not None:
                    x, y = result
                    result = (float(x), float(y))
            except BaseException as exc:
                if isinstance(exc, _BOUNDS_NEVER_SWALLOW):
                    raise
                _native_broken = True
                if required:
                    raise RuntimeError(f"native bed scan required but failed: {exc!r}") from exc
                logger.warning("native bed scan failed (%r); using Python", exc)
            else:
                return result
    return _first_off_bed_python(text, bed)
