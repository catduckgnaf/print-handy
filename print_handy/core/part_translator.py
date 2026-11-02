"""Offline CAD part-name translation and size hints.

Presentation only. Never change persisted part keys or decision schemas.
No network and no model calls: the table next to this file is the whole dictionary.
"""
import json
import colorsys
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_TABLE_PATH = Path(__file__).with_name('part_name_translations.json')
_TOKEN = re.compile(r'[^\W\d_]+|\d+', re.UNICODE)
_EXT = re.compile(r'\.(stl|3mf|obj|step|stp)$', re.I)
_HASH = re.compile(r'^[0-9a-f]{12,}$', re.I)
_CJK = re.compile(r'[\u3040-\u30ff\u3400-\u9fff]')


def _fold(text):
    """Lowercase and strip accents so 'Cuerpo' and 'Körper' share a lookup key."""
    norm = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(ch for ch in norm if not unicodedata.combining(ch)).lower()


@lru_cache(maxsize=1)
def _table():
    # Read once per process: translate_name runs for every part name in every message, and the
    # table is shipped data that never changes while the process runs. Callers only read it.
    raw = json.loads(_TABLE_PATH.read_text(encoding='utf-8'))
    folded = {}
    for lang, words in raw.items():
        folded[lang] = {_fold(token): _fold(english) for token, english in words.items()}
    return folded


def tokenize(name):
    """Split CamelCase, underscores, hyphens, and glued digits. Keep the numbers."""
    stem = _EXT.sub('', str(name or '').strip())
    pieces = []
    for chunk in re.split(r'[_\-\s]+', stem):
        if not chunk:
            continue
        split = re.sub(r'([a-z])([A-Z])', r'\1 \2', chunk)
        split = re.sub(r'([A-Za-z])(\d)', r'\1 \2', split)
        split = re.sub(r'(\d)([A-Za-z])', r'\1 \2', split)
        pieces.extend(bit for bit in _TOKEN.findall(split) if bit)
    return pieces


def _segment(token, lang):
    """Greedy longest match for unspaced CJK names; None unless fully covered."""
    out, i = [], 0
    longest = max((len(k) for k in lang), default=0)
    while i < len(token):
        for n in range(min(longest, len(token) - i), 0, -1):
            if token[i:i + n] in lang:
                out.append(lang[token[i:i + n]])
                i += n
                break
        else:
            return None
    return ' '.join(out)


def is_meaningless(name):
    """Raw hashes/UUID file names (8ccb96b5...stl) say nothing about the part."""
    stem = _EXT.sub('', str(name or '').strip()).replace('-', '')
    return bool(_HASH.match(stem))


def translate_name(name):
    """Return 'English (Original)' or the original when nothing translated.

    Tokens already in English (Body, Component, Sketch) stay as written.
    Unknown tokens pass through. Numbers stay attached as 'Body 3'.
    A bare file extension is not part of the name.
    """
    original = str(name or '').strip()
    if not original:
        return original
    shown = _EXT.sub('', original).strip()
    tokens = tokenize(original)
    if not tokens:
        return shown or original
    words = _table()
    changed = False
    out = []
    for token in tokens:
        if token.isdigit():
            out.append(token)
            continue
        key = _fold(token)
        english = None
        for lang in words.values():
            if key in lang and lang[key] != key:
                english = lang[key]
                break
        if not english and _CJK.search(token):
            english = _segment(key, words.get('zh', {}))
        if english:
            changed = True
            out.append(english)
        else:
            out.append(token)
    if not changed:
        return shown or original
    label = ' '.join(out)
    label = ' '.join(bit.capitalize() if bit.isalpha() else bit for bit in label.split())
    return f'{label} ({shown or original})'


def named_color(hex_color):
    """Plain English color for an explicit RGB value, entirely offline.

    Hue/lightness bands instead of nearest-of-eleven RGB points: the RGB
    distance named a gold (#FFE57C) "pink" and a dark slate (#383845) "brown"
    on the 2026-10-31 Witch Ghost intake (#711).
    """
    raw = str(hex_color or '').strip().lstrip('#')
    if not re.fullmatch(r'[0-9A-Fa-f]{6}', raw):
        return 'unknown color'
    r, g, b = (int(raw[i:i + 2], 16) / 255 for i in range(0, 6, 2))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    hue *= 360
    if light >= 0.93 or (sat < 0.25 and light >= 0.85):
        return 'white'
    if light <= 0.12 or (sat < 0.2 and light <= 0.35):
        return 'black'
    if sat < 0.14:
        return 'gray'
    if 15 <= hue < 45 and light < 0.42:
        return 'brown'
    if hue < 12 or hue >= 345:
        return 'pink' if light >= 0.72 else 'red'
    if hue < 40:
        return 'orange'
    if hue < 70:
        return 'yellow'
    if hue < 170:
        return 'green'
    if hue < 255:
        return 'blue'
    if hue < 290:
        return 'purple'
    return 'pink' if light >= 0.45 else 'purple'


def size_hint(part):
    """Brief millimetre box from a part or geometry record. Empty when unknown."""
    box = None
    if isinstance(part, dict):
        box = part.get('bounds_mm') or part.get('bbox_mm') or part.get('size_mm')
    if not box or len(box) < 3:
        return ''
    dims = []
    for axis in box[:3]:
        if isinstance(axis, (list, tuple)) and len(axis) >= 2:
            dims.append(abs(float(axis[1]) - float(axis[0])))
        else:
            dims.append(abs(float(axis)))
    text = ' x '.join(f'{d:.0f}' if abs(d - round(d)) < 0.05 else f'{d:.1f}' for d in dims)
    return f'{text} mm'


def display_name(part):
    """English label plus a size hint when the part carries bounds."""
    if isinstance(part, dict):
        name = part.get('name') or ''
        if is_meaningless(name):
            fallback = part.get('object_name')
            name = fallback if fallback and not is_meaningless(fallback) else 'Main body'
    else:
        name = str(part or '')
        part = {}
    label = translate_name(name)
    hint = size_hint(part)
    return f'{label}, {hint}' if hint else label
