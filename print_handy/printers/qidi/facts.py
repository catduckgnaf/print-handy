"""Sourced facts for the Qidi models. Bed size and height are OrcaSlicer's printable_area /
printable_height for the model (the registry convention), never the marketed build volume;
the X-Smart 3 is the case that proves the rule — it is marketed as 180 x 180 mm but the Orca
profile's printable area is 175 x 180 mm, and the profile value is what is recorded. The Q1
Pro's bed exclusion polygon (the purge zone along the back edge) is Orca's bed_exclude_area,
recorded point for point. Identities pin the machine, process and checked PLA filament
profiles a generated template folder must hash to, read from the pinned OrcaSlicer installs
(2.4.2 vendor profiles 02.04.00.06; 2.5.0-alpha 02.04.00.17; 2.5.0-beta 02.04.00.19; 2.5.0-rc 02.04.00.20). One 2.5 change to know:
upstream renamed the vendor-prefixed filament family (`Qidi Generic PLA ...`) to the generic
form (`Generic PLA ...`), so the checked default filaments recorded here carry the 2.5.0-alpha
names — the same rename the Prusa pack records. The recorded 2.4.2 identities were computed
with the 2.4.2-era filament names, so a folder generated from 2.4.2 keeps verifying against
its own stored copies. Temperature ceilings are the manufacturer's headline figures, recorded
unverified like the Elegoo pack's; they feed only the post-slice foreign-file guard."""
from dataclasses import dataclass

ORCA = "OrcaSlicer bundled Qidi profile (pinned install)"
QIDI = "Qidi published specification (unverified)"


@dataclass(frozen=True)
class Fact:
    value: object
    source: str
    verified: bool


def _identities(*packagings):
    """Checked upstream identities for one model, in a fixed order: OrcaSlicer 2.4.2
    (resources tree), OrcaSlicer 2.5.0-alpha (resources tree), the same 2.5.0-alpha
    content from an installed OrcaSlicer's packed preset cache, then OrcaSlicer
    2.5.0-beta and OrcaSlicer 2.5.0-rc, each in the same two packagings. The packed form hashes
    differently from the tree (payloads are re-serialised into the cache container and
    every profile carries an extra preset-identity field), so the packaging is part of
    the identity — the Prusa pack's precedent. Generating templates takes a 2.5 installation
    (2.5.0-alpha, 2.5.0-beta or 2.5.0-rc; the rename note in the module docstring); a folder generated from
    2.4.2 keeps verifying against its stored copies."""
    labels = (
        "OrcaSlicer 2.4.2 (Qidi vendor profiles 02.04.00.06)",
        "OrcaSlicer 2.5.0-alpha (Qidi vendor profiles 02.04.00.17)",
        "OrcaSlicer 2.5.0-alpha (Qidi vendor profiles 02.04.00.17, packed cache)",
        "OrcaSlicer 2.5.0-beta (Qidi vendor profiles 02.04.00.19)",
        "OrcaSlicer 2.5.0-beta (Qidi vendor profiles 02.04.00.19, packed cache)",
        "OrcaSlicer 2.5.0-rc (Qidi vendor profiles 02.04.00.20)",
        "OrcaSlicer 2.5.0-rc (Qidi vendor profiles 02.04.00.20, packed cache)",
    )
    return tuple(
        {"label": label, "vendor_version": version, "sha256": hashes}
        for label, version, hashes in zip(labels, ('02.04.00.06', '02.04.00.17', '02.04.00.17', '02.04.00.19', '02.04.00.19', '02.04.00.20', '02.04.00.20'), packagings))


def _model(*, printer_model, names, hashes, bed, height, exclusion, marketed,
           nozzle_type, hotend, bed_temp, materials, enclosed):
    width, depth = bed
    return {
        "printer_model": Fact(printer_model, ORCA, True),
        "orca_profiles": Fact(names, ORCA, True),
        "orca_identity": Fact(_identities(*hashes), ORCA, True),
        "bed_mm": Fact((float(width), float(depth)), ORCA, True),
        "max_print_height_mm": Fact(float(height), ORCA, True),
        "bed_exclude_area": Fact(exclusion, ORCA, True),
        "motion_envelope_mm": Fact((0.0, 0.0, float(width), float(depth)), "DERIVED from the Orca printable area", True),
        "travel_z_headroom_mm": Fact(1.0, "DERIVED: printable height plus Z-hop headroom", True),
        "stock_nozzle": Fact((0.4, nozzle_type), ORCA, True),
        "nozzle_sizes_mm": Fact((0.2, 0.4, 0.6, 0.8), ORCA, True),
        "max_hotend_c": Fact(hotend, QIDI, False),
        "max_bed_c": Fact(bed_temp, QIDI, False),
        "ideal_materials": Fact(materials, QIDI, False),
        "tools": Fact(1, QIDI, False),
        "enclosed": Fact(enclosed, QIDI, False),
        "marketed_bed_mm": Fact((float(marketed[0]), float(marketed[1])), QIDI, False),
    }


MODELS = {
    "q1pro": _model(
        printer_model='Qidi Q1 Pro',
        names={"machine": 'Qidi Q1 Pro 0.4 nozzle', "process": '0.20mm Standard @Qidi Q1 Pro', "filament": 'Generic PLA @Qidi Q1 Pro 0.4 nozzle'},
        hashes=({'machine': '94f740976a63b06be1c5074d8d2312723021a69a7a9f909f92e220de819c83c9', 'process': 'ae44da2ac4024583f4d0adbe33f978dfe31b1856a56766e161c8424cdec4111c', 'filament': 'f1840c92523ecb1502483e6d8103bb7dd47fbd4a8fb57ba25f416aeb5c1b757c'}, {'machine': 'b700c0007335917802e9eec58513eb308dc26ec09b635eb59ce192cc1f5841c6', 'process': 'f6aa5a4180a81c8cbb01e6c270ee50e73b349092587aad44a930ddd8e22b8997', 'filament': '20820ab306f1d065a25e851589fb80154f23b6785e6bd886a8bac41d303ce387'}, {'machine': '72ffdf1f88efb5d0ce4f687babf19039588758e06a7b1e50ad71192f7a29d95b', 'process': '3cc36c804e82652d478607910e9ec22ef0fc78b024a8a919837724dc7b90444b', 'filament': '320b6141842f43495902b4cf3187b4386729ddfa8bad92a3a095d388c371c085'}, {'machine': 'b700c0007335917802e9eec58513eb308dc26ec09b635eb59ce192cc1f5841c6', 'process': 'f6aa5a4180a81c8cbb01e6c270ee50e73b349092587aad44a930ddd8e22b8997', 'filament': '20820ab306f1d065a25e851589fb80154f23b6785e6bd886a8bac41d303ce387'}, {'machine': '72ffdf1f88efb5d0ce4f687babf19039588758e06a7b1e50ad71192f7a29d95b', 'process': '1f89d5cdfc78577fe69379fa6f38372cf12eac7aa5fa03bdec7a46a68433cfcd', 'filament': '320b6141842f43495902b4cf3187b4386729ddfa8bad92a3a095d388c371c085'}, {'machine': 'b700c0007335917802e9eec58513eb308dc26ec09b635eb59ce192cc1f5841c6', 'process': '5091e2cc3f75f67d39f8db30d854c4b8d4e102e89539b58d329ad0de57349d04', 'filament': '20820ab306f1d065a25e851589fb80154f23b6785e6bd886a8bac41d303ce387'}, {'machine': '72ffdf1f88efb5d0ce4f687babf19039588758e06a7b1e50ad71192f7a29d95b', 'process': 'abeb32c8ff446d75b7c10dfa720eb1ab430012ec064abb961f14725dae6287b0', 'filament': '320b6141842f43495902b4cf3187b4386729ddfa8bad92a3a095d388c371c085'}),
        bed=(245, 245), height=240, exclusion=((25, 245), (25, 240), (115, 240), (115, 245), (208, 245), (208, 240), (245, 240), (245, 245), (208, 245)), marketed=(245, 245),
        nozzle_type='hardened_steel', hotend=350, bed_temp=120,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC', 'PA'), enclosed=True,
    ),
    "xplus3": _model(
        printer_model='Qidi X-Plus 3',
        names={"machine": 'Qidi X-Plus 3 0.4 nozzle', "process": '0.20mm Standard @Qidi XPlus3', "filament": 'Generic PLA @Qidi'},
        hashes=({'machine': 'be33e4dcb22ac044808b6ad2936103332f49bd786f8484e0ad1c5e9c5aff2bbb', 'process': '147c9399fe42b882d55d011b42bf745bf0954650ebffb84a1dce6d9ba3d0ad71', 'filament': 'fe2a68ec679b881ed1c8f3aaa549933c97c9b6d6bb94b1651fc40b526f95477f'}, {'machine': '2a398619552bc0918db30aad5b1cc401c393653e2035c041ac16bd3c45e2f5af', 'process': '1adef09755b1ac72a81234f911e4d246fb850ece8e593db423a6834c15b82221', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '8aabe114a9be720b20c8fbc3ca13c78f55db08f5e08067ec2fbe57a1afffa6ab', 'process': 'fb0ea88c1e6141922c93582f8c30be2cadc4f5d1613905dd3322600301e03907', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '2a398619552bc0918db30aad5b1cc401c393653e2035c041ac16bd3c45e2f5af', 'process': '1adef09755b1ac72a81234f911e4d246fb850ece8e593db423a6834c15b82221', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '8aabe114a9be720b20c8fbc3ca13c78f55db08f5e08067ec2fbe57a1afffa6ab', 'process': 'c96e85f1934d97aec7b16a2e34083706756f91ad2d8e3d331a4c5ff346e502c0', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '2a398619552bc0918db30aad5b1cc401c393653e2035c041ac16bd3c45e2f5af', 'process': 'dd6d8f1271e76929e398c18ba2a53114e2a9bd294dfb7f3e111e935d832d8a73', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '8aabe114a9be720b20c8fbc3ca13c78f55db08f5e08067ec2fbe57a1afffa6ab', 'process': 'e1beda8025d86e0890b97b8ab37e747d742e5d6e359fee6fc0cba52ee7dead51', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}),
        bed=(280, 280), height=270, exclusion=(), marketed=(280, 280),
        nozzle_type='hardened_steel', hotend=350, bed_temp=120,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC', 'PA'), enclosed=True,
    ),
    "xplus4": _model(
        printer_model='Qidi X-Plus 4',
        names={"machine": 'Qidi X-Plus 4 0.4 nozzle', "process": '0.20mm Standard @Qidi XPlus4', "filament": 'Generic PLA @Qidi X-Plus 4 0.4 nozzle'},
        hashes=({'machine': '0368426d92c4e66458eaac49d4e61542b8f74871d3ce90ebd1e183bcaa01641b', 'process': '5b26eb63a7ca8f15cbaeab94e9517df6558032cf1807367fe99f91840ee6a0c9', 'filament': 'cc7b8726f0722f8ba7ad86a34880920185a04635c086c4f340a90393fbd1b021'}, {'machine': '2117e7f0b0b91f1212a7a8f2b431b373570719857ba08378df493da98c071a59', 'process': '48916463e190b07f3aabcb050506dc043f75d80bd27eae60657410ec6a0a525b', 'filament': 'd92c17d6285cd15f7eda979d8641c07853426da4874ccaf6c2794c7607a1c7ef'}, {'machine': 'dc5a422f8a36fa4d3812c3032b8ca60d066e492cf66f0cb0e06e6ce72401cae5', 'process': '742e7d6f060cce40863d47b85221127f18ca70aa0b05e3a4e9d013cff476bd45', 'filament': '1582c2d45fbaf6a479dfa9f34d607e0d45134cdc6d8d14118890322dc0baf95a'}, {'machine': '2117e7f0b0b91f1212a7a8f2b431b373570719857ba08378df493da98c071a59', 'process': '48916463e190b07f3aabcb050506dc043f75d80bd27eae60657410ec6a0a525b', 'filament': 'd92c17d6285cd15f7eda979d8641c07853426da4874ccaf6c2794c7607a1c7ef'}, {'machine': 'dc5a422f8a36fa4d3812c3032b8ca60d066e492cf66f0cb0e06e6ce72401cae5', 'process': '453794443a23f40405d3b54524166ecbb0845592ead184211121f5026a855469', 'filament': '1582c2d45fbaf6a479dfa9f34d607e0d45134cdc6d8d14118890322dc0baf95a'}, {'machine': '2117e7f0b0b91f1212a7a8f2b431b373570719857ba08378df493da98c071a59', 'process': '0bf90e82abbeb10aa5bc76c68ce29a5a372c5663802fc0e88d54db1184b0cb39', 'filament': 'd92c17d6285cd15f7eda979d8641c07853426da4874ccaf6c2794c7607a1c7ef'}, {'machine': 'dc5a422f8a36fa4d3812c3032b8ca60d066e492cf66f0cb0e06e6ce72401cae5', 'process': '963a474e83b0004c19ab5a2dbda1e23b0fe62ef2180c86332d52367459cc9c45', 'filament': '1582c2d45fbaf6a479dfa9f34d607e0d45134cdc6d8d14118890322dc0baf95a'}),
        bed=(305, 305), height=280, exclusion=((0, 305), (0, 302), (35, 302), (35, 305), (305, 305), (305, 305), (305, 305), (305, 20), (293, 20), (293, 0), (305, 0), (305, 20), (305, 305)), marketed=(305, 305),
        nozzle_type='hardened_steel', hotend=370, bed_temp=120,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC', 'PA'), enclosed=True,
    ),
    "xmax3": _model(
        printer_model='Qidi X-Max 3',
        names={"machine": 'Qidi X-Max 3 0.4 nozzle', "process": '0.20mm Standard @Qidi XMax3', "filament": 'Generic PLA @Qidi'},
        hashes=({'machine': '8f2a839c492b755e1e24277dd3b67556182b09f2cc1b9f3d3780dd26f7291bdf', 'process': 'a1d76493d496ff07c2a3ff7ceb177bd87bd83e434e13ac99e7e58e8acf516008', 'filament': 'fe2a68ec679b881ed1c8f3aaa549933c97c9b6d6bb94b1651fc40b526f95477f'}, {'machine': '84811498ef29edda250d8c8ba7aac6645870a2774c59adf8c4cac2a268fa5fe4', 'process': '20cb78b302282dee863ee3b3be042b1068e464acd6effd6069e275a4efd81423', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': 'fc343ebc98bec1c67d3cfe5e196bf8e33543ba0b088a32184bb8c52417c6a604', 'process': '197591fd2f83838ac14e49875887eae82b778d30e1031d2b6b9512f745bdf115', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '84811498ef29edda250d8c8ba7aac6645870a2774c59adf8c4cac2a268fa5fe4', 'process': '20cb78b302282dee863ee3b3be042b1068e464acd6effd6069e275a4efd81423', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': 'fc343ebc98bec1c67d3cfe5e196bf8e33543ba0b088a32184bb8c52417c6a604', 'process': '1968070973d259e01956d573dbde038cca1408c1e944fe18a9fe3fe5d57b3b92', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '84811498ef29edda250d8c8ba7aac6645870a2774c59adf8c4cac2a268fa5fe4', 'process': 'c715c6e296d02ddb18c5fa063d9e8735446257ef82febeb1a380983800e6e3f3', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': 'fc343ebc98bec1c67d3cfe5e196bf8e33543ba0b088a32184bb8c52417c6a604', 'process': '7432ae1253c851b80e182752f8f26d209e55b7f2b275e8b234318236bd446885', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}),
        bed=(325, 325), height=315, exclusion=(), marketed=(325, 325),
        nozzle_type='hardened_steel', hotend=350, bed_temp=120,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC', 'PA'), enclosed=True,
    ),
    "xsmart3": _model(
        printer_model='Qidi X-Smart 3',
        names={"machine": 'Qidi X-Smart 3 0.4 nozzle', "process": '0.20mm Standard @Qidi XSmart3', "filament": 'Generic PLA @Qidi'},
        hashes=({'machine': 'c53f68c004028f73fb94807b65ef2c00283a8604c18aa7cd05a7b55c538644a7', 'process': 'cbd210d62f889f9a4d70a62af0eff8f874330fc652aa2bed62442bfe564ded04', 'filament': 'fe2a68ec679b881ed1c8f3aaa549933c97c9b6d6bb94b1651fc40b526f95477f'}, {'machine': '74b43660614c2a4d696ae98a9f8f6c0017e4c93736b447df97bdbb3dbbe2e509', 'process': '52f114e9656f7d961f5d6360ad6dd40e8e9cd8b2ce4d0307d08bed6cd6f3070f', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '2dfaa004fc0aa674073102a0fda178080d4432948826e32975426a6a117c9592', 'process': 'acd17575914dbabb68d778d545a9615e349a996fee5dfcc5fb9ce16ff10f9589', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '74b43660614c2a4d696ae98a9f8f6c0017e4c93736b447df97bdbb3dbbe2e509', 'process': '52f114e9656f7d961f5d6360ad6dd40e8e9cd8b2ce4d0307d08bed6cd6f3070f', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '2dfaa004fc0aa674073102a0fda178080d4432948826e32975426a6a117c9592', 'process': '51ae06b54ad6397e37a4c62d183ea8abb696aeb618c7f5067eb83af5d6ac1990', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}, {'machine': '74b43660614c2a4d696ae98a9f8f6c0017e4c93736b447df97bdbb3dbbe2e509', 'process': '130540b1b463c8dcc9e9ed1c83b17885bb49fe7d248e5b83ed3591617332e3fb', 'filament': 'e5d442f8e67350705a97f5c405c67ceaecee48334acfb325ad648afda9d96aab'}, {'machine': '2dfaa004fc0aa674073102a0fda178080d4432948826e32975426a6a117c9592', 'process': '3538cc833f3f9cfa9afec7294dbecbc58a0a54dc10d2d328765809bdd75f6550', 'filament': '90bd93928a28d8694aafef68ddf9591d2db4e2eff9f2f66c9139b036b1323b2c'}),
        bed=(175, 180), height=170, exclusion=(), marketed=(180, 180),
        nozzle_type='hardened_steel', hotend=300, bed_temp=110,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC', 'PA'), enclosed=True,
    ),
}


Q1PRO = MODELS["q1pro"]
XPLUS3 = MODELS["xplus3"]
XPLUS4 = MODELS["xplus4"]
XMAX3 = MODELS["xmax3"]
XSMART3 = MODELS["xsmart3"]
