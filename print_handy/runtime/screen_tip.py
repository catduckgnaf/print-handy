"""Put the roast of the printer's last print on its displays.

HelixScreen's tips widget reads one Moonraker database item and shows it in
place of its tips, with ``details`` behind the tap. This module writes that
item: the exact roast line the print's completion, failure, cancel or
spaghetti post carried, never a fresh pick. It is best effort and
fire-and-forget: a printer without Moonraker, an unreachable one, or any error
leaves the chat post untouched.

The record shape is shared with HelixScreen (``include/featured_tip.h``):
namespace ``helix-screen``, key ``featured_tip``, value
``{"id", "title", "details", "source", "posted_at"}``. ``id`` is stable for one
print's post, so a retried delivery does not show twice.

The publish is a fan-out over sinks (#1672 Part 2), each independent, each
on its own thread:

- the Moonraker sink above, for HelixScreen;
- an MQTT sink on the [notify] broker, for every printer family: a
  retained state message at ``<mqtt_topic>/<printer_id>/last_roast``
  carrying the same record plus ``printer_id`` and ``status``, and —
  the first time a printer's roast goes out after start — a retained
  Home Assistant discovery config, so ``sensor.<printer>_last_roast``
  appears with the full line and details as attributes. Printers with
  no Moonraker screen (Bambu, Prusa, stock Flashforge) get a display
  surface this way; Moonraker printers get both sinks.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)

NAMESPACE = "helix-screen"
KEY = "featured_tip"
SOURCE = "Print Handy"
TIMEOUT_S = 5

# Printers whose Home Assistant discovery config has gone out since
# start: the config is retained, so once per printer per run is enough.
_DISCOVERY_SENT: set[str] = set()
_DISCOVERY_LOCK = threading.Lock()

# How each roast bank's print ended, for the line behind the tap.
_ENDINGS = {
    "completed": "finished",
    "failed": "stopped with an error",
    "cancelled": "was cancelled",
    "spaghetti": "turned into spaghetti",
}


def _moonraker_url(printer_id: str | None) -> str:
    from .. import settings
    if printer_id:
        try:
            return str(settings.by_id(str(printer_id)).get("moonraker_url") or "")
        except Exception:
            logger.debug("screen tip: unknown printer %r, using the default", printer_id)
    return str(settings.current()["services"].get("moonraker_url") or "")


def event_id(status: str, key: str | None, line: str) -> str:
    """A stable id for one print's roast: its post key, else the line itself."""
    basis = f"{status}:{key}" if key else f"{status}:{line}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def details(title: str, status: str, facts: Mapping[str, Any] | None = None) -> str:
    """What a tap on the roast opens: which print it was about and how it went."""
    facts = dict(facts or {})
    bits = []
    try:
        if int(facts.get("print_number") or 0) > 0:
            bits.append(f"print #{int(facts['print_number'])}")
        if int(facts.get("copies") or 0) > 1:
            bits.append(f"{int(facts['copies'])} copies")
    except (TypeError, ValueError) as exc:
        logger.debug("screen tip: bad print number/copies (%r, %r): %s",
                     facts.get("print_number"), facts.get("copies"), exc)
    try:
        if facts.get("duration_s"):
            from ..core.roast import human_duration
            bits.append(human_duration(float(facts["duration_s"])))
    except (TypeError, ValueError) as exc:
        logger.debug("screen tip: bad duration %r: %s", facts.get("duration_s"), exc)
    material = " ".join(str(facts[k]).strip() for k in ("color", "material")
                        if str(facts.get(k) or "").strip())
    if material:
        bits.append(material)
    if facts.get("grams"):
        try:
            bits.append(f"{round(float(facts['grams']))} g")
        except (TypeError, ValueError) as exc:
            logger.debug("screen tip: bad grams %r: %s", facts.get("grams"), exc)
    ending = _ENDINGS.get(status, "finished")
    text = f"Your last print, {title}, {ending}."
    return f"{text} {', '.join(bits)}." if bits else text


def record(*, event_id: str, title: str, details: str = "", now: float | None = None) -> dict[str, Any]:
    """The database value HelixScreen reads."""
    return {
        "id": event_id,
        "title": title,
        "details": details,
        "source": SOURCE,
        "posted_at": round(time.time() if now is None else now, 3),
    }


def _post(url: str, value: dict[str, Any]) -> None:
    from .. import moonraker
    moonraker.url_json(url.rstrip("/") + "/server/database/item", method="POST",
                       data={"namespace": NAMESPACE, "key": KEY, "value": value},
                       headers={"Content-Type": "application/json"},
                       timeout=TIMEOUT_S, moonraker=True)


def _publish_moonraker(value: dict[str, Any], printer_id: str | None) -> bool:
    """Queue the HelixScreen database write. True when a write was queued."""
    url = _moonraker_url(printer_id)
    if not url:
        return False

    def send() -> None:
        try:
            _post(url, value)
        except Exception as exc:
            logger.info("screen tip not published: %s", exc)

    threading.Thread(target=send, name="screen-tip", daemon=True).start()
    return True


def _mqtt_roast_on() -> dict[str, Any] | None:
    """The [notify] config when the MQTT roast sink is on, else None.

    On when MQTT events are on, the last-roast sensor is not switched
    off (``notify.mqtt_roast``, on by default) and roasts themselves are
    on — the same ``vision.roast_enabled`` switch as every roast sink.
    """
    try:
        from .. import settings
        cfg = settings.current()
        notify = cfg.get("notify") or {}
        if notify.get("mqtt_enabled") is not True:
            return None
        if notify.get("mqtt_roast", True) is not True:
            return None
        if (cfg.get("vision") or {}).get("roast_enabled", True) is not True:
            return None
        return notify
    except Exception:
        logger.debug("mqtt roast sink unavailable", exc_info=True)
        return None


def roast_state_topic(notify: Mapping[str, Any], printer_id: str) -> str:
    """The retained topic carrying one printer's last roast."""
    root = str(notify.get("mqtt_topic") or "print-handy").strip("/") or "print-handy"
    return f"{root}/{printer_id}/last_roast"


def _printer_label(printer_id: str) -> str:
    """The printer's display name for the discovery device block."""
    try:
        from .. import settings
        name = (settings.by_id(printer_id).get("printer") or {}).get("name")
        if name:
            return str(name)
    except Exception:
        logger.debug("mqtt roast: no label for printer %r", printer_id)
    return printer_id


def discovery_config(notify: Mapping[str, Any], printer_id: str) -> tuple[str, dict[str, Any]]:
    """The Home Assistant discovery topic and config for one printer's sensor.

    Home Assistant caps an entity state at 255 characters, so the value
    template truncates the title there; the full line stays available
    in the ``title`` attribute through ``json_attributes_topic``.
    """
    topic = roast_state_topic(notify, printer_id)
    config = {
        "name": "Last roast",
        "state_topic": topic,
        "value_template": "{{ value_json.title[:255] }}",
        "json_attributes_topic": topic,
        "unique_id": f"print_handy_{printer_id}_last_roast",
        "device": {
            "identifiers": [f"print_handy_{printer_id}"],
            "name": _printer_label(printer_id),
            "manufacturer": "Print Handy",
        },
    }
    return f"homeassistant/sensor/print_handy_{printer_id}/last_roast/config", config


def _mqtt_send(value: dict[str, Any], status: str, printer_id: str,
               notify: Mapping[str, Any]) -> None:
    """The MQTT sink's thread body: discovery once per printer, then the state."""
    from .. import notify as notify_bus
    try:
        payload = dict(value)
        payload["printer_id"] = printer_id
        payload["status"] = status
        with _DISCOVERY_LOCK:
            first = printer_id not in _DISCOVERY_SENT
        if first:
            topic, config = discovery_config(notify, printer_id)
            if notify_bus.publish_retained(topic, json.dumps(config)):
                with _DISCOVERY_LOCK:
                    _DISCOVERY_SENT.add(printer_id)
            else:
                logger.info("mqtt roast discovery not published for %s", printer_id)
        if not notify_bus.publish_retained(
                roast_state_topic(notify, printer_id), json.dumps(payload)):
            logger.info("mqtt roast not published for %s", printer_id)
    except Exception as exc:
        logger.info("mqtt roast not published: %s", exc)


def _publish_mqtt(value: dict[str, Any], status: str, printer_id: str | None) -> bool:
    """Queue the MQTT last-roast publish. True when a publish was queued."""
    notify = _mqtt_roast_on()
    if notify is None:
        return False
    pid = str(printer_id or "default")
    threading.Thread(target=_mqtt_send, args=(value, status, pid, notify),
                     name="screen-tip-mqtt", daemon=True).start()
    return True


def clear_retained_roasts() -> int:
    """Remove the retained last-roast sensors when the sensor is switched off (#1674 review).

    Turning off ``notify.mqtt_roast`` or ``vision.roast_enabled`` stops new
    publishes, but the discovery config and state already on the broker are
    retained, so Home Assistant would keep a stale sensor. With MQTT still
    on and the sensor off, an empty retained payload on each roster
    printer's discovery and state topics clears them; Home Assistant
    removes an entity whose discovery config is emptied. Runs once at
    start. With MQTT itself off there is no broker to reach, and nothing
    is sent. Returns the number of topics cleared; never raises.
    """
    from .. import notify as notify_bus
    from .. import settings
    try:
        cfg = settings.current()
        notify = cfg.get("notify") or {}
        if notify.get("mqtt_enabled") is not True or _mqtt_roast_on() is not None:
            return 0
        cleared = 0
        for entry in settings.printers(cfg):
            pid = str(entry.get("id") or "")
            if not pid:
                continue
            topic, _config = discovery_config(notify, pid)
            for target in (topic, roast_state_topic(notify, pid)):
                if notify_bus.publish_retained(target, ""):
                    cleared += 1
        return cleared
    except Exception as exc:
        logger.info("mqtt roast sensors not cleared: %s", exc)
        return 0


def clear_retained_roasts_in_background() -> None:
    """``clear_retained_roasts`` on a daemon thread, so start never waits on the broker."""
    threading.Thread(target=clear_retained_roasts, name="screen-tip-mqtt-clear",
                     daemon=True).start()


def publish_roast(line: str, *, title: str, status: str = "completed", key: str | None = None,
                  facts: Mapping[str, Any] | None = None, printer_id: str | None = None) -> bool:
    """Queue a print's roast for that printer's displays. True when a sink queued it.

    ``printer_id`` is the roster id of the printer the print ran on; None is
    the default printer. The sinks are independent: the Moonraker /
    HelixScreen write and the MQTT last-roast publish each go out (or
    fail) on their own thread, and neither delays or changes the chat
    post the line came from.
    """
    try:
        if not line:
            return False
        value = record(event_id=event_id(status, key, line), title=line,
                       details=details(title, status, facts))
        queued = _publish_moonraker(value, printer_id)
        queued = _publish_mqtt(value, status, printer_id) or queued
        return queued
    except Exception:
        logger.debug("screen tip unavailable", exc_info=True)
        return False


__all__ = ("KEY", "NAMESPACE", "SOURCE", "clear_retained_roasts", "details", "discovery_config", "event_id",
           "publish_roast", "record", "roast_state_topic")
