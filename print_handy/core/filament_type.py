"""A spool's type as a family plus a set of flags, and the one label every surface shows.

Design record: docs/history/FILAMENT-TYPES-DESIGN.md (#1788 phase B, sections 1, 2 and 4).

* ``family`` is the base polymer with its filler (PLA, PETG, PETG-CF ...), read through
  ``printer_state.material_family`` so it is the same family verification and the slicer use.
* ``flags`` is a frozenset of sub-type words (``high_speed``, ``matte``, ``silk``, ``plus`` ...).
  The vocabulary and each source's mapping live in ``data/filament_flags.toml``.
* Unknown words are dropped, never guessed: they land in ``unmapped`` so a caller can log them.
* Nothing here talks to Spoolman, a screen or a tag. It reads dicts the caller already has, and it
  never writes anything back.

Readers, one per documented source: :func:`from_spoolman_filament`, :func:`from_helix_lane`,
:func:`from_modifiers` (OpenTag3D / zmod_nfc ``material`` + ``modifiers[]``) and :func:`from_text`
(any free-text material label, the compatibility path for records that only carry text).
:func:`label` and :func:`spool_label` build ``<Color> <Finish>, <Family> <Sub-type>``.
"""
from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent.parent / "data" / "filament_flags.toml"

# PAHT is accepted here and normalized to PA (PAHT-CF to PA-CF) by material_family.
_FAMILY_TOKEN = re.compile(r"^(?:PETG|PCTG|PLA|ABS|ASA|TPU|PA\d{0,3}|PAHT|PC|PET)(?:-?(?:CF|GF))?\+?$")
_VALID_FAMILY = re.compile(r"^(?:(?:PETG|PCTG|PLA|ABS|ASA|TPU|PA|PC)(?:-(?:CF|GF))?|SUPPORT-PLA|SUPPORT-PA)$")
_UNKNOWN_COLOR = "color unknown"
_UNKNOWN_MATERIAL = "material unknown"


class FilamentFlagsError(RuntimeError):
    """filament_flags.toml is missing or malformed."""


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    try:
        with DATA.open("rb") as source:
            table = tomllib.load(source)
        flags = table["flags"]
        for name, row in flags.items():
            if row["kind"] not in ("grade", "finish", "effect"):
                raise ValueError(f"{name}: unknown kind {row['kind']!r}")
        return table
    except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise FilamentFlagsError(f"invalid filament flag table {DATA}: {exc}") from exc


def known_flags() -> tuple[str, ...]:
    """Every flag name Print Handy understands, in table order."""
    return tuple(_table()["flags"])


@dataclass(frozen=True)
class FilamentType:
    """What one source says a spool is. ``family`` is None when the source does not say."""

    family: str | None = None
    flags: frozenset[str] = frozenset()
    #: The raw material text, kept only when no family could be read from it (blends, vendor lines).
    raw_material: str = ""
    #: Words a source used that the table does not map; dropped from the flags.
    unmapped: tuple[str, ...] = ()
    #: flag -> the source field that set it, for diagnosing precedence.
    sources: Mapping[str, str] = field(default_factory=dict)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def known(self) -> bool:
        return self.family is not None

    def to_dict(self) -> dict[str, Any]:
        return {"family": self.family, "flags": sorted(self.flags), "raw_material": self.raw_material,
                "unmapped": list(self.unmapped)}


UNKNOWN = FilamentType()


# ---------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------

def _words(text: Any) -> list[str]:
    """Upper-case word tokens. A trailing '+' stays on its word (PLA+), so the plus grade is found."""
    return re.findall(r"[A-Z0-9]+\+?|\+", str(text or "").upper().replace("_", " "))


def _flag_words() -> tuple[dict[str, str], list[tuple[tuple[str, ...], str]]]:
    singles: dict[str, str] = {}
    phrases: list[tuple[tuple[str, ...], str]] = []
    for flag, row in _table()["flags"].items():
        for word in row.get("words", ()):
            singles[word.upper()] = flag
        for phrase in row.get("phrases", ()):
            phrases.append((tuple(p.upper() for p in phrase), flag))
    phrases.sort(key=lambda item: -len(item[0]))
    return singles, phrases


def _scan(tokens: list[str], *, name_only: bool = False) -> tuple[set[str], list[str], list[str]]:
    """(flags, leftover tokens, indexes consumed) for one tokenised label.

    ``name_only`` limits the words to the flags the table allows in a free-text spool name.
    """
    singles, phrases = _flag_words()
    flags_table = _table()["flags"]

    def allowed(flag: str) -> bool:
        return not name_only or bool(flags_table[flag].get("name_words"))

    flags: set[str] = set()
    left: list[str] = []
    consumed: list[str] = []
    i = 0
    while i < len(tokens):
        for phrase, flag in phrases:
            if tuple(tokens[i:i + len(phrase)]) == phrase and allowed(flag):
                flags.add(flag)
                consumed.extend(tokens[i:i + len(phrase)])
                i += len(phrase)
                break
        else:
            token = tokens[i]
            bare = token.rstrip("+")
            if token == "+" or (token.endswith("+") and _FAMILY_TOKEN.match(token)):
                # "PLA+" / "PETG+" / a lone "+": the plus grade, then the family word without it.
                if allowed("plus"):
                    flags.add("plus")
                    consumed.append("+")
                    if bare:
                        left.append(bare)
                else:
                    left.append(bare or token)
            elif token in singles and allowed(singles[token]):
                flags.add(singles[token])
                consumed.append(token)
            else:
                left.append(token)
            i += 1
    return flags, left, consumed


def _family_of(tokens: list[str]) -> tuple[str | None, list[str]]:
    """The family named by the leftover tokens, and the tokens that were not part of it.

    Conservative: exactly one polymer word (optionally with a CF/GF filler word) or nothing. A blend
    ("PLA/PHA"), two different polymers or an unrecognised label gives None rather than a guess.
    """
    from .printer_state import material_family

    polymers = [t for t in tokens if _FAMILY_TOKEN.match(t)]
    if not polymers:
        return None, tokens
    rest = [t for t in tokens if t not in polymers]
    fillers = [t for t in rest if t in ("CF", "GF")]
    joined = polymers[0] + ("-" + fillers[0] if fillers and "-" not in polymers[0] and not polymers[0].endswith(("CF", "GF")) else "")
    if len(set(polymers)) > 1:
        return None, tokens
    family = material_family(joined)
    if not _VALID_FAMILY.match(family or ""):
        return None, tokens
    rest = [t for t in rest if t not in fillers]
    if family in ("PLA", "PA") and "SUPPORT" in rest:
        # "Support PLA" / "Support-PA" name the support families, not the plain polymer.
        family = "SUPPORT-" + family
        rest = [t for t in rest if t != "SUPPORT"]
    return family, rest


def _from_material_text(text: Any, source: str, *, name_only: bool = False) -> FilamentType:
    raw = str(text or "").strip()
    if not raw:
        return UNKNOWN
    if "/" in raw:  # a blend names two materials; do not pick one
        return FilamentType(raw_material=raw, unmapped=tuple(_words(raw)))
    tokens = _words(_FILLER_PHRASE.sub(lambda m: m.group(1)[0] + "F", raw.upper()))
    flags, left, _ = _scan(tokens, name_only=name_only)
    family, rest = _family_of(left)
    if family is None:
        # The family is not readable, so flags found in the same label are not trustworthy either.
        return FilamentType(raw_material=raw, unmapped=tuple(tokens))
    unmapped = tuple(t for t in rest if not _SHORE.match(t))
    return FilamentType(family=family, flags=frozenset(flags), unmapped=unmapped,
                        sources={flag: source for flag in flags})


_FILLER_PHRASE = re.compile(r"\b(CARBON|GLASS)[ -]FIBR?E?R?\b")
_SHORE = re.compile(r"^\d{2,3}[AD]$")


# ---------------------------------------------------------------------------------------------
# Readers, one per documented source
# ---------------------------------------------------------------------------------------------

def from_text(material: Any, name: Any = None) -> FilamentType:
    """Compatibility reader for records that only carry text (a color/material pair, a legacy cache).

    The family comes from ``material`` alone; ``name`` only adds the flags the table allows in names.
    """
    result = _from_material_text(material, "material")
    return _add_name_flags(result, name)


def from_orca_profile(material: Any, name: Any) -> FilamentType:
    """An OrcaSlicer vendor filament profile (#1816): the family from its ``filament_type``, the flags from
    every word of its name before the "@<printer>" part.

    Orca's profile names are curated ("SUNLU PLA Marble @FF AD5M", "Flashforge HS PETG @FF AD5X"), so every
    flag word counts there, not only the ones the table trusts in a free-text spool name. The remaining
    words (the brand, a version such as "2.0") are kept in ``unmapped``.
    """
    result = _from_material_text(material, "orca filament_type")
    if not result.known:
        return result
    head = str(name or "").split("@", 1)[0]
    tokens = _words(_FILLER_PHRASE.sub(lambda m: m.group(1)[0] + "F", head.upper()))
    flags, left, _ = _scan(tokens)
    unmapped = tuple(t for t in left if not _FAMILY_TOKEN.match(t) and t not in ("CF", "GF") and not _SHORE.match(t))
    flags = set(flags) | set(result.flags)
    return FilamentType(family=result.family, flags=frozenset(flags), unmapped=unmapped,
                        sources={flag: "orca profile" for flag in flags})


def _add_name_flags(result: FilamentType, name: Any) -> FilamentType:
    if not result.known or not name:
        return result
    flags, _left, _ = _scan(_words(name), name_only=True)
    new = {f for f in flags if f not in result.flags}
    if not new:
        return result
    # A finish already set by the material field stays; a name never contradicts it.
    finishes = {f for f in result.flags if _kind(f) == "finish"}
    new = {f for f in new if not (_kind(f) == "finish" and finishes)}
    return FilamentType(result.family, result.flags | new, result.raw_material, result.unmapped,
                        {**result.sources, **{f: "name" for f in new}})


def _kind(flag: str) -> str:
    return _table()["flags"].get(flag, {}).get("kind", "")


def _decode(value: Any) -> Any:
    """A Spoolman extra value: stored JSON-encoded (``'"matte"'``), tolerated raw."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in ('"', "[", "{") or text in ("true", "false"):
            try:
                return json.loads(text)
            except ValueError:
                return text
        return text
    return value


def _truthy(value: Any) -> bool:
    value = _decode(value)
    return value is True or (isinstance(value, str) and value.strip().casefold() in ("true", "yes", "1"))


def _spoolman_extra_flags(filament: Mapping[str, Any]) -> dict[str, str]:
    """flag -> field name, from the documented Spoolman/SpoolmanDB fields on a filament record."""
    table = _table()["spoolman"]
    raw_extra = filament.get("extra")
    extra: Mapping[str, Any] = raw_extra if isinstance(raw_extra, Mapping) else {}

    def field_value(key: str) -> Any:
        value = extra.get(key, filament.get(key))
        return _decode(value)

    found: dict[str, str] = {}
    finish = field_value("finish")
    if isinstance(finish, str) and finish.strip().casefold() in table["finish"]:
        found[table["finish"][finish.strip().casefold()]] = "finish"
    pattern = field_value("pattern")
    if isinstance(pattern, str) and pattern.strip().casefold() in table["pattern"]:
        found[table["pattern"][pattern.strip().casefold()]] = "pattern"
    fill = field_value("fill")
    if isinstance(fill, str) and fill.strip().casefold() in table["fill"]:
        found[table["fill"][fill.strip().casefold()]] = "fill"
    for key, flag in table["boolean"].items():
        value = extra.get(key, filament.get(key))
        if value is not None and _truthy(value):
            found[flag] = key
    return found


def from_spoolman_filament(filament: Mapping[str, Any] | None) -> FilamentType:
    """A Spoolman filament record.

    Precedence: ``material`` gives the family and its own flag words; the documented structured fields
    (``finish``, ``pattern``, ``fill``, ``translucent``, ``glow``) come next and replace a finish read
    from free text; name words add only the flags the table allows in a name. A name never changes the
    family, and a record with no ``material`` has no family (identity is not guessed from a name).
    """
    filament = filament or {}
    base = _from_material_text(filament.get("material"), "material")
    if not base.known:
        return base
    structured = _spoolman_extra_flags(filament)
    flags = set(base.flags)
    sources = dict(base.sources)
    if any(_kind(f) == "finish" for f in structured):
        for flag in [f for f in flags if _kind(f) == "finish"]:
            flags.discard(flag)
            sources.pop(flag, None)
    flags.update(structured)
    sources.update(structured)
    result = FilamentType(base.family, frozenset(flags), "", base.unmapped, sources)
    return _add_name_flags(result, filament.get("name"))


def from_modifiers(material: Any, modifiers: Iterable[Any] | None = None) -> FilamentType:
    """OpenTag3D / Snapmaker ``material`` plus ``modifiers[]`` as zmod_nfc reports them.

    The family comes from ``material``. Each modifier maps through the table; ``Basic`` sets nothing and
    a modifier the table does not list is dropped into ``unmapped``.
    """
    base = _from_material_text(material, "material")
    if not base.known:
        return base
    table = {k.casefold(): v for k, v in _table()["modifiers"].items()}
    flags, sources, unmapped = set(base.flags), dict(base.sources), list(base.unmapped)
    for modifier in modifiers or ():
        word = str(modifier or "").strip()
        if not word:
            continue
        flag = table.get(word.casefold())
        if flag:
            flags.add(flag)
            sources[flag] = "modifiers"
        elif flag is None:
            unmapped.append(word.upper())
    return FilamentType(base.family, frozenset(flags), "", tuple(unmapped), sources)


def from_helix_lane(record: Mapping[str, Any] | None) -> FilamentType:
    """A HelixScreen v2 ``lane_data`` record.

    ``material`` is the reduced family (PLA+ and Silk become PLA) and wins for the family;
    ``filament_type`` is the precise label and contributes flags only when its family agrees. The legacy
    ``type`` / ``helix_material`` strings are read only when both v2 fields are empty. ``filament_id``
    is a profile pointer, not a type: resolving it belongs to the profile step, not here.
    """
    record = record or {}
    material = _from_material_text(record.get("material"), "material")
    precise = _from_material_text(record.get("filament_type"), "filament_type")
    if not material.known and not precise.known and not material.raw_material and not precise.raw_material \
            and not str(record.get("material") or "").strip() and not str(record.get("filament_type") or "").strip():
        legacy = record.get("type") or record.get("helix_material")
        return _from_material_text(legacy, "legacy")
    if material.known and precise.known:
        if material.family == precise.family:
            return FilamentType(material.family, material.flags | precise.flags, "",
                                material.unmapped + precise.unmapped, {**material.sources, **precise.sources})
        return material  # disagreement: the reduced family is authoritative, the precise flags are not trusted
    if material.known:
        return material
    if precise.known:
        return precise
    return material if material.raw_material else precise


# ---------------------------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------------------------

def _finish_words(flags: Iterable[str]) -> list[str]:
    rows = _table()["flags"]
    chosen = set(flags)
    return [row["word"] for name, row in rows.items() if row["kind"] == "finish" and name in chosen]


def sub_type(ftype: FilamentType) -> str:
    """The family with its short grade codes: ``PETG HS``, ``PLA+``, ``PLA+ HS`` (empty when unknown)."""
    if not ftype.known:
        return ftype.raw_material
    rows = _table()["flags"]
    family = ftype.family or ""
    plus = "+" if "plus" in ftype.flags else ""
    codes = [row["short"] for name, row in rows.items()
             if row["kind"] == "grade" and name != "plus" and name in ftype.flags]
    return (family + plus + "".join(" " + c for c in codes)).strip()


def color_name(name: Any, ftype: FilamentType | None = None) -> str:
    """A spool's color words: its name with the type words (family, flags, '+') taken out.

    ``Matte White`` -> ``White``; ``High Speed Matte Black PETG`` -> ``Black``; ``Dark Green PLA`` ->
    ``Dark Green``. Original capitalisation and word order of the remaining words are kept.
    """
    raw = str(name or "").strip()
    if not raw or raw.casefold().startswith("spool #"):
        return ""
    if ftype is None or not ftype.known:
        return raw  # the family is not known, so no word of the name is known to be a type word
    _singles, phrases = _flag_words()
    rows = _table()["flags"]
    drop = {w for flag, row in rows.items()
            if row.get("name_words") and flag in ftype.flags for w in row.get("words", ())}
    drop = {w.upper() for w in drop}
    parts = raw.replace("_", " ").split()
    out: list[str] = []
    i = 0
    while i < len(parts):
        upper = tuple(re.sub(r"[^A-Z0-9+]", "", p.upper()) for p in parts[i:])
        skipped = False
        for phrase, flag in phrases:
            if rows[flag].get("name_words") and flag in ftype.flags and upper[:len(phrase)] == phrase:
                i += len(phrase)
                skipped = True
                break
        if skipped:
            continue
        token = upper[0]
        if token in drop or (token == "+" and "plus" in ftype.flags) or _same_family(token, ftype):
            i += 1
            continue
        out.append(parts[i])
        i += 1
    return " ".join(out)


def _same_family(token: str, ftype: FilamentType) -> bool:
    """Drop only an exact family token or an explicitly supported display alias."""
    if token.endswith("+") and "plus" not in ftype.flags:
        return False
    family = (ftype.family or "").replace("-", "")
    material = token.rstrip("+")
    if material == family:
        return True
    # PET is an established alias for the PETG base, including its reinforced family.
    return material == "PET" and ftype.family in ("PETG", "PETG-CF", "PETG-GF")


def label(color: Any, ftype: FilamentType | None, *, color_fallback: str = "", material_fallback: str = "") -> str:
    """``<Color> <Finish>, <Family> <Sub-type>``. ``color`` is the color words only (see :func:`color_name`).

    ``Black Matte, PETG HS``; ``White Matte, PLA``; ``Bronze, PLA+``; ``Dark Green, PLA``.
    A missing part is left out unless a fallback is given.
    """
    ftype = ftype or UNKNOWN
    head = " ".join(w for w in (str(color or "").strip() or color_fallback, *_finish_words(ftype.flags)) if w)
    tail = sub_type(ftype) or material_fallback
    return ", ".join(part for part in (head, tail) if part)


def spool_label(filament: Mapping[str, Any] | None, *, color_fallback: str = "",
                material_fallback: str = "") -> str:
    """The label for one Spoolman filament record (``spool["filament"]``)."""
    filament = filament or {}
    ftype = from_spoolman_filament(filament)
    return label(color_name(filament.get("name"), ftype), ftype,
                 color_fallback=color_fallback, material_fallback=material_fallback)


def text_label(color: Any, material: Any, *, color_fallback: str = "", material_fallback: str = "") -> str:
    """The label for a record that carries only a color/name string and a material string."""
    ftype = from_text(material, color)
    return label(color_name(color, ftype), ftype, color_fallback=color_fallback,
                 material_fallback=material_fallback or str(material or "").strip())


def picker_label(filament: Mapping[str, Any] | None, spool_id: Any, grams: Any) -> str:
    """The spool picker line: ``Black Matte, PETG HS · #22 · 812 g left`` (unknowns said in place)."""
    text = spool_label(filament, color_fallback=_UNKNOWN_COLOR, material_fallback=_UNKNOWN_MATERIAL)
    return _picker(text, spool_id, grams)


def picker_label_text(name: Any, material: Any, spool_id: Any, grams: Any) -> str:
    text = text_label(name, material, color_fallback=_UNKNOWN_COLOR, material_fallback=_UNKNOWN_MATERIAL)
    return _picker(text, spool_id, grams)


def _picker(text: str, spool_id: Any, grams: Any) -> str:
    if isinstance(grams, (int, float)) and not isinstance(grams, bool):
        amount = f"{grams:g} g left"
    else:
        amount = "grams unknown"
    return f"{text} · #{spool_id} · {amount}"
