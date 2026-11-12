"""Motion bounds of a G-code file: the Python parser, the native accelerator and the cache.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names
are still importable from there for one release.
"""
import hashlib, json, logging, math, os, re
from pathlib import Path

from .. import settings

# The logger name stays the one the code had in model_job, so log routing
# and filters see the same records after the move.
logger = logging.getLogger("print_handy.core.model_job")


NUMBER=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'


# One G-code line as Klipper and Marlin tokenize it (API 3, S4): optional
# leading whitespace, an optional N<digits> line number, then either a
# letter-number command word (G1, G01, G 1, G92.1; the parameters may be
# glued on: G1X400) or a Klipper extended command name (SET_GCODE_OFFSET).
# The words run to the first ';' (both firmwares end the command there) or
# the end of the line. A '*checksum' needs no handling: it is not an axis
# word. Parenthesised comments are deliberately NOT stripped: Klipper has
# none and stock Marlin builds leave PAREN_COMMENTS off, so an axis word
# inside parentheses is counted, which can only widen the bounds.
_GCODE_LINE_RE = re.compile(
    r'(?im)^[^\S\n]*(?:N[0-9]+[^\S\n]*)?'
    r'(?:([GM])[^\S\n]*([0-9]+)(?:\.([0-9]+))?|([A-Z_][A-Z0-9_]*))'
    r'([^;\n]*)')
_GCODE_XYZ_RE = re.compile(r'([XYZ])\s*('+NUMBER+r')')
# G10 sets work or tool offsets when it carries an L word or an axis word
# (RepRapFirmware, Marlin CNC builds); bare G10 is firmware retraction.
_GCODE_G10_OFFSET_RE = re.compile(r'[LXYZ]\s*' + NUMBER, re.I)
# A carriage return followed by more text on the same line: Marlin ends
# the line there, Klipper does not, so the two disagree on what runs.
_GCODE_STRAY_CR_RE = re.compile(r'\r[^\S\n]*[^\s]')
_GCODE_FORBIDDEN_NAMES = frozenset({"FORCE_MOVE", "MANUAL_STEPPER", "STEPPER_BUZZ", "SET_KINEMATIC_POSITION"})
# Commands that change what later coordinates mean. The verifier cannot
# model them, so they refuse by name (RESTORE_GCODE_STATE restores saved
# offsets and the G90/G91 mode).
_GCODE_OFFSET_NAMES = frozenset({"SET_GCODE_OFFSET", "RESTORE_GCODE_STATE"})
# G numbers (leading zeros stripped) that refuse outright: homing, machine
# coordinates, curves the verifier does not trace, direct stepper moves,
# probing moves, and the work coordinate systems.
_GCODE_G_FORBIDDEN = frozenset({"28", "53", "5", "6", "38"})
_GCODE_G_WORK_OFFSETS = frozenset({"54", "55", "56", "57", "58", "59"})
# G numbers the verifier interprets; any of them with a subcode (G92.1,
# G91.1, G1.5) refuses, because firmwares disagree on what it means.
_GCODE_G_KNOWN = frozenset({"0", "1", "2", "3", "5", "6", "10", "20", "28", "38", "53", "90", "91", "92"}
                           | _GCODE_G_WORK_OFFSETS)


_GCODE_XYZ_RE_I = re.compile(r'([XYZ])\s*(' + NUMBER + r')', re.I)


def _gcode_motion_bounds_python(text):
    """Return fail-closed XYZ bounds for ordinary slicer G-code.

    One finditer pass over the line-start commands (#925), with the
    position/bounds state in locals. API 3 (S4) reads lines the way the
    firmware does: a ';' ends the command even with no space before it
    (``G91;rel``), parameters may be glued to the command (``G1X400``), an
    ``N<digits>`` line number may lead, the command number may carry
    leading zeros, and commands that shift later coordinates
    (SET_GCODE_OFFSET, M206, M428, G10 L/axis, G54-G59) refuse by name.
    """
    if "\r" in text:
        stray = _GCODE_STRAY_CR_RE.search(text)
        if stray is not None:
            lineno = text.count("\n", 0, stray.start()) + 1
            raise RuntimeError(f"line {lineno}: carriage return inside a line; firmwares split it "
                               "differently, so bounds are ambiguous")
    absolute = True
    pos_x = pos_y = pos_z = float("nan")
    low_x = low_y = low_z = float("inf")
    high_x = high_y = high_z = float("-inf")
    recognized = 0
    for match in _GCODE_LINE_RE.finditer(text):
        letter, number, sub, name, words = match.groups()
        if letter is None:
            name = name.upper()
            if name in _GCODE_FORBIDDEN_NAMES:
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: unsupported motion-affecting command {name}")
            if name in _GCODE_OFFSET_NAMES:
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: {name} changes coordinate offsets; bounds are ambiguous")
            continue
        if number[0] == "0" and len(number) > 1:
            number = number.lstrip("0") or "0"
        if letter == "M" or letter == "m":
            # Marlin dispatches on the number alone, whatever the subcode.
            if (number == "428" or number == "206" and _GCODE_XYZ_RE_I.search(words)):
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: M{number} changes coordinate offsets; bounds are ambiguous")
            continue
        if sub is not None:
            if number in _GCODE_G_KNOWN:
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: unsupported motion-affecting command G{number}.{sub}")
            continue
        if number == "1" or number == "0":
            found = False
            for xyz_match in _GCODE_XYZ_RE_I.finditer(words):
                axis, value_text = xyz_match.groups()
                found = True
                value = float(value_text)
                if axis == "X" or axis == "x":
                    if absolute:
                        pos_x = value
                    else:
                        if pos_x != pos_x:
                            lineno = text.count("\n", 0, match.start()) + 1
                            raise RuntimeError(f"line {lineno}: relative X motion before an absolute X position")
                        pos_x += value
                    if pos_x < low_x:
                        low_x = pos_x
                    if pos_x > high_x:
                        high_x = pos_x
                elif axis == "Y" or axis == "y":
                    if absolute:
                        pos_y = value
                    else:
                        if pos_y != pos_y:
                            lineno = text.count("\n", 0, match.start()) + 1
                            raise RuntimeError(f"line {lineno}: relative Y motion before an absolute Y position")
                        pos_y += value
                    if pos_y < low_y:
                        low_y = pos_y
                    if pos_y > high_y:
                        high_y = pos_y
                else:
                    if absolute:
                        pos_z = value
                    else:
                        if pos_z != pos_z:
                            lineno = text.count("\n", 0, match.start()) + 1
                            raise RuntimeError(f"line {lineno}: relative Z motion before an absolute Z position")
                        pos_z += value
                    if pos_z < low_z:
                        low_z = pos_z
                    if pos_z > high_z:
                        high_z = pos_z
            if found:
                recognized += 1
        elif number == "90":
            absolute = True
        elif number == "91":
            absolute = False
        elif number == "92":
            if _GCODE_XYZ_RE.search(words.upper()):
                lineno = text.count("\n", 0, match.start()) + 1
                raise RuntimeError(f"line {lineno}: G92 changes XYZ coordinates; bounds are ambiguous")
        elif number == "2" or number == "3":
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: arc motion is unsupported by the bounds verifier")
        elif number == "20":
            # Inch coordinates read as millimetres would shrink every bound
            # about 25x and pass a print that cannot fit (#952 phase 2).
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: inch units (G20) are unsupported by the bounds verifier")
        elif number in _GCODE_G_FORBIDDEN:
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: unsupported motion-affecting command G{number}")
        elif number in _GCODE_G_WORK_OFFSETS or number == "10" and _GCODE_G10_OFFSET_RE.search(words):
            lineno = text.count("\n", 0, match.start()) + 1
            raise RuntimeError(f"line {lineno}: G{number} changes coordinate offsets; bounds are ambiguous")
    if not recognized:
        raise RuntimeError("no bounded XYZ motion found")
    missing = [axis for axis, low in (("X", low_x), ("Y", low_y), ("Z", low_z)) if low == float("inf")]
    if missing:
        raise RuntimeError("missing bounded motion for axis " + ", ".join(missing))
    return {"x": (low_x, high_x), "y": (low_y, high_y), "z": (low_z, high_z)}


# --- Native acceleration (#952, D1) ---------------------------------------
# The Rust/PyO3 extension in native/bounds parses the same semantics an
# order of magnitude faster. It is an accelerator only, never load
# bearing: _gcode_motion_bounds_python above stays forever as the
# fallback and the test oracle, and every install without the built
# extension (pip-from-clone, raw checkouts) runs it exactly as before.
# The dispatcher uses the native path only when ALL of these hold:
#   * the PRINT_HANDY_BOUNDS_IMPL setting allows it ("auto"/"native";
#     "python" forces the fallback; unset means "auto"),
#   * the input text is pure ASCII — the Python patterns are
#     Unicode-aware and the crate implements the ASCII subset, so any
#     non-ASCII input takes the Python parser and equivalence at this
#     API is exact by construction,
#   * the extension imports, its API_VERSION matches
#     _BOUNDS_NATIVE_API_VERSION, and a golden self-check (known inputs
#     with known outcomes, refusals included) passes on first use.
# A native refusal (BoundsRefusal, a RuntimeError) propagates unchanged.
# Any other native error falls back to the Python parser for that input,
# is logged, and disables the native path for the rest of the process.
# The bounds semantics version. It is the native API version (a native
# build of any other version is never used) and part of the bounds cache
# key and the full-verification marker, so a verdict reached by an older
# parser is never reused. Bump it on any semantic change.
# 2: G20 refused (#952 phase 2). 3: firmware tokenization (';' ends a
# command, glued parameters, N line numbers) and offset commands refused (S4).
BOUNDS_PARSER_VERSION = 3
_BOUNDS_NATIVE_API_VERSION = BOUNDS_PARSER_VERSION
_BOUNDS_SELF_CHECK_OK = (
    ("G90\nG1 X1 Y2 Z3\n", {"x": (1.0, 1.0), "y": (2.0, 2.0), "z": (3.0, 3.0)}),
    ("G90\nG1 X-5 Y2 Z3\nG91\nG1 X5\n", {"x": (-5.0, 0.0), "y": (2.0, 2.0), "z": (3.0, 3.0)}),
    # API 3: ';' ends a command, glued parameters, line numbers.
    ("G90\nG1 X10 Y1 Z1\nG91;rel\nG1 X100\nN7 G1X100*12\n",
     {"x": (10.0, 210.0), "y": (1.0, 1.0), "z": (1.0, 1.0)}),
)
_BOUNDS_SELF_CHECK_REFUSALS = (
    ("G90\nG2 X1 Y1\n", "line 2: arc motion is unsupported by the bounds verifier"),
    ("G90\nG1 X1 Y1 Z1\nSET_GCODE_OFFSET X=300\n",
     "line 3: SET_GCODE_OFFSET changes coordinate offsets; bounds are ambiguous"),
    ("G20;inch\nG1 X1 Y1 Z1\n", "line 1: inch units (G20) are unsupported by the bounds verifier"),
)
_native_bounds_module = None
_native_bounds_broken = False
# A Rust panic inside the extension reaches Python as PyO3's
# PanicException, which derives from BaseException, not Exception: the
# fallback must catch it too, or a native defect would escape the
# verifier instead of degrading to the Python parser. Only these are
# never swallowed.
_BOUNDS_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _bounds_impl_setting():
    return os.environ.get("PRINT_HANDY_BOUNDS_IMPL", "auto").strip().lower()


def _load_native_bounds():
    """Return the verified native module, or None when unavailable.

    The result is cached after the first successful verification; a
    module that fails the import, the version handshake or the golden
    self-check is never retried in this process.
    """
    global _native_bounds_module, _native_bounds_broken
    if _native_bounds_module is not None or _native_bounds_broken:
        return _native_bounds_module
    try:
        from ..core import _bounds_native as module
        if module.API_VERSION != _BOUNDS_NATIVE_API_VERSION:
            raise ValueError(
                f"native bounds API version {module.API_VERSION!r} "
                f"!= expected {_BOUNDS_NATIVE_API_VERSION}")
        for text, expected in _BOUNDS_SELF_CHECK_OK:
            if module.parse_bounds(text) != expected:
                raise ValueError(f"native bounds self-check mismatch for {text!r}")
        for text, message in _BOUNDS_SELF_CHECK_REFUSALS:
            try:
                module.parse_bounds(text)
            except module.BoundsRefusal as refusal:
                if str(refusal) != message:
                    raise ValueError(f"native bounds self-check refusal mismatch: {refusal}")
            else:
                raise ValueError(f"native bounds self-check refusal did not raise for {text!r}")
    except BaseException as exc:
        if isinstance(exc, _BOUNDS_NEVER_SWALLOW):
            raise
        _native_bounds_broken = True
        if _bounds_impl_setting() == "native":
            raise RuntimeError(f"native bounds parser required but unavailable: {exc}") from exc
        logger.debug("native bounds parser unavailable, using Python parser: %s", exc)
        return None
    _native_bounds_module = module
    return module


def _reset_native_bounds_cache():
    """Forget the cached native module (tests only)."""
    global _native_bounds_module, _native_bounds_broken
    _native_bounds_module = None
    _native_bounds_broken = False


def _native_bounds_result(module, text):
    """Run the native parser and shape-check its result.

    Returns the bounds dict, or None when the native result is
    malformed (the caller then falls back). BoundsRefusal propagates:
    it is a legitimate verifier refusal, identical to the Python one.
    """
    result = module.parse_bounds(text)
    if not isinstance(result, dict) or set(result) != {"x", "y", "z"}:
        return None
    bounds = {}
    for axis in "xyz":
        low, high = result[axis]
        bounds[axis] = (float(low), float(high))
    return bounds


def gcode_motion_bounds(text):
    """Return fail-closed XYZ bounds for ordinary slicer G-code.

    Dispatches to the native parser when it is verified available and
    the input is pure ASCII; otherwise (and on any unexpected native
    error) runs the Python implementation, whose outcomes — results and
    refusal messages alike — are the contract both paths meet.
    """
    global _native_bounds_broken
    setting = _bounds_impl_setting()
    required = setting == "native"
    if setting != "python" and text.isascii():
        if _native_bounds_broken and required:
            # "native" means native or nothing, for every call, not only
            # the first: never degrade silently after an earlier failure.
            raise RuntimeError(
                "native bounds parser required but unavailable: "
                "disabled after an earlier failure in this process")
        module = None if _native_bounds_broken else _load_native_bounds()
        if module is not None:
            try:
                bounds = _native_bounds_result(module, text)
            except module.BoundsRefusal:
                raise
            except BaseException as exc:
                if isinstance(exc, _BOUNDS_NEVER_SWALLOW):
                    raise
                _native_bounds_broken = True
                if required:
                    raise RuntimeError(f"native bounds parser required but failed: {exc!r}") from exc
                logger.warning(
                    "native bounds parser failed (%r); using the Python parser", exc)
            else:
                if bounds is not None:
                    return bounds
                _native_bounds_broken = True
                if required:
                    raise RuntimeError("native bounds parser required but returned a malformed result")
                logger.warning(
                    "native bounds parser returned a malformed result; using the Python parser")
    return _gcode_motion_bounds_python(text)


BOUNDS_CACHE = Path(os.environ.get("PRINT_HANDY_BOUNDS_CACHE", settings.state_dir()/"gcode-bounds-cache"))
# One small JSON per distinct plate ever verified; without a cap the directory
# grows forever (#952). 512 entries is far above any real working set (a hit
# needs the same bytes re-verified) and keeps the directory a non-issue.
BOUNDS_CACHE_MAX_ENTRIES = 512


def _cached_bounds(hit):
    """Bounds from a cache entry, or None when the entry is not trustworthy.

    A hit must carry exactly two finite numbers per axis, low <= high: the
    pre-#952 reader returned any JSON value as tuples, so a hand-edited or
    corrupt entry with string values came back as tuples of characters and
    only failed later, as a comparison crash inside verify. An entry
    written by another parser version (S5) is not trustworthy either: the
    parser it came from may have read the same bytes differently.
    """
    if not isinstance(hit, dict) or hit.get("parser") != BOUNDS_PARSER_VERSION:
        return None
    bounds = {}
    for axis in "xyz":
        pair = hit.get(axis)
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        low, high = pair
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (low, high)):
            return None
        if low > high:
            return None
        bounds[axis] = (float(low), float(high))
    return bounds


def _prune_bounds_cache():
    """Drop the oldest cache entries beyond BOUNDS_CACHE_MAX_ENTRIES."""
    try:
        entries = sorted(BOUNDS_CACHE.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for stale in entries[:max(0, len(entries) - BOUNDS_CACHE_MAX_ENTRIES)]:
        try:
            stale.unlink()
        except OSError:
            pass


def cached_motion_bounds(raw, text=None):
    """gcode_motion_bounds keyed by content sha256.

    Slicing verifies offline first; print-now then reverifies the same bytes.
    Reparsing a 24 MB plate costs ~30 s per pass, so identical bytes reuse the
    stored result. Failures are never cached, so a bad file still fails closed.
    An entry that does not validate (_cached_bounds) is treated as a miss and
    recomputed; writes prune the directory to BOUNDS_CACHE_MAX_ENTRIES (#952).
    The key carries BOUNDS_PARSER_VERSION and the entry records it (S5), so
    an upgrade never reuses a verdict an older parser reached on the same
    bytes; pre-version entries (plain ``<sha>.json``) are never read again
    and age out through the pruning.
    """
    digest = hashlib.sha256(raw).hexdigest()
    entry = BOUNDS_CACHE/f"{digest}.p{BOUNDS_PARSER_VERSION}.json"
    try:
        hit = _cached_bounds(json.loads(entry.read_text()))
        if hit is not None:
            return hit
    except (OSError, ValueError):
        pass
    bounds = gcode_motion_bounds(text if text is not None else raw.decode(errors="ignore"))
    try:
        BOUNDS_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_suffix(".tmp")
        tmp.write_text(json.dumps({"parser": BOUNDS_PARSER_VERSION,
                                   **{axis: list(bounds[axis]) for axis in "xyz"}}))
        tmp.replace(entry)
        _prune_bounds_cache()
    except OSError:
        pass
    return bounds
