"""Read-only thermal probe for a printer being connected (#741, Stage 3 of #740).

The per-model registry (``[printer.thermal]``, #720) records what published
sources settle for a model. Some facts are per install: an optional fan kit,
a chamber reading the connected surface may or may not carry, a fan mapping
the vendor never published. The answer to those sits on the printer itself,
so guided onboarding asks it, once the connection test has passed:

- moonraker: ``GET /printer/objects/list``, then one ``objects/query`` for
  the temperature, fan, heater and button objects (plus ``print_stats``).
  Chamber-named ``temperature_sensor`` / ``temperature_fan`` /
  ``heater_generic`` objects carry the chamber reading (an exhaust-named
  ``temperature_fan`` too: the Sovol Zero reads its chamber that way);
  ``fan_generic`` / ``generic_fan`` / ``temperature_fan`` objects are
  sorted into roles by name; door- or lid-named ``gcode_button`` /
  ``filament_switch_sensor`` objects make the door state knowable. The
  object list is complete for that firmware, so a role with no object is
  a candidate ``absent``/``no``, except where a fan the name rule cannot
  place might be that role.
- bambulab_lan: one full push report. ``print.chamber_temper`` (X1
  generation) or ``device.ctc.info.temp`` bits 0-15 (P2S/H2 generation)
  settle the chamber reading; ``cooling_fan_speed`` / ``big_fan1_speed`` /
  ``big_fan2_speed`` settle part, auxiliary and exhaust (P1/P2/P3 per
  BambuStudio ``DevFan.h``). Door state is not probed: no published field.
- elegoo_sdcp: one status frame. ``TempOfBox`` and the fan channels
  (``ModelFan`` / ``AuxiliaryFan`` / ``BoxFan``) report what the printer has.
- prusalink: a schema check of ``/api/v1/status`` on the connected firmware.
  Today it carries no chamber or door field, which confirms ``no``; a
  firmware that adds one is picked up by name.
- octoprint and every other connection: cannot be probed (a stock OctoPrint
  has no chamber channel, and nothing is guessed from tool or bed channels).

Safety: every call is a read (a GET, a status request, the push report). No
fan, heater or G-code command is ever sent. A busy printer is not probed. A
failed or partial probe returns no findings, so it changes nothing.

Findings are proposals. Only the ones that differ from the model record are
shown, the operator confirms or declines each, and confirmed values land in
that printer's hardware record under ``thermal_install`` with provenance
``operator-confirmed``. The per-model registry is never written.
"""
from __future__ import annotations

import datetime
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

PROVENANCE = "operator-confirmed"
SOURCE = "probed from the connected printer on {date}"
_PRESENCE = ("present", "absent")
_KNOWABLE = ("yes", "no")
# The fields a probe may settle and the values each accepts (None: a mapping
# string). Chamber heater control is never probed: presence is not control.
FIELDS = {
    "door_lid_state_knowable": _KNOWABLE,
    "chamber_heater_present": ("present",),
    "chamber_sensor_present": _PRESENCE,
    "chamber_sensor_fresh_reading": _KNOWABLE,
    "fan_part_cooling_present": _PRESENCE, "fan_part_cooling_mapping": None,
    "fan_auxiliary_present": _PRESENCE, "fan_auxiliary_mapping": None,
    "fan_exhaust_present": _PRESENCE, "fan_exhaust_mapping": None,
    "fan_filtration_present": _PRESENCE, "fan_filtration_mapping": None,
}
_ROLES = ("part_cooling", "auxiliary", "exhaust", "filtration")


@dataclass(frozen=True)
class Finding:
    field: str
    value: str
    evidence: str


@dataclass(frozen=True)
class ThermalProbe:
    """What one probe read: ``probed`` is False when nothing could be read
    (no surface, a busy printer, a failure); ``note`` says why, or names
    what was seen but could not be placed."""
    probed: bool
    findings: tuple = ()
    note: str = ""


def valid(field, value):
    """True when ``value`` is one the probe may record for ``field``."""
    if field not in FIELDS or not isinstance(value, str) or not value.strip():
        return False
    allowed = FIELDS[field]
    return value in allowed if allowed else (len(value) <= 120 and value != "unknown")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _celsius(value):
    return f"{float(value):g} °C"


# ---------------------------------------------------------------------- Moonraker / Klipper

_CHAMBER = re.compile(r"chamber|enclosure|cavity|box", re.I)
_NOT_CHAMBER = re.compile(r"mcu|electronic|board|host|driver", re.I)
_DOOR = re.compile(r"door|lid", re.I)
_ROLE_NAMES = (("exhaust", re.compile(r"exhaust|vent", re.I)),
               ("filtration", re.compile(r"filt|purif|carbon|hepa|scrub|nevermore", re.I)),
               ("auxiliary", re.compile(r"aux|side", re.I)),
               ("part_cooling", re.compile(r"part", re.I)))
MOONRAKER_TYPES = ("temperature_sensor", "temperature_fan", "heater_generic",
                   "fan_generic", "generic_fan", "gcode_button", "filament_switch_sensor")
_FAN_TYPES = ("fan_generic", "generic_fan", "temperature_fan")


def _split(obj):
    kind, _, name = str(obj).partition(" ")
    return kind, name


def _role(name):
    roles = [role for role, rx in _ROLE_NAMES if rx.search(name)]
    return roles[0] if len(roles) == 1 else None


def moonraker_findings(objects, status):
    """Findings from a complete Moonraker object list and its queried status.

    Returns (findings, note). The note names fan objects whose role the name
    rule cannot place; while any exist, no fan role is proposed absent.
    """
    status = status if isinstance(status, dict) else {}
    objects = [str(o) for o in objects or ()]
    found = []
    chamber, heaters, doors, unplaced = [], [], [], []
    fans = {role: [] for role in _ROLES}
    if "fan" in objects:
        fans["part_cooling"].append(("fan", "M106"))
    for obj in objects:
        kind, name = _split(obj)
        if kind in ("temperature_sensor", "temperature_fan", "heater_generic") and name \
                and not _NOT_CHAMBER.search(name) and (
                    _CHAMBER.search(name) or (kind == "temperature_fan" and _role(name) == "exhaust")):
            chamber.append(obj)
            if kind == "heater_generic":
                heaters.append(obj)
        if kind in _FAN_TYPES and name:
            role = _role(name)
            if role:
                fans[role].append((obj, obj))
            else:
                unplaced.append(obj)
        if kind in ("gcode_button", "filament_switch_sensor") and _DOOR.search(name):
            doors.append(obj)
    if chamber:
        readings = [(obj, _number((status.get(obj) or {}).get("temperature"))) for obj in chamber]
        live = [(obj, t) for obj, t in readings if t is not None]
        shown = "; ".join(f"`{obj}` reads {_celsius(t)}" if t is not None else f"`{obj}` (no reading)"
                          for obj, t in readings)
        found.append(Finding("chamber_sensor_present", "present", f"the printer reports {shown}"))
        if live:
            found.append(Finding("chamber_sensor_fresh_reading", "yes",
                                 f"`{live[0][0]}` returned a live temperature ({_celsius(live[0][1])})"))
        else:
            found.append(Finding("chamber_sensor_fresh_reading", "no",
                                 "the chamber object returned no temperature"))
    else:
        found.append(Finding("chamber_sensor_present", "absent",
                             "the printer's complete object list has no chamber temperature object"))
        found.append(Finding("chamber_sensor_fresh_reading", "no",
                             "the printer's complete object list has no chamber temperature object"))
    if heaters:
        found.append(Finding("chamber_heater_present", "present",
                             f"the printer reports `{heaters[0]}`, a heater with a chamber name"))
    for role in _ROLES:
        hits = fans[role]
        if hits:
            names = " + ".join(f"`{obj}`" for obj, _m in hits)
            found.append(Finding(f"fan_{role}_present", "present", f"the printer reports {names}"))
            found.append(Finding(f"fan_{role}_mapping", " + ".join(m for _o, m in hits),
                                 f"the Klipper object{'s' if len(hits) > 1 else ''} {names}"))
        elif not unplaced:
            found.append(Finding(f"fan_{role}_present", "absent",
                                 "the printer's complete object list has no fan object for this role"))
    if doors:
        found.append(Finding("door_lid_state_knowable", "yes",
                             f"the printer reports `{doors[0]}`, a door or lid switch it can read"))
    else:
        found.append(Finding("door_lid_state_knowable", "no",
                             "the printer's complete object list has no door or lid switch"))
    note = ""
    if unplaced:
        note = ("Fan objects whose role the name does not show (no fan role was proposed absent "
                "because of them; answer those questions yourself): "
                + ", ".join(f"`{obj}`" for obj in unplaced))
    return found, note


def _get_json(url, headers, timeout_s, opener):
    from .connection_probe import _http_get_json
    return _http_get_json(url, headers=headers, timeout_s=timeout_s, opener=opener)[1]


def _probe_moonraker(fields, secrets, timeout_s, opener):
    url = str(fields.get("moonraker_url") or "").strip().rstrip("/")
    if not url:
        return ThermalProbe(False, note="no Moonraker URL was given")
    headers = {"X-Api-Key": secrets["moonraker_api_key"]} if secrets.get("moonraker_api_key") else {}
    listing = _get_json(url + "/printer/objects/list", headers, timeout_s, opener)
    objects = ((listing or {}).get("result") or {}).get("objects")
    if not isinstance(objects, list):
        return ThermalProbe(False, note="Moonraker answered without an object list")
    wanted = [o for o in objects if isinstance(o, str)
              and (o == "fan" or o == "print_stats" or _split(o)[0] in MOONRAKER_TYPES)]
    query = url + "/printer/objects/query?" + "&".join(urllib.parse.quote(o) for o in wanted)
    status = ((_get_json(query, headers, timeout_s, opener) or {}).get("result") or {}).get("status")
    if not isinstance(status, dict):
        return ThermalProbe(False, note="Moonraker answered the object query without a status")
    state = str((status.get("print_stats") or {}).get("state") or "").lower()
    if state in ("printing", "paused"):
        return ThermalProbe(False, note=f"the printer is {state}; the thermal probe only runs on an idle printer")
    found, note = moonraker_findings(objects, status)
    return ThermalProbe(True, tuple(found), note)


# ---------------------------------------------------------------------- Bambu Lab LAN (MQTT)

# BambuStudio DevFan.h: 1 part, 2 auxiliary, 3 chamber exhaust; DevFan.cpp names the report fields.
_BAMBU_FANS = (("part_cooling", "cooling_fan_speed", "M106 P1"),
               ("auxiliary", "big_fan1_speed", "M106 P2"),
               ("exhaust", "big_fan2_speed", "M106 P3"))
_BAMBU_BUSY = ("RUNNING", "PREPARE", "SLICING", "PAUSE")


def bambu_findings(report):
    """Findings from one full Bambu push report (``{"print": {...}}`` or the print dict)."""
    report = report if isinstance(report, dict) else {}
    data = report.get("print") if isinstance(report.get("print"), dict) else report
    found = []
    reading, where = _number(data.get("chamber_temper")), "print.chamber_temper"
    if reading is None:
        device = data.get("device") if isinstance(data.get("device"), dict) else report.get("device")
        raw = (((device or {}).get("ctc") or {}).get("info") or {}).get("temp") if isinstance(device, dict) else None
        if _number(raw) is not None:
            reading, where = int(raw) & 0xFFFF, "device.ctc.info.temp"
    if reading is not None:
        found.append(Finding("chamber_sensor_present", "present", f"the report carries `{where}` = {_celsius(reading)}"))
        found.append(Finding("chamber_sensor_fresh_reading", "yes", f"`{where}` is in the live report ({_celsius(reading)})"))
    else:
        text = "the full report carries neither `print.chamber_temper` nor `device.ctc.info.temp`"
        found.append(Finding("chamber_sensor_present", "absent", text))
        found.append(Finding("chamber_sensor_fresh_reading", "no", text))
    for role, key, mapping in _BAMBU_FANS:
        if key in data:
            found.append(Finding(f"fan_{role}_present", "present", f"the report carries `print.{key}` = {data[key]!r}"))
            found.append(Finding(f"fan_{role}_mapping", mapping, f"BambuStudio's fan index for `print.{key}`"))
        else:
            found.append(Finding(f"fan_{role}_present", "absent", f"the full report has no `print.{key}`"))
    return found


def _probe_bambu(fields, secrets, timeout_s, factory):
    from ..printers.bambulab.client import BambuLanClient
    if not secrets.get("access_code"):
        return ThermalProbe(False, note="no LAN access code is stored")
    client = (factory or BambuLanClient)(str(fields.get("lan_host") or "").strip(),
                                         str(fields.get("lan_serial") or "").strip(),
                                         secrets["access_code"], timeout_s=max(timeout_s, 15.0))
    report = client.snapshot()
    data = (report or {}).get("print") or {}
    state = str(data.get("gcode_state") or "").upper()
    if state in _BAMBU_BUSY:
        return ThermalProbe(False, note=f"the printer is busy ({state}); the thermal probe only runs on an idle printer")
    return ThermalProbe(True, tuple(bambu_findings(report)),
                        "Door state is not probed on Bambu Lab printers: no published report field carries it.")


# ---------------------------------------------------------------------- Elegoo SDCP

_SDCP_FANS = (("part_cooling", "ModelFan"), ("auxiliary", "AuxiliaryFan"), ("exhaust", "BoxFan"))


def sdcp_findings(frame):
    """Findings from one SDCP status answer (``{"Status": {...}}``)."""
    status = frame.get("Status") if isinstance(frame, dict) and isinstance(frame.get("Status"), dict) else {}
    found = []
    box = _number(status.get("TempOfBox"))
    if box is not None:
        found.append(Finding("chamber_sensor_present", "present", f"the status frame carries `TempOfBox` = {_celsius(box)}"))
        found.append(Finding("chamber_sensor_fresh_reading", "yes", f"`TempOfBox` is in the live status frame ({_celsius(box)})"))
    elif status:
        found.append(Finding("chamber_sensor_fresh_reading", "no", "the status frame has no `TempOfBox`"))
    speeds = status.get("CurrentFanSpeed", status.get("CurrenFanSpeed"))
    if isinstance(speeds, dict):
        for role, key in _SDCP_FANS:
            if key in speeds:
                found.append(Finding(f"fan_{role}_present", "present", f"the status frame reports the `{key}` channel"))
            else:
                found.append(Finding(f"fan_{role}_present", "absent", f"the status frame's fan channels have no `{key}`"))
    return found


def _probe_sdcp(fields, _secrets, timeout_s, factory):
    from ..printers.elegoo_sdcp.adapter import _BUSY_CODES, _status_code
    from ..printers.elegoo_sdcp.client import SdcpClient
    client = (factory or SdcpClient)(str(fields.get("host") or "").strip(), request_timeout=timeout_s)
    try:
        frame = client.status()
    finally:
        try:
            client.close()
        except Exception:
            pass
    if _status_code(frame) in _BUSY_CODES:
        return ThermalProbe(False, note="the printer is busy; the thermal probe only runs on an idle printer")
    found = sdcp_findings(frame)
    return ThermalProbe(bool(found), tuple(found), "" if found else "the status frame carried no thermal fields")


# ---------------------------------------------------------------------- PrusaLink

_PRUSA_CHAMBER = re.compile(r"chamber|enclosure|box", re.I)
_PRUSA_BUSY = ("PRINTING", "PAUSED", "BUSY")


def prusalink_findings(status, firmware=""):
    """Schema check of a PrusaLink ``/api/v1/status`` answer on the connected firmware."""
    printer = status.get("printer") if isinstance(status, dict) and isinstance(status.get("printer"), dict) else None
    if printer is None:
        return []
    on = f" on firmware {firmware}" if firmware else ""
    found = []
    chamber = [(k, _number(v)) for k, v in printer.items() if _PRUSA_CHAMBER.search(str(k))]
    live = [(k, v) for k, v in chamber if v is not None]
    if live:
        found.append(Finding("chamber_sensor_present", "present",
                             f"PrusaLink{on} reports `{live[0][0]}` = {_celsius(live[0][1])}"))
        found.append(Finding("chamber_sensor_fresh_reading", "yes", f"PrusaLink{on} reports `{live[0][0]}` live"))
    else:
        found.append(Finding("chamber_sensor_fresh_reading", "no", f"PrusaLink's status{on} has no chamber field"))
    doors = [k for k in printer if _DOOR.search(str(k))]
    if doors:
        found.append(Finding("door_lid_state_knowable", "yes", f"PrusaLink{on} reports `{doors[0]}`"))
    else:
        found.append(Finding("door_lid_state_knowable", "no", f"PrusaLink's status{on} has no door or lid field"))
    return found


def _probe_prusa(fields, secrets, _timeout_s, factory):
    from ..printers.prusa.client import PrusaLinkClient
    if not secrets.get("api_key"):
        return ThermalProbe(False, note="no PrusaLink API key is stored")
    client = (factory or PrusaLinkClient)(str(fields.get("link_host") or "").strip(), secrets["api_key"])
    info = client.info()
    status = client.status()
    state = str(((status or {}).get("printer") or {}).get("state") or "").upper()
    if state in _PRUSA_BUSY:
        return ThermalProbe(False, note=f"the printer is {state.lower()}; the thermal probe only runs on an idle printer")
    found = prusalink_findings(status, str((info or {}).get("firmware") or ""))
    return ThermalProbe(bool(found), tuple(found), "" if found else "PrusaLink answered without a printer status")


# ---------------------------------------------------------------------- entry points

_CANNOT = {
    "octoprint": "OctoPrint has no chamber channel on a stock install, so this printer's thermal "
                 "facts cannot be probed; answer the thermal questions yourself.",
}
_PROBES = {
    "moonraker": lambda f, s, t, kw: _probe_moonraker(f, s, t, kw.get("opener") or urllib.request.urlopen),
    "bambulab_lan": lambda f, s, t, kw: _probe_bambu(f, s, t, kw.get("bambu_factory")),
    "elegoo_sdcp": lambda f, s, t, kw: _probe_sdcp(f, s, t, kw.get("sdcp_factory")),
    "prusalink": lambda f, s, t, kw: _probe_prusa(f, s, t, kw.get("prusa_factory")),
}


def probe(connection, fields, secrets=None, *, timeout_s=10.0, **factories):
    """Read the connected printer's thermal facts. Never raises, never writes.

    Same arguments as ``connection_probe.probe``, which the caller must have
    passed first. A connection with no thermal surface, a busy printer and
    any failure all return ``probed=False`` with no findings.
    """
    connection = str(connection or "")
    if connection in _CANNOT:
        return ThermalProbe(False, note=_CANNOT[connection])
    handler = _PROBES.get(connection)
    if handler is None:
        return ThermalProbe(False, note="this printer's connection has no surface the thermal probe can "
                                        "read; answer the thermal questions yourself.")
    try:
        return handler(dict(fields or {}), dict(secrets or {}), float(timeout_s), factories)
    except Exception as exc:  # a partial read is no read: nothing is proposed
        return ThermalProbe(False, note=f"the thermal probe could not read the printer ({type(exc).__name__}); "
                                        "nothing was proposed or recorded.")


def proposals(result, thermal):
    """The findings worth confirming, as JSON-safe dicts for a setup session.

    A finding that matches the model record adds nothing and is dropped. One
    that disagrees with a known model value is kept and says so: the model
    record is never overridden silently.
    """
    out = []
    for finding in getattr(result, "findings", ()) or ():
        if not valid(finding.field, finding.value):
            continue
        model_value = getattr(thermal, finding.field, "unknown")
        if model_value == finding.value:
            continue
        out.append({"field": finding.field, "value": finding.value,
                    "evidence": finding.evidence, "model_value": model_value})
    return out


def proposal_lines(items):
    """Numbered operator-facing lines, one per proposal: question, proposed value, evidence."""
    from ..knowledge.thermal import question_for
    lines = []
    for index, item in enumerate(items, 1):
        line = (f"{index}. {question_for(item['field'])} Proposed: **{item['value']}**. "
                f"Evidence: {item['evidence']}.")
        if item.get("model_value") not in (None, "", "unknown"):
            line += f" The model record says {item['model_value']}; confirming records this printer's value."
        lines.append(line)
    return lines


# ---------------------------------------------------------------------- the per-printer record

def record_confirmed(printer_id, model_key, accepted, *, today=None):
    """Store confirmed proposals in the printer's hardware record. Returns how many.

    Nothing accepted (declined, or a probe that found nothing) leaves the
    record untouched. Facts recorded for another model are dropped first:
    a printer whose model changed starts over.
    """
    clean = [item for item in accepted or ()
             if isinstance(item, dict) and valid(item.get("field"), item.get("value"))]
    if not clean:
        return 0
    from . import printer_state
    record = printer_state.load(printer_id)
    block = record.get("thermal_install")
    if not isinstance(block, dict) or block.get("model") != model_key or not isinstance(block.get("facts"), dict):
        block = {"model": model_key, "facts": {}}
    source = SOURCE.format(date=(today or datetime.date.today()).isoformat())
    for item in clean:
        block["facts"][item["field"]] = {"value": item["value"], "provenance": PROVENANCE,
                                         "source": source, "evidence": str(item.get("evidence") or "")}
    record["thermal_install"] = block
    printer_state.save(record, printer_id)
    return len(clean)


def install_facts(printer_id, model_key):
    """The confirmed facts recorded for this printer and model: {field: fact}, {} when none."""
    if not printer_id or not model_key:
        return {}
    try:
        from . import printer_state
        block = printer_state.load(printer_id).get("thermal_install")
    except Exception:
        return {}
    if not isinstance(block, dict) or block.get("model") != model_key or not isinstance(block.get("facts"), dict):
        return {}
    return {field: fact for field, fact in block["facts"].items()
            if isinstance(fact, dict) and valid(field, fact.get("value"))
            and fact.get("provenance") == PROVENANCE}
