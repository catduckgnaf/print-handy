"""Bambu Lab Orca profiles, generated on the operator's machine from their own OrcaSlicer installation.

Print Handy ships no OrcaSlicer or Bambu Lab profile content (those are AGPL-3.0). It ships only the profile
names, Print Handy's own overrides (facts.ORCA_OVERRIDES) and the SHA-256 identity of the vendor profiles it has
been checked against (the printer's `orca_identity` fact). `install` reads the vendor index and profiles from an
installed OrcaSlicer `resources` folder, resolves their `inherits`/`include` chains, refuses unless the result has
an expected identity, and writes two copies into the profile's state folder (never the repository):

    <state>/orca-profiles/<model templates>/upstream/{machine,process,filament}.json   flattened vendor profiles
    <state>/orca-profiles/<model templates>/{machine,process,filament}.json            the same plus overrides
    <state>/orca-profiles/<model templates>/[upstream/]filaments/<MATERIAL>.json        each other checked material

`problems` re-checks that folder before it is used: the upstream copies must still hash to an expected identity,
the Orca templates must be exactly those copies plus the overrides, and the profile must describe the printer
in facts.py. Anything missing or different fails closed; nothing is fetched or regenerated implicitly.

Material-aware filaments (#264): a model's `orca_filaments` fact maps a base polymer (PLA, PETG, ...) to the one
vendor filament profile checked for it, and each identity records those profiles' hashes under "filaments".
`filament_template` picks the profile for the hardware record's material and verifies it the same way; a material
with no checked profile is refused, never sliced with another material's profile. filament.json stays the
model's default profile (PLA), so a folder generated before #264 keeps slicing PLA until it is regenerated. A model
without `orca_filaments` has only that default.

OrcaSlicer 2.5+ packs each vendor's profiles into one binary preset cache (`profiles/<Vendor>.opc`) instead of
the JSON tree; `preset_cache.py` decodes it back into source dicts and the same resolution runs on them. The
cache is Orca's own build of the profiles (its option renames applied, options its config definitions do not
know dropped), so the packed packaging flattens differently from a source checkout of the same release and
carries its own recorded identities in facts.py, labelled "(packed cache)".

Operator filament presets (#264 phase 2): the operator may register one exported Bambu Studio / OrcaSlicer
filament preset per material (`import_filament`, via scripts/bambu-templates.py --import-filament). A preset is
partial — an `inherits` parent plus overrides — and is trusted only over a checked vendor parent: it is flattened
against the installed OrcaSlicer, may set only that parent's filament settings, must keep the parent's
filament_type and filament start/end G-code, and its values must stay inside the model's limits in facts.py.
The flattened preset, its template and a manifest entry (name, parent, SHA-256) live under the generated
folder's `operator/` subfolder, never the repository. While registered, a preset replaces the vendor profile
for its material; every use re-verifies it exactly like a generated template (hash, template text, material,
and its filament G-code still equalling the checked parent's), and regenerating the vendor templates keeps it.

    python3 scripts/bambu-templates.py --printer a1 --resources <OrcaSlicer resources folder>
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from . import facts, preset_cache

KINDS = ("machine", "process", "filament")
INDEX_LISTS = {"machine": "machine_list", "process": "process_list", "filament": "filament_list"}
UPSTREAM = "upstream"
FILAMENTS = "filaments"
VENDOR = "BBL"
OPERATOR = "operator"
OPERATOR_MANIFEST = "manifest.json"
# Bookkeeping keys an exported user preset may carry that are not settings; flattening drops them.
_PRESET_META = {"inherits", "include", "setting_id", "filament_settings_id"}
# Filament settings bounded at preset import (#264): nozzle and bed temperatures by the model's facts,
# fan speeds as percentages, retraction and volumetric speed positive.
_NOZZLE_TEMP_KEYS = ("nozzle_temperature", "nozzle_temperature_initial_layer")
_BED_TEMP_KEYS = tuple(f"{plate}_temp{suffix}" for plate in ("cool_plate", "eng_plate", "hot_plate", "textured_plate")
                       for suffix in ("", "_initial_layer"))
_FAN_KEYS = ("fan_min_speed", "fan_max_speed", "overhang_fan_speed")


class ProfileError(RuntimeError):
    """The printer's Orca profiles are missing, unreadable or not the ones Print Handy was checked against."""


def install_hint(adapter):
    return (f"generate them from your own OrcaSlicer installation: python3 scripts/bambu-templates.py "
            f"--printer {adapter.model.printer_key} --resources <OrcaSlicer resources folder> "
            "(see docs/BAMBU-LAB.md)")


def find_vendor(resources, vendor=VENDOR):
    """The vendor profile folder under a resources folder, or raise.

    Two layouts are served: the JSON tree (`<vendor>.json` and `<vendor>/`, OrcaSlicer up to
    2.4 and source checkouts) and the packed preset cache (`<vendor>.opc`, OrcaSlicer 2.5+).
    The JSON tree wins where both sit side by side.
    """
    root = Path(resources).expanduser()
    for profiles in (root / "profiles", root):
        if (profiles / f"{vendor}.json").is_file() and (profiles / vendor).is_dir():
            return profiles
        if (profiles / f"{vendor}.opc").is_file():
            return profiles
    raise ProfileError(f"no OrcaSlicer {vendor} vendor profiles under {root} "
                       f"(expected profiles/{vendor}.json and profiles/{vendor}/, "
                       f"or the packed profiles/{vendor}.opc)")


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from None


def flatten(resources, names, vendor=VENDOR):
    """{kind: profile} for the named vendor profiles, `inherits` and `include` resolved, as Orca loads them."""
    resolve, version = _resolver(resources, vendor)
    return {kind: resolve(names[kind]) for kind in KINDS}, version


def flatten_filaments(resources, names):
    """{name: profile} for the named vendor filament profiles, resolved like flatten."""
    resolve, _ = _resolver(resources)
    return {name: resolve(name) for name in names}


def _sources(resources, vendor=VENDOR):
    """(version, {name: source dict}) for the vendor under a resources folder, either layout.

    The JSON tree is read straight from its files; the packed cache is decoded whole by
    preset_cache (a corrupt or foreign cache is refused here, with its reason).
    """
    profiles = find_vendor(resources, vendor)
    if (profiles / f"{vendor}.json").is_file() and (profiles / vendor).is_dir():
        index = _read(profiles / f"{vendor}.json")
        paths = {entry["name"]: entry["sub_path"] for key in INDEX_LISTS.values()
                 for entry in index.get(key, [])}
        sources = {}

        def source(name):
            if name not in sources:
                sources[name] = _read(profiles / vendor / paths[name])
            return sources[name]

        return index.get("version"), set(paths), source
    try:
        cache = preset_cache.load(profiles / f"{vendor}.opc", vendor)
    except preset_cache.CacheError as exc:
        raise ProfileError(f"{profiles} holds packed vendor profiles ({vendor}.opc, OrcaSlicer 2.5+) "
                           f"that Print Handy cannot read: {exc}") from None
    return cache.version, set(cache.sources), cache.source


def _resolver(resources, vendor=VENDOR):
    version, names, source = _sources(resources, vendor)

    def resolve(name, seen=()):
        if name not in names:
            raise ProfileError(f"{vendor} vendor profile {name!r} is not in the {vendor} vendor profiles")
        if name in seen:
            raise ProfileError(f"{vendor} vendor profile {name!r} inherits itself")
        data = source(name)
        out = resolve(data["inherits"], (*seen, name)) if data.get("inherits") else {}
        for include in data.get("include", []):
            if include not in names:
                raise ProfileError(f"{vendor} vendor profile {include!r} is not in the {vendor} vendor profiles")
            out.update({k: v for k, v in source(include).items()
                        if k not in ("type", "name", "instantiation", "from")})
        out.update(data)
        out.pop("inherits", None)
        out.pop("include", None)
        return out

    return resolve, version


def _dump(data):
    head = [k for k in ("type", "name", "from", "instantiation") if k in data]
    return json.dumps({**{k: data[k] for k in head}, **{k: data[k] for k in sorted(data) if k not in head}},
                      indent=2, ensure_ascii=False) + "\n"


def sha256(data):
    return hashlib.sha256(_dump(data).encode()).hexdigest()


def template_text(kind, upstream):
    """The Orca template for one kind: the flattened vendor profile plus Print Handy's overrides and settings ID."""
    data = dict(upstream)
    data.update(facts.ORCA_OVERRIDES[kind])
    data[facts.SETTINGS_IDS[kind]] = [data["name"]] if kind == "filament" else data["name"]
    return _dump(data)


def identity(adapter, hashes, version=None):
    """The expected identity these flattened profiles match (and, when given, their vendor version), or None."""
    for known in adapter.fact("orca_identity"):
        if known["sha256"] == hashes and (version is None or known["vendor_version"] == version):
            return known
    return None


def _first(value):
    return value[0] if isinstance(value, list) and value else value


def material_key(material):
    """The base polymer a vendor filament profile is chosen by: PLA+, Matte PLA and PLA-CF -> PLA, PETG -> PETG.

    Reinforced fills keep their base polymer's profile, as their temperatures do (printer_state); the guard still
    requires a hardened steel nozzle for them.
    """
    from ...core.printer_state import material_family
    family = material_family(material)
    return {"FLEXIBLE": "TPU", "NYLON": "PA", "POLYAMIDE": "PA"}.get(family.split("-", 1)[0], family.split("-", 1)[0])


def filament_set(adapter):
    """{material: vendor filament profile name} checked for this printer; empty when it has only the default."""
    return dict(adapter.fact("orca_filaments")) if "orca_filaments" in adapter.facts else {}


def filament_hashes(adapter, known):
    """{profile name: sha256} of every filament profile an identity covers, the default included."""
    return {**known.get("filaments", {}), adapter.fact("orca_profiles")["filament"]: known["sha256"]["filament"]}


def filament_problems(adapter, material, data):
    """Why a flattened filament profile must not be the one for material on this printer; empty when it may."""
    out, name = [], data.get("name")
    kind = _first(data.get("filament_type"))
    if material_key(kind) != material:
        out.append(f"filament profile {name!r} is {kind!r}, not {material}")
    machine = adapter.fact("orca_profiles")["machine"]
    if data.get("compatible_printers") and machine not in data["compatible_printers"]:
        out.append(f"filament profile {name!r} does not list {machine!r} in compatible_printers")
    return out


def describe_problems(adapter, upstream):
    """Profile values that disagree with the printer's facts: a profile for another printer is refused."""
    machine, out = upstream["machine"], []
    if machine.get("printer_model") != adapter.fact("printer_model"):
        out.append(f"machine profile is for {machine.get('printer_model')!r}, not {adapter.fact('printer_model')!r}")
    (w, d), height = adapter.fact("bed_mm"), adapter.fact("max_print_height_mm")
    # Orca 2.4.2's X2D profile writes one bound with a trailing space ("0x256 ");
    # bounds compare whitespace-stripped, the recorded fact stays clean (#308).
    if [str(v).strip() for v in machine.get("printable_area") or []] != ["0x0", f"{w:g}x0", f"{w:g}x{d:g}", f"0x{d:g}"]:
        out.append(f"machine printable_area {machine.get('printable_area')} is not 0..{w:g} x 0..{d:g} mm")
    # The Orca machine profile may be shorter than the vendor volume (P1S: 250 of 256 mm); default to the volume.
    orca_height = adapter.facts["orca_profile_height_mm"].value if "orca_profile_height_mm" in adapter.facts else height
    try:
        height_ok = math.isclose(float(machine.get("printable_height")), float(orca_height), abs_tol=1e-6)
    except (TypeError, ValueError):
        height_ok = False
    if not height_ok:
        out.append(f"machine printable_height {machine.get('printable_height')} is not {orca_height:g} mm")
    exclusion = adapter.facts["bed_exclude_area"].value if "bed_exclude_area" in adapter.facts else ()
    expected_exclusion = [f"{x:g}x{y:g}" for x, y in exclusion]
    if machine.get("bed_exclude_area") not in (None, [], [""]) and machine.get("bed_exclude_area") != expected_exclusion:
        out.append(f"machine bed_exclude_area {machine.get('bed_exclude_area')} is not "
                   f"{expected_exclusion or 'empty'}")
    nozzle = machine.get("nozzle_diameter")
    # A dual-nozzle profile lists one diameter per extruder; the recorded fact is
    # the same stock size for each (#308, facts' "extruders" count).
    extruders = adapter.facts["extruders"].value if "extruders" in adapter.facts else 1
    if [str(v) for v in (nozzle if isinstance(nozzle, list) else [nozzle])] != \
            [f"{adapter.fact('stock_nozzle')[0]:g}"] * extruders:
        out.append(f"machine nozzle_diameter {nozzle} is not {adapter.fact('stock_nozzle')[0]:g} mm")
    return out


def check_resources(adapter, resources):
    """(upstream, {material: filament}, version, identity) from an installed OrcaSlicer, or ProfileError.

    Every profile must hash to one expected identity, describe the printer and, for filaments, its material.
    """
    upstream, version = flatten(resources, adapter.fact("orca_profiles"))
    hashes = {kind: sha256(upstream[kind]) for kind in KINDS}
    known = identity(adapter, hashes, version)
    if known is None:
        raise ProfileError(f"the {VENDOR} profiles in {resources} (vendor version {version}) are not a version "
                           f"Print Handy has checked for the {adapter.label}: sha256 {hashes}. Nothing was written")
    problems = describe_problems(adapter, upstream)
    names = filament_set(adapter)
    flat = flatten_filaments(resources, sorted(set(names.values()))) if names else {}
    expected = filament_hashes(adapter, known)
    for material, name in names.items():
        if sha256(flat[name]) != expected.get(name):
            problems.append(f"{material} filament profile {name!r} (sha256 {sha256(flat[name])}) is not the one "
                            f"checked for {known.get('label')}")
        problems += filament_problems(adapter, material, flat[name])
    if problems:
        raise ProfileError("; ".join(problems) + ". Nothing was written")
    default = adapter.fact("orca_profiles")["filament"]
    return upstream, {material: flat[name] for material, name in names.items() if name != default}, version, known


def install(adapter, resources, folder=None):
    """Generate the printer's Orca templates from an installed OrcaSlicer; return the identity it matched.

    Nothing is written unless the flattened vendor profiles match an expected identity and the printer's facts.
    """
    folder = Path(folder or adapter.pinned_templates_folder())
    upstream, filaments, version, known = check_resources(adapter, resources)
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{folder.name}-", dir=folder.parent))
    try:
        (work / UPSTREAM).mkdir()
        for kind in KINDS:
            (work / UPSTREAM / f"{kind}.json").write_text(_dump(upstream[kind]))
            (work / f"{kind}.json").write_text(template_text(kind, upstream[kind]))
        if filaments:
            (work / UPSTREAM / FILAMENTS).mkdir()
            (work / FILAMENTS).mkdir()
        for material, data in filaments.items():
            (work / UPSTREAM / FILAMENTS / f"{material}.json").write_text(_dump(data))
            (work / FILAMENTS / f"{material}.json").write_text(template_text("filament", data))
        (work / "SOURCE.json").write_text(json.dumps(
            {"resources": str(Path(resources).expanduser().resolve()), "vendor_version": version,
             "identity": known.get("label"), "sha256": {kind: sha256(upstream[kind]) for kind in KINDS},
             "filaments": {material: {"name": name, "sha256": filament_hashes(adapter, known)[name]}
                           for material, name in filament_set(adapter).items()}}, indent=2) + "\n")
        # Operator presets (#264 phase 2) are the operator's own state, not generated output: regenerating
        # the vendor templates keeps them. Each use re-verifies a preset against the regenerated parent.
        if (folder / OPERATOR).is_dir():
            shutil.copytree(folder / OPERATOR, work / OPERATOR)
        old = folder.with_name(f".{folder.name}-old")
        if folder.exists():
            shutil.rmtree(old, ignore_errors=True)
            os.replace(folder, old)
        os.replace(work, folder)
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return known


def problems(adapter, folder=None):
    """Why the generated templates in folder must not be used; empty when they are the checked profiles."""
    folder = Path(folder or adapter.pinned_templates_folder())
    if not all((folder / UPSTREAM / f"{kind}.json").is_file() for kind in KINDS):
        return [f"{adapter.label} Orca profiles are not installed in {folder}; {install_hint(adapter)}"]
    try:
        upstream = {kind: json.loads((folder / UPSTREAM / f"{kind}.json").read_text()) for kind in KINDS}
        templates = {kind: (folder / f"{kind}.json").read_text() for kind in KINDS}
    except (OSError, ValueError) as exc:
        return [f"{adapter.label} Orca profiles in {folder} are unreadable ({exc}); {install_hint(adapter)}"]
    hashes = {kind: sha256(upstream[kind]) for kind in KINDS}
    if identity(adapter, hashes) is None:
        return [f"{adapter.label} Orca profiles in {folder} are not a version Print Handy has checked "
                f"(sha256 {hashes}); {install_hint(adapter)}"]
    out = [f"{folder / (kind + '.json')} is not the generated template (edited?); {install_hint(adapter)}"
           for kind in KINDS if templates[kind] != template_text(kind, upstream[kind])]
    return out + describe_problems(adapter, upstream)


def require(adapter, folder=None):
    """The verified template folder, or ProfileError."""
    found = problems(adapter, folder)
    if found:
        raise ProfileError("; ".join(found))
    return Path(folder or adapter.pinned_templates_folder())


def _no_profile(adapter, material, checked):
    return ProfileError(f"{adapter.label}: no checked vendor filament profile for {material!r} "
                        f"(checked: {', '.join(checked)}); load a checked material or add one (docs/BAMBU-LAB.md). "
                        "Nothing was sliced")


def filament_template(adapter, material, folder=None):
    """The verified generated filament template for the hardware record's material, or ProfileError.

    The profile is the one the printer's `orca_filaments` fact names for the material's base polymer, and its files
    must still be the checked vendor profile plus overrides. A material with no checked profile is refused. An
    operator preset registered for the material (#264 phase 2) replaces the vendor profile, verified the same way.
    """
    folder = require(adapter, folder)
    operator = operator_template(adapter, material_key(material), folder)
    if operator is not None:
        return operator
    return _vendor_filament_template(adapter, material, folder)


def _vendor_filament_template(adapter, material, folder):
    """The checked vendor profile's template for a material, in an already-required generated folder."""
    key, names = material_key(material), filament_set(adapter)
    default = folder / "filament.json"
    if not names:
        kind = material_key(_first(json.loads(default.read_text()).get("filament_type")))
        if kind != key:
            raise _no_profile(adapter, material, [kind])
        return default
    if key not in names:
        raise _no_profile(adapter, material, names)
    name = names[key]
    if name == adapter.fact("orca_profiles")["filament"]:
        return default  # verified with the machine and process by require()
    upstream, path = folder / UPSTREAM / FILAMENTS / f"{key}.json", folder / FILAMENTS / f"{key}.json"
    if not (upstream.is_file() and path.is_file()):
        raise ProfileError(f"{adapter.label} {key} filament profile ({name}) is not installed in {folder}; "
                           f"{install_hint(adapter)}")
    try:
        data, text = json.loads(upstream.read_text()), path.read_text()
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{adapter.label} {key} filament profile in {folder} is unreadable ({exc}); "
                           f"{install_hint(adapter)}") from None
    known = identity(adapter, {kind: sha256(json.loads((folder / UPSTREAM / f"{kind}.json").read_text()))
                               for kind in KINDS})
    if data.get("name") != name or sha256(data) != filament_hashes(adapter, known).get(name):
        raise ProfileError(f"{upstream} is not the checked {name!r} profile; {install_hint(adapter)}")
    if text != template_text("filament", data):
        raise ProfileError(f"{path} is not the generated template (edited?); {install_hint(adapter)}")
    found = filament_problems(adapter, key, data)
    if found:
        raise ProfileError("; ".join(found))
    return path


def _stored_profile_material(adapter, settings_id, folder, names):
    """The material a settings id names in the installed folder, or False.

    A folder generated under a retained identity can carry that release's
    profile names — P1S templates from OrcaSlicer 2.4.2 name the PLA
    profile "Bambu PLA Basic @BBL X1C", where the current facts name the
    dedicated P1S profile (P2-128, P2-129). The folder is verified by
    require() before any of its names are trusted, so resolving against
    the stored names accepts exactly the checked folders, nothing else.
    """
    default = adapter.fact("orca_profiles")["filament"]
    try:
        require(adapter, folder)
        stored = {}
        for material, current in names.items():
            if material is None or current == default:
                stored[material] = json.loads((folder / "filament.json").read_text()).get("name")
            else:
                path = folder / UPSTREAM / FILAMENTS / f"{material}.json"
                if path.is_file():
                    stored[material] = json.loads(path.read_text()).get("name")
    except (ProfileError, OSError, ValueError):
        return False
    return next((m for m, name in stored.items() if name == settings_id), False)


def filament_for_settings_id(adapter, settings_id, folder=None):
    """The verified filament template whose profile name a sliced file's filament_settings_id names, or ProfileError."""
    folder = Path(folder or adapter.pinned_templates_folder())
    for material, entry in operator_presets(adapter, folder).items():
        if entry["name"] == settings_id:
            return operator_template(adapter, material, folder)
    names = filament_set(adapter) or {None: adapter.fact("orca_profiles")["filament"]}
    material = next((m for m, name in names.items() if name == settings_id), False)
    if material is False:
        material = _stored_profile_material(adapter, settings_id, folder, names)
    if material is False:
        raise ProfileError(f"filament_settings_id {settings_id!r} is not a checked {adapter.label} vendor filament "
                           f"profile ({', '.join(names.values())})")
    if material is None:
        return require(adapter, folder) / "filament.json"
    # A file that names the vendor profile is checked against that profile, even while an operator
    # preset replaces it for slicing: replacement governs selection by material, not lookup by name.
    return _vendor_filament_template(adapter, material, require(adapter, folder))


# --- Operator filament presets (#264 phase 2) ----------------------------------------------------
# One exported user preset per material, living in the generated folder's operator/ subfolder:
# operator/manifest.json records {material: {name, parent, sha256}} and is the trust anchor,
# operator/upstream/<MATERIAL>.json is the flattened preset it identifies, and
# operator/<MATERIAL>.json is that preset as a template (Print Handy's overrides applied).


def operator_presets(adapter, folder=None):
    """{material: {name, parent, sha256}} the operator has registered for this printer; {} when none.

    A manifest that exists but cannot be read or is malformed refuses: Print Handy cannot tell
    which presets it is being asked to trust.
    """
    folder = Path(folder or adapter.pinned_templates_folder())
    path = folder / OPERATOR / OPERATOR_MANIFEST
    if not path.is_file():
        return {}
    try:
        presets = json.loads(path.read_text())["presets"]
        if not isinstance(presets, dict):
            raise ValueError("presets is not an object")
        out = {}
        for material, entry in presets.items():
            if not isinstance(entry, dict) or not all(
                    isinstance(entry.get(k), str) and entry[k] for k in ("name", "parent", "sha256")):
                raise ValueError(f"the {material} entry is malformed")
            out[material] = {k: entry[k] for k in ("name", "parent", "sha256")}
        return out
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProfileError(f"{adapter.label} operator filament presets in {path} cannot be verified ({exc}); "
                           f"re-import them or remove the {OPERATOR} folder. Nothing was sliced") from None


def _write_operator_manifest(folder, presets):
    path = Path(folder) / OPERATOR / OPERATOR_MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"presets": presets}, indent=2) + "\n")


def _checked_parent_flat(adapter, folder, entry, material):
    """The flattened checked vendor profile an operator preset was imported against, from the generated folder.

    The preset stays trusted only while its parent is still the checked profile for its material.
    """
    names = filament_set(adapter)
    default = adapter.fact("orca_profiles")["filament"]
    if names:
        if names.get(material) != entry["parent"]:
            raise ProfileError(f"operator preset {entry['name']!r} was imported over {entry['parent']!r}, which is "
                               f"no longer the checked {adapter.label} profile for {material}; re-import it")
        path = (folder / UPSTREAM / "filament.json" if entry["parent"] == default
                else folder / UPSTREAM / FILAMENTS / f"{material}.json")
    else:
        if entry["parent"] != default:
            raise ProfileError(f"operator preset {entry['name']!r} was imported over {entry['parent']!r}, which is "
                               f"not the checked {adapter.label} profile; re-import it")
        path = folder / UPSTREAM / "filament.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{adapter.label} checked profile for {material} in {path} is unreadable ({exc}); "
                           f"{install_hint(adapter)}") from None


def operator_template(adapter, material, folder=None):
    """The verified operator preset template for a material (base polymer), or None when none is registered.

    The preset is trusted only while its flattened copy still hashes to the manifest entry recorded at
    import, its template is exactly that copy plus the overrides, it still describes this printer and
    material, and its filament G-code still equals its checked parent's.
    """
    folder = Path(folder or adapter.pinned_templates_folder())
    entry = operator_presets(adapter, folder).get(material)
    if entry is None:
        return None
    base = folder / OPERATOR
    upstream_path, path = base / UPSTREAM / f"{material}.json", base / f"{material}.json"
    hint = f"re-import it (python3 scripts/bambu-templates.py --printer {adapter.model.printer_key} " \
           f"--import-filament <preset>) or remove it (--remove-filament {material})"
    if not (upstream_path.is_file() and path.is_file()):
        raise ProfileError(f"{adapter.label} operator preset {entry['name']!r} for {material} is registered in "
                           f"{base} but its files are missing; {hint}")
    try:
        data, text = json.loads(upstream_path.read_text()), path.read_text()
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{adapter.label} operator preset for {material} in {base} is unreadable ({exc}); "
                           f"{hint}") from None
    if data.get("name") != entry["name"] or sha256(data) != entry["sha256"]:
        raise ProfileError(f"{upstream_path} is not the operator preset {entry['name']!r} recorded at import "
                           f"(edited?); {hint}")
    if text != template_text("filament", data):
        raise ProfileError(f"{path} is not the imported operator preset template (edited?); {hint}")
    found = filament_problems(adapter, material, data)
    if found:
        raise ProfileError("; ".join(found))
    parent = _checked_parent_flat(adapter, folder, entry, material)
    if (_first(data.get("filament_start_gcode")) != _first(parent.get("filament_start_gcode"))
            or _first(data.get("filament_end_gcode")) != _first(parent.get("filament_end_gcode"))):
        raise ProfileError(f"operator preset {entry['name']!r} filament G-code no longer matches its checked "
                           f"parent {entry['parent']!r}; {hint}")
    return path


def _preset_numbers(data, key):
    """Every value of a numeric filament setting as floats; None when a value is not a number, [] when unset.

    Orca's "nil" is the unset sentinel for a nullable setting (a preset
    that leaves retraction at the printer default flattens to ["nil"]), so
    it counts as unset, not as a malformed number (P2-130).
    """
    if key not in data:
        return []
    values = data[key] if isinstance(data[key], list) else [data[key]]
    out = []
    for value in values:
        if value == "nil":
            continue
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return None
    return out


def _preset_bound_problems(adapter, flat):
    """Imported preset values outside the model's recorded limits (facts.py); empty when all are inside."""
    out = []
    for key in _NOZZLE_TEMP_KEYS:
        values = _preset_numbers(flat, key)
        if values is None:
            out.append(f"{key} is not numeric")
        elif values and (min(values) < 0 or max(values) > adapter.fact("max_hotend_c")):
            out.append(f"{key} reaches {max(values):g} C, outside 0..{adapter.fact('max_hotend_c')} C "
                       f"for the {adapter.label}")
    for key in _BED_TEMP_KEYS:
        values = _preset_numbers(flat, key)
        if values is None:
            out.append(f"{key} is not numeric")
        elif values and (min(values) < 0 or max(values) > adapter.fact("max_bed_c")):
            out.append(f"{key} reaches {max(values):g} C, outside 0..{adapter.fact('max_bed_c')} C "
                       f"for the {adapter.label}")
    for key in _FAN_KEYS:
        values = _preset_numbers(flat, key)
        if values is None:
            out.append(f"{key} is not numeric")
        elif values and (min(values) < 0 or max(values) > 100):
            out.append(f"{key} reaches {max(values):g}%, outside 0..100%")
    for key in ("filament_retraction_length", "filament_max_volumetric_speed"):
        values = _preset_numbers(flat, key)
        if values is None:
            out.append(f"{key} is not numeric")
        elif values and min(values) <= 0:
            out.append(f"{key} must be positive, got {min(values):g}")
    return out


def import_filament(adapter, resources, preset_file, material=None, folder=None):
    """Register an exported Bambu Studio / OrcaSlicer filament preset for its material; return its manifest entry.

    The preset is flattened against the installed OrcaSlicer (check_resources rules: only a checked
    OrcaSlicer qualifies). It is refused, and nothing is written, unless its parent is one of the
    printer's checked filament profiles, it sets only that parent's filament settings, it keeps the
    parent's filament_type and filament start/end G-code, its material is not one Bambu lists as not
    recommended for the model, and its values stay inside the model's limits.
    """
    folder = Path(folder or adapter.pinned_templates_folder())
    try:
        preset = json.loads(Path(preset_file).read_text())
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read filament preset {preset_file}: {exc}") from None
    if not isinstance(preset, dict):
        raise ProfileError(f"{preset_file} is not a filament preset (expected a JSON object of settings)")
    upstream, filaments, _version, _known = check_resources(adapter, resources)
    default = adapter.fact("orca_profiles")["filament"]
    parents = {default: upstream["filament"]}
    for mat, name in filament_set(adapter).items():
        if name != default:
            parents[name] = filaments[mat]
    name, parent_name = preset.get("name"), preset.get("inherits")
    structural = []
    if not isinstance(name, str) or not name.strip():
        structural.append(f"{preset_file} names no preset (a 'name' is required)")
    if "type" in preset and preset["type"] != "filament":
        structural.append(f"{preset_file} is a {preset['type']!r} preset, not a filament preset")
    if not isinstance(parent_name, str) or parent_name not in parents:
        structural.append(f"the preset inherits {parent_name!r}, which is not a checked {adapter.label} filament "
                          f"profile ({', '.join(sorted(parents))}); a preset is trusted only over a checked "
                          "vendor parent")
    if structural:
        raise ProfileError("; ".join(structural) + ". Nothing was written")
    parent = parents[parent_name]
    key = material_key(_first(parent.get("filament_type")))
    problems = []
    if material is not None and material_key(material) != key:
        problems.append(f"--material {material} does not match the parent profile {parent_name!r}, "
                        f"which is a {key} profile")
    unknown = sorted(k for k in preset if k not in _PRESET_META and k not in parent)
    if unknown:
        problems.append(f"the preset sets {', '.join(unknown)}, which are not filament settings of "
                        f"{parent_name!r}; machine and process settings cannot be imported")
    flat = dict(parent)
    for k, value in preset.items():
        if k not in _PRESET_META:
            flat[k] = value
    flat.pop("inherits", None)
    flat.pop("include", None)
    if _first(flat.get("filament_type")) != _first(parent.get("filament_type")):
        problems.append(f"the preset's filament_type {_first(flat.get('filament_type'))!r} is not its parent's "
                        f"{_first(parent.get('filament_type'))!r}; a preset keeps its parent's material")
    for bad in {material_key(_first(flat.get("filament_type"))), key} & set(adapter.fact("not_recommended_materials")):
        problems.append(f"{bad} is not recommended by Bambu Lab for the {adapter.label}; "
                        "a preset for it is refused")
    if (_first(flat.get("filament_start_gcode")) != _first(parent.get("filament_start_gcode"))
            or _first(flat.get("filament_end_gcode")) != _first(parent.get("filament_end_gcode"))):
        problems.append("the preset changes the filament start or end G-code; those stay the checked "
                        "parent's, unchanged")
    problems += filament_problems(adapter, key, flat)
    problems += _preset_bound_problems(adapter, flat)
    if name in parents:
        problems.append(f"the preset name {name!r} is a checked vendor profile name; rename the preset")
    existing = operator_presets(adapter, folder)
    for other, entry in existing.items():
        if entry["name"] == name and other != key:
            problems.append(f"an operator preset named {name!r} is already registered for {other}")
    if problems:
        raise ProfileError("; ".join(problems) + ". Nothing was written")
    entry = {"name": name, "parent": parent_name, "sha256": sha256(flat)}
    base = folder / OPERATOR
    (base / UPSTREAM).mkdir(parents=True, exist_ok=True)
    (base / UPSTREAM / f"{key}.json").write_text(_dump(flat))
    (base / f"{key}.json").write_text(template_text("filament", flat))
    existing[key] = entry
    _write_operator_manifest(folder, existing)
    return {**entry, "material": key}


def remove_filament(adapter, material, folder=None):
    """Remove the operator preset registered for a material (base polymer); return its manifest entry."""
    folder = Path(folder or adapter.pinned_templates_folder())
    key = material_key(material)
    presets = operator_presets(adapter, folder)
    entry = presets.pop(key, None)
    if entry is None:
        raise ProfileError(f"no operator filament preset is registered for {key} on the {adapter.label}")
    base = folder / OPERATOR
    for path in (base / f"{key}.json", base / UPSTREAM / f"{key}.json"):
        path.unlink(missing_ok=True)
    _write_operator_manifest(folder, presets)
    return {**entry, "material": key}
