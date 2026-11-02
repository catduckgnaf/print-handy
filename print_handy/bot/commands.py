"""The slash commands the standalone bot registers, and the help text it answers unmatched messages with.

Each slash command becomes the same text a person could type ("/print-stop"), so one handler serves both.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Option:
    name: str
    description: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    options: tuple[Option, ...] = ()
    usage: str = ""
    # Short names for the same command (#388 close-out): an alias registers,
    # parses and answers exactly like the canonical name — slash registration,
    # the typed form and help all derive from this one table entry.
    aliases: tuple[str, ...] = ()


COMMANDS = (
    Command("print-next", "Next plate of a multi-plate plan (asks first)",
            (Option("plan_id", "Plan id from the completion message"),
             Option("part_id", "A specific plate (optional)")), "/print-next <plan_id> [part_id]"),
    Command("print-stop", "Stop the current print (asks first)", usage="/print-stop"),
    Command("print-resume", "Resume a paused print (asks first)", usage="/print-resume"),
    Command("print-again", "Reprint a past job, or list them",
            (Option("query", "list, a number from the list, a name, or 'next'; add x3 for copies"),),
            "/print-again [list | N | name]"),
    Command("print-reboot", "Safe printer restart (asks first)", usage="/print-reboot"),
    Command("print-coldpull", "Guided cold pull (asks first)", usage="/print-coldpull"),
    Command("nozzle", "Nozzle change (heats only after confirmation)",
            (Option("args", "change"),),
            "/nozzle change"),
    Command("print-maintenance", "Maintenance checklist",
            (Option("mode", "full or quick", ("full", "quick")),), "/print-maintenance [full | quick]"),
    Command("print-colors", "Choose part colors for an archived job",
            (Option("job", "Path to the archived JOB.json"),), "/print-colors <JOB.json>"),
    Command("spool", "Guided spool add/change, list or sync",
            (Option("args", "guided when blank | list | change <slot> <spool#> | new brand, material, color, grams | sync"),),
            "/spool [guided | list | change <slot> <spool#> | new ... | sync]"),
    Command("print-spools", "Loaded filament and amounts (read-only)",
            usage="/print-spools"),
    Command("print-lookup", "Look up a command, macro, error or symptom (read-only)",
            (Option("query", "e.g. M600, zmod ifs load, bed mesh klipper, first layer not sticking"),),
            "/print-lookup <query>"),
    Command("print-troubleshoot", "Troubleshooting (advice only)",
            (Option("flow", "Which problem", ("first-layer", "clog", "heat-creep", "bed-level", "ringing")),),
            "/print-troubleshoot [first-layer | clog | heat-creep | bed-level | ringing]"),
    Command("print-macros", "List or run printer macros (asks first)",
            (Option("args", "run <name> [KEY=VALUE ...]"),),
            "/print-macros [run <name> [KEY=VALUE ...]]"),
    Command("print-firmware", "Firmware versions and updates (asks first)",
            (Option("args", "update <component> | guide zmod | guide stock"),),
            "/print-firmware [update <name> | guide <zmod|stock>]"),
    Command("print-calibration", "Calibrations; saves only on accept",
            (Option("args", "mesh | screws | pid extruder|bed [temp] | shaper | belts | zoffset | records"),),
            "/print-calibration [mesh|screws|pid|shaper|belts|zoffset|records]"),
    Command("print-live", "Live camera in the voice channel (Discord only)",
            (Option("action", "start, stop or status", ("start", "stop", "status")),),
            "/print-live [start | stop | status]"),
    Command("print-design", "Design or edit a model with AI (asks before slicing)",
            (Option("prompt", "What to make or change, e.g. a 40x20x3 plate with two 4 mm holes, or 'make it taller' with a file dropped"),),
            "/print-design <what to make or change>", aliases=("design",)),
    Command("print-handy", "Setup and read-only debugging (never prints)",
            (Option("action", "setup, add-printer, debugging, bundle, debug, status, cancel or help",
                    ("setup", "add-printer", "debugging", "bundle", "debug", "status",
                     "cancel", "help")),
             # Free-form words after the action (P2-186): 'debug on 5' needs
             # them, and a fixed-choice action alone can only ever send
             # '/print-handy debug' (the status form).
             Option("args", "Extra words after the action — for debug: on, off, or on 5 (minutes)")),
            "/print-handy [setup | add-printer | debug on|off [minutes] | status | help]"),
    Command("print-source", "Which model sites are connected", usage="/print-source"),
    Command("print-help", "What Print Handy can do", usage="/print-help"),
)


def by_name(name):
    return next((c for c in COMMANDS if c.name == name or name in c.aliases), None)


def registrations():
    """(name, command) for every slash registration: canonical names, then aliases."""
    return [(c.name, c) for c in COMMANDS] + \
        [(alias, c) for c in COMMANDS for alias in c.aliases]


def as_text(name, values):
    """The typed form of a slash command: '/spool change 1 12'."""
    command = by_name(name)
    args = [str(values.get(o.name) or "").strip() for o in (command.options if command else ())]
    return " ".join(["/" + name] + [a for a in args if a])


def help_text(bot_name="Print Handy"):
    lines = [f"**{bot_name}** prints from this channel. Nothing starts without a button press.",
             "- Post a model link or a .3mf/.stl to print; `/print-source` lists the sites.",
             '- "print it" after a link, or "print the last one again", reuses the last model.',
             '- "print 57" (or "print #57") reprints print #57, like "/print-again 57".',
             '- "add new filament" / "add a new spool" / "swap the filament" starts the guided /spool flow.']
    lines += [f"- `{c.usage}`: {c.description}"
              + (f" (also `/{c.aliases[0]}`)" if c.aliases else "")
              for c in COMMANDS if c.name != "print-help"]
    return "\n".join(lines)
