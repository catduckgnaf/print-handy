"""Triangle-painted (Bambu/Orca paint_color, PrusaSlicer mmu_segmentation) 3MF support.

paint_color is a TriangleSelector tree serialized as hex nibbles read from the
END of the string. Per node: low 2 bits = split side count (0 = leaf). A split
node is followed by split+1 children. A leaf stores its state in the high 2
bits; 3 means "state >= 3": then a run of F nibbles (15 each) and one final
nibble give state - 3, so painted models past 17 filaments read. State 0 is the
object's base extruder; state N >= 1 is source filament N.

PrusaSlicer's slic3rpe:mmu_segmentation attribute carries the same
TriangleSelector encoding with the same state numbering (the corpus
prusa-mmu-project fixture's "8" is state 2, exactly as paint_color's "8" is).
Both attributes are therefore decoded by the same reader. The destination
slicer (Orca) reads paint_color, so a remap translates mmu_segmentation codes
to paint_color codes for the mapped tool and drops the Prusa attribute —
registry rule mmu-segmentation-paint-translation. A state with no slot choice
refuses, exactly as an uncovered paint_color state does.

This module decodes states, remaps them to contiguous logical tools for the
physical IFS slots chosen, and derives a one-plate package that keeps every
painted triangle. Geometry is never flattened or recolored by guesswork.
"""
import collections
import contextlib
import contextvars
import functools
import hashlib
import logging
import os
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# One read per call (#1155): a slice, an export or an inventory runs inside a
# decode scope, and inside it each .model member of a source is decompressed
# once and each painted mesh is decoded once, however many consumers ask.
# Outside a scope every read goes to the archive, exactly as before. The key
# carries the file's size and mtime, so a rewritten file is never served
# stale bytes even inside one scope. Nothing is kept across calls.
_SCOPE = contextvars.ContextVar('painted_geometry_decode_scope', default=None)


@contextlib.contextmanager
def decode_scope():
    """Share member bytes and decoded paint for the duration of one call; nests."""
    if _SCOPE.get() is not None:
        yield
        return
    token = _SCOPE.set({})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def decode_scoped(func):
    """Run func inside a decode scope (the outermost scope owns the cache)."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with decode_scope():
            return func(*args, **kwargs)
    return wrapper


def _file_key(path):
    path = Path(path)
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def read_member(archive, name):
    """archive.read(name), served from the decode scope when one is active."""
    cache = _SCOPE.get()
    filename = getattr(archive, 'filename', None)
    if cache is None or not isinstance(filename, (str, Path)):
        return archive.read(name)
    key = ('member', _file_key(filename), name)
    if key not in cache:
        cache[key] = archive.read(name)
    return cache[key]


def model_member(source, name):
    """The bytes of one member of the package at source, read once per decode scope."""
    cache = _SCOPE.get()
    if cache is not None and ('member', _file_key(source), name) in cache:
        return cache[('member', _file_key(source), name)]
    with zipfile.ZipFile(source) as archive:
        return read_member(archive, name)


def scoped(key, compute, *, source):
    """compute() once per decode scope for ``key`` and the bytes of ``source``.

    Outside a scope it always computes. The source's path, size and mtime
    are part of the key, so a rewritten file is computed again.
    """
    cache = _SCOPE.get()
    if cache is None:
        return compute()
    key = ('scoped', _file_key(source), key)
    if key not in cache:
        cache[key] = compute()
    return cache[key]


PAINT_RE = re.compile(rb'paint_color="([0-9A-Fa-f]+)"')
MMU_RE = re.compile(rb'(?:slic3rpe:)?mmu_segmentation="([0-9A-Fa-f]+)"')
_TRIANGLE_TAG_RE = re.compile(rb'<(?:\w+:)?triangle\b[^>]*>')
# Either paint attribute on one triangle tag; paint_color wins when both are
# present (no known writer emits both, and Orca reads paint_color).
ANY_PAINT_RE = re.compile(rb'(?:paint_color|slic3rpe:mmu_segmentation|mmu_segmentation)="([0-9A-Fa-f]+)"')


def _parse(code):
    nib = [int(c, 16) for c in reversed(code)]
    pos = 0

    def node():
        nonlocal pos
        if pos >= len(nib):
            raise ValueError('truncated paint_color')
        c = nib[pos]; pos += 1
        split = c & 3
        if split:
            return ('s', c, [node() for _ in range(split + 1)])
        state = c >> 2
        if state == 3:
            # Extended state (BambuStudio/OrcaSlicer TriangleSelector): a run of
            # 0xF nibbles, each adding 15, then one final nibble; state is
            # 3 + 15 * run + final. Orca writes states 18..32 as F then (n - 18),
            # which is the same rule with a run of one.
            run = 0
            while True:
                if pos >= len(nib):
                    raise ValueError('truncated paint_color')
                n = nib[pos]; pos += 1
                if n != 0xF:
                    break
                run += 1
            state = 3 + 15 * run + n
        return ('l', state)

    tree = node()
    if pos != len(nib):
        raise ValueError('trailing paint_color data')
    return tree


def _emit(tree, out):
    if tree[0] == 's':
        out.append(tree[1])
        for child in tree[2]:
            _emit(child, out)
    else:
        state = tree[1]
        if state < 3:
            out.append(state << 2)
        else:
            run, rest = divmod(state - 3, 15)
            out.append(0xC)
            out.extend([0xF] * run)
            out.append(rest)


def _encode(tree):
    out = []
    _emit(tree, out)
    return ''.join('%X' % n for n in reversed(out))


def _states(tree, counter):
    if tree[0] == 's':
        for child in tree[2]:
            _states(child, counter)
    else:
        counter[tree[1]] += 1


def decode_states(code):
    counter = collections.Counter()
    _states(_parse(code), counter)
    return counter


def remap_code(code, table):
    def walk(tree):
        if tree[0] == 's':
            return ('s', tree[1], [walk(c) for c in tree[2]])
        return ('l', table.get(tree[1], tree[1]))
    return _encode(walk(_parse(code)))


def _object_blocks(model_bytes):
    """{object_id: (start, end)} byte spans of <object> blocks."""
    return {m.group(1).decode(): (m.start(), m.end())
            for m in re.finditer(rb'<(?:\w+:)?object\b[^>]*\bid="(\d+)".*?</(?:\w+:)?object>', model_bytes, re.S)}


def _mesh_ids(model_bytes, object_id):
    """Mesh object ids reachable from object_id through same-file components."""
    blocks = _object_blocks(model_bytes)
    seen, todo, meshes = set(), [object_id], []
    while todo:
        oid = todo.pop()
        if oid in seen or oid not in blocks:
            continue
        seen.add(oid)
        body = model_bytes[slice(*blocks[oid])]
        if re.search(rb'<(?:\w+:)?mesh\b', body):
            meshes.append(oid)
        for comp in re.finditer(rb'<(?:\w+:)?component\b[^>]*>', body):
            if re.search(rb'\bp:path=|\bpath=', comp.group(0)):
                raise RuntimeError('painted export supports single-model packages only')
            todo.append(re.search(rb'objectid="(\d+)"', comp.group(0)).group(1).decode())
    return sorted(meshes)


def paint_census(source, build_object_ids=None):
    """{build_object_id: {state: leaf_count}} plus unpainted triangle counts."""
    model = model_member(source, '3D/3dmodel.model')
    blocks = _object_blocks(model)
    items = re.findall(rb'<(?:\w+:)?item\b[^>]*\bobjectid="(\d+)"', model)
    out = {}
    for raw in items:
        oid = raw.decode()
        if build_object_ids is not None and oid not in build_object_ids:
            continue
        counter, unpainted = collections.Counter(), 0
        for mid in _mesh_ids(model, oid):
            body = model[slice(*blocks[mid])]
            for tri in re.finditer(rb'<(?:\w+:)?triangle\b[^>]*>', body):
                m = PAINT_RE.search(tri.group(0)) or MMU_RE.search(tri.group(0))
                if m:
                    counter.update(decode_states(m.group(1).decode()))
                else:
                    unpainted += 1
        out[oid] = {'states': dict(counter), 'unpainted': unpainted}
    return out


def plan_tools(census, choices, base_extruders):
    """Map used source filaments to contiguous logical tools by physical slot.

    choices: {'filament:N': {'slot': ...}}. base_extruders: {object_id: N} for
    unpainted triangles and state-0 leaves. Returns (state_table, mapping,
    used_filaments) where state_table rewrites source state -> tool+1.
    """
    used = set()
    for oid, row in census.items():
        for state, count in row['states'].items():
            if count:
                used.add(state if state else base_extruders[oid])
        if row['unpainted']:
            used.add(base_extruders[oid])
    missing = [n for n in sorted(used) if f'filament:{n}' not in choices]
    if missing:
        raise RuntimeError(f'painted plate uses source filament(s) {missing} with no slot choice')
    slots = sorted({choices[f'filament:{n}']['slot'] for n in used})
    if not 1 <= len(slots) <= 4:
        raise RuntimeError('painted plate needs one to four IFS slots')
    table = {n: slots.index(choices[f'filament:{n}']['slot']) + 1 for n in sorted(used)}
    mapping = {str(i): slot for i, slot in enumerate(slots)}
    return table, mapping, sorted(used)


def rewrite_paint(model_bytes, mesh_ids, table):
    """Remap paint states inside the given mesh objects only."""
    blocks = _object_blocks(model_bytes)
    spans = sorted(blocks[m] for m in mesh_ids)
    pieces, last = [], 0
    for start, end in spans:
        body = model_bytes[start:end]
        body = PAINT_RE.sub(lambda m: b'paint_color="' + remap_code(m.group(1).decode(), table).encode() + b'"', body)
        pieces += [model_bytes[last:start], body]
        last = end
    pieces.append(model_bytes[last:])
    return b''.join(pieces)


def rewrite_paint_bytes(data, table):
    """Remap every paint code in raw model bytes, translating mmu to paint_color.

    Byte-level, so everything outside the two paint attributes — geometry,
    support/seam paint, material properties — stays byte-for-byte. A code whose
    state the table does not map keeps its state (the caller validates the
    table covers every state present before writing).

    A triangle never leaves carrying two paint attributes: when it holds both
    paint_color and mmu_segmentation, paint_color wins (the same precedence
    paint_census and leaf_states read) and the mmu attribute is dropped.
    """
    def _code(match):
        return b'paint_color="' + remap_code(match.group(1).decode(), table).encode() + b'"'

    def _triangle(match):
        tag = match.group(0)
        if PAINT_RE.search(tag):
            tag = re.sub(rb'\s+(?:slic3rpe:)?mmu_segmentation="[0-9A-Fa-f]+"', b'', tag)
            return PAINT_RE.sub(_code, tag)
        return MMU_RE.sub(_code, tag)

    return _TRIANGLE_TAG_RE.sub(_triangle, data)


def paint_states_in(data):
    """Every paint state named by raw model bytes (both attributes), decoded: {state: leaves}.

    Inside a decode scope the census of one byte string is decoded once: the
    remapper, its staged-package re-read and the remap integrity check
    (#1525) all ask for the same members, and a painted model can be 100+ MB.
    The key is a digest of the bytes outside the <build> element, which
    holds no paint: a package moved onto the bed (slice_route translates
    its build items) shares the census of the package it was moved from,
    and any other change is decoded again.
    """
    cache = _SCOPE.get()
    key = None
    if cache is not None and (b'paint_color=' in data or b'mmu_segmentation=' in data):
        digest = hashlib.blake2b(digest_size=20)
        # The core spec puts <build> after <resources>, so the last one is it.
        start = data.rfind(b'<build')
        if start >= 0 and data[start + 6:start + 7] not in (b'>', b' ', b'\n', b'\r', b'\t'):
            start = -1
        end = data.find(b'</build>', start) if start >= 0 else -1
        if end >= 0:
            digest.update(memoryview(data)[:start])
            digest.update(memoryview(data)[end + len(b'</build>'):])
        else:
            digest.update(data)
        key = ('paint_states', digest.digest())
        if key in cache:
            return dict(cache[key])
    counter = collections.Counter()
    for regex in (PAINT_RE, MMU_RE):
        for match in regex.finditer(data):
            counter.update(decode_states(match.group(1).decode()))
    if key is not None:
        cache[key] = dict(counter)
    return dict(counter)


# --- Native acceleration (#1715) ---------------------------------------
# The Rust/PyO3 census in the bounds extension (native/bounds/src/
# paint.rs) counts every mesh object of a .model member in one pass —
# the per-triangle paint decode below is 78% of a cold painted
# inventory (the #1715 profile), and the census runs it well over an
# order of magnitude faster. It is an accelerator only, never load bearing:
# _leaf_states_python stays forever as the fallback and the test
# oracle, and every install without the built extension (pip-from-
# clone, raw checkouts) runs it exactly as before. The dispatcher uses
# the native path only when ALL of these hold:
#   * the PRINT_HANDY_BOUNDS_IMPL setting allows it ("auto"/"native";
#     "python" forces the fallback; unset means "auto") — the same
#     setting and rules as the bounds parser and the bed scan, whose
#     crate this shares,
#   * the extension imports, its PAINT_CENSUS_VERSION matches
#     PAINT_CENSUS_VERSION, and a golden self-check (known members
#     with known censuses, a per-mesh decode error included) passes
#     on first use.
# A mesh whose paint decode fails carries the Python decode's exact
# error message in its census row, and leaf_states raises it as the
# same ValueError, for that mesh only — a bad mesh never poisons the
# member's other meshes, exactly as the per-mesh Python decode behaves.
# Any other native error falls back to the Python counting for that
# member, is logged, and disables the native path for the rest of the
# process. Parts with triangle_ranges always take the Python path (see
# leaf_states): that is the documented design, not a fallback.
# The paint census semantics version. It is the native handshake
# version (a native build of any other version is never used). Bump it
# on any semantic change to the census contract.
PAINT_CENSUS_VERSION = 1
_PAINT_SELF_CHECK = (
    (b'<model><resources>'
     b'<object id="1"><mesh><triangles>'
     b'<triangle v1="0" v2="1" v3="2" paint_color="4"/>'
     b'<triangle v1="0" v2="1" v3="2" paint_color="8"/>'
     b'<triangle v1="0" v2="1" v3="2"/>'
     b'</triangles></mesh></object>'
     b'<object id="2"><mesh><triangles>'
     b'<triangle v1="0" v2="1" v3="2" paint_color="1"/>'
     b'</triangles></mesh></object>'
     b'</resources></model>',
     {'1': {'states': {1: 1, 2: 1}, 'unpainted': 1},
      '2': {'error': 'truncated paint_color'}}),
    (b'<model><resources>'
     b'<p:object id="7"><mesh><triangles>'
     b'<p:triangle v1="0" v2="1" v3="2" paint_color="841"/>'
     b'<p:triangle v1="0" v2="1" v3="2" paint_color="0FC"/>'
     b'<p:triangle v1="0" v2="1" v3="2" slic3rpe:mmu_segmentation="8"/>'
     b'</triangles></mesh></p:object>'
     b'</resources></model>',
     {'7': {'states': {1: 1, 2: 2, 18: 1}, 'unpainted': 0}}),
)
_native_paint_module = None
_native_paint_broken = False
# A Rust panic inside the extension reaches Python as PyO3's
# PanicException, which derives from BaseException, not Exception: the
# fallback must catch it too, or a native defect would escape instead
# of degrading to the Python counting. Only these are never swallowed.
_PAINT_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _paint_impl_setting():
    return os.environ.get("PRINT_HANDY_BOUNDS_IMPL", "auto").strip().lower()


def _load_native_paint():
    """Return the verified native module, or None when unavailable.

    The result is cached after the first successful verification; a
    module that fails the import, the version handshake or the golden
    self-check is never retried in this process.
    """
    global _native_paint_module, _native_paint_broken
    if _native_paint_module is not None or _native_paint_broken:
        return _native_paint_module
    try:
        from . import _bounds_native as module
        if getattr(module, "PAINT_CENSUS_VERSION", None) != PAINT_CENSUS_VERSION:
            raise ValueError(
                f"native paint census version {getattr(module, 'PAINT_CENSUS_VERSION', None)!r} "
                f"!= expected {PAINT_CENSUS_VERSION}")
        for member, expected in _PAINT_SELF_CHECK:
            if module.paint_census(member) != expected:
                raise ValueError("native paint census self-check mismatch")
    except BaseException as exc:
        if isinstance(exc, _PAINT_NEVER_SWALLOW):
            raise
        _native_paint_broken = True
        if _paint_impl_setting() == "native":
            raise RuntimeError(f"native paint census required but unavailable: {exc}") from exc
        logger.debug("native paint census unavailable, using Python counting: %s", exc)
        return None
    _native_paint_module = module
    return module


def _reset_native_paint_cache():
    """Forget the cached native module (tests only)."""
    global _native_paint_module, _native_paint_broken
    _native_paint_module = None
    _native_paint_broken = False


def _census_shape_ok(census):
    """True when a native census result has exactly the contract shape."""
    if not isinstance(census, dict):
        return False
    for mesh_id, row in census.items():
        if not isinstance(mesh_id, str) or not isinstance(row, dict):
            return False
        if set(row) == {'error'}:
            if not isinstance(row['error'], str):
                return False
        elif set(row) == {'states', 'unpainted'}:
            if not isinstance(row['states'], dict) or not isinstance(row['unpainted'], int):
                return False
            if any(not isinstance(state, int) or not isinstance(count, int)
                   for state, count in row['states'].items()):
                return False
        else:
            return False
    return True


def _member_census(source, model_path):
    """The native census of one .model member, or None when unavailable.

    {mesh_id: {'states': {state: leaves}, 'unpainted': n} or
    {'error': message}}. Inside a decode scope the census is computed
    once per (source, member) and shared by every leaf_states call in
    the scope, so a flow that asks about many parts of one member pays
    one native pass; outside a scope it is computed per call and
    nothing is kept, exactly like the member bytes themselves.
    """
    global _native_paint_broken
    setting = _paint_impl_setting()
    required = setting == "native"
    if setting == "python":
        return None
    if _native_paint_broken:
        if required:
            raise RuntimeError("native paint census required but unavailable: "
                               "disabled after an earlier failure in this process")
        return None
    module = _load_native_paint()
    if module is None:
        return None
    cache = _SCOPE.get()
    key = ('census', _file_key(source), model_path) if cache is not None else None
    if key is not None and key in cache:
        return cache[key]
    model = model_member(source, model_path)
    try:
        census = module.paint_census(model)
    except BaseException as exc:
        if isinstance(exc, _PAINT_NEVER_SWALLOW):
            raise
        _native_paint_broken = True
        if required:
            raise RuntimeError(f"native paint census required but failed: {exc!r}") from exc
        logger.warning("native paint census failed (%r); using the Python counting", exc)
        return None
    if not _census_shape_ok(census):
        _native_paint_broken = True
        if required:
            raise RuntimeError("native paint census required but returned a malformed result")
        logger.warning("native paint census returned a malformed result; using the Python counting")
        return None
    if key is not None:
        cache[key] = census
    return census


def leaf_states(source, part):
    """{'states': {state: leaves}, 'unpainted': n} for one inventory part's mesh.

    part['ancestry'] ends at the mesh object: [[model_path, object_id], ...].
    A row with 'triangle_ranges' (a PrusaSlicer volume split out of its
    object's one mesh, #1260) counts only the triangles in those ranges:
    a whole-mesh census cannot answer for a subset, so ranges parts
    always take the Python counting — the documented design (#1715),
    not a fallback from a fault. Every other part is served from the
    native member census when it is verified available, and from the
    Python counting otherwise; the two are held to identical results
    by the differential tests.
    """
    model_path, mesh_id = part['ancestry'][-1]
    if part.get('triangle_ranges'):
        return _leaf_states_python(source, part)
    cache = _SCOPE.get()
    key = ('leaf', _file_key(source), model_path, str(mesh_id), ()) if cache is not None else None
    if key is not None and key in cache:
        row = cache[key]
        return {'states': dict(row['states']), 'unpainted': row['unpainted']}
    census = _member_census(source, model_path)
    if census is None:
        return _leaf_states_python(source, part)
    if str(mesh_id) not in census:
        raise RuntimeError(f'painted part mesh {mesh_id} not found in {model_path}')
    row = census[str(mesh_id)]
    if 'error' in row:
        raise ValueError(row['error'])
    if key is not None:
        cache[key] = {'states': dict(row['states']), 'unpainted': row['unpainted']}
    return {'states': dict(row['states']), 'unpainted': row['unpainted']}


def _leaf_states_python(source, part):
    """leaf_states counted in Python: the permanent fallback and oracle.

    part['ancestry'] ends at the mesh object: [[model_path, object_id], ...].
    A row with 'triangle_ranges' (a PrusaSlicer volume split out of its
    object's one mesh, #1260) counts only the triangles in those ranges.
    """
    model_path, mesh_id = part['ancestry'][-1]
    ranges = tuple(tuple(r) for r in part.get('triangle_ranges') or ())
    cache = _SCOPE.get()
    key = ('leaf', _file_key(source), model_path, str(mesh_id), ranges) if cache is not None else None
    if key is not None and key in cache:
        row = cache[key]
        return {'states': dict(row['states']), 'unpainted': row['unpainted']}
    model = model_member(source, model_path)
    if cache is not None:
        blocks_key = ('blocks', _file_key(source), model_path)
        if blocks_key not in cache:
            cache[blocks_key] = _object_blocks(model)
        span = cache[blocks_key].get(str(mesh_id))
    else:
        span = _object_blocks(model).get(str(mesh_id))
    if span is None:
        raise RuntimeError(f'painted part mesh {mesh_id} not found in {model_path}')
    body = model[slice(*span)]
    counter, unpainted = collections.Counter(), 0
    for index, tri in enumerate(re.finditer(rb'<(?:\w+:)?triangle\b[^>]*>', body)):
        if ranges and not any(first <= index <= last for first, last in ranges):
            continue
        m = PAINT_RE.search(tri.group(0)) or MMU_RE.search(tri.group(0))
        if m:
            counter.update(decode_states(m.group(1).decode()))
        else:
            unpainted += 1
    row = {'states': dict(counter), 'unpainted': unpainted}
    if key is not None:
        cache[key] = row
        return {'states': dict(row['states']), 'unpainted': row['unpainted']}
    return row
