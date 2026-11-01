"""Load, validate and search the root-level registries (issue #169).

The data lives at the repository root so both the bot and a person browsing the
repo can find it without knowing the package layout:

    knowledge/symptoms.toml         what users report, and the flow that works it
    knowledge/troubleshooting.toml  the guided flows (see flows.py)
    firmware/commands/              one file per firmware family (zmod.toml,
                                    klipper.toml, marlin.toml, prusa.toml):
                                    commands, macros and errors for that family
    printers/registry/<vendor>.toml  one entry per registered printer model,
                                    one file per vendor package

This module is read-only. It opens no socket, imports no printer adapter and can
never turn a lookup into a printer action; a lookup returns registry text, and
physical actions stay behind the existing guarded print-control flows.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..core import fuzzy
from .flows import Flow, Node, Option

ROOT = Path(__file__).resolve().parents[2]
FIRMWARES = ("zmod", "klipper", "marlin", "prusa")
# Mirrors print_handy.printers.base.COLOR_SYSTEMS (kept local so this read-only
# loader imports no printer code); the registry drift test asserts the two agree.
COLOR_SYSTEMS = ("single", "multi_slot", "multi_tool")
# Thermal capabilities (issue #720): structured per-model facts, never
# inferred from "enclosed". Unknown is a first-class, explicit value.
ENCLOSURES = ("enclosed", "open", "unknown")
THERMAL_PROVENANCES = ("vendor documentation", "verified profile", "operator-confirmed", "unknown")
PRESENCE_STATES = ("present", "absent", "unknown")
KNOWABLE_STATES = ("yes", "no", "unknown")
CHAMBER_HEATER_CONTROLS = ("supported", "not_supported", "unknown")
ENTRY_KINDS = ("command", "macro", "error")
FLOW_KINDS = ("question", "advice")


class RegistryError(ValueError):
    """A registry file is missing, malformed, or breaks the schema rules."""


@dataclass(frozen=True)
class FirmwareEntry:
    id: str
    name: str
    aliases: tuple[str, ...]
    firmware: str
    kind: str
    printers: tuple[str, ...]
    command: str
    description: str
    safety: str
    notes: str = ""
    # Optional macro pack that rides on the firmware (issue #644); "" is the
    # firmware's stock set. No shipped entry carries a pack yet.
    pack: str = ""


@dataclass(frozen=True)
class Symptom:
    id: str
    title: str
    aliases: tuple[str, ...]
    flow: str
    firmware_entries: tuple[str, ...]
    summary: str
    first_checks: tuple[str, ...]


@dataclass(frozen=True)
class ThermalCapabilities:
    """Structured thermal facts for one model (issue #720).

    Every field is explicit in the registry TOML. A fact is only recorded
    as known when the repository already carries support for it (a vendor
    facts module, a registry note, or a profile/template pin); otherwise
    it stays "unknown". In particular, chamber heater control is only
    "supported" when ``chamber_heater_evidence`` names a verified firmware
    command or macro, and nothing here changes any slicing, fan,
    temperature or chamber behavior.
    """

    enclosure: str = "unknown"
    enclosure_provenance: str = "unknown"
    door_lid_state_knowable: str = "unknown"
    chamber_heater_present: str = "unknown"
    chamber_heater_control: str = "unknown"
    chamber_heater_evidence: str = ""
    chamber_provenance: str = "unknown"
    chamber_sensor_present: str = "unknown"
    chamber_sensor_fresh_reading: str = "unknown"
    fan_part_cooling_present: str = "unknown"
    fan_part_cooling_mapping: str = "unknown"
    fan_auxiliary_present: str = "unknown"
    fan_auxiliary_mapping: str = "unknown"
    fan_exhaust_present: str = "unknown"
    fan_exhaust_mapping: str = "unknown"
    fan_filtration_present: str = "unknown"
    fan_filtration_mapping: str = "unknown"
    fan_provenance: str = "unknown"


@dataclass(frozen=True)
class PrinterEntry:
    key: str
    label: str
    vendor: str
    firmware: str
    firmware_family: str
    connection: str
    kinematics: str
    bed_mm: tuple[float, float]
    max_height_mm: float
    filament_slots: int
    profile: str
    notes: str = ""
    # Independent toolheads (a tool changer) when the model has them; 0 means the
    # model's filament positions are its filament_slots (or a single extruder).
    tools: int = 0
    # The model's color architecture (printers.base.COLOR_SYSTEMS, issue #656):
    # single, multi_slot or multi_tool. The drift test pins it to MODEL.color_system.
    color_system: str = "single"
    # Structured thermal facts (issue #720); the loader requires an explicit
    # [printer.thermal] table for every registered model.
    thermal: ThermalCapabilities = field(default_factory=ThermalCapabilities)


@dataclass(frozen=True)
class Match:
    """One lookup hit. kind is firmware, symptom, flow or printer."""

    kind: str
    score: int
    item: object


def _norm(text):
    """The shared fold — ``core.fuzzy.norm`` is the one implementation.

    Kept as a local name because the registry's callers import it from
    here; routing it through core.fuzzy means the Unicode-honest fold
    (diacritics, CJK) applies to registry matching too, and the copies
    can never drift apart again.
    """
    return fuzzy.norm(text)


def _score(query, targets):
    """Best match score of a normalized query against an item's names/aliases/ids. 0 is no match.

    The tiers are exact (100), substring (85) and shared tokens (75 / scaled),
    plus the #468 stage-1 fuzzy tier: a typo'd query ("adheison", "nozzel
    clog") shares too few tokens with the right entry for the tiers above, so
    whole-string difflib similarity (``core.fuzzy``, floor 65, capped at 99)
    is the last chance before no match. Lookup is read-only — a fuzzy hit is
    a ranked candidate the user reads, never an action — and exact matches
    still score 100 and sort first.
    """
    q = _norm(query)
    if not q:
        return 0
    qtokens = set(q.split())
    best = 0
    for target in targets:
        t = _norm(target)
        if not t:
            continue
        if q == t:
            return 100
        ttokens = set(t.split())
        if q in t or t in q:
            best = max(best, 85 if len(t) >= 3 else best)
        shared = qtokens & ttokens
        if shared:
            if qtokens <= ttokens:
                best = max(best, 75)
            best = max(best, round(60 * len(shared) / max(len(qtokens), len(ttokens))))
        best = max(best, fuzzy.fuzzy_score(q, t))
    return best


def _load(path):
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        raise RegistryError(f"registry file missing: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"registry file {path.name} does not parse: {exc}") from None


def _load_printers(root):
    """Every raw printer entry, paired with the vendor file it came from.

    The printer registry is one ``<vendor>.toml`` per vendor package under
    ``printers/registry/``; the files are merged in sorted filename order into
    one logical registry. A missing or empty directory is an error, never a
    silently empty registry.
    """
    registry_dir = root / "printers" / "registry"
    files = sorted(registry_dir.glob("*.toml")) if registry_dir.is_dir() else []
    if not files:
        raise RegistryError(f"printer registry missing or empty: {registry_dir}")
    raws = []
    for path in files:
        for raw in _load(path).get("printer", []):
            raws.append((raw, path.stem))
    return raws


def _strings(value):
    return tuple(str(v).strip() for v in (value or ()) if str(v).strip())


@dataclass
class Registry:
    firmware_entries: tuple[FirmwareEntry, ...] = ()
    symptoms: tuple[Symptom, ...] = ()
    flows: tuple[Flow, ...] = ()
    printers: tuple[PrinterEntry, ...] = ()
    _by_id: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._by_id = {}
        for entry in self.firmware_entries:
            self._by_id[entry.id] = ("firmware", entry)
        for symptom in self.symptoms:
            self._by_id[symptom.id] = ("symptom", symptom)
        for flow in self.flows:
            self._by_id[flow.id] = ("flow", flow)
        for printer in self.printers:
            self._by_id[printer.key] = ("printer", printer)

    # ------------------------------------------------------------------ lookups

    def flow(self, flow_id):
        hit = self._by_id.get(str(flow_id or "").strip())
        return hit[1] if hit and hit[0] == "flow" else None

    def lookup(self, query, *, firmware=None, printer=None, pack=None, limit=5):
        """Ranked matches across all registries.

        `firmware` restricts firmware entries to one family; `printer` keeps only
        firmware entries known to apply to that printer model (plus "*" entries);
        `pack` keeps only firmware entries belonging to that macro pack (entries
        without a pack are the firmware's stock set, so pack="" keeps only those).
        Symptoms, flows and printers are never filtered out: a symptom report
        should find its flow whatever the machine is.
        """
        firmware = _norm(firmware).replace(" ", "") or None
        if firmware and firmware not in FIRMWARES:
            raise RegistryError(f"firmware must be one of {', '.join(FIRMWARES)}, got {firmware!r}")
        printer = str(printer or "").strip() or None
        pack = _norm(pack).replace(" ", "-") if pack is not None else None
        scored: list[Match] = []
        for entry in self.firmware_entries:
            if firmware and entry.firmware != firmware:
                continue
            if printer and "*" not in entry.printers and printer not in entry.printers:
                continue
            if pack is not None and entry.pack != pack:
                continue
            score = _score(query, (entry.name, entry.id, *entry.aliases))
            if score:
                scored.append(Match("firmware", score, entry))
        for symptom in self.symptoms:
            score = _score(query, (symptom.title, symptom.id, *symptom.aliases))
            if score:
                scored.append(Match("symptom", score, symptom))
        for flow in self.flows:
            score = _score(query, (flow.title, flow.id, *flow.aliases))
            if score:
                scored.append(Match("flow", score, flow))
        for entry in self.printers:
            score = _score(query, (entry.label, entry.key, entry.key.split(".", 1)[-1], entry.vendor))
            if score:
                scored.append(Match("printer", score, entry))
        order = {"firmware": 0, "symptom": 1, "flow": 2, "printer": 3}
        scored = [m for m in scored if m.score >= 30]
        scored.sort(key=lambda m: (-m.score, order[m.kind], getattr(m.item, "id", getattr(m.item, "key", ""))))
        return scored[:limit]


PACK_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def _firmware_entry(raw, file_firmware, problems):
    """Build one FirmwareEntry, appending schema problems found.

    `file_firmware` is the firmware the file the entry sits in is named for
    (firmware/commands/<firmware>.toml): an entry whose firmware field
    disagrees with its file is a problem, so the per-firmware partition the
    files claim is the partition the loader enforces (issue #644).
    """
    entry = FirmwareEntry(
        id=str(raw.get("id", "")).strip(), name=str(raw.get("name", "")).strip(),
        aliases=_strings(raw.get("aliases")), firmware=str(raw.get("firmware", "")).strip(),
        kind=str(raw.get("kind", "")).strip(), printers=_strings(raw.get("printers")),
        command=str(raw.get("command", "")).strip(), description=str(raw.get("description", "")).strip(),
        safety=str(raw.get("safety", "")).strip(), notes=str(raw.get("notes", "")).strip(),
        pack=str(raw.get("pack", "") or "").strip())
    where = f"firmware entry {entry.id or '?'}"
    if entry.firmware not in FIRMWARES:
        problems.append(f"{where}: firmware must be one of {FIRMWARES}")
    elif entry.firmware != file_firmware:
        problems.append(f"{where}: firmware {entry.firmware!r} disagrees with the file it sits in "
                        f"(firmware/commands/{file_firmware}.toml); move it to its own firmware's file")
    if entry.kind not in ENTRY_KINDS:
        problems.append(f"{where}: kind must be one of {ENTRY_KINDS}")
    if not entry.id.startswith(entry.firmware + "."):
        problems.append(f"{where}: id must start with '{entry.firmware}.'")
    if not entry.name or not entry.description or not entry.safety or not entry.printers:
        problems.append(f"{where}: name, description, safety and printers are required")
    if entry.kind != "error" and not entry.command:
        problems.append(f"{where}: a {entry.kind} entry needs its command line")
    if entry.pack and not PACK_SLUG.fullmatch(entry.pack):
        problems.append(f"{where}: pack must be a lowercase slug (letters, digits, hyphens), got {entry.pack!r}")
    return entry


def _load_firmware_entries(root, problems):
    """Load every firmware/commands/*.toml and merge, family files first.

    Files are read in FIRMWARES order (any further *.toml files after them,
    in sorted filename order), which reproduces the family sequence the
    single firmware/commands.toml used. That order is observable: the macro
    name index keeps the first entry that claims a name, and a bare
    START_PRINT must keep resolving to the ZMod entry it resolved to when
    the registry was one file (the #645 Creality/Sovol START_PRINT entries
    are reached through their printer scopes, not the unscoped name).

    The merged entries are the same logical registry the single
    firmware/commands.toml used to hold (issue #644); duplicate ids are
    rejected across files exactly as they were within the one file, by the
    caller's shared seen-ids pass.
    """
    commands_dir = root / "firmware" / "commands"
    if not commands_dir.is_dir():
        raise RegistryError(f"registry file missing: {commands_dir}")
    by_name = {path.stem: path for path in commands_dir.glob("*.toml")}
    paths = [by_name.pop(family) for family in FIRMWARES if family in by_name]
    paths += [by_name[name] for name in sorted(by_name)]
    if not paths:
        raise RegistryError(f"registry file missing: no *.toml files in {commands_dir}")
    entries: list[FirmwareEntry] = []
    for path in paths:
        doc = _load(path)
        for raw in doc.get("entry", []):
            entries.append(_firmware_entry(raw, path.stem, problems))
    return entries


def _thermal_capabilities(raw, where, problems):
    """Build one ThermalCapabilities, appending schema problems found.

    ``raw`` is the [printer.thermal] table (or None when it is missing,
    which is itself a problem: every model must carry an explicit record,
    with unknown spelled out, issue #720).
    """
    if not isinstance(raw, dict):
        problems.append(f"{where}: thermal record is required ([printer.thermal] table, issue #720)")
        return ThermalCapabilities()
    thermal = ThermalCapabilities(
        enclosure=str(raw.get("enclosure", "")).strip(),
        enclosure_provenance=str(raw.get("enclosure_provenance", "")).strip(),
        door_lid_state_knowable=str(raw.get("door_lid_state_knowable", "")).strip(),
        chamber_heater_present=str(raw.get("chamber_heater_present", "")).strip(),
        chamber_heater_control=str(raw.get("chamber_heater_control", "")).strip(),
        chamber_heater_evidence=str(raw.get("chamber_heater_evidence", "")).strip(),
        chamber_provenance=str(raw.get("chamber_provenance", "")).strip(),
        chamber_sensor_present=str(raw.get("chamber_sensor_present", "")).strip(),
        chamber_sensor_fresh_reading=str(raw.get("chamber_sensor_fresh_reading", "")).strip(),
        fan_part_cooling_present=str(raw.get("fan_part_cooling_present", "")).strip(),
        fan_part_cooling_mapping=str(raw.get("fan_part_cooling_mapping", "")).strip(),
        fan_auxiliary_present=str(raw.get("fan_auxiliary_present", "")).strip(),
        fan_auxiliary_mapping=str(raw.get("fan_auxiliary_mapping", "")).strip(),
        fan_exhaust_present=str(raw.get("fan_exhaust_present", "")).strip(),
        fan_exhaust_mapping=str(raw.get("fan_exhaust_mapping", "")).strip(),
        fan_filtration_present=str(raw.get("fan_filtration_present", "")).strip(),
        fan_filtration_mapping=str(raw.get("fan_filtration_mapping", "")).strip(),
        fan_provenance=str(raw.get("fan_provenance", "")).strip())
    if thermal.enclosure not in ENCLOSURES:
        problems.append(f"{where}: thermal.enclosure must be one of {ENCLOSURES}")
    for name in ("enclosure_provenance", "chamber_provenance", "fan_provenance"):
        if getattr(thermal, name) not in THERMAL_PROVENANCES:
            problems.append(f"{where}: thermal.{name} must be one of {THERMAL_PROVENANCES}")
    if thermal.door_lid_state_knowable not in KNOWABLE_STATES:
        problems.append(f"{where}: thermal.door_lid_state_knowable must be one of {KNOWABLE_STATES}")
    if thermal.chamber_sensor_fresh_reading not in KNOWABLE_STATES:
        problems.append(f"{where}: thermal.chamber_sensor_fresh_reading must be one of {KNOWABLE_STATES}")
    for name in ("chamber_heater_present", "chamber_sensor_present", "fan_part_cooling_present",
                 "fan_auxiliary_present", "fan_exhaust_present", "fan_filtration_present"):
        if getattr(thermal, name) not in PRESENCE_STATES:
            problems.append(f"{where}: thermal.{name} must be one of {PRESENCE_STATES}")
    for name in ("fan_part_cooling_mapping", "fan_auxiliary_mapping",
                 "fan_exhaust_mapping", "fan_filtration_mapping"):
        if not getattr(thermal, name):
            problems.append(f"{where}: thermal.{name} is required (a verified index or macro name, or 'unknown')")
    if thermal.chamber_heater_control not in CHAMBER_HEATER_CONTROLS:
        problems.append(f"{where}: thermal.chamber_heater_control must be one of {CHAMBER_HEATER_CONTROLS}")
    if thermal.chamber_heater_present == "absent" and thermal.chamber_heater_control != "not_supported":
        problems.append(f"{where}: thermal.chamber_heater_control must be 'not_supported' when no chamber heater is present")
    if thermal.chamber_heater_control == "supported":
        if thermal.chamber_heater_present != "present":
            problems.append(f"{where}: thermal.chamber_heater_control 'supported' requires chamber_heater_present 'present'")
        if not thermal.chamber_heater_evidence or thermal.chamber_heater_evidence == "unknown":
            problems.append(f"{where}: thermal.chamber_heater_control 'supported' requires "
                            "chamber_heater_evidence naming a verified firmware command or macro")
    return thermal


def load_registry(root=ROOT) -> Registry:
    """Load and validate every registry. Raises RegistryError listing every problem found."""
    root = Path(root)
    problems: list[str] = []

    firmware_entries = _load_firmware_entries(root, problems)
    symptoms_doc = _load(root / "knowledge" / "symptoms.toml")
    flows_doc = _load(root / "knowledge" / "troubleshooting.toml")
    printer_raws = _load_printers(root)

    entries: list[FirmwareEntry] = []
    seen_ids: set[str] = set()
    for entry in firmware_entries:
        where = f"firmware entry {entry.id or '?'}"
        if not entry.id or entry.id in seen_ids:
            problems.append(f"{where}: id missing or duplicated")
        seen_ids.add(entry.id)
        entries.append(entry)
    entry_ids = {e.id for e in entries}

    flows: list[Flow] = []
    for raw in flows_doc.get("flow", []):
        nodes: dict[str, Node] = {}
        for nraw in raw.get("node", []):
            options = tuple(Option(label=str(o.get("label", "")).strip(), next=str(o.get("next", "")).strip())
                            for o in nraw.get("option", []))
            node = Node(id=str(nraw.get("id", "")).strip(), kind=str(nraw.get("kind", "")).strip(),
                        prompt=str(nraw.get("prompt", "")).strip(), options=options,
                        next=(str(nraw["next"]).strip() if nraw.get("next") else None))
            if node.id in nodes:
                problems.append(f"flow {raw.get('id')}: node id {node.id!r} duplicated")
            nodes[node.id] = node
        flow = Flow(id=str(raw.get("id", "")).strip(), title=str(raw.get("title", "")).strip(),
                    aliases=_strings(raw.get("aliases")), safety=_strings(raw.get("safety")),
                    start=str(raw.get("start", "")).strip(), nodes=nodes)
        where = f"flow {flow.id or '?'}"
        if not flow.title or not flow.safety:
            problems.append(f"{where}: title and at least one safety warning are required")
        if flow.start not in nodes:
            problems.append(f"{where}: start node {flow.start!r} does not exist")
        for node in nodes.values():
            if node.kind not in FLOW_KINDS:
                problems.append(f"{where} node {node.id}: kind must be one of {FLOW_KINDS}")
            if not node.prompt:
                problems.append(f"{where} node {node.id}: prompt is required")
            if node.kind == "question":
                if len(node.options) < 2:
                    problems.append(f"{where} node {node.id}: a question needs at least two options")
                for option in node.options:
                    if not option.label or option.next not in nodes:
                        problems.append(f"{where} node {node.id}: option {option.label!r} points at unknown node {option.next!r}")
            if node.next is not None and node.next not in nodes:
                problems.append(f"{where} node {node.id}: next points at unknown node {node.next!r}")
            if node.kind == "advice" and node.options:
                problems.append(f"{where} node {node.id}: advice nodes do not take options")
        flows.append(flow)
    flow_ids = {f.id for f in flows}
    if len(flow_ids) != len(flows):
        problems.append("flow ids must be unique")

    symptoms: list[Symptom] = []
    for raw in symptoms_doc.get("symptom", []):
        symptom = Symptom(
            id=str(raw.get("id", "")).strip(), title=str(raw.get("title", "")).strip(),
            aliases=_strings(raw.get("aliases")), flow=str(raw.get("flow", "")).strip(),
            firmware_entries=_strings(raw.get("firmware_entries")),
            summary=str(raw.get("summary", "")).strip(), first_checks=_strings(raw.get("first_checks")))
        where = f"symptom {symptom.id or '?'}"
        if not symptom.id or not symptom.title or not symptom.summary:
            problems.append(f"{where}: id, title and summary are required")
        if symptom.flow and symptom.flow not in flow_ids:
            problems.append(f"{where}: flow {symptom.flow!r} does not exist")
        for entry_id in symptom.firmware_entries:
            if entry_id not in entry_ids:
                problems.append(f"{where}: firmware entry {entry_id!r} does not exist")
        symptoms.append(symptom)

    printers: list[PrinterEntry] = []
    for raw, vendor_file in printer_raws:
        bed = raw.get("bed_mm") or (0, 0)
        where = f"printer {str(raw.get('key', '')).strip() or '?'}"
        thermal = _thermal_capabilities(raw.get("thermal"), where, problems)
        printer = PrinterEntry(
            key=str(raw.get("key", "")).strip(), label=str(raw.get("label", "")).strip(),
            vendor=str(raw.get("vendor", "")).strip(), firmware=str(raw.get("firmware", "")).strip(),
            firmware_family=str(raw.get("firmware_family", "")).strip(),
            connection=str(raw.get("connection", "")).strip(),
            kinematics=str(raw.get("kinematics", "")).strip(),
            bed_mm=(float(bed[0]), float(bed[1])), max_height_mm=float(raw.get("max_height_mm") or 0),
            filament_slots=int(raw.get("filament_slots") or 0), tools=int(raw.get("tools") or 0),
            profile=str(raw.get("profile", "")).strip(), notes=str(raw.get("notes", "")).strip(),
            color_system=str(raw.get("color_system", "")).strip(), thermal=thermal)
        if printer.key.split(".", 1)[0] != vendor_file or printer.vendor != vendor_file:
            problems.append(f"{where}: filed in printers/registry/{vendor_file}.toml, but the "
                            f"key prefix and vendor field must both be '{vendor_file}'")
        if printer.firmware_family not in (*FIRMWARES, "none"):
            problems.append(f"{where}: firmware_family must be one of {FIRMWARES} or 'none'")
        if printer.color_system not in COLOR_SYSTEMS:
            problems.append(f"{where}: color_system must be one of {COLOR_SYSTEMS}")
        if printer.color_system == "multi_slot" and printer.filament_slots <= 1:
            problems.append(f"{where}: color_system 'multi_slot' requires filament_slots above 1")
        if printer.color_system != "multi_slot" and printer.filament_slots > 1:
            problems.append(f"{where}: filament_slots above 1 requires color_system 'multi_slot'")
        if printer.color_system == "multi_tool" and printer.tools <= 1:
            problems.append(f"{where}: color_system 'multi_tool' requires tools above 1")
        if printer.color_system != "multi_tool" and printer.tools > 1:
            problems.append(f"{where}: tools above 1 requires color_system 'multi_tool'")
        if not printer.key or not printer.profile:
            problems.append(f"{where}: key and profile are required")
        if printer.key in {p.key for p in printers}:
            problems.append(f"{where}: key duplicated")
        printers.append(printer)
    printer_keys = {p.key for p in printers}
    for entry in entries:
        for key in entry.printers:
            if key != "*" and key not in printer_keys:
                problems.append(f"firmware entry {entry.id}: printer {key!r} is not in the printer registry (printers/registry/)")

    if problems:
        raise RegistryError("invalid registries:\n- " + "\n- ".join(problems))
    return Registry(tuple(entries), tuple(symptoms), tuple(flows), tuple(printers))
