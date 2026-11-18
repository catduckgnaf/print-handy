"""The slash commands the standalone bot registers, and the help text it answers unmatched messages with.

Each slash command becomes the same text a person could type ("/print-stop"), so one handler serves both.
Slash registration models subcommands and typed options for real (#1475): the
values a person picks in Discord are mapped back to that same typed text by
``as_text``, so the typed forms in the help text keep working unchanged and
Telegram, Matrix, web chat and mention routing are untouched.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Option:
    name: str
    description: str
    choices: tuple[str, ...] = ()
    # "string" or "integer": the Discord option type. Handlers still receive
    # the typed text form, so this only shapes the picker a person sees.
    kind: str = "string"
    # Autocomplete provider key (bot.client.AUTOCOMPLETE_PROVIDERS): the
    # option's choices are offered live from the lists the bot already holds.
    autocomplete: str = ""
    required: bool = False


@dataclass(frozen=True)
class Subcommand:
    name: str
    description: str
    options: tuple[Option, ...] = ()
    # The word the typed form carries for this subcommand. "" means the
    # subcommand IS the command's bare typed form ("/spool" opens the guided
    # flow; "/print-firmware" alone is the versions report).
    text_word: str | None = None
    # How the subcommand's option values join in the typed form. "/spool new"
    # takes its spec comma-separated ("new brand, material, color, grams").
    joiner: str = " "

    @property
    def word(self):
        return self.name if self.text_word is None else self.text_word


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
    # Live viewing (#311) is a Discord Activity: a command marked here is
    # registered and advertised only on platforms whose adapter reports
    # supports_live_stream (P2-123) — other platforms must not offer a
    # command their handler deliberately refuses to run.
    live_stream: bool = False
    # Real Discord subcommands (#1475). When present, the command registers
    # as a group of these and ``options`` stays empty; ``as_text`` maps a
    # picked subcommand plus its typed option values back to the one typed
    # form the handlers have always parsed.
    subcommands: tuple[Subcommand, ...] = ()
    # Read-only commands answer ephemerally as a slash command (#1475): the
    # answer is the interaction's own response and only the asker sees it.
    ephemeral: bool = False
    # A command that acts on the printer with no confirmation of its own
    # (#1524 review: /print-pause). Every surface that would run it from
    # something short of typing it (a bare mention word, the help menu,
    # a did-you-mean "yes") reads this flag instead of a name list.
    at_once: bool = False


COMMANDS = (
    Command("print-next", "Next plate of a multi-plate plan (asks first)",
            (Option("plan_id", "Plan from the completion message", autocomplete="plan", required=True),
             Option("part_id", "A specific plate (optional)", autocomplete="plate")), "/print-next <plan_id> [part_id]"),
    Command("print-stop", "Stop the current print (asks first)", usage="/print-stop"),
    Command("print-pause", "Pause the print", usage="/print-pause", at_once=True),
    Command("print-resume", "Resume a paused print (asks first)", usage="/print-resume"),
    Command("print-status", "Current print progress (read-only)",
            (Option("printer", "Printer (optional; defaults to this channel's printer)",
                    autocomplete="printer"),),
            "/print-status", ephemeral=True),
    Command("print-again", "Reprint or list past jobs",
            (Option("query", "list, a number from the list, a name, or 'next'; add x3 for copies",
                    autocomplete="job"),),
            "/print-again [list | N | name]"),
    Command("print-reboot", "Safe printer restart (asks first)", usage="/print-reboot"),
    Command("print-coldpull", "Guided cold pull (asks first)", usage="/print-coldpull"),
    Command("print-bed-down", "Prepare to clean (asks first)",
            usage="/print-bed-down"),
    Command("nozzle", "Nozzle change (heats only after confirmation)",
            usage="/nozzle change",
            subcommands=(
                Subcommand("change", "Guided nozzle change (heats only after confirmation)"),
            )),
    Command("print-maintenance", "Maintenance checklist",
            (Option("mode", "full or quick", ("full", "quick")),), "/print-maintenance [full | quick]"),
    Command("print-slice-result", "Slice result card (read-only)",
            (Option("job", "A past job (optional; defaults to the newest sliced job)",
                    autocomplete="job"),),
            "/print-slice-result [job]", ephemeral=True),
    Command("print-colors", "Part colors for an archived job",
            (Option("job", "A past job (start typing to pick one)", autocomplete="job", required=True),),
            "/print-colors <job>"),
    Command("spool", "Guided spool add/change, list or sync",
            usage="/spool [list | change | new | rename | archive | sync]",
            subcommands=(
                Subcommand("guided", "Guided spool add/change (asks step by step)", text_word=""),
                Subcommand("list", "List loaded and shelf spools"),
                Subcommand("change", "Load a spool into a slot",
                           (Option("slot", "Slot number", kind="integer", autocomplete="slot", required=True),
                            Option("spool", "Spool number", kind="integer", autocomplete="spool", required=True))),
                Subcommand("new", "New spool: brand, material, color, grams",
                           (Option("brand", "Brand, e.g. Sunlu", required=True),
                            Option("material", "Material, e.g. PLA+", required=True),
                            Option("color", "Color, e.g. Matte White", required=True),
                            Option("grams", "Full starting weight in grams", kind="integer", required=True)),
                           joiner=", "),
                Subcommand("weight", "Correct a spool's grams left",
                           (Option("spool", "Spool number", kind="integer", autocomplete="spool", required=True),
                            Option("grams", "Grams left", kind="integer", required=True))),
                Subcommand("shelve", "Put a slot's spool back on the shelf",
                           (Option("slot", "Slot number", kind="integer", autocomplete="slot", required=True),)),
                Subcommand("rename", "Give one spool its own name",
                           (Option("spool", "Spool number", kind="integer", autocomplete="spool", required=True),
                            Option("name", "New name (empty clears it)", required=True))),
                Subcommand("archive", "Archive a spool that is not loaded",
                           (Option("spool", "Spool number", kind="integer", autocomplete="spool", required=True),)),
                Subcommand("unarchive", "Bring an archived spool back to the shelf",
                           (Option("spool", "Spool number", kind="integer", required=True),)),
                Subcommand("sync", "Sync spool screens with Spoolman"),
            )),
    Command("print-spools", "Loaded filament and amounts (read-only)",
            usage="/print-spools", ephemeral=True),
    Command("print-lookup", "Look up a command, macro or error (read-only)",
            (Option("query", "e.g. M600, zmod ifs load, bed mesh klipper, first layer not sticking"),),
            "/print-lookup <query>"),
    Command("print-troubleshoot", "Troubleshooting flows (advice only)",
            (Option("flow", "Which problem", ("first-layer", "clog", "heat-creep", "bed-level", "ringing", "prepare-to-clean")),),
            "/print-troubleshoot [flow]"),
    Command("print-macros", "List or run printer macros (asks first)",
            usage="/print-macros [run <name> [KEY=VALUE ...]]",
            subcommands=(
                Subcommand("list", "List the printer's macros", text_word=""),
                Subcommand("run", "Run one macro (asks first)",
                           (Option("name", "Macro name", autocomplete="macro", required=True),
                            Option("args", "Optional KEY=VALUE pairs"))),
            )),
    Command("print-firmware", "Firmware versions and updates (asks first)",
            usage="/print-firmware [update|channel|guide]",
            subcommands=(
                Subcommand("versions", "Firmware versions and update state", text_word=""),
                Subcommand("update", "Update one component (asks first)",
                           (Option("component", "Component name, e.g. zmod, klipper, moonraker, helixscreen",
                                   required=True),)),
                Subcommand("channel", "Switch one component's update channel (asks first)",
                           (Option("component", "Component name, e.g. helixscreen, klipper, moonraker",
                                   required=True),
                            Option("channel", "New channel", ("stable", "beta", "dev"),
                                   required=True))),
                Subcommand("guide", "Firmware update guide",
                           (Option("flavor", "Which firmware", ("zmod", "stock"), required=True),)),
            )),
    Command("print-calibration", "Calibrations; saves only on accept",
            usage="/print-calibration [mode]",
            subcommands=(
                Subcommand("mesh", "Bed mesh calibration"),
                Subcommand("screws", "Bed screw calibration"),
                Subcommand("pid", "PID tuning",
                           (Option("target", "What to tune", ("extruder", "bed"), required=True),
                            Option("temp", "Temperature (optional)", kind="integer"))),
                Subcommand("shaper", "Input shaper calibration"),
                Subcommand("belts", "Belt tension calibration"),
                Subcommand("zoffset", "Z offset calibration"),
                Subcommand("records", "Past calibration records"),
            )),
    Command("print-live", "Live camera in voice chat (Discord only)",
            (Option("action", "start, stop or status", ("start", "stop", "status")),),
            "/print-live [start | stop | status]", live_stream=True),
    Command("print-plate-calibrate", "Calibrate plate reference",
            (Option("action", "capture or status", ("capture", "status")),),
            "/print-plate-calibrate"),
    Command("print-design", "Design or edit a model with AI (asks first)",
            (Option("prompt", "What to make or change, e.g. a 40x20x3 plate with two 4 mm holes, or 'make it taller' with a file dropped"),),
            "/print-design <what to make or change>", aliases=("design",)),
    Command("print-handy", "Setup and read-only debugging",
            (Option("action", "setup, add-printer, debugging, bundle, debug, status, cancel or help",
                    ("setup", "add-printer", "debugging", "bundle", "debug", "status",
                     "cancel", "help")),
             # Free-form words after the action (P2-186): 'debug on 5' needs
             # them, and a fixed-choice action alone can only ever send
             # '/print-handy debug' (the status form).
             Option("args", "Extra words after the action — for debug: on, off, or on 5 (minutes)")),
            "/print-handy [setup | debug | status | help]"),
    Command("print-cost", "Print cost (read-only)",
            (Option("query", "Grams and hours, e.g. '50 g, 3 h, in PETG'; blank asks about the last print"),),
            "/print-cost", ephemeral=True),
    Command("print-source", "Connected model sites", usage="/print-source", ephemeral=True),
    Command("print-help", "What Print Handy can do", usage="/print-help", ephemeral=True),
)


def by_name(name):
    return next((c for c in COMMANDS if c.name == name or name in c.aliases), None)


def subcommand(command, name):
    return next((s for s in command.subcommands if s.name == name), None)


def registrations(supports_live_stream=True):
    """(name, command) for every slash registration: canonical names, then aliases.

    A platform whose adapter reports no live-stream support registers
    without the live command (P2-123); Discord, the default, registers all.
    """
    visible = [c for c in COMMANDS if supports_live_stream or not c.live_stream]
    return [(c.name, c) for c in visible] + \
        [(alias, c) for c in visible for alias in c.aliases]


def _option_text(value):
    """One option's typed form; only a missing option is empty, so 0 stays "0"."""
    return "" if value is None else str(value).strip()


def as_text(name, values):
    """The typed form of a slash command: '/spool change 1 12'.

    A picked subcommand maps to the typed words the handlers parse: the
    subcommand's word (empty for the bare form) followed by its option
    values in table order, joined the way that form's grammar joins them.
    """
    command = by_name(name)
    if command is not None and command.subcommands:
        sub = subcommand(command, str(values.get("subcommand") or ""))
        if sub is None:  # no subcommand picked: the bare typed form
            return "/" + name
        args = [_option_text(values.get(o.name)) for o in sub.options]
        args = [a for a in args if a]
        parts = ["/" + name]
        if sub.word:
            parts.append(sub.word)
        if args:
            parts.append(sub.joiner.join(args))
        return " ".join(parts)
    args = [_option_text(values.get(o.name)) for o in (command.options if command else ())]
    return " ".join(["/" + name] + [a for a in args if a])


# The five things people do most, shown by /print-help (#1475) with a menu
# for the rest instead of the whole command wall.
HELP_TOP_ACTIONS = (
    "Post a model link or a .3mf/.stl file — it prints from here, and nothing starts without a button press.",
    "`/print-status` — how the current print is going, with a camera frame.",
    "`/print-again` — reprint a past job (or list them).",
    "`/spool` — filament: what is loaded, change a spool, add a new one.",
    "`/print-stop` — stop the current print (it asks first).",
)

# Commands the help menu offers: everything except /print-help itself and
# the ones the top actions already name.
HELP_MENU_EXCLUDE = frozenset({"print-help", "print-status", "print-again", "spool", "print-stop"})


def help_menu_commands(supports_live_stream=True):
    # A menu pick runs the command on the spot, so a command with no
    # confirmation of its own is never offered there (#1524 review).
    return [c for c in COMMANDS if c.name not in HELP_MENU_EXCLUDE and not c.at_once
            and (supports_live_stream or not c.live_stream)]


def at_once_names():
    """The slash forms of the commands that act with no confirmation of their own."""
    return frozenset("/" + c.name for c in COMMANDS if c.at_once)


def short_help_text(bot_name="Print Handy"):
    lines = [f"**{bot_name}** prints from this channel. The five things people do most:"]
    lines += [f"- {action}" for action in HELP_TOP_ACTIONS]
    lines.append("Everything else is in the menu below — pick a command and it runs here.")
    return "\n".join(lines)


def help_text(bot_name="Print Handy", supports_live_stream=True):
    # The name appears once, and the whole text stays one Discord message
    # (2000 characters) for a bot_name of up to 32 characters.
    lines = [f"**{bot_name}** prints from this channel. Nothing starts without a button press.",
             "- Post a model link or .3mf/.stl.",
             '- "print it" or "print again" reprints the last link.',
             '- "add a new spool" or "add new filament" starts the guided /spool flow.',
             '- @mention me: "spool list", or just ask.']
    lines += [f"- `{c.usage}`: {c.description}"
              + (f" (also `/{c.aliases[0]}`)" if c.aliases else "")
              for c in COMMANDS if c.name != "print-help"
              and (supports_live_stream or not c.live_stream)]
    return "\n".join(lines)
