"""Vendor profile, printer model and adapter contract.

Layout: vendor first, then printer. One package per vendor under print_handy/printers/, one module per
printer model inside it. The [printer] model key is "<vendor>.<printer>", the package and module names.

    flashforge/__init__.py   VENDOR: Moonraker, ZMod START_PRINT start code, OrcaSlicer, .gcode
    flashforge/ad5x.py       MODEL for "flashforge.ad5x"; adapter.py (ZMod adapter); zmod/ (AD5X-only code)
    flashforge/ad5m.py, ad5m_pro.py  MODELs for the single-extruder ZMod printers (no IFS);
                             adapter.py (ZmodSingleExtruderAdapter: ZMod start code, IFS off)
    flashforge/creator5.py, creator5_pro.py  MODELs for the 4-toolhead ZMod printers;
                             adapter.py (ZmodMultiToolAdapter: tools, not IFS slots)
    klipper/__init__.py      VENDOR: Moonraker, PRINT_START/PRINT_END macros, OrcaSlicer, .gcode
    klipper/generic.py       MODEL for "klipper.generic"; adapter.py (the stock Klipper adapter)
    sovol/__init__.py        VENDOR: the Klipper vendor profile under the Sovol name
    sovol/sv06.py            MODEL for "sovol.sv06" on the stock Klipper adapter
    octoprint/__init__.py    VENDOR: OctoPrint REST, plain Marlin start code, OrcaSlicer, .gcode
    octoprint/marlin.py      MODEL for "octoprint.marlin"
    bambulab/__init__.py     VENDOR: opt-in read-only LAN MQTT telemetry, Bambu start code, OrcaSlicer or Bambu Studio,
                             .gcode.3mf handoff. Printer modules (A1, later P1S) drop in beside it.
    bambulab/a1.py, p1s.py   MODEL for "bambulab.a1" and "bambulab.p1s"; adapter.py (adapter with a bambulab_lan transport), guard.py
                             (A1 G-code checks), facts.py (sourced hardware facts, supplemental metadata),
                             profiles.py (templates generated from the operator's OrcaSlicer, none shipped)

A vendor package defines VENDOR = VendorProfile(...): what every printer of that vendor shares, namely
the connection type, the start G-code family, the slicers it may use, the file formats it hands over,
the delivery methods offered after a slice, and shared setting defaults (firmware, macros, layout
margins, prime tower, cold pull). A printer module defines MODEL = PrinterModel(..., vendor=VENDOR):
only what differs per printer (bed, height, printable-area exclusions, nozzle and material limits,
slots, AMS), its adapter class and its Orca template folder under templates/orca. The printer's
settings are merged over its vendor's settings, key by key, printer wins.

printers/__init__ finds every module with a MODEL, so adding a printer is one file plus templates, and
adding a vendor is one package with an __init__ defining VENDOR. Vendor code that is not a model lives
in helper modules without a MODEL or in a subpackage. See docs/PRINTER-PROFILES.md.

Settings merge, later wins key by key: vendor defaults, printer defaults, YAML home config, selected YAML/TOML local config, environment.
config/home.yaml picks the model with printer.model: flashforge.ad5x.

Color architecture (issue #656): every MODEL names color_system, one of "single", "multi_slot"
(one extruder fed by several IFS-style slots; the two AD5X profiles and, since #671, the
single-nozzle Bambu AMS models), or "multi_tool" (independent toolheads, each with its own
extruder and filament; the Creator pair and, since #670, the Prusa XL). Every other model is
"single" — including the dual-nozzle Bambu models (H2D/H2C family): two independent extruders,
each AMS-fed, are neither the one-extruder multi_slot shape nor the multi_tool tool-changer
contract, so they stay "single" with the reason recorded on their profiles (#671); their AMS
trays are resolved per filament slot at print start (bambulab/ams_mapping.py), outside the
class machinery.
The class is the authority: PrinterAdapter.multi_slot / multi_tool derive from it, the settings
validation for filament_slots and tool_count reads it, and a model whose settings contradict its
class (filament_slots above 1 outside multi_slot, tool_count above 1 outside multi_tool) is refused
when the profile loads.

Where printer specifics live, and which responsibilities sit behind PrinterAdapter:

  responsibility                 flashforge.ad5x (ZMod)                  klipper.generic / sovol.sv06
  -----------------------------  --------------------------------------  ----------------------------
  build volume, margins          model settings                          same keys, 1 slot
  slot count / multi-slot        [printer] filament_slots (IFS, 4)            1, single extruder
  Orca template folder           templates/orca/flashforge_ad5x          templates/orca/<model folder>
  Orca start G-code              START_PRINT ... MESH=<mesh> + M190/     PRINT_START BED= HOTEND=
                                 M104 preamble (adapter)                 (macro names from settings)
  start plan for a job           START_PRINT BED_TEMP EXTRUDER_TEMP      PRINT_START BED HOTEND
  tool -> slot mapping           zmod.multicolor_contract                T0 only; multicolor refused
  readiness check                model_job.check_filament_readiness      print_stats / heater sanity
  screen / slot sync             zmod.screen (CHANGE_ZCOLOR)             none (returns None)
  native status API              zmod.stock (/detail)                    none (returns None)
  telemetry normalization        Moonraker + zmod_color                  plain Moonraker objects

Core reaches ZMod code only through adapter.native (the printers.flashforge.zmod package): finalize,
embedded_mapping, normalize_mapping, describe_mapping, validate_materials, the stock /detail readers,
native_startup and native_idle. A printer without them raises MultiColorUnsupported from `native`.

Runtime decisions that follow the adapter (core/model_job, printer_state, orca_adapter, the listener):
  - multi_slot False (generic/sovol): no IFS selection or spool-slot validation, no multicolor finalization,
    no native_startup/native_idle, no mapped-native launch; slicing refuses multicolor and multi-plate sources
    with MultiColorUnsupported (a REFUSED line).
  - multi_tool True (flashforge.creator5, creator5_pro, prusa.xl): filament_slots stays 1, so every multi_slot gate above
    stays off; the tools live at the adapter contract instead — normalize_tool_mapping accepts only the
    identity tool -> toolhead map for T0..T{tool_count-1} and refuses anything else, readiness and status
    are per-toolhead where the connection publishes them (extruder..extruder3, zmod_color.active_tool_id
    on the Creator pair; PrusaLink publishes no per-toolhead telemetry, so the XL's stay printer-level —
    evidence on #670), and a job's G-code tool changes are
    the sliced file's own T lines. The colour-question seam in core is tool-shaped for these models (#346):
    prepared_intake, discord_color_intake and link_intake ask one question per colour with toolhead
    labels (model_job.build_tool_labels), a toolhead already assigned a different source colour is not
    offered again, more distinct colours than tool_count is refused, and model_job.position_limit
    bounds selections by the adapter's slot_count. Slicing honours the picks as the identity mapping:
    color_geometry exports in identity_tools mode, orca_adapter loads one filament profile per toolhead
    through the model's own multi-extruder templates (no SEMM flip, no native finalize), and
    model_job._slice_merged_multi_tool checks the recorded mapping against normalize_tool_mapping
    unchanged. Split (non-merged) plates stay refused: they are IFS-shaped.
  - has_native_screen False: no display_off/LOAD_ZOFFSET/Adventurer5M offset authority, no ZMod MD5, no stock
    /detail polling, no screen-cancel firmware restart, no mesh-profile readiness (the printer owns its mesh).
  - readiness (readiness_problems), the start request (moonraker_start_commands) and the multicolor refusal
    (normalize_tool_mapping) are adapter calls on every model; flashforge.ad5x delegates to the old functions.
  - JOB.json records `printer_model` when sliced; print-now refuses a job whose model differs from
    [printer] model. A job without the field is a flashforge.ad5x job.

Still AD5X-coupled code in shared modules, reached only when multi_slot / has_native_screen. It is mixed
with printer-neutral logic (Spoolman, Discord, plate handling), so it stays until the follow-up
refactor: the second multi-slot family now exists (the Bambu AMS models, #671), and that PR's seam
report records, module by module, which of these the Bambu classification reaches and which it
cannot reach (the Bambu print, slice and readiness paths dispatch by connection/transport first,
and its slots are printer-reported rather than record-bound — see PrinterAdapter.slots_bound_by_record):
  - core/model_job: IFS selection and filament readiness, mapped-native launch (print_mapped_native), the
    zmod_color / display_off telemetry read, thin delegators to zmod.stock
  - core/prepared_intake, core/plate_merge, core/color_geometry, core/source_colors: filament_slots bounds, the
    four-slot mapping and prime tower placement for IFS multicolor plates
  - core/orca_adapter: the START_PRINT/MESH start G-code goes through the adapter; FILAMENT_SLOTS guard bounds
    and the tool-mapped prime tower retry remain
  - core/spool_lifecycle, core/clear_job: IFS slot reconciliation, native idle cancel handling
  - hermes/spool_sync (Spoolman binding), moonraker_event_listener (IFS presence poll, runout pause),
    print_control (native /detail cancel), print_next and the Discord IFS prompts: "AD5X" wording
"""
from __future__ import annotations

import copy
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class MultiColorUnsupported(RuntimeError):
    """The selected printer model cannot run this multi-color job."""


@dataclass(frozen=True)
class BuildVolume:
    bed_mm: tuple[float, float]
    max_height_mm: float
    single_copy_edge_mm: float
    multi_copy_edge_mm: float


# How Print Handy reaches a printer. "offline" means it never does: it slices and validates, a person
# moves the file.
CONNECTIONS = ("moonraker", "octoprint", "offline", "bambulab_lan", "flashforge_stock", "prusalink", "elegoo_sdcp")
# The three color architectures (issue #656). Every model names exactly one; the class is the
# authority the adapter properties (multi_slot, multi_tool) and the settings validation read, so the
# architecture is never re-inferred from filament_slots or adapter flags:
#   single      one extruder, one filament position (the large majority of models)
#   multi_slot  one extruder fed by several filament slots (an IFS-style feeder; the AD5X pair
#               and the single-nozzle Bambu AMS models)
#   multi_tool  several independent toolheads, each with its own extruder and filament (the Creator pair)
COLOR_SYSTEMS = ("single", "multi_slot", "multi_tool")
# Start G-code families. Each printer runs only its own; see start_code_problems.
START_CODES = ("zmod", "klipper_macro", "marlin", "bambu", "flashforge_stock", "prusa", "elegoo_sdcp", "elegoo_inline",
               "qidi", "creality", "anycubic")
# Slicers a vendor may pick as its primary. OrcaSlicer is the default everywhere.
SLICERS = ("orca", "bambu_studio")
# What can happen to a validated slice, besides nothing:
#   printer        start it on the printer through the vendor connection, behind the per-job bed-clear button
#   discord        attach the sliced files to the Discord thread
#   bambu_handoff  package a .gcode.3mf in Bambu Studio's export layout; nothing is uploaded (untested on hardware)
DELIVERY_METHODS = ("printer", "discord", "bambu_handoff")


# Margins, prime tower and cold pull every current vendor shares. A vendor or printer overrides any key.
SHARED_SETTINGS = {
    # The part gaps and margins below (arrange_*, merge_*, prime_tower clearances) are
    # resolved from the layout.spacing preset (settings.SPACING_PRESETS) unless the
    # operator sets one explicitly; the values here are the "minimum" preset.
    "layout": {
        # Gap preset: minimum | low | medium | high (see docs/SETTINGS.md).
        "spacing": "minimum",
        # Distance from the bed edge for a lone copy and for multi copy plates.
        "single_copy_edge_mm": 1.0,
        "multi_copy_edge_mm": 3.0,
        # Part-to-part gap and bed-edge margin when parts are rearranged onto one plate.
        "arrange_gap_mm": 2.0,
        "arrange_edge_mm": 1.0,
        # Gaps when several source plates are merged onto one bed.
        "merge_edge_margin_mm": 1.0,
        "merge_spacing_mm": 2.0,
    },
    "prime_tower": {
        "size_mm": 45.0,
        # Clearance between the tower and any part.
        "part_clearance_mm": 3.5,
        # Closest the tower may sit to the bed edge.
        "edge_mm": 4.0,
        # Extra room reserved for the tower when merging plates.
        "merge_slack_mm": 1.0,
    },
    # Profile name = [hot C, cold C]
    "cold_pull": {"pla_trial": [220, 90], "petg": [245, 100]},
}


def merge_settings(base: dict, extra: dict) -> dict:
    """Deep copy of base with extra merged over it, key by key."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_settings(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass(frozen=True)
class VendorProfile:
    """What every printer of one vendor shares. Printer models inherit it and override settings."""

    key: str  # the vendor package name, the part of "vendor.printer" before the dot
    label: str
    connection: str  # one of CONNECTIONS
    start_code: str  # one of START_CODES
    # Allowed [printer] slicer values; the first is the default.
    slicers: tuple[str, ...] = ("orca",)
    # Tried once, isolated, after an OrcaSlicer execution failure. None disables the fallback.
    fallback_slicer: str | None = "bambu_studio"
    # File formats this vendor's printers take, primary first. Delivery hands over the primary format.
    file_formats: tuple[str, ...] = (".gcode",)
    # Delivery methods offered after a slice (DELIVERY_METHODS), and the ones on by default.
    # [delivery] methods in config picks a subset of `delivery`.
    delivery: tuple[str, ...] = ("printer", "discord")
    default_delivery: tuple[str, ...] = ("printer",)
    # Shared defaults for [printer], [layout], [prime_tower] and [cold_pull]; each printer model overrides.
    settings: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.connection not in CONNECTIONS:
            raise ValueError(f"vendor {self.key}: connection must be one of {CONNECTIONS}, got {self.connection!r}")
        if self.start_code not in START_CODES:
            raise ValueError(f"vendor {self.key}: start_code must be one of {START_CODES}, got {self.start_code!r}")
        if not self.slicers or any(s not in SLICERS for s in self.slicers):
            raise ValueError(f"vendor {self.key}: slicers must be from {SLICERS}, got {self.slicers!r}")
        unknown = [m for m in (*self.delivery, *self.default_delivery) if m not in DELIVERY_METHODS]
        if unknown:
            raise ValueError(f"vendor {self.key}: unknown delivery methods {unknown}")
        if not set(self.default_delivery) <= set(self.delivery):
            raise ValueError(f"vendor {self.key}: default_delivery must be a subset of delivery")
        if "printer" in self.delivery and self.connection == "offline":
            raise ValueError(f"vendor {self.key}: an offline vendor cannot offer printer delivery")
        if "bambu_handoff" in self.delivery and self.start_code != "bambu":
            raise ValueError(f"vendor {self.key}: bambu_handoff needs Bambu start code")

    def derive(self, **changes) -> "VendorProfile":
        """A vendor that shares this one's profile, e.g. a brand whose printers run stock Klipper."""
        from dataclasses import replace
        return replace(self, **changes)


@dataclass(frozen=True)
class PrinterModel:
    """One printer of one vendor: its own defaults, its adapter and its Orca template folder."""

    key: str  # "brand.model", the [printer] model value
    label: str
    adapter: type  # PrinterAdapter subclass
    templates: str  # folder under print_handy/templates/orca (or under <state>/orca-profiles when generated)
    # Printer-specific defaults for [printer], [layout], [prime_tower] and [cold_pull], merged over the
    # vendor's settings. Config files and the environment override both.
    settings: dict
    # Protected-settings baseline file under config/; None uses the shared protected-settings-baseline.json.
    baseline: str | None = None
    # Supported nozzle diameters in mm (e.g. 0.25, 0.4, 0.6, 0.8)
    supported_nozzle_sizes: tuple[float, ...] = (0.2, 0.25, 0.4, 0.6, 0.8)
    # Supported nozzle types (e.g. brass, hardened_steel, ruby, tungsten_carbide, diamond)
    supported_nozzle_types: tuple[str, ...] = (
        "brass",
        "hardened_steel",
        "stainless_steel",
        "ruby",
        "tungsten_carbide",
        "diamond",
    )
    # The vendor this printer inherits from. printers/__init__ fills it from the vendor package when a
    # model module leaves it out, and refuses a vendor that does not match the package.
    vendor: VendorProfile | None = None
    # True when the Orca templates are not shipped but generated on the operator's machine into
    # <state>/orca-profiles/<templates> (third-party profiles; see printers/bambulab/profiles.py).
    generated_templates: bool = False
    # The model's color architecture (COLOR_SYSTEMS). "single" unless the model's own
    # settings say otherwise: multi_slot needs filament_slots > 1, multi_tool needs tool_count > 1,
    # and no other class may carry either count (see __post_init__).
    color_system: str = "single"

    def __post_init__(self):
        if self.color_system not in COLOR_SYSTEMS:
            raise ValueError(f"model {self.key}: color_system must be one of {COLOR_SYSTEMS}, "
                             f"got {self.color_system!r}")
        merged = merge_settings(self.vendor.settings if self.vendor else {}, self.settings)
        printer = merged.get("printer") or {}
        filament_slots = int(printer.get("filament_slots", 1))
        tool_count = int(printer.get("tool_count", 1))
        if filament_slots > 1 and self.color_system != "multi_slot":
            raise ValueError(f"model {self.key}: filament_slots {filament_slots} > 1 requires "
                             f"color_system 'multi_slot', got {self.color_system!r}")
        if tool_count > 1 and self.color_system != "multi_tool":
            raise ValueError(f"model {self.key}: tool_count {tool_count} > 1 requires "
                             f"color_system 'multi_tool', got {self.color_system!r}")
        if self.color_system == "multi_slot" and filament_slots <= 1:
            raise ValueError(f"model {self.key}: color_system 'multi_slot' requires filament_slots > 1, "
                             f"got {filament_slots}")
        if self.color_system == "multi_tool" and tool_count <= 1:
            raise ValueError(f"model {self.key}: color_system 'multi_tool' requires tool_count > 1, "
                             f"got {tool_count}")

    @property
    def vendor_key(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def printer_key(self) -> str:
        return self.key.split(".", 1)[1]

    def defaults(self) -> dict[str, Any]:
        """Settings this model contributes: vendor settings, then the printer's, then identity keys."""
        out = merge_settings(self.vendor.settings if self.vendor else {}, self.settings)
        printer = out.setdefault("printer", {})
        printer["model"] = self.key
        # Empty means generated: settings resolves it to <state>/orca-profiles/<templates>.
        out.setdefault("orca", {})["templates"] = "" if self.generated_templates else f"print_handy/templates/orca/{self.templates}"
        if self.vendor is not None:
            printer["vendor"] = self.vendor.key
            printer["slicer"] = self.vendor.slicers[0]
            out["delivery"] = {"methods": list(self.vendor.default_delivery)}
        return out


class PrinterAdapter(ABC):
    """What call sites need from a printer model. Build with a settings dict (default: process settings)."""

    # ZMod-style native screen: display_off modes, saved/screen Z offset authority, MD5 gcode, stock /detail.
    has_native_screen: bool = False
    # True when this printer's filament slots are bound to spools in the hardware record's
    # slot map (the IFS shape: the spool lifecycle writes the map, and intake, slicing and
    # print-now validate selections against it). A multi_slot printer whose feeder reports
    # its own contents instead (Bambu AMS: trays decoded from the printer's status, matched
    # to a job's filament slots at print start) leaves this False: its slots are real slots
    # for counting and readiness, but Print Handy does not slice per record-bound slot for
    # it, and the record's single spool stays the slot-1 offer (#263). Only the AD5X
    # families (ZMod and stock Flashforge) bind slots in the record.
    slots_bound_by_record: bool = False
    # True when filament_template() selects a checked vendor profile per material (Bambu Lab). Every other
    # printer has one PLA template per model, so core.filament_families puts the job's material family on it.
    vendor_material_profiles: bool = False
    # (xmin, ymin, xmax, ymax) when vendor start/end G-code moves off the bed; None holds every move to the bed.
    motion_envelope_mm: tuple[float, float, float, float] | None = None
    # Slicers ([printer] slicer values) whose output this printer's guards have been checked against; None trusts
    # every slicer the vendor allows. A slicer outside it is refused as primary and as fallback.
    verified_slicers: tuple[str, ...] | None = None

    def __init__(self, config: dict[str, Any] | None = None, model: PrinterModel | None = None):
        if config is None:
            from .. import settings
            config = settings.current()
        if model is None:
            from . import model as _model
            model = _model(config["printer"]["model"])
        self.config = config
        self.model = model

    @property
    def color_system(self) -> str:
        """The model's color architecture (COLOR_SYSTEMS); the model is the only authority for it."""
        return self.model.color_system

    @property
    def multi_slot(self) -> bool:
        """One extruder fed by several filament slots (an IFS-style feeder), from the model's class."""
        return self.color_system == "multi_slot"

    @property
    def multi_tool(self) -> bool:
        """Independent toolheads (a tool changer), as opposed to one extruder fed by multi_slot
        IFS slots: tools are fixed positions (slicer tool i always prints with toolhead i),
        each with its own filament. [printer] tool_count names them; filament_slots stays 1."""
        return self.color_system == "multi_tool"

    @property
    def key(self) -> str:
        return self.model.key

    @property
    def label(self) -> str:
        return self.model.label

    @property
    def display_name(self) -> str:
        """Configured printer name for user-facing messages, with a profile label fallback."""
        configured = (self.config.get("printer") or {}).get("name")
        return str(configured or self.model.label or "the printer")

    @property
    def firmware_name(self) -> str:
        """Configured firmware name for user-facing messages, with a generic fallback."""
        configured = (self.config.get("printer") or {}).get("firmware")
        return str(configured or "the firmware")

    @property
    def connection(self) -> str | None:
        """The vendor's connection type (CONNECTIONS); the vendor profile is the only authority for it."""
        return self.model.vendor.connection if self.model.vendor else None

    @property
    def baseline_path(self) -> Path | None:
        """This model's protected-settings baseline, or None for the shared file."""
        if self.model.baseline is None:
            return None
        from .. import settings
        return settings.ROOT / "config" / self.model.baseline

    @property
    def native(self):
        """ZMod/native helpers (printers.flashforge.zmod); stock Klipper has none."""
        raise MultiColorUnsupported(f"{self.label} has no ZMod/native support")

    @property
    def native_or_none(self):
        """The native (ZMod) contract, or None when this adapter has none.

        Shared code that consults the native mapping only where an adapter offers
        it (a start record, a preparation message) uses this instead of keying on
        multi_slot alone: since #671 a multi_slot adapter may have no native
        contract at all (Bambu AMS), and the stock AD5X never had one either.
        """
        try:
            return self.native
        except MultiColorUnsupported:
            return None

    @property
    def supported_nozzle_sizes(self) -> tuple[float, ...]:
        return self.model.supported_nozzle_sizes

    @property
    def supported_nozzle_types(self) -> tuple[str, ...]:
        return self.model.supported_nozzle_types

    # geometry and slots
    def build_volume(self) -> BuildVolume:
        p, lay = self.config["printer"], self.config["layout"]
        return BuildVolume((float(p["bed_mm"][0]), float(p["bed_mm"][1])), float(p["max_print_height_mm"]),
                           float(lay["single_copy_edge_mm"]), float(lay["multi_copy_edge_mm"]))

    @property
    @abstractmethod
    def slot_count(self) -> int: ...

    # slicer
    def templates_dir(self) -> Path:
        """Folder with machine.json, process.json and filament.json for Orca (an [orca] templates override applies)."""
        from .. import settings
        if self.config["printer"].get("model") == self.key:
            return Path(self.config["orca"]["templates"])
        if self.model.generated_templates:
            return settings.generated_templates_dir(self.model, self.config)
        return settings.ROOT / "print_handy" / "templates" / "orca" / self.model.templates

    def filament_template(self, templates: Path, material: str) -> Path:
        """The filament template a job for material slices with; the template folder's filament.json by default."""
        return Path(templates) / "filament.json"

    @abstractmethod
    def machine_start_gcode(self, template_start: str, mesh: str = "auto") -> str:
        """Orca machine_start_gcode for this printer; template_start is the template's own text."""

    @abstractmethod
    def machine_end_gcode(self, template_end: str) -> str: ...

    @abstractmethod
    def start_plan(self, bed_temp: float, nozzle_temp: float, *, mesh: str = "auto") -> list[str]:
        """Resolved start G-code lines for one job (what the printer executes first)."""

    @abstractmethod
    def moonraker_start_commands(self, remote_filename: str) -> list[dict[str, Any]]:
        """Moonraker requests, in order, that start a print: [{'method', 'path', 'body'}]."""

    def gcode_problems(self, text: str, state: dict | None = None) -> list[str]:
        """Printer-specific reasons a sliced G-code must not be used, beyond start_code_problems; empty when none."""
        return []

    # color / tools
    @abstractmethod
    def normalize_tool_mapping(self, mapping: dict, state: dict) -> dict[str, int]:
        """Validate a tool index -> physical slot map. Raises MultiColorUnsupported when unsupported."""

    # readiness and sync
    @abstractmethod
    def readiness_problems(self, state: dict, status: dict, selection: dict | None = None, **kw) -> list[str]:
        """Reasons the printer is not ready to start; empty when ready."""

    def screen_command(self, slot: int, material: str, hex_code: str) -> str | None:
        """G-code that tells a native screen what is loaded, or None when the printer has no screen sync."""
        return None

    def stock_api_url(self, host: str) -> str | None:
        """Vendor status endpoint, or None when the printer only speaks Moonraker."""
        return None

    # telemetry
    @abstractmethod
    def moonraker_objects(self) -> list[str]:
        """Moonraker objects to query for status."""

    @abstractmethod
    def normalize_status(self, raw: dict) -> dict[str, Any]:
        """Moonraker 'status' dict -> {state, progress, filename, hotend_c, bed_c, ...}."""


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def basic_status(raw: dict) -> dict[str, Any]:
    """Plain Moonraker fields shared by every model."""
    raw = raw if isinstance(raw, dict) else {}
    stats = raw.get("print_stats") or {}
    info = stats.get("info") if isinstance(stats.get("info"), dict) else {}
    progress = (raw.get("virtual_sdcard") or {}).get("progress")
    out: dict[str, Any] = {
        "state": stats.get("state", "unknown"),
        "filename": stats.get("filename") or "",
        "progress": progress,
        "hotend_c": (raw.get("extruder") or {}).get("temperature"),
        "bed_c": (raw.get("heater_bed") or {}).get("temperature"),
        "hotend_target_c": _number((raw.get("extruder") or {}).get("target")),
        "bed_target_c": _number((raw.get("heater_bed") or {}).get("target")),
        # Moonraker's print_stats.info is filled only when the slicer emits SET_PRINT_STATS_INFO.
        "layer": _int(info.get("current_layer")),
        "total_layers": _int(info.get("total_layer")),
        "remaining_min": None,
    }
    # Moonraker reports no remaining time: estimate it from elapsed print time and file
    # progress (elapsed / progress - elapsed), only while printing with progress above zero.
    duration = _number(stats.get("print_duration"))
    if out["state"] == "printing" and duration is not None and _number(progress) and progress > 0:
        out["remaining_min"] = int((duration / progress - duration) // 60)
    # Dynamic multi-toolhead telemetry if extruder1..N exist
    extras = []
    for k, v in raw.items():
        if k.startswith("extruder") and k[8:].isdigit() and isinstance(v, dict):
            extras.append((int(k[8:]), k, v))
    if extras:
        out["toolheads"] = [
            {"tool": 0, "name": "extruder", "temperature": out["hotend_c"]}
        ]
        for idx, k, v in sorted(extras):
            out["toolheads"].append(
                {"tool": idx, "name": k, "temperature": v.get("temperature")}
            )
    return out


# A Bambu Lab printer runs plain G/M/T codes only. Klipper macros (PRINT_START, the ZMod
# START_PRINT ... MESH= line, CHANGE_ZCOLOR, LOAD_ZOFFSET) are another vendor's start code.
_GCODE_WORD = re.compile(r"^[GMT]\d+(?:\.\d+)?$", re.I)


def start_code_problems(text: str, vendor: VendorProfile) -> list[str]:
    """Reasons this G-code must not reach a printer of `vendor`; empty when none apply.

    Bambu and stock Flashforge are checked line by line: every command must be a G, M or T
    code, so no Klipper or ZMod macro can reach that hardware. Other vendors keep their
    existing slice guards.
    """
    if vendor.start_code == "flashforge_stock":
        from .flashforge_stock.guard import stock_gcode_problems
        return stock_gcode_problems(text)
    if vendor.start_code != "bambu":
        return []
    foreign = []
    for number, line in enumerate(text.splitlines(), 1):
        command = line.split(";", 1)[0].strip()
        if not command:
            continue
        word = command.split()[0]
        if not _GCODE_WORD.match(word):
            foreign.append(f"line {number}: {command[:60]}")
            if len(foreign) == 3:
                break
    if not foreign:
        return []
    return [f"G-code for {vendor.label} contains non-Bambu commands (Klipper or ZMod start code?): "
            + "; ".join(foreign)]
