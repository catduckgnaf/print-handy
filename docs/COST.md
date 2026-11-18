# Print cost calculator

Print Handy can work out what a print cost, or will cost, from data it
already has: slicer grams or resin volume, print time, the printer
profile, and Spoolman prices. One engine (`print_handy/core/cost.py`)
does the math for every surface, so chat, the Web UI calculator page,
the completion record, the Data tab and Pasta Wrapped never disagree.

Cost is information only. It never blocks, delays or refuses a print.
A missing price or profile produces a labeled estimate or a plain
"cannot estimate yet" naming what is missing.

## Hobby and seller

Every cost carries two figures:

- **Out of pocket**: what the print actually cost you. Filament (with
  the waste allowance), electricity and consumables, grossed up by the
  failure allowance, plus any hardware, packaging and flat fee.
- **Full cost**: out of pocket plus labor, depreciation and maintenance.
  This is what a quote has to recover.

`[cost] cost_mode` picks which one leads. `hobby` (the default) leads
chat answers, the Ready card line and Pasta Wrapped with out of pocket.
`seller` leads with the full cost and shows selling price tiers. Both
figures are always recorded, so switching modes later changes how
history reads, never what was recorded.

## Asking in chat

- "How much will this cost" and "price this print" price the file in
  hand: the newest sliced job that has not printed yet, per slot at each
  assigned spool's price. With nothing sliced, the answer says so
  instead of falling back to an old print.
- "How much did that print cost" and a bare `/print-cost` answer from
  the last finished print's frozen record.
- "What did filament cost this month" and "what did I spend this month"
  answer the month's recorded spend, out of pocket first, with what
  failed prints cost named.
- `/print-cost 50 g, 3 h, in PETG` (or "how much will this cost, 50 g,
  3 h") answers an estimate for those amounts.

The chat answer is compact: the leading figure for the cost mode, the
top cost lines, the failure allowance and the provenance line. The
full breakdown is on the Web UI cost page.

## On the Ready card

When a job is sliced, the Ready card carries one line such as
"Estimated cost: about USD 0.31 in filament and power", priced from the
sliced file and its assigned spools (the larger variant when both
support choices are shown). It is information only: it adds no button,
asks nothing, and a problem working it out only leaves the line off.

The command also appears in the live demo's command list
(`demo/scenarios.json`), which tracks the production command table.

## The Web UI calculator

The Cost page (in the header, where the Printer settings tab used to
be: printer settings now open from each printer's card on the Printers
page) has a simple mode (material amount and
print time plus the technology picker) and an advanced mode with every
input the engine reads. Dropping a G-code or sliced 3MF file on the
page auto-fills amount and time. An unsliced 3MF, and a PrusaSlicer
3MF (which never contains time or weight), answer with the explicit
not-sliced state instead of a zero cost.

Workspace defaults live on the Settings page in the Print cost group.
Per-printer profiles live in the settings file's `[cost_printers]`
table (file only, because it is a table of tables), keyed by roster id
or printer name. A printer with no entry is costed on its model's
shipped wattage preset (for example the AD5X preset for a
`flashforge.ad5x`), and an entry layers over that preset, so setting
only `purchase_price` keeps the preset's watts. Without a purchase
price, depreciation is 0.00 and the result says so.

Below the calculator, the page lists the 20 most recent finished prints
with their outcome, out-of-pocket and full cost.

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

At completion, the cost is resolved once and frozen on the job record.
Measured duration and filament use win over slicer estimates. A
multicolor print is priced per slot at each slot's own spool rate,
from the same slot plan the spool accounting charges, with measured
filament scaling the plan. A failed or cancelled print is frozen at
what it actually used, with its prep labor but no post-processing and
no failure allowance: it is the failure, so it is not charged for one
twice. Later price edits never rewrite history.

The Data tab's Print costs section and Pasta Wrapped aggregate the
frozen records only: out-of-pocket and full spend, spend by printer,
material and color, cost per gram and per print hour, the most
expensive and cheapest print, and what the failed and cancelled prints
cost. Wrapped adds a "What it cost" screen and a video slide.

- A period with no priced jobs says so plainly. A period where only
  some prints were priced says "Priced N of M prints"; prints finished
  before costs were recorded carry no cost.
- Those older prints get a separate, labeled estimate from their own
  recorded grams and hours at today's prices. It is shown under the
  totals and is never added to them.
- Currencies are never mixed: totals and counts are for the main
  currency, and any other currency is named with its count.
- No model is involved in any cost figure.

## Sharing cost with HelixScreen (proposed format)

Status: proposed. The namespace and record shape below are Print
Handy's proposal, published for agreement on
prestonbrown/helixscreen#2132. Print Handy adapts to what the
HelixScreen maintainer prefers, so treat this format as a draft until
that agreement lands.

When a printer runs both Print Handy and HelixScreen, Print Handy is
the source of truth for print cost, and HelixScreen reads Print Handy's
number instead of computing its own. HelixScreen keeps its own
calculation for printers without Print Handy, and for any file with no
record.

Print Handy writes one record per file into a Moonraker database
namespace, through Moonraker's `POST /server/database/item`:

- Namespace: `print_handy_cost`. Print Handy writes only inside this
  namespace, never into Moonraker's reserved namespaces or the
  `spoolman` namespace.
- Key: the one-element array `[file name]`, with the file name exactly
  as Moonraker reports it, path included. Moonraker treats a string key
  as a dot-separated path into nested fields, which would split a name
  like `benchy.gcode`; the array form keeps the whole name, dots and
  slashes included, as one literal field.
- Lifecycle: when Print Handy sends a file to a Moonraker-backed
  printer, it writes the pre-print estimate. When the print finishes
  and the cost is frozen on the job record, it writes the frozen cost
  under the same key, replacing the estimate. A failed or cancelled
  print is frozen at what it used, the same figure the job record
  carries.
- Publishing is best effort: a failed or slow write is logged and
  never blocks, delays or fails a print send or a completion.
  Printers that are not Moonraker-backed publish nothing. There is no
  setting; whenever a cost record exists for a Moonraker-backed
  printer, it is published.

The record:

| Field | Meaning |
| --- | --- |
| `schema` | Record schema version (currently 1). Readers fail soft on a version newer than they know and keep their own calculation. |
| `state` | `estimate` for the pre-print quote, `final` once the cost is frozen at completion. |
| `file` | The file name the record is keyed by, as Moonraker reports it. |
| `outcome` | The engine outcome: `estimate`, `completed`, `failed` or `cancelled`. |
| `out_of_pocket` | Filament, electricity, consumables and extras, with their failure share: the money actually spent on the print. |
| `full_cost` | The full cost of one build, every cost line included. |
| `cost_per_part` | The full cost divided by the parts in the build. |
| `parts_per_build` | How many parts the build makes. |
| `currency` | The currency code every amount in the record uses. |
| `tools` | One entry per tool (material line): name, material, color, grams or milliliters used, the line amount, and where its price came from. |
| `lines` | The full cost breakdown lines (material, electricity, depreciation, maintenance, labor, consumables, and any extras, overhead or failure allowance), each with its amount. |
| `updated_at` | When this record was written, UTC, ISO 8601. |

Each `tools` entry carries the price source twice: `price_source`, the
human label (for example "spool price (Spoolman spool 12)"), and
`price_source_code`, a stable code for machine readers: one of
`spoolman_spool` (that spool's own price in Spoolman),
`spoolman_filament` (the filament profile price in Spoolman),
`spoolman_bottle` (a resin bottle priced by volume in Spoolman),
`slicer` (the price embedded in the sliced file's profile), `manual`
(a price entered by hand) or `default` (the workspace default cost per
kilogram). The entry also carries the `spool_id` when a Spoolman spool
priced the line.

An example record, the frozen cost of a finished print:

```json
{
  "schema": 1,
  "state": "final",
  "file": "benchy.gcode",
  "outcome": "completed",
  "out_of_pocket": 1.54,
  "full_cost": 6.69,
  "cost_per_part": 6.69,
  "parts_per_build": 1,
  "currency": "USD",
  "tools": [
    {
      "name": "PLA Red",
      "material": "PLA",
      "color": "#FF0000",
      "grams": 50.0,
      "volume_ml": 0.0,
      "amount": 1.25,
      "price_source": "spool price (Spoolman spool 12)",
      "price_source_code": "spoolman_spool",
      "spool_id": 12
    }
  ],
  "lines": [
    {"key": "material", "label": "Material (incl. waste allowance)", "amount": 1.29},
    {"key": "electricity", "label": "Electricity", "amount": 0.07},
    {"key": "depreciation", "label": "Depreciation (over 5,000 lifetime hours)", "amount": 0.0},
    {"key": "maintenance", "label": "Maintenance", "amount": 0.15},
    {"key": "labor", "label": "Labor (prep per build, post per part, pack)", "amount": 5.0},
    {"key": "consumables", "label": "Consumables", "amount": 0.1},
    {"key": "failure", "label": "Failure allowance (5 percent, repeatable lines divided by 1 minus the rate)", "amount": 0.08}
  ],
  "updated_at": "2026-11-18T03:00:00Z"
}
```

## Defaults and presets

Workspace defaults: currency USD, electricity 0.19 per kWh, labor 20
per hour, failure 5 percent FDM and 10 percent resin, pricing mode
margin at 40 percent, tax 0, no marketplace fee, no overhead, no flat
fee, default filament 22 per kilogram, cost mode hobby, resin IPA 0.23
and cleanup/PPE 0.12 per print.

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
