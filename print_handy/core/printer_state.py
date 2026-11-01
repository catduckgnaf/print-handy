#!/usr/bin/env python3
"""Installed nozzle + loaded spool: single source of truth and live cross-check.

State file: <state_dir>/printers/<printer_id>/printer_hardware.json (#334, design §2.4);
with no printer named, the legacy root-level <state_dir>/printer_hardware.json, exactly as
before. A printer whose per-printer file does not exist yet reads the legacy file (the
default printer only — it was the singleton's record); new writes go to the per-printer
location and the legacy file is left in place for one release.
Live checks: Klipper (via Moonraker) nozzle_diameter + probe z_offset, Moonraker
active Spoolman spool, Spoolman spool record.

probe.z_offset, the screenless saved gcode_offsets.z, and the native screen
zProbeOffset are separate stores. None is copied into another, and none is
marked verified just because a config number was readable.

  python3 -m print_handy.core.printer_state check          -> prints one summary line, exit 0 OK / 2 mismatch
  python3 -m print_handy.core.printer_state show           -> prints the state JSON
  python3 -m print_handy.core.printer_state set --spool 7  -> operator-instructed change; also sets Moonraker active spool
  python3 -m print_handy.core.printer_state set --nozzle 0.4 --nozzle-type "brass" (syncs printer.cfg, restarts Klipper, marks z_offset unverified)
  python3 -m print_handy.core.printer_state set --z-offset N -> record a measured user offset; does not write Klipper
  python3 -m print_handy.core.printer_state set --sync-z   -> record live probe.z_offset as the probe store only
"""
import argparse, datetime, json, os, re, sys, urllib.request
from pathlib import Path

from .. import printers, settings
from . import filament_color, spoolman_client

STATE = str(settings.state_dir() / "printer_hardware.json")


_FAMILY_BASES = ("PETG", "PCTG", "PLA", "ABS", "ASA", "TPU", "PA", "PC")


# Conservative documented fallback targets, configurable in [material_temps].
# Values reflect representative material profiles, not universal guarantees.
DEFAULT_MATERIAL_TEMPS = {
    "PLA": {"nozzle": 210, "bed": 60},
    "PETG": {"nozzle": 240, "bed": 85},
    # PCTG is its own family, not folded into PETG: it runs ~10 C hotter and a little cooler on the
    # bed, and a PCTG spool must not silently stand in for a PETG record (same_material). Vendor
    # ranges are 240-270 C nozzle, 70-90 C bed; these are the conservative middle.
    "PCTG": {"nozzle": 250, "bed": 80},
    "ABS": {"nozzle": 245, "bed": 100},
    "ASA": {"nozzle": 260, "bed": 105},
    "TPU": {"nozzle": 230, "bed": 50},
    "PA": {"nozzle": 260, "bed": 90},
    "PC": {"nozzle": 275, "bed": 110},
}


# Per-variant adjustments on top of the family defaults (#512). A variant is
# a named blend or finish within a family — Silk PLA, PLA+, Matte PLA, the
# fibre-reinforced *-CF / *-GF variants — whose representative printing
# behaviour differs from the plain family profile. Values are conservative
# representative defaults, not universal guarantees: a temperature the
# operator recorded on the Spoolman filament itself always wins and is never
# adjusted, and the family [material_temps] configuration remains the base
# these deltas apply to. ``outer_wall_speed_factor`` scales the process
# profile's outer-wall speed when slicing (Silk PLA's gloss needs slower
# outer walls); 1.0 means no change. ``outer_wall_speed_max`` caps the result in mm/s and
# ``slow_down_layer_time_min`` raises the filament's minimum layer time in seconds; None means
# no cap. Silk's values are OrcaSlicer's own: its fdm_filament_pla_silk profile asks for 40 to
# 60 mm/s outer walls and an 8 s minimum layer time. A factor alone is not enough: 0.7 x a
# 200 mm/s template is still 140 mm/s.
MATERIAL_VARIANT_ADJUSTMENTS = {
    "SILK PLA": {"nozzle_delta": 10, "bed_delta": 0, "outer_wall_speed_factor": 0.7,
                 "outer_wall_speed_max": 60, "slow_down_layer_time_min": 8,
                 "note": "Silk PLA prints hotter than plain PLA and needs slower outer walls (at most 60 mm/s) for its gloss."},
    "PLA+": {"nozzle_delta": 5, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
             "note": "PLA+ / PLA Pro blends print slightly hotter than plain PLA."},
    "MATTE PLA": {"nozzle_delta": 5, "bed_delta": 0, "outer_wall_speed_factor": 0.9,
                  "note": "Matte PLA's additives print slightly hotter, with slightly slower outer walls."},
    "PLA-CF": {"nozzle_delta": 5, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
               "note": "Carbon-fibre PLA prints slightly hotter than plain PLA."},
    "PETG-CF": {"nozzle_delta": 5, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
                "note": "Carbon-fibre PETG prints slightly hotter than plain PETG."},
    "PA-CF": {"nozzle_delta": 10, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
              "note": "Carbon-fibre nylon prints hotter than unfilled nylon."},
    "REINFORCED": {"nozzle_delta": 5, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
                   "note": "Fibre-reinforced variants print slightly hotter than their base polymer."},
}

_NO_ADJUSTMENT = {"nozzle_delta": 0, "bed_delta": 0, "outer_wall_speed_factor": 1.0,
                  "outer_wall_speed_max": None, "slow_down_layer_time_min": None, "note": ""}


def _material_tokens(material):
    return re.findall(r"[A-Z0-9]+", str(material or "").upper())


def material_variants(material):
    """Every adjustment variant key that applies to a material label, in a fixed order.

    Silk and Matte are finishes that can sit either side of the family token ("Silk PLA",
    "PLA Silk"); PLA+ / PLA Pro is its own blend; a fibre-reinforced family (PLA-CF, PA-GF, ...)
    keys by family, falling back to the generic REINFORCED entry when no family-specific entry
    exists. Variants stack ("PLA+ Silk", "Matte PLA+", "Silk PLA-CF" carry two keys): the order
    is finishes, then the blend, then the reinforcement. Bare "Silk" is Silk PLA.
    """
    label = str(material or "").upper()
    squashed = re.sub(r"[\s_+\-]+", "", label)
    tokens = _material_tokens(material)
    family = material_family(material)
    base = family.split("-", 1)[0]
    keys = []
    if base == "PLA":
        if "SILK" in squashed:
            keys.append("SILK PLA")
        if "MATTE" in squashed:
            keys.append("MATTE PLA")
        if "+" in label or "PLUS" in tokens or "PRO" in tokens:
            keys.append("PLA+")
    if family.endswith(("-CF", "-GF", "-CARBONFIBER", "-GLASSFIBER")):
        keys.append(family if family in MATERIAL_VARIANT_ADJUSTMENTS else "REINFORCED")
    return tuple(keys)


def material_variant(material):
    """The dominant adjustment variant key for a material label, or None for a plain family label.

    With stacked variants the one with the larger nozzle delta leads (ties keep the order of
    material_variants), so "PLA+ Silk" and "Silk PLA-CF" report SILK PLA.
    """
    keys = material_variants(material)
    if not keys:
        return None
    return max(keys, key=lambda k: MATERIAL_VARIANT_ADJUSTMENTS[k]["nozzle_delta"])


def _smallest(values):
    values = [v for v in values if v is not None]
    return min(values) if values else None


def _largest(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def material_adjustments(material):
    """The variant adjustments for a material label (#512): temperature deltas
    on the family defaults plus the outer-wall speed factor, with its note.

    Stacked variants combine conservatively: the larger nozzle and bed delta (the hotter
    material's behaviour wins), the smaller outer-wall speed factor and cap (the slower finish wins)
    and the longer minimum layer time.
    "abrasive" carries the nozzle-wear flag for the same label, so a finish never hides CF.
    Plain family labels (PLA, PETG, ABS, ASA, TPU, PA, PC, ...) get the zero adjustment. The
    returned dict always carries every key.
    """
    from .nozzle import is_abrasive_material
    keys = material_variants(material)
    abrasive = is_abrasive_material(material)
    if not keys:
        return {"variant": None, "variants": (), "abrasive": abrasive, **_NO_ADJUSTMENT}
    rows = [MATERIAL_VARIANT_ADJUSTMENTS[k] for k in keys]
    return {"variant": material_variant(material), "variants": keys, "abrasive": abrasive,
            "nozzle_delta": max(r["nozzle_delta"] for r in rows),
            "bed_delta": max(r["bed_delta"] for r in rows),
            "outer_wall_speed_factor": min(r["outer_wall_speed_factor"] for r in rows),
            "outer_wall_speed_max": _smallest(r.get("outer_wall_speed_max") for r in rows),
            "slow_down_layer_time_min": _largest(r.get("slow_down_layer_time_min") for r in rows),
            "note": " ".join(r["note"] for r in rows)}


def material_family(material):
    """Slicing family for a material label: PLA+, PLA Pro, Matte PLA, Silk PLA -> PLA.

    Carbon fiber stays distinct (PLA-CF). Unknown materials compare by their
    normalized label, so PETG never matches PLA.

    Vocabulary decisions (2026-10-31 audit):
      * PA6, PA11, PA12, PA66, PAHT -> PA; with CF/GF -> PA-CF / PA-GF (the grade changes the
        recommended temperature, which the Spoolman filament carries, not the family).
      * PET -> PETG. Unmodified PET prints like PETG (it is the same polymer without the glycol
        modifier, sold interchangeably, and uses the same vendor profiles); PET-CF -> PETG-CF.
      * PCTG stays its own family (see DEFAULT_MATERIAL_TEMPS).
      * Shore hardness ("TPU 95A") and colour-effect or fill words are not part of the family.
      * Bare "Silk" is Silk PLA: it is how the AD5X screen and several vendors write it.
      * Bambu support grades PLA-S / PA-S are SUPPORT-PLA / SUPPORT-PA: distinct families that
        match only themselves and have no print temperatures, so they are refused as model
        material instead of being mistaken for the polymer they support.
      * Speed/flow grades (HS, HF, High Speed) stay refused: no checked profile exists for them.
    """
    # A vendor name is never part of the family: Bambu sub-brands read "Bambu PLA Basic".
    label = _BRAND_WORDS.sub(" ", str(material or "").upper())
    m = re.sub(r"[\s_+\-]+", "", label)
    support = {"PLAS": "SUPPORT-PLA", "PAS": "SUPPORT-PA", "SUPPORTFORPLA": "SUPPORT-PLA",
               "SUPPORTPLA": "SUPPORT-PLA", "PLASUPPORT": "SUPPORT-PLA",
               "SUPPORTFORPA": "SUPPORT-PA", "SUPPORTPA": "SUPPORT-PA", "PASUPPORT": "SUPPORT-PA"}
    if m in support:
        return support[m]
    # Labels may place the polymer name before or after a descriptor (Matte PLA).
    # Match only complete known base tokens, never a substring like PETG inside another label.
    for base in _FAMILY_BASES:
        if m == base:
            return base
        if m.startswith(base):
            suffix = m[len(base):]
            if suffix in {"PRO", "PLUS", "MATTE", "SILK", "FLEX", "TOUGH"}:
                return base
            if suffix in {"CF", "GF", "CARBONFIBER", "GLASSFIBER"}:
                return f"{base}-{suffix}"
        if m.endswith(base) and m[:-len(base)] in {"MATTE", "SILK", "TOUGH", "PRO"}:
            return base
    if m in {"FLEXIBLE", "NYLON", "POLYAMIDE"}:
        return {"FLEXIBLE": "TPU", "NYLON": "PA", "POLYAMIDE": "PA"}[m]
    # Fallback for labels the rules above do not resolve: drop finish, colour, fill and hardness
    # words around one polymer token ("Shiny Silk PLA", "PLA (Silk)", "Wood PLA", "TPU 95A",
    # "Silk PLA-CF"), optionally followed by a CF/GF reinforcement. Only an exact known base
    # survives, so blends ("PLA/PHA"), speed/flow grades and unknown names stay refused rather
    # than guessed.
    if "/" not in str(material or ""):
        words = _polymer_words(material)
        if len(words) == 1 and words[0] in _FAMILY_BASES:
            return words[0]
        if len(words) == 2 and words[0] in _FAMILY_BASES and words[1] in {"CF", "GF"}:
            return f"{words[0]}-{words[1]}"
        if len(words) == 2 and words[1] in _FAMILY_BASES and words[0] in {"CF", "GF"}:  # "Carbon Fiber PETG"
            return f"{words[1]}-{words[0]}"
        if not words and "SILK" in _material_tokens(label):
            return "PLA"
    return m


_BRAND_WORDS = re.compile(r"\b(?:BAMBU\s*LAB|BAMBU|GENERIC)\b")
_POLYMER_ALIASES = {"PET": "PETG", "PAHT": "PA", "NYLON": "PA", "POLYAMIDE": "PA", "FLEXIBLE": "TPU"}
_POLYMER_TOKEN = re.compile(r"^(PA\d{1,3}|PAHT|PET|PETG|PCTG|PLA|ABS|ASA|TPU|PC)(CF|GF)?$")
_SHORE_HARDNESS = re.compile(r"^\d{2,3}[AD]$")


def _polymer_words(material):
    """The label's tokens after dropping finish, colour, fill and Shore-hardness words, with
    polymer grades canonicalised (PA12 -> PA, PET -> PETG) and fused reinforcement split (PA6CF)."""
    words = []
    for token in _material_tokens(_BRAND_WORDS.sub(" ", str(material or "").upper())):
        if token in _FINISH_WORDS or token in _FILL_WORDS or token in _COLOR_WORDS or _SHORE_HARDNESS.match(token):
            continue
        if re.fullmatch(r"TPU\d{2,3}[AD]", token):
            token = "TPU"
        match = _POLYMER_TOKEN.match(token)
        if match:
            base, fibre = match.groups()
            base = "PA" if base.startswith("PA") and base != "PAHT" else base
            words.append(_POLYMER_ALIASES.get(base, base))
            if fibre:
                words.append(fibre)
            continue
        words.append(token)
    # "Carbon Fiber" / "Glass Fibre" are CF / GF spelled out.
    joined = " ".join(words)
    joined = re.sub(r"\bCARBON FIB(?:ER|RE)\b", "CF", joined)
    joined = re.sub(r"\bGLASS FIB(?:ER|RE)\b", "GF", joined)
    return joined.split()


# Surface-finish descriptors that do not change the thermal family (#512 covers the
# adjustments that matter: Silk, Matte, PLA+). Speed/flow grades (HS, HF) print hotter and
# reinforcement (CF/GF) is abrasive, so both stay distinct families, never folded in here.
_FINISH_WORDS = frozenset({"SILK", "MATTE", "SHINY", "GLOSSY", "SATIN", "BASIC", "PRO", "PLUS", "TOUGH",
                           "METALLIC", "PEARL", "PEARLESCENT"})
# Colour-effect and colour words: pigments, not polymers ("Silk Rainbow PLA", "Dual-Color Silk PLA",
# "Tri-color Silk PLA", "Silk PLA Metallic Gold").
_COLOR_WORDS = frozenset({
    "RAINBOW", "DUAL", "TRI", "BI", "COLOR", "COLOUR", "COLORS", "COLOURS", "TRICOLOR", "DUALCOLOR",
    "BICOLOR", "MULTICOLOR", "MULTI", "GRADIENT", "COEXTRUDED",
    "GOLD", "GOLDEN", "SILVER", "BLACK", "WHITE", "RED", "BLUE", "GREEN", "YELLOW", "ORANGE", "PURPLE",
    "PINK", "GRAY", "GREY", "BROWN", "TEAL", "CYAN", "MAGENTA", "VIOLET", "NAVY", "BEIGE", "IVORY"})
# Fill and additive words: the polymer underneath prints at its own temperatures, but most of
# these wear a brass nozzle, which core.nozzle.is_abrasive_material flags separately.
_FILL_WORDS = frozenset({
    "WOOD", "HARDWOOD", "WOODEN", "GLOW", "LUMINOUS", "MARBLE", "SPARKLE", "GALAXY", "GLITTER",
    "METAL", "BASALT", "CERAMIC", "COPPER", "BRONZE", "STEEL", "ALUMINUM", "ALUMINIUM", "MICA",
    "MAGNETIC", "STONE", "FILL", "FILLED"})


_DISPLAY_ACRONYMS = frozenset({"PLA", "PETG", "PET", "PCTG", "ABS", "ASA", "TPU", "TPE", "PA", "PA6", "PA11",
                               "PA12", "PC", "PVA", "BVOH", "HIPS", "PP", "PE", "PEEK", "PEI", "CF", "GF", "HT", "HF"})


def display_material(material):
    """A material label for chat: polymer names upper-case, other lower-case words capitalised.

    'Pla' -> 'PLA', 'silk pla' -> 'Silk PLA', 'petg-cf' -> 'PETG-CF'. Display only: stored and
    compared labels (Spoolman, job records, material_family) are never rewritten with this.
    """
    def word(match):
        text = match.group(0)
        if text.upper() in _DISPLAY_ACRONYMS:
            return text.upper()
        return text[:1].upper() + text[1:] if text.islower() else text
    return re.sub(r"[A-Za-z0-9]+", word, str(material or "").strip())


def same_material(a, b):
    """True when two material labels slice as the same family."""
    fa, fb = material_family(a), material_family(b)
    return bool(fa) and fa == fb


def resolve_material_temperatures(material, filament, config=None):
    """Resolve validated nozzle/bed targets and provenance from a Spoolman filament."""
    import math
    family = material_family(material)
    aliases = {"FLEXIBLE": "TPU", "NYLON": "PA", "POLYAMIDE": "PA"}
    # Abrasive reinforced variants retain their distinct family for nozzle
    # safety checks, but inherit their base polymer's thermal profile.
    thermal_family = family.split("-", 1)[0] if family.endswith(("-CF", "-GF", "-CARBONFIBER", "-GLASSFIBER")) else family
    thermal_family = aliases.get(thermal_family, thermal_family)
    defaults = DEFAULT_MATERIAL_TEMPS.copy()
    if config is None:
        config = settings.current()
    defaults.update(config.get("material_temps", {}))
    if thermal_family not in defaults:
        raise ValueError(f"Unsupported filament material {material!r}; configure a recognized material profile, no PLA fallback is allowed")
    base = defaults[thermal_family]
    for key in ("nozzle", "bed"):
        value = base.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 < float(value) <= 300:
            raise ValueError(f"Invalid configured {thermal_family} {key} temperature {value!r} °C")
    result, sources = {}, {}
    adjustments = material_adjustments(material)
    deltas = {"nozzle": adjustments["nozzle_delta"], "bed": adjustments["bed_delta"]}
    for key, field in (("nozzle", "settings_extruder_temp"), ("bed", "settings_bed_temp")):
        value = filament.get(field)
        # Spoolman stores "not set" as 0 or null: neither is an explicit operator temperature.
        if value is not None and not isinstance(value, bool) and isinstance(value, (int, float)) and value == 0:
            value = None
        if value is None:
            # The variant adjustment (#512) refines the family default only:
            # a temperature recorded on the Spoolman filament is explicit
            # operator/manufacturer data and is never adjusted.
            value = base[key] + deltas[key]
            sources[key] = (f"material default + {' + '.join(adjustments['variants'])} adjustment"
                            if adjustments["variant"] and deltas[key] else "material default")
        else:
            sources[key] = "Spoolman filament"
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 < float(value) <= 300:
            raise ValueError(f"Invalid {key} temperature {value!r} °C for {material}; expected a finite value from 1 to 300 °C")
        result[key] = float(value) if not float(value).is_integer() else int(value)
    return family, result, sources


def _is_seeded_default(material, stored):
    """True when stored temps equal the unadjusted family default for material while its
    variant adjustment would change them: a seeded value, not explicit legacy temperatures."""
    try:
        family, targets, _ = resolve_material_temperatures(material, {})
        base = resolve_material_temperatures(family, {})[1]
    except (ValueError, RuntimeError):
        return False
    same = all(int(stored.get(k) or -1) == int(base[k]) for k in ("nozzle", "bed"))
    return same and (targets["nozzle"], targets["bed"]) != (base["nozzle"], base["bed"])


def slice_temperatures(state, filament=None, fetch_filament=None, stored_ok=True):
    """Nozzle/bed targets for slicing the job's selected spool, resolved from that spool.

    The stored temps_c is a printer-wide record: `set --spool` writes it, but slot
    selection, active-spool sync and Spoolman slot sync change the spool without it.
    It is used when its temperature_resolution names this exact spool and material, or
    when the record has no resolution at all (explicit legacy temperatures, kept as
    _material_temperature_contract always has; the spool-sync paths mark the record
    stale instead of leaving it bare). A resolution for another spool, or an older one
    that names only a family, is resolved again from the material defaults, the
    variant adjustment (#512) and the spool's Spoolman temperatures. ``filament`` is
    the spool's Spoolman filament when the caller already has it; ``fetch_filament`` is
    called for it only when needed (Spoolman is advisory: None means defaults).
    ``stored_ok=False`` always resolves (a slot spool other than the active one).
    Returns (temps_c, temperature_resolution). Unknown materials raise; no PLA fallback.
    """
    spool = state.get("spool") or {}
    material = spool.get("material")
    stored, resolution = state.get("temps_c"), state.get("temperature_resolution") or {}
    if not material:
        if stored:
            return dict(stored), resolution  # _material_temperature_contract reports the missing material
        raise RuntimeError("selected spool has no material; cannot determine safe temperatures")
    if filament is None and stored_ok and stored and not resolution and _is_seeded_default(material, stored):
        # A bare record holding exactly the family default (what seeding writes: 210/60 for
        # every PLA) is not an operator choice; Silk PLA sliced at 210 °C on 2026-10-31 (#64).
        stored_ok = False
    if filament is None and stored_ok and stored and (not resolution or (
            resolution.get("material") == material and resolution.get("spoolman_id") == spool.get("spoolman_id"))):
        return dict(stored), resolution
    if filament is None and fetch_filament is not None and spool.get("spoolman_id") is not None:
        try:
            filament = fetch_filament(spool["spoolman_id"])
        except Exception:
            filament = None
        if filament is not None and not same_material(filament.get("material"), material):
            filament = None  # a different material on file: the slot record decides, at its defaults
    try:
        family, targets, sources = resolve_material_temperatures(material, filament or {})
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return ({"nozzle": targets["nozzle"], "bed": targets["bed"]},
            {"material_family": family, "material": material, "spoolman_id": spool.get("spoolman_id"),
             "sources": sources,
             "source": next(iter(set(sources.values()))) if len(set(sources.values())) == 1
             else "material default / Spoolman filament"})


# One filament profile per slot of a multicolor job. Orca emits an M104 for each tool change (the
# wipe tower / filament-change sequence sets the new filament's nozzle_temperature), so a single
# heater follows the slots, but it cannot wait for a large swing mid-print: a job whose slots
# resolve to nozzle targets further apart than this is refused on a printer with one heater.
# The bed is one plate for the whole job, so its slots must agree on the same bound.
MULTICOLOR_TEMPERATURE_TOLERANCE_C = 10


def _is_active_spool(state, record):
    """True when a slot record is the active spool itself (same spool and material): only then does the
    printer-wide temps_c describe it. Any other slot spool is resolved from its own filament."""
    spool = state.get("spool") or {}
    norm = lambda m: " ".join(str(m or "").split()).casefold()  # noqa: E731
    return (record.get("spoolman_id") == spool.get("spoolman_id")
            and norm(record.get("material")) == norm(spool.get("material")))


def multicolor_slot_temperatures(state, slots, fetch_filament=None, *, shared_heater=True, label="slot",
                                 reference_material=None):
    """Per-slot temperatures for one multicolor job, under the one rule every multi-filament printer follows.

    ``slots`` is the job's slot records (material, spoolman_id) in tool order. Rule: the job's spools
    must all be the same material family (PLA + Silk PLA + PLA+ is allowed, PLA + PETG is not), and
    each slot is sliced at its own spool's temperatures (slice_temperatures with that slot's spool).
    On a printer with one heater (``shared_heater``) the nozzle targets may differ by at most
    MULTICOLOR_TEMPERATURE_TOLERANCE_C; the bed is shared by every printer, so its targets must agree
    within the same bound. ``reference_material`` (the active spool) must be of that family too.
    Returns [(temps_c, resolution)] in slot order; raises RuntimeError otherwise.
    """
    records = [dict(slot) for slot in slots]
    labels = [str(r.get("material") or "") for r in records]
    families = {material_family(m) for m in labels + ([str(reference_material)] if reference_material else [])}
    if len(families) > 1:
        raise RuntimeError("mixed materials in one job: " +
                           ", ".join(dict.fromkeys(labels + ([str(reference_material)] if reference_material else []))) +
                           ". Only finishes of one material family (PLA with Silk PLA or PLA+) can share a "
                           "job; slice each material separately")
    spool = state.get("spool") or {}
    out = []
    for record in records:
        slot_state = {**state, "spool": {**spool, "spoolman_id": record.get("spoolman_id"),
                                         "material": record.get("material"), "name": record.get("name")}}
        out.append(slice_temperatures(slot_state, None, fetch_filament,
                                      stored_ok=_is_active_spool(state, record)))
    for key, shared, noun in (("nozzle", shared_heater, "nozzle"), ("bed", True, "bed")):
        if not shared:
            continue
        values = [float(temps[key]) for temps, _ in out]
        if max(values) - min(values) > MULTICOLOR_TEMPERATURE_TOLERANCE_C:
            detail = ", ".join(f"{label} {i} {r.get('material')} {temps[key]:g} C"
                               for i, (r, (temps, _)) in enumerate(zip(records, out), 1))
            raise RuntimeError(
                f"the {noun} temperatures of this job's spools differ by more than "
                f"{MULTICOLOR_TEMPERATURE_TOLERANCE_C} C ({detail}); {'one heater' if key == 'nozzle' else 'one bed'} "
                "cannot follow that safely. Use spools with closer temperatures, or slice them separately")
    return out


def recorded_nozzle_type(state):
    return (state.get("nozzle") or {}).get("type") or settings.current().get("printer", {}).get("nozzle_type") or "hardened_steel"


def _header_numbers(text, key):
    """The numbers of an Orca config line (`; nozzle_temperature = 220,210`), or [] when absent or not numeric."""
    match = re.search(rf"^; {re.escape(key)} = (.+)$", text or "", re.M)
    try:
        return [float(piece) for piece in match.group(1).split(",")] if match else []
    except ValueError:
        return []


def gcode_tool_temperatures(state, materials, text, fetch_filament=None):
    """Temperatures a multi-filament G-code must carry, checked per tool against its own header, or None.

    The header lists each filament's nozzle target (already raised by the nozzle-type offset). Every
    tool the file uses must carry what a slot spool holding that material resolves to, and the
    filament that heats the nozzle at the start (the first T command, else T0) sets the job's
    nozzle temperature. None when the header does not list one target per filament (not Orca output): the
    caller then keeps the active spool's targets.
    """
    from .nozzle import effective_nozzle_temperature
    listed = [" ".join(str(m).split()) for m in materials]
    header = _header_numbers(text, "nozzle_temperature_initial_layer") or _header_numbers(text, "nozzle_temperature")
    if len(header) != len(listed):
        return None
    tools = [int(n) for n in re.findall(r"^[ \t]*T(\d+)\b", text or "", re.M)]
    start = min(tools[0] if tools else 0, len(listed) - 1)
    used = {t for t in tools if t < len(listed)} | {start}
    offset = effective_nozzle_temperature(recorded_nozzle_type(state), 200.0) - 200.0
    slots = [rec for rec in (state.get("filament_slots") or {}).values() if isinstance(rec, dict)]
    spool = state.get("spool") or {}
    resolved = {}
    for tool in sorted(used):
        label, shown = listed[tool], header[tool]
        norm = lambda m: " ".join(str(m or "").split()).casefold()  # noqa: E731
        # The slot spools holding exactly this label decide; the family only when no slot holds it.
        holders = ([rec for rec in slots if norm(rec.get("material")) == norm(label)]
                   or [rec for rec in slots if same_material(rec.get("material"), label)])
        ids = sorted({rec.get("spoolman_id") for rec in holders}, key=str) or [spool.get("spoolman_id")]
        options = [slice_temperatures({**state, "spool": {**spool, "spoolman_id": sid, "material": label}},
                                      None, fetch_filament,
                                      stored_ok=_is_active_spool(state, {"spoolman_id": sid, "material": label}))[0]
                   for sid in ids]
        if not any(abs(float(o["nozzle"]) - (shown - offset)) <= 1e-3 for o in options):
            raise RuntimeError(f"G-code tool T{tool} ({label}) is set to {shown:g} C but its spool resolves to "
                               + " or ".join(f"{float(o['nozzle']) + offset:g} C" for o in options))
        resolved[tool] = {"nozzle": shown - offset, "bed": options[0]["bed"]}
    nozzle = resolved[start]["nozzle"]
    # One plate heats the whole job: its target is the lowest-numbered tool's, the one orca_adapter sliced the bed at.
    return {"nozzle": int(nozzle) if float(nozzle).is_integer() else nozzle, "bed": resolved[min(resolved)]["bed"]}


def gcode_temperatures(state, materials, fetch_filament=None, text=None):
    """The temperatures a G-code sliced for ``materials`` (its filament_type list) must carry,
    resolved for the same spool slice_temperatures used: the active spool when its material is
    the G-code's, else the one slot spool holding that material, else the material defaults.
    A multi-filament G-code (``text`` given) is checked per tool against its own header instead
    (gcode_tool_temperatures). Two slot spools with that material and different targets are
    ambiguous and refuse.
    """
    labels = {" ".join(str(m).split()) for m in materials or () if str(m).strip()}
    spool = state.get("spool") or {}
    if text is not None and len([m for m in materials or () if str(m).strip()]) > 1:
        per_tool = gcode_tool_temperatures(state, [m for m in materials if str(m).strip()], text, fetch_filament)
        if per_tool is not None:
            return per_tool
    if len(labels) != 1:
        return slice_temperatures(state, None, fetch_filament)[0]
    label = labels.pop()
    ids = sorted({rec.get("spoolman_id") for rec in (state.get("filament_slots") or {}).values()
                  if isinstance(rec, dict) and " ".join(str(rec.get("material") or "").split()).casefold()
                  == label.casefold()}, key=str)
    if not ids or spool.get("spoolman_id") in ids:
        # The active spool holds it, or no slot does (a single-spool printer): slice_model sliced
        # the active spool, so resolve the same way.
        return slice_temperatures(state, None, fetch_filament)[0]
    # Another slot's spool: slice_model swapped to it and resolved its own targets.
    resolved = [slice_temperatures({**state, "spool": {**spool, "spoolman_id": sid, "material": label}},
                                   None, fetch_filament, stored_ok=False)[0] for sid in ids]
    if any(r != resolved[0] for r in resolved):
        raise RuntimeError(f"slots holding {label} resolve to different temperatures; reslice from one slot")
    return resolved[0]


def _material_temperature_contract(state):
    """Validate selected material temperatures and retain compatibility with old state files."""
    material = (state.get("spool") or {}).get("material")
    if not material:
        raise RuntimeError("selected spool has no material; cannot determine safe temperatures")
    resolution = state.get("temperature_resolution")
    if resolution and resolution.get("material_family"):
        if resolution["material_family"] != material_family(material):
            raise RuntimeError("stored temperature material does not match selected spool")
        return
    # A resolution without material_family is the "resolved per slice" marker that slot sync
    # and launch sync write; it names no material, so it cannot mismatch one. Fall through to
    # the recognised-material check instead of refusing every slice (2.2.3, Discord 2026-10-31).
    # Existing explicitly selected runtime temperatures remain valid for legacy state.
    # This only validates material recognition; it never substitutes PLA for another family.
    family = material_family(material)
    aliases = {"FLEXIBLE": "TPU", "NYLON": "PA", "POLYAMIDE": "PA"}
    family = aliases.get(family, family)
    config = settings.current()
    if family not in config.get("material_temps", DEFAULT_MATERIAL_TEMPS):
        raise RuntimeError(f"Unsupported filament material {material!r}; no PLA fallback is allowed")


def state_path(printer_id=None):
    """The hardware record for one printer: <state>/printers/<id>/printer_hardware.json.

    Derived from STATE's folder (the module's one state seam, which tests
    patch) rather than from settings, so the per-printer layout follows the
    same root the legacy record lives in. No printer named: the legacy path.
    """
    if printer_id is None:
        return Path(STATE)
    return Path(STATE).parent / "printers" / printer_id / "printer_hardware.json"


def _read_path(printer_id=None):
    """Where a load reads: the per-printer file, or the legacy file it adopts.

    Adoption is read-only and belongs to the default printer alone: a second
    printer must never inherit the singleton's loaded spool and nozzle.
    """
    target = state_path(printer_id)
    if printer_id is None or target.exists():
        return target
    try:
        default_id = settings.default_printer_id()
    except Exception:
        default_id = None
    if printer_id == default_id and Path(STATE).is_file():
        return Path(STATE)
    return target


def load(printer_id=None):
    with open(_read_path(printer_id)) as f:
        state = json.load(f)
    # A loaded slot with no color code gets one derived from its name (or a
    # neutral default it says out loud), never overwriting a valid hex. The
    # next save persists it; the listener upgrades it from Spoolman or the
    # printer's own report when those answer.
    from .filament_color import adopt_renamed_slots_key, fill_missing_slot_hexes
    adopt_renamed_slots_key(state)
    fill_missing_slot_hexes(state)
    return state


def save(st, printer_id=None):
    target = state_path(printer_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(target) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)


def _model_nozzle_default(view=None):
    """The nozzle type the printer model ships with: the configured/model [printer] nozzle_type,
    else None (unknown). Used to resolve a "stock" record to something the model really has."""
    try:
        return ((view if view is not None else settings.current()).get("printer") or {}).get("nozzle_type") or None
    except Exception:
        return None


def _seed_nozzle_type(printer, view):
    """The nozzle type a new record starts with: the model's own stock type when known (its
    configured/model default, else the first type it supports), else "stock" (unconfirmed,
    which refuses abrasive filament until the operator records the real nozzle)."""
    declared = printer.get("nozzle_type") or _model_nozzle_default(view)
    if declared:
        return declared
    try:
        return printers.get(printer["model"], view).supported_nozzle_types[0]
    except Exception:
        return "stock"


def seed_hardware_record(printer_id, view=None):
    """Create a newly onboarded printer's first hardware record (#537).

    Nothing else creates one: records otherwise appear only as a side
    effect of state-writing flows, and ``load``/``check`` raise on a
    missing file — so a printer added to the roster without a record could
    never pass readiness or bind a first spool. The seed carries only what
    the printer's own settings view already states (identity, bed, height,
    the configured nozzle size, the PLA temperature defaults, the Moonraker
    URL for the Moonraker check path) plus an empty spool and empty IFS
    slots: no spool is claimed loaded, no offset is marked verified. An
    existing record is never overwritten. Returns True when a record was
    created.
    """
    if state_path(printer_id).exists():
        return False
    view = view if view is not None else settings.view_for(printer_id)
    printer = view["printer"]
    pla = (view.get("material_temps") or {}).get("PLA") or DEFAULT_MATERIAL_TEMPS["PLA"]
    record = {
        "printer": printer.get("name") or printer_id,
        "moonraker_url": (view.get("services") or {}).get("moonraker_url") or "",
        "bed_mm": [float(v) for v in printer["bed_mm"]],
        "max_print_height_mm": float(printer["max_print_height_mm"]),
        "nozzle": {"diameter_mm": float(printer["nozzle_diameter_mm"]), "type": _seed_nozzle_type(printer, view),
                   "installed": datetime.date.today().isoformat()},
        "temps_c": {"nozzle": pla["nozzle"], "bed": pla["bed"]},
        "spool": {},
        "filament_slots": {},
    }
    save(record, printer_id)
    return True


def _get(url, data=None, method=None, timeout=6):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ZMod's AD5X colour-change purge, the saved variable use_trash_on_print
# (SAVE_ZMOD_DATA USE_TRASH_ON_PRINT=n; https://wiki.zmod.link/Global/ and
# https://wiki.zmod.link/AD5X/):
#   0 "Nopoop": ZMod does not purge into the trash chute; the slicer must purge
#     into the prime tower (Orca "Purge in prime tower", flush volumes).
#   1 default: ZMod purges into the trash chute on every colour change.
#   2 slicer-controlled poop: ZMod parks at the chute and the slicer's own
#     change-filament G-code purges there (ZMod's Orca profiles only).
PURGE_MODES = {0: "nopoop (slicer purges into the prime tower)",
               1: "trash chute (ZMod purges on colour change)",
               2: "slicer-controlled poop (ZMod profile G-code purges)"}


def purge_mode(variables):
    """use_trash_on_print from a save_variables dict: 0, 1 or 2, else None (missing or unknown)."""
    value = variables.get("use_trash_on_print") if isinstance(variables, dict) else None
    return value if type(value) is int and value in PURGE_MODES else None


def read_purge_mode(st):
    """The live ZMod purge mode for st's printer, or None when Moonraker cannot say."""
    url = (st or {}).get("moonraker_url")
    if not url:
        return None
    try:
        status = _get(f"{url}/printer/objects/query?save_variables")["result"]["status"]
        return purge_mode((status.get("save_variables") or {}).get("variables"))
    except Exception:
        return None


def sync_klipper_nozzle(moonraker_url, nozzle_diameter):
    """Update nozzle_diameter in printer.cfg via Moonraker upload API and restart Klipper."""
    cfg_url = f"{moonraker_url}/server/files/config/printer.cfg"
    req = urllib.request.Request(cfg_url)
    with urllib.request.urlopen(req, timeout=10) as r:
        content = r.read().decode("utf-8")

    pattern = r"^(nozzle_diameter\s*:\s*)([0-9.]+)"
    match = re.search(pattern, content, flags=re.MULTILINE)
    if not match:
        raise ValueError("Could not find nozzle_diameter in printer.cfg")

    current_val = float(match.group(2))
    if abs(current_val - nozzle_diameter) < 1e-4:
        return False

    new_content = re.sub(pattern, rf"\g<1>{nozzle_diameter:.3f}", content, count=1, flags=re.MULTILINE)

    boundary = "----KlipperConfigUploadBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="root"\r\n\r\n'
        f"config\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="printer.cfg"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"{new_content}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    upload_url = f"{moonraker_url}/server/files/upload"
    up_req = urllib.request.Request(upload_url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    })
    with urllib.request.urlopen(up_req, timeout=15) as r:
        pass

    restart_url = f"{moonraker_url}/printer/restart"
    res_req = urllib.request.Request(restart_url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(res_req, timeout=10) as r:
        pass

    return True


def pressure_advance_problems(st, live_pa):
    """Recorded pressure advance must match Klipper and belong to the installed nozzle."""
    rec = st.get("pressure_advance")
    if not rec:
        return []
    probs = []
    if rec.get("nozzle_installed") != (st.get("nozzle") or {}).get("installed"):
        probs.append(f"Pressure advance {rec.get('value')} was tuned for a different nozzle; retune it")
    try:
        if live_pa is not None and abs(float(live_pa) - float(rec["value"])) > 1e-4:
            probs.append(f"Klipper pressure_advance {live_pa} != recorded {rec['value']}")
    except (KeyError, TypeError, ValueError):
        probs.append("Recorded pressure advance is unreadable")
    return probs


def offset_authority_problems(st, variables, native):
    """Compare like-for-like stores; never substitute probe or another mode's offset."""
    import math
    def numeric(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    authority = st.get("calibration_authority") or {}
    mode = variables.get("display_off")
    if mode == 0:
        expected = (st.get("calibration_baseline") or {}).get("native_zProbeOffset")
        actual = (native.get("leftExtruderOffset") or {}).get("zProbeOffset")
        if authority.get("mode") != "native" or authority.get("operator_confirmed") is not True:
            return ["native calibration authority is not operator confirmed"]
        if not numeric(expected) or not numeric(actual) or abs(expected - actual) > 0.000001:
            return ["native zProbeOffset missing or differs from the operator-confirmed baseline"]
        return []
    if mode == 1:
        expected = st.get("z_offset_mm")
        actual = (variables.get("gcode_offsets") or {}).get("z")
        if (authority.get("mode") != "screenless" or authority.get("operator_confirmed") is not True
                or not st.get("z_offset_verified") or variables.get("load_zoffset") != 1
                or not numeric(expected) or not numeric(actual) or abs(expected - actual) > 0.000001):
            return ["screenless user offset has no verified matching authority; native calibration cannot substitute"]
        return []
    return ["calibration mode is unknown; no offset substitution permitted"]


def spoolman_url(st=None):
    """The Spoolman base URL to use: configured settings win over the hardware record.

    `services.spoolman_url` in settings (which includes the
    PRINT_HANDY__SERVICES__SPOOLMAN_URL and legacy SPOOLMAN_URL environment
    variables) is the operator's current configuration. The copy cached in
    printer_hardware.json can go stale during migrations — it kept pointing at
    a decommissioned host while the bundled Spoolman lived on 127.0.0.1:8000
    (#235) — so it is only a fallback for when settings do not name a URL.
    """
    try:
        configured = (settings.current().get("services") or {}).get("spoolman_url")
    except Exception:
        configured = None
    for candidate in (configured, (st or {}).get("spoolman_url")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().rstrip("/")
    return ""


def _ams_disagreements(st, facts):
    """The Bambu printer's own AMS report (#307) against the configured spool record.

    Advisory only, on the #235 pattern: the decoded active tray (ams.py, surfaced on
    facts["bambu"]["ams"]) is an additional source next to the record, never a
    replacement for it — the configured record stays authoritative, so a disagreement
    is appended to facts["warnings"] to be shown, never silently resolved, and never
    a readiness problem. Colour is compared only when both sides are hex, and only a
    clear difference warns (filament_color.same_color), so FFFFFF against FAFAFA is quiet.
    The record side is the spool's hex, else its colour field when that is itself hex;
    a colour name is never compared against the printer's hex.
    """
    decoded = (facts.get("bambu") or {}).get("ams") or {}
    active = None
    if decoded.get("active_tray") == 254:  # the external spool is the active tray
        active = decoded.get("external")
    else:
        for unit in decoded.get("units") or []:
            for tray in unit.get("trays") or []:
                if tray.get("active"):
                    active = tray
    if not active or not active.get("present"):
        return
    spool = st.get("spool") or {}
    warnings = facts["warnings"]
    live_material, recorded_material = active.get("material"), spool.get("material")
    if (live_material and recorded_material
            and not same_material(live_material, recorded_material)):
        warnings.append(f"the printer reports its active Bambu tray holds {live_material}, "
                        f"but the recorded spool is {recorded_material}; the record is kept — "
                        "check what is loaded before printing")
    live_color = filament_color.first_hex(active.get("color"))
    recorded_color = filament_color.first_hex(spool.get("hex")) or filament_color.first_hex(spool.get("color"))
    if filament_color.same_color(live_color, recorded_color) is False:
        warnings.append(f"the printer reports its active Bambu tray colour is #{live_color}, "
                        f"but the recorded spool colour is #{recorded_color}; the record is kept")


def _check_spoolman(st, warnings, facts):
    """The recorded spool against its Spoolman record; every outcome is a warning (#235).

    Spoolman is inventory/accounting, never a hardware gate: a lookup failure,
    timeout, missing record, archived spool or name drift must warn and let
    slicing and printing proceed on the recorded state (material, density and
    temperatures already live in printer_hardware.json). Nothing here appends
    to the caller's problems list.
    """
    spool_id = (st.get("spool") or {}).get("spoolman_id")
    base = spoolman_url(st)
    if not base or spool_id is None:
        warnings.append("Spoolman check skipped: no Spoolman URL or no recorded spool; "
                        "continuing on the recorded spool state")
        return
    try:
        sp = spoolman_client.request("GET", f"/api/v1/spool/{spool_id}", base_url=base)
        filament = sp["filament"]
        facts["spool_name"] = filament["name"]
        facts["spool_material"] = filament.get("material")
        facts["spool_remaining_g"] = round(sp.get("remaining_weight") or 0)
        if sp.get("archived"):
            warnings.append(f"Spoolman spool #{sp['id']} is archived")
        if filament["name"] != st["spool"]["name"]:
            warnings.append(f"Spoolman #{sp['id']} is '{filament['name']}', state says '{st['spool']['name']}'")
        recorded_material, spoolman_material = st["spool"].get("material"), filament.get("material")
        if recorded_material and spoolman_material and not same_material(recorded_material, spoolman_material):
            warnings.append(f"Spoolman #{sp['id']} material is {spoolman_material!r}, but the recorded spool "
                            f"is {recorded_material!r}; the record is kept — check what is loaded, then "
                            "re-run set --spool if Spoolman is right")
    except Exception as e:
        warnings.append("Spoolman could not be reached, so the recorded spool was used instead; "
                        "printing can continue (#235)")


def check(st=None, printer_id=None, adapter=None):
    """Return (ok, problems, facts). Never raises on network errors; reports them.

    Spoolman outcomes never affect `ok`: they are collected in
    facts["warnings"] (alias facts["spoolman_warnings"]) so callers can surface
    them without refusing work (#235). Problems are the real hardware gates:
    Moonraker/Klipper state, nozzle, calibration authority and adapter readiness.

    With a `printer_id` (#334) the record and the adapter are that roster
    entry's — the adapter is built on the entry's settings view — and the
    result's facts name the printer ("printer_id", "printer_name"). Without
    one, this is the configured default printer, exactly as before.
    """
    st = st or load(printer_id)
    probs, facts = [], {}
    warnings = []
    facts["warnings"] = warnings
    facts["spoolman_warnings"] = warnings
    if adapter is None:
        if printer_id is not None:
            entry = settings.by_id(printer_id)
            adapter = printers.get(entry["model"], settings.view_for(printer_id))
            facts["printer_id"], facts["printer_name"] = entry["id"], entry["name"]
        else:
            adapter = printers.current()
            try:
                entry = settings.printers()[0]
                facts["printer_id"], facts["printer_name"] = entry["id"], entry["name"]
            except Exception:
                pass
    elif printer_id is not None:
        facts["printer_id"] = printer_id
    if getattr(adapter, 'transport', None) == 'octoprint':
        try:
            snapshot = adapter.client().status()
            facts['octoprint'] = adapter.normalize_status(snapshot)
            probs.extend(adapter.readiness_problems(st, snapshot))
        except RuntimeError as exc:
            probs.append(str(exc))
        # Nozzle/calibration and loaded spool are operator records on Marlin.
        return (not probs), probs, facts
    if getattr(adapter, 'transport', None) == 'bambulab_lan':
        # Bambu Lab: the hardware record and Spoolman, plus read-only LAN MQTT telemetry when the
        # operator configured [bambu] lan_host/lan_serial. Never Moonraker. An unconfigured LAN is
        # the original offline behaviour, not an error.
        facts["connection"] = "bambulab_lan"
        facts["lan_configured"] = adapter.lan_configured()
        if adapter.lan_configured():
            try:
                snapshot = adapter.client().snapshot()
                facts["bambu"] = adapter.normalize_status(snapshot)
                probs.extend(adapter.readiness_problems(st, snapshot))
                _ams_disagreements(st, facts)
            except RuntimeError as exc:
                probs.append(str(exc))
        else:
            probs.extend(adapter.readiness_problems(st, {}))
        _check_spoolman(st, warnings, facts)
        return (not probs), probs, facts
    if getattr(adapter, 'transport', None) == 'prusalink':
        # Prusa: the hardware record and Spoolman, plus read-only PrusaLink status when the
        # operator configured [prusa] link_host. Never Moonraker. An unconfigured link is the
        # offline behaviour, not an error.
        facts["connection"] = "prusalink"
        facts["link_configured"] = adapter.link_configured()
        if adapter.link_configured():
            try:
                snapshot = adapter.client().snapshot()
                facts["prusa"] = adapter.normalize_status(snapshot)
                probs.extend(adapter.readiness_problems(st, snapshot))
            except RuntimeError as exc:
                probs.append(str(exc))
        else:
            probs.extend(adapter.readiness_problems(st, {}))
        _check_spoolman(st, warnings, facts)
        return (not probs), probs, facts
    if getattr(adapter, 'transport', None) == 'elegoo_sdcp':
        # Elegoo SDCP: the hardware record and Spoolman, plus read-only SDCP status when the
        # operator configured [elegoo_sdcp] host. Never Moonraker. An unconfigured host is
        # the offline behaviour, not an error.
        facts["connection"] = "elegoo_sdcp"
        facts["sdcp_configured"] = adapter.sdcp_configured()
        if adapter.sdcp_configured():
            try:
                snapshot = adapter.client().status()
                facts["elegoo_sdcp"] = adapter.normalize_status(snapshot)
                probs.extend(adapter.readiness_problems(st, snapshot))
            except RuntimeError as exc:
                probs.append(str(exc))
        else:
            probs.extend(adapter.readiness_problems(st, {}))
        _check_spoolman(st, warnings, facts)
        return (not probs), probs, facts
    if adapter.connection == 'offline':
        # No printer connection: the hardware record and Spoolman only, never Moonraker.
        facts["connection"] = "offline"
        probs.extend(adapter.readiness_problems(st, {}))
        _check_spoolman(st, warnings, facts)
        return (not probs), probs, facts
    native = adapter.has_native_screen  # offset authority is a ZMod screen concept
    mr = st["moonraker_url"]
    try:
        s = _get(f"{mr}/printer/objects/query?configfile=settings")["result"]["status"]["configfile"]["settings"]
        facts["klipper_nozzle"] = float(s["extruder"]["nozzle_diameter"])
        facts["klipper_probe_z_offset"] = float(s.get("probe", {}).get("z_offset", "nan"))
        probs.extend(pressure_advance_problems(st, s["extruder"].get("pressure_advance")))
        recorded_probe = (st.get("calibration_baseline") or {}).get("probe_z_offset", st.get("z_offset_mm"))
        if native and abs(facts["klipper_probe_z_offset"] - float(recorded_probe)) > 1e-3:
            probs.append(
                f"Klipper probe.z_offset {facts['klipper_probe_z_offset']} != recorded probe offset {recorded_probe}. "
                "This is the probe trigger, not the user offset; do not copy it into gcode_offsets.z or zProbeOffset"
            )
    except Exception as e:
        probs.append("the printer could not be reached — check it is on and on the network, then try again")
    if "klipper_nozzle" in facts and abs(facts["klipper_nozzle"] - st["nozzle"]["diameter_mm"]) > 1e-3:
        probs.append(f"Klipper nozzle_diameter {facts['klipper_nozzle']} != state {st['nozzle']['diameter_mm']}")
    try:
        facts["moonraker_active_spool"] = _get(f"{mr}/server/spoolman/spool_id")["result"].get("spool_id")
        if facts["moonraker_active_spool"] != st["spool"]["spoolman_id"]:
            facts["active_spool_mismatch"] = f"Moonraker active spool {facts['moonraker_active_spool']} != state #{st['spool']['spoolman_id']}"
    except Exception as e:
        warnings.append("the printer's active spool could not be checked, so the recorded spool was used")
    _check_spoolman(st, warnings, facts)
    try:
        if not native:
            return (not probs), probs, facts
        variables = _get(f"{mr}/printer/objects/query?save_variables")["result"]["status"]["save_variables"]["variables"]
        native = _get(f"{mr}/server/files/config/Adventurer5M.json") if variables.get("display_off") == 0 else {}
        calibration_problems = offset_authority_problems(st, variables, native)
        probs.extend(calibration_problems)
        facts["calibration_valid"] = not calibration_problems
        facts["calibration_owner"] = "native" if variables.get("display_off") == 0 else "screenless" if variables.get("display_off") == 1 else "unknown"
        facts["purge_mode"] = PURGE_MODES.get(purge_mode(variables), f"purge mode unknown, assuming trash chute (use_trash_on_print={variables.get('use_trash_on_print')!r})")
    except Exception as e:
        probs.append("the printer's saved calibration could not be read — check the printer screen, then try again")
    return (not probs), probs, facts


def summary(st, calibration_owner="unknown", calibration_valid=None):
    resolution = st.get("temperature_resolution") or {}
    material = (st.get("spool") or {}).get("material", "unknown")
    source = resolution.get("source", "explicit/legacy state")
    temp_summary = f"{material} nozzle {st['temps_c']['nozzle']}°C, bed {st['temps_c']['bed']}°C ({source})"
    if not printers.current().has_native_screen:
        return (f"{st['nozzle']['diameter_mm']} mm {st['nozzle']['type']} nozzle, "
                f"Spoolman #{st['spool']['spoolman_id']} {st['spool']['name']} ({material}), "
                f"{temp_summary}")
    if calibration_owner == "native":
        offset = (st.get("calibration_baseline") or {}).get("native_zProbeOffset", "unknown")
        calibration = f"native zProbeOffset {offset} (screenless user offset not applicable)"
    elif calibration_owner == "screenless":
        value = st.get("z_offset_mm")
        verified = st.get("z_offset_verified") is True
        calibration = f"screenless user offset {value} ({'verified' if verified else 'unverified'})"
    else:
        calibration = f"user offset {st.get('z_offset_mm')} ({'unverified' if not st.get('z_offset_verified') else 'verified'}); calibration mode unknown"
    # Only the live checker may report validation. A renderer without its live
    # inputs must not synthesize a failure from absent gcode_offsets/native data.
    if calibration_valid is False:
        calibration += "; strict calibration validation failed"
    return (f"{st['nozzle']['diameter_mm']} mm {st['nozzle']['type']} nozzle, "
            f"probe z_offset {((st.get('calibration_baseline') or {}).get('probe_z_offset', st.get('z_offset_mm')))}, "
            f"{calibration}, "
            f"Spoolman #{st['spool']['spoolman_id']} {st['spool']['name']} ({st['spool']['material']}), "
            f"{st['temps_c']['nozzle']}/{st['temps_c']['bed']} C")


def fetch_spoolman_spool(spool_id, st=None):
    """The Spoolman record for the operator `set --spool` command.

    An explicit operator operation against Spoolman itself, so an
    unavailable Spoolman raises (SpoolmanUnavailable) instead of being
    swallowed; the transport is still the one wrapper (#387).
    """
    return spoolman_client.request("GET", f"/api/v1/spool/{spool_id}",
                                   base_url=spoolman_url(st))


def _nozzle_arg(text):
    """argparse type for --nozzle: accepts units and comma decimals like validate_nozzle_setting."""
    from .nozzle import NozzleError, parse_nozzle_diameter
    try:
        return parse_nozzle_diameter(text)
    except NozzleError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check"); sub.add_parser("show")
    s = sub.add_parser("set")
    s.add_argument("--spool", type=int); s.add_argument("--nozzle", type=_nozzle_arg, help="diameter in mm: 0.4, 0,4 or 0.40mm")
    s.add_argument("--nozzle-type"); s.add_argument("--nozzle-temp", type=int); s.add_argument("--bed-temp", type=int)
    s.add_argument("--z-offset", type=float, help="record a measured user offset; does not write Klipper or the native screen")
    s.add_argument("--sync-z", action="store_true", help="record live probe.z_offset as the probe store only; never marks the user offset verified")
    s.add_argument("--pressure-advance", type=float, help="record the pressure advance tuned for the installed nozzle; does not write Klipper")
    s.add_argument("--no-sync-klipper", action="store_true", help="skip syncing nozzle to Klipper printer.cfg")
    a = ap.parse_args()
    st = load()
    if a.cmd == "show":
        print(json.dumps(st, indent=2)); return
    if a.cmd == "check":
        ok, probs, facts = check(st)
        print(("HW OK: " if ok else "HW MISMATCH: ") + summary(st, facts.get("calibration_owner", "unknown"), facts.get("calibration_valid")) +
              (f", {facts.get('spool_remaining_g')} g left" if "spool_remaining_g" in facts else "") +
              (f", IFS purge: {facts['purge_mode']}" if "purge_mode" in facts else ""))
        for p in probs:
            print("  - " + p)
        for w in facts.get("warnings") or []:
            print("  - warning: " + w)
        spacing = settings.spacing_summary(settings.current())
        print(f"  part spacing: {spacing['label']} preset - "
              + ", ".join(f"{name} {value:g}" for name, value in spacing["values"].items())
              + (f" (set explicitly: {', '.join(spacing['overridden'])})" if spacing["overridden"] else ""))
        sys.exit(0 if ok else 2)
    # set
    if a.spool is not None:
        sp = fetch_spoolman_spool(a.spool, st)
        f = sp["filament"]
        st["spool"] = {"spoolman_id": a.spool, "name": f["name"], "material": f.get("material"),
                       "diameter_mm": f.get("diameter", 1.75), "density": f.get("density", 1.24)}
        spool_hex = filament_color.first_hex(f.get("color_hex")) or filament_color.first_hex(f.get("multi_color_hexes"))
        if spool_hex:
            st["spool"]["hex"] = spool_hex
        family, targets, sources = resolve_material_temperatures(f.get("material"), f)
        from .nozzle import effective_nozzle_temperature, recorded_nozzle_type
        nozzle_type = recorded_nozzle_type((st.get("nozzle") or {}).get("type"), _model_nozzle_default())
        effective_nozzle_temperature(nozzle_type, targets["nozzle"])
        st["temps_c"] = {"nozzle": targets["nozzle"], "bed": targets["bed"]}
        st["temperature_resolution"] = {"material_family": family, "material": f.get("material"),
                                         "spoolman_id": a.spool, "sources": sources,
                                         "source": "Spoolman filament" if all(v == "Spoolman filament" for v in sources.values()) else "material default / Spoolman filament" if len(set(sources.values())) > 1 else next(iter(sources.values()), "material default")}
        _get(f"{st['moonraker_url']}/server/spoolman/spool_id", {"spool_id": a.spool}, "POST")
    if a.nozzle is not None or a.nozzle_type is not None:
        from .nozzle import (CANONICAL_NOZZLE_SIZES, NozzleError, normalize_nozzle_type, recorded_nozzle_type,
                             validate_nozzle_setting)
        current_nozzle = st.get("nozzle") or {}
        dia_in = a.nozzle if a.nozzle is not None else current_nozzle.get("diameter_mm", 0.4)
        raw_type = a.nozzle_type if a.nozzle_type is not None else recorded_nozzle_type(
            current_nozzle.get("type"), _model_nozzle_default())
        
        # Check against active printer capabilities if available
        supported_sizes = None
        supported_types = None
        try:
            cur_p = printers.current()
            supported_sizes = cur_p.supported_nozzle_sizes
            supported_types = cur_p.supported_nozzle_types
        except Exception:
            pass

        try:
            dia, norm_type = validate_nozzle_setting(
                dia_in,
                raw_type,
                supported_sizes=supported_sizes,
                supported_types=supported_types,
            )
        except NozzleError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        st["nozzle"] = {
            "diameter_mm": dia,
            "type": norm_type,
            "installed": datetime.date.today().isoformat() if a.nozzle is not None else current_nozzle.get("installed", datetime.date.today().isoformat()),
        }
        if a.nozzle is not None:
            st["z_offset_verified"] = False
            st["z_offset_mm"] = None
            st["calibration_authority"] = {"mode": None, "operator_confirmed": False, "reason": "Nozzle changed; previous calibration is historical only"}
            st["z_offset_note"] = "Nozzle changed; calibration needs a measured confirmation for the active mode. --sync-z only records the probe trigger."
            if st.get("pressure_advance"):
                st["pressure_advance"]["stale"] = True
            if not a.no_sync_klipper:
                try:
                    if sync_klipper_nozzle(st["moonraker_url"], a.nozzle):
                        print(f"Klipper printer.cfg updated to {a.nozzle:.3f} mm and Klipper restarted.")
                    else:
                        print(f"Klipper printer.cfg already matches {a.nozzle:.3f} mm.")
                except Exception as e:
                    print(f"Warning: Failed to sync nozzle to Klipper printer.cfg: {e}")
    if getattr(a, "sync_z", False):
        try:
            s = _get(f"{st['moonraker_url']}/printer/objects/query?configfile=settings")["result"]["status"]["configfile"]["settings"]
            live_z = float(s.get("probe", {}).get("z_offset", "nan"))
            if live_z == live_z:
                st.setdefault("calibration_baseline", {})["probe_z_offset"] = live_z
                print(f"Recorded live Klipper probe.z_offset ({live_z} mm) as the probe store only. User offset unchanged.")
            else:
                print("Warning: Could not read probe.z_offset from Klipper.")
        except Exception as e:
            print(f"Warning: Failed to read live z_offset from Klipper: {e}")
    if a.z_offset is not None:
        st["z_offset_mm"], st["z_offset_verified"] = a.z_offset, True
        st["z_offset_note"] = (
            f"Operator-supplied measured user offset on {datetime.date.today()} with "
            f"{st['nozzle']['diameter_mm']} {st['nozzle']['type']}. Not copied from probe.z_offset."
        )
    if a.pressure_advance is not None:
        st["pressure_advance"] = {"value": a.pressure_advance, "nozzle_installed": st["nozzle"].get("installed"),
                                  "recorded": datetime.date.today().isoformat()}
    if a.nozzle_temp: st["temps_c"]["nozzle"] = a.nozzle_temp
    if a.bed_temp: st["temps_c"]["bed"] = a.bed_temp
    if a.nozzle_temp or a.bed_temp:
        # An explicit temperature holds for this spool only; another spool resolves its own.
        spool = st.get("spool") or {}
        st["temperature_resolution"] = {**(st.get("temperature_resolution") or {}),
                                        "material_family": material_family(spool.get("material")),
                                        "material": spool.get("material"), "spoolman_id": spool.get("spoolman_id"),
                                        "source": "operator setting"}
    st["updated"] = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    save(st)
    print("saved: " + summary(st))


if __name__ == "__main__":
    main()
