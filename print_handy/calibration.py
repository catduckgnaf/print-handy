"""Printer calibration with a before/after accept gate, through Moonraker (issue #565).

Print Handy's calibration pattern — the pattern is the product — is the
same for every driven calibration in this module:

1. Read and present the BEFORE values.
2. The operator confirms, with the plain heat / motion / noise /
   attendance warnings for that calibration stated up front.
3. Run — idle-only: ``print_stats`` printing, paused or unreadable
   refuses before anything is sent (fail closed), checked at prepare
   time and again immediately before the run is sent.
4. Poll for the result (objects query / ``/server/gcode_store``), never
   one blocking HTTP call; polling is injectable so tests run instantly.
5. Present the NEW values AND the numeric differences (old -> new,
   signed deltas).
6. Accept / Discard: on Accept exactly the documented save is sent once
   (with a warning first when the save restarts Klipper); on Discard the
   documented revert is sent, or nothing when nothing was applied — and
   never a save.
7. A calibration record is written either way (see :func:`append_record`).

The per-calibration primitives and save semantics are the Stage 0
evidence posted on issue #565 (ZMod wiki Calibrations/SetupCalibrations,
Klipper docs and source, Moonraker). The load-bearing facts:

- ZMod's ``PID_TUNE_EXTRUDER`` / ``PID_TUNE_BED`` macros auto-SAVE_CONFIG,
  so the PID flows here never send them. Raw ``PID_CALIBRATE
  HEATER=<heater> TARGET=<t>`` does not auto-save: it applies the tuned
  values in memory, stores them for SAVE_CONFIG and reports ``PID
  parameters: pid_Kp=... pid_Ki=... pid_Kd=...`` on the console. Accept
  is therefore SAVE_CONFIG; Discard is FIRMWARE_RESTART, which drops the
  in-memory values.
- A bed mesh is measured without saving (``AUTO_FULL_BED_LEVEL SAVE=0``
  on screenless ZMod, ``BED_MESH_CALIBRATE`` on generic Klipper — both
  leave the mesh in memory only), into a scratch profile. Accept copies
  it with ``BED_MESH_PROFILE SAVE=`` into the profile prints load
  (``auto`` on ZMod, ``default`` on Klipper) plus ``SAVE_CONFIG``;
  Discard removes the scratch profile and reloads the previously active
  profile (or clears the mesh), and nothing is ever written.
- Input shaper calibration (``ZSHAPER`` on ZMod, ``SHAPER_CALIBRATE``
  on generic Klipper) only *recommends* values on the console; they
  persist solely through SAVE_CONFIG. Accept is SAVE_CONFIG; Discard
  sends nothing (the recommendation stays pending until the next
  restart, which the result text says out loud).
- Z offset is report + guide on every ZMod install mode (Stage 0
  verdict): with the native screen the screen owns the offset (its own
  store, adjusted during printing, saved by the screen; the wiki says
  adjusting it through the web interface is not advisable and
  ``Z_OFFSET_APPLY_ENDSTOP`` must not be used); screenless, ZMod saves
  in-print web adjustments itself once ``LOAD_ZOFFSET=1`` is armed, and
  ``PROBE_CALIBRATE`` is explicitly not the mechanism. Generic Klipper's
  ``PROBE_CALIBRATE`` is an interactive TESTZ paper-test dialogue the
  bot cannot drive — guide only.
- Bed screws: ZMod's ``BED_LEVEL_SCREWS_TUNE`` waits for its own
  confirmation at the printer, so it is a guide on ZMod. Generic
  Klipper's ``SCREWS_TILT_CALCULATE`` is one non-interactive command
  whose console lines name each screw's probed height and the exact
  turn, so it is driven as a measure -> adjust -> re-measure loop where
  ``[screws_tilt_adjust]`` is configured. Nothing is ever saved for
  screws — the change is physical.
- Belts (``BELTS_SHAPER_CALIBRATION``) apply nothing: a confirmed,
  warned run whose console output is reported back. Report-only.

Eligibility mirrors the firmware rule (#561): Moonraker connection plus
firmware family ``zmod`` or vanilla ``klipper``; vendor forks (Elegoo,
Creality, QIDI, Anycubic) are refused with a plain explanation. Beyond
eligibility, a calibration is offered only when live discovery (objects
list / configfile settings) shows its primitive actually exists on that
printer — never assumed from the model alone.

The transport is the spool_sync.moonraker client shape: a callable
taking ``(method, path, body=None)`` and returning the parsed JSON body.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

OBJECTS_LIST_PATH = "/printer/objects/list"
GCODE_SCRIPT_PATH = "/printer/gcode/script"
PRINTER_INFO_PATH = "/printer/info"
GCODE_STORE_PATH = "/server/gcode_store"
PRINT_STATE_PATH = "/printer/objects/query?print_stats=state"
SETTINGS_PATH = "/printer/objects/query?configfile=settings"
FACTS_PATH = ("/printer/objects/query?configfile=settings&bed_mesh"
              "&input_shaper&save_variables")
NATIVE_CONFIG_PATH = "/server/files/config/Adventurer5M.json"
MACRO_PREFIX = "gcode_macro "

COULD_NOT_READ_TEXT = ("Could not read the printer's calibration state from Moonraker. "
                       "Nothing was sent to the printer.")
RUN_LABEL = "Run calibration"
ACCEPT_LABEL = "Accept — save these values"
DISCARD_LABEL = "Discard — don't save"
CANCEL_LABEL = "Cancel"
MEASURE_AGAIN_LABEL = "I adjusted them — measure again"
FINISH_LABEL = "Done — the bed is level"
UNANSWERED_TEXT = ("No answer came in time, so Print Handy sent nothing and saved nothing. "
                   "The new values stay in the printer's memory only until Klipper "
                   "restarts (restarting the printer drops them) — avoid an unrelated "
                   "SAVE_CONFIG before then, because it would save them too.")

RECORDS_NAME = "calibration_records.json"
# The measured mesh lands in this scratch profile first, so Discard can
# reload the untouched saved profile; Accept copies it into the profile
# the printer's prints actually load and removes the scratch one.
MEASURE_PROFILE = "print_handy"
# The profile prints load: ZMod's START_PRINT loads MESH=auto (the same
# name as core/model_job.MESH_PROFILE and its saved-mesh readiness check);
# vanilla Klipper loads "default" at startup.
MESH_PROFILES = {"zmod": "auto", "klipper": "default"}
# The screenless mesh contract (config/mesh-interface.json): EXTRUDER_TEMP=220
# BED_TEMP=60, not the macro's own defaults.
ZMOD_MESH_EXTRUDER_TEMP = 220
ZMOD_MESH_BED_TEMP = 60
# Offered PID targets, default first — the choice stays in the flow.
EXTRUDER_PID_TARGETS = (240, 200, 255)
BED_PID_TARGETS = (60, 80, 110)

# Klipper print_stats states in which no job is on the printer — the same
# set the firmware and macro guards use. Anything else — printing,
# paused, or a state Print Handy does not recognise — refuses (fail closed).
IDLE_STATES = frozenset({"standby", "complete", "cancelled", "error"})
BUSY_STATES = frozenset({"printing", "paused"})

ELIGIBLE_FAMILIES = frozenset({"zmod", "klipper"})
FORK_VENDORS = frozenset({"elegoo", "creality", "qidi", "anycubic", "snapmaker"})

# Macros the PID flows must never send: both auto-SAVE_CONFIG, which
# would destroy the accept gate (Stage 0 evidence, ZMod wiki).
NEVER_SEND = ("PID_TUNE_EXTRUDER", "PID_TUNE_BED")


class CalibrationError(RuntimeError):
    """The printer's calibration state could not be read, or a run failed."""


class CalibrationRefusal(RuntimeError):
    """A calibration was refused before anything was sent; the message is user-ready."""


# --------------------------------------------------------------------- profiles

def printer_entry(model_key, registry=None):
    """The printer registry entry for a model key, or None when unregistered."""
    from . import firmware_update
    return firmware_update.printer_entry(model_key, registry=registry)


def block_reason(entry) -> str | None:
    """Why calibrations are not offered for this registry entry, or None when they are.

    Same rule shape as #561's firmware eligibility: Moonraker connection,
    upstream firmware family, never a vendor fork.
    """
    if entry is None:
        return ("Print Handy does not have this printer's profile in its registry, so it "
                "cannot judge whether driving its calibrations is safe.")
    if entry.connection != "moonraker":
        return (f"This printer does not connect over Moonraker (it uses the "
                f"{entry.connection} connection), and Print Handy has no calibration "
                f"path for that connection.")
    if entry.vendor in FORK_VENDORS:
        return (f"This printer runs {entry.vendor}'s own Klipper-based firmware — a vendor "
                f"fork, not upstream Klipper. Its calibration macros and save behavior "
                f"are the vendor's own, so Print Handy does not drive calibrations on it.")
    if entry.firmware_family not in ELIGIBLE_FAMILIES:
        return (f"This printer's firmware family ({entry.firmware_family or 'none'}) is not "
                f"one Print Handy can calibrate: only ZMod and vanilla Klipper printers "
                f"are eligible.")
    return None


def is_eligible(entry) -> bool:
    return block_reason(entry) is None


def not_moonraker_text(connection) -> str:
    """The plain answer for a printer with no Moonraker calibration path."""
    where = f" (it uses the {connection} connection)" if connection else ""
    return (f"Calibrations are read and run through Moonraker. This printer has no "
            f"Moonraker connection{where}, so Print Handy cannot run its calibrations. "
            "Nothing was sent to the printer.")


# --------------------------------------------------------------------- printer state

def print_state(transport) -> str:
    """The printer's print_stats state, case-folded, through `transport`.

    Same contract as the firmware/macro guards: a response without a
    readable state is a CalibrationError, not a guess.
    """
    try:
        payload = transport("GET", PRINT_STATE_PATH)
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"the print-state request failed: {exc}") from exc
    try:
        state = payload["result"]["status"]["print_stats"]["state"]
    except (TypeError, KeyError):
        raise CalibrationError("Moonraker's print-state response had no print_stats state") from None
    if not isinstance(state, str) or not state.strip():
        raise CalibrationError("Moonraker's print-state response had no print_stats state")
    return state.strip().casefold()


def _require_idle(state, *, recheck=False):
    if state in IDLE_STATES:
        return
    when = "On the re-check just now, the" if recheck else "The"
    if state in BUSY_STATES:
        raise CalibrationRefusal(f"{when} printer is {state}, and a calibration must not "
                                 "run while a print is running or paused. Nothing was sent.")
    raise CalibrationRefusal(f"{when} printer reported an unrecognised state ({state!r}), "
                             "so Print Handy cannot confirm it is idle. Nothing was sent.")


def _require_idle_now(transport, *, recheck=False):
    try:
        state = print_state(transport)
    except CalibrationError:
        raise CalibrationError("Could not read the printer's state from Moonraker, so "
                               "Print Handy cannot confirm it is idle. Nothing was sent.") from None
    _require_idle(state, recheck=recheck)


def _send_script(transport, line, *, long_running=False):
    """POST exactly one gcode script line; a failure is reported as uncertain.

    Moonraker answers /printer/gcode/script only once the gcode finishes,
    so a calibration that runs for minutes outlives the transport's read
    timeout while the printer keeps running it. With `long_running`, a
    read timeout after the request went out (a bare TimeoutError — urllib
    wraps connect/send failures in URLError) counts as sent, and the
    caller's completion polling collects the result. A failure before the
    request was sent stays a failure.
    """
    try:
        transport("POST", GCODE_SCRIPT_PATH, {"script": line})
    except TimeoutError as exc:
        if not long_running:
            raise CalibrationError(f"The send did not succeed ({exc}). It is not known "
                                   "whether the printer received or ran anything — "
                                   "check the printer before trying again.") from exc
    except Exception as exc:
        raise CalibrationError(f"The send did not succeed ({exc}). It is not known whether "
                               "the printer received or ran anything — check the printer "
                               "before trying again.") from exc


def _is_restart_disconnect(exc) -> bool:
    """Whether a send failure is the connection dropping as Klipper restarts.

    SAVE_CONFIG restarts Klipper while Moonraker still holds the request:
    Moonraker answers 503 "Klippy Disconnected", or the connection is cut.
    A failure from BEFORE the request went out is not that signature: a
    refused connection means nothing ever reached Moonraker, and a
    timeout urllib wrapped in URLError is a connect timeout (a bare
    TimeoutError is the post-send read timeout — see _send_script), so
    neither may confirm a save that never happened.
    """
    if getattr(exc, "code", None) == 503:
        return True
    reason = getattr(exc, "reason", None)
    failure = reason if isinstance(reason, BaseException) else exc
    if isinstance(failure, ConnectionRefusedError):
        return False
    if failure is not exc and isinstance(failure, TimeoutError):
        return False  # wrapped by urllib: a connect timeout, pre-send
    return isinstance(failure, (ConnectionError, TimeoutError))


def _save_config(transport, *, attempts=45, interval_s=2.0, sleep=time.sleep):
    """Send SAVE_CONFIG once, then wait for Klipper to come back ready.

    SAVE_CONFIG writes printer.cfg and restarts Klipper, so the HTTP call
    usually fails with 503 or a dropped connection even though the save
    went through (seen live on an AD5X, 2026-10-29). That is expected and
    is never resent. Any other failure is uncertain, like any other send.
    Success is Klipper reporting ready again; if it does not within the
    wait, the save is reported as unconfirmed rather than either outcome.
    """
    try:
        transport("POST", GCODE_SCRIPT_PATH, {"script": "SAVE_CONFIG"})
    except Exception as exc:
        if not _is_restart_disconnect(exc):
            raise CalibrationError(f"The send did not succeed ({exc}). It is not known "
                                   "whether the printer received or ran anything — "
                                   "check the printer before trying again.") from exc
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(interval_s)
        try:
            state = transport("GET", PRINTER_INFO_PATH)["result"]["state"]
        except Exception:
            continue
        if state == "ready":
            return
    raise CalibrationError(
        "SAVE_CONFIG was sent and Klipper restarted, but it did not report ready again "
        "in the time Print Handy waited, so the save is not confirmed. Check the "
        "printer (Fluidd/Mainsail or the screen) before printing; do not send "
        "SAVE_CONFIG again.")


# --------------------------------------------------------------------- discovery

@dataclass(frozen=True)
class PrinterFacts:
    """One live snapshot the availability verdicts and before-values read from."""

    objects: tuple = ()        # every object name Moonraker lists
    macros: tuple = ()         # macro names (the gcode_macro objects, prefix stripped)
    settings: dict = None      # configfile settings
    bed_mesh: dict = None      # the live bed_mesh object
    input_shaper: dict = None  # the live input_shaper object
    variables: dict = None     # save_variables variables

    def __post_init__(self):
        for name in ("settings", "bed_mesh", "input_shaper", "variables"):
            if getattr(self, name) is None:
                object.__setattr__(self, name, {})

    def has_macro(self, name) -> bool:
        wanted = str(name).casefold()
        return any(macro.casefold() == wanted for macro in self.macros)

    def has_object(self, name) -> bool:
        return name in self.objects

    def section(self, name) -> dict:
        section = self.settings.get(name)
        return section if isinstance(section, dict) else {}


def _objects_payload(transport):
    try:
        payload = transport("GET", OBJECTS_LIST_PATH)
    except Exception as exc:
        raise CalibrationError(f"the objects-list request failed: {exc}") from exc
    objects = payload
    if isinstance(payload, dict):
        if "result" not in payload:
            raise CalibrationError("Moonraker's objects-list response had no result")
        objects = payload["result"]
        if isinstance(objects, dict):
            objects = objects.get("objects")
    if not isinstance(objects, list) or not all(isinstance(o, str) for o in objects):
        raise CalibrationError("Moonraker's objects-list response was not a list of objects")
    return objects


def fetch_facts(transport) -> PrinterFacts:
    """The discovery snapshot: objects list + one objects query. Read-only.

    Any transport failure or malformed response raises CalibrationError;
    nothing is ever sent on this path.
    """
    objects = _objects_payload(transport)
    macros = tuple(o[len(MACRO_PREFIX):].strip() for o in objects
                   if o.startswith(MACRO_PREFIX) and o[len(MACRO_PREFIX):].strip())
    try:
        payload = transport("GET", FACTS_PATH)
        status = payload["result"]["status"]
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"the calibration-state request failed: {exc}") from exc
    if not isinstance(status, dict):
        raise CalibrationError("Moonraker's calibration-state response had no status")
    configfile = status.get("configfile") or {}
    save_variables = status.get("save_variables") or {}
    return PrinterFacts(
        objects=tuple(objects), macros=macros,
        settings=configfile.get("settings") or {},
        bed_mesh=status.get("bed_mesh") or {},
        input_shaper=status.get("input_shaper") or {},
        variables=save_variables.get("variables") or {})


def install_mode(entry, facts) -> str:
    """The install mode that owns calibration values: native / screenless / unknown / klipper.

    ZMod keeps two separate stores (Stage 0): ``display_off`` 0 means the
    native screen is fitted and in charge, 1 means screenless. Anything
    else on a ZMod install is unknown, never guessed. Non-ZMod Klipper
    has the one Klipper store.
    """
    if entry is not None and entry.firmware_family == "zmod":
        display_off = facts.variables.get("display_off")
        if display_off == 0:
            return "native"
        if display_off == 1:
            return "screenless"
        return "unknown"
    return "klipper"


@dataclass(frozen=True)
class CalOption:
    """One menu entry: how Print Handy serves this calibration on this printer."""

    key: str        # mesh | screws | pid_extruder | pid_bed | shaper | belts | zoffset
    title: str
    mode: str       # run (accept-gated) | run-report | guide | report
    note: str


def available_options(entry, facts) -> tuple:
    """The calibrations offered for this printer, from live discovery only.

    A calibration appears only when its primitive is actually present:
    the macro in the objects list, or the config section in the
    configfile settings. Mode verdicts follow the Stage 0 evidence.
    """
    mode = install_mode(entry, facts)
    options = []
    if mode == "native":
        options.append(CalOption(
            "mesh", "Full bed mesh", "report",
            "the printer's screen owns the mesh it loads for every print — "
            "Print Handy reports it and guides the rebuild"))
    elif mode == "screenless":
        if facts.has_macro("AUTO_FULL_BED_LEVEL"):
            options.append(CalOption(
                "mesh", "Full bed mesh", "run",
                "ZMod's AUTO_FULL_BED_LEVEL (load-cell aware, cleans the nozzle first), "
                "measured without saving — you accept before anything is stored"))
    elif mode == "unknown":
        options.append(CalOption(
            "mesh", "Full bed mesh", "report",
            "Print Handy cannot tell whether the native screen owns the mesh on "
            "this install, so it reports the stored mesh instead of measuring "
            "over a possible owner"))
    elif mode == "klipper":
        if facts.section("bed_mesh") or facts.has_object("bed_mesh"):
            options.append(CalOption(
                "mesh", "Full bed mesh", "run",
                "Klipper's BED_MESH_CALIBRATE, measured into memory only — you accept "
                "before the profile is saved"))
    if entry is not None and entry.firmware_family == "zmod":
        if facts.has_macro("BED_LEVEL_SCREWS_TUNE"):
            options.append(CalOption(
                "screws", "Bed screws", "guide",
                "BED_LEVEL_SCREWS_TUNE waits for its own confirmation at the printer, "
                "so Print Handy guides it instead of driving it"))
    elif facts.section("screws_tilt_adjust"):
        options.append(CalOption(
            "screws", "Bed screws", "run",
            "SCREWS_TILT_CALCULATE measures every screw; you turn them, Print Handy "
            "re-measures — nothing is saved, the change is physical"))
    elif facts.section("bed_screws"):
        options.append(CalOption(
            "screws", "Bed screws", "guide",
            "BED_SCREWS_ADJUST is an interactive dialogue at the nozzle, so Print "
            "Handy guides it instead of driving it"))
    if _pid_before(facts.settings, "extruder") is not None:
        options.append(CalOption(
            "pid_extruder", "Extruder PID", "run",
            "PID_CALIBRATE at the temperature you pick — never ZMod's auto-saving "
            "PID_TUNE_EXTRUDER"))
    if _pid_before(facts.settings, "heater_bed") is not None:
        options.append(CalOption(
            "pid_bed", "Bed PID", "run",
            "PID_CALIBRATE at the temperature you pick — never ZMod's auto-saving "
            "PID_TUNE_BED"))
    if entry is not None and entry.firmware_family == "zmod":
        if facts.has_macro("ZSHAPER"):
            options.append(CalOption(
                "shaper", "Input shaper", "run",
                "ZSHAPER measures both axes and recommends values — you accept before "
                "SAVE_CONFIG stores them"))
        if facts.has_macro("BELTS_SHAPER_CALIBRATION"):
            options.append(CalOption(
                "belts", "Belt check", "run-report",
                "BELTS_SHAPER_CALIBRATION analyses belt frequencies and reports — "
                "nothing is applied or saved"))
    elif facts.section("resonance_tester") or facts.has_object("resonance_tester"):
        options.append(CalOption(
            "shaper", "Input shaper", "run",
            "SHAPER_CALIBRATE recommends values — you accept before SAVE_CONFIG "
            "stores them"))
    if entry is not None and entry.firmware_family == "zmod":
        options.append(CalOption(
            "zoffset", "Z offset", "report",
            "report + guide: on ZMod the offset is owned by the "
            + ("native screen" if mode == "native" else
               "screenless saved store" if mode == "screenless" else
               "one of two separate stores")
            + " and adjusted during printing — there is no pre-print value for the "
              "bot to set"))
    else:
        options.append(CalOption(
            "zoffset", "Z offset", "guide",
            "PROBE_CALIBRATE is an interactive paper-test dialogue at the bed, so "
            "Print Handy reports the configured value and guides it"))
    return tuple(options)


def option_for(options, key):
    for option in options:
        if option.key == key:
            return option
    return None


_MENU_FORMS = {
    "mesh": "`/print-calibration mesh`",
    "screws": "`/print-calibration screws`",
    "pid_extruder": "`/print-calibration pid extruder [temp]`",
    "pid_bed": "`/print-calibration pid bed [temp]`",
    "shaper": "`/print-calibration shaper`",
    "belts": "`/print-calibration belts`",
    "zoffset": "`/print-calibration zoffset`",
}
_MODE_WORDS = {"run": "guided run, accept before anything saves",
               "run-report": "guided run, report only",
               "guide": "guided — done at the printer",
               "report": "report + guide"}


def menu_text(entry, options) -> str:
    """The bare-command answer: what this printer offers, and how each is served."""
    label = entry.label if entry is not None else "your printer"
    lines = [f"**Printer calibration — {label}**", ""]
    if not options:
        lines.append("Live discovery found none of the calibration primitives Print "
                     "Handy can drive or guide on this printer (no ZMod calibration "
                     "macros, no bed mesh / PID / resonance configuration it recognizes).")
    for option in options:
        lines.append(f"- {_MENU_FORMS[option.key]} — **{option.title}** "
                     f"({_MODE_WORDS[option.mode]}): {option.note}.")
    lines.append("- `/print-calibration records` — the recent calibration records "
                 "for this printer.")
    lines.append("")
    lines.append("Every guided run follows the same pattern: Print Handy shows the "
                 "current values first, asks before running, then shows the new "
                 "values and the differences and asks again before anything is "
                 "saved. Runs happen only while the printer is idle.")
    lines.append("")
    lines.append("Read-only: this menu came from live reads of the printer; nothing "
                 "was sent to it.")
    return "\n".join(lines)


# --------------------------------------------------------------------- values

def _num(value):
    """A finite float from a config/status value, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if number == number and abs(number) != float("inf") else None


def _fmt(value, digits=3):
    return f"{value:.{digits}f}" if value is not None else "not reported"


def _delta(old, new, digits=3, unit=""):
    """The signed difference new - old, e.g. ``+0.055 mm``; '' when either side is unknown."""
    if old is None or new is None:
        return ""
    return f"{new - old:+.{digits}f}{unit}"


@dataclass(frozen=True)
class PidValues:
    kp: float
    ki: float
    kd: float

    def as_dict(self):
        return {"kp": self.kp, "ki": self.ki, "kd": self.kd}

    def text(self):
        return f"Kp {_fmt(self.kp)} · Ki {_fmt(self.ki)} · Kd {_fmt(self.kd)}"


def pid_differences(before: PidValues, after: PidValues) -> dict:
    return {name: {"before": getattr(before, name), "after": getattr(after, name),
                   "delta": round(getattr(after, name) - getattr(before, name), 3)}
            for name in ("kp", "ki", "kd")}


def pid_difference_lines(before: PidValues, after: PidValues) -> list:
    lines = []
    for name, label in (("kp", "Kp"), ("ki", "Ki"), ("kd", "Kd")):
        old, new = getattr(before, name), getattr(after, name)
        lines.append(f"- {label}: {_fmt(old)} → {_fmt(new)} ({_delta(old, new)})")
    return lines


def _pid_before(settings, heater):
    """The configured PID terms for one heater from configfile settings, or None."""
    section = settings.get(heater) if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return None
    # Klipper's configfile status lowercases every option name, so the
    # terms arrive as pid_kp / pid_ki / pid_kd whatever casing the
    # printer.cfg file itself uses; look them up case-insensitively.
    lowered = {str(key).lower(): value for key, value in section.items()}
    values = [_num(lowered.get(key)) for key in ("pid_kp", "pid_ki", "pid_kd")]
    if any(value is None for value in values):
        return None
    return PidValues(*values)


@dataclass(frozen=True)
class MeshSummary:
    """A mesh reduced to what the pattern presents: where it came from, low, high."""

    source: str                 # e.g. "profile 'print_handy'" or "the active mesh"
    low: float
    high: float

    @property
    def range(self):
        return self.high - self.low

    def as_dict(self):
        return {"source": self.source, "min_mm": self.low, "max_mm": self.high,
                "range_mm": round(self.range, 3)}

    def text(self):
        return (f"{self.source}: lowest {_fmt(self.low)} mm, highest {_fmt(self.high)} mm, "
                f"range {_fmt(self.range)} mm")


def _matrix_summary(matrix, source):
    values = []
    if isinstance(matrix, list):
        for row in matrix:
            if isinstance(row, list):
                values += [v for v in (_num(item) for item in row) if v is not None]
            else:
                value = _num(row)
                if value is not None:
                    values.append(value)
    if not values:
        return None
    return MeshSummary(source, min(values), max(values))


def active_mesh_summary(bed_mesh) -> MeshSummary | None:
    """The currently loaded mesh, from the live bed_mesh object's probed matrix."""
    if not isinstance(bed_mesh, dict):
        return None
    return _matrix_summary(bed_mesh.get("probed_matrix"), "the currently loaded mesh")


def profile_mesh_summary(bed_mesh, profile) -> MeshSummary | None:
    """A stored profile's mesh, from the live bed_mesh object's profiles."""
    if not isinstance(bed_mesh, dict):
        return None
    profiles = bed_mesh.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        return None
    entry = profiles[profile]
    if not isinstance(entry, dict):
        return None
    return _matrix_summary(entry.get("points"), f"profile '{profile}'")


def mesh_differences(before: MeshSummary | None, after: MeshSummary) -> dict:
    if before is None:
        return {"range_mm": {"before": None, "after": round(after.range, 3), "delta": None},
                "min_mm": {"before": None, "after": after.low, "delta": None},
                "max_mm": {"before": None, "after": after.high, "delta": None}}
    return {
        "range_mm": {"before": round(before.range, 3), "after": round(after.range, 3),
                     "delta": round(after.range - before.range, 3)},
        "min_mm": {"before": before.low, "after": after.low,
                   "delta": round(after.low - before.low, 3)},
        "max_mm": {"before": before.high, "after": after.high,
                   "delta": round(after.high - before.high, 3)},
    }


@dataclass(frozen=True)
class ShaperValues:
    type_x: str | None = None
    freq_x: float | None = None
    type_y: str | None = None
    freq_y: float | None = None

    def as_dict(self):
        return {"type_x": self.type_x, "freq_x": self.freq_x,
                "type_y": self.type_y, "freq_y": self.freq_y}

    def axis_text(self, axis):
        shaper_type = getattr(self, f"type_{axis}")
        freq = getattr(self, f"freq_{axis}")
        if shaper_type is None and freq is None:
            return "not reported"
        return f"{shaper_type or 'type not reported'} @ {_fmt(freq, 1)} Hz"


def shaper_from_settings(settings) -> ShaperValues:
    """The configured input shaper values from configfile settings (either axis form)."""
    section = settings.get("input_shaper") if isinstance(settings, dict) else None
    if not isinstance(section, dict):
        return ShaperValues()
    shared_type = section.get("shaper_type")
    return ShaperValues(
        type_x=str(section.get("shaper_type_x") or shared_type or "") or None,
        freq_x=_num(section.get("shaper_freq_x")),
        type_y=str(section.get("shaper_type_y") or shared_type or "") or None,
        freq_y=_num(section.get("shaper_freq_y")))


def shaper_differences(before: ShaperValues, after: ShaperValues) -> dict:
    out = {}
    for axis in ("x", "y"):
        old_f, new_f = getattr(before, f"freq_{axis}"), getattr(after, f"freq_{axis}")
        out[axis] = {"type_before": getattr(before, f"type_{axis}"),
                     "type_after": getattr(after, f"type_{axis}"),
                     "freq_before": old_f, "freq_after": new_f,
                     "freq_delta": (round(new_f - old_f, 1)
                                    if old_f is not None and new_f is not None else None)}
    return out


def shaper_difference_lines(before: ShaperValues, after: ShaperValues) -> list:
    lines = []
    for axis in ("x", "y"):
        old_t, new_t = getattr(before, f"type_{axis}"), getattr(after, f"type_{axis}")
        old_f, new_f = getattr(before, f"freq_{axis}"), getattr(after, f"freq_{axis}")
        if new_t is None and new_f is None:
            lines.append(f"- {axis.upper()} axis: no recommendation was reported")
            continue
        delta = _delta(old_f, new_f, 1, " Hz") if old_f is not None and new_f is not None else ""
        lines.append(f"- {axis.upper()} axis: {old_t or 'not reported'} @ {_fmt(old_f, 1)} Hz → "
                     f"{new_t or 'not reported'} @ {_fmt(new_f, 1)} Hz"
                     + (f" ({delta})" if delta else ""))
    return lines


@dataclass(frozen=True)
class Screw:
    """One screw's tilt reading: probed height and the exact turn Klipper advises."""

    name: str
    z: float
    adjust: str | None = None     # e.g. "CW 01:15"; None for the base screw
    is_base: bool = False


@dataclass(frozen=True)
class ScrewReading:
    screws: tuple = ()

    @property
    def base_z(self):
        for screw in self.screws:
            if screw.is_base:
                return screw.z
        return self.screws[0].z if self.screws else None

    def errors(self) -> dict:
        """{screw name: signed height error vs the base screw, mm}."""
        base = self.base_z
        if base is None:
            return {}
        return {screw.name: round(screw.z - base, 3) for screw in self.screws}

    def as_dict(self):
        return {"screws": [{"name": s.name, "z_mm": s.z, "adjust": s.adjust,
                            "is_base": s.is_base} for s in self.screws],
                "errors_mm": self.errors()}


# --------------------------------------------------------------------- gcode_store polling

def _fetch_store_entries(transport) -> list:
    """The console entries Moonraker has stored, oldest first, as (time, message)."""
    try:
        payload = transport("GET", GCODE_STORE_PATH)
    except Exception as exc:
        raise CalibrationError(f"the console-history request failed: {exc}") from exc
    try:
        entries = payload["result"]["gcode_store"]
    except (TypeError, KeyError):
        raise CalibrationError("Moonraker's console-history response had no gcode_store") from None
    if not isinstance(entries, list):
        raise CalibrationError("Moonraker's console-history response was not a list")
    return [(entry.get("time") if isinstance(entry.get("time"), (int, float)) else 0.0,
             entry["message"]) for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("message"), str)]


def fetch_store_messages(transport) -> list:
    """The console messages Moonraker has stored, oldest first (strings only)."""
    return [message for _when, message in _fetch_store_entries(transport)]


def store_mark(transport) -> float:
    """A position in the console history: the newest stored entry's time.

    Moonraker keeps only the newest entries (1000 by default), so once
    the store is full a message count stops moving; entry timestamps keep
    increasing, so they anchor "what arrived after the send".
    """
    return max((when for when, _message in _fetch_store_entries(transport)),
               default=float("-inf"))


def _entries_since(transport, mark) -> list:
    return [(when, message) for when, message in _fetch_store_entries(transport)
            if when > mark]


def messages_since(transport, mark) -> list:
    """The console messages stored after `mark` (see store_mark), oldest first."""
    return [message for _when, message in _entries_since(transport, mark)]


def _poll(transport, mark, matches, *, attempts, interval_s, sleep):
    """Poll the console store until `matches` finds a result in the new messages.

    `matches(messages_after_mark)` returns a non-None result to stop,
    or None to keep waiting. Returns the result, or None when the attempt
    budget ran out. Timing is injectable so tests run instantly.
    """
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(interval_s)
        result = matches(messages_since(transport, mark))
        if result is not None:
            return result
    return None


# --------------------------------------------------------------------- PID

HEATER_LABELS = {"extruder": "Extruder", "heater_bed": "Bed"}
HEATER_TARGETS = {"extruder": EXTRUDER_PID_TARGETS, "heater_bed": BED_PID_TARGETS}
_PID_REPORT_RE = re.compile(
    r"PID parameters: pid_Kp=([0-9]+(?:\.[0-9]+)?) pid_Ki=([0-9]+(?:\.[0-9]+)?) "
    r"pid_Kd=([0-9]+(?:\.[0-9]+)?)")


def parse_pid_report(messages) -> PidValues | None:
    """The last PID parameter set reported in console messages, or None."""
    found = None
    for message in messages:
        match = _PID_REPORT_RE.search(message)
        if match:
            found = PidValues(*(float(group) for group in match.groups()))
    return found


@dataclass(frozen=True)
class PreparedPid:
    heater: str                 # "extruder" | "heater_bed"
    target: int
    before: PidValues

    @property
    def label(self):
        return HEATER_LABELS[self.heater]

    @property
    def script(self):
        return f"PID_CALIBRATE HEATER={self.heater} TARGET={self.target}"


def prepare_pid(transport, entry, heater, target) -> PreparedPid:
    """Validate one PID calibration against the profile and the live printer; send nothing."""
    reason = block_reason(entry)
    if reason is not None:
        raise CalibrationRefusal(reason + " Nothing was sent.")
    if heater not in HEATER_LABELS:
        raise CalibrationRefusal(f"{heater!r} is not a heater Print Handy calibrates. Use "
                                 "`/print-calibration pid extruder` or "
                                 "`/print-calibration pid bed`. Nothing was sent.")
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise CalibrationRefusal(f"{target!r} is not a whole-number temperature. Nothing "
                                 "was sent.") from None
    low, high = (150, 300) if heater == "extruder" else (30, 120)
    if not low <= target <= high:
        raise CalibrationRefusal(f"{target} °C is outside the range Print Handy will tune "
                                 f"the {HEATER_LABELS[heater].lower()} at ({low}–{high} °C). "
                                 "Nothing was sent.")
    try:
        payload = transport("GET", SETTINGS_PATH)
        settings = payload["result"]["status"]["configfile"]["settings"]
    except Exception as exc:
        raise CalibrationError(COULD_NOT_READ_TEXT) from exc
    before = _pid_before(settings, heater)
    if before is None:
        raise CalibrationRefusal(f"The printer's configuration reports no PID values for "
                                 f"the {HEATER_LABELS[heater].lower()}, so there is no "
                                 "before-value to compare against and nothing to tune "
                                 "from here. Nothing was sent.")
    _require_idle_now(transport)
    return PreparedPid(heater=heater, target=target, before=before)


def pid_prompt_text(prepared: PreparedPid) -> str:
    """The run confirmation: before-values, the exact command, the warnings."""
    return "\n".join([
        f"Run the {prepared.label.lower()} PID calibration at {prepared.target} °C?",
        "",
        f"Current values: {prepared.before.text()}.",
        "",
        f"This sends exactly one command: `{prepared.script}` — Klipper's own "
        "calibration, which does **not** save anything by itself. It deliberately "
        "does not use ZMod's PID_TUNE macros, because those save automatically "
        "before you have seen the result.",
        "",
        f"Warnings: the {prepared.label.lower()} will heat to {prepared.target} °C and "
        "cycle around it for several minutes — stay nearby, keep the area around the "
        "printer clear, and do not start this and leave. The printer must stay idle; "
        "its state is checked again immediately before the command is sent.",
        "",
        "When it finishes, Print Handy shows the new values and the differences and "
        "asks before anything is saved. Discarding restarts Klipper's firmware to "
        "drop the unsaved values — you will be warned again at that step."])


@dataclass(frozen=True)
class PidResult:
    prepared: PreparedPid
    after: PidValues

    @property
    def before(self):
        return self.prepared.before


def execute_pid(transport, prepared: PreparedPid, *, poll_attempts=120,
                poll_interval_s=5.0, sleep=time.sleep) -> PidResult:
    """Re-check idle, send the one PID_CALIBRATE, poll the console for its report.

    The tuned values take effect in the heater's memory as soon as the
    calibration completes; nothing is written to the config. A timeout is
    a CalibrationError (the run may still finish later on the printer —
    the message says so); the accept/discard decision belongs to the
    caller once a result exists.
    """
    _require_idle_now(transport, recheck=True)
    mark = store_mark(transport)
    _send_script(transport, prepared.script, long_running=True)
    after = _poll(transport, mark, parse_pid_report,
                  attempts=poll_attempts, interval_s=poll_interval_s, sleep=sleep)
    if after is None:
        raise CalibrationError(
            f"The {prepared.label.lower()} PID calibration did not report its new "
            "values in the time Print Handy waited, so there is no result to accept "
            "or discard. If it is still running on the printer, let it finish — its "
            "values then sit in memory only, and restarting Klipper (or the printer) "
            "drops them; nothing was saved.")
    return PidResult(prepared=prepared, after=after)


def pid_result_text(result: PidResult) -> str:
    """The result presentation: new values AND the signed differences, then the gate."""
    return "\n".join([
        f"**{result.prepared.label} PID calibration finished** "
        f"(target {result.prepared.target} °C).",
        "",
        "Before → after:",
        *pid_difference_lines(result.before, result.after),
        "",
        "The new values are live in the heater's memory only — nothing has been "
        "saved.",
        f"- **{ACCEPT_LABEL}**: saves them with SAVE_CONFIG (this restarts Klipper) "
        "so they survive restarts.",
        f"- **{DISCARD_LABEL}**: restarts Klipper's firmware (FIRMWARE_RESTART) to "
        "drop the new values and bring back the saved ones. The printer is "
        "unavailable for about a minute while it restarts."])


def accept_pid(transport, result: PidResult, *, sleep=time.sleep) -> str:
    """Accept: exactly one SAVE_CONFIG, which also restarts Klipper."""
    _require_idle_now(transport, recheck=True)
    _save_config(transport, sleep=sleep)
    return (f"Saved the new {result.prepared.label.lower()} PID values "
            f"({result.after.text()}) with SAVE_CONFIG. Klipper restarted and is "
            "ready; the values are now the saved configuration. "
            "A calibration record was written — `/print-calibration records` lists it.")


def discard_pid(transport, result: PidResult) -> str:
    """Discard: exactly one FIRMWARE_RESTART, which drops the in-memory values. Never a save."""
    _require_idle_now(transport, recheck=True)
    _send_script(transport, "FIRMWARE_RESTART")
    return (f"Discarded the new {result.prepared.label.lower()} PID values and restarted "
            f"Klipper's firmware, so the saved values ({result.before.text()}) are back "
            "in force. Nothing was saved. A calibration record was written — "
            "`/print-calibration records` lists it.")


# --------------------------------------------------------------------- bed mesh

@dataclass(frozen=True)
class PreparedMesh:
    family: str                     # "zmod" | "klipper"
    profile: str                    # the profile the measured mesh is saved under on Accept
    before: MeshSummary | None      # the stored profile's mesh, else the active mesh
    previous_profile: str | None    # the profile name loaded before, for the discard reload
    before_matrix: tuple = ()       # the active probed matrix before, for change detection

    @property
    def script(self):
        if self.family == "zmod":
            return (f"AUTO_FULL_BED_LEVEL PROFILE={MEASURE_PROFILE} SAVE=0 "
                    f"EXTRUDER_TEMP={ZMOD_MESH_EXTRUDER_TEMP} BED_TEMP={ZMOD_MESH_BED_TEMP}")
        return f"BED_MESH_CALIBRATE PROFILE={MEASURE_PROFILE}"


def _matrix_key(matrix):
    summary = _matrix_summary(matrix, "")
    if summary is None:
        return None
    return (round(summary.low, 4), round(summary.high, 4))


def prepare_mesh(transport, entry, facts=None) -> PreparedMesh:
    """Validate one mesh calibration against the profile and the live printer; send nothing.

    Native-screen ZMod installs are refused here by design: the screen
    owns the mesh (see mesh_report_text) — the dispatch routes them to
    the report before ever preparing a run.
    """
    reason = block_reason(entry)
    if reason is not None:
        raise CalibrationRefusal(reason + " Nothing was sent.")
    if facts is None:
        facts = fetch_facts(transport)
    mode = install_mode(entry, facts)
    if entry is not None and entry.firmware_family == "zmod":
        if mode == "native":
            raise CalibrationRefusal(
                "This printer's native screen owns the bed mesh — it loads its mesh "
                "for every print, and a mesh Print Handy saved would not be the one "
                "used. `/print-calibration mesh` on this printer reports the stored "
                "mesh and the screen rebuild steps instead. Nothing was sent.")
        if mode == "unknown":
            raise CalibrationRefusal(
                "Print Handy could not tell whether this printer's native screen is "
                "in charge of the mesh (its saved display mode was unreadable), so it "
                "will not measure one. `/print-calibration mesh` reports what it can "
                "read. Nothing was sent.")
        if not facts.has_macro("AUTO_FULL_BED_LEVEL"):
            raise CalibrationRefusal(
                "The printer does not define ZMod's AUTO_FULL_BED_LEVEL macro, so "
                "Print Handy has no load-cell-aware mesh measurement to run (the "
                "built-in web mesh tools are not suitable for these printers — they "
                "skip the nozzle cleaning and the load cells). Nothing was sent.")
        family = "zmod"
    else:
        if not (facts.section("bed_mesh") or facts.has_object("bed_mesh")):
            raise CalibrationRefusal(
                "The printer's configuration has no bed mesh (no [bed_mesh] section), "
                "so there is no mesh calibration to run. Nothing was sent.")
        family = "klipper"
    profile = MESH_PROFILES[family]
    before = (profile_mesh_summary(facts.bed_mesh, profile)
              or active_mesh_summary(facts.bed_mesh))
    previous = facts.bed_mesh.get("profile_name")
    _require_idle_now(transport)
    return PreparedMesh(
        family=family, profile=profile, before=before,
        previous_profile=previous if isinstance(previous, str) and previous else None,
        before_matrix=tuple(tuple(row) for row in facts.bed_mesh.get("probed_matrix") or []
                            if isinstance(row, list)))


def mesh_prompt_text(prepared: PreparedMesh) -> str:
    """The run confirmation: before-values, the exact command, the warnings."""
    lines = ["Run the full bed mesh calibration?", ""]
    if prepared.before is not None:
        lines.append(f"Current mesh — {prepared.before.text()}.")
    else:
        lines.append("No mesh is currently stored or loaded that Print Handy can read.")
    lines += ["",
              f"This sends exactly one command: `{prepared.script}`"
              + (" — ZMod's own leveling: it heats the bed and nozzle, cleans the "
                 "nozzle, then probes the whole bed with the load cells. ZMod's macro "
                 "is told SAVE=0, so it measures and loads the mesh in memory only; "
                 "your saved meshes are not touched."
                 if prepared.family == "zmod" else
                 " — it probes the whole bed and loads the mesh in memory only; "
                 "your saved profiles are not touched."),
              "",
              "Warnings: the printer will heat up and move across the whole bed for "
              "several minutes — stay nearby and keep the bed clear of tools and "
              "prints. This also re-zeros the Z offset's start point on ZMod, as "
              "ZMod's leveling always does.",
              "",
              "When it finishes, Print Handy shows the new mesh range and the "
              "difference and asks before anything is saved."]
    return "\n".join(lines)


@dataclass(frozen=True)
class MeshResult:
    prepared: PreparedMesh
    after: MeshSummary
    # True when the heaters were turned off after measuring and the readback
    # showed both targets at 0; False when that could not be confirmed; None
    # when they were left alone because the printer was no longer idle.
    cooled: bool | None = True

    @property
    def before(self):
        return self.prepared.before


def _fetch_bed_mesh(transport):
    try:
        payload = transport("GET", "/printer/objects/query?bed_mesh")
        status = payload["result"]["status"]["bed_mesh"]
    except Exception as exc:
        raise CalibrationError(f"the bed-mesh readback failed: {exc}") from exc
    if not isinstance(status, dict):
        raise CalibrationError("Moonraker's bed-mesh response had no bed_mesh object")
    return status


HEATER_TARGETS_PATH = "/printer/objects/query?extruder=target&heater_bed=target"


def _cool_down(transport) -> bool | None:
    """Turn both heaters off after a measurement and confirm it by readback.

    The leveling macros heat the bed and nozzle; the Accept/Discard prompt
    can wait a long time, so the heaters must not sit at temperature while
    it does. The printer is re-checked first: a print started from the
    screen or another client while the mesh was being read back must keep
    its heaters, so a printer that is not idle (or whose state cannot be
    read) is left alone and None is returned. Returns False (never raises)
    when the off command or the readback fails, so the measured mesh is
    still presented.
    """
    try:
        _require_idle_now(transport, recheck=True)
    except Exception:
        return None
    try:
        _send_script(transport, "TURN_OFF_HEATERS")
        status = transport("GET", HEATER_TARGETS_PATH)["result"]["status"]
        return all(float((status.get(name) or {}).get("target", 1)) == 0
                   for name in ("extruder", "heater_bed"))
    except Exception:
        return False


def execute_mesh(transport, prepared: PreparedMesh, *, poll_attempts=180,
                 poll_interval_s=5.0, sleep=time.sleep) -> MeshResult:
    """Re-check idle, send the one measurement command, poll for the new mesh.

    Completion is the probed matrix actually changing (read back through
    the bed_mesh object), confirmed by a second identical read so a
    half-written matrix is never presented.
    """
    _require_idle_now(transport, recheck=True)
    before_key = _matrix_key([list(row) for row in prepared.before_matrix])
    _send_script(transport, prepared.script, long_running=True)
    last_key = None
    for attempt in range(max(1, poll_attempts)):
        if attempt:
            sleep(poll_interval_s)
        summary = active_mesh_summary(_fetch_bed_mesh(transport))
        if summary is None:
            last_key = None
            continue
        key = (round(summary.low, 4), round(summary.high, 4))
        if before_key is not None and key == before_key:
            last_key = None
            continue
        if key == last_key:
            return MeshResult(prepared=prepared, after=summary, cooled=_cool_down(transport))
        last_key = key
    raise CalibrationError(
        "The mesh measurement did not produce a new mesh Print Handy could read back "
        "in the time it waited, so there is no result to accept or discard. If the "
        "printer is still measuring, let it finish — the measured mesh is loaded in "
        "memory only and nothing was saved; a restart restores the saved state.")


def mesh_result_text(result: MeshResult) -> str:
    """The result presentation: new range AND the signed range difference, then the gate."""
    before, after = result.before, result.after
    lines = ["**Full bed mesh measured.**", ""]
    if before is not None:
        lines.append(f"- Before — {before.text()}.")
        lines.append(f"- After — {after.text()}.")
        lines.append(f"- Range difference: {_fmt(before.range)} mm → {_fmt(after.range)} mm "
                     f"({_delta(before.range, after.range, 3, ' mm')})"
                     + (" — flatter." if after.range < before.range else
                        " — not flatter than before." if after.range > before.range else
                        " — unchanged."))
    else:
        lines.append(f"- Measured — {after.text()} (no earlier mesh was readable "
                     "to compare against).")
    if result.cooled:
        lines += ["", "Heaters turned off after measuring (checked)."]
    elif result.cooled is None:
        lines += ["", "**Heaters left as they are: the printer was no longer idle after "
                      "measuring (a print may have started), so Print Handy did not turn "
                      "them off. Check the printer.**"]
    else:
        lines += ["", "**Print Handy could not confirm the heaters turned off after "
                      "measuring. Check the printer and turn them off yourself.**"]
    lines += ["",
              "The measured mesh is loaded in memory only — nothing has been saved.",
              f"- **{ACCEPT_LABEL}**: stores it as profile '{result.prepared.profile}' "
              "with BED_MESH_PROFILE SAVE + SAVE_CONFIG (SAVE_CONFIG restarts Klipper). "
              "That is the profile your prints load (ZMod screenless START_PRINT "
              "MESH=auto; Klipper's startup profile 'default'); it never replaces the "
              "native screen's mesh.",
              f"- **{DISCARD_LABEL}**: puts back the mesh that was loaded before "
              "(or clears the mesh) and saves nothing."]
    return "\n".join(lines)


def accept_mesh(transport, result: MeshResult, *, sleep=time.sleep) -> str:
    """Accept: copy the measured mesh into the profile prints load, then SAVE_CONFIG once.

    The measurement sits in the scratch profile; it is loaded, saved under
    the prints' profile, and the scratch profile is removed so it is not
    persisted alongside.
    """
    _require_idle_now(transport, recheck=True)
    _send_script(transport, f"BED_MESH_PROFILE LOAD={MEASURE_PROFILE}")
    _send_script(transport, f"BED_MESH_PROFILE SAVE={result.prepared.profile}")
    _send_script(transport, f"BED_MESH_PROFILE REMOVE={MEASURE_PROFILE}")
    _save_config(transport, sleep=sleep)
    return (f"Saved the measured mesh as profile '{result.prepared.profile}' with "
            "SAVE_CONFIG. Klipper restarted and is ready; the profile is stored in "
            "the configuration. A calibration record was written — "
            "`/print-calibration records` lists it.")


def discard_mesh(transport, result: MeshResult) -> str:
    """Discard: restore the previously loaded mesh (or clear it). Never a save.

    The scratch profile is removed first, so a later unrelated SAVE_CONFIG
    cannot persist the discarded measurement.
    """
    _require_idle_now(transport, recheck=True)
    _send_script(transport, f"BED_MESH_PROFILE REMOVE={MEASURE_PROFILE}")
    previous = result.prepared.previous_profile
    if previous and previous != MEASURE_PROFILE:
        _send_script(transport, f"BED_MESH_PROFILE LOAD={previous}")
        restored = f"reloaded the mesh that was active before ('{previous}')"
    else:
        _send_script(transport, "BED_MESH_CLEAR")
        restored = "cleared the measured mesh"
    return (f"Discarded the measured mesh — {restored}, and nothing was saved. "
            "A calibration record was written — `/print-calibration records` lists it.")


# --------------------------------------------------------------------- input shaper

_SHAPER_REC_KLIPPER_RE = re.compile(
    r"Recommended shaper_type_([xy])\s*=\s*([A-Za-z0-9_]+)\s*,\s*"
    r"shaper_freq_[xy]\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*Hz", re.I)
_SHAPER_REC_ZMOD_RE = re.compile(
    r"Recommended shaper is\s+([A-Za-z0-9_]+)\s*@\s*([0-9]+(?:\.[0-9]+)?)\s*Hz", re.I)
_AXIS_CONTEXT_RE = re.compile(r"for\s+([xy])\s+axis", re.I)


def parse_shaper_recommendations(messages) -> ShaperValues:
    """The recommended shaper values from console messages (either format).

    Stock Klipper names the axis in the line itself
    (``Recommended shaper_type_y = mzv, shaper_freq_y = 36.8 Hz``); ZMod's
    ZSHAPER prints ``Recommended shaper is zv @ 53.2 Hz`` per axis under
    Klipper's ``... for x axis`` / ``... for y axis`` headers, so the
    axis is tracked from those headers, in message order.
    """
    found = {}
    axis = None
    for message in messages:
        header = _AXIS_CONTEXT_RE.search(message)
        if header:
            axis = header.group(1).casefold()
        match = _SHAPER_REC_KLIPPER_RE.search(message)
        if match:
            found[match.group(1).casefold()] = (match.group(2).casefold(),
                                                float(match.group(3)))
            continue
        match = _SHAPER_REC_ZMOD_RE.search(message)
        if match and axis is not None:
            found[axis] = (match.group(1).casefold(), float(match.group(2)))
    return ShaperValues(
        type_x=found.get("x", (None, None))[0], freq_x=found.get("x", (None, None))[1],
        type_y=found.get("y", (None, None))[0], freq_y=found.get("y", (None, None))[1])


@dataclass(frozen=True)
class PreparedShaper:
    family: str                     # "zmod" | "klipper"
    before: ShaperValues

    @property
    def script(self):
        return "ZSHAPER" if self.family == "zmod" else "SHAPER_CALIBRATE"


def prepare_shaper(transport, entry, facts=None) -> PreparedShaper:
    """Validate one input-shaper calibration; send nothing."""
    reason = block_reason(entry)
    if reason is not None:
        raise CalibrationRefusal(reason + " Nothing was sent.")
    if facts is None:
        facts = fetch_facts(transport)
    if entry is not None and entry.firmware_family == "zmod":
        if not facts.has_macro("ZSHAPER"):
            raise CalibrationRefusal("The printer does not define ZMod's ZSHAPER macro, "
                                     "so Print Handy has no input-shaper measurement to "
                                     "run on it. Nothing was sent.")
        family = "zmod"
    else:
        if not (facts.section("resonance_tester") or facts.has_object("resonance_tester")):
            raise CalibrationRefusal("The printer has no resonance tester configured "
                                     "(no accelerometer setup in its config), so Klipper "
                                     "has no way to measure its resonances from here. "
                                     "Nothing was sent.")
        family = "klipper"
    _require_idle_now(transport)
    return PreparedShaper(family=family, before=shaper_from_settings(facts.settings))


def shaper_prompt_text(prepared: PreparedShaper) -> str:
    """The run confirmation: before-values, the exact command, the warnings."""
    return "\n".join([
        "Run the input shaper calibration?",
        "",
        f"Current values — X axis: {prepared.before.axis_text('x')}; "
        f"Y axis: {prepared.before.axis_text('y')}.",
        "",
        f"This sends exactly one command: `{prepared.script}`. It only *measures and "
        "recommends* — nothing is applied or saved by the calibration itself.",
        "",
        "Warnings: the printer will vibrate hard at many frequencies for several "
        "minutes and will be loud — stay nearby, make sure nothing loose is sitting "
        "on the printer, and check afterwards that nothing has worked loose. Klipper "
        "advises against running this often: the resonant vibration wears parts.",
        "",
        "When it finishes, Print Handy shows the recommended values and the "
        "differences and asks before anything is saved. The graphs and data files "
        "land in the printer's Configuration → mod_data in its web interface."])


@dataclass(frozen=True)
class ShaperResult:
    prepared: PreparedShaper
    after: ShaperValues

    @property
    def before(self):
        return self.prepared.before


def execute_shaper(transport, prepared: PreparedShaper, *, poll_attempts=120,
                   poll_interval_s=5.0, sleep=time.sleep) -> ShaperResult:
    """Re-check idle, send the one calibration command, poll for its recommendations.

    Stops as soon as both axes have recommendations; a single-axis result
    at the end of the budget is still a result (presented as such). No
    recommendation at all is a CalibrationError.
    """
    _require_idle_now(transport, recheck=True)
    mark = store_mark(transport)
    _send_script(transport, prepared.script, long_running=True)

    def both_axes(messages):
        values = parse_shaper_recommendations(messages)
        if values.freq_x is not None and values.freq_y is not None:
            return values
        return None

    after = _poll(transport, mark, both_axes,
                  attempts=poll_attempts, interval_s=poll_interval_s, sleep=sleep)
    if after is None:
        partial = parse_shaper_recommendations(messages_since(transport, mark))
        if partial.freq_x is None and partial.freq_y is None:
            raise CalibrationError(
                "The input shaper calibration did not report any recommended values "
                "in the time Print Handy waited, so there is no result to accept or "
                "discard. If it is still measuring on the printer, let it finish — "
                "its recommendations are not applied or saved by the calibration "
                "itself, so nothing needs undoing.")
        after = partial
    return ShaperResult(prepared=prepared, after=after)


def shaper_result_text(result: ShaperResult) -> str:
    """The result presentation: recommended type/frequency AND differences, then the gate."""
    lines = ["**Input shaper calibration finished.**", "",
             "Before → recommended:",
             *shaper_difference_lines(result.before, result.after),
             "",
             "The recommended values are not in force and nothing has been saved — "
             "the calibration only holds them ready.",
             f"- **{ACCEPT_LABEL}**: stores them with SAVE_CONFIG, which restarts "
             "Klipper. Note SAVE_CONFIG does not change max_accel — if the report "
             "suggested a lower acceleration, set that separately.",
             f"- **{DISCARD_LABEL}**: sends nothing. One honesty note: the "
             "recommendation stays pending inside Klipper until the next restart, "
             "so an unrelated SAVE_CONFIG before a restart would include it — "
             "restarting the printer drops it for good."]
    return "\n".join(lines)


def accept_shaper(transport, result: ShaperResult, *, sleep=time.sleep) -> str:
    """Accept: exactly one SAVE_CONFIG, which also restarts Klipper."""
    _require_idle_now(transport, recheck=True)
    _save_config(transport, sleep=sleep)
    return ("Saved the recommended input shaper values with SAVE_CONFIG "
            f"(X: {result.after.axis_text('x')}; Y: {result.after.axis_text('y')}). "
            "Klipper restarted and is ready with them. Print a "
            "ringing test to confirm the improvement before trusting them. "
            "A calibration record was written — `/print-calibration records` lists it.")


def discard_shaper(transport, result: ShaperResult) -> str:
    """Discard: send nothing — the calibration applied nothing. Never a save."""
    return ("Discarded the recommended input shaper values — nothing was sent and "
            "nothing was saved; your configured values "
            f"(X: {result.before.axis_text('x')}; Y: {result.before.axis_text('y')}) "
            "stay in force. The recommendation stays pending inside Klipper until "
            "the next restart drops it, so avoid an unrelated SAVE_CONFIG before "
            "then. A calibration record was written — `/print-calibration records` "
            "lists it.")


# --------------------------------------------------------------------- bed screws

_SCREW_LINE_RE = re.compile(
    r"^(?://\s*)?(.+?)\s*:\s*x=(-?[0-9]+(?:\.[0-9]+)?),\s*y=(-?[0-9]+(?:\.[0-9]+)?),"
    r"\s*z=(-?[0-9]+(?:\.[0-9]+)?)(?:\s*:\s*adjust\s+(CW|CCW)\s+([0-9]+):([0-9]+))?\s*$")


def parse_screw_lines(messages) -> ScrewReading:
    """One SCREWS_TILT_CALCULATE result set from console messages.

    The documented output names each screw with its probed z and, for
    every screw but the base, the exact turn (``adjust CW 01:15`` —
    hours:minutes of a clock face). The last complete set in the
    messages wins, so a re-measure supersedes the earlier one.
    """
    found = []
    for message in messages:
        match = _SCREW_LINE_RE.match(message.strip())
        if not match:
            continue
        name = match.group(1).strip()
        is_base = "(base)" in name
        if is_base:
            name = name.replace("(base)", "").strip()
        adjust = (f"{match.group(5)} {int(match.group(6)):02d}:{int(match.group(7)):02d}"
                  if match.group(5) else None)
        found.append(Screw(name=name, z=float(match.group(4)),
                           adjust=adjust, is_base=is_base))
    bases = [i for i, screw in enumerate(found) if screw.is_base]
    if not bases:
        return ScrewReading(())
    start = bases[-1]
    return ScrewReading(tuple(found[start:]))


@dataclass(frozen=True)
class PreparedScrews:
    pass


def prepare_screws(transport, entry, facts=None) -> PreparedScrews:
    """Validate the driven screws flow (generic Klipper, [screws_tilt_adjust]); send nothing."""
    reason = block_reason(entry)
    if reason is not None:
        raise CalibrationRefusal(reason + " Nothing was sent.")
    if entry is not None and entry.firmware_family == "zmod":
        raise CalibrationRefusal(
            "On ZMod printers the bed-screw tune is BED_LEVEL_SCREWS_TUNE, which "
            "waits for its own confirmation at the printer — `/print-calibration "
            "screws` gives that guide on this printer instead of driving it. "
            "Nothing was sent.")
    if facts is None:
        facts = fetch_facts(transport)
    if not facts.section("screws_tilt_adjust"):
        raise CalibrationRefusal(
            "The printer's configuration has no [screws_tilt_adjust] section, so "
            "Klipper cannot measure the screws from here. Nothing was sent.")
    _require_idle_now(transport)
    return PreparedScrews()


def screws_prompt_text() -> str:
    """The run confirmation for the first screws measurement."""
    return "\n".join([
        "Measure the bed screws?",
        "",
        "This sends `SCREWS_TILT_CALCULATE`: the printer homes and probes above "
        "every bed screw, then reports each screw's height error and the exact "
        "turn it needs. Print Handy shows you the turns; you make them by hand "
        "at the printer; then it measures again to prove the result.",
        "",
        "Warnings: the printer will home and move across the bed — keep the bed "
        "clear and stay nearby. Nothing is ever saved for screws: the adjustment "
        "is physical, and your bed mesh and probe calibration may need a refresh "
        "afterwards if the bed moved a lot.",
        "",
        "The printer's state is checked again immediately before the command "
        "is sent."])


def measure_screws(transport, *, poll_attempts=60, poll_interval_s=2.0,
                   sleep=time.sleep) -> ScrewReading:
    """Send SCREWS_TILT_CALCULATE once and poll the console for its result set."""
    _require_idle_now(transport, recheck=True)
    mark = store_mark(transport)
    _send_script(transport, "SCREWS_TILT_CALCULATE", long_running=True)

    def complete(messages):
        reading = parse_screw_lines(messages)
        return reading if len(reading.screws) >= 2 else None

    reading = _poll(transport, mark, complete,
                    attempts=poll_attempts, interval_s=poll_interval_s, sleep=sleep)
    if reading is None:
        raise CalibrationError(
            "The screw measurement did not report results Print Handy could read "
            "in the time it waited. Nothing was saved (screw adjustments are "
            "physical — there is nothing to undo in software).")
    return reading


def screws_text(reading: ScrewReading, previous: ScrewReading | None = None) -> str:
    """One measurement presented: per-screw error and turn; with deltas vs the previous."""
    errors = reading.errors()
    lines = ["**Bed screw measurement** (heights relative to the base screw):", ""]
    previous_errors = previous.errors() if previous is not None else {}
    for screw in reading.screws:
        error = errors.get(screw.name)
        line = f"- **{screw.name}**: error {_fmt(error, 3)} mm"
        if screw.is_base:
            line += " (base — do not turn this one)"
        elif screw.adjust:
            line += f" — turn {screw.adjust}"
        if screw.name in previous_errors and previous is not None:
            line += (f" · was {_fmt(previous_errors[screw.name], 3)} mm "
                     f"({_delta(previous_errors[screw.name], error, 3, ' mm')})")
        lines.append(line)
    worst = max((abs(v) for v in errors.values()), default=0.0)
    if worst <= 0.02:
        lines.append("\nEvery screw is within 0.02 mm of level — that is a level bed.")
    else:
        lines.append("\nTurn the screws by the amounts above (the numbers are a clock "
                     "face: 01:15 is one full turn plus a quarter), then measure again.")
    return "\n".join(lines)


def screws_differences(first: ScrewReading, last: ScrewReading) -> dict:
    first_errors, last_errors = first.errors(), last.errors()
    return {name: {"before": first_errors.get(name), "after": last_errors.get(name),
                   "delta": (round(last_errors[name] - first_errors[name], 3)
                             if name in first_errors and name in last_errors else None)}
            for name in last_errors}


SCREWS_ZMOD_GUIDE = """**Bed screws on ZMod (guided — you run this one at the printer)**

ZMod's screw tune, `BED_LEVEL_SCREWS_TUNE`, measures with the load cells and
then waits for its own confirmation on the printer's screen / web prompt —
Print Handy cannot answer that prompt from chat, so this one is a guide.

1. Clean the nozzle first: any plastic on the tip makes the measurements
   wrong. Heat and wipe it, or run ZMod's nozzle cleaning.
2. Start the tune from the printer's console (Fluidd/Mainsail) or screen:
   `BED_LEVEL_SCREWS_TUNE`. ZMod's setup walkthrough uses
   `BED_LEVEL_SCREWS_TUNE EXTRUDER_TEMP=130 BED_TEMP=80` — a cooler nozzle
   (130 °C) so heat expansion and ooze do not distort the measurement.
3. The printer measures each screw and tells you which screw to turn and
   which way. Follow its prompts at the printer until it reports done.
4. **Afterwards the printer does NOT reset the temperatures** — set the
   extruder and bed back to 0 yourself.
5. **The bed mesh and Z offset are invalid after a screw tune** (ZMod's
   own warning): rebuild the mesh and check the Z offset before printing
   — on the native screen, start the leveling calibration from the
   stock screen.

Print Handy sent nothing to the printer."""

SCREWS_KLIPPER_GUIDE = """**Bed screws with [bed_screws] (guided — you run this one at the printer)**

This printer's config uses Klipper's `BED_SCREWS_ADJUST` tool, which is an
interactive dialogue at the nozzle — it moves to each screw and waits for
you to run `ACCEPT` or `ADJUSTED` after a paper test — so Print Handy
cannot drive it from chat.

1. Home the printer (`G28`).
2. Run `BED_SCREWS_ADJUST` from the printer's console (Fluidd/Mainsail).
3. At each screw, slide paper under the nozzle and turn that screw until
   the paper drags slightly, then send `ACCEPT` (no real change) or
   `ADJUSTED` (you turned it more than about 1/8 turn — the tool will
   schedule another round). `ABORT` leaves the tool early.
4. Repeat rounds until a whole round needs no `ADJUSTED`.
5. If the bed moved a lot, re-run your probe calibration and bed mesh
   afterwards — a tilted bed invalidates both.

Print Handy sent nothing to the printer."""


# --------------------------------------------------------------------- belts

@dataclass(frozen=True)
class PreparedBelts:
    pass


def prepare_belts(transport, entry, facts=None) -> PreparedBelts:
    """Validate the belt check (ZMod's BELTS_SHAPER_CALIBRATION present); send nothing."""
    reason = block_reason(entry)
    if reason is not None:
        raise CalibrationRefusal(reason + " Nothing was sent.")
    if facts is None:
        facts = fetch_facts(transport)
    if not facts.has_macro("BELTS_SHAPER_CALIBRATION"):
        raise CalibrationRefusal("The printer does not define ZMod's "
                                 "BELTS_SHAPER_CALIBRATION macro, so Print Handy has "
                                 "no belt check to run on it. Nothing was sent.")
    _require_idle_now(transport)
    return PreparedBelts()


def belts_prompt_text() -> str:
    """The run confirmation for the report-only belt check."""
    return "\n".join([
        "Run the belt check?",
        "",
        "This sends `BELTS_SHAPER_CALIBRATION`: the printer vibrates each axis "
        "in turn and analyses the belt frequencies (CoreXY half-axis test), "
        "producing a spectrogram you can view in the printer's web interface "
        "(Configuration → mod_data).",
        "",
        "Warnings: it is loud and the printer shakes hard for a few minutes — "
        "stay nearby and make sure nothing loose is on the printer. ZMod notes "
        "the analysis needs spare memory and swap on the printer.",
        "",
        "This is a report, not a tuning: nothing is applied and nothing is "
        "saved, whatever it finds. The printer's state is checked again "
        "immediately before the command is sent."])


def execute_belts(transport, prepared: PreparedBelts, *, poll_attempts=90,
                  poll_interval_s=5.0, sleep=time.sleep, quiet_polls=6) -> str:
    """Re-check idle, send the belt check, and report what the console answers.

    Completion is a quiet console: once new lines have appeared, several
    consecutive polls with nothing new end the wait. No output at all in
    the budget is a CalibrationError — Print Handy will not claim a run
    it could not observe.
    """
    _require_idle_now(transport, recheck=True)
    mark = store_mark(transport)
    _send_script(transport, "BELTS_SHAPER_CALIBRATION", long_running=True)
    seen, quiet = [], 0
    for attempt in range(max(1, poll_attempts)):
        if attempt:
            sleep(poll_interval_s)
        entries = _entries_since(transport, mark)
        if entries:
            mark = entries[-1][0]
            seen += [message for _when, message in entries]
            quiet = 0
        elif seen:
            quiet += 1
            if quiet >= quiet_polls:
                break
    if not seen:
        raise CalibrationError(
            "The belt check produced no console output Print Handy could read in "
            "the time it waited, so it cannot report a result. Check the printer "
            "itself — if the check ran there, its spectrogram is in the web "
            "interface (Configuration → mod_data). Nothing was applied or saved "
            "either way.")
    lines = ["**Belt check finished** — what the printer reported:", ""]
    lines += [f"- {message.strip()}" for message in seen if message.strip()][:20]
    lines += ["",
              "The full spectrogram is in the printer's web interface "
              "(Configuration → mod_data). Nothing was applied and nothing was "
              "saved — the belt check only measures. A calibration record was "
              "written — `/print-calibration records` lists it."]
    return "\n".join(lines)


# --------------------------------------------------------------------- reports and guides

def mesh_report_text(entry, facts) -> str:
    """The native/unknown-mode mesh answer: report the stored mesh, guide the rebuild.

    Stage 0: on a native-screen install the stock screen owns the mesh
    and loads it for every print; on an unknown-mode install Print Handy
    cannot tell who owns it. Either way the bot reports what is stored
    and never measures over the owner.
    """
    mode = install_mode(entry, facts)
    active = active_mesh_summary(facts.bed_mesh)
    stored = profile_mesh_summary(facts.bed_mesh, "auto")
    lines = ["**Bed mesh on this printer** (read live; nothing was sent):", ""]
    if stored is not None:
        lines.append(f"- Stored mesh the printer keeps — {stored.text()}.")
    if active is not None and (stored is None or active.text() != stored.text()):
        lines.append(f"- Currently loaded — {active.text()}.")
    if stored is None and active is None:
        lines.append("- No stored or loaded mesh could be read from the printer.")
    lines.append("")
    if mode == "native":
        lines += [
            "This printer's native screen owns the bed mesh: it loads its mesh for "
            "every print, so a mesh Print Handy measured and saved would not be the "
            "one your prints use. To rebuild it, use the stock screen's leveling "
            "calibration (from the screen: start the bed leveling there) — ZMod's "
            "screen flow cleans the nozzle, measures with the load cells and "
            "re-zeros the Z offset start point as part of the run.",
            "",
            "On a screenless install (native screen disabled) Print Handy can run "
            "this calibration for you with `/print-calibration mesh`, measuring "
            "with ZMod's AUTO_FULL_BED_LEVEL and asking before anything is saved."]
    else:
        lines += [
            "Print Handy could not tell whether this printer's native screen is in "
            "charge of the mesh (its saved display mode was unreadable), so it "
            "reports the mesh instead of measuring a new one that might fight the "
            "screen. Reconcile the display mode (the printer's settings, or "
            "`/print-handy debugging`) and try again."]
    return "\n".join(lines)


def _native_zprobe_offset(transport):
    """The native screen's Z offset from Adventurer5M.json, or None when unreadable.

    Mirrors print_handy.core.model_job.screen_z_offset: the value lives
    at leftExtruderOffset.zProbeOffset, with a recursive search as the
    fallback for firmware variants that nest it differently.
    """
    try:
        data = transport("GET", NATIVE_CONFIG_PATH)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    def numeric(value):
        return _num(value)

    nested = data.get("leftExtruderOffset")
    if isinstance(nested, dict) and "zProbeOffset" in nested:
        return numeric(nested.get("zProbeOffset"))

    def find(node):
        if isinstance(node, dict):
            if numeric(node.get("zProbeOffset")) is not None:
                return numeric(node.get("zProbeOffset"))
            for child in node.values():
                found = find(child)
                if found is not None:
                    return found
        return None

    return find(data)


_ZOFFSET_NATIVE_GUIDE = [
    "To change it: the offset can only be adjusted **during a print**, on the "
    "printer's own screen — print something, tap the bottom-right square on the "
    "touch screen, then the pencil icon, and judge by the first layer. The screen "
    "saves the value itself and uses it for the next print.",
    "Do not adjust it from Fluidd/Mainsail while the native screen is in use: "
    "the screen does not know about web changes and overwrites them at print "
    "start. (On AD5M models the screen also adds a fixed 0.025 mm, so the web "
    "interface always shows 0.025 mm more than the screen value — that is "
    "normal.)",
]
_ZOFFSET_SCREENLESS_GUIDE = [
    "To change it: adjustments happen **during a print**, from Fluidd/Mainsail "
    "or the GuppyScreen/HelixScreen panel — ZMod saves the value itself "
    "under the hood, no SAVE_CONFIG and no Z_OFFSET_APPLY commands are needed "
    "or wanted.",
    "If you are switching from the native screen, run `LOAD_ZOFFSET_NATIVE` "
    "once in the printer's console to copy the screen's offset into the "
    "screenless store.",
]
_ZOFFSET_SYNCED_NOTE = ("Creator 5 / Creator 5 Pro are the exception: their native "
                        "and screenless offsets are one synchronised value.")


def zoffset_report(transport, entry, facts=None) -> str:
    """The Z-offset report + mode-correct guide. Read-only; the Stage 0 verdict.

    There is no honest pre-print Z-offset calibration for the bot to run
    in any mode (see the module docstring), so this reports the value
    that actually owns the next print and explains how it is adjusted.
    """
    if facts is None:
        facts = fetch_facts(transport)
    label = entry.label if entry is not None else "your printer"
    if entry is None or entry.firmware_family != "zmod":
        probe_z = _num(facts.section("probe").get("z_offset"))
        lines = [f"**Z offset — {label}** (read live; nothing was sent):", ""]
        lines.append(f"- Klipper's configured probe z_offset: {_fmt(probe_z)} mm."
                     if probe_z is not None else
                     "- The printer's config reports no probe z_offset Print Handy "
                     "can read.")
        lines += ["", PROBE_CALIBRATE_GUIDE]
        return "\n".join(lines)
    mode = install_mode(entry, facts)
    lines = [f"**Z offset — {label}** (read live; nothing was sent):", ""]
    saved = _num((facts.variables.get("gcode_offsets") or {}).get("z")) \
        if isinstance(facts.variables.get("gcode_offsets"), dict) else None
    if mode == "native":
        value = _native_zprobe_offset(transport)
        lines.append(f"- Native screen Z offset (zProbeOffset): {_fmt(value)} mm"
                     + (" — this is the value the screen loads at every print start."
                        if value is not None else
                        " — the screen owns the offset but its value could not be "
                        "read, so check it on the screen itself."))
        if saved is not None and value is not None:
            lines.append(f"- The screenless store holds {_fmt(saved)} mm — a separate "
                         f"value ({_delta(value, saved, 3, ' mm')} from the screen's), "
                         "not used while the native screen is in charge and never "
                         "added to it.")
        elif saved is not None:
            lines.append(f"- The screenless store holds {_fmt(saved)} mm — a separate "
                         "value, not used while the native screen is in charge and "
                         "never added to the screen's.")
        else:
            lines.append("- The screenless saved offset is a separate store and is "
                         "not used while the native screen is in charge.")
        lines += ["", *_ZOFFSET_NATIVE_GUIDE, "", _ZOFFSET_SYNCED_NOTE]
    elif mode == "screenless":
        value = saved
        armed = facts.variables.get("load_zoffset") == 1
        lines.append(f"- Screenless saved Z offset: {_fmt(value)} mm"
                     + (" (loaded at print start: SAVE_ZMOD_DATA LOAD_ZOFFSET=1 is "
                        "armed)." if armed and value is not None else
                        " — but LOAD_ZOFFSET=1 is not armed, so this value is not "
                        "loaded at print start. Run `SAVE_ZMOD_DATA LOAD_ZOFFSET=1` "
                        "once in the printer's console to arm it."
                        if value is not None else
                        " — no numeric saved offset could be read."))
        lines.append("- The native screen's offset is a separate store and is not "
                     "used in screenless mode.")
        native_value = _native_zprobe_offset(transport)
        if native_value is not None and value is not None:
            lines.append(f"- For reference, the native screen's store holds "
                         f"{_fmt(native_value)} mm "
                         f"({_delta(value, native_value, 3, ' mm')} from the "
                         "screenless value) — if you switch back to the screen, "
                         "that is the value it will use. LOAD_ZOFFSET_NATIVE only "
                         "copies toward screenless: with its default START=0 it "
                         "copies the native screen's offset into screenless mode "
                         "and saves it (START=1, used at boot, only reports); no "
                         "command copies the screenless value back to the screen.")
        lines += ["", *_ZOFFSET_SCREENLESS_GUIDE, "", _ZOFFSET_SYNCED_NOTE]
    else:
        lines.append("- Print Handy could not tell which store owns the Z offset: "
                     "the printer's saved display mode (display_off) was neither "
                     "0 (native) nor 1 (screenless). The two stores are separate "
                     "values on ZMod — the native screen's zProbeOffset and the "
                     "screenless saved gcode offset — and guessing which one your "
                     "next print uses would be worse than saying so.")
        lines += ["", "Reconcile the display mode in the printer's settings, then "
                      "run `/print-calibration zoffset` again."]
    return "\n".join(lines)


PROBE_CALIBRATE_GUIDE = """**Adjusting the Z offset (guided — you run this one at the printer)**

Klipper's `PROBE_CALIBRATE` is an interactive dialogue: it probes, then you
move the nozzle in small steps with `TESTZ` while doing a paper test at
the bed, and finish with `ACCEPT`. It needs hands and eyes at the printer,
so Print Handy guides it instead of driving it.

1. Home the printer and heat the nozzle and bed to your printing
   temperatures (offsets measured cold are wrong when hot).
2. From the printer's console (Fluidd/Mainsail), run `PROBE_CALIBRATE`.
3. Follow the paper test: `TESTZ Z=-0.01` lowers the nozzle a hundredth,
   `TESTZ Z=+0.01` raises it; adjust until the paper drags slightly.
4. `ACCEPT` the position, then `SAVE_CONFIG` to store it — Klipper keeps
   the result only in memory until you save.
5. Re-check with a first-layer test print; small live adjustments during
   a print use `SET_GCODE_OFFSET Z_ADJUST=... MOVE=1`.

Print Handy sent nothing to the printer."""


# --------------------------------------------------------------------- records

def make_record(*, printer, calibration, outcome, before=None, after=None,
                differences=None, detail="", when=None) -> dict:
    """One calibration record (JSON-able), as written by append_record."""
    moment = when or datetime.now(timezone.utc)
    return {"printer": printer, "calibration": calibration,
            "timestamp": moment.isoformat(timespec="seconds"),
            "outcome": outcome, "before": before, "after": after,
            "differences": differences, "detail": detail}


def append_record(path, record) -> None:
    """Append one record to the printer's calibration_records.json (capped at 100).

    Best-effort bookkeeping: a malformed existing file is replaced rather
    than failing the calibration, matching how the state-dir records are
    treated elsewhere.
    """
    path = Path(path)
    records = []
    try:
        existing = json.loads(path.read_text())
        if isinstance(existing, list):
            records = [item for item in existing if isinstance(item, dict)]
    except Exception:
        records = []
    records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records[-100:], indent=1))


def read_records(path, limit=10) -> list:
    """The most recent records, newest first; [] when there are none or the file is off."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    records = [item for item in data if isinstance(item, dict)]
    return list(reversed(records))[:max(0, limit)]


_OUTCOME_WORDS = {"accepted": "accepted and saved", "discarded": "discarded",
                  "failed": "failed", "reported": "report/guide only",
                  "unanswered": "no answer — nothing sent"}


def _record_summary(record) -> str:
    differences = record.get("differences") or {}
    parts = []
    if record.get("calibration") == "pid" or str(record.get("calibration", "")).startswith("pid"):
        for name, label in (("kp", "Kp"), ("ki", "Ki"), ("kd", "Kd")):
            diff = differences.get(name)
            if isinstance(diff, dict) and diff.get("delta") is not None:
                parts.append(f"{label} {_fmt(diff.get('before'))} → {_fmt(diff.get('after'))} "
                             f"({_delta(diff.get('before'), diff.get('after'))})")
    elif isinstance(differences.get("range_mm"), dict):
        diff = differences["range_mm"]
        if diff.get("delta") is not None:
            parts.append(f"mesh range {_fmt(diff.get('before'))} → {_fmt(diff.get('after'))} mm "
                         f"({_delta(diff.get('before'), diff.get('after'), 3, ' mm')})")
    return "; ".join(parts)


def records_text(records, label) -> str:
    """The `/print-calibration records` answer: recent records, newest first."""
    if not records:
        return (f"No calibration records for {label} yet. Records are written when a "
                "`/print-calibration` run finishes — accepted, discarded or failed — "
                "and when a report or guide is shown.")
    lines = [f"**Recent calibrations — {label}** (newest first):", ""]
    for record in records:
        outcome = _OUTCOME_WORDS.get(record.get("outcome"), str(record.get("outcome")))
        line = (f"- {record.get('timestamp', 'unknown time')} — "
                f"**{record.get('calibration', 'calibration')}**: {outcome}")
        summary = _record_summary(record)
        if summary:
            line += f" ({summary})"
        if record.get("detail"):
            line += f" — {record['detail']}"
        lines.append(line)
    return "\n".join(lines)
