"""The one print cost engine (#1625).

Every surface that names a cost calls this module: chat, the Web UI
calculator, the print confirmation flow and the completion record all
show the same numbers because they all come from :func:`compute`.

The model, in canonical order (see docs/COST.md):

- Material per line: FDM grams times (spool price / spool net grams);
  resin milliliters times (bottle price / bottle milliliters). A bottle
  sold by weight converts through density (default 1.10 g/mL). Slicer
  gram totals already include supports, brim, purge and prime tower, so
  those are never added a second time.
- Waste allowance on material only: 3 percent FDM, 10 percent resin.
- Electricity from average watts, never power supply or peak watts.
- Depreciation over lifetime hours (5,000 FDM / 4,000 resin defaults)
  and maintenance per hour; the machine subtotal carries no buffer.
- Labor split: prep once per build, post per part, pack once.
- Consumables: one flat line for FDM; film, IPA, cleanup/PPE and screen
  wear itemized per build for resin.
- Failure allowance: the repeatable lines (material, machine,
  consumables) are divided by (1 - failure rate); labor and extras are
  counted once. 5 percent FDM, 10 percent resin by default.
- Selling price: margin mode divides by (1 - margin); markup mode
  multiplies by (1 + markup). The two are labeled in words everywhere.
- Rounding: every breakdown line is rounded to cents first, then
  summed, so the visible lines always add to the visible total.
- Provenance rides on every result: grams source, price source per
  line, printer profile, currency and timestamp.

Cost is informational only. Nothing in this module blocks, delays or
refuses a print, and a missing price or profile produces a labeled
estimate or an explicit gap, never an exception to the caller.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

FDM = "fdm"
RESIN = "resin"

# Density defaults (g/mL) from slicer generic profiles.
DENSITY_DEFAULTS = {
    "PLA": 1.24, "PETG": 1.27, "ABS": 1.04, "ASA": 1.04, "TPU": 1.21,
    "PA": 1.14, "PC": 1.20, "RESIN": 1.10, "STANDARD RESIN": 1.10,
}
DEFAULT_RESIN_DENSITY = 1.10

# Shipped average-watt presets. basis names where the figure comes from:
# "manufacturer" (the maker's published PLA average), "measured" (a
# wall-meter test) or "estimate" (no reliable figure found; labeled as
# an estimate in the UI, per the issue).
WATTAGE_PRESETS = {
    "bambu_a1_mini": {"label": "Bambu A1 mini", "watts": 80.0, "basis": "manufacturer"},
    "bambu_a1": {"label": "Bambu A1", "watts": 95.0, "basis": "manufacturer"},
    "bambu_p1s": {"label": "Bambu P1S", "watts": 105.0, "basis": "manufacturer"},
    "bambu_x1c": {"label": "Bambu X1C", "watts": 105.0, "basis": "manufacturer"},
    "prusa_mk4s": {"label": "Prusa MK4S", "watts": 80.0, "basis": "manufacturer"},
    "prusa_core_one": {"label": "Prusa CORE One", "watts": 80.0, "basis": "manufacturer"},
    "prusa_xl": {"label": "Prusa XL", "watts": 80.0, "basis": "manufacturer"},
    "ender3_class": {"label": "Ender-3 class", "watts": 110.0, "basis": "estimate"},
    "flashforge_ad5m": {"label": "Flashforge Adventurer 5M", "watts": 130.0, "basis": "measured"},
    # No reliable measured average for the AD5X was found in research;
    # this is the same platform's measured figure, labeled an estimate.
    "flashforge_ad5x": {"label": "Flashforge AD5X", "watts": 130.0, "basis": "estimate"},
    "voron_24_350": {"label": "Voron 2.4 350 (ABS workload)", "watts": 225.0, "basis": "measured"},
    "generic_fdm": {"label": "Generic FDM", "watts": 120.0, "basis": "estimate"},
    "generic_resin": {"label": "Generic resin", "watts": 60.0, "basis": "estimate"},
    "elegoo_mars_class": {"label": "Elegoo Mars class", "watts": 50.0, "basis": "estimate"},
    "elegoo_saturn_class": {"label": "Elegoo Saturn class", "watts": 60.0, "basis": "estimate"},
}

MARGIN_TIERS = (0.25, 0.40, 0.60, 0.80)


def _num(value, default=None):
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _round2(value):
    """Round to cents, half away from zero on the visible figure.

    Decimal on the shortest repr, so a true half cent (1.155) rounds
    up instead of following the binary float just under it.
    """
    from decimal import Decimal, ROUND_HALF_UP
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class WorkspaceCost:
    """Workspace-wide cost defaults (the [cost] settings section)."""

    currency_code: str = "USD"
    electricity_rate_per_kwh: float = 0.19
    labor_rate_per_hour: float = 20.0
    failure_rate_fdm: float = 0.05
    failure_rate_resin: float = 0.10
    pricing_mode: str = "margin"
    default_margin: float = 0.40
    tax_rate: float = 0.0
    tax_mode: str = "add_on_top"
    marketplace_fee_rate: float = 0.0
    overhead_percent: float = 0.0
    flat_fee_per_job: float = 0.0
    default_filament_cost_per_kg: float = 22.0

    @classmethod
    def from_settings(cls, data):
        raw = (data or {}).get("cost") or {}
        out = cls()
        out.currency_code = str(raw.get("currency_code") or out.currency_code).upper()
        for key in ("electricity_rate_per_kwh", "labor_rate_per_hour",
                    "failure_rate_fdm", "failure_rate_resin", "default_margin",
                    "tax_rate", "marketplace_fee_rate", "overhead_percent",
                    "flat_fee_per_job", "default_filament_cost_per_kg"):
            value = _num(raw.get(key))
            if value is not None:
                setattr(out, key, value)
        mode = str(raw.get("pricing_mode") or out.pricing_mode).lower()
        out.pricing_mode = mode if mode in ("margin", "markup") else "margin"
        tax_mode = str(raw.get("tax_mode") or out.tax_mode).lower()
        out.tax_mode = tax_mode if tax_mode in ("add_on_top", "included", "none") else "add_on_top"
        return out


@dataclass
class PrinterCostProfile:
    """One printer's cost profile."""

    name: str = "Generic FDM"
    technology: str = FDM
    purchase_price: float = 0.0
    upfront_cost: float = 0.0
    salvage_value: float = 0.0
    lifetime_hours: float = 5000.0
    maintenance_per_hour: float = 0.05
    watts: float = 120.0
    watts_basis: str = "estimate"
    watts_by_material: dict = field(default_factory=dict)
    idle_watts: float = 0.0
    # Resin only.
    wash_cure_watts: float = 60.0
    wash_cure_minutes: float = 15.0
    screen_price: float = 60.0
    screen_life_hours: float = 2000.0
    film_price: float = 12.0
    film_life_prints: float = 100.0

    @property
    def is_resin(self):
        return self.technology == RESIN

    def watts_for(self, material=None):
        if material:
            override = self.watts_by_material.get(str(material).upper())
            if override is not None:
                return float(override)
        return self.watts

    @classmethod
    def from_mapping(cls, name, raw):
        raw = raw or {}
        tech = str(raw.get("technology") or FDM).lower()
        tech = RESIN if tech in ("resin", "msla", "sla", "dlp") else FDM
        out = cls(name=str(name), technology=tech)
        if tech == RESIN:
            out.lifetime_hours = 4000.0
            out.watts = 60.0
        for key in ("purchase_price", "upfront_cost", "salvage_value", "lifetime_hours",
                    "maintenance_per_hour", "watts", "idle_watts", "wash_cure_watts",
                    "wash_cure_minutes", "screen_price", "screen_life_hours",
                    "film_price", "film_life_prints"):
            value = _num(raw.get(key))
            if value is not None:
                setattr(out, key, value)
        basis = str(raw.get("watts_basis") or "").lower()
        if basis in ("manufacturer", "measured", "estimate"):
            out.watts_basis = basis
        overrides = raw.get("watts_by_material")
        if isinstance(overrides, dict):
            out.watts_by_material = {str(k).upper(): float(v) for k, v in overrides.items()
                                     if _num(v) is not None}
        return out


def printer_profile_for(data, printer_id=None, printer_name=None):
    """The cost profile for one printer: [cost_printers] entry, else a preset/default.

    Profiles live in the file-only [cost_printers] table keyed by roster
    id or printer name. A printer with no entry gets the generic profile
    for its kind at the workspace defaults, so a cost is always
    computable and the profile name says it is the generic one.
    """
    workspace = WorkspaceCost.from_settings(data)
    profiles = (data or {}).get("cost_printers") or {}
    if isinstance(profiles, dict):
        for key in (printer_id, printer_name):
            if key and str(key) in profiles and isinstance(profiles[str(key)], dict):
                return PrinterCostProfile.from_mapping(str(key), profiles[str(key)]), workspace
    return PrinterCostProfile(), workspace


@dataclass
class MaterialLine:
    """One filament or resin line of a job."""

    name: str = "Filament"
    material: str = ""
    grams: float = 0.0            # FDM: grams (slicer total for this line)
    volume_ml: float = 0.0        # resin: milliliters
    spool: dict | None = None     # a Spoolman spool record (with nested filament)
    filament: dict | None = None  # a Spoolman filament record
    slicer_price_per_kg: float | None = None  # embedded slicer-profile price
    manual_price_per_kg: float | None = None
    density: float | None = None  # g/mL, for length and resin weight conversions
    spool_id: object = None

    def density_or_default(self):
        if self.density:
            return float(self.density)
        key = (self.material or "").upper()
        return DENSITY_DEFAULTS.get(key, DEFAULT_RESIN_DENSITY if not self.grams else 1.24)


def _spool_filament(spool):
    if isinstance(spool, dict):
        filament = spool.get("filament")
        if isinstance(filament, dict):
            return filament
    return {}


def spool_net_grams(spool, filament=None):
    """Net filament grams on a spool: tare is never priced.

    Spoolman's spool ``initial_weight`` is the net filament weight the
    spool started with; the filament record's ``weight`` is its net
    weight too (the empty spool is ``spool_weight``, kept separate for
    exactly this reason).
    """
    spool = spool or {}
    filament = filament if filament is not None else _spool_filament(spool)
    for value in (spool.get("initial_weight"), (filament or {}).get("weight")):
        grams = _num(value)
        if grams and grams > 0:
            return grams
    return None


def resolve_price(line: MaterialLine, workspace: WorkspaceCost, technology=FDM):
    """(price per gram or per mL, source label) for one material line.

    Precedence: the spool's own price, then the filament profile price,
    then the slicer-embedded price, then the workspace default per kg.
    Resin converts a weight-priced bottle through density.
    """
    spool = line.spool or {}
    filament = line.filament or _spool_filament(spool)
    density = line.density_or_default()
    net_g = spool_net_grams(spool, filament)
    spool_price = _num(spool.get("price"))
    if spool_price is not None and net_g:
        per_g = spool_price / net_g
        label = f"spool price (Spoolman spool {spool.get('id', '?')})"
        return (per_g * density if technology == RESIN else per_g), label
    filament_price = _num(filament.get("price"))
    filament_g = _num(filament.get("weight"))
    if filament_price is not None and filament_g:
        per_g = filament_price / filament_g
        label = "filament profile price (Spoolman)"
        return (per_g * density if technology == RESIN else per_g), label
    # A resin bottle priced by volume: price over bottle milliliters.
    bottle_ml = _num((filament or {}).get("volume_ml")) or _num(spool.get("volume_ml"))
    bottle_price = spool_price if spool_price is not None else filament_price
    if technology == RESIN and bottle_price is not None and bottle_ml:
        return bottle_price / bottle_ml, "bottle price by volume (Spoolman)"
    for value, label in ((line.manual_price_per_kg, "manual price"),
                         (line.slicer_price_per_kg, "slicer-embedded price")):
        per_kg = _num(value)
        if per_kg is not None and per_kg > 0:
            per_g = per_kg / 1000.0
            return (per_g * density if technology == RESIN else per_g), label
    per_g = workspace.default_filament_cost_per_kg / 1000.0
    return (per_g * density if technology == RESIN else per_g), "workspace default cost per kilogram"


@dataclass
class JobCostInput:
    """Everything the engine needs for one build."""

    lines: list = field(default_factory=list)  # of MaterialLine
    print_hours: float = 0.0
    parts_per_build: int = 1
    prep_minutes: float = 5.0
    post_minutes_per_part: float = 10.0
    pack_minutes: float = 0.0
    hardware_cost: float = 0.0
    packaging_cost: float = 0.0
    consumables_per_print: float | None = None  # FDM flat; None uses 0.10
    waste_fraction: float | None = None         # None uses the technology default
    failure_rate: float | None = None           # None uses the workspace default
    measured_kwh: float | None = None           # a meter reading beats the estimate
    grams_source: str = "manual entry"
    title: str = ""


@dataclass
class CostResult:
    lines: list                 # of {"key", "label", "amount"} cent-rounded
    material_lines: list        # per-material detail dicts
    cost_per_build: float
    cost_per_part: float
    parts_per_build: int
    currency_code: str
    provenance: dict
    selling: dict               # tiers and the default price
    failure_rate: float
    failure_amount: float
    technology: str
    totals_detail: dict

    def to_dict(self):
        return {
            "lines": self.lines,
            "material_lines": self.material_lines,
            "cost_per_build": self.cost_per_build,
            "cost_per_part": self.cost_per_part,
            "parts_per_build": self.parts_per_build,
            "currency_code": self.currency_code,
            "provenance": self.provenance,
            "selling": self.selling,
            "failure_rate": self.failure_rate,
            "failure_amount": self.failure_amount,
            "technology": self.technology,
            "totals_detail": self.totals_detail,
        }

    @classmethod
    def from_dict(cls, raw):
        raw = raw or {}
        return cls(lines=list(raw.get("lines") or []),
                   material_lines=list(raw.get("material_lines") or []),
                   cost_per_build=_num(raw.get("cost_per_build"), 0.0),
                   cost_per_part=_num(raw.get("cost_per_part"), 0.0),
                   parts_per_build=int(raw.get("parts_per_build") or 1),
                   currency_code=str(raw.get("currency_code") or "USD"),
                   provenance=dict(raw.get("provenance") or {}),
                   selling=dict(raw.get("selling") or {}),
                   failure_rate=_num(raw.get("failure_rate"), 0.0),
                   failure_amount=_num(raw.get("failure_amount"), 0.0),
                   technology=str(raw.get("technology") or FDM),
                   totals_detail=dict(raw.get("totals_detail") or {}))


def compute(job: JobCostInput, profile: PrinterCostProfile, workspace: WorkspaceCost,
            *, timestamp=None):
    """The canonical calculation. Pure: no I/O, no settings reads."""
    tech = profile.technology
    is_resin = tech == RESIN
    parts = max(1, int(job.parts_per_build or 1))
    waste = job.waste_fraction if job.waste_fraction is not None else (0.10 if is_resin else 0.03)
    failure = job.failure_rate
    if failure is None:
        failure = workspace.failure_rate_resin if is_resin else workspace.failure_rate_fdm
    failure = min(max(float(failure), 0.0), 0.95)

    # Material, per line, at that line's own price.
    material_total = 0.0
    material_lines = []
    for line in job.lines:
        per_unit, source = resolve_price(line, workspace, tech)
        amount_raw = (line.volume_ml if is_resin else line.grams) * per_unit
        material_total += amount_raw
        material_lines.append({
            "name": line.name, "material": line.material,
            "grams": line.grams, "volume_ml": line.volume_ml,
            "price_per_kg_or_l": round(per_unit * (1000.0 / line.density_or_default()
                                                   if is_resin else 1000.0), 4),
            "price_source": source, "amount": _round2(amount_raw),
            "spool_id": line.spool_id if line.spool_id is not None
                        else (line.spool or {}).get("id"),
        })
    material_with_waste = material_total * (1 + waste)
    waste_amount = material_with_waste - material_total

    # Machine: electricity, depreciation, maintenance. No buffer.
    hours = max(0.0, float(job.print_hours or 0.0))
    if job.measured_kwh is not None:
        electricity = float(job.measured_kwh) * workspace.electricity_rate_per_kwh
        kwh = float(job.measured_kwh)
    else:
        material_name = job.lines[0].material if job.lines else ""
        watts = profile.watts_for(material_name)
        kwh = watts * hours / 1000.0
        if is_resin:
            kwh += profile.wash_cure_watts * (profile.wash_cure_minutes / 60.0) / 1000.0
        electricity = kwh * workspace.electricity_rate_per_kwh
    lifetime = profile.lifetime_hours or 5000.0
    depreciation = hours * (profile.purchase_price + profile.upfront_cost
                            - profile.salvage_value) / lifetime
    maintenance = hours * profile.maintenance_per_hour

    # Labor: prep once per build, post per part, pack once.
    labor_minutes = job.prep_minutes + job.post_minutes_per_part * parts + job.pack_minutes
    labor = labor_minutes / 60.0 * workspace.labor_rate_per_hour

    # Consumables.
    if is_resin:
        film = profile.film_price / profile.film_life_prints if profile.film_life_prints else 0.0
        # IPA cost per print: a workspace-priced loss per wash is carried
        # on the profile-free resin defaults below; the itemized lines
        # stay explicit so the breakdown names each one.
        ipa = RESIN_IPA_PER_PRINT
        ppe = RESIN_PPE_PER_PRINT
        screen = (profile.screen_price / profile.screen_life_hours
                  if profile.screen_life_hours else 0.0) * hours
        consumable_parts = {"film": film, "ipa": ipa, "ppe": ppe, "screen_wear": screen}
        consumables = film + ipa + ppe + screen
    else:
        flat = 0.10 if job.consumables_per_print is None else float(job.consumables_per_print)
        consumable_parts = {"flat": flat}
        consumables = flat

    extras = (float(job.hardware_cost or 0.0) + float(job.packaging_cost or 0.0)
              + workspace.flat_fee_per_job)

    # Cent-first rounding: round every visible line, then sum.
    r_material = _round2(material_with_waste)
    r_electricity = _round2(electricity)
    r_depreciation = _round2(depreciation)
    r_maintenance = _round2(maintenance)
    r_labor = _round2(labor)
    r_consumables = _round2(consumables)
    r_extras = _round2(extras)
    repeatable = r_material + r_electricity + r_depreciation + r_maintenance + r_consumables
    once = r_labor + r_extras
    overhead = _round2((repeatable + once) * workspace.overhead_percent)
    once += overhead
    if failure > 0:
        adjusted_repeatable = _round2(repeatable / (1 - failure))
        failure_amount = _round2(adjusted_repeatable - repeatable)
    else:
        adjusted_repeatable = repeatable
        failure_amount = 0.0
    total = _round2(once + adjusted_repeatable)
    per_part = _round2(total / parts) if parts else total

    lines = [
        {"key": "material", "label": "Material (incl. waste allowance)", "amount": r_material},
        {"key": "electricity", "label": "Electricity", "amount": r_electricity},
        {"key": "depreciation",
         "label": f"Depreciation (over {lifetime:,.0f} lifetime hours)", "amount": r_depreciation},
        {"key": "maintenance", "label": "Maintenance", "amount": r_maintenance},
        {"key": "labor", "label": "Labor (prep per build, post per part, pack)", "amount": r_labor},
        {"key": "consumables", "label": "Consumables", "amount": r_consumables},
    ]
    if r_extras:
        lines.append({"key": "extras", "label": "Hardware, packaging and fees", "amount": r_extras})
    if overhead:
        lines.append({"key": "overhead", "label": "Overhead", "amount": overhead})
    if failure_amount:
        lines.append({"key": "failure",
                      "label": f"Failure allowance ({failure * 100:.0f} percent, repeatable lines "
                               f"divided by 1 minus the rate)",
                      "amount": failure_amount})

    selling = _selling(total, workspace)
    total_grams = sum(line.grams for line in job.lines)
    total_ml = sum(line.volume_ml for line in job.lines)
    provenance = {
        "grams_source": job.grams_source,
        "price_sources": [entry["price_source"] for entry in material_lines],
        "printer_profile": profile.name,
        "lifetime_hours": lifetime,
        "currency_code": workspace.currency_code,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "waste_fraction": waste,
    }
    return CostResult(
        lines=lines, material_lines=material_lines,
        cost_per_build=total, cost_per_part=per_part, parts_per_build=parts,
        currency_code=workspace.currency_code, provenance=provenance,
        selling=selling, failure_rate=failure, failure_amount=failure_amount,
        technology=tech,
        totals_detail={
            "material_before_waste": _round2(material_total),
            "waste_amount": _round2(waste_amount),
            "machine_subtotal": _round2(r_electricity + r_depreciation + r_maintenance),
            "electricity_kwh": round(kwh, 4),
            "consumable_parts": {k: _round2(v) for k, v in consumable_parts.items()},
            "print_hours": hours,
            "total_grams": total_grams, "total_volume_ml": total_ml,
            "cost_per_gram": _round2(total / total_grams) if total_grams else None,
            "cost_per_print_hour": _round2(total / hours) if hours else None,
        })


# Resin per-print consumable defaults (the reference model's itemized
# block): IPA lost per wash and cleanup/PPE per print.
RESIN_IPA_PER_PRINT = 0.23
RESIN_PPE_PER_PRINT = 0.12


def _selling(cost, workspace: WorkspaceCost):
    """Selling prices at the tiers, labeled margin or markup in words."""
    mode = workspace.pricing_mode
    tiers = {}
    for tier in MARGIN_TIERS:
        tiers[f"{int(tier * 100)}"] = _price_with_fees_and_tax(
            _apply_profit(cost, mode, tier), workspace)
    default_rate = workspace.default_margin
    return {
        "mode": mode,
        "mode_label": ("Margin (share of the final price)" if mode == "margin"
                       else "Markup (added to cost)"),
        "default_rate": default_rate,
        "default_price": _price_with_fees_and_tax(
            _apply_profit(cost, mode, default_rate), workspace),
        "tiers": tiers,
        "tax_rate": workspace.tax_rate, "tax_mode": workspace.tax_mode,
        "marketplace_fee_rate": workspace.marketplace_fee_rate,
    }


def _apply_profit(cost, mode, rate):
    if mode == "margin":
        return cost / (1 - rate) if rate < 1 else cost
    return cost * (1 + rate)


def _price_with_fees_and_tax(price, workspace: WorkspaceCost):
    if workspace.marketplace_fee_rate:
        price = price / (1 - workspace.marketplace_fee_rate)
    if workspace.tax_mode == "add_on_top" and workspace.tax_rate:
        price = price * (1 + workspace.tax_rate)
    return _round2(price)


def freeze(result: CostResult):
    """The frozen per-job cost record: plain data, stored once at completion."""
    return result.to_dict()


def should_decrement_spoolman(accounting: str, moonraker_already_reported: bool) -> bool:
    """The single-decrement guard.

    Usage is written to Spoolman once. When the printer host integration
    (for example the Moonraker Spoolman component) already reported this
    job, or the printer is the accounting authority under the [spool]
    accounting setting (#1536), Print Handy does not write it again.
    """
    if moonraker_already_reported:
        return False
    return str(accounting or "auto").lower() not in ("printer",)


def aggregate_costs(records):
    """Cost aggregates over job records carrying a frozen ``cost`` dict.

    Frozen records only: a record with no priced cost counts as
    unpriced and is never estimated here. Currencies are never mixed:
    totals are reported per currency, and the headline currency is the
    one with the most priced records.
    """
    priced, unpriced = [], 0
    for record in records or []:
        cost = record.get("cost") if isinstance(record, dict) else None
        if isinstance(cost, dict) and _num(cost.get("cost_per_build")) is not None:
            priced.append((record, cost))
        else:
            unpriced += 1
    by_currency = {}
    for pair in priced:
        by_currency.setdefault(pair[1].get("currency_code") or "USD", []).append(pair)
    headline = None
    if by_currency:
        headline = max(by_currency, key=lambda code: len(by_currency[code]))
    rows = by_currency.get(headline, []) if headline else []

    def _sum_by(key_of):
        table = {}
        for record, cost in rows:
            key = key_of(record, cost) or "unknown"
            table[key] = _round2(table.get(key, 0.0) + float(cost["cost_per_build"]))
        return table

    total = _round2(sum(float(cost["cost_per_build"]) for _, cost in rows))
    total_grams = sum(float((cost.get("totals_detail") or {}).get("total_grams") or 0)
                      for _, cost in rows)
    total_hours = sum(float((cost.get("totals_detail") or {}).get("print_hours") or 0)
                      for _, cost in rows)
    failure_total = _round2(sum(float(cost.get("failure_amount") or 0) for _, cost in rows))
    most_expensive = None
    if rows:
        record, cost = max(rows, key=lambda pair: float(pair[1]["cost_per_build"]))
        most_expensive = {"title": record.get("title") or record.get("gcode_file") or "print",
                          "cost_per_build": cost["cost_per_build"],
                          "currency_code": headline}
    by_material = {}
    for _record, cost in rows:
        for entry in cost.get("material_lines") or []:
            key = entry.get("material") or entry.get("name") or "unknown"
            by_material[key] = _round2(by_material.get(key, 0.0) + float(entry.get("amount") or 0))
    return {
        "has_priced": bool(rows),
        "priced_count": len(priced), "unpriced_count": unpriced,
        "currency_code": headline,
        "currencies": sorted(by_currency),
        "total_spend": total,
        "by_printer": _sum_by(lambda r, c: r.get("printer")),
        "by_material": by_material,
        "by_spool": _sum_by(lambda r, c: ", ".join(
            str(e.get("spool_id")) for e in c.get("material_lines") or [] if e.get("spool_id"))),
        "cost_per_gram": _round2(total / total_grams) if total_grams else None,
        "cost_per_print_hour": _round2(total / total_hours) if total_hours else None,
        "most_expensive": most_expensive,
        "failure_total": failure_total,
        "failure_share": _round2(failure_total / total) if total else None,
        "note": ("" if rows else
                 "No priced jobs in this period: no completed print has a recorded cost yet."),
    }


def format_chat_summary(result: CostResult, title=""):
    """The compact chat breakdown: per part and per build, top lines, provenance."""
    cur = result.currency_code
    lines = [f"Cost{(' for ' + title) if title else ''}: "
             f"{cur} {result.cost_per_part:.2f} per part, "
             f"{cur} {result.cost_per_build:.2f} per build "
             f"({result.parts_per_build} part(s) per build)"]
    top = sorted(result.lines, key=lambda entry: entry["amount"], reverse=True)[:3]
    for entry in top:
        lines.append(f"- {entry['label']}: {cur} {entry['amount']:.2f}")
    if result.failure_amount:
        lines.append(f"Failure allowance included: {cur} {result.failure_amount:.2f} "
                     f"at {result.failure_rate * 100:.0f} percent")
    prov = result.provenance
    lines.append(f"Priced from: {', '.join(prov.get('price_sources') or [])}; "
                 f"amounts from {prov.get('grams_source')}; profile {prov.get('printer_profile')}.")
    lines.append("Full breakdown is on the Web UI cost page. Cost is information only; "
                 "it never blocks a print.")
    return "\n".join(lines)


def format_breakdown(result: CostResult):
    """The full itemized breakdown text (Web UI and detailed chat answers)."""
    cur = result.currency_code
    out = [f"{entry['label']}: {cur} {entry['amount']:.2f}" for entry in result.lines]
    out.append(f"Cost per build: {cur} {result.cost_per_build:.2f}")
    out.append(f"Cost per part: {cur} {result.cost_per_part:.2f}")
    selling = result.selling or {}
    if selling:
        out.append(f"Selling price at the default {selling.get('mode')} of "
                   f"{int(float(selling.get('default_rate') or 0) * 100)} percent "
                   f"({selling.get('mode_label')}): {cur} {selling.get('default_price', 0):.2f}")
    return "\n".join(out)
