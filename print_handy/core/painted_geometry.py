"""Triangle-painted (Bambu/Orca paint_color, PrusaSlicer mmu_segmentation) 3MF support.

paint_color is a TriangleSelector tree serialized as hex nibbles read from the
END of the string. Per node: low 2 bits = split side count (0 = leaf). A split
node is followed by split+1 children. A leaf stores its state in the high 2
bits; 3 means "state >= 3", and the next nibble holds state - 3. State 0 is the
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
import re
import zipfile
from pathlib import Path

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
            if pos >= len(nib):
                raise ValueError('truncated paint_color')
            state = nib[pos] + 3; pos += 1
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
            if state - 3 > 15:
                raise ValueError('paint state too large')
            out.extend([0xC, state - 3])


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
    with zipfile.ZipFile(source) as archive:
        model = archive.read('3D/3dmodel.model')
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
    """Every paint state named by raw model bytes (both attributes), decoded."""
    counter = collections.Counter()
    for regex in (PAINT_RE, MMU_RE):
        for match in regex.finditer(data):
            counter.update(decode_states(match.group(1).decode()))
    return dict(counter)


def leaf_states(source, part):
    """{'states': {state: leaves}, 'unpainted': n} for one inventory part's mesh.

    part['ancestry'] ends at the mesh object: [[model_path, object_id], ...].
    """
    model_path, mesh_id = part['ancestry'][-1]
    with zipfile.ZipFile(source) as archive:
        model = archive.read(model_path)
    span = _object_blocks(model).get(str(mesh_id))
    if span is None:
        raise RuntimeError(f'painted part mesh {mesh_id} not found in {model_path}')
    body = model[slice(*span)]
    counter, unpainted = collections.Counter(), 0
    for tri in re.finditer(rb'<(?:\w+:)?triangle\b[^>]*>', body):
        m = PAINT_RE.search(tri.group(0)) or MMU_RE.search(tri.group(0))
        if m:
            counter.update(decode_states(m.group(1).decode()))
        else:
            unpainted += 1
    return {'states': dict(counter), 'unpainted': unpainted}
