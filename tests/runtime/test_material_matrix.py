"""Material vocabulary matrix (2026-10-31 audit): one label -> family, variant, abrasive flag, the
temperatures it prints at, and the AD5X screen type. Every expectation is written out, not derived,
so a change to the vocabulary shows up here as a visible diff.

Temperatures are the built-in material defaults (no [material_temps] override, no Spoolman values):
PLA 210/60, PETG 240/85, PCTG 250/80, ABS 245/100, ASA 260/105, TPU 230/50, PA 260/90, PC 275/110,
plus the variant nozzle delta (Silk +10, PLA+ +5, Matte +5, CF +5, PA-CF +10; stacked: the larger).
"""
import pytest

from print_handy.core import printer_state
from print_handy.core.nozzle import is_abrasive_material
from print_handy.printers.flashforge.zmod import screen

# label, family, dominant variant, abrasive, nozzle C, bed C, AD5X screen type
MATRIX = [
    # Plain families and the labels that resolved before the audit.
    ("PLA", "PLA", None, False, 210, 60, "PLA"),
    ("PLA+", "PLA", "PLA+", False, 215, 60, "PLA"),
    ("Silk PLA", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Matte PLA", "PLA", "MATTE PLA", False, 215, 60, "PLA"),
    ("PLA Matte", "PLA", "MATTE PLA", False, 215, 60, "PLA"),
    ("PETG", "PETG", None, False, 240, 85, "PETG"),
    ("ABS", "ABS", None, False, 245, 100, "ABS"),
    ("ASA", "ASA", None, False, 260, 105, "ABS"),
    ("TPU", "TPU", None, False, 230, 50, "TPU"),
    ("PA", "PA", None, False, 260, 90, "?"),
    ("PC", "PC", None, False, 275, 110, "?"),
    ("PLA-CF", "PLA-CF", "PLA-CF", True, 215, 60, "PLA-CF"),
    ("PETG-CF", "PETG-CF", "PETG-CF", True, 245, 85, "PETG-CF"),
    # Nylon grades (PA6, PA11, PA12, PAHT) are PA; reinforced grades are PA-CF.
    ("PA6", "PA", None, False, 260, 90, "?"),
    ("PA12", "PA", None, False, 260, 90, "?"),
    ("PA11", "PA", None, False, 260, 90, "?"),
    ("PA6-CF", "PA-CF", "PA-CF", True, 270, 90, "?"),
    ("PA12-CF", "PA-CF", "PA-CF", True, 270, 90, "?"),
    ("PAHT-CF", "PA-CF", "PA-CF", True, 270, 90, "?"),
    # PET prints like PETG (same polymer, same profile); PCTG is its own family near PETG.
    ("PET", "PETG", None, False, 240, 85, "PETG"),
    ("PET-G", "PETG", None, False, 240, 85, "PETG"),
    ("PET-CF", "PETG-CF", "PETG-CF", True, 245, 85, "PETG-CF"),
    ("PCTG", "PCTG", None, False, 250, 80, "?"),
    # Shore hardness is not part of the family.
    ("TPU 95A", "TPU", None, False, 230, 50, "TPU"),
    ("TPU-95A", "TPU", None, False, 230, 50, "TPU"),
    # Silk variants: multi-colour silk is still silk PLA; bare "Silk" is Silk PLA.
    ("Silk Rainbow PLA", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Silk Dual Color PLA", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Dual-Color Silk PLA", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Tri-color Silk PLA", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Silk", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("SILK", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    # A metallic pigment look is not metal fill.
    ("Silk PLA Metallic Gold", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Metallic PLA", "PLA", None, False, 210, 60, "PLA"),
    # Fill materials print at PLA temperatures; the wearing ones are flagged abrasive.
    ("Wood PLA", "PLA", None, True, 210, 60, "PLA"),
    ("PLA Wood", "PLA", None, True, 210, 60, "PLA"),
    ("Hardwood PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Glow PLA", "PLA", None, True, 210, 60, "PLA"),
    ("PLA Glow", "PLA", None, True, 210, 60, "PLA"),
    ("Marble PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Sparkle PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Galaxy PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Metal PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Basalt PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Ceramic PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Copper PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Bronze PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Steel PLA", "PLA", None, True, 210, 60, "PLA"),
    ("Aluminum PLA", "PLA", None, True, 210, 60, "PLA"),
    # Variants stack: the larger nozzle delta wins and CF keeps its abrasive flag.
    ("PLA+ Silk", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Silk PLA+", "PLA", "SILK PLA", False, 220, 60, "SILK"),
    ("Matte PLA+", "PLA", "MATTE PLA", False, 215, 60, "PLA"),
    ("Silk PLA-CF", "PLA-CF", "SILK PLA", True, 220, 60, "PLA-CF"),
    # The screen's SILK is silk PLA only; it has no ABS-CF type, so ABS-CF shows ABS (#798).
    ("Silk PETG", "PETG", None, False, 240, 85, "PETG"),
    ("ABS-CF", "ABS-CF", "REINFORCED", True, 250, 100, "ABS"),
    # Bambu sub-brands carry the vendor name.
    ("Bambu PLA Basic", "PLA", None, False, 210, 60, "PLA"),
]


@pytest.mark.parametrize("label, family, variant, abrasive, nozzle, bed, screen_label", MATRIX,
                         ids=[row[0] for row in MATRIX])
def test_material_matrix(label, family, variant, abrasive, nozzle, bed, screen_label):
    assert printer_state.material_family(label) == family
    assert printer_state.material_variant(label) == variant
    assert is_abrasive_material(label) is abrasive
    assert printer_state.material_adjustments(label)["abrasive"] is abrasive
    resolved, temps, _ = printer_state.resolve_material_temperatures(label, {}, {"material_temps": {}})
    assert resolved == family
    assert temps == {"nozzle": nozzle, "bed": bed}
    assert screen.screen_type(label) == screen_label


# Deliberate refusals: no checked profile or no print temperatures, so no family is guessed.
REFUSED = ["HS", "HF", "High Speed", "High Speed PLA", "PLA HS", "PETG-HF", "NoPLA+", "", None, "Wood",
           "PLA/PHA", "PLA-S", "PA-S", "Support for PLA"]


@pytest.mark.parametrize("label", REFUSED, ids=[repr(label) for label in REFUSED])
def test_refused_labels_have_no_temperatures_and_only_the_placeholder_screen_type(label):
    with pytest.raises(ValueError, match="Unsupported filament material"):
        printer_state.resolve_material_temperatures(label, {}, {"material_temps": {}})
    # No family is guessed for slicing, but the screen still gets its "?" placeholder (#798);
    # a blank material has nothing to show.
    assert screen.screen_type(label) == (None if not label else "?")


def test_support_grades_are_their_own_families_and_never_match_the_polymer_they_support():
    assert printer_state.material_family("PLA-S") == "SUPPORT-PLA"
    assert printer_state.material_family("Support for PLA") == "SUPPORT-PLA"
    assert printer_state.material_family("PA-S") == "SUPPORT-PA"
    assert not printer_state.same_material("PLA-S", "PLA")
    assert not printer_state.same_material("PA-S", "PA")
    assert printer_state.same_material("PLA-S", "Support for PLA")


def test_pctg_is_not_petg_and_pet_is():
    assert not printer_state.same_material("PCTG", "PETG")
    assert printer_state.same_material("PET", "PETG")
    assert printer_state.same_material("PET-CF", "PETG-CF")


@pytest.mark.parametrize("label, expected", [
    # whole tokens, not substrings
    ("Silk PLA Metallic Gold", False), ("Metallic PLA", False), ("Pearl PLA", False), ("PLA", False),
    ("Hardwood PLA", True), ("Wood PLA", True), ("Metal-fill PLA", True), ("Metal PLA", True),
    ("Basalt PLA", True), ("Ceramic PLA", True), ("Copper PLA", True), ("Bronze PLA", True),
    ("Steel PLA", True), ("Aluminum PLA", True), ("Aluminium PLA", True), ("Magnetic PLA", True),
    ("Luminous PLA", True), ("Glow-in-the-dark PLA", True), ("Kevlar Nylon", True), ("Mica PLA", True),
    ("Marble PLA", True), ("Galaxy PLA", True), ("Sparkle PLA", True), ("Glitter PLA", True),
    ("PLA-CF", True), ("PETG-GF", True), ("PA6-CF", True), ("PAHT-CF", True), ("PLACF", True),
    ("PETG Carbon Fiber", True), ("Glass fibre PA", True), ("Silk PLA-CF", True),
    (None, False), ("", False),
])
def test_abrasive_detection_is_token_based(label, expected):
    assert is_abrasive_material(label) is expected


def test_the_screen_label_agrees_with_what_print_handy_writes():
    assert screen.label_matches("SILK", "Silk")
    assert screen.label_matches("SILK", "Silk Rainbow PLA")
    assert screen.label_matches("PLA", "Silk PLA")
    assert screen.label_matches("PLA", "Matte PLA")
    assert screen.label_matches("PLA-CF", "Silk PLA-CF")
    assert not screen.label_matches("SILK", "Silk PLA-CF")  # the screen shows PLA-CF for it
    assert not screen.label_matches("SILK", "Silk PETG")
    assert not screen.label_matches("SILK", "PLA")
    assert not screen.label_matches("SILK", "PETG")
