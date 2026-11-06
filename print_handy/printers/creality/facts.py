"""Sourced facts for the Creality models: the Klipper-based Creality OS line only (Ender-3 V3
series and K1 series). Bed size and height are OrcaSlicer's printable_area /
printable_height for the model (the registry convention), never the marketed build volume.
The vendor writes bed_exclude_area two ways — a ["0x0"] list on the Ender-3 V3 line and a
bare "0x0" string on the K1 line — and both mean no exclusion, so every model here records
an empty tuple and the profile checker treats the bare string as empty. Identities pin the
machine, process and checked PLA filament profiles a generated template folder must hash
to, read from the pinned OrcaSlicer installs (2.4.2 vendor profiles 02.03.02.75;
2.5.0-alpha 02.03.02.87; 2.5.0-beta kept 02.03.02.87; 2.5.0-rc 02.03.02.88). One 2.5 change to know: upstream renamed the vendor-prefixed
filament family (`Creality Generic PLA ...`, `Creality HF Generic PLA`) to the generic form
(`Generic PLA ...`), so the checked default filaments recorded here carry the 2.5.0-alpha
names — the same rename the Prusa pack records. The recorded 2.4.2 identities were computed
with the 2.4.2-era filament names, so a folder generated from 2.4.2 keeps verifying against
its own stored copies. Three upstream quirks are recorded so they are not "fixed" later:
the Ender-3 V3 and V3 Plus machine profiles name a default_print_profile that does not
exist in the shipped process list (the list names are the real ones and are what is
recorded), the K1C machine profile's default adds a "(0.4 nozzle)" suffix its process
profile does not carry, and the checked filament names genuinely differ per model in 2.5
(`@Ender-3 V3-all` vs `@Creality Ender-3V3-all` vs `@Ender-3 V3 Plus-all`). Temperature
ceilings are the manufacturer's headline figures, recorded unverified like the Elegoo
pack's; they feed only the post-slice foreign-file guard."""
from dataclasses import dataclass

ORCA = "OrcaSlicer bundled Creality profile (pinned install)"
CREALITY = "Creality published specification (unverified)"


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
        "OrcaSlicer 2.4.2 (Creality vendor profiles 02.03.02.75)",
        "OrcaSlicer 2.5.0-alpha (Creality vendor profiles 02.03.02.87)",
        "OrcaSlicer 2.5.0-alpha (Creality vendor profiles 02.03.02.87, packed cache)",
        "OrcaSlicer 2.5.0-beta (Creality vendor profiles 02.03.02.87)",
        "OrcaSlicer 2.5.0-beta (Creality vendor profiles 02.03.02.87, packed cache)",
        "OrcaSlicer 2.5.0-rc (Creality vendor profiles 02.03.02.88)",
        "OrcaSlicer 2.5.0-rc (Creality vendor profiles 02.03.02.88, packed cache)",
    )
    return tuple(
        {"label": label, "vendor_version": version, "sha256": hashes}
        for label, version, hashes in zip(labels, ('02.03.02.75', '02.03.02.87', '02.03.02.87', '02.03.02.87', '02.03.02.87', '02.03.02.88', '02.03.02.88'), packagings))


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
        "max_hotend_c": Fact(hotend, CREALITY, False),
        "max_bed_c": Fact(bed_temp, CREALITY, False),
        "ideal_materials": Fact(materials, CREALITY, False),
        "tools": Fact(1, CREALITY, False),
        "enclosed": Fact(enclosed, CREALITY, False),
        "marketed_bed_mm": Fact((float(marketed[0]), float(marketed[1])), CREALITY, False),
    }


MODELS = {
    "ender3v3": _model(
        printer_model='Creality Ender-3 V3',
        names={"machine": 'Creality Ender-3 V3 0.4 nozzle', "process": '0.20mm Standard @Creality Ender-3 V3', "filament": 'Generic PLA @Ender-3 V3-all'},
        hashes=({'machine': 'f0b498b797d025b39b8b670d43b138d06942bbf9fae84f3c06cf771021b9d909', 'process': 'd14a71542128e6823f28f5d1fb1717fda33f156d6708e4c8c1ed2993cb397b42', 'filament': 'd74512d9eb11cba13d1fd106a0b4466f24919a2ef4695059effab118f765372f'}, {'machine': '56f57f77e7f44d597d04e5305dcdbb02743e92eb0ecc1e6fe88f4e1f44074b48', 'process': 'd3213502049185d659d278cbd306f131db6e699654b53a500541eccd8f3b2183', 'filament': '4d5a31e9b0b6b43a63dab13c65fe3b933f8b6f740a43307c3245eef2c52f6216'}, {'machine': '7f9c936131f14a86afd5e18245f57f71d67c6927021227b681a9063f813e1772', 'process': 'f82c897d2f7879bae34492cfbf80775cfe6db7c207357047e00a818f2843b2c6', 'filament': 'b0133663cbcb8336d6f61445492896e948611e6e306d1a1b32ee5622bb38e412'}, {'machine': '56f57f77e7f44d597d04e5305dcdbb02743e92eb0ecc1e6fe88f4e1f44074b48', 'process': 'd3213502049185d659d278cbd306f131db6e699654b53a500541eccd8f3b2183', 'filament': '4d5a31e9b0b6b43a63dab13c65fe3b933f8b6f740a43307c3245eef2c52f6216'}, {'machine': '7f9c936131f14a86afd5e18245f57f71d67c6927021227b681a9063f813e1772', 'process': '15418e0dfa2e1305591fea2f0dbf50001e8ab7323833ad90918710ed7e591cb1', 'filament': 'b0133663cbcb8336d6f61445492896e948611e6e306d1a1b32ee5622bb38e412'}, {'machine': '56f57f77e7f44d597d04e5305dcdbb02743e92eb0ecc1e6fe88f4e1f44074b48', 'process': 'd3213502049185d659d278cbd306f131db6e699654b53a500541eccd8f3b2183', 'filament': '4d5a31e9b0b6b43a63dab13c65fe3b933f8b6f740a43307c3245eef2c52f6216'}, {'machine': '7f9c936131f14a86afd5e18245f57f71d67c6927021227b681a9063f813e1772', 'process': '15418e0dfa2e1305591fea2f0dbf50001e8ab7323833ad90918710ed7e591cb1', 'filament': 'b0133663cbcb8336d6f61445492896e948611e6e306d1a1b32ee5622bb38e412'}),
        bed=(220, 220), height=250, exclusion=(), marketed=(220, 220),
        nozzle_type='hardened_steel', hotend=300, bed_temp=110,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA'), enclosed=False,
    ),
    "ender3v3ke": _model(
        printer_model='Creality Ender-3 V3 KE',
        names={"machine": 'Creality Ender-3 V3 KE 0.4 nozzle', "process": '0.20mm Standard @Creality Ender3V3KE', "filament": 'Generic PLA @Creality Ender-3V3-all'},
        hashes=({'machine': '0ddaf3903b66e5d3a67ec84b171aeab6e1c04e37406d93ee5998aa91d3ec3e15', 'process': 'fc73b9efee8b27b234cfa0836f1625da528fe1d540404adb8a1666f1130a3fe7', 'filament': 'd74512d9eb11cba13d1fd106a0b4466f24919a2ef4695059effab118f765372f'}, {'machine': '4e621b73e0972a9edf601ccc0d33dde179ba7539fd5d19cbe87e2ac8499b2354', 'process': 'a6334272bec36d79b6beabbe0677c999b73d27eda8966d428927d0fb171fd5d4', 'filament': '621da411d2bc69cafc1a96b3c65f897ac9c7f0708af4b5236be2e6f8dc10365e'}, {'machine': '2c4d9c61b106adc060e1ff36741d337d05d0ff0b7948f45b6feb3fc15b640061', 'process': 'fe1e58234c114c961c58b3d5054485461af59fb380b16fef2e1564457e062ee6', 'filament': '8d42f144eeb336556726ab4f3e38277154e9c0a646a69903a6d5cdcbfdcea19b'}, {'machine': '4e621b73e0972a9edf601ccc0d33dde179ba7539fd5d19cbe87e2ac8499b2354', 'process': 'a6334272bec36d79b6beabbe0677c999b73d27eda8966d428927d0fb171fd5d4', 'filament': '621da411d2bc69cafc1a96b3c65f897ac9c7f0708af4b5236be2e6f8dc10365e'}, {'machine': '2c4d9c61b106adc060e1ff36741d337d05d0ff0b7948f45b6feb3fc15b640061', 'process': 'fba3ff40021eb2fcf67587e2ec5611912e66e38431badf1349979049ab290356', 'filament': '8d42f144eeb336556726ab4f3e38277154e9c0a646a69903a6d5cdcbfdcea19b'}, {'machine': '4e621b73e0972a9edf601ccc0d33dde179ba7539fd5d19cbe87e2ac8499b2354', 'process': 'a6334272bec36d79b6beabbe0677c999b73d27eda8966d428927d0fb171fd5d4', 'filament': '621da411d2bc69cafc1a96b3c65f897ac9c7f0708af4b5236be2e6f8dc10365e'}, {'machine': '2c4d9c61b106adc060e1ff36741d337d05d0ff0b7948f45b6feb3fc15b640061', 'process': 'fba3ff40021eb2fcf67587e2ec5611912e66e38431badf1349979049ab290356', 'filament': '8d42f144eeb336556726ab4f3e38277154e9c0a646a69903a6d5cdcbfdcea19b'}),
        bed=(220, 220), height=245, exclusion=(), marketed=(220, 220),
        nozzle_type='brass', hotend=300, bed_temp=110,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA'), enclosed=False,
    ),
    "ender3v3plus": _model(
        printer_model='Creality Ender-3 V3 Plus',
        names={"machine": 'Creality Ender-3 V3 Plus 0.4 nozzle', "process": '0.20mm Standard @Creality Ender-3 V3 Plus', "filament": 'Generic PLA @Ender-3 V3 Plus-all'},
        hashes=({'machine': '4b51e869eb1227e82b89d65bf63e416ec3734e3ac4eac8375869d9297fdaca12', 'process': 'b9cd8d36a3becd28030a316f2246227012ff49ad7a0dca6f2d881411b6597c69', 'filament': 'd74512d9eb11cba13d1fd106a0b4466f24919a2ef4695059effab118f765372f'}, {'machine': '16d9325f8ac5f12f2c914788c38cf2d5a69d1e8d028efe4c6e591103c1a8df4a', 'process': '370f8950944970160d73f65ca8d917db8734556d6aacf9d2b07d15c3e0d5fa14', 'filament': 'd2e5851488818f367f2ea9ce42e98d92e19458f43ee62e1ded61bd1d9cf38931'}, {'machine': '17f9fc7dd57a42c8a88d359b77983d6e9931c66c400234b4bb92299075f893d5', 'process': '2ee3ef712ff7e2b64481f503b650cc5cf3bb8f8b454776867867fc4a7d67a3b8', 'filament': 'fdbf37acae4479cc472af03e051338e6c8f6d45e99705c5b569a46c162d31298'}, {'machine': '16d9325f8ac5f12f2c914788c38cf2d5a69d1e8d028efe4c6e591103c1a8df4a', 'process': '370f8950944970160d73f65ca8d917db8734556d6aacf9d2b07d15c3e0d5fa14', 'filament': 'd2e5851488818f367f2ea9ce42e98d92e19458f43ee62e1ded61bd1d9cf38931'}, {'machine': '17f9fc7dd57a42c8a88d359b77983d6e9931c66c400234b4bb92299075f893d5', 'process': '44b3ad5a4c3a1688e1073bee2f548fdbc863a77023cadf4434208e1f29e7a643', 'filament': 'fdbf37acae4479cc472af03e051338e6c8f6d45e99705c5b569a46c162d31298'}, {'machine': '16d9325f8ac5f12f2c914788c38cf2d5a69d1e8d028efe4c6e591103c1a8df4a', 'process': '370f8950944970160d73f65ca8d917db8734556d6aacf9d2b07d15c3e0d5fa14', 'filament': 'd2e5851488818f367f2ea9ce42e98d92e19458f43ee62e1ded61bd1d9cf38931'}, {'machine': '17f9fc7dd57a42c8a88d359b77983d6e9931c66c400234b4bb92299075f893d5', 'process': '44b3ad5a4c3a1688e1073bee2f548fdbc863a77023cadf4434208e1f29e7a643', 'filament': 'fdbf37acae4479cc472af03e051338e6c8f6d45e99705c5b569a46c162d31298'}),
        bed=(300, 300), height=330, exclusion=(), marketed=(300, 300),
        nozzle_type='hardened_steel', hotend=300, bed_temp=110,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA'), enclosed=False,
    ),
    "k1": _model(
        printer_model='Creality K1',
        names={"machine": 'Creality K1 (0.4 nozzle)', "process": '0.20mm Standard @Creality K1 (0.4 nozzle)', "filament": 'Generic PLA HF @Creality'},
        hashes=({'machine': '9069cc8406b19fe52691ee326077b40ef753657df5df2c594a900c1e1a9bf855', 'process': 'b23704fcfbfea2543c1f1506061808223aeb6c1c70376101a0bb0cb943121be0', 'filament': 'f06dbcd1f9a217cec4338e81e08c59fdee10abf572544d9f8c001582bd3dd8cf'}, {'machine': '7db94fa2cd80743e61275e7dcd3f4545c9c221bdc28d08d7c59a3ecc5a93a9cf', 'process': '012f1937e31fbd877b117bbcdb1a545c60893c3f62886e8093490f8c8d40afc9', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': 'a75ca74e9beb851813fb5ec3e275c57a61d96bc71d42af720eb75f249ad397cf', 'process': 'd63d0bf233d91d8742cdf6cb8fa900ee848dbe3dd356e5c468b8885d7f439b60', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}, {'machine': '7db94fa2cd80743e61275e7dcd3f4545c9c221bdc28d08d7c59a3ecc5a93a9cf', 'process': '012f1937e31fbd877b117bbcdb1a545c60893c3f62886e8093490f8c8d40afc9', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': 'a75ca74e9beb851813fb5ec3e275c57a61d96bc71d42af720eb75f249ad397cf', 'process': '4ac814b53afc086e5c9adbc83de101b3f88d9e092dc6de04e63f3db322084a26', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}, {'machine': '7db94fa2cd80743e61275e7dcd3f4545c9c221bdc28d08d7c59a3ecc5a93a9cf', 'process': '012f1937e31fbd877b117bbcdb1a545c60893c3f62886e8093490f8c8d40afc9', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': 'a75ca74e9beb851813fb5ec3e275c57a61d96bc71d42af720eb75f249ad397cf', 'process': '4ac814b53afc086e5c9adbc83de101b3f88d9e092dc6de04e63f3db322084a26', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}),
        bed=(220, 220), height=250, exclusion=(), marketed=(220, 220),
        nozzle_type='brass', hotend=300, bed_temp=100,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC'), enclosed=True,
    ),
    "k1c": _model(
        printer_model='Creality K1C',
        names={"machine": 'Creality K1C 0.4 nozzle', "process": '0.20mm Standard @Creality K1C', "filament": 'Generic PLA @Creality K1-all'},
        hashes=({'machine': 'b486c884bd8f1e116934b01de60d84614b594e0ddc31c5bdd436ba772fcbc341', 'process': '4c11751454538e5ec0e530697797e8b8b88628d9b1173996d3f71c0478ff061b', 'filament': '1fb6b95fd90569d8c4f2319edea12051156b1cfe8521c68b446dd6c35c341526'}, {'machine': '1778d0851b1a83e79006212c93f724e3b36d540326d993780ef0090e1977858c', 'process': '2c83324f83290644e397f7b7d92c50938353b9412d600412ae2291ffde672724', 'filament': 'fadefe76b262d9a6732d0cd5f2e86ae53d61dd94b730d290be89680e7a99e347'}, {'machine': '2417216a7bd3490c61787618229cc96afc3b3e971902a5294297cb4309df32c6', 'process': 'ee4022d209bd3ef166045d7711361ab756c818960795caf9b63a53626f038708', 'filament': 'fcc56f64c74303846b1953b29290c154d8f716cf786db4151e29e06ff5b152b5'}, {'machine': '1778d0851b1a83e79006212c93f724e3b36d540326d993780ef0090e1977858c', 'process': '2c83324f83290644e397f7b7d92c50938353b9412d600412ae2291ffde672724', 'filament': 'fadefe76b262d9a6732d0cd5f2e86ae53d61dd94b730d290be89680e7a99e347'}, {'machine': '2417216a7bd3490c61787618229cc96afc3b3e971902a5294297cb4309df32c6', 'process': '3df9fd8187ddd9c1801159b0867f9d966ab1dce01a18483127e93edbcdaad2b8', 'filament': 'fcc56f64c74303846b1953b29290c154d8f716cf786db4151e29e06ff5b152b5'}, {'machine': '1778d0851b1a83e79006212c93f724e3b36d540326d993780ef0090e1977858c', 'process': '2c83324f83290644e397f7b7d92c50938353b9412d600412ae2291ffde672724', 'filament': 'fadefe76b262d9a6732d0cd5f2e86ae53d61dd94b730d290be89680e7a99e347'}, {'machine': '2417216a7bd3490c61787618229cc96afc3b3e971902a5294297cb4309df32c6', 'process': '3df9fd8187ddd9c1801159b0867f9d966ab1dce01a18483127e93edbcdaad2b8', 'filament': 'fcc56f64c74303846b1953b29290c154d8f716cf786db4151e29e06ff5b152b5'}),
        bed=(220, 220), height=250, exclusion=(), marketed=(220, 220),
        nozzle_type='brass', hotend=300, bed_temp=100,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC'), enclosed=True,
    ),
    "k1max": _model(
        printer_model='Creality K1 Max',
        names={"machine": 'Creality K1 Max (0.4 nozzle)', "process": '0.20mm Standard @Creality K1Max (0.4 nozzle)', "filament": 'Generic PLA HF @Creality'},
        hashes=({'machine': '84899396f69e5049b79ca01824720dd5da8ff901c99a9b4f1192b9e851142c43', 'process': '476494307761b2f2305c66f3da4f41075768a14d0611f051324e9e9ae5b22129', 'filament': 'f06dbcd1f9a217cec4338e81e08c59fdee10abf572544d9f8c001582bd3dd8cf'}, {'machine': '9f9548946e0caa0204dbd1b193990a9199c0ba6e76010d55851d105663eebb19', 'process': '4f310c1ef7d4459a683d4273cc3a99b3fd8a8050475fcfa524e3e7ba77539e28', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': '65470fcbf14f349884508ac2c40642059d3dfbd97261b97e3f18a200806b3d4d', 'process': '74b15009840cbb131fb59ade3da2cf212c80434b5530816ee91c188def3383f2', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}, {'machine': '9f9548946e0caa0204dbd1b193990a9199c0ba6e76010d55851d105663eebb19', 'process': '4f310c1ef7d4459a683d4273cc3a99b3fd8a8050475fcfa524e3e7ba77539e28', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': '65470fcbf14f349884508ac2c40642059d3dfbd97261b97e3f18a200806b3d4d', 'process': '94db08a59c81a5014aeb1227512d94a84d5d9f1cb3555deb419d62a7edeb8df9', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}, {'machine': '70506206921d3feace7c217b228e8fd70d4188eba0dad1071c2b620a4c1290c1', 'process': '4f310c1ef7d4459a683d4273cc3a99b3fd8a8050475fcfa524e3e7ba77539e28', 'filament': 'eead1a81044b4f28c5390ea8fbd4375ba7e71186a536b3d058c05ba5303ce8b4'}, {'machine': '1523f4e8882217cfa9bb0ae0a44a419a8b6cff2189ce9b2d1a7adc285e6c5d77', 'process': '94db08a59c81a5014aeb1227512d94a84d5d9f1cb3555deb419d62a7edeb8df9', 'filament': 'fde4cd395a1db1d0047ed4918fab3f78ba459de50b3263faeade21c26e1e67ec'}),
        bed=(300, 300), height=300, exclusion=(), marketed=(300, 300),
        nozzle_type='brass', hotend=300, bed_temp=100,
        materials=('PLA', 'PETG', 'TPU', 'ABS', 'ASA', 'PC'), enclosed=True,
    ),
}


ENDER3V3 = MODELS["ender3v3"]
ENDER3V3KE = MODELS["ender3v3ke"]
ENDER3V3PLUS = MODELS["ender3v3plus"]
K1 = MODELS["k1"]
K1C = MODELS["k1c"]
K1MAX = MODELS["k1max"]
