"""Sourced hardware facts and template provenance for Prusa printers. Supplemental metadata only.

Connection, start code, slicers, delivery, defaults and registration are decided by VENDOR
(prusa/__init__.py) and each MODEL; nothing here overrides them. Values follow the bambulab/facts.py
conventions: ORCA values were read directly from the Prusa vendor profiles of OrcaSlicer v2.4.2
(vendor profiles 02.04.00.03), v2.5.0-alpha / v2.5.0-beta (02.04.00.17 in both) and the v2.5.0 final
(02.04.00.18), as the JSON tree and as the packed
preset cache an installed 2.5 serves, flattened with the shared profile pipeline (identities
reproduce the same way the Bambu ones do), PRUSA values come from Prusa's published product
specifications and were not reached from the sandbox that wrote these files, DERIVED values are
computed from the ORCA templates. verified=True means the value was read from its source directly.

Upstream renamed the whole "Prusa Generic …" filament family in 2.5.0-alpha ("Generic PLA
@Prusa MK4S" and kin), so the checked `orca_profiles` names below are the 2.5 ones and
generating templates takes a 2.5 install (2.5.0-alpha, 2.5.0-beta or 2.5.0); the recorded 2.4.2 identities remain so
folders already generated from 2.4.2 keep verifying (problems() hashes the stored copies, it
does not re-resolve profile names). The machine profiles store start/end G-code as a list of
lines; profiles.py joins them into the string form every other vendor's templates use when it
writes templates.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


ORCA_RELEASE = ("v2.5.0-beta", "345efec64c7b95a05ce5cf96df0dde66a158835c")
ORCA_PROFILE_ROOT = "resources/profiles/Prusa"
_IDENTITY_LABEL = "OrcaSlicer 2.4.2 (Prusa vendor profiles 02.04.00.03)"
_IDENTITY_LABEL_25A = "OrcaSlicer 2.5.0-alpha (Prusa vendor profiles 02.04.00.17)"
_IDENTITY_LABEL_25A_PACKED = "OrcaSlicer 2.5.0-alpha (Prusa vendor profiles 02.04.00.17, packed cache)"
_IDENTITY_LABEL_25B = "OrcaSlicer 2.5.0-beta (Prusa vendor profiles 02.04.00.17)"
_IDENTITY_LABEL_25B_PACKED = "OrcaSlicer 2.5.0-beta (Prusa vendor profiles 02.04.00.17, packed cache)"
_IDENTITY_LABEL_250 = "OrcaSlicer 2.5.0 (Prusa vendor profiles 02.04.00.18)"
_IDENTITY_LABEL_250_PACKED = "OrcaSlicer 2.5.0 (Prusa vendor profiles 02.04.00.18, packed cache)"


def _entry(label, vendor_version, machine, process, filament):
    return {"label": label, "vendor_version": vendor_version,
            "sha256": {"machine": machine, "process": process, "filament": filament}}


def _identities(release_242, alpha_tree, alpha_packed, beta_tree, beta_packed,
                final_tree, final_packed):
    """The checked identities for a model, oldest first: OrcaSlicer 2.4.2's JSON tree, then
    OrcaSlicer 2.5.0-alpha's tree and its packed preset cache, then OrcaSlicer 2.5.0-beta in
    the same two packagings, then the 2.5.0 final in the same two packagings (the release
    candidate kept the beta's vendor version and hashes, so it records no entries of its
    own; the final moved the vendor profiles to 02.04.00.18). The two 2.5 packagings flatten
    differently (the cache applies Orca's option renames and drops unknown options), so each
    carries its own hashes; templates generated from any of them stay valid."""
    return Fact((_entry(_IDENTITY_LABEL, "02.04.00.03", *release_242),
                 _entry(_IDENTITY_LABEL_25A, "02.04.00.17", *alpha_tree),
                 _entry(_IDENTITY_LABEL_25A_PACKED, "02.04.00.17", *alpha_packed),
                 _entry(_IDENTITY_LABEL_25B, "02.04.00.17", *beta_tree),
                 _entry(_IDENTITY_LABEL_25B_PACKED, "02.04.00.17", *beta_packed),
                 _entry(_IDENTITY_LABEL_250, "02.04.00.18", *final_tree),
                 _entry(_IDENTITY_LABEL_250_PACKED, "02.04.00.18", *final_packed)), "ORCA", True)


MK4S = {
    "printer_model": Fact("Prusa MK4S", "ORCA", True),
    "orca_profiles": Fact({"machine": "Prusa MK4S 0.4 nozzle", "process": "0.20mm SPEED @MK4S 0.4",
                           "filament": "Generic PLA @Prusa MK4S"}, "ORCA", True),
    "orca_identity": _identities(("a6e0656b4ca33570eca4b22fc8f0875b320a81bfc637cba442de6e27391acf92", "a883e071bbbd1b7f40f23ea8ae0b0b4fa2419aae08efa4a3af377d0b4e33bebf", "ee4d5ee2806a3786400f38d625832aac261a1451d7f2cc5e0eb51672dc252a21"),
                                 ("0c2c0f4c61c45f2500e1e603640855d37adbe694f8622fcaaf361bd900f5811a", "769e3270294f065223e7f2bd13623d55747e96862901c066148e061c6529703a", "08b4bf0a1767c05792b3174d49ac11c2bf124edda824859765868b3feb725821"),
                                 ("af3e3e48d0de061f5e211c6390b4a0b7a49e2f17cbc3e99e7500a4c777c7f232", "af4f58ce5c04e18b9a0358c1196592eefe14ad1c7f1ffd837735b6821115e915", "20e338fd51c44e33f0faf289c1ab3cb575a136e53311ad79ba07265a5110508e"),
                                 ("0c2c0f4c61c45f2500e1e603640855d37adbe694f8622fcaaf361bd900f5811a", "769e3270294f065223e7f2bd13623d55747e96862901c066148e061c6529703a", "08b4bf0a1767c05792b3174d49ac11c2bf124edda824859765868b3feb725821"),
                                 ("af3e3e48d0de061f5e211c6390b4a0b7a49e2f17cbc3e99e7500a4c777c7f232", "f3af3f44637a96eda39029575b12ea7fa1e73c4ab6bc46f61cd2607a3c4aee21", "20e338fd51c44e33f0faf289c1ab3cb575a136e53311ad79ba07265a5110508e"),
                                 ("0c2c0f4c61c45f2500e1e603640855d37adbe694f8622fcaaf361bd900f5811a", "4e31a3a109be8e6dae736c9c295ad4c7bf4904ad1c72005ca8247addee83d0bb", "08b4bf0a1767c05792b3174d49ac11c2bf124edda824859765868b3feb725821"),
                                 ("af3e3e48d0de061f5e211c6390b4a0b7a49e2f17cbc3e99e7500a4c777c7f232", "f3af3f44637a96eda39029575b12ea7fa1e73c4ab6bc46f61cd2607a3c4aee21", "20e338fd51c44e33f0faf289c1ab3cb575a136e53311ad79ba07265a5110508e")),
    "bed_mm": Fact((250.0, 210.0), "ORCA", True),
    "max_print_height_mm": Fact(220.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -4.0, 250.0, 210.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "stock_nozzle": Fact((0.4, "brass"), "PRUSA", False),
    "nozzle_sizes_mm": Fact((0.25, 0.4, 0.6, 0.8), "PRUSA", False),
    "max_hotend_c": Fact(290, "PRUSA", False),
    "max_bed_c": Fact(120, "PRUSA", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "PRUSA", False),
    "tools": Fact(1, "PRUSA", False),
    "enclosed": Fact(False, "PRUSA", False),
}

COREONE = {
    "printer_model": Fact("Prusa CORE One", "ORCA", True),
    "orca_profiles": Fact({"machine": "Prusa CORE One 0.4 nozzle", "process": "0.20mm SPEED @CORE One 0.4",
                           "filament": "Prusament PLA @CORE One"}, "ORCA", True),
    "orca_identity": _identities(("0f459a1a7c43c5686c14b52052c29ac75cdd4891c9250094885a16d3370db361", "c4685e5445632fea68f223771f9d2a41c6602e0daaddcb60ef6593e4d6890aa6", "91ee9aba52aa662d6ee067e5e3215771d2c7ec034730dde1263608ee621070bb"),
                                 ("0f459a1a7c43c5686c14b52052c29ac75cdd4891c9250094885a16d3370db361", "8917f9f55e2ee5e779758b69c89946eb4a3adaf22999deaf78928eaeb82d6045", "d9973c25e506b667ac1baf5d1f158ffc55c1a707f49e75590d0124847e81f8c5"),
                                 ("0af2518046d7449bcf4f3bee609d17170a05339a17a55b8ae7a91aa9fe6bfbd9", "d7eaa5be19193bf66d6e2845cbe101662814a93ee7e43f9b5eb6893e2782eaba", "85e7d36c29e792a654da87e974dd19d18f59daa80fc1be721723e0e481891785"),
                                 ("0f459a1a7c43c5686c14b52052c29ac75cdd4891c9250094885a16d3370db361", "8917f9f55e2ee5e779758b69c89946eb4a3adaf22999deaf78928eaeb82d6045", "d9973c25e506b667ac1baf5d1f158ffc55c1a707f49e75590d0124847e81f8c5"),
                                 ("0af2518046d7449bcf4f3bee609d17170a05339a17a55b8ae7a91aa9fe6bfbd9", "ac71a59c00efd42915125d74e09c26092ffb1235ac928b2736d0ac29e6cb5f17", "85e7d36c29e792a654da87e974dd19d18f59daa80fc1be721723e0e481891785"),
                                 ("0f459a1a7c43c5686c14b52052c29ac75cdd4891c9250094885a16d3370db361", "7f569b377d94911f2be33320620299e36ba34f2b6fee369a11f2a94fce31586b", "d9973c25e506b667ac1baf5d1f158ffc55c1a707f49e75590d0124847e81f8c5"),
                                 ("0af2518046d7449bcf4f3bee609d17170a05339a17a55b8ae7a91aa9fe6bfbd9", "ac71a59c00efd42915125d74e09c26092ffb1235ac928b2736d0ac29e6cb5f17", "85e7d36c29e792a654da87e974dd19d18f59daa80fc1be721723e0e481891785")),
    "bed_mm": Fact((250.0, 220.0), "ORCA", True),
    "max_print_height_mm": Fact(270.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -9.0, 250.0, 220.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "stock_nozzle": Fact((0.4, "brass"), "PRUSA", False),
    "nozzle_sizes_mm": Fact((0.25, 0.4, 0.6, 0.8), "PRUSA", False),
    "max_hotend_c": Fact(290, "PRUSA", False),
    "max_bed_c": Fact(120, "PRUSA", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "PRUSA", False),
    "tools": Fact(1, "PRUSA", False),
    "enclosed": Fact(True, "PRUSA", False),
}

XL = {
    # The 5-tool XL is a multi_tool model (#670): its toolheads are driven through the
    # adapter contract; readiness/status stay printer-level (no per-tool PrusaLink telemetry).
    "printer_model": Fact("Prusa XL 5T", "ORCA", True),
    "orca_profiles": Fact({"machine": "Prusa XL 5T 0.4 nozzle", "process": "0.20mm Speed @Prusa XL 5T 0.4",
                           "filament": "Generic PLA @Prusa XL"}, "ORCA", True),
    "orca_identity": _identities(("374fecdd33aeb7fdf3d7e2fb9299d3870890fd2be7487feaaebe01502d2fc32a", "58d27bec25b44655d8acacb1438e338702a706cbc249806583713f9676dce4b4", "c52476ebdcdc2b86a24e8eb9ce8d837ee86f546da16ca40f2d7676d07a78e7d4"),
                                 ("9c5fbe83190069909850e84a44a48d78174ef7c082ed3bfe4db151e159b81b0d", "43ee1057b855924de34fc3ee01efb6d6364bcc200366df333e2ffcd86fbea3e9", "5ce6b86be7cab63cedc14f74938cc32a6e83a564cfe7c1aa5cd4d27081ede231"),
                                 ("49d744689917840761f006b57e67e5ca3a6a73a4e226147106367b56060a50e8", "9d83029dc817ddbf38995b242822876a64838646f5541095f6bc54e41ad6755e", "a90103513033b8f2e62363497e2682f869ea8e344024f9fcbe7c8b533ac80878"),
                                 ("9c5fbe83190069909850e84a44a48d78174ef7c082ed3bfe4db151e159b81b0d", "43ee1057b855924de34fc3ee01efb6d6364bcc200366df333e2ffcd86fbea3e9", "5ce6b86be7cab63cedc14f74938cc32a6e83a564cfe7c1aa5cd4d27081ede231"),
                                 ("49d744689917840761f006b57e67e5ca3a6a73a4e226147106367b56060a50e8", "3948e24078aaa6cc05e31b2a453334ba638c1138cc919ae3151b6e3f48dff9e3", "a90103513033b8f2e62363497e2682f869ea8e344024f9fcbe7c8b533ac80878"),
                                 ("f9de3c90ae08a2d7a29e21c636fc8dd857b364891d3c9efad63e5a0d5ba85d28", "2a8f42b976e14387784801fc84e20761b30975c1430d92da9c3bbaf119473067", "5ce6b86be7cab63cedc14f74938cc32a6e83a564cfe7c1aa5cd4d27081ede231"),
                                 ("055bfce16a8b49848793b89f3b5949115d629390dceaaac4ccec63935a9e582e", "3948e24078aaa6cc05e31b2a453334ba638c1138cc919ae3151b6e3f48dff9e3", "a90103513033b8f2e62363497e2682f869ea8e344024f9fcbe7c8b533ac80878")),
    "bed_mm": Fact((360.0, 360.0), "ORCA", True),
    "max_print_height_mm": Fact(360.0, "ORCA", True),
    # The flattened profile's bed_exclude_area is ["0x0"], a single origin point: no exclusion.
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, 0.0, 360.0, 360.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "stock_nozzle": Fact((0.4, "brass"), "PRUSA", False),
    "nozzle_sizes_mm": Fact((0.25, 0.4, 0.6, 0.8), "PRUSA", False),
    "max_hotend_c": Fact(290, "PRUSA", False),
    "max_bed_c": Fact(120, "PRUSA", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU", "ABS", "ASA", "PC", "PA"), "PRUSA", False),
    "tools": Fact(5, "PRUSA", False),
    "enclosed": Fact(False, "PRUSA", False),
}

MINIPLUS = {
    # The MINI+ runs the MINI profiles: Orca's machine printer_model is "Prusa MINI".
    "printer_model": Fact("Prusa MINI", "ORCA", True),
    "orca_profiles": Fact({"machine": "Prusa MINI 0.4 nozzle", "process": "0.20mm Standard @MINI",
                           "filament": "Generic PLA @Prusa"}, "ORCA", True),
    "orca_identity": _identities(("b4bb5458837a5c118cb60d158010b7a1bb265696671ad32f1acdd207297f7d6d", "fc4f42973415e07680e30915a1884ce02d1ea1284c1b599e3d2a0a772d70a8d8", "cff434489029840d6f489e41f87aa46b422e8f9d4c7927dc0ed224e941780a97"),
                                 ("64fa6ca9481fc996fd54eca55f28b435dd60f347861d30c1f583b80fbfaf47e0", "3b52a62c5db3b028971ba3057466361b8dcaf094e166b28afbb599d8ec59ff62", "17a569ac838bc39b65dd0f4b6d8d72b4cf9cfee9558fcbaea117c32ed45ed5d4"),
                                 ("688dfb6d3bb3645657089d3e7e50a355b951131f36204b16b50eb8d14c473946", "095b1ec3d043c51a50e9863214edaf7b3d642080d345056eebfc164edb00f4b5", "124f61b7f8c61d3945f2d04d65fa57a63e6e9cef4da0fb940cd570dc195c9b34"),
                                 ("64fa6ca9481fc996fd54eca55f28b435dd60f347861d30c1f583b80fbfaf47e0", "3b52a62c5db3b028971ba3057466361b8dcaf094e166b28afbb599d8ec59ff62", "17a569ac838bc39b65dd0f4b6d8d72b4cf9cfee9558fcbaea117c32ed45ed5d4"),
                                 ("688dfb6d3bb3645657089d3e7e50a355b951131f36204b16b50eb8d14c473946", "9adc63bd06f1cdf35fac69634c933ca74761c73f994b0ca9327235367d796a07", "124f61b7f8c61d3945f2d04d65fa57a63e6e9cef4da0fb940cd570dc195c9b34"),
                                 ("64fa6ca9481fc996fd54eca55f28b435dd60f347861d30c1f583b80fbfaf47e0", "3b52a62c5db3b028971ba3057466361b8dcaf094e166b28afbb599d8ec59ff62", "17a569ac838bc39b65dd0f4b6d8d72b4cf9cfee9558fcbaea117c32ed45ed5d4"),
                                 ("688dfb6d3bb3645657089d3e7e50a355b951131f36204b16b50eb8d14c473946", "9adc63bd06f1cdf35fac69634c933ca74761c73f994b0ca9327235367d796a07", "124f61b7f8c61d3945f2d04d65fa57a63e6e9cef4da0fb940cd570dc195c9b34")),
    "bed_mm": Fact((180.0, 180.0), "ORCA", True),
    "max_print_height_mm": Fact(180.0, "ORCA", True),
    "bed_exclude_area": Fact((), "ORCA", True),
    "motion_envelope_mm": Fact((0.0, -2.0, 180.0, 180.0), "DERIVED", False),
    "travel_z_headroom_mm": Fact(1.0, "DERIVED", False),
    "stock_nozzle": Fact((0.4, "brass"), "PRUSA", False),
    "nozzle_sizes_mm": Fact((0.25, 0.4, 0.6, 0.8), "PRUSA", False),
    "max_hotend_c": Fact(280, "PRUSA", False),
    "max_bed_c": Fact(100, "PRUSA", False),
    "ideal_materials": Fact(("PLA", "PETG", "TPU"), "PRUSA", False),
    "tools": Fact(1, "PRUSA", False),
    "enclosed": Fact(False, "PRUSA", False),
}
