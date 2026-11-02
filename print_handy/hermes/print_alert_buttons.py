"""Stop/pause alerts with Resume and Cancel buttons for #3d-printer.

The listener posts the alert (Discord REST, bot token) and records a short alert id.
The print-feedback gateway plugin receives the button click and calls ``handle_click``.
Every action re-reads the printer first and refuses when the state no longer matches.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from print_handy.bot import rest

from . import _config

STATE = _config.state_file("PRINT_ALERT_STATE", "print_alert_buttons.json")
BASE = _config.moonraker_url()
PREFIX = "pbalert"
RETRY_PREFIX = "pbretry"
INSERTION_PREFIX = "pbinsert"
SPOOL_QUESTION_PREFIX = "pbspool"
NEW_SPOOL_PREFIX = "pbnewspool"
NEW_SPOOL_SELECT_PREFIX = "pbnewspoolselect"
PLATE_PREFIX = "pbplate"
CONFIRM_LABEL = "Confirm bed is clear & print"
TTL = 24 * 3600
_LOCK = threading.Lock()


def _base_for(printer_id: str | None) -> str:
    """The Moonraker base URL for a roster printer (#335); the configured one when printer_id is None."""
    if not printer_id:
        return BASE
    from print_handy import settings
    return str(settings.view_for(printer_id)["services"]["moonraker_url"])


def _call(path: str, method: str = "GET", timeout: float = 10, printer_id: str | None = None) -> dict[str, Any]:
    req = urllib.request.Request(_config.require(_base_for(printer_id), "services.moonraker_url") + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def shutdown_message(printer_id: str | None = None) -> str:
    """Printer firmware's own reason for the stop. Empty when it can't be read."""
    try:
        s = (_call("/printer/objects/query?webhooks=state_message", printer_id=printer_id).get("result") or {}).get("status") or {}
        return str((s.get("webhooks") or {}).get("state_message") or "")
    except Exception:
        return ""


def explain(state_message: str) -> str:
    """Plain English cause. Unknown text is passed through, never guessed at."""
    from .. import printers
    firmware = printers.current().firmware_name
    text = (state_message or "").strip()
    if not text:
        return f"{firmware} didn't say why. Check the printer screen."
    weight = re.search(r"Weight\s*(\d+(?:\.\d+)?)\s*g?\s*>\s*(?:max\s*)?(\d+(?:\.\d+)?)", text)
    if weight or re.search(r"nozzle hit|part detachment", text, re.I):
        z = re.search(r"\bZ\s*=\s*(-?\d+(?:\.\d+)?)", text)
        bits = []
        if weight:
            bits.append(f"the nozzle load cell read {float(weight.group(1)):g} g against a "
                        f"{float(weight.group(2)):g} g limit")
        if z:
            bits.append(f"at Z={float(z.group(1)):g} mm")
        detail = (" (" + ", ".join(bits) + ")") if bits else ""
        return ("Nozzle collision stop: the nozzle hit something" + detail + ". Usually a blob on the bed, "
                "a curled edge, or a part that came loose. Clear the bed and check the nozzle before anything else.")
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return f"{firmware} says: {first}"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict[str, Any]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, STATE)


def _timestamp(value: Any) -> float | None:
    """A stored row's timestamp as a float, or None when it cannot be read as one (#404)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fresh(row: Any, now: float) -> bool:
    """True only for a mapping row whose timestamp reads as a number inside the TTL."""
    if not isinstance(row, dict):
        return False
    at = _timestamp(row.get("at", 0))
    return at is not None and now - at < TTL


def record(kind: str, filename: str, now: float | None = None, **extra: Any) -> str:
    """Remember one alert; returns its short id. Extra fields support inventory-only UI flows."""
    now = time.time() if now is None else now
    aid = secrets.token_hex(4)
    with _LOCK:
        loaded = _load()
        data = {k: v for k, v in loaded.items() if _fresh(v, now)}
        if isinstance(loaded.get(_MUTE_STORE), dict):
            data[_MUTE_STORE] = loaded[_MUTE_STORE]  # the per-print mute store is not an alert row
        data[aid] = {"kind": kind, "filename": filename, "at": now, **extra}
        _save(data)
    return aid


def peek(aid: str) -> dict[str, Any] | None:
    """Read an alert without claiming it, so a bad choice can be refused and the prompt stays usable."""
    alert = _load().get(aid)
    if _fresh(alert, time.time()):
        return alert
    return None


def open_insertion_slots(now: float | None = None) -> set[int]:
    """IFS slots whose insertion prompt is still unanswered and inside the TTL."""
    now = time.time() if now is None else now
    return {int(row["slot"]) for row in _load().values()
            if _fresh(row, now) and row.get("kind") in ("insertion", "insertion-select")
            and str(row.get("slot")).isdigit()}


def take(aid: str) -> dict[str, Any] | None:
    """Claim an alert once. A second click on the same alert gets None."""
    with _LOCK:
        data = _load()
        alert = data.pop(aid, None)
        _save(data)
    if _fresh(alert, time.time()):
        return alert
    return None


# ---------------------------------------------------------------------------
# Per-print visual-watchdog mute (#476)
#
# "Stop asking for this print" silences every watchdog post (suspicious-
# verdict alerts and dark-camera notices) for ONE print. The record lives in
# this module's shared state file, under a reserved key the alert pruning in
# record() must carry over untouched, so the listener process that checks
# the mute and the gateway process that records a button press see the same
# store. The mute keys to the print's identity (the listener's notification
# identity / the Bambu monitor's job key), never to the filename alone: the
# next print asks again even when it has the same filename. When a caller
# has no identity, the fallback key is filename + day, so an old same-named
# job can mute at most the rest of that day, never a later reprint.

_MUTE_STORE = "visual_muted"
_MUTE_KEEP_S = 7 * 24 * 3600


def _mute_key(filename: str, identity: Any = None) -> str:
    identity = str(identity or "").strip()
    if identity:
        return f"id:{identity}|{filename or ''}"
    return f"file:{filename or ''}|day:{time.strftime('%Y-%m-%d')}"


def mute_visual(filename: str, identity: Any = None, now: float | None = None) -> str:
    """Record the per-print watchdog mute; returns the store key used."""
    now = time.time() if now is None else now
    key = _mute_key(filename, identity)
    with _LOCK:
        data = _load()
        store = data.get(_MUTE_STORE)
        if not isinstance(store, dict):
            store = {}
        store = {k: v for k, v in store.items()
                 if isinstance(v, dict) and now - (_timestamp(v.get("at")) or 0) < _MUTE_KEEP_S}
        store[key] = {"filename": str(filename or ""), "identity": str(identity or ""), "at": now}
        data[_MUTE_STORE] = store
        _save(data)
    return key


def visual_muted(filename: str, identity: Any = None) -> bool:
    """True when the operator muted watchdog posts for this print (#476)."""
    store = _load().get(_MUTE_STORE)
    if not isinstance(store, dict):
        return False
    return _mute_key(filename, identity) in store


MUTE_LABEL = "Stop asking for this print"


def _mute_button(aid: str) -> dict[str, Any]:
    """The per-print watchdog mute (#476): silences camera posts for one print only."""
    return {"type": 2, "style": 2, "label": MUTE_LABEL, "custom_id": f"{PREFIX}:mute:{aid}"}


def components(aid: str, kind: str) -> list[dict[str, Any]]:
    if kind == "visual":
        return [{"type": 1, "components": [
            {"type": 2, "style": 2, "label": "Pause", "custom_id": f"{PREFIX}:pause:{aid}"},
            {"type": 2, "style": 3, "label": "It's fine", "custom_id": f"{PREFIX}:acknowledge:{aid}"},
            _mute_button(aid),
        ]}]
    if kind == "visual-dark":
        # The dark-camera notice (#476) carries no print-action buttons at
        # all — only the mute, so a dead camera cannot nag for a whole print.
        return [{"type": 1, "components": [_mute_button(aid)]}]
    buttons = []
    if kind == "paused":
        buttons.append({"type": 2, "style": 3, "label": "Resume print", "custom_id": f"{PREFIX}:resume:{aid}"})
    buttons.append({"type": 2, "style": 4, "label": "Cancel print", "custom_id": f"{PREFIX}:cancel:{aid}"})
    return [{"type": 1, "components": buttons}]


def insertion_components(aid: str) -> list[dict[str, Any]]:
    """Three non-actuating choices for one idle IFS insertion prompt."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 2, "label": "Same spool", "custom_id": f"{INSERTION_PREFIX}:same:{aid}"},
        {"type": 2, "style": 1, "label": "Existing spool", "custom_id": f"{INSERTION_PREFIX}:existing:{aid}"},
        {"type": 2, "style": 1, "label": "New spool", "custom_id": f"{INSERTION_PREFIX}:new:{aid}"},
    ]}]


def spool_question_components(aid: str, prior_spool_id: int | None = None) -> list[dict[str, Any]]:
    """Answers for a paused runout or swap question.

    Same/Existing/New are inventory-only. Cancel print is the one actuating
    choice: it ends the paused print through print_stop.cancel (the same
    path as the paused alert's Cancel button) and closes the question.
    """
    buttons = []
    if prior_spool_id is not None:
        buttons.append({"type": 2, "style": 2, "label": "Same spool",
                        "custom_id": f"{SPOOL_QUESTION_PREFIX}:same:{aid}"})
    buttons.extend([
        {"type": 2, "style": 1, "label": "Existing spool",
         "custom_id": f"{SPOOL_QUESTION_PREFIX}:existing:{aid}"},
        {"type": 2, "style": 1, "label": "New spool",
         "custom_id": f"{SPOOL_QUESTION_PREFIX}:new:{aid}"},
        {"type": 2, "style": 4, "label": "Cancel print",
         "custom_id": f"{SPOOL_QUESTION_PREFIX}:cancel:{aid}"},
    ])
    return [{"type": 1, "components": buttons}]


def plate_picker_components(aid: str, options: list[dict[str, str]]) -> list[dict[str, Any]]:
    """One select menu listing the remaining plates; choosing one only prepares a Confirm button."""
    return [{"type": 1, "components": [
        {"type": 3, "custom_id": f"{PLATE_PREFIX}:pick:{aid}", "placeholder": "Choose the next plate",
         "min_values": 1, "max_values": 1,
         "options": [{"label": o["label"][:100], "value": o["value"][:100]} for o in options[:25]]}]}]


def parse_plate_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return ('pick' | 'go' | 'spool' | 'drop', alert id) for plate controls, or None."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == PLATE_PREFIX and parts[1] in ("pick", "go", "spool", "drop") and parts[2]:
        return parts[1], parts[2]
    return None


def post_plate_picker(token: str, channel_id: str, content: str, plan_id: str,
                      options: list[dict[str, str]]) -> bool:
    """Post the remaining-plate picker. False means the caller should fall back to a plain-text line."""
    if not token or not channel_id or not plan_id or not options:
        return False
    aid = record("plate-picker", "", plan_id=plan_id)
    body = json.dumps({"content": content[:1990], "components": plate_picker_components(aid, options),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def post_plate_confirm(token: str, channel_id: str, content: str, plan_id: str, part_id: str) -> bool:
    """Post the empty-bed offer with the existing next-plate confirm button.

    The go button is the same control the plate picker uses, so confirm runs
    print_next.start. Drop leaves the queue alone. This function only posts.
    """
    if not token or not channel_id or not plan_id or not part_id:
        return False
    from . import print_next
    from .next_plate_offer import confirm_components
    aid = record("plate-confirm", "", plan_id=plan_id, part_id=str(part_id))
    body = json.dumps({
        "content": content[:1990],
        "components": confirm_components(aid, confirm_label=print_next.START_LABEL,
                                         cancel_label=print_next.CANCEL_LABEL),
        "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def post_spool_question(token: str, channel_id: str, content: str, context: dict[str, Any],
                        message_id: str | None = None) -> str | None:
    """Post or edit one paused-spool question. Returns its Discord message id."""
    if not token or not channel_id:
        return None
    aid = record("spool-question", "", context=dict(context or {}), message_id=message_id or "")
    body = json.dumps({"content": content[:1990],
                       "components": spool_question_components(aid, context.get("prior_spool_id")),
                       "allowed_mentions": {"parse": []}}).encode()
    url = f"{rest.API}/channels/{channel_id}/messages"
    method = "POST"
    if message_id:
        url += f"/{message_id}"
        method = "PATCH"
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bot {token}",
                                          "Content-Type": "application/json",
                                          "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read() or b"{}")
            return str(data.get("id") or message_id or "") or None
    except Exception:
        take(aid)
        return None


def edit_message(token: str, channel_id: str, message_id: str, content: str,
                 components: list | None = None) -> bool:
    """Replace the text of one bot message in place. False on any failure.

    ``components`` is omitted from the payload unless given: an explicit
    list replaces the message's buttons ([] strips them), while the
    default leaves them untouched, as every pre-existing caller relies on.
    """
    if not (token and channel_id and message_id):
        return False
    payload = {"content": content[:1990], "allowed_mentions": {"parse": []}}
    if components is not None:
        payload["components"] = components
    req = urllib.request.Request(
        f"{rest.API}/channels/{channel_id}/messages/{message_id}",
        data=json.dumps(payload).encode(),
        method="PATCH", headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                 "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def post_insertion_confirmation(token: str, channel_id: str, content: str, slot: int) -> bool:
    """Post an answer-only IFS insertion UI. It has no printer or inventory side effect."""
    if not token or not channel_id or slot not in (1, 2, 3, 4):
        return False
    aid = record("insertion", "", slot=slot)
    body = json.dumps({"content": content[:1990], "components": insertion_components(aid),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def new_spool_components(aid: str) -> list[dict[str, Any]]:
    """Load / shelf / dismiss for one spool created outside Print Handy (#483).

    Every choice is inventory-only at this step: Load opens the slot choice,
    the other two simply close the prompt (the watcher already recorded the
    spool as known when it prompted, so no answer is ever asked twice).
    """
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Load into a slot", "custom_id": f"{NEW_SPOOL_PREFIX}:load:{aid}"},
        {"type": 2, "style": 2, "label": "Leave on the shelf", "custom_id": f"{NEW_SPOOL_PREFIX}:shelf:{aid}"},
        {"type": 2, "style": 2, "label": "Dismiss", "custom_id": f"{NEW_SPOOL_PREFIX}:dismiss:{aid}"},
    ]}]


def post_new_spool(token: str, channel_id: str, content: str, spool_id: int, label: str) -> bool:
    """Post the one-time new-spool prompt (#483). False means the caller should fall back to plain text."""
    if not token or not channel_id or not spool_id:
        return False
    aid = record("spoolman-new", "", spool_id=int(spool_id), label=str(label))
    body = json.dumps({"content": content[:1990], "components": new_spool_components(aid),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def new_spool_slot_options(state: dict[str, Any] | None) -> list[dict[str, str]]:
    """Select options for the new-spool load step: every slot, with what it holds now."""
    slots = (state or {}).get("filament_slots") or {}
    options = []
    for slot in (1, 2, 3, 4):
        rec = slots.get(str(slot)) or {}
        sid = rec.get("spoolman_id")
        detail = f"#{sid} {rec.get('name') or ''}".strip() if sid else "empty"
        options.append({"label": f"Slot {slot}", "value": str(slot),
                        "description": f"Now holds {detail}"[:100]})
    return options


def insertion_select_options(spools: list[dict[str, Any]], mapped: dict[str, Any] | None = None,
                             excluded_ids: set[int] | None = None) -> list[dict[str, str]]:
    """Discord select options, closest material/color to the old slot record first."""
    mapped = mapped or {}
    excluded_ids = excluded_ids or set()
    material = str(mapped.get("material") or "").casefold()
    color = str(mapped.get("color") or "").casefold()

    def score(spool):
        filament = spool.get("filament") or {}
        candidate_material = str(filament.get("material") or "").casefold()
        candidate_color = str(filament.get("name") or "").casefold()
        return (candidate_material != material, candidate_color != color, int(spool.get("id") or 0))

    options = []
    for spool in sorted((s for s in spools if isinstance(s, dict) and not s.get("archived")
                         and int(s.get("id") or 0) not in excluded_ids), key=score)[:25]:
        filament = spool.get("filament") or {}
        grams = spool.get("remaining_weight")
        from .discord_print_summary import filament_label
        detail = " ".join(part for part in (filament_label(filament.get("name"), filament.get("material")),
                                               f"{grams:g} g" if isinstance(grams, (int, float)) else "") if part)
        options.append({"label": f"#{spool.get('id')} {detail}"[:100], "value": str(spool.get("id")),
                        "description": "Select this shelf spool"})
    return options


# ---------------------------------------------------------------------------
# Conversational new-spool intake walkthrough (#512)
#
# "New spool" on an insertion prompt (or a paused spool question) used to
# open one five-field text modal. The walkthrough replaces it with one
# button/select step at a time — material, brand (only when the operator
# turned spool.prompt_brand on), colour/finish, starting weight, the
# replaced spool's disposition, and one confirmation — so it works on
# mobile, where modals are at their worst. The state for one walkthrough
# rides on the alert record (kind "intake"); this layer stays data-only so
# the whole step machine is covered without a gateway. Single free-text
# values (an "Other" material/colour, a custom weight, a typed brand) are
# the only pieces the click layer still collects with a one-field modal.
# When any extraction route is available (the label's barcode/QR code,
# a vision endpoint, or local OCR), the first step also offers a photo of
# the spool label (#514): the click layer waits for the photo message,
# spool_label extracts a spec, and the values land here as a pre-filled
# state that still ends at the same confirmation — nothing is created
# from a photo without it.

INTAKE_PREFIX = "pbintake"
INTAKE_SELECT_PREFIX = "pbintakeselect"
INTAKE_MATERIALS = ("PLA", "Silk PLA", "PLA+", "PETG", "TPU", "ABS")
INTAKE_WEIGHTS = (1000, 500, 250)
INTAKE_COMMON_COLORS = ("Black", "White", "Matte White", "Grey", "Red", "Blue", "Green",
                        "Orange", "Yellow", "Pink", "Purple", "Natural")
INTAKE_OTHER = "__other__"
INTAKE_SKIP = "__skip__"

_MATERIAL_ACTIONS = {f"mat_{i}": label for i, label in enumerate(INTAKE_MATERIALS)}
_WEIGHT_ACTIONS = {f"w_{grams}": grams for grams in INTAKE_WEIGHTS}
_DISPOSITION_ACTIONS = {"disp_shelf": "shelf", "disp_archive": "archive",
                        "disp_depleted": "depleted", "disp_delete": "delete"}
INTAKE_ACTIONS = frozenset(
    [*_MATERIAL_ACTIONS, "mat_other", *_WEIGHT_ACTIONS, "w_custom",
     *_DISPOSITION_ACTIONS, "brand_type", "brand_skip", "photo", "edit",
     "confirm", "cancel"])


def parse_intake_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return the walkthrough action and alert id, or None for unrelated controls."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == INTAKE_PREFIX and parts[1] in INTAKE_ACTIONS and parts[2]:
        return parts[1], parts[2]
    return None


def intake_material_for_action(action: str) -> str | None:
    return _MATERIAL_ACTIONS.get(action)


def intake_weight_for_action(action: str) -> int | None:
    return _WEIGHT_ACTIONS.get(action)


def intake_disposition_for_action(action: str) -> str | None:
    return _DISPOSITION_ACTIONS.get(action)


def intake_preview_text(predicted_id: Any, slot: Any) -> str:
    """The early spool-number preview (#512 §2): said at the very first step,
    so the operator can write the number on the physical spool right away."""
    if predicted_id is None:
        return f"Preparing a new spool for Slot {slot}. Nothing is created until you confirm at the end."
    return (f"Preparing new **Spool #{predicted_id}** for Slot {slot} — you can write "
            f"#{predicted_id} on the spool now. Nothing is created until you confirm at the end.")


def intake_color(state: dict[str, Any]) -> tuple[str | None, str | None]:
    """The color code the walkthrough will record: (hex, source).

    A picked swatch (or label read) wins; otherwise the color name is read
    against the named-color table. (None, None) means nothing could be
    worked out and the swatch step must ask.
    """
    from ..core import filament_color
    picked = filament_color.normalize_hex(state.get("hex"))
    if picked:
        return picked, "picked"
    derived = filament_color.derive_hex(state.get("color"), state.get("material"))
    return (derived, "name") if derived else (None, None)


def _needs_swatch(state: dict[str, Any]) -> bool:
    return bool(str(state.get("color") or "").strip()) and intake_color(state)[0] is None


def intake_next_step(state: dict[str, Any]) -> str:
    """The step after the next one, unless the color code is still unknown.

    When a color name cannot be read (a typed "Galaxy"), the walkthrough
    asks once with swatch buttons — never for a hex — before going on, so a
    new spool always records a color code.
    """
    following = _intake_following_step(state)
    if following in ("weight", "disposition", "confirm") and _needs_swatch(state):
        return "swatch"
    return following


def _intake_following_step(state: dict[str, Any]) -> str:
    """The step after the one ``state['step']`` names as just completed.

    Brand is asked only when the walkthrough was started with
    prompt_brand on (#512 intake refinement); the disposition step exists
    only for an insertion into an occupied slot (a paused spool question
    keeps its own runout/shelf semantics in the answer path).
    """
    step = state.get("step")
    if step == "start":
        return "material"
    if step == "material":
        return "brand" if state.get("prompt_brand") else "color"
    if step == "brand":
        return "color"
    if step == "color":
        return "weight"
    if step == "swatch":
        if state.get("grams") is None:
            return "weight"
        if state.get("origin") == "insertion" and state.get("old_spool_id"):
            return "disposition" if not state.get("disposition") else "confirm"
        return "confirm"
    if step == "weight":
        if state.get("origin") == "insertion" and state.get("old_spool_id"):
            return "disposition"
        return "confirm"
    if step == "disposition":
        return "confirm"
    return "confirm"


def _intake_spec_line(state: dict[str, Any]) -> str:
    head = " ".join(x for x in (str(state.get("brand") or "").strip(),
                                str(state.get("material") or "").strip()) if x)
    color = str(state.get("color") or "").strip()
    hx, _ = intake_color(state)
    if color and hx:
        color = f"{color} (#{hx})"
    return ", ".join(x for x in (head, color) if x)


def intake_step_text(state: dict[str, Any]) -> str:
    """The message for the walkthrough step ``state['step']`` is about to ask."""
    step = state.get("step")
    slot = state.get("slot")
    if step == "material":
        return (intake_preview_text(state.get("predicted_id"), slot)
                + "\n\n**Material** — pick one, or Other to type a different material.")
    if step == "brand":
        return "**Brand** — pick a known brand, type one, or skip. The spool works the same either way."
    if step == "color":
        return f"**Colour / finish** — {state.get('material')}: pick one, or Other to type it exactly as labelled."
    if step == "swatch":
        return (f"**Color code** — I could not tell the color of “{state.get('color')}” from its name. "
                "Pick the closest color; it sets the preview color and the printer screen swatch, "
                "not how the spool prints.")
    if step == "weight":
        return "**Starting weight** — how many grams is a full spool? Pick one, or Custom to type it."
    if step == "disposition":
        old = state.get("old_spool_id")
        grams = state.get("old_remaining")
        left = f" ({grams:g} g left)" if isinstance(grams, (int, float)) else ""
        recommend = ("\nIt has nothing left, so **Archive as depleted** is the recommended choice."
                     if isinstance(grams, (int, float)) and grams <= 0 else "")
        return (f"Slot {slot} still holds Spool #{old}{left}. What should happen to it "
                f"when the new spool goes in?{recommend}")
    if step == "confirm":
        predicted = state.get("predicted_id")
        number = f"Spool #{predicted}" if predicted is not None else "A new spool"
        fate = {"shelf": f" Spool #{state.get('old_spool_id')} goes back on the shelf.",
                "archive": f" Spool #{state.get('old_spool_id')} will be archived.",
                "depleted": f" Spool #{state.get('old_spool_id')} will be archived as depleted (0 g left).",
                "delete": f" Spool #{state.get('old_spool_id')} will be DELETED from Spoolman."}.get(
                    state.get("disposition") or "", "")
        text = (f"Create {number} — {_intake_spec_line(state)}, {state.get('grams'):g} g — "
                f"and load it into Slot {slot}?{fate}")
        if state.get("from_photo"):
            source = {"code": "the label's barcode/QR code",
                      "vision": "the label photo, read by the vision endpoint",
                      "ocr": "the label photo, read by local OCR"}.get(
                          state.get("photo_route"), "the label photo")
            text += (f"\nRead from {source} — check every value before you confirm; "
                     "Edit details walks the normal steps instead.")
        temps = []
        if state.get("nozzle_temp") is not None:
            temps.append(f"nozzle {state['nozzle_temp']:g}°C")
        if state.get("bed_temp") is not None:
            temps.append(f"bed {state['bed_temp']:g}°C")
        if temps:
            text += (f"\nLabel print temps ({', '.join(temps)}) will be saved on the filament, "
                     f"so prints use them.")
        return text
    return "New spool"


def intake_material_components(aid: str, photo: bool = False) -> list[dict[str, Any]]:
    buttons = [{"type": 2, "style": 1, "label": label, "custom_id": f"{INTAKE_PREFIX}:mat_{i}:{aid}"}
               for i, label in enumerate(INTAKE_MATERIALS)]
    last = [{"type": 2, "style": 2, "label": "Other material…", "custom_id": f"{INTAKE_PREFIX}:mat_other:{aid}"}]
    if photo:
        # The photo alternative (#514) rides the first step, and only when
        # the caller knows a vision endpoint is available — with no endpoint
        # the button is never rendered and the walkthrough is unchanged.
        last.append({"type": 2, "style": 1, "label": "Use a photo of the label",
                     "custom_id": f"{INTAKE_PREFIX}:photo:{aid}"})
    last.append({"type": 2, "style": 2, "label": "Cancel", "custom_id": f"{INTAKE_PREFIX}:cancel:{aid}"})
    return [{"type": 1, "components": buttons[:3]},
            {"type": 1, "components": buttons[3:]},
            {"type": 1, "components": last}]


def intake_weight_components(aid: str) -> list[dict[str, Any]]:
    return [{"type": 1, "components": [
        *[{"type": 2, "style": 1, "label": f"{grams} g", "custom_id": f"{INTAKE_PREFIX}:w_{grams}:{aid}"}
          for grams in INTAKE_WEIGHTS],
        {"type": 2, "style": 2, "label": "Custom…", "custom_id": f"{INTAKE_PREFIX}:w_custom:{aid}"},
        {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"{INTAKE_PREFIX}:cancel:{aid}"}]}]


def intake_disposition_components(aid: str, old_remaining: Any = None) -> list[dict[str, Any]]:
    """The replaced spool's fate (#512 §3). At 0 g the depleted choice leads
    and says so; Delete is offered but never first and never the default."""
    depleted = {"type": 2, "style": 1, "label": "Archive as depleted",
                "custom_id": f"{INTAKE_PREFIX}:disp_depleted:{aid}"}
    if isinstance(old_remaining, (int, float)) and old_remaining <= 0:
        depleted = {**depleted, "label": "Archive as depleted (recommended)"}
        order = [depleted,
                 {"type": 2, "style": 2, "label": "Move to shelf", "custom_id": f"{INTAKE_PREFIX}:disp_shelf:{aid}"},
                 {"type": 2, "style": 2, "label": "Archive", "custom_id": f"{INTAKE_PREFIX}:disp_archive:{aid}"}]
    else:
        order = [{"type": 2, "style": 1, "label": "Move to shelf", "custom_id": f"{INTAKE_PREFIX}:disp_shelf:{aid}"},
                 {"type": 2, "style": 2, "label": "Archive", "custom_id": f"{INTAKE_PREFIX}:disp_archive:{aid}"},
                 depleted]
    order.append({"type": 2, "style": 4, "label": "Delete", "custom_id": f"{INTAKE_PREFIX}:disp_delete:{aid}"})
    order.append({"type": 2, "style": 2, "label": "Cancel", "custom_id": f"{INTAKE_PREFIX}:cancel:{aid}"})
    return [{"type": 1, "components": order}]


def intake_confirm_components(aid: str, editable: bool = False) -> list[dict[str, Any]]:
    buttons = [{"type": 2, "style": 3, "label": "Create & load", "custom_id": f"{INTAKE_PREFIX}:confirm:{aid}"}]
    if editable:
        # A photo pre-fill (#514) can be walked back through the normal
        # steps instead of confirmed as read.
        buttons.append({"type": 2, "style": 2, "label": "Edit details",
                        "custom_id": f"{INTAKE_PREFIX}:edit:{aid}"})
    buttons.append({"type": 2, "style": 2, "label": "Cancel", "custom_id": f"{INTAKE_PREFIX}:cancel:{aid}"})
    return [{"type": 1, "components": buttons}]


def intake_brand_components(aid: str) -> list[dict[str, Any]]:
    """Brand step when no brand is known yet: type one, or skip it."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Type the brand…", "custom_id": f"{INTAKE_PREFIX}:brand_type:{aid}"},
        {"type": 2, "style": 2, "label": "Skip brand", "custom_id": f"{INTAKE_PREFIX}:brand_skip:{aid}"},
        {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"{INTAKE_PREFIX}:cancel:{aid}"}]}]


def _select_components(aid: str, placeholder: str, options: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{"type": 1, "components": [
        {"type": 3, "custom_id": f"{INTAKE_SELECT_PREFIX}:{aid}", "placeholder": placeholder,
         "min_values": 1, "max_values": 1,
         "options": [{"label": o["label"][:100], "value": o["value"][:100]} for o in options[:25]]}]}]


def intake_brand_options(types: list[dict[str, Any]] | None, material: Any) -> list[dict[str, str]]:
    """Known brands for this material first, then every known brand, then skip/type."""
    material = str(material or "").casefold()
    first, rest = [], []
    for entry in types or []:
        brand = str(entry.get("brand") or "").strip()
        if not brand:
            continue
        bucket = first if str(entry.get("material") or "").casefold() == material else rest
        if brand not in bucket:
            bucket.append(brand)
    options = [{"label": b, "value": b} for b in (first + rest)[:22]]
    options.append({"label": "Type a brand…", "value": INTAKE_OTHER})
    options.append({"label": "No brand / skip", "value": INTAKE_SKIP})
    return options


def intake_color_options(types: list[dict[str, Any]] | None, material: Any) -> list[dict[str, str]]:
    """Colours already known for this material first, then common colours, then Other."""
    material = str(material or "").casefold()
    known = []
    for entry in types or []:
        if str(entry.get("material") or "").casefold() != material:
            continue
        color = str(entry.get("color") or "").strip()
        if color and color not in known:
            known.append(color)
    options = [{"label": c, "value": c} for c in known[:15]]
    for color in INTAKE_COMMON_COLORS:
        if len(options) >= 23:
            break
        if color not in known:
            options.append({"label": color, "value": color})
    options.append({"label": "Other (type it exactly)…", "value": INTAKE_OTHER})
    return options


def intake_step_components(aid: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """The controls for the walkthrough step ``state['step']`` is about to ask."""
    step = state.get("step")
    if step == "material":
        return intake_material_components(aid, photo=bool(state.get("photo_offered")))
    if step == "brand":
        options = intake_brand_options(state.get("types"), state.get("material"))
        if len(options) > 2:  # at least one real brand is known
            return _select_components(aid, "Choose the brand", options)
        return intake_brand_components(aid)
    if step == "color":
        return _select_components(aid, "Choose the colour / finish",
                                  intake_color_options(state.get("types"), state.get("material")))
    if step == "swatch":
        from ..core.filament_color import SWATCHES
        return _select_components(aid, "Choose the closest color",
                                  [{"label": f"{label} (#{hx})", "value": hx} for label, hx in SWATCHES])
    if step == "weight":
        return intake_weight_components(aid)
    if step == "disposition":
        return intake_disposition_components(aid, state.get("old_remaining"))
    return intake_confirm_components(aid, editable=bool(state.get("from_photo")))


def bind_insertion_existing(slot: int, spool_id: int, spool_sync) -> str:
    """Reuse the one slot-binding path; insertion UI never issues a printer command."""
    result = spool_sync.bind_slot(slot, spool_id, note="Bound from Discord IFS insertion", push_screen_update=False)
    record = result["record"]
    grams = f", {record['remaining_grams']:g} g left" if record.get("remaining_grams") is not None else ""
    return f"Slot {slot} → spool #{spool_id} ({record['name']}{grams}). Inventory updated; printer untouched."


def create_and_bind_insertion(slot: int, values: dict[str, Any], spool_sync,
                              prior_disposition: str = "shelf",
                              expected_prior_spool_id: int | None = None) -> str:
    """Create the operator-described spool and bind it in one inventory-only step.

    ``prior_disposition`` is the replaced spool's fate the operator picked in
    the walkthrough (#512 §3); it defaults to the shelf, which is what the
    pre-walkthrough flow silently did. The result names the fate so a silent
    shelf move can never happen again. ``expected_prior_spool_id`` is the
    replaced spool the walkthrough's confirmation named; bind_slot re-checks
    it before writing, so the disposition can only land on that spool (#515).
    """
    spec = spool_sync.parse_new(", ".join(str(values.get(key) or "").strip() for key in ("brand", "material", "color", "grams")))
    spec["lot"] = str(values.get("lot") or "").strip() or None
    # The color code is always recorded: a picked swatch wins,
    # else create_spool derives it from the color name.
    if values.get("hex"):
        spec["hex"] = values["hex"]
    # Label temperatures read from a spool photo (#514) ride along so
    # create_spool records them on a new filament; absent values stay absent.
    for key in ("nozzle_temp", "bed_temp"):
        if values.get(key) is not None:
            spec[key] = values[key]
    made = spool_sync.create_spool(spec)
    result = spool_sync.bind_slot(slot, int(made["spool_id"]), note="Created and bound from Discord IFS insertion",
                                  push_screen_update=False, prior_disposition=prior_disposition,
                                  expected_prior_spool_id=expected_prior_spool_id)
    record = result["record"]
    prior = result.get("prior_spool_id")
    moved = ""
    if prior and int(prior) != int(made["spool_id"]):
        moved = {"archive": f" Spool #{prior} was archived.",
                 "depleted": f" Spool #{prior} was archived as depleted (0 g left).",
                 "delete": f" Spool #{prior} was deleted from Spoolman.",
                 }.get(prior_disposition, f" Spool #{prior} went back on the shelf.")
    return (f"Created spool #{made['spool_id']}; slot {slot} → #{made['spool_id']} "
            f"({record['name']}).{moved} Inventory updated; printer untouched.")


def post_with_buttons(token: str, channel_id: str, content: str, kind: str, filename: str,
                      connection: str | None = None, printer_id: str | None = None) -> bool:
    """Post the alert with buttons. False means the caller should fall back to plain text.

    A family pause alert (#460) names its connection (and roster printer
    id) so handle_click routes the press to that family's own control;
    Moonraker alerts pass neither and their records are unchanged.
    """
    if not token or not channel_id:
        return False
    extra: dict[str, Any] = {}
    if connection:
        extra["connection"] = connection
    if printer_id:
        extra["printer_id"] = printer_id
    aid = record(kind, filename, **extra)
    body = json.dumps({"content": content[:1990], "components": components(aid, kind),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                with contextlib.suppress(Exception):
                    mid = json.loads(resp.read() or b"{}").get("id")
                    if mid:
                        with _LOCK:
                            data = _load()
                            if aid in data:
                                data[aid].update(message_id=str(mid), channel_id=str(channel_id))
                                _save(data)
            return ok
    except Exception:
        take(aid)
        return False


def retire_alerts(token: str, filename: str | None, note: str, kinds: tuple[str, ...] = ("shutdown", "error", "pause", "paused"),
                  *, headline: str = "Printer firmware stopped.", printer_id: str | None = None) -> int:
    """Strip the buttons from stale alerts for this job and say why, so a dead Cancel button is not left behind.

    ``printer_id`` scopes the retirement to one printer's alerts
    (P2-176): two printers can pause the same filename in one channel,
    and one printer's print ending must not strip the other's live
    Resume/Cancel buttons. Unscoped (None) retires every match, as
    before — the Moonraker listener retires its own printer's alerts
    that way.
    """
    retired = 0
    with _LOCK:
        data = _load()
        stale = {aid: a for aid, a in data.items() if isinstance(a, dict) and a.get("kind") in kinds
                 and (filename is None or a.get("filename") == filename)
                 and (printer_id is None or a.get("printer_id") == printer_id)}
        for aid in stale:
            data.pop(aid, None)
        _save(data)
    for alert in stale.values():
        mid, chan = alert.get("message_id"), alert.get("channel_id")
        if not (token and mid and chan):
            continue
        req = urllib.request.Request(
            f"{rest.API}/channels/{chan}/messages/{mid}",
            data=json.dumps({"components": [], "content": f"~~{headline}~~ {note}"[:1990],
                             "allowed_mentions": {"parse": []}}).encode(),
            method="PATCH", headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                                     "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
        with contextlib.suppress(Exception):
            with urllib.request.urlopen(req, timeout=15) as resp:
                retired += 200 <= resp.status < 300
    return retired


def post_visual_alert(token: str, channel_id: str, content: str, filename: str, image: Path,
                      identity: Any = None) -> bool:
    """Post one camera image and its safe visual-review buttons in a single Discord message.

    ``identity`` is the caller's per-print identity (#476): it rides on the
    alert record so a "Stop asking for this print" press mutes that print,
    not just that filename.
    """
    if not token or not channel_id or not image.is_file():
        return False
    aid = record("visual", filename, **({"identity": str(identity)} if identity else {}))
    boundary = f"----hermes-print-{secrets.token_hex(12)}"
    payload = json.dumps({"content": content[:1990], "components": components(aid, "visual"),
                          "allowed_mentions": {"parse": []}}).encode()
    try:
        frame = image.read_bytes()
        if not frame:
            raise ValueError("empty camera frame")
        body = b"".join((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            "Content-Type: application/json\r\n\r\n".encode() + payload + b"\r\n",
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"{image.name}\"\r\n"
            "Content-Type: image/jpeg\r\n\r\n".encode() + frame + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ))
        req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                     method="POST", headers={"Authorization": f"Bot {token}",
                                                              "Content-Type": f"multipart/form-data; boundary={boundary}",
                                                              "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False


def post_visual_dark_notice(token: str, channel_id: str, content: str, filename: str,
                            identity: Any = None) -> bool:
    """Post the once-per-print dark-camera notice (#476): text plus the mute button only.

    No image (the frame is black; it would only look like a failed upload)
    and no print-action buttons — a dark feed is a camera problem, never a
    print alert. False means the caller should fall back to a plain post.
    """
    if not token or not channel_id:
        return False
    aid = record("visual-dark", filename, **({"identity": str(identity)} if identity else {}))
    body = json.dumps({"content": content[:1990], "components": components(aid, "visual-dark"),
                       "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(f"{rest.API}/channels/{channel_id}/messages", data=body,
                                 method="POST", headers={"Authorization": f"Bot {token}",
                                                         "Content-Type": "application/json",
                                                         "User-Agent": "DiscordBot (hermes-3dprinting, 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        take(aid)
        return False

def parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == PREFIX and parts[1] in ("resume", "cancel", "pause", "acknowledge", "mute"):
        return parts[1], parts[2]
    return None


def retry_custom_id(aid: str) -> str:
    """Return the button ID for one intake receipt."""
    return f"{RETRY_PREFIX}:{aid}"


def retry_components(aid: str) -> list[dict[str, Any]]:
    """Single safe retry action after a confirmed pre-upload refusal."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Retry", "custom_id": retry_custom_id(aid)},
    ]}]


def parse_retry_custom_id(custom_id: str) -> str | None:
    """Return an intake receipt id only for a well-formed single-use Retry button."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 2 and parts[0] == RETRY_PREFIX and re.fullmatch(r"[a-f0-9]{32}", parts[1]):
        return parts[1]
    return None


def parse_insertion_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return the inventory-only insertion choice and alert id, or None for unrelated controls."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == INSERTION_PREFIX and parts[1] in ("same", "existing", "new") and parts[2]:
        return parts[1], parts[2]
    return None


def parse_spool_question_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return a paused-spool choice and alert id, or None for unrelated controls."""
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == SPOOL_QUESTION_PREFIX and parts[1] in ("same", "existing", "new", "cancel") and parts[2]:
        return parts[1], parts[2]
    return None


def parse_new_spool_custom_id(custom_id: str) -> tuple[str, str] | None:
    """Return a new-spool choice and alert id, or None for unrelated controls (#483).

    One prefix serves the whole prompt chain; the alert record's kind says
    which step an action belongs to (the prompt itself: load/shelf/dismiss;
    the disposition step: shelf/archive/depleted/cancel; the confirmation
    step: confirm/cancel).
    """
    parts = str(custom_id or "").split(":")
    if len(parts) == 3 and parts[0] == NEW_SPOOL_PREFIX and parts[1] in (
            "load", "shelf", "dismiss", "archive", "depleted", "delete", "confirm", "cancel") and parts[2]:
        return parts[1], parts[2]
    return None


def clear_shutdown(wait_s: float = 60, poll_s: float = 2.0, *, caller=None, reader=None,
                   printer_id: str | None = None) -> tuple[bool, str]:
    """FIRMWARE_RESTART once after an emergency stop, then wait for Klipper ready. No motion, no heat."""
    from . import print_control as pc
    firmware = pc.firmware_name(printer_id)
    if caller is not None:
        call = caller
    elif printer_id:
        call = lambda path, method="GET", timeout=10: _call(path, method, timeout, printer_id=printer_id)
    else:
        call = _call
    read = reader or (lambda: pc.status(printer_id))
    try:
        call("/printer/firmware_restart", method="POST", timeout=15)
    except Exception:
        pass  # the restart drops the connection; readback decides
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            st = read()
        except Exception:
            continue
        if st.get("klippy") == "ready":
            return True, f"Cleared the emergency stop. {firmware} is ready, heaters off."
    return False, f"Sent a firmware restart, but {firmware} wasn't ready after {wait_s:.0f}s. Check the printer screen. Not retrying."


def handle_click(action: str, alert: dict[str, Any] | None) -> str:
    """Run one button action. Always re-reads state first; never retries."""
    from . import print_control as pc
    from . import print_stop
    if not alert:
        return "That alert is old or was already answered. Nothing sent."
    filename = alert.get("filename") or ""
    if alert.get("kind") in ("visual", "visual-dark"):
        if action == "mute":
            # #476: mute this print's watchdog posts. Records only — the
            # printer is never read or touched for a mute.
            mute_visual(filename, alert.get("identity"))
            return ("Camera checks muted for the rest of this print. You'll get no more "
                    "camera alerts for it; the print itself is untouched.")
        if alert.get("kind") == "visual-dark":
            return "That camera notice action is not recognized. Nothing sent."
        if action == "acknowledge":
            return "Visual check acknowledged. Monitoring continues; nothing sent to the printer."
        if action == "pause":
            if print_stop._lan_adapter() is not None:
                # Bambu camera watch (#294): the same button, the Bambu pause —
                # print_stop.pause re-reads the LAN state and checks the file itself.
                return print_stop.pause(filename)[1]
            st = pc.status()
            if st.get("klippy") != "ready":
                return f"The printer's controller is {st.get('klippy')}. Nothing sent."
            if st.get("state") != "printing":
                return f"Nothing to pause: printer is {st.get('state')}. Nothing sent."
            if filename and st.get("filename") and st.get("filename") != filename:
                return "A different print is loaded now than the one in this alert. Nothing sent."
            pc._call("/printer/print/pause", method="POST", timeout=15)
            return "Pause sent. Check the printer screen for the current state."
        return "That visual alert action is not recognized. Nothing sent."
    connection = alert.get("connection")
    if connection:
        # A non-Moonraker family's pause alert (#460): the press goes to
        # that family's own adapter control (family_control), with its
        # state re-read and filename check — never the Moonraker path
        # below, whose transport only speaks to Moonraker endpoints.
        from . import family_control
        control = family_control.control_for(alert.get("printer_id"))
        if control is None or control.connection != connection:
            return "That alert belongs to a printer I can't control from here. Nothing sent."
        if action == "resume":
            return control.resume(filename)[1]
        if action == "cancel":
            return control.cancel(filename)[1]
        return "That alert action is not recognized. Nothing sent."
    st = pc.status()
    if action == "resume":
        return pc.resume(filename)[1]
    # cancel
    notes = []
    if st.get("klippy") == "shutdown":
        ok, msg = clear_shutdown()
        notes.append(msg)
        if not ok:
            return "\n".join(notes)
        st = pc.status()
    if st.get("state") in print_stop.ACTIVE:
        if filename and st.get("filename") and st["filename"] != filename:
            notes.append("A different print is loaded now than the one in this alert. Not cancelling it.")
        else:
            notes.append(print_stop.cancel(st.get("filename") or filename)[1])
    else:
        notes.append(f"The job is already gone (printer is {st.get('state')}). Nothing to cancel.")
    return "\n".join(notes)
