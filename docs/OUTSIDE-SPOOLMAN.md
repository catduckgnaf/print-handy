# Working with an outside Spoolman

Print Handy keeps its filament inventory in Spoolman: which spools exist, which one is loaded in which slot, and how much is left on each. The Spoolman it talks to does not have to be the one bundled with Print Handy. This page covers the three places Spoolman can live, and the one rule that keeps filament accounting correct in all of them: **one accounting authority per printer**.

## The one rule: one accounting authority per printer

A Moonraker that is linked to a Spoolman charges filament usage itself, live, while a print runs. Print Handy also knows how to charge a finished print (that is how multi-slot prints are accounted, because Moonraker only ever charges the one active spool). If both sides charge the same print, the spool is counted twice and its remaining weight drops too fast.

So for every printer exactly one side counts:

- **The printer** counts when its Moonraker is linked to the Spoolman Print Handy uses. Moonraker charges live; Print Handy records the print in its usage ledger as skipped, with the reason named, and charges nothing.
- **Print Handy** counts when the printer has no Spoolman link of its own. Print Handy charges at the end of the print, through its usage ledger, so a print is never charged twice. A multi-slot print charges each slot's spool. A one-spool print on a printer whose Moonraker has no Spoolman link (Forge-X ships without a `[spoolman]` section, and so can any plain Klipper install) charges the spool recorded as loaded when the print started, set with `/spool`. A completed print charges the sliced grams; a cancelled or stopped one charges its share, scaled by the filament it actually used. If no spool is recorded as loaded, the launch says so and nothing is charged.

The choice is a setting, shown with the rest of the spool settings:

- `[spool] accounting` (environment: `PRINT_HANDY__SPOOL__ACCOUNTING`) is the install-wide choice and the default printer's: `auto`, `print_handy`, or `printer`. It is also on the Web UI settings page, in the Spools group, and the guided setup asks it right after the Spoolman URL.
- A `[[printers]]` entry can override both halves for one printer: `spoolman_url` names that printer's own Spoolman instance, and `spoolman_accounting` its accounting choice. An entry that sets neither uses the shared Spoolman and the shared choice.

`auto` decides from the printer itself: Print Handy asks the printer's Moonraker for its loaded config (`/server/config`) and its active-spool endpoint (`/server/spoolman/spool_id`). If Moonraker names a `[spoolman]` server, or the endpoint answers at all, the printer is linked. A link alone does not hand counting to the printer. `auto` makes the printer count only when it is linked and its firmware picks the active spool itself at each tool change, which today means OpenCreator with AFC. On every other printer `auto` keeps Print Handy counting, as it always has: Print Handy points Moonraker at the job's spool for a one-spool print, and for a multi-slot print it clears that pointer and charges each slot itself when the print ends. The decision is made when a print starts and is recorded on the print, so the end-of-print charge follows the link as it was at launch, not as it happens to be later.

The printer check (`printer_state check`, and the status surfaces that use it) reports the detection and the resolved authority for every Moonraker printer, and warns about the combinations that cannot work:

- the printer is set to count, but no Moonraker Spoolman link was detected: nothing will count usage until one side is fixed;
- Moonraker points at one Spoolman instance while Print Handy is configured for a different one: two inventories are in play, and if Print Handy is also set to count, the same print can be charged by both.

One detail for single-spool printers with a linked Moonraker: Moonraker's live charge is checked against the launch baseline and corrected only when it runs away (it charged far more than the print used). That correction only ever undoes an overcharge; it never adds a second one, so it stays on under either authority.

## Case 1: the bundled Spoolman

Nothing to set up. The Docker stack runs Spoolman beside Print Handy and `services.spoolman_url` already points at it. `auto` resolves to Print Handy counting, which is the long-standing behavior, whether or not the printer's Moonraker is linked to the bundled Spoolman.

## Case 2: an outside Spoolman on another host

You already run Spoolman somewhere else (a NAS, another server, a shared inventory for several tools) and want Print Handy to use it.

Setup:

1. Point Print Handy at it: `services.spoolman_url` (environment: `PRINT_HANDY__SERVICES__SPOOLMAN_URL`), or answer the Spoolman URL step in guided setup. For one printer only, set `spoolman_url` on that printer's `[[printers]]` entry instead.
2. Decide who counts, per the rule above: if no printer's Moonraker links to that Spoolman, leave accounting on `auto` (Print Handy counts). If a printer's Moonraker links to it and you want the printer to count, set that printer's `spoolman_accounting` to `printer`; `auto` does that by itself only for firmware that picks the active spool itself (OpenCreator).
3. Run `/spool sync` once (or the Web UI spools page Sync, or `POST /api/printers/<id>/spools/sync`). It converges the loaded-spool assignments both ways. A spool only Print Handy's record shows as loaded is written to Spoolman as loaded (its `ph_slot` claim), unless Spoolman no longer has it or has archived it; then the reply names it and the slot follows Spoolman. Then Spoolman's claims rebuild the local record, and remaining weights are taken from the Spoolman records. When the two sides name different spools for one slot, the reply names the conflict and Spoolman's claim wins; rebind the slot if that is wrong. Because a local-only slot is written back, a spool you unloaded in Spoolman's own screen comes back as loaded if Print Handy's record still holds it; unload it in Print Handy (`/spool shelve <slot>`) instead.

Print Handy's own spool features keep working against an outside Spoolman:

- The spool picker, the shelf, and archive use Spoolman's own fields (location and the archived flag), so they work the same as with the bundled instance.
- A spool's own nickname (`/spool rename`) lives in a Spoolman extra field, `ph_nickname`, which Print Handy creates on first use. An outside Spoolman may refuse to create extra fields, because it is not Print Handy's to reconfigure. The rename still works then: the nickname is kept in a local store on the Print Handy install instead, and reads fall back to it. The one limit is that the nickname is local to that install; another Print Handy pointed at the same Spoolman will not see it.
- Loaded-slot claims (`ph_slot`) are written best-effort for the same reason; `/spool sync` repairs a claim an outside Spoolman refused.

The Spoolman API has no authentication. Keep an outside Spoolman on your LAN or a private network, never forwarded from a router.

## Case 3: the printer's own Moonraker Spoolman

Some firmware setups link Moonraker to a Spoolman themselves. OpenCreator with its AFC toolchanger is the standing example: the firmware points Moonraker at each tool's spool as tools change, and Moonraker charges usage live, per tool. A Klipper and Moonraker install with a hand-written `[spoolman]` section charges whatever spool is active, but nothing on the printer changes the active spool per tool, so `auto` leaves counting with Print Handy there.

Setup:

1. Point Print Handy at the same instance the printer's Moonraker uses: `services.spoolman_url`, or that printer entry's `spoolman_url`. Print Handy and the printer must read and write one inventory, or loaded spools and remaining weights cannot line up.
2. Set accounting so the printer counts: on OpenCreator `auto` does this once it detects the link. On any other printer, set `spoolman_accounting` to `printer` on the printer's entry. Print Handy then never charges that printer's prints at the end, and on a multi-slot start it leaves Moonraker's active spool alone instead of clearing it.
3. Check the printer: the printer check names the detected link (which instance Moonraker points at) and the resolved authority. If it warns that Moonraker points at a different instance than Print Handy is configured for, fix the URL from step 1 before printing.

## Settings summary

| Setting | Where | Values |
| --- | --- | --- |
| `spool.accounting` | `[spool]`, `PRINT_HANDY__SPOOL__ACCOUNTING`, Web UI Spools group | `auto` (default), `print_handy`, `printer` |
| `printers.spoolman_url` | one `[[printers]]` entry | that printer's own Spoolman URL; empty uses the shared one |
| `printers.spoolman_accounting` | one `[[printers]]` entry | same values; empty uses `spool.accounting` |

See [Settings](SETTINGS.md) for the full reference rows.
