"""ZMod screen sync: the CHANGE_ZCOLOR command that tells the AD5X screen what is loaded in a slot."""
import re

SLOTS = (1, 2, 3, 4)
SCREEN_TYPES = ("PLA", "ABS", "PETG", "TPU", "PLA-CF", "PETG-CF", "SILK")
# The screen also accepts "?" for a material it has no type for (seen live on the AD5X).
UNKNOWN_TYPE = "?"
# Families the screen has no type of its own for, shown as the nearest type it has.
NEAREST_TYPES = {"ASA": "ABS", "ASA-CF": "ABS", "ABS-CF": "ABS"}


def screen_type(material):
    """Spoolman material -> the type the AD5X screen shows for it, or None for a blank material.

    Derived from the material's family and variants, never from its spelling: PLA+ / PLA Pro /
    PLA Matte / Matte PLA / Wood PLA -> PLA, Silk PLA (bare "Silk" too) -> SILK, Silk PLA-CF ->
    PLA-CF, PET-G -> PETG. The screen's SILK is silk PLA only, so Silk PETG is PETG. A material
    the screen has no type for falls back (#798): ASA, ASA-CF and ABS-CF -> ABS, the nearest
    type, and anything else (PC, PCTG, PA, nylon, an unknown name) -> "?", the screen's own
    placeholder, so the color still reaches the screen instead of the push failing. Only a
    blank material returns None. fallback_note says when a fallback was used.
    """
    from ....core.printer_state import material_family, material_variants
    if not str(material or "").strip():
        return None
    family = material_family(material)
    if family == "PLA":
        return "SILK" if "SILK PLA" in material_variants(material) else "PLA"
    if family in SCREEN_TYPES and family != "SILK":
        return family
    return NEAREST_TYPES.get(family, UNKNOWN_TYPE)


def fallback_note(material):
    """A short phrase when the screen shows a stand-in type for this material, else None.

    "screen shows ABS for ASA" / "screen shows ? for PA". PLA+ and the other PLA finishes are
    not fallbacks: the screen's PLA is their family. Bind and sync results append the phrase so
    the operator is told the screen label is not the Spoolman material.
    """
    from ....core.printer_state import material_family
    stype = screen_type(material)
    family = material_family(material)
    if stype is None or family == "PLA" or family in SCREEN_TYPES:
        return None
    return f"screen shows {stype} for {str(material).strip()}"


def label_matches(label, material):
    """True when a slot's screen label agrees with the Spoolman material loaded there.

    The screen has seven types, so CHANGE_ZCOLOR writes "SILK" for Silk PLA and the
    printer reports it back that way. A label matches when it is the same family
    (PLA == PLA+) or is exactly the screen type Print Handy would write for the
    material (SILK for Silk PLA). The SILK label is the exception to the family rule:
    material_family reads a bare "Silk" as PLA, but a slot the screen marks SILK holds
    silk PLA, never a plain PLA spool. A fallback label (ABS for ASA, "?" for PA, #798) matches
    only the material it stands in for, so a mapped launch is not refused for a label Print
    Handy wrote itself. It never widens the family check otherwise.
    """
    from ....core.printer_state import same_material
    native = re.sub(r"[\s_]+", "", str(label or "").upper())
    if native == "SILK":
        return screen_type(material) == "SILK"
    if same_material(label, material):
        return True
    return native in (*SCREEN_TYPES, UNKNOWN_TYPE) and native == screen_type(material)


def screen_command(slot, material, hex_code):
    from ....hermes.spool_sync import SpoolError, screen_hex
    stype = screen_type(material)
    hx = screen_hex(hex_code)
    if slot not in SLOTS:
        raise SpoolError(f"slot {slot!r} is not 1-4")
    if stype is None:
        raise SpoolError(f"the printer screen has no type for {material!r}; screen left unchanged")
    if hx is None:
        raise SpoolError(f"spool color {hex_code!r} is not a hex color; screen left unchanged")
    return f"CHANGE_ZCOLOR SLOT={slot} TYPE={stype} HEX={hx}"
