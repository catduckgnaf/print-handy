# Print cost calculator

Print Handy can work out what a print cost, or will cost, from data it
already has: slicer grams or resin volume, print time, the printer
profile, and Spoolman prices. One engine (`print_handy/core/cost.py`)
does the math for every surface, so chat, the Web UI calculator page,
the completion record, the Data tab and Pasta Wrapped never disagree.

Cost is information only. It never blocks, delays or refuses a print.
A missing price or profile produces a labeled estimate or a plain
"cannot estimate yet" naming what is missing.

## Asking in chat

- `/print-cost` asks about the last completed print, answered from its
  frozen completion record.
- `/print-cost 50 g, 3 h, in PETG` (or just asking "how much will this
  cost, 50 g, 3 h") answers an estimate for those amounts.
- Plain phrases work too: "how much did that print cost", "price this
  print", "what did filament cost this month".
- An ask with no job and no amounts asks which print is meant instead
  of guessing.

The chat answer is compact: cost per part and per build, the top cost
lines, the failure allowance and the provenance line. The full
breakdown is on the Web UI cost page.

## The Web UI calculator

The Cost page (in the header) has a simple mode (material amount and
print time plus the technology picker) and an advanced mode with every
input the engine reads. Dropping a G-code or sliced 3MF file on the
page auto-fills amount and time. An unsliced 3MF, and a PrusaSlicer
3MF (which never contains time or weight), answer with the explicit
not-sliced state instead of a zero cost.

Workspace defaults live on the Settings page in the Print cost group.
Per-printer profiles live in the settings file's `[cost_printers]`
table (file only, because it is a table of tables).

## The formula

In order, per build:

1. Material, per filament or resin line: FDM grams times (spool price
   divided by spool net grams); resin milliliters times (bottle price
   divided by bottle milliliters). A bottle sold by weight converts
   through density (default 1.10 g/mL). Slicer totals already include
   supports, brim, purge and prime tower, so those are never added
   again.
2. Waste allowance on material: 3 percent FDM, 10 percent resin.
3. Electricity: average watts times print hours (plus wash and cure
   watts times wash and cure hours for resin), divided by 1000, times
   the rate per kWh. Average watts are always used, never the power
   supply rating.
4. Depreciation: print hours times (printer price plus upfront cost
   minus salvage value) divided by lifetime hours. Default lifetime
   5,000 hours FDM, 4,000 hours resin. The lifetime is shown next to
   every machine rate.
5. Maintenance: print hours times maintenance per hour (default 0.05
   FDM). Resin film and screen are separate lines, not in maintenance.
6. Labor: prep minutes once per build, plus post minutes per part
   times parts, plus pack minutes, divided by 60, times the labor
   rate. The hobby preset sets the rate to 0 and still shows the line.
7. Consumables: FDM one flat line (default 0.10). Resin itemized per
   build: film price divided by prints per film, IPA lost per wash,
   cleanup and PPE per print, and screen wear per hour (screen price
   divided by screen life hours) times print hours.
8. Extras: hardware plus packaging plus an optional flat fee, at cost.
9. Failure allowance: the repeatable lines (material, machine,
   consumables) are divided by (1 minus the failure rate); labor and
   extras are counted once. Defaults: 5 percent FDM, 10 percent resin.
10. Totals: cost per build is the failure-adjusted total; cost per
    part divides by parts per build.

Selling price (optional): margin mode (the default, default 40
percent, tiers 25, 40, 60 and 80 percent) divides cost by (1 minus
margin). Markup mode multiplies cost by (1 plus markup). The control
is labeled in words because the two are not interchangeable: a 40
percent margin is a 66.7 percent markup. Marketplace fees gross the
price up before tax; tax then applies per the tax mode.

Rounding: every breakdown line is rounded to cents first, then
summed, so the visible lines always add to the visible total.

Provenance: every result names the grams source (slicer G-code, 3MF
slice info, measured history, manual entry), the price source per
filament, the printer profile, the currency and the timestamp.

## Price precedence (Spoolman first)

Per filament line, the first available price wins, and the result
says which one priced it:

1. The selected spool's own price divided by its net weight.
2. The filament profile price divided by its net weight.
3. The slicer-embedded price (a fallback, labeled as such).
4. The workspace default cost per kilogram.

Net weight always excludes the empty spool tare. Multi-material jobs
are priced per spool at that spool's own rate, purge and tower grams
included at the purged filament's rate. Usage is decremented once:
when the printer host integration already reported a job's usage, or
the printer is the accounting authority under `[spool] accounting`,
Print Handy does not write it again.

## Frozen records, Data and Pasta Wrapped

At completion, the cost is resolved once (measured duration and
filament use win over slicer estimates) and frozen on the job record.
Later price edits never rewrite history. The Data tab and Pasta
Wrapped aggregate the frozen records only: total spend, spend by
printer, by material and by spool, cost per gram and per print hour,
the most expensive print, and the failure cost share. A period with
no priced jobs says so plainly. No model is involved in cost figures,
and currencies are never mixed silently: totals are reported per
currency.

## Defaults and presets

Workspace defaults: currency USD, electricity 0.19 per kWh, labor 20
per hour, failure 5 percent FDM and 10 percent resin, pricing mode
margin at 40 percent, tax 0, no marketplace fee, no overhead, no flat
fee, default filament 22 per kilogram.

Shipped average-watt presets, labeled by basis in the UI: Bambu A1
mini 80 W, A1 95 W, P1S 105 W, X1C 105 W (manufacturer); Prusa MK4S,
CORE One and XL 80 W (manufacturer, PLA figures); Ender-3 class 110 W
(estimate); Flashforge Adventurer 5M 130 W (measured); Flashforge
AD5X 130 W (estimate, from the same platform: no reliable measured
average for the AD5X was found); Voron 2.4 350 about 225 W for an ABS
workload (measured); generic FDM 120 W, generic resin 60 W, Elegoo
Mars class 50 W, Saturn class 60 W (estimates).

Density defaults: PLA 1.24, PETG 1.27, ABS and ASA 1.04, TPU 1.21,
standard resin 1.10 g/mL.

## Resin file support

ChiTuBox and Lychee textual metadata carrying volume and time is
parsed. The binary ChiTuBox project format (.cxdlpv4) and the Lychee
.lys container are not parsed in this version; a resin job in those
formats is costed from its Spoolman bottle and entered volume, and
the calculator says what is missing rather than guessing.
