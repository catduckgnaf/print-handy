# Troubleshooting: lookups and guided physical diagnosis

Print Handy keeps its troubleshooting knowledge in root-level registries so a
person browsing the repository and the bot read the same source (issue #169):

| Registry | What is in it |
| --- | --- |
| [`print_handy/knowledge/data/`](../print_handy/knowledge/data/README.md) | Symptom index and guided troubleshooting flows (first layer, clogs, heat creep, bed levelling/mesh, ringing). |
| [`print_handy/data/firmware-commands/`](../print_handy/data/firmware-commands/README.md) | Commands, macros and error codes for ZMod, Klipper and Marlin. |
| [`print_handy/data/printers-registry/`](../print_handy/data/printers-registry/README.md) | One data entry per registered printer model: hardware facts, firmware family, and the profile module that is the source of truth. |

The executable printer profiles did not move: they stay in
`print_handy/printers/`, and tests check the root printer registry against them.

## Look something up

In Discord (standalone bot or Hermes, same handlers):

```text
/print-lookup M600
/print-lookup zmod ifs load
/print-lookup bed mesh klipper
/print-lookup first layer not sticking
```

A lookup searches all three registries: firmware commands and macros (for
example the ZMod IFS load path, Klipper `PROBE_CALIBRATE`, Marlin `M851`), error
codes, symptoms, guided flows and printer facts. It returns the registry text —
command syntax, what it does, and its safety notes.

## Guided troubleshooting

```text
/print-troubleshoot clog
/print-troubleshoot extruder clicking
```

With no argument the bot lists the flows: `first-layer`, `clog`, `heat-creep`,
`bed-level`, `ringing` and `prepare-to-clean`. A flow opens with its safety warnings, then asks one
diagnostic question at a time (buttons, for example "Is filament curling up at
the nozzle tip?") and branches on the answer. Advice steps are single bounded
physical checks: one observation or one small change, then the next question.
"Stop here" ends the walkthrough at any question.

## Safety boundary

Lookups and troubleshooting are **read-only and advisory**. They never send a
command, heat, motion or extrusion to a printer, and there is no printer client
on that code path. Where a fix is itself an action — a cold pull is the example
in the clog flow — the flow points at the existing guarded command
(`/print-coldpull`), which asks for its own confirmation before doing anything.
The `prepare-to-clean` flow does the same with `/print-bed-down`, which
separates the bed and nozzle after a stop. `/print-lookup bed reset` ranks that
flow first, ahead of heater G-code.
Heater and thermal errors deliberately have no walkthrough past "power off,
cool down, inspect the wiring": that protection is doing its job.

## Adding or fixing knowledge

Edit the TOML in the registry that owns the fact, following its README schema,
and run `python3 -m pytest -q tests/test_knowledge.py`. The loader rejects
duplicate ids, dangling flow branches, symptoms pointing at missing flows or
firmware entries, and firmware entries naming unregistered printers, so a bad
edit fails tests instead of misleading someone at a printer.
