"""Sourced hardware facts and template provenance for Bambu Lab printers. Supplemental metadata only.

Connection, start code, slicers, delivery, defaults and registration are decided by VENDOR (bambulab/__init__.py)
and each MODEL; nothing here overrides them. These are the values the A1 guard enforces, each with its source:

  ORCA     OrcaSlicer's own Bambu Lab (BBL) vendor profiles at ORCA_RELEASE, read directly from that commit,
           or OrcaSlicer's output with those profiles. Only the values below are recorded; the profiles
           themselves are never copied into this repository (profiles.py generates them on the operator's
           machine from their own OrcaSlicer installation).
  BAMBU    Bambu Lab product and tech-spec pages. Not reachable from the sandbox that wrote these files; read
           from search-result extracts. Re-check against bambulab.com before relying on them.
  DERIVED  Computed from the ORCA G-code templates, not published by Bambu. Not a hardware certification.
verified=True means the value was read from its source directly; False means it must be re-checked.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


# OrcaSlicer release whose BBL vendor profiles were checked: (tag, commit). A provenance reference only; the
# profiles (AGPL-3.0) are not in this repository. See docs/BAMBU-LAB.md "Provenance and licence".
ORCA_RELEASE = ("v2.5.0-rc", "af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea")
ORCA_PROFILE_ROOT = "resources/profiles/BBL"
# Print Handy's only changes to the flattened vendor profiles, by kind. The shared post-slice guard allows only
# Normal Lift and no arc fitting; settings IDs let it match the effective settings Orca writes back.
ORCA_OVERRIDES = {
    "machine": {"z_hop_types": ["Normal Lift"]},
    # The plate is chosen explicitly: Orca's CLI otherwise resolves "Cool Plate", and the vendor start G-code's
    # Textured PEI nozzle offset branch depends on it. The guard refuses any other plate.
    "process": {"enable_arc_fitting": "0", "enable_prime_tower": "0", "brim_type": "outer_only",
                "curr_bed_type": "Textured PEI Plate"},
    "filament": {"filament_colour": ["#FFFFFF"], "filament_z_hop_types": ["Normal Lift"]},
}
SETTINGS_IDS = {"machine": "printer_settings_id", "process": "print_settings_id", "filament": "filament_settings_id"}

# The 2.5 identities recorded below (2.5.0-alpha, then 2.5.0-beta) come in two packagings per model: the JSON tree
# a source checkout serves, and the packed preset cache (profiles/BBL.opc) an installed
# OrcaSlicer 2.5+ serves. Orca's cache build applies its option renames and drops options
# its config definitions do not know, so the packagings flatten differently.
_IDENTITY_LABEL_25A = "OrcaSlicer 2.5.0-alpha (BBL vendor profiles 02.08.00.15)"
_IDENTITY_LABEL_25A_PACKED = "OrcaSlicer 2.5.0-alpha (BBL vendor profiles 02.08.00.15, packed cache)"
_IDENTITY_LABEL_25B = "OrcaSlicer 2.5.0-beta (BBL vendor profiles 02.08.00.16)"
_IDENTITY_LABEL_25B_PACKED = "OrcaSlicer 2.5.0-beta (BBL vendor profiles 02.08.00.16, packed cache)"

# Vendor-wide claims, documented here only; VENDOR holds the behaviour.
VENDOR_FACTS = {
    # Orca writes Bambu-dialect G-code for BBL profiles: "; FEATURE:" role tags and "; CHANGE_LAYER" layers.
    "gcode_dialect": Fact("bambu", "ORCA", True),
    # Bambu Studio, LAN and cloud jobs are .gcode.3mf. Whether an A1 runs Print Handy's package, or a plain .gcode
    # from microSD, has not been tested.
    "native_job_format": Fact(".gcode.3mf", "BAMBU", False),
    "connection_types": Fact(("LAN (MQTT status, FTPS upload, access code)", "Bambu cloud"), "BAMBU", False),
}

A1 = {
    # printer_model as Orca writes it into G-code.
    "printer_model": Fact("Bambu Lab A1", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab A1 0.4 nozzle", "process": "0.20mm Standard @BBL A1",
                           "filament": "Generic PLA @BBL A1"}, "ORCA", True),
    # The vendor profiles Print Handy was checked against: SHA-256 of each profile above with its inherits and
    # includes resolved (profiles.sha256), from an installed OrcaSlicer. Hashes only, no profile content. A
    # different OrcaSlicer or vendor profile version is refused until it has been checked and added here.
    # One checked vendor filament profile per base polymer (#264): slicing picks the one for the hardware record's
    # material and refuses a material that has none. PLA is the default profile above. Bambu's ideal materials only.
    "orca_filaments": Fact({"PLA": "Generic PLA @BBL A1", "PETG": "Generic PETG @BBL A1",
                            "TPU": "Generic TPU @BBL A1", "PVA": "Generic PVA @BBL A1"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)",
        "vendor_version": "02.01.00.19",
        "sha256": {"machine": "41a75dfaca7903b95f3a9bc81fb8d1a8e6684c449f151e74963119f959e50a72",
                   "process": "7cfbed1af4c0310589fab4857a86eb83b84bd5db71f7d34bbe2201933b9c9f3c",
                   "filament": "dc7ed678c86bc8fb2bb5e61ff8264ed211dc9e25b50badd650bdd9bccd10e81e"},
        # The other orca_filaments profiles, hashed the same way.
        "filaments": {"Generic PETG @BBL A1": "eb5c733468d29eb92ae2f0176b65880a6880e1ccc38ea91cd7de9e536fffc4d4",
                      "Generic TPU @BBL A1": "d30fd112cfbfdcc79e8946c6874354d20f41a72353e65566bf5888c1eaaea8c9",
                      "Generic PVA @BBL A1": "1974432a9cc01611daea0b34759fb3284ae1aa4d204114b3ead8ee2ded28dc80"},
    }, {
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "982b916f4f6ed74ca380cbf0d9738c65d6cbe144310c17b02c0266d702adf5f6",
                   "process": "cac0280f0ad75060d5930d96ad73d192859ee818daf312fb3e2718c914b6c14a",
                   "filament": "64846c80d1b94a90f41a1026e7103f1d372514e2f94e33fdc003b4d51f202e0b"},
        "filaments": {"Generic PETG @BBL A1": "a9dcd1a3adccd30c8b0c61fd1b83759657f92ab58119372c1c6faf6b90ed4915",
                      "Generic PVA @BBL A1": "965e1ee07a0f342ca72b54af5992c772a1a4ee0d79bb71833a3d6bb9724c1cfc",
                      "Generic TPU @BBL A1": "a945ed4f30f4e949287b37803c01a1cbcd56dcd7f383fff792d985cb6e7bd2f1"}
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "d121f19dd6a78ccff3ccc19131579ef1f7d04ac57730565cc97ae2d57b754c95",
                   "process": "70e676b3b06f6468dd0fe51cc5f8629871896121efbf280ad1bcfda01caacf33",
                   "filament": "762a61b2f6473216631fc946d7857ae0a363066f34df366bbef0aea92e53c173"},
        "filaments": {"Generic PETG @BBL A1": "657d44a63229e52b00290484828cf4a366ef0b40abf87e7e6431e35a85ea2736",
                      "Generic PVA @BBL A1": "971372674affbc3434155859c32401b10c522e92b4ff1623da5c7e73735a4f9a",
                      "Generic TPU @BBL A1": "f5c6fc63f9d62c2e214563791a9d1db1b6edfc2d2b03cae3c9d3eece026fa99a"}
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "982b916f4f6ed74ca380cbf0d9738c65d6cbe144310c17b02c0266d702adf5f6",
                   "process": "cac0280f0ad75060d5930d96ad73d192859ee818daf312fb3e2718c914b6c14a",
                   "filament": "64846c80d1b94a90f41a1026e7103f1d372514e2f94e33fdc003b4d51f202e0b"},
        "filaments": {"Generic PETG @BBL A1": "a9dcd1a3adccd30c8b0c61fd1b83759657f92ab58119372c1c6faf6b90ed4915",
                      "Generic PVA @BBL A1": "965e1ee07a0f342ca72b54af5992c772a1a4ee0d79bb71833a3d6bb9724c1cfc",
                      "Generic TPU @BBL A1": "a945ed4f30f4e949287b37803c01a1cbcd56dcd7f383fff792d985cb6e7bd2f1"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "d121f19dd6a78ccff3ccc19131579ef1f7d04ac57730565cc97ae2d57b754c95",
                   "process": "666fd56c6462083c3901f1d9dda40ac1ef5aa549c2e3a3849bdaf2f88fcb7d96",
                   "filament": "762a61b2f6473216631fc946d7857ae0a363066f34df366bbef0aea92e53c173"},
        "filaments": {"Generic PETG @BBL A1": "657d44a63229e52b00290484828cf4a366ef0b40abf87e7e6431e35a85ea2736",
                      "Generic PVA @BBL A1": "971372674affbc3434155859c32401b10c522e92b4ff1623da5c7e73735a4f9a",
                      "Generic TPU @BBL A1": "f5c6fc63f9d62c2e214563791a9d1db1b6edfc2d2b03cae3c9d3eece026fa99a"}
    }), "ORCA", True),
    # printable_area 0x0..256x256 and printable_height 256; Bambu also publishes 256 x 256 x 256 mm.
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "ORCA", True),
    # The A1 profile empties the X1 family's corner exclusion: no printable-area exclusion.
    "bed_exclude_area": Fact((), "ORCA", True),
    # Extremes of the absolute XY moves in the vendor G-code (wiper X-48.2, cutter X267, purge lane Y-0.5/262.5),
    # plus the highest travel Z allowed above the print (z-hop and timelapse lift). Not hardware certification.
    "motion_envelope_mm": Fact((-48.2, -0.5, 267.0, 262.5), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    # The vendor profile's default plate; the start G-code lowers the nozzle 0.02 mm only for this plate type.
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    # Stock 0.4 mm stainless steel; 0.2, 0.6 and 0.8 mm hotends exist. Only 0.4 has a template here.
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(100, "BAMBU", False),
    # Bambu lists PLA, PETG, TPU and PVA as ideal; ABS, ASA, PC, PA, PET and CF/GF fills as not recommended
    # (open frame). CF/GF and glow fills need a hardened steel nozzle.
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    # AMS Lite: four spool holders feeding one hotend (A1 Combo). Not driven by Print Handy.
    "ams": Fact(("AMS Lite", 4), "BAMBU", False),
}


# P1S system profiles selected from the checked OrcaSlicer BBL index. The selected process is the
# P1S machine profile's actual 0.20 mm default (an X1C profile). The default filament was the X1C
# PLA profile under 2.4.2, which listed P1S compatibility; 2.5.0-alpha dropped the P1S from that
# profile and added a dedicated P1S one, which is the checked name below — so the recorded 2.4.2
# identity now serves folders already generated from it, and generation needs 2.5.0-alpha.
P1S = {
    "printer_model": Fact("Bambu Lab P1S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P1S 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL P1S 0.4 nozzle"}, "ORCA", True),
    # As on the A1 (#264). The PETG/TPU/PVA profiles are the Bambu-branded X1C family; each still
    # lists the P1S in the 2.5.0-alpha tree. The PLA profile is the default above.
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL P1S 0.4 nozzle", "PETG": "Bambu PETG Basic @BBL X1C",
                            "TPU": "Bambu TPU 95A @BBL X1C", "PVA": "Bambu PVA @BBL X1C"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)",
        "vendor_version": "02.01.00.19",
        "sha256": {"machine": "61c68e30dd084e185ec0cf75beab03195130dabf11702cacb168205770a4782f",
                   "process": "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14",
                   "filament": "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "dbf3b8e6a26bd2a9202b97e3f0501219c4883caf639b950757050ef6a0004d60",
                      "Bambu TPU 95A @BBL X1C": "e01de656a3c239354f989b625c0411f209f5779ed60c2190f0532cf5de0ee245",
                      "Bambu PVA @BBL X1C": "619f13393e86b02f8613c8a9c5bf7cd79793a4262068f1355bb935765702dfcf"},
    }, {
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "06b95a4f135d60354f4fcf40a58f7bc42488bac87d23a725c12d98bcd86c10e7",
                   "process": "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15",
                   "filament": "ac0e13bc450f16258e22fcc4d079d125a4a7483a66e2b30bd0cb042cb16433db"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "ef6d3405e1d22983670d782d421099dd587337307ba1c824c11aacac04479559",
                      "Bambu PVA @BBL X1C": "c24079a4ddd4973aa438963647957a4323933ace6f8f6977b4566e40c69a04cb",
                      "Bambu TPU 95A @BBL X1C": "7f87a5c5cdead8963ca7c33d7b5c3741d3d62b630d345856f3af60c507bb1eae"}
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "d680536a52477d09a04ca1c6bb61745278ae3426b4beb94a91b3370bfd6a3bb7",
                   "process": "9418fb34abdb33a94050f6c5215ac151f3d93a326728c2128d909a76269db36e",
                   "filament": "131be523c92dc278e1975f46f1767cb906081d25f9800e1f1e1cb8116a619ec8"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "dc3bc18e1f0b3e399b552541928598e4e74b0d009bbc1e8eef99a3edc2a2cd4a",
                      "Bambu PVA @BBL X1C": "2753beff2620b47767fed31ed13b817e401de3ce3fe0e407b3b2525c9228b183",
                      "Bambu TPU 95A @BBL X1C": "d9f049a97aa7579bd84825a190558995d172de6ef1c74bc6019e26dee29a1302"}
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "b8dd3d19570b254653b8c036c4cb89e8e8e72bc655aea34701c0ebbe8a42762e",
                   "process": "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15",
                   "filament": "ac0e13bc450f16258e22fcc4d079d125a4a7483a66e2b30bd0cb042cb16433db"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "ef6d3405e1d22983670d782d421099dd587337307ba1c824c11aacac04479559",
                      "Bambu PVA @BBL X1C": "c24079a4ddd4973aa438963647957a4323933ace6f8f6977b4566e40c69a04cb",
                      "Bambu TPU 95A @BBL X1C": "7f87a5c5cdead8963ca7c33d7b5c3741d3d62b630d345856f3af60c507bb1eae"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "0ef08255e1c1bbe8c859625c5711b10aff23c9d1b96950ab97982f700f75087b",
                   "process": "bbcb8f44181b2f3112a7b6055886c94b328e56c7dbd194020ad8aa13052a682c",
                   "filament": "131be523c92dc278e1975f46f1767cb906081d25f9800e1f1e1cb8116a619ec8"},
        "filaments": {"Bambu PETG Basic @BBL X1C": "dc3bc18e1f0b3e399b552541928598e4e74b0d009bbc1e8eef99a3edc2a2cd4a",
                      "Bambu PVA @BBL X1C": "2753beff2620b47767fed31ed13b817e401de3ce3fe0e407b3b2525c9228b183",
                      "Bambu TPU 95A @BBL X1C": "d9f049a97aa7579bd84825a190558995d172de6ef1c74bc6019e26dee29a1302"}
    }), "ORCA", True),
    "bed_mm": Fact((256.0, 256.0), "BAMBU", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", True),
    # Orca's conservative machine profile limits slices to Z=250 although Bambu lists a 256 mm volume.
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    # The literal vendor-profile start, end and timelapse moves include the front purge lane through Y=265.
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", True),
    "max_hotend_c": Fact(300, "BAMBU", True),
    "max_bed_c": Fact(100, "BAMBU", True),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", True),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", True),
    # P1S's standard AMS has four slots; offline jobs are single-color, and LAN print starts
    # resolve trays per filament slot from live status (#308).
    "ams": Fact(("AMS", 4), "BAMBU", True),
    "enclosed": Fact(True, "BAMBU", True),
}


# --- #127 Stage 1: the remaining single-nozzle models ---------------------------------------------
# Same derivation as A1/P1S: profile names and identities computed by running profiles.flatten +
# profiles.sha256 over the pinned OrcaSlicer releases (the pipeline reproduces the A1 and P1S
# identities above exactly). Motion envelopes are DERIVED the A1 way: extremes of the literal XY
# moves in the flattened vendor start/end/timelapse/change-filament blocks, widened to cover the
# bed. Dual-nozzle models (H2D, H2D Pro, X2D) are in the Stage 2 section below (#308): their
# machine profiles carry two nozzle_diameter entries, handled by the extruder-aware identity and
# guard checks. The H2C is not here either: it arrived with OrcaSlicer v2.5.0-alpha, so it is
# registered with the A2L in the #356 section at the end of this file.

_IDENTITY_LABEL = "OrcaSlicer 2.4.2 (BBL vendor profiles 02.01.00.19)"


def _entry(label, vendor_version, machine, process, filament):
    return {"label": label, "vendor_version": vendor_version,
            "sha256": {"machine": machine, "process": process, "filament": filament}}


def _identities(release_242, alpha_tree, alpha_packed, beta_tree, beta_packed):
    """The checked identities for a model, oldest first: OrcaSlicer 2.4.2's JSON tree, then
    OrcaSlicer 2.5.0-alpha's tree and its packed preset cache, then OrcaSlicer 2.5.0-beta in
    the same two packagings (BBL vendor profiles 02.08.00.16). The two 2.5 packagings flatten
    differently (the cache applies Orca's option renames and drops unknown options), so each
    carries its own hashes; templates generated from any of them stay valid."""
    return Fact((_entry(_IDENTITY_LABEL, "02.01.00.19", *release_242),
                 _entry(_IDENTITY_LABEL_25A, "02.08.00.15", *alpha_tree),
                 _entry(_IDENTITY_LABEL_25A_PACKED, "02.08.00.15", *alpha_packed),
                 _entry(_IDENTITY_LABEL_25B, "02.08.00.16", *beta_tree),
                 _entry(_IDENTITY_LABEL_25B_PACKED, "02.08.00.16", *beta_packed)), "ORCA", True)


A1MINI = {
    "printer_model": Fact("Bambu Lab A1 mini", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab A1 mini 0.4 nozzle", "process": "0.20mm Standard @BBL A1M",
                           "filament": "Bambu PLA Basic @BBL A1M"}, "ORCA", True),
    "orca_identity": _identities(("044aaabe61a992dd5611ac4b58d91ab3f17f71213fb07d6d61092b87d55a7d7a", "6e04aa942c3afb3f112ae98c9b418e67c2a8c1f50f655b29394b01748fe24a17", "1567b1027fe09e6adac7347b77eca107e1677aae9935b9d2e076fb034649ef2e"),
                                 ("c73aad55e349ea8762bba56744e62e10d65946d68a9d61fa1ee3b2a7de03dfc1", "72bddeffb89aae83373bbfd8848ff8fb7efe5dfdd9643b37e0dee697de816978", "ce98149758d7abdf93c99a10ca4f03388cf7ed2ddfdef56f9dd3c9a5d65a0561"),
                                 ("cc9d2ed2507c11543837b9ed722b5ddd9e96f04c7f9451849a3911db742d81aa", "1d366be667115e0a5e275f0c1a03ba433f1a86e9e6ad0d49355a1b9d61e1ff49", "b27fe07d92fcd20fee2f1ef536d8adc7116bca9907e1a73db0e4453b8ef381c2"),
                                 ("c73aad55e349ea8762bba56744e62e10d65946d68a9d61fa1ee3b2a7de03dfc1", "72bddeffb89aae83373bbfd8848ff8fb7efe5dfdd9643b37e0dee697de816978", "ce98149758d7abdf93c99a10ca4f03388cf7ed2ddfdef56f9dd3c9a5d65a0561"),
                                 ("cc9d2ed2507c11543837b9ed722b5ddd9e96f04c7f9451849a3911db742d81aa", "129039c169554327b8e73f02240bf541fc72c49a114f0710b21d63290f93de78", "b27fe07d92fcd20fee2f1ef536d8adc7116bca9907e1a73db0e4453b8ef381c2")),
    "bed_mm": Fact((180.0, 180.0), "ORCA", True),
    "max_print_height_mm": Fact(180.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((-30.0, -4.0, 187.0, 185.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(80, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    # AMS Lite, as on the A1; not driven by Print Handy.
    "ams": Fact(("AMS Lite", 4), "BAMBU", False),
    "enclosed": Fact(False, "BAMBU", False),
}

P1P = {
    "printer_model": Fact("Bambu Lab P1P", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P1P 0.4 nozzle", "process": "0.20mm Standard @BBL P1P",
                           "filament": "Bambu PLA Basic @BBL P1P"}, "ORCA", True),
    "orca_identity": _identities(("511d2d5a976e36d2e2d2009efe352668a40113a3207b1f40a3970d4e28d52706", "359b0093641490765cd659320eb8ddfcaf72904c4ee7e9b39758da932754e92f", "228c186abd2a0e25cc30dd41d19a55fc88e0fa8c89870eed51924efe4db1249e"),
                                 ("ab75923826b4ec70908c8f4f74056a8654c2140bc86b6814b0935d542359ae80", "6c68c40619a3e40ee553759ada5a5555b5a7007378cbee5d6f4c4fb26f86f307", "95023034bed1d3c9545b398d34003302ab821ab4123619026697512295e9acbb"),
                                 ("23f6946de5af6de2f8e28722ba07c088787f58c75b95bbec11a15e2ba8823199", "67013eefd251b38fa319e0fee85301f9ddafa9626a86d0d4308caa43218ce47b", "f4b9720450d21b4da0d9a8c32eae059ba2d95f0d654a27decaeaffeb7258721c"),
                                 ("ab75923826b4ec70908c8f4f74056a8654c2140bc86b6814b0935d542359ae80", "6c68c40619a3e40ee553759ada5a5555b5a7007378cbee5d6f4c4fb26f86f307", "95023034bed1d3c9545b398d34003302ab821ab4123619026697512295e9acbb"),
                                 ("23f6946de5af6de2f8e28722ba07c088787f58c75b95bbec11a15e2ba8823199", "1a0dd1a6c4ef9fce09680f25552b9ac0adb82468b9e9c01dbabe9fab26a1f0f8", "f4b9720450d21b4da0d9a8c32eae059ba2d95f0d654a27decaeaffeb7258721c")),
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", False),
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(100, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    "ams": Fact(("AMS", 4), "BAMBU", False),
    # Open frame as shipped (enclosure panels are an optional upgrade).
    "enclosed": Fact(False, "BAMBU", False),
}

# The X1 family shares the P1 platform geometry (256 mm bed, 250 mm Orca height, the same front-left
# exclusion and purge-lane envelope) and the X1C process/filament profiles; the X1 and X1E machine
# profiles differ, so each model keeps its own identity.
_X1_FAMILY = {
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(256.0, "BAMBU", False),
    "orca_profile_height_mm": Fact(250.0, "ORCA", True),
    "bed_exclude_area": Fact(((0.0, 0.0), (18.0, 0.0), (18.0, 28.0), (0.0, 28.0)), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -3.0, 256.0, 265.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(110, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}

X1 = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1"}, "ORCA", True),
    "orca_identity": _identities(("8ab80fbf8f84e2962e5dd6f9b2c41955152ccfb27ad1bd592f860d7430720346", "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14", "df9074c5777e7a2cf0f3b73c54ebd8c9cf8dc2782f04b4698b10974039f5db19"),
                                 ("45868aaf5548955e4eb3d000aeb2df28de4f275dabf371871cd3cd21c2c8340b", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "818dae7922d64e9364a8d0d2f051870a2c3e35ed97eb603f751a1b6d2620da37"),
                                 ("133c770b30ed9ce86aaac342c6f20bf8417fd16922f5f9385d4790526728d615", "9418fb34abdb33a94050f6c5215ac151f3d93a326728c2128d909a76269db36e", "94a9f2dcf31d194ee9b5f23dc3ff4c7c1749b56dcf18f17601f4a0936b620f40"),
                                 ("6e702340078c0540b267af5111bf9b6a483902463f7f05b824b82e2465092d9d", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "818dae7922d64e9364a8d0d2f051870a2c3e35ed97eb603f751a1b6d2620da37"),
                                 ("9e27059f9cda087bfcb622edda0fbb6b19efd1a2fccf6b41a72f474f35e7857c", "bbcb8f44181b2f3112a7b6055886c94b328e56c7dbd194020ad8aa13052a682c", "94a9f2dcf31d194ee9b5f23dc3ff4c7c1749b56dcf18f17601f4a0936b620f40")),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
}

X1C = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1 Carbon", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1 Carbon 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1C"}, "ORCA", True),
    "orca_identity": _identities(("c3ba8d3d84ed20abe5783cf5a54798ae35ef9cd06f84da3a60272ce08cb38396", "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14", "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"),
                                 ("1bc634e74d3f6279fd128bfaf00af4f2e6781b53e7b656cb4af92360843bd8ad", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "a1f4774338164bcb7aad95caf84fdf90ca6413fe2f58a8f61aff92e46f5d08d4"),
                                 ("ed41c845b3855281ce0d0b2b335826156210ab7c6c05a2036627813e08c3de52", "9418fb34abdb33a94050f6c5215ac151f3d93a326728c2128d909a76269db36e", "e0f9846909ae2cb55c1e832e788226ed82077c7befc8dbf387cfb9bf1d42ddd9"),
                                 ("43802a2dc01b3ca72eb53ed41fbf4f5f3a32d0dbf3c4df473fb987eff5b73a8a", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "a1f4774338164bcb7aad95caf84fdf90ca6413fe2f58a8f61aff92e46f5d08d4"),
                                 ("96293ad3c1babab6b11d5e6b7942159ba17477274eb54eca67df87c952c20e65", "bbcb8f44181b2f3112a7b6055886c94b328e56c7dbd194020ad8aa13052a682c", "e0f9846909ae2cb55c1e832e788226ed82077c7befc8dbf387cfb9bf1d42ddd9")),
    # The X1 Carbon ships with a hardened steel hotend (Carbon = CF-ready).
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
}

X1E = {
    **_X1_FAMILY,
    "printer_model": Fact("Bambu Lab X1E", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab X1E 0.4 nozzle", "process": "0.20mm Standard @BBL X1C",
                           "filament": "Bambu PLA Basic @BBL X1C"}, "ORCA", True),
    "orca_identity": _identities(("75d3b7457906753319d74f2efc4d4d020154da2373ad73ca8a56a3fa506ef980", "bc4e02dad296e7584bf12143276bbbe2debf73f390f7e6dd2ec2aeac9e3d3c14", "ff10a3d1ad9e7ed43f5dec36996248b6abb934eed6692c480732670848e0a403"),
                                 ("36be52316970e3cc7bed84207d49f73c363027ea48255a7b14e6e3b996f2d3a0", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "a1f4774338164bcb7aad95caf84fdf90ca6413fe2f58a8f61aff92e46f5d08d4"),
                                 ("e7ca63c57c1d9419eecf2d05e51f57736ef71f81f0c013cc1869682451bb8827", "9418fb34abdb33a94050f6c5215ac151f3d93a326728c2128d909a76269db36e", "e0f9846909ae2cb55c1e832e788226ed82077c7befc8dbf387cfb9bf1d42ddd9"),
                                 ("36be52316970e3cc7bed84207d49f73c363027ea48255a7b14e6e3b996f2d3a0", "e84477df8e71b87ee3a0161ebf96c5b7587eec3d9e8fdc0d080d18058b0bdc15", "a1f4774338164bcb7aad95caf84fdf90ca6413fe2f58a8f61aff92e46f5d08d4"),
                                 ("e7ca63c57c1d9419eecf2d05e51f57736ef71f81f0c013cc1869682451bb8827", "bbcb8f44181b2f3112a7b6055886c94b328e56c7dbd194020ad8aa13052a682c", "e0f9846909ae2cb55c1e832e788226ed82077c7befc8dbf387cfb9bf1d42ddd9")),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
}

P2S = {
    "printer_model": Fact("Bambu Lab P2S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab P2S 0.4 nozzle", "process": "0.20mm Standard @BBL P2S",
                           "filament": "Bambu PLA Basic @BBL P2S"}, "ORCA", True),
    "orca_identity": _identities(("85e88eba88cb41d79620b99e7483bed8c6e94106089611f7ba17f2b998b3615e", "363db4359eb29d59313749d0d56b5985041ecd56cb5ed6f75626223b1c4199e9", "9dae1470940aaa6dfdd354644342722b675c8ae8c6805c3b109def21aba50d7c"),
                                 ("33852ae821a14d14d9822e0f31d2a1d2696c069dba17ac15618767d0d5ac80f4", "a6ea5ba26c140c5afe4595221bedb36fb3f4baa0a8cf3d3ad6d16319153a5e37", "54cfdfcae02170bf4aefdf7d279eb27244abf47fc8a372e1fe3c9ec67e1085a5"),
                                 ("4683d0289ae67c03a1de311fcf7c974a118d07d2b27a1b8a7666f717da7c511f", "3a095f9ce05c94da8da1f57049fa69a7ebf8a199ec8f7832ffadc8343463a845", "43d0ca816a0bd0627413806b85f43a214299b0f43737dd3cbc02db7cd255f565"),
                                 ("b890196d0560e5a6d56f18f78d917d5b65ecd038f16ac2480ef9ed078b023b8c", "a6ea5ba26c140c5afe4595221bedb36fb3f4baa0a8cf3d3ad6d16319153a5e37", "54cfdfcae02170bf4aefdf7d279eb27244abf47fc8a372e1fe3c9ec67e1085a5"),
                                 ("41a412d55f55a009acd38d8e8f45609a01b943e453915592d6c382ab78fbea3c", "b35334c0ad9dfee968e595cd61582733474b401112610e7ff69d8fa203614d21", "43d0ca816a0bd0627413806b85f43a214299b0f43737dd3cbc02db7cd255f565")),
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    # Unlike the P1/X1 platform, the P2S Orca profile uses the full listed 256 mm height.
    "max_print_height_mm": Fact(256.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 256.0, 256.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(110, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    # AMS 2 Pro on the combo unit; Print Handy drives a single path only.
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}

H2S = {
    "printer_model": Fact("Bambu Lab H2S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab H2S 0.4 nozzle", "process": "0.20mm Standard @BBL H2S",
                           "filament": "Bambu PLA Basic @BBL H2S"}, "ORCA", True),
    "orca_identity": _identities(("5956872ea1d691853fd08158134c67da1291fe7b89252e32be281e09a3541f23", "c7b55397a79d3bf03c2389ce868b50c49943f08b9e2f39e97e6e5f3bdf2db2c0", "781806ef94f4bdbcb30525df9d265e80a20e0858d92fe7511e20ac10dcb2ec10"),
                                 ("c7af5fdf1306bd6eea75bd3528437903b81450f1295045a869f892ca470c5356", "3bbb1ffeaaa5ce4076beaa5e6e07fcafd0a5d38f59b57cc726b2d09f7339981b", "12904f3897740660807c3a9cdd06f8e36a245e2540edc7a03a52bddb711679cb"),
                                 ("e0a18de32010e80e9a706ebaa0f8f0957b1bf4903b6a82056a833d6bda55c9f0", "9e9f8176d4ea156dd66542941f4b0106fb8d9c0ceb156c1732bc6cd9651fd529", "c4846ddc03f8c88c760cb99dfb4fdca55f6fdb0beb016880547bafb68c738e75"),
                                 ("81dc2e6845cab7f492b4cfd843c530d99dd660c47a6d0234eabe9aa84db28692", "3bbb1ffeaaa5ce4076beaa5e6e07fcafd0a5d38f59b57cc726b2d09f7339981b", "12904f3897740660807c3a9cdd06f8e36a245e2540edc7a03a52bddb711679cb"),
                                 ("ee9596d0b6702dfeefb413694f30d06dadced2c57c212ee00217e43535853047", "4121721dbb5bf98f1fd1de60661e94acedbde949fe9d5df29619c07413e05722", "c4846ddc03f8c88c760cb99dfb4fdca55f6fdb0beb016880547bafb68c738e75")),
    # Single-nozzle H2 variant: one 0.4 nozzle in the profile (the dual-nozzle H2D/H2C are Stage 2).
    "bed_mm": Fact((340.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(340.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 340.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(350, "BAMBU", False),
    "max_bed_c": Fact(120, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}

# --- #127 Stage 2 / #308: the dual-nozzle models ---------------------------------------------------
# Same derivation as Stage 1: identities computed by running profiles.flatten + profiles.sha256
# over the pinned OrcaSlicer releases (the pipeline reproduces the A1 and P1S identities
# exactly). Motion envelopes are DERIVED the A1 way from the flattened vendor blocks' literal XY
# extremes, widened to cover the bed. "extruders" is the profile's nozzle_diameter entry count and
# drives the extruder-aware identity/guard checks and the AMS mapping's nozzle filter.
# The H2D build width depends on the tool (305 mm single-tool, 350 mm dual, per Bambu's specs);
# bed_mm records the full profile volume — a multi-tool job is the operator's to slice inside the
# per-tool limits their mapping uses.

H2D = {
    "printer_model": Fact("Bambu Lab H2D", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab H2D 0.4 nozzle", "process": "0.20mm Standard @BBL H2D",
                           "filament": "Bambu PLA Basic @BBL H2D"}, "ORCA", True),
    # One checked vendor filament profile per base polymer (#264), so a multi-material file's
    # slots can each name a checked profile. PLA is the default profile above.
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL H2D", "PETG": "Generic PETG @BBL H2D",
                            "TPU": "Generic TPU @BBL H2D", "PVA": "Generic PVA @BBL H2D"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": _IDENTITY_LABEL, "vendor_version": "02.01.00.19",
        "sha256": {"machine": "013c853bac2942a86e1ef32e073ecc8381345a0757dbd676789f1d1b3cd5c8b8",
                   "process": "1208765d895439e9f66e3e0a913881a6b4e7d28b8edce62fb1f8ce002f6503ee",
                   "filament": "cd3044c9e637453702e81254b351a702ef116dd2b2ffcf9b1b82b0bf8f89137d"},
        "filaments": {"Generic PETG @BBL H2D": "93e86898330e805436f9f4f03d5eb89cca141fea43754333bd3def9327c197aa",
                      "Generic TPU @BBL H2D": "05a1e6055eb3685900af5f86766d2fe922c06febd665db68814bcd86af3670b9",
                      "Generic PVA @BBL H2D": "fb40c97e52ec35c86d838295f2900ba61b8293236b23d6ea346e15848c09545f"},
    }, {
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "c815d18b3c39eac25393b4624d801d834d7186e5ead8a8bbea45f0fcea294f6e",
                   "process": "c52c60748dbe16f6b79b8890f5e4e3bbea5bb19bec2532312fc2720aea04f5bc",
                   "filament": "8e503828cd68583974be37859fa6f19b21365b1a84283917f21f0d6e146cfdec"},
        "filaments": {"Generic PETG @BBL H2D": "e1362ec947f99b780b155780100635e9a36e80edcc7bcfc44f047e34a871690c",
                      "Generic PVA @BBL H2D": "8a4bbaf623c69b1b075d873498c1066a24f0ea2ce00a2edd4d65ad2965b5fc2f",
                      "Generic TPU @BBL H2D": "cda2f178be40f6c82f2d3505b945e4dec946a62bb7daa9650b3dcd3eb6ade5af"}
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "d17ae7948c3def8f1e3e3e7af9e3712818e6453d73a6389ade51c40385f4441c",
                   "process": "1ede83c7ac6c8b443042a0c00d7d792d40053e157d0579ac269d34ef7b94190e",
                   "filament": "6925b9fb8f90ea0791de5f09904294ce4219a46b82c4d33732a0b689c8fc735f"},
        "filaments": {"Generic PETG @BBL H2D": "3810c0c012b9c000e0d317cc6f56eff26f610cea3be1476f0bfc81c99aa6895c",
                      "Generic PVA @BBL H2D": "2efcb2c4d156c392a3f3e430c03b9be71fc7ee8f90ed4a25ddb1c36f361dcfa8",
                      "Generic TPU @BBL H2D": "416b78d992e00990ca624848a893c6769cd1a0785733dc1d08709348b76a5b2d"}
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "c8ac589d8a8ddf3f3c69cc989aca83b53dded6a8e32d8ab5ff96f1bfc7d86cf9",
                   "process": "c52c60748dbe16f6b79b8890f5e4e3bbea5bb19bec2532312fc2720aea04f5bc",
                   "filament": "8e503828cd68583974be37859fa6f19b21365b1a84283917f21f0d6e146cfdec"},
        "filaments": {"Generic PETG @BBL H2D": "e1362ec947f99b780b155780100635e9a36e80edcc7bcfc44f047e34a871690c",
                      "Generic PVA @BBL H2D": "8a4bbaf623c69b1b075d873498c1066a24f0ea2ce00a2edd4d65ad2965b5fc2f",
                      "Generic TPU @BBL H2D": "cda2f178be40f6c82f2d3505b945e4dec946a62bb7daa9650b3dcd3eb6ade5af"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "5ca62ed94bde8663417d32a6702580950a619e55c92222855fddc53d5008f5a3",
                   "process": "8c05674e5d9cbeab08dc9c62eb7d61ba9d5a9fe04b0f76b2e93729dabc89fd77",
                   "filament": "6925b9fb8f90ea0791de5f09904294ce4219a46b82c4d33732a0b689c8fc735f"},
        "filaments": {"Generic PETG @BBL H2D": "3810c0c012b9c000e0d317cc6f56eff26f610cea3be1476f0bfc81c99aa6895c",
                      "Generic PVA @BBL H2D": "2efcb2c4d156c392a3f3e430c03b9be71fc7ee8f90ed4a25ddb1c36f361dcfa8",
                      "Generic TPU @BBL H2D": "416b78d992e00990ca624848a893c6769cd1a0785733dc1d08709348b76a5b2d"}
    }), "ORCA", True),
    "bed_mm": Fact((350.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(325.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 350.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(350, "BAMBU", False),
    "max_bed_c": Fact(120, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
    "extruders": Fact(2, "ORCA", True),
}

H2DPRO = {
    "printer_model": Fact("Bambu Lab H2D Pro", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab H2D Pro 0.4 nozzle", "process": "0.20mm Standard @BBL H2DP",
                           "filament": "Bambu PLA Basic @BBL H2DP"}, "ORCA", True),
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL H2DP", "PETG": "Generic PETG @BBL H2DP",
                            "TPU": "Generic TPU @BBL H2DP", "PVA": "Generic PVA @BBL H2DP"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": _IDENTITY_LABEL, "vendor_version": "02.01.00.19",
        "sha256": {"machine": "7284d424b3525b5e6b91f8f2f606e22b9690be76528c12498f07ee0e90a6b07f",
                   "process": "bbf17643942051e8dff92ac2fa5d7fea61848b4cf212b550984bc5d25d7f4135",
                   "filament": "2f19516c9f6728bec42cc58069cd97b6fa03ac1ab900381c5d919523d745ba29"},
        "filaments": {"Generic PETG @BBL H2DP": "3185372d4c336bd44769c5145f18e8b3eb400747db794e264505b656572f4dc9",
                      "Generic TPU @BBL H2DP": "9cd55060df6dc5a017705675cfec73eec3a97570d799c60bee1b989bf4815a51",
                      "Generic PVA @BBL H2DP": "44323c62943ea2b48a499f47e19f0981253a31354bd3f77eee5907ff43dcb7ae"},
    }, {
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "07ed55c78a15f342e6df14ddbf0897b100a5e39144aa358673ee4fda67fed09c",
                   "process": "dcf0524db372846825f026042bac3ba95ec7e3b58cd295fa014f4b8be130bd40",
                   "filament": "7d259d70d1b5174885b66dee001c18f7c0afd9c7009f73efe7f8a6af763f7900"},
        "filaments": {"Generic PETG @BBL H2DP": "b35317e91f0cc4c219e22b0f2ce23f71e5c2752d75f5fcb594977d2d4032a343",
                      "Generic PVA @BBL H2DP": "b7532db2add00fb6468b4e90dcb8a8d703d696bcaed5041edc02f9299340da7c",
                      "Generic TPU @BBL H2DP": "9e79b641a6771207734cb1f0f2036354d5c4801cdcc42d393a00ca473a24e243"}
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "ed06e1096dc40918e83e93e9d04d27a5930b84152d2bd47f3a6b3475dfb544da",
                   "process": "3ac1d68e83a0fdb6a1ea84887b5e9e5b44025297506ef3056bac41cf48c57ccd",
                   "filament": "6643cc9eb4ec716d806ec172f2f3ce7df4ebe0e4e2e3f80defec678a726613dc"},
        "filaments": {"Generic PETG @BBL H2DP": "962662a49bbb46f1fbf273700b03165342b282591ae507152a19f303ba098c9b",
                      "Generic PVA @BBL H2DP": "8991d68ce6e07be856de7db3e700c49421b03947b4aeb5a9a5ed13e16fa387c8",
                      "Generic TPU @BBL H2DP": "37a3eab971f2577ff3a441d2d65321fc43f88b0ee046f859210504323004472a"}
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "263417335a3dfb5433ac23bd5f490b303117e189a5019024969e034ace8ee53e",
                   "process": "dcf0524db372846825f026042bac3ba95ec7e3b58cd295fa014f4b8be130bd40",
                   "filament": "7d259d70d1b5174885b66dee001c18f7c0afd9c7009f73efe7f8a6af763f7900"},
        "filaments": {"Generic PETG @BBL H2DP": "b35317e91f0cc4c219e22b0f2ce23f71e5c2752d75f5fcb594977d2d4032a343",
                      "Generic PVA @BBL H2DP": "b7532db2add00fb6468b4e90dcb8a8d703d696bcaed5041edc02f9299340da7c",
                      "Generic TPU @BBL H2DP": "9e79b641a6771207734cb1f0f2036354d5c4801cdcc42d393a00ca473a24e243"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "41595c7c46e8c4210a6c3e5af8011fc953027240ae1f6d333f2d95c5fd3b513f",
                   "process": "6c89ca8aa1a2a1fd4e21a2bb2d4b4845866764e3b6a4aa44d6efd400cdb4fb56",
                   "filament": "6643cc9eb4ec716d806ec172f2f3ce7df4ebe0e4e2e3f80defec678a726613dc"},
        "filaments": {"Generic PETG @BBL H2DP": "962662a49bbb46f1fbf273700b03165342b282591ae507152a19f303ba098c9b",
                      "Generic PVA @BBL H2DP": "8991d68ce6e07be856de7db3e700c49421b03947b4aeb5a9a5ed13e16fa387c8",
                      "Generic TPU @BBL H2DP": "37a3eab971f2577ff3a441d2d65321fc43f88b0ee046f859210504323004472a"}
    }), "ORCA", True),
    "bed_mm": Fact((350.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(325.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 350.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(350, "BAMBU", False),
    "max_bed_c": Fact(120, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
    "extruders": Fact(2, "ORCA", True),
}

X2D = {
    "printer_model": Fact("Bambu Lab X2D", "ORCA", True),
    # The machine's own default filament is the 0.4-nozzle-suffixed profile; the un-suffixed
    # @BBL X2D PLA profile lists only 0.6/0.8 nozzle machines and is not a checked default.
    "orca_profiles": Fact({"machine": "Bambu Lab X2D 0.4 nozzle", "process": "0.20mm Standard @BBL X2D",
                           "filament": "Bambu PLA Basic @BBL X2D 0.4 nozzle"}, "ORCA", True),
    # The X2D's Generic profiles are nozzle-suffixed like its default: the un-suffixed
    # @BBL X2D Generics list only the 0.6/0.8 nozzle machines in compatible_printers,
    # so the checked set is the 0.4 nozzle variants (verified against the pinned tree).
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL X2D 0.4 nozzle",
                            "PETG": "Generic PETG @BBL X2D 0.4 nozzle",
                            "TPU": "Generic TPU @BBL X2D 0.4 nozzle",
                            "PVA": "Generic PVA @BBL X2D 0.4 nozzle"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": _IDENTITY_LABEL, "vendor_version": "02.01.00.19",
        "sha256": {"machine": "72e2f78a837f4e85ef370125b5e82457cae0cd37a511795fd4ed9d321dfcf829",
                   "process": "351fc2f4f031bb0209260c640acbfba000d55e20ee12983874aad1215c449d54",
                   "filament": "7df0376d688adcb93649261bad60dc357a11cea08126bfb7b24e5eedca946c2d"},
        "filaments": {"Generic PETG @BBL X2D 0.4 nozzle": "81cb8d9fc131ed3961aa1c6b8e7a432e5dbad1bbc0d93ac1aecfc785b679757c",
                      "Generic TPU @BBL X2D 0.4 nozzle": "2c2a1b9dce7cc7c17961b9d84d2c11c20e9ca89890b0f9811364ffa0e340c0af",
                      "Generic PVA @BBL X2D 0.4 nozzle": "c3bad6320ef116a054f1eaf6832567589c162992a2027c45e5c09ce26f655c4a"},
    }, {
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "980cdbe38f293f29c74624e42a6de05b055f09c965e0302fdfc6b39665a6a07f",
                   "process": "55401b1df522a3d2c3149d1d27f9b8e3d2424d89b2a816fbfe61254bea22458a",
                   "filament": "93616a8fdc77a5be2420bcefcf68b72bb2eaa376b96bac4cbff6e012174d17f0"},
        "filaments": {"Generic PETG @BBL X2D 0.4 nozzle": "591413dd707188052ea6514c6b0af88eb81bda427c11152a777910310d282aa1",
                      "Generic PVA @BBL X2D 0.4 nozzle": "804b84c3f164125ed1fb9ac1649cce390d895230b7fb6766e7611435db324287",
                      "Generic TPU @BBL X2D 0.4 nozzle": "2cc0b27c655d202a2fb9e3ab4df33c5abe90b192af6ebd90bacec9e437462679"}
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "ea79b249feb45cb836ff2135bc1a0312a3a006497d0b8fb8d3f8e2d3bddedebb",
                   "process": "9d93c29ddc772ebc03def8a2946bbd8858b17a86b09cc382e461d23e601a43de",
                   "filament": "dce24e095da8d2ef716f3a2636ea0f26e1db74f3226e0d26c62da68d510fa678"},
        "filaments": {"Generic PETG @BBL X2D 0.4 nozzle": "15f5ed29144c9a64fdf495e4caa24dee0cc1d00c3bcee1d0d3018e892049aac0",
                      "Generic PVA @BBL X2D 0.4 nozzle": "0d2da5d848c9ca9449766f24276fd872fabbd319230d186823781e69f624695c",
                      "Generic TPU @BBL X2D 0.4 nozzle": "a6759bfe22fcf919dbbca4b10bc316eb015edc6b11a09853274ca9edc939e04b"}
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "77cb6dfa4d263a88ab60ca363cc57abff34bc18ddfb5ab93ab792cc83115bd9d",
                   "process": "55401b1df522a3d2c3149d1d27f9b8e3d2424d89b2a816fbfe61254bea22458a",
                   "filament": "93616a8fdc77a5be2420bcefcf68b72bb2eaa376b96bac4cbff6e012174d17f0"},
        "filaments": {"Generic PETG @BBL X2D 0.4 nozzle": "591413dd707188052ea6514c6b0af88eb81bda427c11152a777910310d282aa1",
                      "Generic PVA @BBL X2D 0.4 nozzle": "804b84c3f164125ed1fb9ac1649cce390d895230b7fb6766e7611435db324287",
                      "Generic TPU @BBL X2D 0.4 nozzle": "2cc0b27c655d202a2fb9e3ab4df33c5abe90b192af6ebd90bacec9e437462679"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "326f30c021c4a87fcfc44b32f12113a4af7e764e7e15d534d19c9ae8c0796b31",
                   "process": "76b6dc874d078c5499910d897861a874a1db7495a1b5da0b88bd193cc5e517f8",
                   "filament": "dce24e095da8d2ef716f3a2636ea0f26e1db74f3226e0d26c62da68d510fa678"},
        "filaments": {"Generic PETG @BBL X2D 0.4 nozzle": "15f5ed29144c9a64fdf495e4caa24dee0cc1d00c3bcee1d0d3018e892049aac0",
                      "Generic PVA @BBL X2D 0.4 nozzle": "0d2da5d848c9ca9449766f24276fd872fabbd319230d186823781e69f624695c",
                      "Generic TPU @BBL X2D 0.4 nozzle": "a6759bfe22fcf919dbbca4b10bc316eb015edc6b11a09853274ca9edc939e04b"}
    }), "ORCA", True),
    "bed_mm": Fact((256.0, 256.0), "ORCA", True),
    "max_print_height_mm": Fact(261.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 256.0, 256.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(320, "BAMBU", False),
    "max_bed_c": Fact(110, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
    "extruders": Fact(2, "ORCA", True),
}


# --- #356: the H2C and A2L ---------------------------------------------------------------
# These two models exist only in the OrcaSlicer 2.5 line's BBL index (02.08.00.15 in the
# alpha, 02.08.00.16 in the beta) — there is no 2.4.2 identity for them. Same derivation as
# the other models: identities computed by running profiles.flatten + profiles.sha256 over
# each release's tree and its packed preset cache.
# Motion envelopes are DERIVED the A1 way from the flattened vendor blocks' literal XY
# extremes, widened to cover the bed (the method reproduces the recorded A1/H2D/H2S envelopes
# exactly). Temperatures, nozzle sizes, materials, AMS and enclosure are Bambu's published
# specs (store/blog pages, read from search-result extracts 2026-10-26; unverified against
# bambulab.com directly, like the other models' BAMBU facts).

H2C = {
    "printer_model": Fact("Bambu Lab H2C", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab H2C 0.4 nozzle", "process": "0.20mm Standard @BBL H2C",
                           "filament": "Bambu PLA Basic @BBL H2C"}, "ORCA", True),
    # One checked vendor filament profile per base polymer (#264), as on the H2D. PLA is the
    # default profile above. Upstream's unsuffixed Generic H2C profiles cover the 0.6/0.8
    # nozzles only; the 0.4 nozzle has its own variants (the X2D pattern), which are these.
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL H2C", "PETG": "Generic PETG @BBL H2C 0.4 nozzle",
                            "TPU": "Generic TPU @BBL H2C 0.4 nozzle", "PVA": "Generic PVA @BBL H2C 0.4 nozzle"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "405faf64d1ff9406a378cc8c4b0740d7ca0d798a1f09b940a754b1ab6c5db264",
                   "process": "947c68de98aed246c4a892b606a5d8e40ffa7d03626c0a9745b95ce8ea19b3b5",
                   "filament": "d53f3a5be35ae45ae26403cdc042f97b100d4fc9f72d4d16ee086f649c4ea026"},
        "filaments": {"Generic PETG @BBL H2C 0.4 nozzle": "36dbad1ec60a775cf589fc6432527cdfee05b6886cdbe6eea624a0fb21ab479c",
                      "Generic TPU @BBL H2C 0.4 nozzle": "7fe7c81c22d3f44eba82a8cdf9e0b78b43d7a168e781dd6c4b0fda5c1c23e42a",
                      "Generic PVA @BBL H2C 0.4 nozzle": "96da27366756213ec43d9aca8f19d5658ec0206c2ea8ba52a91c91e6a5f4478b"},
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "100bca9cbfa3cdbb7b076f607e1c511410c71b29de53265f4ab66b953fc7df9d",
                   "process": "5d9588c952d23fab29c2bd4dbdf0a488de1abd30670facffaf22866d962360d0",
                   "filament": "6c035e64cb3044adc392d0d982b363412ac5596bfe7529cdcb537f6892c33d22"},
        "filaments": {"Generic PETG @BBL H2C 0.4 nozzle": "62824703c5abe70a5d45632c72eadb1d4c936b40f4370e8e379649ca758cdc09",
                      "Generic PVA @BBL H2C 0.4 nozzle": "3a900f27d846edf1bdd5655b12133dd44d181543a29e28fed62a7157d880f127",
                      "Generic TPU @BBL H2C 0.4 nozzle": "d8af6ae7686f2c851cc8e1567aa2025d5294a26bbef811421658edea9edc64eb"},
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "405faf64d1ff9406a378cc8c4b0740d7ca0d798a1f09b940a754b1ab6c5db264",
                   "process": "947c68de98aed246c4a892b606a5d8e40ffa7d03626c0a9745b95ce8ea19b3b5",
                   "filament": "d53f3a5be35ae45ae26403cdc042f97b100d4fc9f72d4d16ee086f649c4ea026"},
        "filaments": {"Generic PETG @BBL H2C 0.4 nozzle": "36dbad1ec60a775cf589fc6432527cdfee05b6886cdbe6eea624a0fb21ab479c",
                      "Generic PVA @BBL H2C 0.4 nozzle": "96da27366756213ec43d9aca8f19d5658ec0206c2ea8ba52a91c91e6a5f4478b",
                      "Generic TPU @BBL H2C 0.4 nozzle": "7fe7c81c22d3f44eba82a8cdf9e0b78b43d7a168e781dd6c4b0fda5c1c23e42a"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "100bca9cbfa3cdbb7b076f607e1c511410c71b29de53265f4ab66b953fc7df9d",
                   "process": "06640afea1e0e1fb44f673b649796546b2c82012b216ddb8f6341d8ff937fcb2",
                   "filament": "6c035e64cb3044adc392d0d982b363412ac5596bfe7529cdcb537f6892c33d22"},
        "filaments": {"Generic PETG @BBL H2C 0.4 nozzle": "62824703c5abe70a5d45632c72eadb1d4c936b40f4370e8e379649ca758cdc09",
                      "Generic PVA @BBL H2C 0.4 nozzle": "3a900f27d846edf1bdd5655b12133dd44d181543a29e28fed62a7157d880f127",
                      "Generic TPU @BBL H2C 0.4 nozzle": "d8af6ae7686f2c851cc8e1567aa2025d5294a26bbef811421658edea9edc64eb"}
    }), "ORCA", True),
    "bed_mm": Fact((330.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(325.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -16.0, 330.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "hardened_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(350, "BAMBU", False),
    "max_bed_c": Fact(120, "BAMBU", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    # AMS 2 Pro on the combo unit; Print Handy drives a single path only.
    "ams": Fact(("AMS 2 Pro", 4), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
    # The H2C is a dual-nozzle model: its machine profile carries two nozzle_diameter entries.
    "extruders": Fact(2, "ORCA", True),
}

A2L = {
    "printer_model": Fact("Bambu Lab A2L", "ORCA", True),
    "orca_profiles": Fact({"machine": "Bambu Lab A2L 0.4 nozzle", "process": "0.20mm Standard @BBL A2L",
                           "filament": "Bambu PLA Basic @BBL A2L 0.4 nozzle"}, "ORCA", True),
    # One checked vendor filament profile per base polymer (#264), as on the A1. PLA is the
    # default profile above.
    "orca_filaments": Fact({"PLA": "Bambu PLA Basic @BBL A2L 0.4 nozzle", "PETG": "Generic PETG @BBL A2L",
                            "TPU": "Generic TPU @BBL A2L", "PVA": "Generic PVA @BBL A2L"}, "ORCA", True),
    "orca_identity": Fact(({
        "label": _IDENTITY_LABEL_25A,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "e222c487abe7f8d5adc007d974dd1c7c850c24c8837d811c05ee19efe81cd892",
                   "process": "ba8c077fca0c56fdcd6e04d2483522b25675ffdae60143d7f582bd8255e6758d",
                   "filament": "363699a477dc236c5b38db80327dbe970aadc1dfa0a4f6e97d16f6b451af7890"},
        "filaments": {"Generic PETG @BBL A2L": "476a7ed35e7fb2e59a9268cb73d67a3bb104add0706fc51d02ba0fd52ae4fff2",
                      "Generic TPU @BBL A2L": "246c29130ab6b8dfbfffd9d7fa843ce55554cbaa05fb2e52665c35dc612201d9",
                      "Generic PVA @BBL A2L": "b62bba232f0cd9679033bf8750ee92e5e1d7abcf12d13bfeb0715efcf46b4f05"},
    }, {
        "label": _IDENTITY_LABEL_25A_PACKED,
        "vendor_version": "02.08.00.15",
        "sha256": {"machine": "41625a52ffdbae16666c7cc768857267dbf4f1f0922cff991f107baf01def1af",
                   "process": "d80799664eb252664bb49a27562d934524c3123dffbaf4de4e6dd56c9c990d46",
                   "filament": "40051ec9d3d2e30ec02a15944d5c7f0f07383ce444f4d05591e009a0d17e7bf8"},
        "filaments": {"Generic PETG @BBL A2L": "ae3cc62a13b141407efb39a4b3bf7b2ec218190bf392269257923db853b39c03",
                      "Generic PVA @BBL A2L": "321ba79c4e66d26a3e94d8d3c8418a713c1d508f1928319a8b8c9b25e5e5657a",
                      "Generic TPU @BBL A2L": "ea0187f0b785b7555538d79c1db05f137ef485bcdc52308e48cb4fbf9db9c891"},
    },
    {
        "label": _IDENTITY_LABEL_25B,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "e222c487abe7f8d5adc007d974dd1c7c850c24c8837d811c05ee19efe81cd892",
                   "process": "ba8c077fca0c56fdcd6e04d2483522b25675ffdae60143d7f582bd8255e6758d",
                   "filament": "363699a477dc236c5b38db80327dbe970aadc1dfa0a4f6e97d16f6b451af7890"},
        "filaments": {"Generic PETG @BBL A2L": "476a7ed35e7fb2e59a9268cb73d67a3bb104add0706fc51d02ba0fd52ae4fff2",
                      "Generic PVA @BBL A2L": "b62bba232f0cd9679033bf8750ee92e5e1d7abcf12d13bfeb0715efcf46b4f05",
                      "Generic TPU @BBL A2L": "246c29130ab6b8dfbfffd9d7fa843ce55554cbaa05fb2e52665c35dc612201d9"}
    },
    {
        "label": _IDENTITY_LABEL_25B_PACKED,
        "vendor_version": "02.08.00.16",
        "sha256": {"machine": "41625a52ffdbae16666c7cc768857267dbf4f1f0922cff991f107baf01def1af",
                   "process": "01ed9f8304185eb9765b336152e3e979f4f0aaa60e6530bc165a7ff97ce3f0f1",
                   "filament": "40051ec9d3d2e30ec02a15944d5c7f0f07383ce444f4d05591e009a0d17e7bf8"},
        "filaments": {"Generic PETG @BBL A2L": "ae3cc62a13b141407efb39a4b3bf7b2ec218190bf392269257923db853b39c03",
                      "Generic PVA @BBL A2L": "321ba79c4e66d26a3e94d8d3c8418a713c1d508f1928319a8b8c9b25e5e5657a",
                      "Generic TPU @BBL A2L": "ea0187f0b785b7555538d79c1db05f137ef485bcdc52308e48cb4fbf9db9c891"}
    }), "ORCA", True),
    # The A2L is the large-format A-series bed-slinger: 330 x 320 mm bed, 325 mm height, and —
    # as its machine profile declares — a single 0.4 nozzle (nozzle_diameter has one entry).
    "bed_mm": Fact((330.0, 320.0), "ORCA", True),
    "max_print_height_mm": Fact(325.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((-20.0, -10.0, 330.0, 320.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "default_plate": Fact("Textured PEI Plate", "ORCA", True),
    "stock_nozzle": Fact((0.4, "stainless_steel"), "ORCA", True),
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", False),
    "max_hotend_c": Fact(300, "BAMBU", False),
    "max_bed_c": Fact(80, "BAMBU", False),
    # Bambu lists PLA, PETG, TPU and support materials as ideal for the open-frame A2L; the
    # high-temperature materials are not recommended (as on the A1).
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "PVA"), "BAMBU", False),
    "not_recommended_materials": Fact(("ABS", "ASA", "PC", "PA", "PET"), "BAMBU", False),
    # AMS Lite, as on the A1; not driven by Print Handy.
    "ams": Fact(("AMS Lite", 4), "BAMBU", False),
    # Open frame.
    "enclosed": Fact(False, "BAMBU", False),
}


# --- H3C: initial support (facts only — NOT a registered model) ----------------------
# The H3C is Bambu Lab's nine-tool Swaptek tool changer, launched 2026-11-03 alongside
# Bambu Studio 02.09.01.70 (the first Studio release carrying it). What is recorded here
# was read directly from Bambu Studio at the pinned commit 56a57d00a591360cf41bf21383915ee5bf357be5
# (the BAMBU_REF PR #990 pins): resources/printers/N10.json (the H3C's device definition:
# display_name, model_id "N10", sn_prefix, nozzle/bed temperature ranges, default nozzle
# count) and resources/profiles/BBL/machine/Bambu Lab H3C*.json (the model profile and
# the four nozzle machine profiles). Spec-page values that could not be read from
# bambulab.com directly are marked unverified, like the other models' BAMBU facts.
#
# There is deliberately NO orca_profiles / orca_filaments / orca_identity here, and no
# MODEL module, registry entry or adapter for the H3C: the pinned OrcaSlicer v2.5.0-rc
# (ORCA_RELEASE above; resources/profiles/BBL at af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea)
# carries no H3C profiles at all — its BBL index and machine folder were checked and
# contain none — so no identity can be computed with Print Handy's flatten + sha256
# pipeline, and registration without an identity is not a supported shape (the profile
# checks index the identity fact directly and would fail open-endedly, and every
# registered-model test contract assumes one). This is the H2C/A2L situation before
# #356, one step further back: the H2C waited unregistered while Orca had no profile
# for it, and was registered only when the Orca 2.5 line brought one. Full H3C
# registration is blocked on an OrcaSlicer release carrying H3C BBL profiles; when one
# is pinned, the model lands the #356 way (identities computed over that tree and its
# packed cache, a MODEL module, a registry entry, family tests).
#
# What this block already wires up: the serial prefix. An H3C's serial starts with
# "32X" (N10.json's sn_prefix), and the HMS table regenerated from Studio 02.09.01.70
# (PR #990) carries 32X per-model override sections. The HMS decode path is
# serial-driven, not model-driven (adapter._hms_prefix takes the configured serial's
# first three characters and consults the table's per-model sections), so no decode
# code waits on registration: the recorded prefix here is the reference the H3C's
# own registration will use, and the resolution path is test-pinned against a
# synthetic 32X table in tests/printers/test_bambu_hms.py.

H3C = {
    # printer_model as Bambu Studio's machine profile writes it; the device
    # definition (resources/printers/N10.json) names the same display name.
    "printer_model": Fact("Bambu Lab H3C", "BAMBU", True),
    # The first three characters of an H3C serial number: N10.json's "sn_prefix",
    # read at the pinned Studio commit above. This is the HMS per-model prefix.
    "serial_prefix": Fact("32X", "BAMBU", True),
    # printable_area 0x0..330x320 and printable_height 325 in the Studio machine
    # profile; Bambu's spec pages publish the same 330 x 320 x 325 mm volume.
    "bed_mm": Fact((330.0, 320.0), "BAMBU", True),
    "max_print_height_mm": Fact(325.0, "BAMBU", True),
    # The Studio machine profile's bed_exclude_area is empty.
    "bed_exclude_area": Fact((), "BAMBU", True),
    # The Studio machine profile's default_bed_type.
    "default_plate": Fact("Textured PEI Plate", "BAMBU", True),
    # No motion envelope or travel headroom: those are DERIVED from the vendor
    # G-code templates of a model's Orca profiles, and no Orca profiles exist yet.
    # The fitted tool is a 0.4 mm hardened steel PrintTool (Bambu's H3C pages, via
    # launch coverage; not read from bambulab.com directly, so unverified).
    "stock_nozzle": Fact((0.4, "hardened_steel"), "BAMBU", False),
    # The model profile offers 0.2, 0.4, 0.6 and 0.8 mm, one machine profile each.
    "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), "BAMBU", True),
    # N10.json's nozzle_temp_range / bed_temp_range upper bounds.
    "max_hotend_c": Fact(350, "BAMBU", True),
    "max_bed_c": Fact(120, "BAMBU", True),
    # The filament families with dedicated @BBL H3C profiles at the pinned Studio
    # commit (PLA, PETG, TPU, ABS, ASA, PC and PA families, CF/GF fills included).
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "BAMBU", False),
    "not_recommended_materials": Fact((), "BAMBU", False),
    # Nine PrintTools on the Swaptek rack (N10.json's default_nozzle_count; the
    # machine profile's extruder_max_nozzle_count agrees). This is recorded as
    # "tools", NOT "extruders": the extruders fact counts a profile's
    # nozzle_diameter entries for the extruder-aware identity/guard checks, and
    # the H3C's tools share the toolhead's single FlexFeed extruder — claiming an
    # extruder count would misstate the architecture those checks assume.
    "tools": Fact(9, "BAMBU", True),
    # AMS 3H: nine slots on top of the machine, two of them drying slots (Bambu's
    # H3C pages, via launch coverage; unverified). Not driven by Print Handy.
    "ams": Fact(("AMS 3H", 9), "BAMBU", False),
    "enclosed": Fact(True, "BAMBU", False),
}
