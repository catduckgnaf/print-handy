"""Multi-file models from file-list sites: which files are parts, which are versions, and one project.

Printables, Thingiverse, MyMiniFactory and the other file-list sites publish a
model as a list of STL/3MF files. Those files are usually the parts of one
model (a box, its lid, a latch), sometimes with a few versions of one part
(a lid with or without a logo, an M3 or M4 insert). This module answers three
questions for intake, with no network or chat I/O:

- ``plan(names)``: which files are plain parts, which form a version group
  (ask once per group), and whether one 3MF probably holds the whole assembly.
- ``parse_file_numbers(text, count)``: the typed "1 3" reply of "Choose files".
- ``combine(target, sources, bed)``: one geometry-only 3MF with one object per
  part, laid out without overlaps (on one bed when they fit), which the
  existing fit, rearrange, split and per-part color machinery then handles.

Version detection is deliberately conservative: two names are versions of one
part only when they differ by a single token that names an obvious version
(``sign``/``nosign``, ``with_logo``/``no_logo``, ``v1``/``v2``, ``easy``/
``hard``, ``supported``/``presupported``, ``M3``/``M4``, ``100mm``/``150mm``).
Anything else, including ``left``/``right`` and ``part1``/``part2``, is a part
that is printed. A wrong "part" costs one extra object the operator can see
in the prompt; a wrong "version" would silently drop a needed part.
"""
from __future__ import annotations

import re
from pathlib import Path

MODEL_EXTS = ('.stl', '.3mf')

_SPLIT = re.compile(r'[\s_\-+()\[\],]+|(?<!\d)\.|\.(?!\d)')
_CAMEL = re.compile(r'(?<=[a-z])(?=[A-Z])')
_NEGATE = ('no', 'without', 'wo', 'sans')
_WITH = ('with', 'w')
_QUANTITY = re.compile(r'(?:x(\d{1,2})|(\d{1,2})x)')
_VERSION = re.compile(r'(?:v|ver|version|rev|r)\d+[a-z]?(?:\.\d+)*')
_SIZE = re.compile(r'(?:m\d+(?:\.\d+)?|\d+(?:\.\d+)?(?:mm|cm|in|inch|deg|pct|%)|\d+(?:\.\d+)?x\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?)')
_FAMILIES = (
    frozenset({'supported', 'presupported', 'unsupported', 'nosupport', 'nosupports', 'supportless',
               'supportfree'}),
    frozenset({'easy', 'medium', 'hard', 'normal', 'standard', 'expert', 'tight', 'loose', 'snug'}),
    frozenset({'xs', 's', 'm', 'l', 'xl', 'xxl', 'tiny', 'mini', 'small', 'medium', 'large', 'big',
               'huge', 'short', 'long', 'tall', 'wide', 'narrow', 'thin', 'thick', 'regular'}),
)
_PAIRED_PARTS = frozenset({'l', 'r', 'left', 'right'})


def _stem(name):
    stem = Path(str(name or '')).name
    for ext in MODEL_EXTS:
        if stem.lower().endswith(ext):
            return stem[:-len(ext)]
    return stem


def tokens(name):
    """Lower-case name tokens, with "no logo"/"with logo" folded to one -logo/+logo token.

    A count token (``x2``, ``2x``) is not part of the name; ``quantity_hint`` reads it.
    """
    raw = [t for t in _SPLIT.split(_CAMEL.sub('_', _stem(name)).lower()) if t]
    out, i = [], 0
    while i < len(raw):
        tok = raw[i]
        if tok in _NEGATE + _WITH and i + 1 < len(raw) and not raw[i + 1].isdigit():
            out.append(('-' if tok in _NEGATE else '+') + raw[i + 1])
            i += 2
            continue
        if not _QUANTITY.fullmatch(tok) or _SIZE.fullmatch(tok):
            out.append(tok)
        i += 1
    return out


def quantity_hint(name):
    """The count a file name asks for (``latch_x2.stl``, ``2x latch.stl``), else None."""
    for tok in _SPLIT.split(_stem(name).lower()):
        m = _QUANTITY.fullmatch(tok)
        if m and not _SIZE.fullmatch(tok):
            n = int(m[1] or m[2])
            if n > 1:
                return n
    return None


def _feature(tok):
    """(feature, present) for a token: -logo/nologo -> (logo, False), +logo/logo -> (logo, True)."""
    if tok[:1] in '+-':
        return tok[1:], tok[0] == '+'
    return tok, True


def _negated(a, b):
    fa, pa = _feature(a)
    fb, pb = _feature(b)
    if fa == fb and pa != pb:
        return True
    # "nosign" against "sign": a run-together negation, only when the bare word is the other token.
    for x, y in ((a, b), (b, a)):
        bare = _feature(y)[0]
        if len(bare) >= 3 and any(x == p + bare for p in _NEGATE):
            return True
    return False


def _versionish(tok):
    return bool(_VERSION.fullmatch(tok) or _SIZE.fullmatch(tok))


def is_version_pair(a, b):
    """Two differing name tokens that name versions of one part (not two parts)."""
    if a == b or {a, b} <= _PAIRED_PARTS:
        return False
    if _negated(a, b):
        return True
    if _versionish(a) and _versionish(b):
        return True
    return any(a in fam and b in fam for fam in _FAMILIES)


def is_version_extra(tok):
    """A token whose presence alone makes a version (``box`` vs ``box_v2``, ``box_nologo``)."""
    if tok[:1] == '-' or _versionish(tok):
        return True
    if any(tok == p + rest for p in _NEGATE for rest in [tok[len(p):]] if len(rest) >= 3):
        return True
    return any(tok in fam for fam in _FAMILIES[:1])


def _differ_by_one(ta, tb):
    """The single (a_token, b_token) difference between two token lists, else None ('' for absent)."""
    if len(ta) == len(tb):
        diff = [(x, y) for x, y in zip(ta, tb) if x != y]
        return diff[0] if len(diff) == 1 else None
    if abs(len(ta) - len(tb)) != 1:
        return None
    short, long_ = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    for i, tok in enumerate(long_):
        if long_[:i] + long_[i + 1:] == short:
            return ('', tok) if short is ta else (tok, '')
    return None


def _related(ta, tb):
    pair = _differ_by_one(ta, tb)
    if pair is None:
        return False
    a, b = pair
    if not a or not b:
        return is_version_extra(a or b)
    return is_version_pair(a, b)


def _human(tok, others):
    """Plain label for a version token: 'nosign' -> 'no sign', '-logo' -> 'no logo', 'm3' -> 'M3'."""
    if not tok:
        return 'standard'
    feature, present = _feature(tok)
    if tok[:1] in '+-':
        return ('with ' if present else 'no ') + feature
    for other in others:
        bare = _feature(other)[0]
        for p in _NEGATE:
            if len(bare) >= 3 and tok == p + bare:
                return f'{"no" if p in ("no", "wo") else p} {bare}'
    if re.fullmatch(r'm\d+(?:\.\d+)?', tok):
        return tok.upper()
    return tok


def _shown_name(toks, common, fallback):
    words = [t for t in toks if t not in common]
    text = ' '.join(_human(t, []) if t[:1] in '+-' else t for t in words)
    return text or fallback


def plan(names):
    """Group a model's file names into parts and version groups.

    Returns ``{'parts': [i, ...], 'groups': [group, ...], 'assembly_3mf': i | None,
    'names': {i: shown name}, 'quantities': {i: n}}``. Each group is
    ``{'subject': 'cap', 'options': [{'label': 'sign', 'files': [i, ...]}, ...]}``;
    groups whose options read the same (box/lid both in 100mm and 150mm) are
    asked as one question. Indexes refer to ``names``.
    """
    names = [str(n) for n in names]
    toks = [tokens(n) for n in names]
    common = set(toks[0]).intersection(*toks[1:]) if len(toks) > 1 else set()
    shown = {i: _shown_name(t, common, _stem(names[i])) for i, t in enumerate(toks)}
    # Union-find over "differs by one version token".
    parent = list(range(len(names)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if Path(names[i]).suffix.lower() == Path(names[j]).suffix.lower() and _related(toks[i], toks[j]):
                parent[find(j)] = find(i)
    members = {}
    for i in range(len(names)):
        members.setdefault(find(i), []).append(i)
    parts, raw_groups = [], []
    for root in sorted(members):
        group = members[root]
        if len(group) == 1:
            parts.append(group[0])
            continue
        shared = set(toks[group[0]]).intersection(*(toks[i] for i in group[1:]))
        subject = ' '.join(t for t in toks[group[0]] if t in shared and t not in common and not t.isdigit())
        options = []
        for i in group:
            own = [t for t in toks[i] if t not in shared]
            peers = [t for j in group if j != i for t in toks[j] if t not in shared]
            label = ' '.join(_human(t, peers) for t in own) if own else _human('', peers)
            options.append({'label': label, 'files': [i]})
        raw_groups.append({'subject': subject or 'version', 'options': options})
    groups = {}
    for g in raw_groups:
        key = tuple(o['label'] for o in g['options'])
        if key in groups:
            merged = groups[key]
            merged['subject'] = merged['subject'] + ' and ' + g['subject']
            for mine, theirs in zip(merged['options'], g['options']):
                mine['files'] = mine['files'] + theirs['files']
        else:
            groups[key] = g
    threemf = [i for i, n in enumerate(names) if n.lower().endswith('.3mf')]
    stls = [i for i, n in enumerate(names) if n.lower().endswith('.stl')]
    quantities = {i: q for i, n in enumerate(names) if (q := quantity_hint(n))}
    return {'parts': parts, 'groups': list(groups.values()),
            'assembly_3mf': threemf[0] if len(threemf) == 1 and stls else None,
            'names': shown, 'quantities': quantities}


def parse_file_numbers(text, count):
    """The file numbers typed for "Choose files" ("1 3", "1,3", "2-4", "all"); ValueError otherwise."""
    text = str(text or '').strip().casefold()
    if text in ('all', 'all files', 'everything'):
        return list(range(count))
    if not re.fullmatch(r'#?\d+(?:\s*-\s*\d+)?(?:\s*(?:,|and|&|\s)\s*#?\d+(?:\s*-\s*\d+)?)*', text):
        raise ValueError('not a list of file numbers')
    picked = []
    for lo, hi in re.findall(r'(\d+)(?:\s*-\s*(\d+))?', text):
        for n in range(int(lo), int(hi or lo) + 1):
            if not 1 <= n <= count:
                raise ValueError(f'file {n} is not between 1 and {count}')
            if n - 1 not in picked:
                picked.append(n - 1)
    if not picked:
        raise ValueError('no file numbers')
    return picked


def _bounds(triangles):
    lo = [float('inf')] * 3
    hi = [float('-inf')] * 3
    for tri in triangles:
        for pt in tri:
            for a in range(3):
                v = float(pt[a])
                if v < lo[a]:
                    lo[a] = v
                if v > hi[a]:
                    hi[a] = v
    return lo, hi


def combine(target, sources, bed=None, gap=10.0, stl_inches=False):
    """Write one geometry-only 3MF (millimeters) with one object per part of ``sources``.

    Parts keep their size and authored orientation. When every part fits the
    bed together they are packed onto it (turned 0 or 90 degrees about Z);
    otherwise they are set out in a row, flat on Z=0, never overlapping, and
    the intake fit step rearranges or splits them like any multi-object 3MF.
    ``stl_inches`` reads the STL files as inches (3MF files carry their own units).
    Returns ``{'path', 'objects': [names], 'on_one_bed': bool}``.
    """
    from .compact_geometry import TransformedTriangles
    from .geometry_layout import load_parts, pack_parts, write_geometry_only_3mf
    target = Path(target)
    parts = []
    seen = {}
    for src in sources:
        inch = stl_inches and Path(src).suffix.lower() == '.stl'
        if Path(src).suffix.lower() == '.3mf':
            import zipfile
            from .source_colors import negative_volumes
            with zipfile.ZipFile(src) as archive:
                negatives = negative_volumes(archive)
            if negatives:
                # A geometry-only 3MF cannot subtract: the object would print
                # uncut (#1260). Modifier and support volumes print nothing
                # and are left out by load_parts.
                raise ValueError(f'{Path(src).name} cuts holes with negative volumes ('
                                 + ', '.join(negatives) + '), which a combined geometry-only '
                                 'model cannot carry')
        loaded, _, _, _ = load_parts(Path(src), input_unit='inch' if inch else None)
        for part in loaded:
            name = str(part.get('node') or part['id'])
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f'{name}_{seen[name]}'
            parts.append({'id': name, 'triangles': part['triangles']})
    if not parts:
        raise ValueError('no printable parts in the chosen files')
    placed_objects = None
    if bed is not None and len(parts) > 1:
        try:
            # A bed may be given as X, Y only; height is the fit step's business.
            size = [float(v) for v in bed][:3]
            placed, _ = pack_parts(parts, bed=tuple(size + [float('inf')] * (3 - len(size))))
            placed_objects = [{'name': p['part']['id'], 'triangles': p['triangles'],
                               'translation_mm': (p['xy'][0], p['xy'][1], 0.0)} for p in placed]
        except (RuntimeError, ValueError):
            placed_objects = None
    on_one_bed = placed_objects is not None
    if placed_objects is None:
        placed_objects, cursor = [], 0.0
        for part in parts:
            lo, hi = _bounds(part['triangles'])
            dx, dy, dz = -lo[0], -lo[1], -lo[2]
            tris = TransformedTriangles(part['triangles'], lambda x, y, z, dx=dx, dy=dy, dz=dz: (x + dx, y + dy, z + dz))
            placed_objects.append({'name': part['id'], 'triangles': tris, 'translation_mm': (cursor, 0.0, 0.0)})
            cursor += (hi[0] - lo[0]) + float(gap)
    write_geometry_only_3mf(placed_objects, target)
    return {'path': str(target), 'objects': [p['name'] for p in placed_objects], 'on_one_bed': on_one_bed}
