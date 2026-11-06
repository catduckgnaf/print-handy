"""Live printer macro discovery and guarded macro runs from Moonraker (issue #291).

Moonraker's ``GET /printer/objects/list`` returns one ``gcode_macro <NAME>``
object per macro defined in the printer's own Klipper config, so the printer —
not Print Handy's registry — is the authority on which macros exist. This
module fetches that list through an injected transport, extracts the macro
names and classifies each one against the descriptive registry in
``print_handy/data/firmware-commands/`` (loaded through print_handy.knowledge):

- **known** — the name matches a registry macro entry: show its description
  and safety note. When the caller names the printer (the /print-macros
  dispatch does), only entries scoped to that printer model — or to every
  printer — count: registry entries are per-printer facts, and a name-only
  match would show one firmware's description for another's macro of the
  same name (issue #612: a Sovol START_PRINT described as ZMod's). Without
  a printer, classification stays name-only for offline callers.
- **printer-defined** — the printer defines it but the registry does not:
  listed by name only; no description is invented for it.
- **internal** — the name starts with ``_`` (the Klipper helper convention):
  separated from the operator list, never presented as an operator macro, and
  never runnable from the bot.
- **user hook** — an underscore name the firmware documents as a hook for the
  operator to fill in (ZMod's ``_USER_PREPARE_PRINT``, #562). Listed so the
  operator knows it runs, never runnable from the bot: the firmware calls it.

Stage 1 (list) is read-only: its only request is the GET of the objects list.
Stage 2 (run) adds exactly one write: after the operator confirms the exact
script line, ``send_run`` re-checks the printer is not printing or paused and
POSTs that single line to ``/printer/gcode/script``. A run name is only ever
taken from the live discovered set — never trusted as typed, never free-typed
G-code — and internal macros are refused even when discovered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

OBJECTS_LIST_PATH = "/printer/objects/list"
GCODE_SCRIPT_PATH = "/printer/gcode/script"
PRINT_STATE_PATH = "/printer/objects/query?print_stats=state"
MACRO_PREFIX = "gcode_macro "

# Documented firmware hooks the operator edits and the firmware calls (#562).
# ZMod 1.7.5 runs _USER_PREPARE_PRINT before a file is sent to print, so it can
# change printer state after Print Handy's readiness checks.
USER_HOOKS = frozenset({"_USER_PREPARE_PRINT"})

COULD_NOT_READ_TEXT = ("Could not read the printer's macros from Moonraker. "
                       "Nothing was sent to the printer.")
READ_ONLY_NOTE = ("Read-only: this list came from the printer itself, and Print Handy sent nothing "
                  "but the objects-list request to build it. To run one of these macros, use "
                  "`/print-macros run <name>` — it asks first and refuses while a print is running.")
RUN_COULD_NOT_READ_TEXT = ("Could not read the printer's macros from Moonraker, so there is no "
                           "discovered set to run from. Nothing was sent to the printer.")
NOT_DESCRIBED_WORDING = ("This macro is defined by your printer's own config and is not described "
                         "by Print Handy — Print Handy cannot say what it does. Only run it if "
                         "you know what it does from the printer's config.")
RUN_LABEL = "Run macro now"
CANCEL_LABEL = "Cancel"

# Klipper print_stats states in which no job is on the printer, mirroring the
# cold-pull guard's allowed set. Anything else — printing, paused, or a state
# Print Handy does not recognise — refuses the run (fail closed).
IDLE_STATES = frozenset({"standby", "complete", "cancelled", "error"})
BUSY_STATES = frozenset({"printing", "paused"})

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ARG_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ARG_VALUE_RE = re.compile(r"-?[A-Za-z0-9][A-Za-z0-9._+-]*")


class MacroError(RuntimeError):
    """The printer's macros could not be read. Nothing was sent when this is raised."""


class MacroRefusal(RuntimeError):
    """A run was refused before anything was sent; the message is user-ready."""


@dataclass(frozen=True)
class MacroListing:
    """The discovered macros, classified. `known` pairs each name with its registry entry."""

    known: tuple = ()              # tuple[tuple[str, FirmwareEntry], ...]
    printer_defined: tuple = ()    # tuple[str, ...]
    internal: tuple = ()           # tuple[str, ...]
    user_hooks: tuple = ()         # tuple[str, ...]

    @property
    def names(self):
        return (tuple(name for name, _entry in self.known) + self.printer_defined
                + self.user_hooks + self.internal)


def parse_macro_names(payload) -> list:
    """Macro names from a Moonraker objects-list response, in the printer's order.

    Accepts the shape Moonraker actually returns (``{"result": {"objects":
    [...]}}``), a bare ``{"result": [...]}`` and a plain list, so recorded
    responses replay unchanged. Anything else is malformed and raises
    MacroError rather than guessing.
    """
    objects = payload
    if isinstance(payload, dict):
        if "result" not in payload:
            raise MacroError("Moonraker's objects-list response had no result")
        objects = payload["result"]
        if isinstance(objects, dict):
            objects = objects.get("objects")
    if not isinstance(objects, list):
        raise MacroError("Moonraker's objects-list response was not a list of objects")
    names: list[str] = []
    for item in objects:
        if not isinstance(item, str):
            raise MacroError("Moonraker's objects-list response held a non-string object")
        if item.startswith(MACRO_PREFIX):
            name = item[len(MACRO_PREFIX):].strip()
            if name and name not in names:
                names.append(name)
    return names


def _registry_index(registry, printer=None):
    """Case-insensitive name -> entry index over the registry's macro entries only.

    A macro entry is reachable by its registry name and by the macro name its
    command line starts with; a name written as several words (the IFS feed
    entry lists two macro names) indexes each word.

    With `printer` (a registered model key), only entries that apply to that
    printer — its `printers` list names the key, or "*" — are indexed, the
    same filter knowledge search applies. Without one, every macro entry is
    indexed by name, as before.
    """
    index = {}
    for entry in registry.firmware_entries:
        if entry.kind != "macro":
            continue
        if printer is not None and "*" not in entry.printers and printer not in entry.printers:
            continue
        keys = [token for token in re.split(r"[^A-Za-z0-9_]+", entry.name) if token]
        if entry.command:
            keys.append(entry.command.split()[0])
        for key in keys:
            index.setdefault(key.casefold(), entry)
    return index


def classify(names, registry=None, printer=None) -> MacroListing:
    """Split discovered names into known / printer-defined / internal.

    `printer` scopes the known match to entries for that printer model (see
    _registry_index): a discovered name with no entry for this printer is
    printer-defined, never described from another printer's registry entry.
    """
    if registry is None:
        from . import knowledge
        registry = knowledge.default_registry()
    index = _registry_index(registry, printer=printer)
    known, printer_defined, internal, user_hooks = [], [], [], []
    for name in names:
        if name.upper() in USER_HOOKS:
            user_hooks.append(name)
        elif name.startswith("_"):
            internal.append(name)
        elif name.casefold() in index:
            known.append((name, index[name.casefold()]))
        else:
            printer_defined.append(name)
    return MacroListing(tuple(known), tuple(printer_defined), tuple(internal), tuple(user_hooks))


def discover(transport, registry=None, printer=None) -> MacroListing:
    """Fetch and classify the printer's macros through `transport`.

    `transport` is the spool_sync.moonraker client shape: a callable taking
    (method, path) and returning the parsed JSON body. The only call made is
    one GET of the objects list. Any transport failure or malformed response
    raises MacroError; nothing is sent in that case (or any other).
    """
    try:
        payload = transport("GET", OBJECTS_LIST_PATH)
    except MacroError:
        raise
    except Exception as exc:
        raise MacroError(f"the objects-list request failed: {exc}") from exc
    return classify(parse_macro_names(payload), registry=registry, printer=printer)


# --------------------------------------------------------------------- stage 2: run

@dataclass(frozen=True)
class PreparedRun:
    """One validated run: the discovered name, its registry entry if known, the exact line."""

    name: str               # the macro name exactly as the printer reports it
    script: str             # the single script line the confirmation shows and the POST sends
    entry: object = None    # the registry FirmwareEntry for a known macro, else None


def print_state(transport) -> str:
    """The printer's print_stats state, case-folded, through `transport`.

    A response without a readable state is a MacroError, not a guess: the
    run guards treat "unreadable" exactly like "busy".
    """
    try:
        payload = transport("GET", PRINT_STATE_PATH)
    except MacroError:
        raise
    except Exception as exc:
        raise MacroError(f"the print-state request failed: {exc}") from exc
    try:
        state = payload["result"]["status"]["print_stats"]["state"]
    except (TypeError, KeyError):
        raise MacroError("Moonraker's print-state response had no print_stats state") from None
    if not isinstance(state, str) or not state.strip():
        raise MacroError("Moonraker's print-state response had no print_stats state")
    return state.strip().casefold()


def _require_idle(state, *, recheck=False):
    if state in IDLE_STATES:
        return
    when = "On the re-check just now, the" if recheck else "The"
    if state in BUSY_STATES:
        raise MacroRefusal(f"{when} printer is {state}, and a macro must not run while a print is "
                           "running or paused. Nothing was sent.")
    raise MacroRefusal(f"{when} printer reported an unrecognised state ({state!r}), so Print Handy "
                       "cannot confirm it is idle. Nothing was sent.")


def _validate_args(arg_tokens) -> tuple:
    """Validate KEY=VALUE tokens; return them as typed, in order.

    Values are restricted to one unquoted word of letters, digits and
    ``. _ + -`` (optionally led by ``-``), so a token can never smuggle a
    second command, a comment or whitespace into the script line.
    """
    validated, seen = [], set()
    for token in arg_tokens:
        key, sep, value = str(token).partition("=")
        if not sep or not _ARG_KEY_RE.fullmatch(key) or not _ARG_VALUE_RE.fullmatch(value or ""):
            raise MacroRefusal(f"{token!r} is not a KEY=VALUE argument. Arguments look like "
                               "`BED_TEMP=60` — one word each, no spaces, quotes or symbols "
                               "beyond letters, digits and . _ + -. Nothing was sent.")
        if key.casefold() in seen:
            raise MacroRefusal(f"The argument {key!r} was given twice. Nothing was sent.")
        seen.add(key.casefold())
        validated.append(str(token))
    return tuple(validated)


def prepare_run(transport, name_token, arg_tokens=(), registry=None, printer=None) -> PreparedRun:
    """Validate one run request against the live printer; send nothing.

    Re-discovers the macro set at run time (the printer is the authority —
    a name typed from memory is never trusted), resolves the typed name
    against the discovered names case-insensitively (Klipper's own macro
    name convention), refuses internal and unknown names, validates the
    KEY=VALUE arguments, and checks the printer is not printing or paused.
    The returned PreparedRun carries the exact script line the operator
    will be asked to confirm.
    """
    try:
        listing = discover(transport, registry=registry, printer=printer)
    except MacroError:
        raise MacroError(RUN_COULD_NOT_READ_TEXT) from None
    name_token = str(name_token or "").strip()
    if not _NAME_RE.fullmatch(name_token):
        raise MacroRefusal(f"{name_token!r} is not a macro name. Run one of the names "
                           "`/print-macros` lists, exactly as one word. Nothing was sent.")
    wanted = name_token.casefold()
    entry = None
    canonical = None
    for known_name, known_entry in listing.known:
        if known_name.casefold() == wanted:
            canonical, entry = known_name, known_entry
            break
    if canonical is None and any(hook.casefold() == wanted for hook in listing.user_hooks):
        raise MacroRefusal(f"{name_token} is a firmware hook: the printer runs it itself (ZMod "
                           "calls it before a file is sent to print). Edit it in the printer's "
                           "config; Print Handy never runs it. Nothing was sent.")
    if canonical is None and name_token.startswith("_"):
        if any(internal_name.casefold() == wanted for internal_name in listing.internal):
            raise MacroRefusal(f"{name_token} is an internal helper macro (names starting with _ "
                               "are the printer config's own machinery). Internal macros are never "
                               "runnable from Print Handy. Nothing was sent.")
    if canonical is None:
        for defined_name in listing.printer_defined:
            if defined_name.casefold() == wanted:
                canonical = defined_name
                break
    if canonical is None:
        raise MacroRefusal(f"The printer does not define a macro named {name_token!r}. Run "
                           "`/print-macros` to see the names it actually defines — a typed name "
                           "is never sent on trust. Nothing was sent.")
    args = _validate_args(arg_tokens)
    script = " ".join((canonical, *args))
    try:
        state = print_state(transport)
    except MacroError:
        raise MacroError("Could not read the printer's state from Moonraker, so Print Handy "
                         "cannot confirm it is idle. Nothing was sent.") from None
    _require_idle(state)
    return PreparedRun(name=canonical, script=script, entry=entry)


def run_prompt_text(prepared: PreparedRun) -> str:
    """The confirmation question: the exact line, plus what is known about the macro."""
    lines = [f"Run macro **{prepared.name}** on the printer?",
             "",
             "This sends exactly one line to the printer:",
             f"`{prepared.script}`",
             ""]
    if prepared.entry is not None:
        lines.append(f"Safety: {prepared.entry.safety}")
    else:
        lines.append(NOT_DESCRIBED_WORDING)
    lines.append("The printer's state is checked again immediately before sending; if a print "
                 "has started, nothing is sent.")
    return "\n".join(lines)


def send_run(transport, prepared: PreparedRun) -> str:
    """Re-check the printer is idle, then POST the one confirmed script line.

    The state can change while the operator decides, so the check in
    prepare_run is repeated here; a busy or unreadable re-check sends
    nothing. A failed POST is reported as failed/uncertain — Moonraker
    accepting the script is the only success this claims.
    """
    try:
        state = print_state(transport)
    except MacroError:
        raise MacroRefusal("Could not re-check the printer's state just now, so the macro was "
                           "not sent. Nothing was sent.") from None
    _require_idle(state, recheck=True)
    try:
        transport("POST", GCODE_SCRIPT_PATH, {"script": prepared.script})
    except Exception as exc:
        raise MacroError(f"The send did not succeed ({exc}). It is not known whether the printer "
                         "received or ran anything — check the printer before trying again.") from exc
    return (f"Sent `{prepared.script}` — Moonraker accepted the script. Print Handy cannot confirm "
            "the macro's own actions have finished, so watch the printer.")


def not_moonraker_text(connection) -> str:
    """The plain answer for a printer with no Moonraker macro namespace."""
    where = f" (it uses the {connection} connection)" if connection else ""
    return ("Macros are a Moonraker/Klipper feature: the printer defines them in its own config "
            f"and Moonraker lists them. This printer has no Moonraker macro namespace{where}, so "
            "there is no macro list to read. Nothing was sent to the printer.")


def listing_text(listing: MacroListing) -> str:
    """The chat-ready Stage 1 answer for one discovered listing."""
    if not listing.names:
        return ("The printer reported no macros in its Moonraker objects list.\n" + READ_ONLY_NOTE)
    lines = ["**Macros on your printer** (read live from Moonraker):"]
    if listing.known:
        lines.append("\nKnown to Print Handy:")
        for name, entry in listing.known:
            lines.append(f"- **{name}** — {entry.description}")
            lines.append(f"  Safety: {entry.safety}")
    if listing.printer_defined:
        lines.append("\nDefined by your printer's config (Print Handy has no description for these):")
        lines += [f"- {name}" for name in listing.printer_defined]
    if listing.user_hooks:
        lines.append("\nFirmware hooks you can fill in (the printer runs these itself; "
                     "ZMod's _USER_PREPARE_PRINT runs before a file is sent to print, after "
                     "Print Handy's readiness checks):")
        lines += [f"- {name}" for name in listing.user_hooks]
    if listing.internal:
        lines.append("\nInternal helper macros (part of the printer's own config machinery, "
                     "not operator macros):")
        lines += [f"- {name}" for name in listing.internal]
    lines.append("\n" + READ_ONLY_NOTE)
    return "\n".join(lines)
